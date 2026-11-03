/**
 * hermes-console · 上下文引擎分区（V3c）：真实 UI，替掉「上下文引擎」的空白。
 *
 * 数据全部来自本插件自己的 8 条端点（本文件不发任何别的请求）：
 *   GET  /vcc/conversations    ★按对话的列表（每行 = 一个对话：条数/字符数/压缩几次/召回几次）
 *   GET  /vcc/conversation     ★一个对话的详情（上下文 / 压缩明细 / 召回明细）
 *   GET  /vcc/overview         全 agent 引擎状态 + 最近压缩 + 落库摘要（15s 后端缓存）
 *   GET  /vcc/log              某 agent 的引擎日志尾（原始行，不加工）
 *   GET  /vcc/compaction       一次压缩的输入与输出（栏目 / 引用 / 抽样）
 *   GET  /vcc/engine_status    某 agent 的引擎自检（引擎名读自它自己的 config.yaml）
 *   POST /vcc/engine           切换某 agent 的压缩引擎（唯一写操作，写后回读）
 *   POST /vcc/search_archived  在已归档消息里搜关键词（含工具输出）
 *
 * 六条不许退让的规则：
 *   1. **六个视图**：对话下钻（默认）/ 引擎概览 / 日志 / 压缩状态 / 引擎切换 / 归档检索，
 *      各是各的取数，切视图不重拉（数据走 lib/query.js 的模块级缓存）。
 *   2. **三态齐备**：加载中是骨架屏（占住形状，不白屏）；没有数据是空态（说清为什么空 +
 *      下一步）；请求失败是错误态，**后端 `error.message` 原样透出**（rest.js 已把
 *      `{"ok":false,"error":{code,message}}` 的 message 抽出来），本文件不编原因、不吞成
 *      「加载失败」。
 *   3. **切引擎必须二次确认**：点「切换」只**出现确认条**（写明「当前引擎 → 目标引擎」
 *      与被改的文件），一个字节都不写；再点确认才发 `confirm:true`。
 *   4. **口径不自己编**：引擎名、版本、允许取值、压缩耗时/轮数/字符、引用覆盖全部来自
 *      后端；前端只做展示，不在这里算「大概」。
 *   5. **没有数据就写「无记录」**：token 数本机没落库（`messages.token_count` 全为 NULL），
 *      对话下钻里就显示「无记录」+ 后端给的原因，**不拿字符数折算 token、不给 0 假装有值**。
 *   6. 颜色只走 `var(--ui-*)`（本文件零字面色值），窄窗（<900px）不横向滚动。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { clip, num } from '../lib/format.js'
import { refreshAll, useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/**
 * 六个视图。id 同时用于 `data-vcc-view` / `data-vcc-tab`（探针靠它定位）。
 * `conv`（对话下钻）排第一并做默认视图：老板要的是「一个对话一个对话地看」。
 */
var VIEWS = [
  { id: 'conv', label: '对话下钻', hint: '一个对话一个对话地看' },
  { id: 'overview', label: '引擎概览', hint: '引擎 / 压缩 / 摘要' },
  { id: 'log', label: '日志', hint: '引擎原始行' },
  { id: 'compaction', label: '压缩状态', hint: '栏目 / 引用 / 抽样' },
  { id: 'engine', label: '引擎切换', hint: '写 config.yaml' },
  { id: 'search', label: '归档检索', hint: '含工具输出' }
]

/** 窄窗断点：`max-width: 900px` 以下行改竖排，绝不产生横向滚动。 */
var NARROW_PX = 900
var NARROW_QUERY = '(max-width: 900px)'

/**
 * 订阅窗口宽度是否窄于 :data:`NARROW_PX`。
 * 没有 `matchMedia`（探针里的极简环境）时按「宽」处理：布局不会崩，只是不折叠。
 * @returns {boolean}
 */
function useNarrow() {
  var state = useState(false)
  var narrow = state[0]
  var setNarrow = state[1]
  useEffect(function () {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return undefined
    var mql = window.matchMedia(NARROW_QUERY)
    var sync = function () {
      setNarrow(Boolean(mql.matches))
    }
    sync()
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', sync)
      return function () {
        mql.removeEventListener('change', sync)
      }
    }
    // 老 Safari 只有 addListener
    if (typeof mql.addListener === 'function') {
      mql.addListener(sync)
      return function () {
        mql.removeListener(sync)
      }
    }
    return undefined
  }, [])
  return narrow
}

/**
 * 取后端响应体。api_vcc 的响应是**扁平** `{ok:true, ...字段}`，rest.js 已把整个 body
 * 放进 `data`，所以这里直接用；形状不对当空（不抛、不崩）。
 * @param {{data?: any}} query
 */
function payloadOf(query) {
  var body = query && query.data
  return body && typeof body === 'object' ? body : null
}

/** 视图切换条（原生 button + aria-selected，键盘可达；不新造样式）。 */
function TabBar(props) {
  var narrow = props.narrow
  return jsx('div', {
    role: 'tablist',
    'aria-label': '上下文引擎视图',
    style: {
      display: 'flex',
      flexWrap: 'wrap',
      gap: 'var(--s2)',
      minWidth: 0,
      maxWidth: '100%'
    },
    children: VIEWS.map(function (item) {
      var on = item.id === props.value
      return jsx(
        'button',
        {
          type: 'button',
          role: 'tab',
          'aria-selected': on ? 'true' : 'false',
          'data-vcc-tab': item.id,
          className: 'hc-btn' + (on ? ' hc-btn--accent' : ''),
          title: item.hint,
          onClick: function () {
            props.onChange(item.id)
          },
          children: item.label + (narrow ? '' : '')
        },
        item.id
      )
    })
  })
}

/** 分区入口。`agent` 是顶部作用域；引擎/日志/摘要都按它解析。 */
export function VccSection(props) {
  var agent = typeof props.agent === 'string' ? props.agent : ''
  var ready = props.ready !== false
  var viewState = useState('conv')
  var view = viewState[0]
  var narrow = useNarrow()

  var body
  if (view === 'log') body = jsx(LogView, { agent: agent, ready: ready, narrow: narrow })
  else if (view === 'compaction') body = jsx(CompactionView, { agent: agent, ready: ready, narrow: narrow })
  else if (view === 'engine') body = jsx(EngineView, { agent: agent, ready: ready, narrow: narrow })
  else if (view === 'search') body = jsx(SearchView, { agent: agent, ready: ready, narrow: narrow })
  else if (view === 'conv') body = jsx(ConvView, { agent: agent, ready: ready, narrow: narrow })
  else body = jsx(OverviewView, { agent: agent, ready: ready, narrow: narrow })

  return jsxs('div', {
    className: 'hc-block',
    'data-vcc-view': view,
    // 窄窗（<900px）约束：容器一律 `min-width: 0` + 内容 `overflow-wrap: anywhere`，
    // 表格型内容全部改成 flex-wrap / 竖排，所以整页**不会出现横向滚动条**。
    'data-vcc-narrow': narrow ? 'true' : 'false',
    style: {
      gap: narrow ? 'var(--s2)' : 'var(--s3)',
      minWidth: 0,
      maxWidth: '100%',
      boxSizing: 'border-box',
      overflowWrap: 'anywhere'
    },
    children: [
      jsx(
        TabBar,
        {
          value: view,
          narrow: narrow,
          onChange: function (next) {
            viewState[1](next)
          }
        },
        'tabs'
      ),
      jsx('div', { style: { minWidth: 0, maxWidth: '100%' }, children: body }, 'body')
    ]
  })
}

/** 统计卡（复用概览的 `.hc-card`，不新造一套）。 */
function Stat(props) {
  var children = [
    jsx('div', { className: 'hc-card__label', children: props.label }, 'label'),
    jsx('div', { className: 'hc-card__value', children: props.value }, 'value')
  ]
  if (props.sub) children.push(jsx('div', { className: 'hc-card__sub', children: props.sub }, 'sub'))
  return jsxs('div', { className: 'hc-card', children: children })
}

/** 「标签 → 值」一格；窄窗竖排，长路径 `overflow-wrap: anywhere` 换行而不是撑宽。 */
function Field(props) {
  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: props.narrow ? 'column' : 'row',
      flexWrap: 'wrap',
      alignItems: props.narrow ? 'flex-start' : 'baseline',
      gap: 'var(--s2)',
      minWidth: 0,
      maxWidth: '100%'
    },
    children: [
      jsx('span', { className: 'hc-muted', style: { flex: '0 0 auto' }, children: props.label }, 'k'),
      jsx(
        'span',
        { style: { minWidth: 0, overflowWrap: 'anywhere' }, children: props.value === undefined || props.value === '' ? '—' : props.value },
        'v'
      )
    ]
  })
}

/**
 * 「实际生效引擎」一行文字。**只念日志证据**，不拿配置冒充实际：
 *   * `source='using'`    —— 宿主 `Using context engine: X`（插件真的加载成功）；
 *   * `source='fallback'` —— 宿主 `Context engine 'X' not found — falling back…`
 *     （配置要插件，实际跑的是内置 compressor）。
 * 没有记录就明说没有（宿主 quiet 模式不打这行），**不显示「应该生效的那个」**。
 */
function effectiveEngineText(eff) {
  if (!eff || typeof eff !== 'object' || !eff.engine) {
    return '日志窗口里没有引擎生效记录（宿主 quiet 模式不打 Using context engine 这行；配置值不是实际值）'
  }
  var ts = eff.ts ? ' · 日志 ' + String(eff.ts) : ''
  if (eff.source === 'fallback') {
    return eff.engine + '（回退：' + (eff.requested || '插件') + ' 没加载到）' + ts
  }
  return eff.engine + '（插件已加载）' + ts
}

/** 一个 agent 的引擎卡：引擎 / 来源 / 最近一次压缩 / 窗口计数 / 配置文件路径。 */
function AgentCard(props) {
  var row = props.row || {}
  var last = row.last_compaction && typeof row.last_compaction === 'object' ? row.last_compaction : null
  var warn = (row.degraded || []).length + (row.failures || []).length
  var bits = []
  if (last) {
    if (last.ms !== null && last.ms !== undefined) bits.push('耗时 ' + String(last.ms) + 'ms')
    if (last.chars !== null && last.chars !== undefined) bits.push(num(last.chars) + ' 字符')
    if (last.turns !== null && last.turns !== undefined) bits.push(num(last.turns) + ' 轮')
    if (last.refs !== null && last.refs !== undefined) bits.push(num(last.refs) + ' 个引用')
  }
  return jsxs('div', {
    className: 'hc-card',
    'data-vcc-agent': row.agent || '',
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs(
        'div',
        {
          className: 'hc-block__head',
          children: [
            jsx('span', { className: 'hc-block__title', children: row.agent || '(未命名 agent)' }, 'name'),
            jsx('span', { className: 'hc-chip', children: '引擎 ' + (row.engine || '—') }, 'engine'),
            jsx('span', { className: 'hc-chip', children: row.active ? '目标引擎' : '非目标引擎' }, 'target'),
            // ★ 本卡核心：版本按**这个 agent 自己的** plugins 目录显示（不再全显示全局版本）
            jsx('span', {
              className: 'hc-chip',
              'data-vcc-engine-version': String(row.engine_version_actual || ''),
              children: '插件 ' + (row.engine_installed === false ? '未安装' : (row.engine_version_actual || '版本未声明'))
            }, 'ver'),
            row.engine_mismatch
              ? jsx('span', {
                  className: 'hc-chip',
                  'data-vcc-engine-mismatch': 'true',
                  children: '与全局 ' + (row.engine_version_global || '—') + ' 不一致'
                }, 'drift')
              : null,
            jsx('span', { className: 'hc-chip', children: '落库摘要 ' + num(row.summary_count) }, 'sum')
          ]
        },
        'head'
      ),
      jsx(Field, { narrow: props.narrow, label: '引擎来源', value: row.engine_source }, 'src'),
      jsx(Field, {
        narrow: props.narrow,
        label: '引擎插件（本 agent 目录）',
        value:
          row.engine_installed === false
            ? '未安装：该 agent 自己的 plugins 目录里没有引擎插件（' + (row.engine_plugin_path || '—') + '）⇒ 运行时回退内置 compressor'
            : (row.engine_version_actual || '版本未声明') +
              (row.engine_requires_hermes ? ' · requires_hermes ' + row.engine_requires_hermes : ' · plugin.yaml 里没有 requires_hermes 行') +
              ' · ' + (row.engine_plugin_path || '')
      }, 'ver'),
      // ★ 实际生效引擎：日志证据（后端 using_last / FALLBACK_RE 抽出来的），不是配置值
      jsx(Field, {
        narrow: props.narrow,
        label: '实际生效引擎（日志）',
        value: effectiveEngineText(row.effective_engine)
      }, 'eff'),
      jsx(
        Field,
        {
          narrow: props.narrow,
          label: '最近压缩',
          value: last ? String(last.ts || '') + ' · ' + (bits.join(' · ') || '数字未给') : '这段日志窗口里没有压缩记录'
        },
        'last'
      ),
      jsx(
        Field,
        {
          narrow: props.narrow,
          label: '日志窗口内',
          value:
            '压缩 ' + num(row.compactions_logged) + ' 次 · 摘要调用 ' + num(row.llm_dispatch_in_tail) + ' 次' +
            (warn ? ' · 降级/失败 ' + warn + ' 条' : ' · 无降级无失败')
        },
        'win'
      ),
      jsx(Field, {
        narrow: props.narrow,
        label: 'config.yaml',
        value: String(row.config_path || '') + (row.config_present === false ? '（文件不存在）' : '')
      }, 'cfg')
    ]
  })
}

/**
 * 引擎概览：全 agent 的引擎状态 + 最近压缩。**每行的引擎读自该 agent 自己的
 * config.yaml**（后端 `note` 明说），所以这里不显示任何「全局引擎」。
 */
function OverviewView(props) {
  var query = useQuery(
    'vcc|overview|' + props.agent,
    function () {
      return get('/vcc/overview', { params: { agent: props.agent, recent: 8 } })
    },
    { staleMs: 15000, enabled: props.ready }
  )

  if (query.status === 'loading') {
    return jsx(Skeleton, { lines: 4, cards: 2, label: '正在读取引擎状态' })
  }
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '引擎概览没读到',
      message: query.error,
      detail: 'GET /vcc/overview',
      onRetry: query.reload
    })
  }
  var data = payloadOf(query)
  if (!data || !Array.isArray(data.profiles)) {
    return jsx(ErrorState, {
      title: '后端返回的结构不对',
      message: '/vcc/overview 没有返回 profiles 数组，面板无法判断引擎状态（不是「没有 agent」）',
      detail: 'GET /vcc/overview',
      onRetry: query.reload
    })
  }

  var rows = data.profiles
  var plugin = data.engine_plugin && typeof data.engine_plugin === 'object' ? data.engine_plugin : {}
  var globalVersion = data.engine_version_global || plugin.version || ''
  var drift = rows.filter(function (row) {
    return row.engine_mismatch === true
  })
  var compactions = 0
  var summaries = 0
  rows.forEach(function (row) {
    compactions += Number(row.compactions_logged) || 0
    summaries += Number(row.summary_count) || 0
  })

  var head = jsxs(
    'div',
    {
      className: 'hc-block__head',
      children: [
        jsx('span', { className: 'hc-block__title', children: '引擎状态（按 agent 各读自己的 config.yaml 与自己的 plugins 目录）' }, 'title'),
        jsx('span', { className: 'hc-chip', children: '目标引擎 ' + (data.engine_target || '—') }, 'target'),
        // 全局那一份只用来标漂移；各 agent 的版本在卡片里各显各的
        jsx('span', {
          className: 'hc-chip',
          'data-vcc-global-version': String(globalVersion || ''),
          children: '全局 ' + (plugin.name || '引擎插件') + ' ' + (globalVersion || '版本未读到')
        }, 'plugin'),
        jsx('span', { className: 'hc-muted', children: '更新于 ' + (data.generated_at || '—') }, 'at'),
        jsx(
          'button',
          {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            onClick: function () {
              refreshAll('vcc|')
            },
            children: '刷新'
          },
          'refresh'
        )
      ]
    },
    'head'
  )

  // 版本漂移：面板以前按全局目录报绿，对只装了旧版的 agent 是假健康；这里显式点名
  var driftNotice = drift.length
    ? jsx('div', {
        className: 'hc-notice',
        role: 'status',
        'data-vcc-drift': String(drift.length),
        children: jsx('span', {
          className: 'hc-notice__text',
          style: { overflowWrap: 'anywhere' },
          children:
            drift.length + ' 个 agent 的引擎插件与全局 ' + (globalVersion || '—') + ' 不一致：' +
            drift.map(function (row) {
              return row.agent + '=' + (row.engine_installed === false ? '未安装' : (row.engine_version_actual || '版本未声明'))
            }).join('、') +
            '。运行时按各自的 plugins 目录解析，切引擎前会校验本体存在。'
        })
      }, 'drift')
    : null

  if (!rows.length) {
    return jsxs('div', {
      className: 'hc-block',
      style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
      children: [
        head,
        jsx(Empty, {
          title: '一个 agent 都没扫到',
          hint: '后端按 <home>/profiles/* 动态扫描 agent，扫不到说明该目录下没有 profile。先建一个 profile 再看；也可以点上面的「刷新」重扫。',
          action: { label: '重新扫描', onClick: function () { query.reload() } }
        })
      ]
    })
  }

  var cards = [
    jsx(Stat, { label: 'agent 数', value: num(data.count), sub: '每个各有一份 config.yaml' }, 'n'),
    jsx(Stat, { label: '目标引擎', value: data.engine_target || '—', sub: plugin.available ? '插件自述：' + (plugin.name || '') : '引擎插件 plugin.yaml 没读到（用了兜底名）' }, 'e'),
    jsx(Stat, {
      label: '引擎插件版本',
      value: '全局 ' + (globalVersion || '—'),
      sub: drift.length
        ? '按各自目录：' + (rows.length - drift.length) + ' 个与全局一致，' + drift.length + ' 个不一致'
        : '9 个 agent 都与全局一致'
    }, 'v'),
    jsx(Stat, { label: '日志窗口内压缩', value: num(compactions) + ' 次', sub: '口径：当前日志尾巴（约 1.2MB/文件），不是历史累计' }, 'c'),
    jsx(Stat, { label: '落库摘要', value: num(summaries) + ' 条', sub: '严格判定过的压缩摘要（到「压缩状态」逐条看）' }, 's')
  ]

  return jsxs('div', {
    className: 'hc-block',
    style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [
      head,
      driftNotice,
      jsx('div', { className: 'hc-cards', children: cards }, 'cards'),
      jsx(
        'div',
        {
          style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
          children: rows.map(function (row) {
            return jsx(AgentCard, { row: row, narrow: props.narrow }, String(row.agent || ''))
          })
        },
        'rows'
      )
    ]
  })
}

/** 日志：某 agent 的引擎相关**原始行**（后端不加工，这里也不改写）。 */
function LogView(props) {
  var limitState = useState(80)
  var limit = limitState[0]
  var filterState = useState('')
  var filter = filterState[0]
  var query = useQuery(
    'vcc|log|' + props.agent + '|' + limit,
    function () {
      return get('/vcc/log', { params: { agent: props.agent, limit: limit } })
    },
    { staleMs: 10000, enabled: props.ready }
  )

  if (query.status === 'loading') {
    return jsx(Skeleton, { lines: 6, label: '正在读取引擎日志' })
  }
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '引擎日志没读到',
      message: query.error,
      detail: 'GET /vcc/log',
      onRetry: query.reload
    })
  }
  var data = payloadOf(query)
  if (!data || !Array.isArray(data.lines)) {
    return jsx(ErrorState, {
      title: '后端返回的结构不对',
      message: '/vcc/log 没有返回 lines 数组，面板无法判断是「没日志」还是「接口变了」',
      detail: 'GET /vcc/log',
      onRetry: query.reload
    })
  }

  var lines = data.lines
  var needle = filter.trim().toLowerCase()
  var shown = needle
    ? lines.filter(function (line) {
        return String(line).toLowerCase().indexOf(needle) !== -1
      })
    : lines

  var toolbar = jsxs('div', {
    className: 'hc-block__head',
    children: [
      jsx('span', { className: 'hc-block__title', children: '引擎日志尾' }, 'title'),
      jsx('span', { className: 'hc-chip', children: '引擎 ' + (data.engine || '—') }, 'engine'),
      jsx('span', { className: 'hc-chip', children: num(lines.length) + ' 行' }, 'count'),
      jsx(
        'select',
        {
          className: 'hc-select',
          'aria-label': '日志行数上限',
          value: String(limit),
          onChange: function (event) {
            limitState[1](Number(event.target.value) || 80)
          },
          children: [80, 200, 300].map(function (value) {
            return jsx('option', { value: String(value), children: '最多 ' + value + ' 行' }, String(value))
          })
        },
        'limit'
      ),
      jsx('input', {
        type: 'search',
        className: 'hc-select',
        'aria-label': '在本页日志里过滤',
        placeholder: '过滤本页（关键字）',
        value: filter,
        style: { minWidth: 0, maxWidth: '100%' },
        onChange: function (event) {
          filterState[1](event.target.value)
        }
      }, 'filter'),
      jsx('button', { type: 'button', className: 'hc-btn hc-btn--tiny', onClick: query.reload, children: '刷新' }, 'refresh')
    ]
  }, 'head')

  var foot = jsx('div', {
    className: 'hc-muted',
    style: { overflowWrap: 'anywhere', minWidth: 0, maxWidth: '100%' },
    children: '口径：该 agent 自己的 logs/*（agent / gateway / desktop / errors）尾部约 1.2MB 里含引擎关键字的行，未做任何摘要；config.yaml = ' + (data.config_path || '—')
  }, 'foot')

  if (!lines.length) {
    return jsxs('div', {
      className: 'hc-block',
      style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
      children: [
        toolbar,
        jsx(Empty, {
          title: '这段日志尾里没有引擎相关行',
          hint: '引擎只在压缩发生时才写日志。要么这个 agent 近期没触发压缩，要么日志被轮转掉了。先去「引擎概览」看该 agent 的最近压缩时间；日志文件在 ' + (data.config_path ? String(data.config_path).replace(/config\.yaml$/, 'logs/') : 'logs/') + '。',
          action: { label: '重新读取', onClick: function () { query.reload() } }
        }),
        foot
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-block',
    style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [
      toolbar,
      shown.length
        ? jsx(
            'div',
            {
              className: 'hc-rows',
              role: 'log',
              'aria-label': '引擎日志原始行',
              style: { maxHeight: '420px', minWidth: 0, maxWidth: '100%' },
              children: shown.map(function (line, index) {
                return jsx(
                  'div',
                  {
                    style: {
                      padding: 'var(--s1) var(--s3)',
                      borderTop: index ? 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)' : undefined,
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                      fontSize: 'var(--fs-xs)',
                      lineHeight: '1.5',
                      overflowWrap: 'anywhere',
                      wordBreak: 'break-word',
                      minWidth: 0,
                      maxWidth: '100%'
                    },
                    children: String(line)
                  },
                  'l' + index
                )
              })
            },
            'lines'
          )
        : jsx(Empty, {
            title: '这 ' + num(lines.length) + ' 行里没有含「' + filter.trim() + '」的',
            hint: '清掉过滤词就能看全部；过滤只作用于本页已读到的行，不会重新向后端要更多。',
            action: { label: '清空过滤', onClick: function () { filterState[1]('') } }
          }, 'none'),
      foot
    ]
  })
}

/** 摘要选择条：一条摘要一个按钮（`#id · 时间 · 字符 · 引用`）。 */
function SummaryPicker(props) {
  return jsxs('div', {
    style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsx('span', { className: 'hc-muted', style: { flex: '0 0 auto' }, children: '选一条摘要' }, 'label'),
      props.items.map(function (item) {
        var on = item.message_id === props.value
        return jsx(
          'button',
          {
            type: 'button',
            'data-vcc-summary': String(item.message_id),
            'aria-pressed': on ? 'true' : 'false',
            className: 'hc-btn hc-btn--tiny' + (on ? ' hc-btn--accent' : ''),
            onClick: function () {
              props.onPick(item.message_id)
            },
            children:
              '#' + item.message_id + ' · ' + String(item.ts || '') + ' · ' + num(item.chars) + ' 字符 · ' + num(item.refs) + ' 引用' +
              (item.ours ? '' : '（旧格式）')
          },
          String(item.message_id)
        )
      })
    ]
  })
}

/** 一行「原消息」：`#id 角色 · 字符 · 时间 · 开头`（长文本换行，不撑宽）。 */
function MessageLine(props) {
  var item = props.item || {}
  return jsxs('div', {
    style: {
      display: 'flex',
      flexDirection: props.narrow ? 'column' : 'row',
      gap: 'var(--s2)',
      padding: 'var(--s1) 0',
      borderTop: props.first ? undefined : 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
      minWidth: 0,
      maxWidth: '100%',
      fontSize: 'var(--fs-sm)'
    },
    children: [
      jsx('span', {
        style: { flex: '0 0 auto', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 'var(--fs-xs)' },
        children: '#' + (item.id === undefined ? '—' : item.id) + ' ' + (item.role || '')
      }, 'id'),
      jsx('span', {
        className: 'hc-muted',
        style: { flex: '0 0 auto' },
        children: num(item.chars) + ' 字符 · ' + (item.ts || '—')
      }, 'meta'),
      jsx('span', {
        style: { minWidth: 0, overflowWrap: 'anywhere' },
        children: clip(item.brief_line || item.preview || '（空）', props.narrow ? 160 : 240)
      }, 'text')
    ]
  })
}

/**
 * 压缩状态：一次压缩的**输入与输出**。
 * 摘要列表来自 `/vcc/overview` 的 `summaries`（同一份缓存，不额外请求），
 * 选中哪条就按 `message_id` 拉 `/vcc/compaction` 的详情。
 */
function CompactionView(props) {
  var overviewQuery = useQuery(
    'vcc|overview|' + props.agent,
    function () {
      return get('/vcc/overview', { params: { agent: props.agent, recent: 8 } })
    },
    { staleMs: 15000, enabled: props.ready }
  )
  var pickState = useState(null)
  var picked = pickState[0]

  var overview = payloadOf(overviewQuery)
  var rows = overview && Array.isArray(overview.profiles) ? overview.profiles : []
  var row = null
  for (var i = 0; i < rows.length; i += 1) {
    if (!props.agent || rows[i].agent === props.agent) {
      row = rows[i]
      break
    }
  }
  var summaries = row && Array.isArray(row.summaries) ? row.summaries : []
  var selected = picked !== null ? picked : summaries.length ? summaries[0].message_id : null

  var detailQuery = useQuery(
    'vcc|compaction|' + props.agent + '|' + String(selected),
    function () {
      return get('/vcc/compaction', { params: { message_id: selected, agent: props.agent, sample: 60 } })
    },
    { staleMs: 60000, enabled: props.ready && selected !== null }
  )

  if (overviewQuery.status === 'loading') {
    return jsx(Skeleton, { lines: 5, label: '正在读取压缩记录' })
  }
  if (overviewQuery.status === 'error') {
    return jsx(ErrorState, {
      title: '压缩记录没读到',
      message: overviewQuery.error,
      detail: 'GET /vcc/overview',
      onRetry: overviewQuery.reload
    })
  }
  if (!summaries.length) {
    return jsx(Empty, {
      title: (props.agent || '当前 agent') + ' 没有落库的压缩摘要',
      hint: '摘要只在触发过压缩之后才写进 state.db。先去「引擎概览」确认该 agent 的引擎与最近压缩时间；如果引擎是内置 compressor，摘要不会有栏目结构，这里也就没有可挑的条目。',
      action: { label: '重新扫描', onClick: function () { overviewQuery.reload() } }
    })
  }

  var head = jsxs('div', {
    className: 'hc-block__head',
    children: [
      jsx('span', { className: 'hc-block__title', children: '一次压缩的输入与输出' }, 'title'),
      jsx('span', { className: 'hc-chip', children: (row && row.engine) || '—' }, 'engine'),
      jsx('span', { className: 'hc-muted', children: '共 ' + num(summaries.length) + ' 条摘要可看' }, 'count')
    ]
  }, 'head')

  var picker = jsx(SummaryPicker, {
    items: summaries,
    value: selected,
    onPick: function (next) {
      pickState[1](next)
    }
  }, 'picker')

  if (detailQuery.status === 'loading') {
    return jsxs('div', {
      className: 'hc-block',
      style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
      children: [head, picker, jsx(Skeleton, { lines: 6, label: '正在读取这条压缩的详情' })]
    })
  }
  if (detailQuery.status === 'error') {
    return jsxs('div', {
      className: 'hc-block',
      style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
      children: [
        head,
        picker,
        jsx(ErrorState, {
          title: '这条压缩没读到',
          message: detailQuery.error,
          detail: 'GET /vcc/compaction?message_id=' + selected,
          onRetry: detailQuery.reload
        })
      ]
    })
  }
  var detail = payloadOf(detailQuery)
  if (!detail || !detail.summary) {
    return jsxs('div', {
      className: 'hc-block',
      style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
      children: [
        head,
        picker,
        jsx(ErrorState, {
          title: '后端返回的结构不对',
          message: '/vcc/compaction 没有返回 summary 字段，无法逐条核对这次压缩',
          detail: 'GET /vcc/compaction?message_id=' + selected,
          onRetry: detailQuery.reload
        })
      ]
    })
  }

  var summary = detail.summary || {}
  var win = detail.window || {}
  var cited = detail.cited || {}
  var kept = detail.kept_tail || {}
  var sections = Array.isArray(summary.sections) ? summary.sections : []
  var brief = Array.isArray(summary.brief_lines) ? summary.brief_lines : []
  var citedRows = Array.isArray(detail.cited_rows) ? detail.cited_rows : []
  var sampled = Array.isArray(detail.sampled_rows) ? detail.sampled_rows : []
  var roleText = Object.keys(win.by_role || {})
    .map(function (key) {
      return key + ' ' + num(win.by_role[key])
    })
    .join(' / ')

  return jsxs('div', {
    className: 'hc-block',
    style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [
      head,
      picker,
      jsxs('div', {
        className: 'hc-card',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            children: [
              jsx('span', { className: 'hc-block__title', children: '摘要 #' + (summary.message_id === undefined ? '—' : summary.message_id) }, 'title'),
              jsx('span', { className: 'hc-chip', children: '会话 ' + (detail.session || '—') }, 'session'),
              jsx('span', { className: 'hc-chip', children: (summary.role || '') + ' · ' + num(summary.chars) + ' 字符' }, 'chars'),
              jsx('span', { className: 'hc-chip', children: '引用 ' + num(summary.refs) + ' 个（去重 ' + num(summary.unique_refs) + '）' }, 'refs'),
              jsx('span', { className: 'hc-chip', children: '压缩比 ' + (detail.ratio_pct === undefined ? '—' : detail.ratio_pct + '%') }, 'ratio')
            ]
          }, 'head'),
          jsx(Field, { narrow: props.narrow, label: '时间', value: summary.ts }, 'ts'),
          jsx(Field, {
            narrow: props.narrow,
            label: '压掉的消息',
            value: num(win.rows) + ' 条 · ' + num(win.chars) + ' 字符 · 区间 #' + (win.first_id === undefined || win.first_id === null ? '—' : win.first_id) + ' → #' + (win.last_id === undefined || win.last_id === null ? '—' : win.last_id) + (roleText ? ' · 按角色 ' + roleText : '')
          }, 'win'),
          jsx(Field, {
            narrow: props.narrow,
            label: '留下的尾部',
            value: num(kept.rows) + ' 条（会话共 ' + num(detail.session_total_rows) + ' 条）'
          }, 'tail'),
          jsx(Field, {
            narrow: props.narrow,
            label: '引用覆盖',
            value: num(cited.count) + ' 条被点名（' + (cited.coverage_pct === undefined ? '—' : cited.coverage_pct + '%') + '）—— 覆盖率低不等于丢数据，其余消息仍在库里，可到「归档检索」搜'
          }, 'cited')
        ]
      }, 'meta'),
      sections.length
        ? jsxs('div', {
            className: 'hc-card',
            style: { minWidth: 0, maxWidth: '100%' },
            children: [
              jsx('div', { className: 'hc-block__head', children: jsx('span', { className: 'hc-block__title', children: '抽出的栏目（摘要上半部分）' }) }, 'head'),
              jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
                children: sections.map(function (section) {
                  return jsxs('div', {
                    style: { minWidth: 0, maxWidth: '100%' },
                    children: [
                      jsx('span', { className: 'hc-chip', children: '[' + section.name + ']' }, 'name'),
                      jsx('ul', {
                        style: { margin: 'var(--s1) 0 0', paddingLeft: 'var(--s4)', minWidth: 0, overflowWrap: 'anywhere' },
                        children: (section.items || []).map(function (text, index) {
                          return jsx('li', { children: text }, 'i' + index)
                        })
                      }, 'items')
                    ]
                  }, section.name)
                })
              }, 'list')
            ]
          }, 'sections')
        : jsxs('div', {
            className: 'hc-card',
            style: { minWidth: 0, maxWidth: '100%' },
            children: [
              jsx('div', {
                className: 'hc-notice',
                children: jsx('span', {
                  className: 'hc-notice__text',
                  children: '这条摘要没有栏目结构（旧格式 / 内置 LLM 时期留下的）：下面给原文片段，不做栏目解读。'
                })
              }, 'notice'),
              jsx('div', {
                style: {
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                  fontSize: 'var(--fs-xs)',
                  lineHeight: '1.5',
                  whiteSpace: 'pre-wrap',
                  overflowWrap: 'anywhere',
                  wordBreak: 'break-word',
                  minWidth: 0,
                  maxWidth: '100%',
                  maxHeight: '420px',
                  overflowY: 'auto'
                },
                children: detail.raw_excerpt
                  ? clip(String(detail.raw_excerpt), props.narrow ? 1500 : 4000)
                  : '（后端没给原文片段）'
              }, 'raw')
            ]
          }, 'legacy'),
      brief.length
        ? jsxs('div', {
            className: 'hc-card',
            style: { minWidth: 0, maxWidth: '100%' },
            children: [
              jsx('div', { className: 'hc-block__head', children: jsx('span', { className: 'hc-block__title', children: '简要转录 ' + num(brief.length) + ' 行（每行 = 一条被点名的原始消息）' }) }, 'head'),
              jsx('div', {
                style: { display: 'flex', flexDirection: 'column', minWidth: 0, maxWidth: '100%' },
                children: brief.map(function (line, index) {
                  return jsx('div', {
                    style: { overflowWrap: 'anywhere', fontSize: 'var(--fs-sm)', padding: 'var(--s1) 0' },
                    children: line
                  }, 'b' + index)
                })
              }, 'list')
            ]
          }, 'brief')
        : null,
      citedRows.length
        ? jsxs('div', {
            className: 'hc-card',
            style: { minWidth: 0, maxWidth: '100%' },
            children: [
              jsx('div', { className: 'hc-block__head', children: jsx('span', { className: 'hc-block__title', children: '被点名的原始消息（左 = 摘要那行，右 = DB 原文开头）' }) }, 'head'),
              jsx('div', {
                style: { display: 'flex', flexDirection: 'column', minWidth: 0, maxWidth: '100%' },
                children: citedRows.map(function (item, index) {
                  return jsx(MessageLine, { item: item, narrow: props.narrow, first: index === 0 }, 'c' + index)
                })
              }, 'list')
            ]
          }, 'cited')
        : null,
      sampled.length
        ? jsxs('div', {
            className: 'hc-card',
            style: { minWidth: 0, maxWidth: '100%' },
            children: [
              jsx('div', { className: 'hc-block__head', children: jsx('span', { className: 'hc-block__title', children: '没被点名的消息（抽样 ' + num(sampled.length) + ' 条）：没丢，只是没进摘要 —— 已归档，可搜' }) }, 'head'),
              jsx('div', {
                style: { display: 'flex', flexDirection: 'column', minWidth: 0, maxWidth: '100%' },
                children: sampled.map(function (item, index) {
                  return jsx(MessageLine, { item: item, narrow: props.narrow, first: index === 0 }, 's' + index)
                })
              }, 'list')
            ]
          }, 'sampled')
        : null
    ]
  })
}

/**
 * 二次确认条：点「切换」只**出现这一条**，一个请求都不发；再点确认才写文件。
 * 文案必须写明「当前引擎 → 目标引擎」和被改的文件，`data-confirm` 供探针定位。
 */
function EngineConfirm(props) {
  return jsxs('div', {
    className: 'hc-notice',
    role: 'group',
    'aria-label': '确认切换压缩引擎',
    'data-confirm': 'vcc-engine',
    style: { flexDirection: 'column', alignItems: 'stretch', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsx('span', {
        className: 'hc-notice__text',
        'data-confirm-q': 'vcc-engine',
        style: { overflowWrap: 'anywhere' },
        children:
          '确认把 ' + (props.agent || '当前 agent') + ' 的压缩引擎从「' + (props.from || '—') + '」切成「' + (props.to || '—') + '」？' +
          '会改写该 agent 自己的 config.yaml（context.engine），正在跑的会话仍用旧引擎、新会话生效。'
      }, 'q'),
      jsx('span', {
        className: 'hc-muted',
        style: { overflowWrap: 'anywhere' },
        children: '目标文件：' + (props.configPath || '—')
      }, 'path'),
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', minWidth: 0 },
        children: [
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--accent',
            'data-confirm-yes': 'vcc-engine',
            disabled: props.pending ? true : undefined,
            onClick: props.onConfirm,
            children: props.pending ? '写入中…' : '确认切换'
          }, 'yes'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn',
            disabled: props.pending ? true : undefined,
            onClick: props.onCancel,
            children: '取消'
          }, 'no')
        ]
      }, 'ops')
    ]
  })
}

/**
 * 引擎切换：**本分区唯一的写操作**，两次点击才落地。
 *   1. 点某个引擎按钮 → 只出现 :func:`EngineConfirm`（当前 → 目标 + 被改文件），不发请求；
 *   2. 点「确认切换」→ 才 `POST /vcc/engine`，body 带 `confirm: true`（后端缺它直接 400）。
 * 失败时把后端 `error.message` 原样贴出来，并显示 `error.code`（如 write_not_applied）。
 */
function EngineView(props) {
  var statusQuery = useQuery(
    'vcc|engine|' + props.agent,
    function () {
      return get('/vcc/engine_status', { params: { agent: props.agent } })
    },
    { staleMs: 15000, enabled: props.ready }
  )
  var targetState = useState('')
  var pendingState = useState(false)
  var resultState = useState(null)
  var target = targetState[0]
  var pending = pendingState[0]
  var result = resultState[0]

  if (statusQuery.status === 'loading') {
    return jsx(Skeleton, { lines: 3, cards: 1, label: '正在读取引擎配置' })
  }
  if (statusQuery.status === 'error') {
    return jsx(ErrorState, {
      title: '引擎配置没读到',
      message: statusQuery.error,
      detail: 'GET /vcc/engine_status',
      onRetry: statusQuery.reload
    })
  }
  var data = payloadOf(statusQuery)
  if (!data || typeof data.engine !== 'string') {
    return jsx(ErrorState, {
      title: '后端返回的结构不对',
      message: '/vcc/engine_status 没有返回 engine 字段，面板无法判断当前引擎（不是「没有引擎」）',
      detail: 'GET /vcc/engine_status',
      onRetry: statusQuery.reload
    })
  }

  var current = data.engine || ''
  var plugin = data.engine_plugin && typeof data.engine_plugin === 'object' ? data.engine_plugin : {}
  // ★ 该 agent 自己目录里那一份（不是全局那份）：没装就不许切，后端也会 409
  var installed = data.engine_installed !== false
  var actualVersion = data.engine_version_actual || ''
  var globalVersion = data.engine_version_global || plugin.version || ''
  var effective = data.effective_engine && typeof data.effective_engine === 'object' ? data.effective_engine : null
  var allowed = ['compressor']
  if (data.engine_target) allowed.push(data.engine_target)
  if (current && allowed.indexOf(current) === -1) allowed.push(current)

  /** 只发请求的那一步：**只有确认条上的按钮会走到这里**。 */
  function apply(next) {
    pendingState[1](true)
    targetState[1]('')
    send('/vcc/engine', { agent: props.agent, engine: next, confirm: true }).then(function (outcome) {
      pendingState[1](false)
      if (outcome && outcome.ok === false) {
        resultState[1]({
          tone: 'error',
          title: '切换没成功',
          message: outcome.message,
          code: outcome.code || outcome.errorCode || ''
        })
        return
      }
      var done = outcome && outcome.data ? outcome.data : {}
      resultState[1]({
        tone: 'ok',
        title: done.changed ? '已切换（回读确认）' : '未改动',
        message:
          (done.message || '已提交') +
          (done.note ? ' · ' + done.note : '') +
          ' · 引擎 = ' + (done.engine || '') +
          ' · config.yaml = ' + (done.config_path || ''),
        code: ''
      })
      // 引擎变了：总览/日志/摘要全部作废（它们都按引擎解析）
      refreshAll('vcc|')
    })
  }

  var context = data.context && typeof data.context === 'object' ? data.context : null
  var last = data.last_compaction && typeof data.last_compaction === 'object' ? data.last_compaction : null

  var feedback = null
  if (result) {
    feedback = jsxs('div', {
      className: result.tone === 'ok' ? 'hc-notice' : 'hc-notice',
      role: result.tone === 'ok' ? 'status' : 'alert',
      'data-vcc-engine-result': result.tone,
      style: { minWidth: 0, maxWidth: '100%' },
      children: [
        jsx('span', { className: 'hc-notice__text', style: { overflowWrap: 'anywhere' }, children: result.title + '：' + result.message }, 'text'),
        result.code ? jsx('span', { className: 'hc-chip', children: '错误码 ' + result.code }, 'code') : null
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-block',
    style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        className: 'hc-block__head',
        children: [
          jsx('span', { className: 'hc-block__title', children: '压缩引擎（按 agent 写自己的 config.yaml）' }, 'title'),
          jsx('span', { className: 'hc-chip', children: (props.agent || 'default') }, 'agent'),
          jsx('span', { className: 'hc-chip', children: '当前 ' + (current || '—') }, 'cur'),
          jsx('span', { className: 'hc-chip', children: '目标 ' + (data.engine_target || '—') }, 'tgt'),
          jsx('span', {
            className: 'hc-chip',
            'data-vcc-effective-engine': effective ? String(effective.engine || '') : '',
            children: '实际生效 ' + (effective ? effective.engine : '无记录')
          }, 'eff'),
          jsx('button', { type: 'button', className: 'hc-btn hc-btn--tiny', onClick: statusQuery.reload, children: '刷新' }, 'refresh')
        ]
      }, 'head'),
      feedback,
      data.warning
        ? jsx('div', {
            className: 'hc-notice',
            children: jsx('span', { className: 'hc-notice__text', style: { overflowWrap: 'anywhere' }, children: String(data.warning) })
          }, 'warn')
        : null,
      jsxs('div', {
        className: 'hc-card',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsx(Field, { narrow: props.narrow, label: '当前引擎', value: current + '（来源 ' + (data.engine_source || '—') + '）' }, 'cur'),
          jsx(Field, {
            narrow: props.narrow,
            label: '引擎插件（本 agent 目录）',
            value: installed
              ? (actualVersion || '版本未声明') +
                (data.engine_requires_hermes ? ' · requires_hermes ' + data.engine_requires_hermes : ' · plugin.yaml 里没有 requires_hermes 行') +
                ' · ' + (data.engine_plugin_path || '')
              : '未安装（' + (data.engine_plugin_path || '—') + ' 不存在）⇒ 切过去只会回退内置 compressor，后端会 409 engine_not_installed'
          }, 'plugin'),
          jsx(Field, {
            narrow: props.narrow,
            label: '实际生效引擎（日志）',
            value: effectiveEngineText(effective)
          }, 'eff'),
          jsx(Field, {
            narrow: props.narrow,
            label: '与全局对比',
            value: installed
              ? (actualVersion || '—') + ' / 全局 ' + (globalVersion || '—') +
                (data.engine_mismatch ? '（不一致：该 agent 装的是旧版，面板按它自己的目录显示）' : '（一致）')
              : '该 agent 没有引擎插件 / 全局 ' + (globalVersion || '—')
          }, 'ver'),
          jsx(Field, { narrow: props.narrow, label: '配置文件', value: (data.config_path || '—') + (data.config_present ? '' : '（文件不存在）') }, 'cfg'),
          jsx(Field, { narrow: props.narrow, label: 'context 块', value: context ? JSON.stringify(context) : '—' }, 'ctx'),
          jsx(Field, {
            narrow: props.narrow,
            label: '最近一次压缩',
            value: last
              ? String(last.ts || '') + ' · ' + (last.ms === null || last.ms === undefined ? '耗时未记录' : String(last.ms) + 'ms') + ' · ' + num(last.chars) + ' 字符 · ' + num(last.refs) + ' 个引用'
              : '没有可读的压缩记录'
          }, 'last')
        ]
      }, 'meta'),
      jsxs('div', {
        className: 'hc-card',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsx('div', { className: 'hc-block__head', children: jsx('span', { className: 'hc-block__title', children: '切换（先点一次，再确认）' }) }, 'head'),
          jsx('div', {
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
            children: allowed.map(function (name) {
              var isCurrent = name === current
              // 切到插件引擎前先看该 agent 自己有没有本体：没有就不让点（后端同样 409）
              var missing = name !== 'compressor' && !installed
              var blocked = isCurrent || missing
              return jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-btn' + (isCurrent ? ' hc-btn--done' : ''),
                  'data-vcc-engine-choice': name,
                  disabled: blocked || pending ? true : undefined,
                  title: isCurrent
                    ? '当前就是这个引擎'
                    : missing
                      ? '该 agent 自己的 plugins 目录里没有引擎插件，切过去只会回退内置 compressor（后端会 409 engine_not_installed）'
                      : '切到 ' + name + '（会先要你确认）',
                  onClick: function () {
                    // ★ 只改「待确认」状态：此处**不发任何请求**
                    resultState[1](null)
                    targetState[1](name)
                  },
                  children: isCurrent ? name + '（当前）' : missing ? name + '（本 agent 未安装）' : '切到 ' + name
                },
                name
              )
            })
          }, 'choices'),
          target
            ? jsx(EngineConfirm, {
                agent: props.agent,
                from: current,
                to: target,
                configPath: data.config_path,
                pending: pending,
                onConfirm: function () {
                  apply(target)
                },
                onCancel: function () {
                  targetState[1]('')
                }
              }, 'confirm')
            : jsx('div', {
                className: 'hc-muted',
                style: { overflowWrap: 'anywhere' },
                children: '点上面任一个「切到 …」只会出现确认条，不会立刻写文件；确认条上再点「确认切换」才发 POST /vcc/engine（confirm: true）。'
              }, 'hint')
        ]
      }, 'switch')
    ]
  })
}

/**
 * 归档检索：在**已归档**消息里按关键词搜（含工具输出）。
 *
 * 为什么需要它：摘要只点名了几条消息（引用覆盖率可能只有 3%），面板必须能证明
 * 「其余消息没丢，只是没进目录」。搜索是 POST（后端要 body），所以不用 useQuery，
 * 结果自己记状态；三种结局都有落点：没搜过 / 没命中 / 失败（后端原话）。
 */
function SearchView(props) {
  var textState = useState('')
  var sessionState = useState('')
  var pendingState = useState(false)
  var resultState = useState(null)
  var text = textState[0]
  var session = sessionState[0]
  var pending = pendingState[0]
  var result = resultState[0]

  function run() {
    var needle = text.trim()
    if (!needle) {
      resultState[1]({
        ok: false,
        code: 'invalid_argument',
        message: '关键词为空：至少给一个词（命令、报错、文件名都可以；多词是 OR 匹配）'
      })
      return
    }
    pendingState[1](true)
    send('/vcc/search_archived', { agent: props.agent, query: needle, session: session.trim() }).then(function (outcome) {
      pendingState[1](false)
      resultState[1](outcome || { ok: false, message: '后端没有返回内容', code: 'empty' })
    })
  }

  var form = jsxs('div', {
    className: 'hc-block__head',
    children: [
      jsx('span', { className: 'hc-block__title', children: '在这段归档里搜关键词' }, 'title'),
      jsx('input', {
        type: 'search',
        className: 'hc-select',
        'aria-label': '搜索关键词',
        placeholder: '命令 / 报错 / 文件名…',
        value: text,
        style: { minWidth: 0, maxWidth: '100%' },
        onChange: function (event) {
          textState[1](event.target.value)
        },
        onKeyDown: function (event) {
          if (event.key === 'Enter') run()
        }
      }, 'q'),
      jsx('input', {
        type: 'search',
        className: 'hc-select',
        'aria-label': '限定会话 id（可留空）',
        placeholder: '会话 id（留空 = 全库）',
        value: session,
        style: { minWidth: 0, maxWidth: '100%' },
        onChange: function (event) {
          sessionState[1](event.target.value)
        }
      }, 's'),
      jsx('button', {
        type: 'button',
        className: 'hc-btn hc-btn--accent',
        disabled: pending ? true : undefined,
        onClick: run,
        children: pending ? '搜索中…' : '搜索这些消息'
      }, 'go')
    ]
  }, 'form')

  var note = jsx('div', {
    className: 'hc-muted',
    style: { overflowWrap: 'anywhere', minWidth: 0, maxWidth: '100%' },
    children: '口径：搜的是该 agent 的整库（含被压缩归档的消息），后端固定带 role_filter=user,assistant,tool —— 默认的 session_search 只搜 user/assistant，工具输出会搜不到。'
  }, 'note')

  var body
  if (pending) {
    body = jsx(Skeleton, { lines: 3, label: '正在搜索归档消息' })
  } else if (!result) {
    body = jsx(Empty, {
      title: '还没搜过',
      hint: '输入一个关键词再点「搜索这些消息」。引用覆盖率低不等于丢数据：没进摘要的消息都还在库里，这里能直接搜到（包括工具输出）。'
    })
  } else if (result.ok === false) {
    body = jsx(ErrorState, {
      title: '搜索没成功',
      message: result.message,
      detail: 'POST /vcc/search_archived' + (result.code ? ' · ' + result.code : ''),
      onRetry: run
    })
  } else {
    var data = result.data && typeof result.data === 'object' ? result.data : null
    var hits = data && Array.isArray(data.hits) ? data.hits : null
    if (!hits) {
      body = jsx(ErrorState, {
        title: '后端返回的结构不对',
        message: '/vcc/search_archived 没有返回 hits 数组，无法判断是「没命中」还是「接口变了」',
        detail: 'POST /vcc/search_archived',
        onRetry: run
      })
    } else if (!hits.length) {
      body = jsx(Empty, {
        title: '没有命中「' + (data.query || text.trim()) + '」',
        hint: '换个更短、更具体的词试试（多词是 OR 匹配）；也可以清空「会话 id」把范围放到全库。确实没有的话，说明这段关键词本来就没进过这个 agent 的消息。',
        action: { label: '换个词再搜', onClick: function () { textState[1](''); } }
      })
    } else {
      body = jsxs('div', {
        className: 'hc-card',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            children: [
              jsx('span', { className: 'hc-block__title', children: '命中 ' + num(data.count) + ' 条（最多回 20 条，新的在前）' }, 'title'),
              jsx('span', { className: 'hc-chip', children: '关键词 ' + (data.query || '') }, 'q'),
              jsx('span', { className: 'hc-chip', children: 'role_filter ' + (data.role_filter || '—') }, 'rf')
            ]
          }, 'head'),
          jsx('div', {
            style: { display: 'flex', flexDirection: 'column', minWidth: 0, maxWidth: '100%' },
            children: hits.map(function (hit, index) {
              return jsx(MessageLine, { item: hit, narrow: props.narrow, first: index === 0 }, 'h' + index)
            })
          }, 'list'),
          data.note ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: String(data.note) }, 'note') : null
        ]
      })
    }
  }

  return jsxs('div', {
    className: 'hc-block',
    style: { gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [form, body, note]
  })
}

// ===========================================================================
// 对话下钻（t_c4c4d49e）：左侧对话列表（可搜索）→ 右侧该对话详情
// ---------------------------------------------------------------------------
// 这一屏是老板要的「一个对话一个对话地看」：上下文长度/占比、压缩几次压缩了哪些、
// 是否召回过每次召回了哪些。**没有数据一律显示「无记录」+ 后端给的原因**，
// 不拿字符数折算 token、不估算占比、不把 0 当「有值」。
// ===========================================================================

/** 「无记录」标记：一个 chip + 一句为什么（原因来自后端，前端不编）。 */
function NoRecord(props) {
  return jsxs('span', {
    'data-vcc-norecord': props.what || 'yes',
    style: {
      display: 'inline-flex',
      flexWrap: 'wrap',
      alignItems: 'baseline',
      gap: 'var(--s2)',
      minWidth: 0,
      maxWidth: '100%'
    },
    children: [
      jsx('span', {
        className: 'hc-chip',
        style: { color: 'var(--ui-warning, inherit)' },
        children: '无记录'
      }, 'chip'),
      props.why
        ? jsx('span', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', minWidth: 0 },
            children: props.why
          }, 'why')
        : null
    ]
  })
}

/** 小标题条（`hc-block__head` + 可选 chips）。 */
function HeadBar(props) {
  return jsxs('div', {
    className: 'hc-block__head',
    style: { minWidth: 0, maxWidth: '100%' },
    children: [jsx('span', { className: 'hc-block__title', children: props.title }, 'title')].concat(
      props.chips || []
    )
  })
}

/** 一段预格式化文本（摘要 / 召回块），带高度上限与自动换行，绝不撑宽。 */
function PreText(props) {
  return jsx('div', {
    style: {
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 'var(--fs-xs)',
      lineHeight: '1.55',
      whiteSpace: 'pre-wrap',
      overflowWrap: 'anywhere',
      wordBreak: 'break-word',
      minWidth: 0,
      maxWidth: '100%',
      maxHeight: props.maxHeight || '240px',
      overflowY: 'auto',
      color: 'var(--ui-text-secondary, inherit)'
    },
    children: props.text
  })
}

/** 左侧一行：对话标题 + 压缩/召回徽标 + 条数/字符/时间。 */
function ConvItem(props) {
  var row = props.row || {}
  var on = props.active
  var compCount = Number(row.summary_distinct) || 0
  var archivedRows = Number(row.rows_archived) || 0
  var recallCount = Number(row.recall_total) || 0
  var chips = []
  if (compCount > 0) {
    chips.push(jsx('span', { className: 'hc-chip', children: '压缩 ' + num(compCount) + ' 次' }, 'c'))
  } else if (archivedRows > 0) {
    chips.push(jsx('span', { className: 'hc-chip', children: '归档 ' + num(archivedRows) + ' 条（摘要无记录）' }, 'a'))
  }
  if (recallCount > 0) {
    chips.push(jsx('span', { className: 'hc-chip', children: '召回 ' + num(recallCount) + ' 次' }, 'r'))
  }
  if (!compCount && !recallCount) {
    chips.push(jsx('span', { className: 'hc-chip', children: '无记录' }, 'n'))
  }
  return jsxs(
    'button',
    {
      type: 'button',
      'data-vcc-conv': String(row.session || ''),
      'aria-pressed': on ? 'true' : 'false',
      onClick: function () {
        props.onPick(row.session)
      },
      title: String(row.session || ''),
      style: {
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'stretch',
        gap: 'var(--s1)',
        width: '100%',
        minWidth: 0,
        maxWidth: '100%',
        textAlign: 'left',
        padding: 'var(--s2) var(--s3)',
        border: 'none',
        borderLeft: 'var(--nav-mark) solid ' + (on ? 'var(--ui-accent, currentColor)' : 'transparent'),
        borderRadius: '0',
        background: on ? 'var(--ui-row-active-background, transparent)' : 'transparent',
        color: 'inherit',
        font: 'inherit',
        cursor: 'pointer'
      },
      children: [
        jsx('span', {
          style: {
            display: 'block',
            fontSize: 'var(--fs-sm)',
            fontWeight: on ? 600 : 400,
            color: 'var(--ui-text-primary, inherit)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0
          },
          children: row.label || row.session || '(未命名)'
        }, 'label'),
        jsx('span', {
          style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s1)', minWidth: 0 },
          children: chips
        }, 'chips'),
        jsx('span', {
          className: 'hc-muted',
          style: { display: 'block', fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
          children:
            '消息 ' + num(row.rows_total) + ' 条 · 当前 ' + num(row.rows_active) + ' 条 / ' +
            num(row.chars_active) + ' 字符 · 最后 ' + (row.last_ts_text || '—')
        }, 'meta')
      ]
    },
    String(row.session || '')
  )
}

/** 上下文卡：条数 / 字符数 / token / 占模型窗口比例 / 归档占比（缺数据就是「无记录」）。 */
function ContextCard(props) {
  var ctx = props.ctx || {}
  var tokenNode = ctx.tokens === null || ctx.tokens === undefined
    ? jsx(NoRecord, { what: 'tokens', why: ctx.token_reason }, 'tok')
    : jsx('span', { children: num(ctx.tokens) + ' token' }, 'tok')
  var ratioNode = ctx.ratio_pct === null || ctx.ratio_pct === undefined
    ? jsx(NoRecord, { what: 'ratio', why: ctx.ratio_reason }, 'ratio')
    : jsx('span', { children: ctx.ratio_pct + '%（' + num(ctx.tokens) + ' / ' + num(ctx.model_window) + '）' }, 'ratio')
  var windowNode = ctx.model_window === null || ctx.model_window === undefined
    ? jsx(NoRecord, { what: 'window', why: ctx.model_window_reason }, 'win')
    : jsx('span', { children: num(ctx.model_window) + ' token（' + (ctx.model_window_source || '—') + '）' }, 'win')

  return jsxs('div', {
    className: 'hc-card',
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      jsx(HeadBar, {
        title: '这个对话的上下文',
        chips: [
          jsx('span', { className: 'hc-chip', children: '模型 ' + (ctx.model || '—') }, 'm'),
          jsx('span', {
            className: 'hc-chip',
            children: '起 ' + (ctx.first_ts || '—') + ' · 止 ' + (ctx.last_ts || '—')
          }, 't')
        ]
      }, 'head'),
      jsxs('div', {
        className: 'hc-cards',
        children: [
          jsx(Stat, {
            label: '当前上下文',
            value: num(ctx.rows_active) + ' 条',
            sub: num(ctx.chars_active) + ' 字符（数出来的）'
          }, 'a'),
          jsx(Stat, {
            label: '已归档（压缩掉的）',
            value: num(ctx.rows_archived) + ' 条',
            sub: num(ctx.chars_archived) + ' 字符' +
              (ctx.archived_share_pct === null || ctx.archived_share_pct === undefined
                ? ''
                : ' · 占全部 ' + ctx.archived_share_pct + '%')
          }, 'b'),
          jsx(Stat, {
            label: '上下文 token 数',
            value: ctx.tokens === null || ctx.tokens === undefined ? '无记录' : num(ctx.tokens),
            sub: ctx.tokens === null || ctx.tokens === undefined ? 'token_count 全为 NULL' : '库里记的值'
          }, 'c'),
          jsx(Stat, {
            label: '占模型窗口',
            value: ctx.ratio_pct === null || ctx.ratio_pct === undefined ? '无记录' : ctx.ratio_pct + '%',
            sub: ctx.model_window === null || ctx.model_window === undefined ? '窗口也缺 ⇒ 算不出' : '窗口 ' + num(ctx.model_window)
          }, 'd')
        ]
      }, 'cards'),
      jsx(Field, { narrow: props.narrow, label: 'token 数', value: tokenNode }, 'f-token'),
      jsx(Field, { narrow: props.narrow, label: '占模型窗口比例', value: ratioNode }, 'f-ratio'),
      jsx(Field, { narrow: props.narrow, label: '模型窗口', value: windowNode }, 'f-window'),
      jsx(Field, {
        narrow: props.narrow,
        label: '全库合计',
        value: num(ctx.rows_total) + ' 条（当前 ' + num(ctx.rows_active) + ' + 归档 ' + num(ctx.rows_archived) + '）'
      }, 'f-total')
    ]
  })
}

/** 一次压缩：摘要 + 压缩前（累计已归档）/本次新增/压缩后长度 + 预览与全文。 */
function CompressionEvent(props) {
  var ev = props.event || {}
  var openState = useState(false)
  var open = openState[0]
  var ratio = ev.before_chars ? Math.round((1000 * (ev.after_chars || 0)) / ev.before_chars) / 10 : null
  var sections = Array.isArray(ev.sections) ? ev.sections : []
  return jsxs('div', {
    'data-vcc-comp-event': String(ev.message_id || ''),
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--s2)',
      minWidth: 0,
      maxWidth: '100%',
      padding: 'var(--s3)',
      border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
      borderRadius: 'var(--r-md)',
      background: 'var(--ui-bg-secondary, transparent)'
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'baseline', minWidth: 0 },
        children: [
          jsx('span', { style: { fontWeight: 600, fontSize: 'var(--fs-md)' }, children: '第 ' + (props.index + 1) + ' 次' }, 'n'),
          jsx('span', { className: 'hc-chip', children: '#' + (ev.message_id === undefined ? '—' : ev.message_id) }, 'id'),
          jsx('span', { className: 'hc-chip', children: ev.ts || '—' }, 'ts'),
          jsx('span', { className: 'hc-chip', children: '库里 ' + num(ev.rows) + ' 行' }, 'rows'),
          ev.rows > 1
            ? jsx('span', { className: 'hc-chip', children: '同一段摘要被重写 ' + num(ev.rows) + ' 次' }, 'dup')
            : null,
          jsx('span', { className: 'hc-chip', children: (ev.kind === 'ours' ? 'vcc 栏目摘要' : ev.kind === 'legacy' ? '旧格式摘要' : '宿主标记摘要') }, 'kind')
        ]
      }, 'head'),
      jsx(Field, {
        narrow: props.narrow,
        label: '压缩前（累计已归档）',
        value: num(ev.before_rows) + ' 条 / ' + num(ev.before_chars) + ' 字符'
      }, 'before'),
      jsx(Field, {
        narrow: props.narrow,
        label: '本次新增归档',
        value: num(ev.delta_rows) + ' 条 / ' + num(ev.delta_chars) + ' 字符'
      }, 'delta'),
      jsx(Field, {
        narrow: props.narrow,
        label: '压缩后（摘要本身）',
        value: num(ev.after_chars) + ' 字符' + (ratio === null ? '' : ' · 摘要/归档 ' + ratio + '%')
      }, 'after'),
      jsx(Field, {
        narrow: props.narrow,
        label: 'token 数',
        value: jsx(NoRecord, {
          what: 'comp-tokens',
          why: '压缩前后的 token 数没有落库（messages.token_count 全为 NULL），所以只给字符数'
        })
      }, 'tok'),
      sections.length
        ? jsx('div', {
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s1)', minWidth: 0 },
            children: sections.map(function (name) {
              return jsx('span', { className: 'hc-chip', children: '[' + name + ']' }, name)
            })
          }, 'sections')
        : null,
      jsx(PreText, { text: open ? ev.excerpt : ev.preview, maxHeight: open ? '360px' : '120px' }, 'text'),
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', minWidth: 0 },
        children: [
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-vcc-comp-expand': String(ev.message_id || ''),
            onClick: function () {
              openState[1](!open)
            },
            children: open ? '收起' : '看全文'
          }, 'expand'),
          jsx('span', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
            children: open ? '全文最多 2600 字符（超出后端已截断）' : '预览 ' + num((ev.preview || '').length) + ' 字符'
          }, 'hint')
        ]
      }, 'ops')
    ]
  })
}

/** 压缩明细卡：几次 + 每次的摘要与前后长度；没有就「无记录」。 */
function CompressionsCard(props) {
  var comp = props.comp || {}
  var events = Array.isArray(comp.events) ? comp.events : []
  var head = jsx(HeadBar, {
    title: '压缩了几次、压缩了哪些',
    chips: [
      jsx('span', { className: 'hc-chip', children: '压缩 ' + num(comp.count) + ' 次' }, 'n'),
      jsx('span', { className: 'hc-chip', children: '库里摘要行 ' + num(comp.rows) + ' 行' }, 'r')
    ]
  }, 'head')
  if (!events.length) {
    var archived = comp.archived || {}
    var archivedRows = Number(archived.rows) || 0
    return jsxs('div', {
      className: 'hc-card',
      'data-vcc-comp-none': 'true',
      style: { minWidth: 0, maxWidth: '100%' },
      children: [
        head,
        jsx(Empty, {
          title: '这个对话：压缩记录 无记录',
          hint: archivedRows > 0
            ? '这个会话有 ' + num(archivedRows) + ' 条 / ' + num(archived.chars) + ' 字符已被归档（compacted = 1），' +
              '说明它压缩过；但库里现在没有任何摘要行（messages._compressed_summary = 1 一行都没有），' +
              '摘要内容已被宿主清掉 —— 所以「压缩了几次、压缩了哪些」按「无记录」显示，不补数字。'
            : '库里这个会话既没有压缩摘要（messages._compressed_summary = 1 一行都没有），也没有被归档（compacted = 1）的消息。' +
              '面板按「无记录」显示，不会补 0 次假数字。',
          action: props.onReload ? { label: '重新读取', onClick: props.onReload } : undefined
        })
      ]
    })
  }
  return jsxs('div', {
    className: 'hc-card',
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      head,
      jsx('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
        children: events.map(function (ev, index) {
          return jsx(CompressionEvent, { event: ev, index: index, narrow: props.narrow }, String(ev.message_id) + '-' + index)
        })
      }, 'events'),
      comp.note
        ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere', fontSize: 'var(--fs-xs)' }, children: String(comp.note) }, 'note')
        : null
    ]
  })
}

/** 召回明细卡：会话召回（session_search）+ 记忆召回注入（memory-context）。 */
function RecallsCard(props) {
  var recalls = props.recalls || {}
  var search = recalls.search || {}
  var memory = recalls.memory || {}
  var searchEvents = Array.isArray(search.events) ? search.events : []
  var memoryEvents = Array.isArray(memory.events) ? memory.events : []
  var head = jsx(HeadBar, {
    title: '是否召回过、每次召回了哪些',
    chips: [
      jsx('span', { className: 'hc-chip', children: '召回 ' + num(recalls.total) + ' 次' }, 'n'),
      jsx('span', { className: 'hc-chip', children: '会话召回 ' + num(search.count) }, 's'),
      jsx('span', { className: 'hc-chip', children: '记忆召回 ' + num(memory.count) }, 'm')
    ]
  }, 'head')

  if (!searchEvents.length && !memoryEvents.length) {
    return jsxs('div', {
      className: 'hc-card',
      'data-vcc-recall-none': 'true',
      style: { minWidth: 0, maxWidth: '100%' },
      children: [
        head,
        jsx(Empty, {
          title: '这个对话：召回记录 无记录',
          hint: '库里这个会话既没有 session_search 工具调用（messages.tool_name），也没有 <memory-context> 记忆注入块。面板按「无记录」显示，不会补 0 次假数字。',
          action: props.onReload ? { label: '重新读取', onClick: props.onReload } : undefined
        })
      ]
    })
  }

  var blocks = []
  if (searchEvents.length) {
    blocks.push(jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
      children: [
        jsx('div', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '会话召回（session_search）：' + num(search.count) + ' 次，共命中 ' + num(search.hit_total) + ' 条' }, 'label'),
        searchEvents.map(function (ev, index) {
          var hits = Array.isArray(ev.hits) ? ev.hits : []
          return jsxs('div', {
            'data-vcc-recall-event': 'search-' + String(ev.message_id),
            style: {
              display: 'flex',
              flexDirection: 'column',
              gap: 'var(--s1)',
              padding: 'var(--s2) var(--s3)',
              border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
              borderRadius: 'var(--r-md)',
              minWidth: 0,
              maxWidth: '100%'
            },
            children: [
              jsxs('div', {
                style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'baseline', minWidth: 0 },
                children: [
                  jsx('span', { style: { fontWeight: 600 }, children: '第 ' + (index + 1) + ' 次召回' }, 'n'),
                  jsx('span', { className: 'hc-chip', children: '#' + ev.message_id }, 'id'),
                  jsx('span', { className: 'hc-chip', children: ev.ts || '—' }, 'ts'),
                  jsx('span', { className: 'hc-chip', children: '命中 ' + num(ev.hit_count) + ' 条' }, 'hits'),
                  ev.query ? jsx('span', { className: 'hc-chip', children: '查询「' + clip(ev.query, 60) + '」' }, 'q') : null
                ]
              }, 'head'),
              hits.length
                ? jsx('div', {
                    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
                    children: hits.map(function (hit, hitIndex) {
                      return jsxs('div', {
                        style: { display: 'flex', flexDirection: props.narrow ? 'column' : 'row', gap: 'var(--s2)', minWidth: 0, fontSize: 'var(--fs-xs)' },
                        children: [
                          jsx('span', {
                            style: { flex: '0 0 auto', color: 'var(--ui-text-secondary, inherit)', fontWeight: 600, overflowWrap: 'anywhere' },
                            children: (hit.title || '(无标题)')
                          }, 't'),
                          jsx('span', {
                            className: 'hc-muted',
                            style: { flex: '0 0 auto', fontVariantNumeric: 'tabular-nums' },
                            children: (hit.when || '—') + (hit.messages ? ' · ' + num(hit.messages) + ' 条' : '')
                          }, 'm'),
                          jsx('span', {
                            style: { minWidth: 0, overflowWrap: 'anywhere' },
                            children: clip(hit.preview || '', props.narrow ? 160 : 220)
                          }, 'p')
                        ]
                      }, 'h' + hitIndex)
                    })
                  }, 'hits')
                : jsx(PreText, { text: ev.snippet || '（这条召回没有可解析的命中内容）', maxHeight: '120px' }, 'raw'),
              jsx('span', {
                className: 'hc-muted',
                style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
                children: '来源 messages.tool_name = session_search（这条工具返回原样解析）'
              }, 'src')
            ]
          }, 's' + index)
        })
      ]
    }, 'search'))
  }
  if (memoryEvents.length) {
    blocks.push(jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
      children: [
        jsx('div', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '记忆召回注入（<memory-context>）：' + num(memory.count) + ' 次' }, 'label'),
        memoryEvents.map(function (ev, index) {
          var bullets = Array.isArray(ev.bullets) ? ev.bullets : []
          return jsxs('div', {
            'data-vcc-recall-event': 'memory-' + String(ev.message_id),
            style: {
              display: 'flex',
              flexDirection: 'column',
              gap: 'var(--s1)',
              padding: 'var(--s2) var(--s3)',
              border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
              borderRadius: 'var(--r-md)',
              minWidth: 0,
              maxWidth: '100%'
            },
            children: [
              jsxs('div', {
                style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'baseline', minWidth: 0 },
                children: [
                  jsx('span', { style: { fontWeight: 600 }, children: '第 ' + (index + 1) + ' 次注入' }, 'n'),
                  jsx('span', { className: 'hc-chip', children: '#' + ev.message_id }, 'id'),
                  jsx('span', { className: 'hc-chip', children: ev.ts || '—' }, 'ts'),
                  jsx('span', { className: 'hc-chip', children: '注入块 ' + num(ev.block_chars) + ' 字符' }, 'chars'),
                  jsx('span', { className: 'hc-chip', children: '条目 ' + num(ev.bullet_count) }, 'bullets')
                ]
              }, 'head'),
              ev.own_text
                ? jsx('div', {
                    className: 'hc-muted',
                    style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
                    children: '同一轮用户消息：' + clip(ev.own_text, props.narrow ? 120 : 200)
                  }, 'own')
                : null,
              bullets.length
                ? jsx('ul', {
                    style: { margin: 0, paddingLeft: 'var(--s4)', minWidth: 0, overflowWrap: 'anywhere', fontSize: 'var(--fs-sm)' },
                    children: bullets.map(function (text, bulletIndex) {
                      return jsx('li', { children: text }, 'b' + bulletIndex)
                    })
                  }, 'bullets')
                : jsx('span', { className: 'hc-muted', children: '注入块里没有以「- 」开头的条目（块本身有 ' + num(ev.block_chars) + ' 字符）' }, 'none'),
              jsx('span', {
                className: 'hc-muted',
                style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
                children: '来源 messages.content 里的 <memory-context> 块（前 ' + num(bullets.length) + ' 条，每条截 240 字符）'
              }, 'src')
            ]
          }, 'm' + index)
        })
      ]
    }, 'memory'))
  }

  return jsxs('div', {
    className: 'hc-card',
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      head,
      jsx('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
        children: blocks
      }, 'blocks'),
      recalls.note
        ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere', fontSize: 'var(--fs-xs)' }, children: String(recalls.note) }, 'note')
        : null
    ]
  })
}

/** 数据来源卡：每个数字来自哪个库/哪张表/哪个字段（后端给的原文，前端只排版）。 */
function SourcesCard(props) {
  var sources = Array.isArray(props.sources) ? props.sources : []
  if (!sources.length) return null
  return jsxs('div', {
    className: 'hc-card',
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      jsx(HeadBar, { title: '数据来源（每个数字都能在这里对到）' }, 'head'),
      jsx('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
        children: sources.map(function (item, index) {
          return jsxs('div', {
            style: {
              display: 'flex',
              flexDirection: 'column',
              gap: 'var(--s1)',
              paddingTop: index ? 'var(--s2)' : 0,
              borderTop: index ? 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)' : undefined,
              minWidth: 0,
              maxWidth: '100%',
              fontSize: 'var(--fs-xs)'
            },
            children: [
              jsx('span', { style: { fontWeight: 600, color: 'var(--ui-text-primary, inherit)' }, children: item.item }, 'item'),
              jsx('span', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: item.where + ' · ' + item.table }, 'where'),
              jsx('span', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: '字段：' + item.fields }, 'fields'),
              item.note ? jsx('span', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: item.note }, 'note') : null
            ]
          }, 's' + index)
        })
      }, 'list')
    ]
  })
}

/** 右侧：一个对话的详情（上下文 → 压缩 → 召回 → 数据来源）。 */
function ConvDetail(props) {
  var session = props.session
  var query = useQuery(
    'vcc|conversation|' + props.agent + '|' + String(session),
    function () {
      return get('/vcc/conversation', {
        params: { agent: props.agent, session: session, event_limit: 40 }
      })
    },
    { staleMs: 60000, enabled: props.ready && !!session }
  )

  if (!session) {
    return jsx(Empty, {
      title: '左边先选一个对话',
      hint: '列表里每一行就是一个对话（按最后活动时间倒序）。点一行，这里就显示它的上下文长度、压缩了几次、召回了几次。'
    })
  }
  if (query.status === 'loading') {
    return jsx(Skeleton, { lines: 6, cards: 2, label: '正在读取这个对话的压缩与召回记录' })
  }
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '这个对话没读到',
      message: query.error,
      detail: 'GET /vcc/conversation?session=' + session,
      onRetry: query.reload
    })
  }
  var data = payloadOf(query)
  if (!data || !data.context) {
    return jsx(ErrorState, {
      title: '后端返回的结构不对',
      message: '/vcc/conversation 没有返回 context 字段，面板无法判断是「没有记录」还是「接口变了」',
      detail: 'GET /vcc/conversation?session=' + session,
      onRetry: query.reload
    })
  }

  var row = data.row || {}
  var comp = data.compressions || {}
  var recalls = data.recalls || {}

  return jsxs('div', {
    'data-vcc-conv-detail': session,
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        className: 'hc-block__head',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsx('span', {
            className: 'hc-block__title',
            style: { overflowWrap: 'anywhere' },
            children: row.label || session
          }, 'title'),
          jsx('span', { className: 'hc-chip', children: '压缩 ' + num(comp.count) + ' 次' }, 'c'),
          jsx('span', { className: 'hc-chip', children: '召回 ' + num(recalls.total) + ' 次' }, 'r'),
          jsx('span', { className: 'hc-chip', children: data.session_row ? '会话在库' : 'sessions 表里没有这行' }, 'row'),
          jsx('button', { type: 'button', className: 'hc-btn hc-btn--tiny', onClick: query.reload, children: '刷新' }, 'refresh')
        ]
      }, 'head'),
      jsx('div', {
        className: 'hc-muted',
        style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', minWidth: 0, maxWidth: '100%' },
        children: '会话 id：' + session + ' · 库：' + (data.db || '—')
      }, 'id'),
      jsx(ContextCard, { ctx: data.context, narrow: props.narrow }, 'ctx'),
      jsx(CompressionsCard, { comp: comp, narrow: props.narrow, onReload: query.reload }, 'comp'),
      jsx(RecallsCard, { recalls: recalls, narrow: props.narrow, onReload: query.reload }, 'recall'),
      data.caliber
        ? jsx('div', {
            className: 'hc-notice',
            children: jsx('span', { className: 'hc-notice__text', style: { overflowWrap: 'anywhere' }, children: String(data.caliber) })
          }, 'caliber')
        : null,
      jsx(SourcesCard, { sources: data.data_sources }, 'sources')
    ]
  })
}

/**
 * 对话下钻：左侧可搜索的对话列表 → 右侧该对话详情。
 *
 * 一次取 500 个对话（后端按最后活动时间倒序），搜索与筛选都在本页内做（打字不发请求）；
 * 选中哪个对话就按 `session` 拉 `/vcc/conversation`。窄窗（<900px）左右改上下。
 */
function ConvView(props) {
  var qState = useState('')
  var filterState = useState('all')
  var pickState = useState('')
  var q = qState[0]
  var filter = filterState[0]
  var picked = pickState[0]

  var listQuery = useQuery(
    'vcc|conversations|' + props.agent,
    function () {
      return get('/vcc/conversations', {
        params: { agent: props.agent, q: '', filter: 'all', limit: 500 }
      })
    },
    { staleMs: 30000, enabled: props.ready }
  )

  if (listQuery.status === 'loading') {
    return jsx(Skeleton, { lines: 6, cards: 1, label: '正在读取对话列表' })
  }
  if (listQuery.status === 'error') {
    return jsx(ErrorState, {
      title: '对话列表没读到',
      message: listQuery.error,
      detail: 'GET /vcc/conversations',
      onRetry: listQuery.reload
    })
  }
  var data = payloadOf(listQuery)
  if (!data || !Array.isArray(data.conversations)) {
    return jsx(ErrorState, {
      title: '后端返回的结构不对',
      message: '/vcc/conversations 没有返回 conversations 数组，面板无法判断是「没有对话」还是「接口变了」',
      detail: 'GET /vcc/conversations',
      onRetry: listQuery.reload
    })
  }

  var rows = data.conversations
  var needle = q.trim().toLowerCase()
  var shown = rows.filter(function (row) {
    if (needle) {
      var hit =
        String(row.session || '').toLowerCase().indexOf(needle) !== -1 ||
        String(row.label || '').toLowerCase().indexOf(needle) !== -1 ||
        String(row.model || '').toLowerCase().indexOf(needle) !== -1
      if (!hit) return false
    }
    if (filter === 'compressed') return Boolean(row.has_compression)
    if (filter === 'recalled') return Boolean(row.has_recall)
    if (filter === 'none') return !row.has_compression && !row.has_recall
    return true
  })
  var selected = picked && shown.some(function (row) { return row.session === picked })
    ? picked
    : shown.length ? shown[0].session : ''

  var filters = [
    { id: 'all', label: '全部 ' + num(rows.length) },
    { id: 'compressed', label: '有压缩 ' + num(rows.filter(function (r) { return r.has_compression }).length) },
    { id: 'recalled', label: '有召回 ' + num(rows.filter(function (r) { return r.has_recall }).length) },
    { id: 'none', label: '无记录 ' + num(rows.filter(function (r) { return !r.has_compression && !r.has_recall }).length) }
  ]

  var pane = jsxs('div', {
    'data-vcc-conv-pane': 'list',
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--s2)',
      flex: props.narrow ? '1 1 auto' : '0 0 320px',
      width: props.narrow ? '100%' : '320px',
      maxWidth: '100%',
      minWidth: 0
    },
    children: [
      jsxs('div', {
        className: 'hc-block__head',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsx('span', { className: 'hc-block__title', children: '对话列表' }, 'title'),
          jsx('span', { className: 'hc-chip', children: num(shown.length) + ' / ' + num(rows.length) }, 'count'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            onClick: function () { listQuery.reload() },
            children: '刷新'
          }, 'refresh')
        ]
      }, 'head'),
      jsx('input', {
        type: 'search',
        className: 'hc-select',
        'aria-label': '搜索对话（标题 / 会话 id / 模型）',
        'data-vcc-conv-search': 'true',
        placeholder: '搜索标题 / 会话 id / 模型',
        value: q,
        style: { minWidth: 0, maxWidth: '100%' },
        onChange: function (event) { qState[1](event.target.value) }
      }, 'q'),
      jsx('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s1)', minWidth: 0 },
        children: filters.map(function (item) {
          var on = item.id === filter
          return jsx('button', {
            type: 'button',
            'data-vcc-conv-filter': item.id,
            'aria-pressed': on ? 'true' : 'false',
            className: 'hc-btn hc-btn--tiny' + (on ? ' hc-btn--accent' : ''),
            onClick: function () { filterState[1](item.id) },
            children: item.label
          }, item.id)
        })
      }, 'filters'),
      shown.length
        ? jsx('div', {
            role: 'list',
            'aria-label': '对话列表',
            style: {
              display: 'flex',
              flexDirection: 'column',
              minWidth: 0,
              maxWidth: '100%',
              maxHeight: props.narrow ? '260px' : '620px',
              overflowY: 'auto',
              border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
              borderRadius: 'var(--r-md)',
              background: 'var(--ui-bg-secondary, transparent)'
            },
            children: shown.map(function (row, index) {
              return jsx('div', {
                role: 'listitem',
                style: {
                  borderTop: index ? 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)' : undefined,
                  minWidth: 0
                },
                children: jsx(ConvItem, {
                  row: row,
                  active: row.session === selected,
                  onPick: function (next) { pickState[1](next) }
                }, String(row.session))
              }, 'r' + index)
            })
          }, 'list')
        : jsx(Empty, {
            title: needle ? '没有匹配「' + q.trim() + '」的对话' : '这个筛选下没有对话',
            hint: needle
              ? '搜索只在本页已取到的 ' + num(rows.length) + ' 个对话里做（标题 / 会话 id / 模型）。清掉关键词，或换「全部」筛选再试。'
              : '换一个筛选条件；「无记录」= 既没有压缩也没有召回的对话。',
            action: {
              label: '清空条件',
              onClick: function () { qState[1](''); filterState[1]('all') }
            }
          }, 'none'),
      jsx('div', {
        className: 'hc-muted',
        style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
        children: '共 ' + num(data.total) + ' 个对话，本页取 ' + num(rows.length) + ' 个（按最后活动时间倒序）· 库 ' + (data.db || '—')
      }, 'foot')
    ]
  }, 'pane')

  return jsxs('div', {
    'data-vcc-conv-view': 'true',
    style: {
      display: 'flex',
      flexDirection: props.narrow ? 'column' : 'row',
      alignItems: 'stretch',
      gap: 'var(--s3)',
      minWidth: 0,
      maxWidth: '100%'
    },
    children: [
      pane,
      jsx('div', {
        'data-vcc-conv-pane': 'detail',
        style: { flex: '1 1 auto', minWidth: 0, maxWidth: '100%' },
        children: jsx(ConvDetail, {
          agent: props.agent,
          session: selected,
          ready: props.ready,
          narrow: props.narrow
        }, 'detail')
      }, 'detail-wrap')
    ]
  })
}

