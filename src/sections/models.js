/**
 * hermes-console · 模型分区（V5）：真实 UI，替掉「模型」的空白。
 *
 * 数据全部来自本插件自己的端点（本文件不发任何别的请求）：
 *   GET  /models/overview        当前模型概览（全部 agent / 单个 agent）
 *   GET  /models/health          自检（官方写入器 / 快照目录 / registry）
 *   GET  /models/providers       某 agent 的 provider 目录（**凭据已脱敏**，含虚拟 provider moa）
 *   GET  /models/models          某 provider 的模型列表（moa 返回 MoA 预设名）
 *   GET  /models/model-library   模型库（每个 provider 的模型全表）
 *   GET  /models/snapshots       已有快照
 *   POST /models/apply           ★切模型（真实写 config.yaml，必须二次确认）
 *   POST /models/rollback        ★按快照回滚（同样写真实配置，必须二次确认）
 *   POST /models/batch/preview   ★批量预览（纯读：将写哪些行、哪个字段）
 *   POST /models/batch/apply     ★批量写入（逐 agent 快照 + 逐 agent 回读校验）
 *   POST /models/batch/rollback  ★按账本一键回滚整批
 *   GET  /models/batches         批量账本列表（最近若干次）
 *   GET/POST /models/presets    预设列表 / 新建
 *   PUT/DELETE /models/presets/{name}  改 / 删
 *
 * 六条不许退让的规则：
 *   1. **改的是谁必须一眼可见**：单 agent 面板标题区常驻「目标 agent：中文名（id）」；
 *      批量面板逐个 agent 勾选、预览表逐行点名、确认条再报一次数量。
 *      单 agent 端点仍然拒绝 `targets` 数组（`multi_agent_forbidden`）；
 *      批量只走 `/models/batch/*`，服务端逐 agent 快照、逐 agent 回读。
 *   2. **写操作必须二次确认**：点「切换」/「下一步」只把「当前 → 目标」摆到确认条上，
 *      **一个写请求都不发**；点「确认」才 POST（并带上 `confirm: true`）。
 *      批量多一道**预览**：先看「将写哪些行、哪个字段、从什么变成什么」再确认。
 *      回滚同样是两步，且回滚前显示「将还原到哪个快照的哪个值」。
 *   3. **三态齐备**：加载中是骨架屏；没有 agent / 没有 provider 是空态（说清为什么、
 *      下一步做什么）；请求失败是错误态，后端 `error.message` 原样透出。
 *   4. **凭据只读**：后端只给 `key_env`（变量名）+ `key_present`（布尔）+ 打码后的
 *      `***`，这里也不做「点击显示」，不做任何写凭据的入口。
 *   5. 颜色只走 `var(--ui-*)`（本文件零字面色值），间距只走 `var(--s*)`。
 *   6. 窄窗（<900px）：表格改竖排卡片、按钮占满一行、`overflow-wrap: anywhere`，
 *      整页**不出现横向滚动条**。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { num } from '../lib/format.js'
import { ProviderModelPicker } from '../lib/provider-model-picker.js'
import { refreshAll, useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 缓存键前缀：操作成功后 `refreshAll('models/')` 只重取本分区。 */
var CACHE_OVERVIEW = 'models/overview'
var CACHE_PROVIDERS = 'models/providers'
var CACHE_LIBRARY = 'models/library'
var CACHE_SNAPSHOTS = 'models/snapshots'
var CACHE_PRESETS = 'models/presets'
var CACHE_MOA_PRESETS = 'models/moa-presets'
var CACHE_MODELS = 'models/models'
var CACHE_BATCHES = 'models/batches'
var CACHE_PREFIX = 'models/'

/** 窄窗断点：`max-width: 900px` 以下表格改竖排，绝不产生横向滚动。 */
var NARROW_PX = 900
var NARROW_QUERY = '(max-width: 900px)'

/**
 * 订阅窗口宽度是否窄于 :data:`NARROW_PX`。
 * 没有 `matchMedia`（探针里的极简环境）时按「宽」处理：布局不崩，只是不折叠。
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

/** `useQuery` 的 data → 响应体（后端成功体是裸 dict，`ok:true` 在外层）。 */
function payloadOf(query) {
  var body = query && query.data
  return body && typeof body === 'object' ? body : null
}

/** 一行概览的 agent id（后端同时给 `agent` 与 `profile`，两个都认）。 */
function agentOf(row) {
  var source = row || {}
  return typeof source.agent === 'string' ? source.agent : typeof source.profile === 'string' ? source.profile : ''
}

/** 空值统一显示成破折号（`undefined` / `null` / `''` 三种都算空）。 */
function orDash(value) {
  if (value === undefined || value === null || value === '') return '—'
  return String(value)
}

/** 布尔 → 「有 / 缺」（凭据只给这一档信息，不给值）。 */
function presentText(flag) {
  return flag === true ? '变量已配置' : '变量未配置'
}

/** 统一定宽的小标签。 */
function Tag(props) {
  // 透传 `data-*` 之类的附加属性：调用方用 `data-models-*-count` / `data-*-source` 这类钩子
  // 把「这一行/这个数字是什么」暴露给验收探针。**不透传** off / tone（它们是本组件的样式开关，
  // 不是 DOM 语义），也不透传 children（单独给）。
  var extra = {}
  Object.keys(props).forEach(function (key) {
    if (key === 'off' || key === 'tone' || key === 'title' || key === 'children') return
    extra[key] = props[key]
  })
  extra.className = 'hc-chip' + (props.off ? ' hc-chip--off' : '') + (props.tone ? ' ' + props.tone : '')
  extra.title = props.title || ''
  extra.children = props.children
  return jsx('span', extra)
}

/** 顶部统计卡（复用概览的 `.hc-card`，不新造一套）。 */
function Stat(props) {
  return jsxs('div', {
    className: 'hc-card',
    children: [
      jsx('div', { className: 'hc-card__label', children: props.label }, 'label'),
      jsx('div', { className: 'hc-card__value', children: props.value }, 'value'),
      props.sub ? jsx('div', { className: 'hc-card__sub', children: props.sub }, 'sub') : null
    ]
  })
}

/** 块级标题 + 说明（统一各面板的抬头，避免每处各写一套字号）。 */
function BlockTitle(props) {
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
    children: [
      jsx('div', {
        className: 'hc-block__title',
        style: { fontSize: 'var(--fs-lg)', fontWeight: 600, overflowWrap: 'anywhere' },
        children: props.children
      }, 't'),
      props.hint
        ? jsx('div', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
            children: props.hint
          }, 'h')
        : null
    ]
  })
}

/** 操作结果条：成功一句话；失败给后端原话 + 错误码（不吞、不美化）。 */
function Feedback(props) {
  var result = props.result
  if (!result) return null
  return jsxs('div', {
    className: 'hc-up__feedback' + (result.ok ? ' hc-up__feedback--ok' : ' hc-up__feedback--error'),
    role: 'status',
    'data-models-feedback': result.ok ? 'ok' : 'error',
    style: { flexWrap: 'wrap', minWidth: 0, maxWidth: '100%' },
    children: [
      jsx('span', { className: 'hc-up__feedback-title', children: result.title }, 'title'),
      jsx('span', { className: 'hc-up__feedback-text', children: result.message }, 'text')
    ]
  })
}

/** 等宽字体（只影响字形，不是颜色；模型名 / 变量名对齐用）。 */
var MONO = "'ui-monospace', 'SFMono-Regular', Menlo, Consolas, monospace"

/** 「当前 → 目标」对比条：切换前**必看**，也是二次确认的文案主体。 */
// ---------------------------------------------------------------------------
// Agent 表：本页面的**主体**
//
// 整改前这里是「一个下拉选 agent + 一堆卡片」，页面被表单和说明文字占满，
// agent 本身要滚很久才看得到。现在 agent 就是列表本身：每行一个 agent，
// 当前 provider / 模型 / 备用首条 / 辅助模型 / reasoning 全在行上，
// 勾选即选择，选 1 个进单编辑、选多个进批量（同一套编辑器）。
// ---------------------------------------------------------------------------

/** 一行里的一个小格（宽窗按 flex 排，窄窗整行竖排）。 */
function tableCell(props) {
  return jsx('div', {
    'data-cell': props.cellKey,
    title: props.title,
    style: {
      flex: props.flex || '1 1 120px',
      minWidth: 0,
      overflowWrap: 'anywhere',
      fontSize: props.mono ? 'var(--fs-xs)' : 'var(--fs-sm)',
      fontFamily: props.mono ? MONO : 'inherit',
      textAlign: props.align || 'left'
    },
    children: props.children
  }, props.cellKey)
}

/** agent 表头：常驻的「全选 / 清空 / 只选未配」+ 列名。 */
function AgentTableHead(props) {
  var narrow = props.narrow === true
  var total = props.total || 0
  return jsxs('div', {
    className: 'hc-models__thead',
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
    children: [
      jsxs('div', {
        style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0 },
        children: [
          jsx('div', { className: 'hc-block__title', children: 'Agent 与当前模型' }, 't'),
          jsx(Tag, { children: num(total) + ' 个' }, 'n'),
          jsx('span', { style: { flex: '1 1 auto' } }, 'sp'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-select-all': true,
            disabled: props.busy === true || !total,
            onClick: props.onSelectAll,
            children: '全选'
          }, 'all'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-select-none': true,
            disabled: props.busy === true || !props.count,
            onClick: props.onSelectNone,
            children: '清空选择'
          }, 'none'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-select-missing': true,
            title: '只勾上「没配 provider 或没配模型」的 agent —— 一次补齐最常见的场景',
            disabled: props.busy === true || !props.missingCount,
            onClick: props.onSelectMissing,
            children: '只选未配（' + num(props.missingCount || 0) + '）'
          }, 'missing')
        ]
      }, 'bar'),
      narrow
        ? null
        : jsxs('div', {
            className: 'hc-muted',
            style: { display: 'flex', gap: 'var(--s2)', alignItems: 'center', fontSize: 'var(--fs-xs)', minWidth: 0 },
            children: [
              tableCell({ cellKey: 'c0', flex: '0 0 26px', children: '' }),
              tableCell({ cellKey: 'c1', flex: '1 1 190px', children: 'Agent' }),
              tableCell({ cellKey: 'c2', flex: '1 1 220px', children: '当前 provider / 模型' }),
              tableCell({ cellKey: 'c3', flex: '1 1 160px', children: '备用链' }),
              tableCell({ cellKey: 'c4', flex: '1 1 150px', children: '辅助 / reasoning' }),
              tableCell({ cellKey: 'c5', flex: '0 0 64px', children: '' })
            ]
          }, 'cols')
    ]
  })
}

/** 一个 agent 一行。`data-models-agent-row` / `data-selected` 是验收口径。 */
function AgentRow(props) {
  var row = props.row || {}
  var id = agentOf(row)
  var narrow = props.narrow === true
  var on = props.selected === true
  var missing = Array.isArray(row.missing) ? row.missing : []
  var fallbacks = Array.isArray(row.fallbacks) ? row.fallbacks : []
  var head = fallbacks.length && fallbacks[0] && typeof fallbacks[0] === 'object' ? fallbacks[0] : null
  var aux = Array.isArray(row.auxiliary) ? row.auxiliary : []
  var auxText = aux.length
    ? aux.map(function (item) {
        return String((item && (item.slot || item.label || '')) || '') + '=' + orDash(item && item.model)
      }).join(' · ')
    : ''
  return jsxs('div', {
    className: on ? 'hc-row hc-models__row hc-models__row--on' : 'hc-row hc-models__row',
    'data-models-agent-row': id,
    'data-selected': on ? '1' : '0',
    style: {
      display: 'flex',
      flexDirection: narrow ? 'column' : 'row',
      gap: narrow ? 'var(--s1)' : 'var(--s2)',
      alignItems: narrow ? 'stretch' : 'center',
      minWidth: 0,
      maxWidth: '100%',
      cursor: 'default'
    },
    children: [
      jsx('div', {
        style: { flex: '0 0 26px', display: 'flex', alignItems: 'center' },
        children: jsx('input', {
          type: 'checkbox',
          'data-models-row-check': id,
          checked: on,
          disabled: props.busy === true,
          onChange: function () {
            props.onToggle(id)
          }
        }, 'cb')
      }, 'c0'),
      tableCell({
        cellKey: 'c1',
        flex: '1 1 190px',
        children: jsxs('div', {
          style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
          children: [
            jsxs('div', {
              style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0 },
              children: [
                jsx('span', { style: { fontWeight: on ? 600 : 500, fontSize: 'var(--fs-md)' }, children: String(row.label || id) }, 'l'),
                jsx('span', { className: 'hc-muted', style: { fontFamily: MONO, fontSize: 'var(--fs-xs)' }, children: id }, 'id')
              ]
            }, 'name'),
            row.error
              ? jsx(Tag, { off: true, title: row.error, children: '读取失败' }, 'err')
              : missing.length
                ? jsx(Tag, { off: true, title: missing.join(' / '), children: '缺 ' + num(missing.length) + ' 项' }, 'miss')
                : jsx(Tag, { children: '已配置' }, 'ok')
          ]
        })
      }),
      tableCell({
        cellKey: 'c2',
        flex: '1 1 220px',
        mono: true,
        title: orDash(row.provider) + ' / ' + orDash(row.model),
        children: orDash(row.provider) + ' / ' + orDash(row.model)
      }),
      tableCell({
        cellKey: 'c3',
        flex: '1 1 160px',
        mono: true,
        title: fallbacks.length
          ? fallbacks.map(function (item, index) {
              return '#' + (index + 1) + ' ' + orDash(item && item.provider) + ' / ' + orDash(item && item.model)
            }).join('；')
          : '没有备用模型',
        children: fallbacks.length
          ? (fallbacks.length > 1 ? '共 ' + num(fallbacks.length) + ' 条 · ' : '') +
            orDash(head.provider) + ' / ' + orDash(head.model)
          : '—'
      }),
      tableCell({
        cellKey: 'c4',
        flex: '1 1 150px',
        mono: true,
        title: (auxText ? auxText : '辅助模型：—') +
          (row.reasoning_effort ? ' · reasoning=' + String(row.reasoning_effort) : ''),
        children: (auxText ? auxText : '辅助模型：—') +
          (row.reasoning_effort ? ' · reasoning=' + String(row.reasoning_effort) : '')
      }),
      jsx('div', {
        style: { flex: '0 0 64px', display: 'flex', justifyContent: 'flex-end' },
        children: jsx('button', {
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-models-row-edit': id,
          disabled: props.busy === true,
          onClick: function () {
            props.onEdit(id)
          },
          children: '编辑'
        }, 'edit')
      }, 'c5')
    ]
  }, id)
}

/** 选择栏：只在有选择时出现；「已选 N 个」是验收口径（data-models-selected）。 */
function SelectionBar(props) {
  var count = props.count || 0
  return jsxs('div', {
    className: 'hc-up__confirm hc-models__selbar',
    'data-models-selection-bar': true,
    'data-models-selected': String(count),
    role: 'group',
    'aria-label': '已选 ' + count + ' 个 agent',
    style: { flexWrap: 'wrap', minWidth: 0, maxWidth: '100%' },
    children: [
      jsx('span', {
        className: 'hc-up__confirm-q',
        children: '已选 ' + num(count) + ' 个 agent（' +
          (count === 1 ? '单个编辑' : '批量编辑') + '）'
      }, 'q'),
      jsx('button', {
        type: 'button',
        className: 'hc-btn hc-btn--accent',
        'data-models-editor-open': true,
        disabled: props.busy === true,
        onClick: props.onOpenEditor,
        children: count === 1 ? '编辑这一个' : '批量编辑这 ' + num(count) + ' 个'
      }, 'open'),
      jsx('button', {
        type: 'button',
        className: 'hc-btn hc-btn--tiny',
        disabled: props.busy === true,
        onClick: props.onSelectNone,
        children: '取消选择'
      }, 'none')
    ]
  })
}

function CurrentModel(props) {
  var row = props.row || {}
  var narrow = props.narrow
  var current = row.current && typeof row.current === 'object' ? row.current : {}
  var provider = row.provider || current.provider || ''
  var model = row.model || current.model || ''
  var baseUrl = row.base_url || current.base_url || ''
  var apiMode = row.api_mode || current.api_mode || ''
  var keyEnv = row.key_env || current.key_env || ''
  var missing = Array.isArray(row.missing) ? row.missing : []
  var cardStyle = {
    display: 'grid',
    gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : 'minmax(0, 140px) minmax(0, 1fr)',
    gap: 'var(--s2)',
    minWidth: 0,
    maxWidth: '100%'
  }
  var rows = [
    { key: 'provider', label: '当前 provider', value: provider },
    { key: 'model', label: '当前模型', value: model },
    { key: 'base_url', label: 'base_url', value: baseUrl, mono: true },
    { key: 'api_mode', label: 'api_mode', value: apiMode },
    {
      key: 'key_env',
      label: '凭据变量',
      value: keyEnv,
      mono: true,
      tail: keyEnv ? presentText(row.key_present === true) : '没有 key_env'
    }
  ]
  return jsxs('div', {
    className: 'hc-block',
    'data-models-current': agentOf(row),
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        className: 'hc-block__head',
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' }
        , children: [
          jsx('div', { className: 'hc-block__title', children: '当前模型' }, 't'),
          jsx(Tag, { off: !model, children: model ? model : '未配置模型' }, 'm'),
          jsx(Tag, { off: !provider, children: provider ? 'provider: ' + provider : '未配置 provider' }, 'p'),
          missing.length
            ? jsx(Tag, { off: true, title: missing.join(' / '), children: '缺 ' + missing.length + ' 项' }, 'x')
            : null
        ]
      }, 'head'),
      jsx('div', {
        style: cardStyle,
        children: rows.map(function (item) {
          return jsxs('div', {
            style: { display: 'contents' },
            children: [
              jsx('span', {
                className: 'hc-muted',
                style: { fontSize: 'var(--fs-xs)', alignSelf: 'baseline' },
                children: item.label
              }, 'k'),
              jsxs('span', {
                style: {
                  minWidth: 0,
                  overflowWrap: 'anywhere',
                  fontFamily: item.mono ? MONO : 'inherit',
                  fontSize: 'var(--fs-md)'
                },
                children: [
                  jsx('span', { children: orDash(item.value) }, 'v'),
                  item.tail
                    ? jsx('span', {
                        className: 'hc-muted',
                        style: { fontSize: 'var(--fs-xs)', marginLeft: 'var(--s2)' },
                        children: '（' + item.tail + '）'
                      }, 'tail')
                    : null
                ]
              }, 'v')
            ]
          }, item.key)
        })
      }, 'body'),
      row.error
        ? jsx('div', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', marginTop: 'var(--s2)' },
            children: '这一行读取失败（其余 agent 不受影响）：' + row.error
          }, 'err')
        : null
    ]
  })
}

/** provider 目录的列标题（Provider / URL / Key env）：与数据行共用同一份列定义。 */
function ProviderListHead() {
  return jsxs('div', {
    className: 'hc-models__pv-head',
    'data-models-pv-head': true,
    children: [
      jsx('span', { children: 'Provider' }, 'c1'),
      jsx('span', { children: 'URL' }, 'c2'),
      jsx('span', { children: 'Key env' }, 'c3')
    ]
  })
}

/**
 * provider 一行：三列紧凑网格（Provider / URL / Key env）。
 *
 * 整改前每一格都是 `flex: 1 1 Npx`：三列被拉到极宽，URL 与 key_env 之间出现大片
 * 横向空白，最右的 key_env 孤立悬浮。现在列宽由 CSS 统一给（`.hc-models__pv-row`），
 * 中间那列承担弹性宽度；key_env 与所在列左对齐，超长变量名用省略号 + title 给全值。
 */
function ProviderRow(props) {
  var row = props.row || {}
  var creds = row.credentials && typeof row.credentials === 'object' ? row.credentials : {}
  var maskedKeys = Array.isArray(creds.masked_keys) ? creds.masked_keys : []
  var keyEnv = String(row.key_env || '')
  var virtual = row.virtual === true
  var keyLabel = keyEnv || (virtual ? '虚拟 · 无 key' : '无 key_env')
  var keyTitle = keyEnv
    ? '变量名：' + keyEnv + (row.key_present === true ? '（已配置）' : '（未配置）')
    : virtual
      ? '虚拟 provider：本地 MoA 方案，base_url 是 moa://local，不需要 key'
      : '这个 provider 没有 key_env（不需要凭据，或还没在 config.yaml 里声明）'
  if (maskedKeys.length) keyTitle += '；后端已打码的凭据字段：' + maskedKeys.join(' / ')
  return jsxs('div', {
    className: 'hc-models__pv-row' + (row.current ? ' hc-models__pv-row--on' : ''),
    'data-models-provider': String(row.provider || ''),
    'data-models-provider-current': row.current ? '1' : '0',
    children: [
      jsxs('span', {
        className: 'hc-models__pv-cell hc-models__pv-name',
        'data-cell': 'provider',
        children: [
          jsx('span', {
            className: 'hc-models__pv-name-text',
            style: { fontFamily: MONO },
            title: String(row.provider || ''),
            children: String(row.provider || '')
          }, 'n'),
          row.current ? jsx(Tag, { tone: 'hc-chip--on', children: '当前' }, 'cur') : null,
          jsx(Tag, {
            off: true,
            'data-models-provider-source': String(row.source || ''),
            title: '这一行的来源：配置文件 = 当前 Agent 的 config.yaml 里 providers: 声明过；' +
              '内置 = 宿主 hermes_cli 的静态 registry；虚拟 = 本地方案（moa://local，不联网）',
            children: String(row.source_label || row.source || '未知')
          }, 'src'),
          row.configured === false
            ? jsx(Tag, { off: true, title: '这个 provider 的凭据变量没配置（写进去也跑不起来）', children: '未配置 key' }, 'nc')
            : null
        ]
      }, 'name'),
      jsx('span', {
        className: 'hc-models__pv-cell hc-models__pv-url',
        'data-cell': 'url',
        style: { fontFamily: MONO },
        title: String(row.base_url || ''),
        children: orDash(row.base_url)
      }, 'url'),
      jsxs('span', {
        className: 'hc-models__pv-cell hc-models__pv-key',
        'data-cell': 'key_env',
        children: [
          jsx('span', {
            className: 'hc-chip' + (row.key_present === true ? '' : ' hc-chip--off'),
            'data-models-provider-key': keyEnv,
            title: keyTitle,
            children: keyLabel
          }, 'key'),
          maskedKeys.length
            ? jsx('span', { className: 'hc-chip hc-chip--off', title: maskedKeys.join(' / '), children: '已打码' }, 'mask')
            : null
        ]
      }, 'keys')
    ]
  })
}


/**
 * 切换面板：**这是唯一会改真实 config.yaml 的地方**。
 *
 * 交互设计（三条硬约束的落点）：
 *   1. 目标 agent 在标题、选择器、确认条上各出现一次；
 *   2. 点「下一步」只是把「当前 → 目标」摆进确认区（`pending`），**一个请求都不发**；
 *   3. 点「确认切换」才真正 POST /models/apply，并且请求体里带 `confirm: true`
 *      （服务端第二道确认，缺了就是 400）。
 */
// ---------------------------------------------------------------------------
// 统一编辑器：单个 agent 与批量 agent 共用**同一套**表单
//
// 为什么只有一个编辑器：整改前的页面把「作用域 + provider + 模型」放了三遍
// （顶部 TargetBanner/AgentPicker、右侧 SwitchPanel、批量 BatchPanel），
// 同一个动作有三条入口、三套文案。现在只有一套：
//
//   * 选 1 个  → `data-models-mode="single"`：字段预填该 agent 的现值，直接改；
//   * 选 N 个  → `data-models-mode="batch"` ：字段默认「保持不变」，点了才写。
//
// 「保持不变」是**硬语义**，不是文案：见 resolveRequests() —— 取值时用该 agent
// 自己的现值，与现值相同就**不进任何请求**，因此不可能覆盖没被改的 agent。
// ---------------------------------------------------------------------------

/** 字段两态：`keep` = 保持不变（这个字段不进请求）；`set` = 写入给定值。 */
var FIELD_KEEP = 'keep'
var FIELD_SET = 'set'

/** 某个概览行的某个段的现值 `(provider, model)`；段不存在时给空串。 */
function pairOfRow(row, scope) {
  var source = row || {}
  if (scope === 'fallback') {
    var list = Array.isArray(source.fallbacks) ? source.fallbacks : []
    var head = list.length && list[0] && typeof list[0] === 'object' ? list[0] : {}
    return { provider: String(head.provider || ''), model: String(head.model || '') }
  }
  return { provider: String(source.provider || ''), model: String(source.model || '') }
}

/** 按 id 找概览行（找不到给空对象，调用方一律走 orDash）。 */
function rowOfId(rows, id) {
  var hit = null
  ;(rows || []).forEach(function (row) {
    if (agentOf(row) === id) hit = row
  })
  return hit || {}
}

/**
 * 编辑器的字段初值。
 * 单个：直接预填现值（能看见「现在是什么」，改哪个改哪个）；
 * 批量：一律「保持不变」（不点就不改，绝不静默覆盖）。
 * @returns {{provider: {mode: string, value: string}, model: {mode: string, value: string}}}
 */
function defaultFields(mode, row, scope) {
  if (mode !== 'single') {
    return {
      provider: { mode: FIELD_KEEP, value: '' },
      model: { mode: FIELD_KEEP, value: '' }
    }
  }
  var pair = pairOfRow(row, scope)
  return {
    provider: { mode: pair.provider ? FIELD_SET : FIELD_KEEP, value: pair.provider },
    model: { mode: pair.model ? FIELD_SET : FIELD_KEEP, value: pair.model }
  }
}

/**
 * ★「保持不变」解析器（纯函数，无副作用，可独立断言）。
 *
 * 输入：选中的 agent id 列表 + 段的现值 + 两个字段的两态；
 * 输出：真正要发的写请求（按 `(provider, model)` 分组）+ 被跳过 / 无需写的 agent。
 *
 * 三条不许退让的语义：
 *   1. 字段是 `keep` ⇒ 用**该 agent 自己的现值**填，绝不发空串；
 *   2. 目标值与现值相同 ⇒ 该 agent **不进请求**（写它等于覆盖，宁可不动）；
 *   3. 现值本身是空的（没配过）⇒ 这个 agent 进 `skipped` 并说清为什么，
 *      不让它被一个「保持不变」的空值写坏。
 *
 * 为什么要分组：后端 `/models/batch/apply` 只接受**成对**的 (provider, model)
 * 且一次一个值（禁止改后端写接口）。所以「provider 换了、模型保持不变」在
 * 多 agent 场景下必须按各自的现值分组，一组一个请求 —— 这样每个 agent 拿到的
 * 都恰好是它自己的现值，语义与单个编辑完全一致。
 *
 * @param {{rows: Array, ids: Array<string>, scope: string,
 *          providerField: {mode: string, value: string},
 *          modelField: {mode: string, value: string}}} opts
 * @returns {{requests: Array, skipped: Array, unchanged: Array, error: string}}
 */
function resolveRequests(opts) {
  var source = opts || {}
  var rows = Array.isArray(source.rows) ? source.rows : []
  var ids = Array.isArray(source.ids) ? source.ids : []
  var scope = source.scope === 'fallback' ? 'fallback' : 'model'
  var providerField = source.providerField || { mode: FIELD_KEEP, value: '' }
  var modelField = source.modelField || { mode: FIELD_KEEP, value: '' }
  var out = { requests: [], skipped: [], unchanged: [], error: '' }

  if (!ids.length) {
    out.error = '先在 Agent 表里选一个 agent'
    return out
  }
  var wantedProvider = String(providerField.value || '').trim()
  var wantedModel = String(modelField.value || '').trim()
  if (providerField.mode === FIELD_SET && !wantedProvider) {
    out.error = 'provider 选了「写入」但值是空的：填一个 provider，或改回「保持不变」'
    return out
  }
  if (modelField.mode === FIELD_SET && !wantedModel) {
    out.error = '模型选了「写入」但值是空的：填一个模型名，或改回「保持不变」'
    return out
  }
  if (providerField.mode === FIELD_KEEP && modelField.mode === FIELD_KEEP) {
    out.error = 'provider 与模型都是「保持不变」：没有要写的内容'
    return out
  }

  var groups = {}
  var order = []
  ids.forEach(function (id) {
    var pair = pairOfRow(rowOfId(rows, id), scope)
    var provider = providerField.mode === FIELD_SET ? wantedProvider : pair.provider
    var model = modelField.mode === FIELD_SET ? wantedModel : pair.model
    var kept = []
    if (providerField.mode === FIELD_KEEP) kept.push('provider')
    if (modelField.mode === FIELD_KEEP) kept.push('model')
    if (!provider || !model) {
      out.skipped.push({
        agent: id,
        reason:
          '这个 agent 当前的 ' + (!provider ? 'provider' : '模型') +
          ' 是空的，没法当作「保持不变」的值 —— 请显式选一个再写'
      })
      return
    }
    if (provider.toLowerCase() === pair.provider.toLowerCase() && model === pair.model) {
      out.unchanged.push({ agent: id, provider: provider, model: model })
      return
    }
    var key = provider.toLowerCase() + '\u0000' + model
    if (!groups[key]) {
      groups[key] = { scope: scope, provider: provider, model: model, agents: [], kept: kept }
      order.push(key)
    }
    groups[key].agents.push(id)
  })
  out.requests = order.map(function (key) {
    return groups[key]
  })
  if (!out.requests.length && !out.error) {
    out.error = '选中的 agent 现在就是这个值：没有要写的内容（一个请求都不会发）'
  }
  return out
}

// ---------------------------------------------------------------------------
// 备用链（fallbacks）/ 辅助槽位（auxiliary）的纯函数
//
// 这两段在配置里本来就是**结构化**的：`fallback_providers` 是有序列表、
// `auxiliary` 是一堆按名字分的槽位。整改前界面只给「一个 provider + 一个模型」，
// 那是契约限制。现在照着后端的 scope=fallbacks / scope=auxiliary 做编辑入口：
//   * 备用链：动态列表，可加 / 删 / 调序 / 清空，一次给整条有序链；
//   * 辅助槽位：槽位名与可写字段都来自 `GET /models/overview` 的 `auxiliary_slots`
//     （按配置动态生成，界面**不硬编码**槽位名）。
// 下面这些函数都是纯的（不碰 DOM、不发请求），可以单独断言。
// ---------------------------------------------------------------------------

/** 归一化一条链：只留 {provider, model} 并去空白（与后端 `_fallback_entry_of` 同口径）。 */
function normChain(list) {
  return (Array.isArray(list) ? list : []).map(function (item) {
    var src = item && typeof item === 'object' ? item : {}
    return {
      provider: String(src.provider === undefined || src.provider === null ? '' : src.provider).trim(),
      model: String(src.model === undefined || src.model === null ? '' : src.model).trim()
    }
  })
}

/** 概览行 → 该 agent 的备用链（后端 `fallbacks`，已经是 {provider, model} 列表）。 */
function chainOfRow(row) {
  var source = row && typeof row === 'object' ? row : {}
  return normChain(source.fallbacks)
}

/** 两条链是否等价（provider 大小写不敏感、model 逐字，**顺序敏感**）。 */
function sameChain(left, right) {
  var a = normChain(left)
  var b = normChain(right)
  if (a.length !== b.length) return false
  for (var i = 0; i < a.length; i += 1) {
    if (a[i].provider.toLowerCase() !== b[i].provider.toLowerCase()) return false
    if (a[i].model !== b[i].model) return false
  }
  return true
}

/** 一条链的文字摘要（预览 / 回执文案用）。 */
function chainText(chain) {
  var list = normChain(chain)
  if (!list.length) return '（空链）'
  return list
    .map(function (item, index) {
      return '#' + (index + 1) + ' ' + item.provider + ' / ' + item.model
    })
    .join('；')
}

/** 把槽位的可选字段拷成字符串字典（`fields` 里是 reasoning_effort / timeout …）。 */
function copyFields(raw) {
  var source = raw && typeof raw === 'object' ? raw : {}
  var out = {}
  Object.keys(source).forEach(function (key) {
    out[String(key)] = String(source[key] === undefined || source[key] === null ? '' : source[key])
  })
  return out
}

/**
 * 概览行 → 该 agent 的辅助槽位（后端 `auxiliary_slots`）。
 *
 * 缺 `auxiliary_slots`（老后端 / 测试桩）时退回 `auxiliary` 摘要，此时只有
 * provider / model 两个可写字段 —— 仍然能编辑，只是没有可选字段控件。
 */
function slotsOfRow(row) {
  var source = row && typeof row === 'object' ? row : {}
  if (Array.isArray(source.auxiliary_slots) && source.auxiliary_slots.length) {
    return source.auxiliary_slots.filter(function (item) {
      return item && typeof item === 'object' && String(item.slot || '')
    })
  }
  var plain = Array.isArray(source.auxiliary) ? source.auxiliary : []
  return plain
    .map(function (item) {
      var one = item && typeof item === 'object' ? item : {}
      return {
        slot: String(one.slot || ''),
        provider: String(one.provider || ''),
        model: String(one.model || ''),
        fields: {},
        writable_fields: ['provider', 'model'],
        read_only_fields: [],
        key_env: ''
      }
    })
    .filter(function (item) {
      return item.slot
    })
}

/** 某槽位在某 agent 上的**可写字段**（provider / model 恒在；其余按该 agent 的实际 schema）。 */
function writableOfSlot(slotRow) {
  var one = slotRow && typeof slotRow === 'object' ? slotRow : {}
  var out = ['provider', 'model']
  var listed = Array.isArray(one.writable_fields) ? one.writable_fields : []
  listed.forEach(function (name) {
    var text = String(name || '')
    if (text && out.indexOf(text) === -1) out.push(text)
  })
  return out
}

/** 某槽位在某 agent 上的**当前取值**（provider / model 在槽位行上，其余在 fields 里）。 */
function currentOfSlot(slotRow, name) {
  var one = slotRow && typeof slotRow === 'object' ? slotRow : {}
  if (name === 'provider' || name === 'model') {
    return String(one[name] === undefined || one[name] === null ? '' : one[name])
  }
  var fields = one.fields && typeof one.fields === 'object' ? one.fields : {}
  return String(fields[name] === undefined || fields[name] === null ? '' : fields[name])
}

/**
 * 选中 agent 的辅助槽位并集（渲染用视图）。
 *
 * 槽位名来自配置本身，**不硬编码**：上游以后加 `image_gen` 之类的槽位，
 * 这里自动多一行。每行给：槽位名 / 首个 agent 的当前值 / 可写字段并集 / key_env（只读）。
 */
function slotViewsFor(rows, ids) {
  var seen = {}
  var order = []
  ;(ids || []).forEach(function (id) {
    slotsOfRow(rowOfId(rows, id)).forEach(function (item) {
      var name = String(item.slot || '')
      if (!name || seen[name]) return
      seen[name] = {
        slot: name,
        current: {
          provider: currentOfSlot(item, 'provider'),
          model: currentOfSlot(item, 'model'),
          fields: copyFields(item.fields)
        },
        writable: writableOfSlot(item),
        key_env: String(item.key_env || '')
      }
      order.push(name)
    })
  })
  return order.sort().map(function (name) {
    return seen[name]
  })
}

/** 一个槽位视图 → 编辑器的初始值（provider / model / 可选字段全填当前值）。 */
function valuesOfView(view) {
  var one = view && typeof view === 'object' ? view : {}
  var values = {
    provider: String((one.current || {}).provider || ''),
    model: String((one.current || {}).model || '')
  }
  var fields = copyFields((one.current || {}).fields)
  Object.keys(fields).forEach(function (name) {
    values[name] = fields[name]
  })
  return values
}

/** 备用链编辑器的初始状态：单个 agent 预填它现在的链；批量默认「保持不变」。 */
function initialChainState(mode, row) {
  if (mode !== 'single') {
    return { mode: FIELD_KEEP, items: chainOfRow(row) }
  }
  return { mode: FIELD_SET, items: chainOfRow(row) }
}

/** 辅助槽位编辑器的初始状态：每个槽位都填当前值，但**一个字段都没标脏**（零写请求）。 */
function initialAuxState(views) {
  var out = {}
  ;(views || []).forEach(function (view) {
    out[view.slot] = { values: valuesOfView(view), dirty: {} }
  })
  return out
}

/**
 * ★备用链解析器（纯函数）：编辑状态 → 真正要发的写请求。
 *
 * 三条硬语义与主模型那段一致：
 *   1. 状态是 `keep`（保持不变）⇒ **一个请求都不发**；
 *   2. 目标链与某个 agent 现在的链相同 ⇒ 这个 agent 不进请求（写它等于白写）；
 *   3. 一个字段都没填（provider / model 空）⇒ 报错并说清是第几条，不猜。
 *
 * 整条链是**一个整体**（顺序敏感），所以一次请求带上所有"需要改"的 agent，
 * 后端 scope=fallbacks 对每个 agent 各做快照、各写各的。
 *
 * @param {{rows: Array, ids: Array<string>,
 *          chain: {mode: string, items: Array<{provider: string, model: string}>}}} opts
 * @returns {{requests: Array, skipped: Array, unchanged: Array, error: string}}
 */
function resolveChainPlan(opts) {
  var source = opts || {}
  var rows = Array.isArray(source.rows) ? source.rows : []
  var ids = Array.isArray(source.ids) ? source.ids : []
  var state = source.chain && typeof source.chain === 'object' ? source.chain : { mode: FIELD_KEEP, items: [] }
  var out = { requests: [], skipped: [], unchanged: [], error: '' }
  if (!ids.length) {
    out.error = '先在 Agent 表里选一个 agent'
    return out
  }
  if (state.mode !== FIELD_SET) {
    out.error = '备用链是「保持不变」：这一条链一个请求都不会发（要改先点「写入这条链」）'
    return out
  }
  var target = normChain(state.items)
  for (var i = 0; i < target.length; i += 1) {
    if (!target[i].provider) {
      out.error = '第 ' + (i + 1) + ' 条备用模型缺 provider：补上，或删掉这一条'
      return out
    }
    if (!target[i].model) {
      out.error = '第 ' + (i + 1) + ' 条备用模型缺模型名：补上，或删掉这一条'
      return out
    }
  }
  if (target.length > 32) {
    out.error = '备用链最多 32 条（现在 ' + target.length + ' 条）'
    return out
  }
  var agents = []
  ids.forEach(function (id) {
    if (sameChain(chainOfRow(rowOfId(rows, id)), target)) {
      out.unchanged.push({ agent: id })
      return
    }
    agents.push(id)
  })
  if (!agents.length) {
    out.error = '选中的 agent 现在就是这条链：没有要写的内容（一个请求都不会发）'
    return out
  }
  out.requests.push({
    scope: 'fallbacks',
    agents: agents,
    payload: {
      fallback_models: target.map(function (item) {
        return { provider: item.provider, model: item.model }
      })
    },
    label: target.length ? target.length + ' 条备用模型' : '清空备用链'
  })
  return out
}

/**
 * ★辅助槽位解析器（纯函数）：只把**用户真改过**的槽位变成请求。
 *
 * 关键点（验收 E 逐条对应）：
 *   * 没改过的槽位**一个字段都不进请求**（`dirty` 为空 ⇒ 整段跳过）；
 *   * 一个槽位一个请求（`slot` 只出现在自己的请求里）⇒ 改 approval 不会碰 compression；
 *   * 也不碰 `model` / `fallback_providers`（scope 不同，后端只写 auxiliary.<slot>）；
 *   * 某个 agent 没有这个槽位 ⇒ 进 `skipped` 并说明，**不**让它去撞后端 404。
 */
function resolveAuxPlan(opts) {
  var source = opts || {}
  var rows = Array.isArray(source.rows) ? source.rows : []
  var ids = Array.isArray(source.ids) ? source.ids : []
  var state = source.auxiliary && typeof source.auxiliary === 'object' ? source.auxiliary : {}
  var out = { requests: [], skipped: [], unchanged: [], error: '' }
  if (!ids.length) {
    out.error = '先在 Agent 表里选一个 agent'
    return out
  }
  var touched = Object.keys(state).filter(function (name) {
    var one = state[name] || {}
    var dirty = one.dirty && typeof one.dirty === 'object' ? one.dirty : {}
    return Object.keys(dirty).length > 0
  })
  if (!touched.length) {
    out.error = '辅助槽位都还是「保持不变」：一个请求都不会发（先改某个槽位）'
    return out
  }
  var groups = {}
  var order = []
  touched.forEach(function (slot) {
    var one = state[slot] || {}
    var values = one.values && typeof one.values === 'object' ? one.values : {}
    var dirty = one.dirty && typeof one.dirty === 'object' ? one.dirty : {}
    var payload = { slot: slot }
    var fields = {}
    var bad = ''
    if (one.advanced === true) payload.allow_unlisted_model = true
    Object.keys(dirty).forEach(function (name) {
      if (!dirty[name]) return
      var text = String(values[name] === undefined || values[name] === null ? '' : values[name]).trim()
      if (name === 'provider') {
        if (!text) {
          bad = '槽位 ' + slot + ' 的 provider 不能留空（不想改就点「还原」）'
          return
        }
        payload.provider = text
        return
      }
      if (name === 'model') {
        payload.model = text
        return
      }
      if (name === 'timeout') {
        if (!/^\d+$/.test(text)) {
          bad = '槽位 ' + slot + ' 的 timeout 必须是整数秒（0..3600）'
          return
        }
        fields.timeout = Number(text)
        return
      }
      fields[name] = text
    })
    if (bad) {
      out.error = bad
      return
    }
    if (Object.keys(fields).length) payload.fields = fields
    var agents = []
    ids.forEach(function (id) {
      var hit = null
      slotsOfRow(rowOfId(rows, id)).forEach(function (item) {
        if (String(item.slot || '') === slot) hit = item
      })
      if (!hit) {
        out.skipped.push({
          agent: id,
          slot: slot,
          reason: '这个 agent 的 config.yaml 里没有槽位 ' + slot + '（不会写它）'
        })
        return
      }
      var differs = false
      Object.keys(payload).forEach(function (name) {
        if (name === 'slot' || name === 'fields') return
        if (String(payload[name]) !== currentOfSlot(hit, name)) differs = true
      })
      if (payload.fields) {
        Object.keys(payload.fields).forEach(function (name) {
          if (String(payload.fields[name]) !== currentOfSlot(hit, name)) differs = true
        })
      }
      if (!differs) {
        out.unchanged.push({ agent: id, slot: slot })
        return
      }
      agents.push(id)
    })
    if (!agents.length) return
    var key = slot + '\u0000' + JSON.stringify(payload)
    if (!groups[key]) {
      groups[key] = {
        scope: 'auxiliary',
        slot: slot,
        agents: [],
        payload: payload,
        label: '槽位 ' + slot
      }
      order.push(key)
    }
    groups[key].agents = groups[key].agents.concat(agents)
  })
  out.requests = order.map(function (key) {
    return groups[key]
  })
  if (!out.requests.length && !out.error) {
    out.error = '选中的 agent 这些槽位现在就是这个值：没有要写的内容（一个请求都不会发）'
  }
  return out
}

/**
 * ★思考强度解析器（纯函数）：只写 `agent.reasoning_effort` 一个键。
 *
 * 「保持不变」是硬语义：状态不是 `set` ⇒ **零请求**；值等于现值 ⇒ 也不进请求。
 * 后端（`scope=reasoning`）自己还会再核一次"只改了这一个路径"，两边都不许有副作用。
 */
function resolveReasoningPlan(opts) {
  var source = opts || {}
  var rows = Array.isArray(source.rows) ? source.rows : []
  var ids = Array.isArray(source.ids) ? source.ids : []
  var field = source.reasoningField || { mode: FIELD_KEEP, value: '' }
  var out = { requests: [], skipped: [], unchanged: [], error: '' }
  if (!ids.length) {
    out.error = '先在 Agent 表里选一个 agent'
    return out
  }
  if (field.mode !== FIELD_SET) {
    out.error = '思考强度是「保持不变」：这一个键一个请求都不会发'
    return out
  }
  var value = String(field.value || '').trim().toLowerCase()
  if (!value) {
    out.error = '思考强度选了「写入」但值是空的：选一个档位，或改回「保持不变」'
    return out
  }
  var agents = []
  ids.forEach(function (id) {
    var row = rowOfId(rows, id)
    if (String(row.reasoning_effort || '').trim().toLowerCase() === value) {
      out.unchanged.push({ agent: id })
      return
    }
    agents.push(id)
  })
  if (!agents.length) {
    out.error = '选中的 agent 现在就是这个档位：没有要写的内容（一个请求都不会发）'
    return out
  }
  out.requests.push({
    scope: 'reasoning',
    agents: agents,
    payload: { reasoning_effort: value },
    label: '思考强度 → ' + value
  })
  return out
}

/**
 * 四段 scope 的统一入口：主模型 / 备用首条沿用 :func:`resolveRequests`（语义一字不动），
 * 备用链与辅助槽位走上面两个结构化解析器。返回形状统一成
 * `{requests: [{scope, agents, payload, label}], skipped, unchanged, error}`。
 */
function resolvePlan(opts) {
  var source = opts || {}
  var scope = source.scope === 'fallbacks' ? 'fallbacks'
    : source.scope === 'auxiliary' ? 'auxiliary'
      : source.scope === 'reasoning' ? 'reasoning'
        : source.scope === 'fallback' ? 'fallback' : 'model'
  if (scope === 'fallbacks') return resolveChainPlan(source)
  if (scope === 'auxiliary') return resolveAuxPlan(source)
  if (scope === 'reasoning') return resolveReasoningPlan(source)
  var base = resolveRequests({
    rows: source.rows,
    ids: source.ids,
    scope: scope,
    providerField: source.providerField,
    modelField: source.modelField
  })
  return {
    requests: (base.requests || []).map(function (item) {
      return {
        scope: scope,
        agents: item.agents,
        payload: { provider: item.provider, model: item.model },
        label: item.provider + ' / ' + item.model
      }
    }),
    skipped: base.skipped || [],
    unchanged: base.unchanged || [],
    error: base.error || ''
  }
}

/**
 * 备用链编辑器：**动态列表**，任意条数、可添加 / 删除单条 / 上移下移 / 整条清空。
 *
 * 不是 `fallback1/fallback2` 两个固定输入框 —— 链本来就是有序列表，用户可能设 3 条以上。
 * 每条只含 provider + model；顺序就是写入顺序（后端 scope=fallbacks 按给定顺序整链替换）。
 * 删中间一条时其余条目原样保持顺序（filter 掉那一条，不动别人的位置）。
 */
function ChainEditor(props) {
  var state = props.state && typeof props.state === 'object' ? props.state : { mode: FIELD_KEEP, items: [] }
  var items = Array.isArray(state.items) ? state.items : []
  var busy = props.busy === true
  var narrow = props.narrow === true
  var options = Array.isArray(props.options) ? props.options : []
  var kept = state.mode !== FIELD_SET

  var emit = function (nextMode, nextItems) {
    props.onChange({ mode: nextMode, items: nextItems })
  }
  var setItem = function (index, field, value) {
    var next = items.map(function (item, at) {
      var one = { provider: String(item.provider || ''), model: String(item.model || '') }
      if (at === index) one[field] = String(value === undefined || value === null ? '' : value)
      return one
    })
    emit(FIELD_SET, next)
  }
  var moveItem = function (index, delta) {
    var to = index + delta
    if (to < 0 || to >= items.length) return
    var next = items.slice()
    var moved = next.splice(index, 1)[0]
    next.splice(to, 0, moved)
    emit(FIELD_SET, next)
  }
  var removeItem = function (index) {
    emit(FIELD_SET, items.filter(function (item, at) {
      return at !== index
    }))
  }
  var rowStyle = {
    display: 'grid',
    gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : 'minmax(0, 26px) minmax(0, 150px) minmax(0, 1fr) auto',
    gap: 'var(--s2)',
    alignItems: 'center',
    minWidth: 0,
    maxWidth: '100%'
  }

  return jsxs('div', {
    className: 'hc-models__chain',
    'data-models-chain': String(items.length),
    'data-models-chain-mode': kept ? FIELD_KEEP : FIELD_SET,
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', minWidth: 0 },
        children: [
          jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '备用链（按顺序依次尝试）' }, 'k'),
          jsx('span', {
            className: kept ? 'hc-chip hc-chip--off' : 'hc-chip',
            'data-models-chain-state': kept ? FIELD_KEEP : FIELD_SET,
            children: kept ? '保持不变' : '写入 ' + items.length + ' 条'
          }, 'chip'),
          jsx('span', { style: { flex: '1 1 auto' } }, 'sp'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-chain-add': true,
            disabled: busy,
            onClick: function () {
              emit(FIELD_SET, items.concat([{ provider: '', model: '' }]))
            },
            children: '添加一条'
          }, 'add'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-chain-clear': true,
            title: '把整条备用链清空（写入后该 agent 的 fallback_providers 为空）',
            disabled: busy,
            onClick: function () {
              emit(FIELD_SET, [])
            },
            children: '清空整条链'
          }, 'clear'),
          kept
            ? jsx('button', {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-models-chain-set': true,
                title: '把下面这条链写进选中的 agent',
                disabled: busy,
                onClick: function () {
                  emit(FIELD_SET, items)
                },
                children: '写入这条链'
              }, 'set')
            : jsx('button', {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-models-chain-keep': true,
                title: '整条链不动（一个请求都不会发）',
                disabled: busy,
                onClick: function () {
                  emit(FIELD_KEEP, items)
                },
                children: '保持不变'
              }, 'keep')
        ]
      }, 'head'),
      items.length
        ? jsx('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
            children: items.map(function (item, index) {
              return jsxs('div', {
                'data-models-chain-row': String(index),
                style: rowStyle,
                children: [
                  jsx('span', {
                    className: 'hc-muted',
                    style: { fontSize: 'var(--fs-xs)', fontFamily: MONO },
                    children: '#' + (index + 1)
                  }, 'n'),
                  jsx('select', {
                    className: 'hc-select',
                    'data-models-chain-provider': String(index),
                    value: String(item.provider || ''),
                    disabled: busy,
                    onChange: function (event) {
                      setItem(index, 'provider', event.target.value)
                    },
                    children: [jsx('option', { value: '', children: '（选 provider）' }, 'empty')].concat(
                      options.map(function (name) {
                        return jsx('option', { value: name, children: name }, name)
                      })
                    )
                  }, 'p'),
                  jsx('input', {
                    className: 'hc-input',
                    type: 'text',
                    'data-models-chain-model': String(index),
                    value: String(item.model || ''),
                    placeholder: '模型名（可直接手填）',
                    disabled: busy,
                    onChange: function (event) {
                      setItem(index, 'model', event.target.value)
                    }
                  }, 'm'),
                  jsxs('span', {
                    style: { display: 'flex', gap: 'var(--s1)', justifyContent: 'flex-end' },
                    children: [
                      jsx('button', {
                        type: 'button',
                        className: 'hc-btn hc-btn--tiny',
                        'data-models-chain-up': String(index),
                        title: '上移一位',
                        disabled: busy || index === 0,
                        onClick: function () {
                          moveItem(index, -1)
                        },
                        children: '↑'
                      }, 'up'),
                      jsx('button', {
                        type: 'button',
                        className: 'hc-btn hc-btn--tiny',
                        'data-models-chain-down': String(index),
                        title: '下移一位',
                        disabled: busy || index === items.length - 1,
                        onClick: function () {
                          moveItem(index, 1)
                        },
                        children: '↓'
                      }, 'down'),
                      jsx('button', {
                        type: 'button',
                        className: 'hc-btn hc-btn--tiny',
                        'data-models-chain-remove': String(index),
                        title: '删除这一条（其余条目顺序不变）',
                        disabled: busy,
                        onClick: function () {
                          removeItem(index)
                        },
                        children: '删除'
                      }, 'rm')
                    ]
                  }, 'acts')
                ]
              }, 'row-' + String(index))
            })
          }, 'rows')
        : jsx('div', {
            className: 'hc-muted',
            'data-models-chain-empty': true,
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
            children: '这条链现在是空的：写进去会清空 fallback_providers（点「添加一条」加回来）。'
          }, 'empty')
    ]
  })
}

/**
 * 辅助槽位编辑器：**为配置里真实存在的每个槽位**生成一组控件。
 *
 * 槽位清单来自 `GET /models/overview` 的 `auxiliary_slots`（后端按该 agent 的
 * `auxiliary:` 段动态生成），界面**不硬编码**槽位名 —— 上游加槽位不用改代码。
 * 每个槽位给：provider / model（恒有）+ 该槽位**已存在**的可选字段
 * （reasoning_effort / timeout / base_url / api_mode，按 `writable_fields` 给）。
 * `key_env` 这类凭据指针**只读展示**，没有写入控件。
 *
 * 「未改动的槽位不产生写请求」靠 `dirty` 记账：只有用户真改过的字段才标脏，
 * 解析器只把标脏的槽位变成请求；点「还原」清空该槽位的脏标记。
 */
function AuxEditor(props) {
  var views = Array.isArray(props.views) ? props.views : []
  var state = props.state && typeof props.state === 'object' ? props.state : {}
  var agent = String(props.agent || '')
  var busy = props.busy === true
  var narrow = props.narrow === true
  var fieldStyle = {
    display: 'grid',
    gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : 'minmax(0, 92px) minmax(0, 1fr)',
    gap: 'var(--s2)',
    alignItems: 'center',
    minWidth: 0,
    maxWidth: '100%'
  }
  var labelOf = function (name) {
    if (name === 'provider') return 'provider'
    if (name === 'model') return '模型'
    if (name === 'reasoning_effort') return 'reasoning'
    return name
  }
  /** 来源层：provider=auto ⇒ 跟随全局/主模型；否则是该 agent 独立配置。 */
  var sourceOf = function (provider) {
    var text = String(provider || '').trim()
    return !text || text.toLowerCase() === 'auto' ? 'global' : 'agent'
  }

  if (!views.length) {
    return jsx('div', {
      className: 'hc-muted',
      'data-models-aux-empty': true,
      style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
      children: '选中的 agent 的 config.yaml 里没有 auxiliary: 段（没有可编辑的辅助槽位）。'
    }, 'empty')
  }

  return jsx('div', {
    className: 'hc-models__aux',
    'data-models-aux': String(views.length),
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
    children: views.map(function (view) {
      var one = state[view.slot] && typeof state[view.slot] === 'object' ? state[view.slot] : { values: {}, dirty: {} }
      var values = one.values && typeof one.values === 'object' ? one.values : {}
      var dirty = one.dirty && typeof one.dirty === 'object' ? one.dirty : {}
      var dirtyNames = Object.keys(dirty).filter(function (name) {
        return dirty[name]
      })
      return jsxs('div', {
        className: 'hc-models__aux-slot',
        'data-models-aux-slot': view.slot,
        'data-models-aux-dirty': dirtyNames.length ? '1' : '0',
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0, maxWidth: '100%' },
        children: [
          jsxs('div', {
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', minWidth: 0 },
            children: [
              jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-md)', fontWeight: 600 }, children: view.slot }, 'name'),
              jsx('span', {
                className: 'hc-muted',
                style: { fontSize: 'var(--fs-xs)', fontFamily: MONO, minWidth: 0, overflowWrap: 'anywhere' },
                children: '当前 ' + orDash((view.current || {}).provider) + ' / ' + orDash((view.current || {}).model)
              }, 'cur'),
              dirtyNames.length
                ? jsx('span', { className: 'hc-chip hc-chip--on', 'data-models-aux-dirty-chip': '1', children: '已改 ' + dirtyNames.length + ' 项' }, 'dirty')
                : jsx('span', { className: 'hc-chip hc-chip--off', children: '保持不变' }, 'keep'),
              jsx('span', {
                className: 'hc-chip hc-chip--off',
                'data-models-aux-source': view.slot,
                'data-models-aux-source-kind': sourceOf(values.provider),
                title: '来源层：该 Agent 独立配置 / 跟随 Hermes 全局（provider=auto）',
                children: sourceOf(values.provider) === 'global' ? '来源：跟随全局' : '来源：该 Agent 独立配置'
              }, 'src'),
              view.key_env
                ? jsx('span', {
                    className: 'hc-chip hc-chip--off hc-models__aux-key',
                    'data-models-aux-keyenv': view.key_env,
                    title: '凭据指针只读：这里只显示变量名，不写、不回明文（要换 key 请改 .env 里那个变量）',
                    children: 'key_env: ' + view.key_env
                  }, 'key')
                : null,
              jsx('span', { style: { flex: '1 1 auto' } }, 'sp'),
              jsx('button', {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-models-aux-reset': view.slot,
                title: '把这个槽位改回当前值（清掉脏标记，不产生任何写请求）',
                disabled: busy || !dirtyNames.length,
                onClick: function () {
                  props.onReset(view.slot)
                },
                children: '还原'
              }, 'reset')
            ]
          }, 'head'),
          jsx('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
            children: [
              // ★ 两级联动（共享组件）：provider 变了旧 model 立即清空，模型列表只来自当前 provider。
              jsx(ProviderModelPicker, {
                agent: agent,
                value: { provider: values.provider, model: values.model },
                allowFollow: true,
                allowEmptyModel: true,
                followValue: 'auto',
                sourceKind: sourceOf(values.provider),
                idPrefix: 'aux-' + view.slot,
                disabled: busy,
                advancedNote: '该槽位的 model 允许留空（= 跟随 provider 默认）。清单外的名字会以 '
                  + 'allow_unlisted_model=true 提交，后端会在响应里记一条「未校验」。',
                onChange: function (next) {
                  if (typeof props.onPair === 'function') {
                    props.onPair(view.slot, next.provider, next.model, next.advanced === true)
                    return
                  }
                  props.onField(view.slot, 'model', next.model)
                }
              }, 'picker')
            ].concat(view.writable.filter(function (name) {
              // provider / model 已由 ProviderModelPicker 接管（两级联动只此一套）。
              return name !== 'provider' && name !== 'model'
            }).map(function (name) {
              var value = String(values[name] === undefined || values[name] === null ? '' : values[name])
              var control = jsx('input', {
                className: 'hc-input',
                type: name === 'timeout' ? 'number' : 'text',
                'data-models-aux-field': view.slot + ':' + name,
                'data-models-aux-dirty-field': dirty[name] ? '1' : '0',
                value: value,
                disabled: busy,
                onChange: function (event) {
                  props.onField(view.slot, name, event.target.value)
                }
              }, 'input')
              return jsxs('div', {
                style: fieldStyle,
                children: [
                  jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: labelOf(name) }, 'k'),
                  control
                ]
              }, name)
            }))
          }, 'fields')
        ]
      }, view.slot)
    })
  })
}

/**
 * 统一编辑器。单个 / 批量只是「目标集合」不同，表单、预览、确认、回读全同一套。
 *
 * 由父组件用 `key={选择签名}` 挂载：换选择即重挂，字段回到初值（不会把上一批
 * 的输入带到下一批）。
 *
 * 五段 scope（与后端 `SCOPES` 同名）：
 *   model     主模型：provider + 模型（「保持不变」逐字段生效）
 *   fallback  备用链首条（旧语义，保留，避免老调用方找不到入口）
 *   fallbacks 备用链整链：动态列表，可增 / 删 / 调序 / 清空（任意条数）
 *   auxiliary 辅助槽位：槽位名与可写字段都由 overview 的 `auxiliary_slots` 动态给
 *   reasoning 思考强度：只写 `agent.reasoning_effort`，默认「保持不变」⇒ 零请求
 */
function AgentEditor(props) {
  var ids = Array.isArray(props.ids) ? props.ids : []
  var rows = Array.isArray(props.rows) ? props.rows : []
  var providerNames = Array.isArray(props.providerNames) ? props.providerNames : []
  var narrow = props.narrow === true
  var externalBusy = props.busy === true
  var mode = ids.length > 1 ? 'batch' : 'single'
  var singleRow = ids.length === 1 ? rowOfId(rows, ids[0]) : {}
  var firstRow = ids.length ? rowOfId(rows, ids[0]) : {}
  var singleLabel = String(singleRow.label || ids[0] || '')
  var scopeState = useState('model')
  var scope = scopeState[0]
  var fieldsState = useState(defaultFields(mode, singleRow, 'model'))
  var fields = fieldsState[0]
  var chainState = useState(initialChainState(mode, firstRow))
  var chain = chainState[0]
  var auxViews = slotViewsFor(rows, ids)
  var auxState = useState(initialAuxState(auxViews))
  var aux = auxState[0]
  /** 思考强度：默认「保持不变」（`{mode: FIELD_KEEP, value: ''}`）⇒ 零写请求。 */
  var reasoningState = useState({ mode: FIELD_KEEP, value: '' })
  var reasoningField = reasoningState[0]
  var busyState = useState(false)
  var busy = busyState[0] === true || externalBusy
  var errorState = useState('')
  var error = errorState[0]
  var planState = useState(null)
  var plan = planState[0]
  var resultState = useState(null)
  var result = resultState[0]
  var askState = useState(false)
  var asking = askState[0] === true

  /** 目标 provider 的模型候选（拿第一个被选 agent 的视角；moa 给的是预设名）。 */
  var refAgent = ids.length ? ids[0] : 'default'
  /**
   * 候选清单的实时拉取开关：默认 **true**，与模型库（`libLive`）同一口径 ——
   * 候选要显示的是「供应商真实可用的模型」，不是 config.yaml 里手写的那几条。
   * 后端 `live=0` 是默认值且有意的（离线/探针依赖），所以必须在这里显式要 `live=1`。
   */
  var candidatesLiveState = useState(true)
  var candidatesLive = candidatesLiveState[0] !== false
  /** 手动刷新：nonce+1 ⇒ 缓存键变化 ⇒ 一定发新请求且带 `refresh=1`（绕过后端 TTL 缓存）。 */
  var candidatesNonceState = useState(0)
  var candidatesNonce = candidatesNonceState[0]
  var candidatesQuery = useQuery(
    CACHE_MODELS + '/' + refAgent + '/' + fields.provider.value + '/'
      + (candidatesLive ? 'live' : 'config') + '/' + candidatesNonce,
    function () {
      var params = { provider: fields.provider.value }
      if (candidatesLive) params.live = '1'
      if (candidatesNonce > 0) params.refresh = '1'
      return get('/models/models', { params: params, agent: refAgent, timeoutMs: 20000 })
    },
    { staleMs: 30000, enabled: fields.provider.mode === FIELD_SET && Boolean(fields.provider.value) }
  )
  var candidatesBody = payloadOf(candidatesQuery)
  var candidates = candidatesBody && Array.isArray(candidatesBody.models) ? candidatesBody.models : []
  /** 逐条来源（`model_entries[].source` ∈ config / live / config+live），名字→来源。 */
  var candidateSourceOf = {}
  if (candidatesBody && Array.isArray(candidatesBody.model_entries)) {
    candidatesBody.model_entries.forEach(function (entry) {
      var name = String((entry && entry.name) || '')
      if (name) candidateSourceOf[name] = String((entry && entry.source) || '')
    })
  }
  var candidatesLiveInfo = candidatesBody && candidatesBody.live && typeof candidatesBody.live === 'object'
    ? candidatesBody.live : null
  var candidatesLiveFailed = Boolean(candidatesLiveInfo && candidatesLiveInfo.requested && !candidatesLiveInfo.ok)

  var reset = function () {
    planState[1](null)
    askState[1](false)
    errorState[1]('')
    resultState[1](null)
  }

  var pickScope = function (next) {
    var want = next === 'fallbacks' ? 'fallbacks'
      : next === 'auxiliary' ? 'auxiliary'
        : next === 'reasoning' ? 'reasoning'
          : next === 'fallback' ? 'fallback' : 'model'
    scopeState[1](want)
    if (want === 'model' || want === 'fallback') {
      fieldsState[1](defaultFields(mode, singleRow, want))
    }
    reset()
  }

  var setProviderField = function (next) {
    var value = String(next || '')
    fieldsState[1]({
      provider: value === '' ? { mode: FIELD_KEEP, value: '' } : { mode: FIELD_SET, value: value },
      model: fields.model
    })
    reset()
  }

  var setModelValue = function (next) {
    fieldsState[1]({ provider: fields.provider, model: { mode: FIELD_SET, value: String(next || '') } })
    reset()
  }

  var toggleModelKeep = function () {
    fieldsState[1]({
      provider: fields.provider,
      model: fields.model.mode === FIELD_KEEP
        ? { mode: FIELD_SET, value: fields.model.value }
        : { mode: FIELD_KEEP, value: '' }
    })
    reset()
  }

  var setChain = function (next) {
    chainState[1](next)
    reset()
  }

  var setAuxField = function (slot, name, value) {
    var next = {}
    Object.keys(aux).forEach(function (key) {
      next[key] = aux[key]
    })
    var one = aux[slot] && typeof aux[slot] === 'object' ? aux[slot] : { values: {}, dirty: {} }
    var values = {}
    Object.keys(one.values || {}).forEach(function (key) {
      values[key] = one.values[key]
    })
    var dirty = {}
    Object.keys(one.dirty || {}).forEach(function (key) {
      dirty[key] = one.dirty[key]
    })
    values[name] = String(value === undefined || value === null ? '' : value)
    dirty[name] = true
    next[slot] = { values: values, dirty: dirty, advanced: one.advanced === true }
    auxState[1](next)
    reset()
  }

  /**
   * 槽位的 **provider + model 一次写完**。
   *
   * 为什么不是连着调两次 `setAuxField`：那个函数用闭包里的 `aux` 重建整份 state，
   * 同一个事件里连调两次 ⇒ 第二次把第一次的 provider 改动覆盖掉（实测：换 provider
   * 后 model 清单没变、旧 model 还在）。两级联动必须原子地写这两个键。
   */
  var setAuxPair = function (slot, nextProvider, nextModel, advanced) {
    var next = {}
    Object.keys(aux).forEach(function (key) {
      next[key] = aux[key]
    })
    var one = aux[slot] && typeof aux[slot] === 'object' ? aux[slot] : { values: {}, dirty: {} }
    var values = {}
    Object.keys(one.values || {}).forEach(function (key) {
      values[key] = one.values[key]
    })
    var dirty = {}
    Object.keys(one.dirty || {}).forEach(function (key) {
      dirty[key] = one.dirty[key]
    })
    var providerText = String(nextProvider === undefined || nextProvider === null ? '' : nextProvider)
    var modelText = String(nextModel === undefined || nextModel === null ? '' : nextModel)
    if (String(values.provider || '') !== providerText) {
      values.provider = providerText
      dirty.provider = true
    }
    if (String(values.model || '') !== modelText) {
      values.model = modelText
      dirty.model = true
    }
    next[slot] = { values: values, dirty: dirty, advanced: advanced === true }
    auxState[1](next)
    reset()
  }

  var resetAuxSlot = function (slot) {
    var view = null
    auxViews.forEach(function (item) {
      if (item.slot === slot) view = item
    })
    var next = {}
    Object.keys(aux).forEach(function (key) {
      next[key] = aux[key]
    })
    next[slot] = { values: valuesOfView(view), dirty: {} }
    auxState[1](next)
    reset()
  }

  var setReasoningField = function (next) {
    var value = String(next === undefined || next === null ? '' : next)
    reasoningState[1]({ mode: value ? FIELD_SET : FIELD_KEEP, value: value })
    reset()
  }

  var currentPlan = function () {
    return resolvePlan({
      rows: rows,
      ids: ids,
      scope: scope,
      providerField: fields.provider,
      modelField: fields.model,
      chain: chain,
      auxiliary: aux,
      reasoningField: reasoningField
    })
  }

  /** 预览：逐请求调 `/models/batch/preview`（**纯读**，后端自己也是零副作用）。 */
  var askPreview = function () {
    var next = currentPlan()
    if (next.error) {
      errorState[1](next.error)
      planState[1](null)
      return
    }
    busyState[1](true)
    errorState[1]('')
    resultState[1](null)
    askState[1](false)
    var requests = next.requests
    var merged = []
    var seq = Promise.resolve()
    requests.forEach(function (request, index) {
      seq = seq.then(function () {
        var body = { agents: request.agents, scope: request.scope }
        Object.keys(request.payload || {}).forEach(function (key) {
          body[key] = request.payload[key]
        })
        return send('/models/batch/preview', body, { method: 'POST', timeoutMs: 30000 }).then(function (res) {
          if (!res || !res.ok) {
            merged.push({
              agent: '（第 ' + (index + 1) + ' 组）',
              label: request.label || '',
              group: index,
              slot: request.slot || '',
              target: request.label || '',
              writable: false,
              changes: [],
              warnings: [],
              error: String((res && res.message) || '预览失败')
            })
            return
          }
          var bodyOut = res.data && typeof res.data === 'object' ? res.data : {}
          var rowsOut = Array.isArray(bodyOut.agents) ? bodyOut.agents : []
          rowsOut.forEach(function (row) {
            merged.push({
              agent: String(row.agent || ''),
              label: String(row.label || row.agent || ''),
              group: index,
              slot: request.slot || '',
              target: request.label || '',
              writable: row.writable === true,
              changes: Array.isArray(row.changes) ? row.changes : [],
              warnings: Array.isArray(row.warnings) ? row.warnings : [],
              error: String(row.error || '')
            })
          })
        })
      })
    })
    seq.then(function () {
      busyState[1](false)
      planState[1]({ requests: requests, rows: merged, skipped: next.skipped, unchanged: next.unchanged })
    })
  }

  /** 写入：只有确认条点了「确认写入」才走到这里；用的就是预览时那一份 requests。 */
  var performApply = function () {
    if (!plan) return
    busyState[1](true)
    askState[1](false)
    errorState[1]('')
    var out = []
    var seq = Promise.resolve()
    plan.requests.forEach(function (request) {
      seq = seq.then(function () {
        var body = {
          scope: request.scope,
          confirm: true,
          note: 'console ' + (mode === 'single' ? '单个编辑' : '批量编辑') +
            '（' + request.agents.length + ' 个 agent）' + (request.slot ? ' · 槽位 ' + request.slot : '')
        }
        Object.keys(request.payload || {}).forEach(function (key) {
          body[key] = request.payload[key]
        })
        var isSingle = mode === 'single' && request.agents.length === 1
        if (isSingle) body.agent = request.agents[0]
        else body.agents = request.agents
        return send(isSingle ? '/models/apply' : '/models/batch/apply', body,
          { method: 'POST', timeoutMs: isSingle ? 60000 : 120000 }).then(function (res) {
          if (res && res.ok) {
            var data = res.data && typeof res.data === 'object' ? res.data : {}
            var list = isSingle ? [{
              agent: request.agents[0],
              label: data.label || request.agents[0],
              ok: true,
              changed: data.changed !== false,
              verify: Array.isArray(data.verify) ? data.verify : [],
              snapshot_ts: data.snapshot_ts || '',
              target: request.label || '',
              error: ''
            }] : (Array.isArray(data.results) ? data.results : []).map(function (item) {
              return {
                agent: String(item.agent || ''),
                label: String(item.label || item.agent || ''),
                ok: item.ok === true,
                changed: item.changed !== false,
                verify: Array.isArray(item.verify) ? item.verify : [],
                snapshot_ts: item.snapshot_ts || '',
                target: request.label || '',
                error: String(item.error || '')
              }
            })
            list.forEach(function (item) {
              item.batch_id = data.batch_id || ''
              item.batch_path = data.batch_path || ''
              out.push(item)
            })
            return
          }
          request.agents.forEach(function (id) {
            out.push({
              agent: id,
              label: id,
              ok: false,
              changed: false,
              verify: [],
              snapshot_ts: '',
              target: request.label || '',
              error: String((res && res.message) || '写入失败') +
                (res && res.code ? '（' + res.code + '）' : '')
            })
          })
        })
      })
    })
    seq.then(function () {
      busyState[1](false)
      resultState[1]({ rows: out })
      // 写成功之后，辅助槽位里那些「刚写进去的值」就是新的现值：把脏标记清掉，
      // 否则下一次预览会拿旧现值去比，把没改的槽位又算成「要写」。
      if (scope === 'auxiliary') {
        var settled = {}
        Object.keys(aux).forEach(function (slot) {
          var one = aux[slot] || {}
          settled[slot] = { values: one.values || {}, dirty: {} }
        })
        auxState[1](settled)
      }
      refreshAll(CACHE_PREFIX)
    })
  }


  // ---- 字段行 -------------------------------------------------------------
  var fieldRowStyle = {
    display: 'grid',
    gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : 'minmax(0, 96px) minmax(0, 1fr)',
    gap: 'var(--s2)',
    alignItems: 'center',
    minWidth: 0,
    maxWidth: '100%'
  }
  var modeChip = function (which) {
    var state = which === 'provider' ? fields.provider : fields.model
    var kept = state.mode === FIELD_KEEP
    return jsx('span', {
      className: kept ? 'hc-chip hc-chip--off' : 'hc-chip',
      'data-models-field-mode': kept ? FIELD_KEEP : FIELD_SET,
      title: kept ? '这一字段不进请求：每个 agent 保留自己的现值' : '这一字段会写进请求',
      children: kept ? '保持不变' : '写入'
    }, 'mode')
  }

  var providerOptions = []
  providerNames.forEach(function (name) {
    providerOptions.push(String(name))
  })
  ;[fields.provider.value, pairOfRow(singleRow, scope).provider].forEach(function (name) {
    var text = String(name || '')
    if (text && providerOptions.indexOf(text) === -1) providerOptions.push(text)
  })

  var providerSelect = jsxs('select', {
    className: 'hc-select',
    'data-models-editor-provider': mode,
    value: fields.provider.mode === FIELD_KEEP ? '' : fields.provider.value,
    disabled: busy,
    onChange: function (event) {
      setProviderField(event.target.value)
    },
    children: [
      jsx('option', {
        value: '',
        children: mode === 'single' ? '（保持不变：用该 agent 现在的 provider）' : '保持不变'
      }, 'keep')
    ].concat(providerOptions.map(function (name) {
      return jsx('option', { value: name, children: name }, name)
    }))
  }, 'select')

  var modelInput = jsx('input', {
    className: 'hc-input',
    type: 'text',
    'data-models-editor-model': mode,
    value: fields.model.mode === FIELD_KEEP ? '' : fields.model.value,
    placeholder: fields.model.mode === FIELD_KEEP ? '保持不变（每个 agent 用自己现在的模型）' : '模型名（可直接手填）',
    disabled: busy,
    onChange: function (event) {
      setModelValue(event.target.value)
    }
  }, 'input')

  // 「模型保持不变」在单个与批量两种模式下都出现：
  // 单个模式默认是「写入」（预填现值），但也可以显式说「模型一个字都别动」——
  // 这样两种模式的语义完全一致，不用记两套规则。
  var modelKeepToggle = jsxs('label', {
        className: 'hc-muted',
        style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', fontSize: 'var(--fs-xs)' },
        children: [
          jsx('input', {
            type: 'checkbox',
            'data-models-editor-model-keep': fields.model.mode === FIELD_KEEP ? '1' : '0',
            checked: fields.model.mode === FIELD_KEEP,
            disabled: busy,
            onChange: toggleModelKeep
          }, 'cb'),
          jsx('span', { children: '模型保持不变' }, 't')
        ]
      }, 'keep')

  // ---- 模型候选（moa 的"模型"是预设名，来源必须如实说明） ------------------
  var candidatesSource = candidatesBody && candidatesBody.source ? String(candidatesBody.source) : ''
  var candidatesNote = candidatesBody && candidatesBody.note ? String(candidatesBody.note) : ''
  var candidatesKind = candidatesBody && candidatesBody.kind ? String(candidatesBody.kind) : ''
  var candidatesLiveErrorKind = candidatesLiveInfo && candidatesLiveInfo.error_kind
    ? String(candidatesLiveInfo.error_kind) : ''
  var candidatesLiveError = candidatesBody && candidatesBody.live_error
    ? String(candidatesBody.live_error)
    : (candidatesLiveInfo && candidatesLiveInfo.error ? String(candidatesLiveInfo.error) : '')
  var candidatesLiveCount = candidatesBody && typeof candidatesBody.live_count === 'number'
    ? candidatesBody.live_count : 0
  var candidatesConfigCount = candidatesBody && typeof candidatesBody.config_count === 'number'
    ? candidatesBody.config_count : 0
  /**
   * 候选的**默认口径**：只列供应商实时成功返回的模型（与 Picker 同一口径，
   * 见 `lib/provider-model-picker.js` 的 `mainModels`）。
   *
   * 后端的 `candidates` 是 config ∪ live 的并集，直接铺成候选 chip 等于把 config.yaml 里的
   * 陈旧项冒充「供应商当前可用」。所以按逐条来源分两组：
   *   * `candidateMain`：`source ∈ {live, config+live}` —— 供应商这次真返回了；
   *   * `candidateConfigOnly`：`source === 'config'` —— 只在 config.yaml 里，
   *     供应商没返回（含**实时拉取失败**的情形：那时一条实时结果都没有，
   *     配置项全落在这一组，绝不冒充实时结果）。
   * 「只看配置」（`candidatesLive=false`）时没有实时结果可比，全部进主列表。
   */
  var candidatesEntriesListed = Boolean(
    candidatesBody && Array.isArray(candidatesBody.model_entries) && candidatesBody.model_entries.length
  )
  var candidateMain = []
  var candidateConfigOnly = []
  candidates.forEach(function (name) {
    var one = String(name)
    var oneSource = String(candidateSourceOf[one] || '')
    var isLiveBacked = oneSource === 'live' || oneSource === 'config+live'
    if (!candidatesLive || !candidatesEntriesListed || isLiveBacked) candidateMain.push(one)
    else candidateConfigOnly.push(one)
  })
  var CANDIDATE_SOURCE_BADGE = { config: '配置', live: '实时', 'config+live': '配置+实时' }
  /** 单条候选 chip —— **只用于主列表**（供应商实时返回的那些）。 */
  var candidateChip = function (name) {
    var text = String(name)
    var on = fields.model.mode === FIELD_SET && fields.model.value === text
    var src = String(candidateSourceOf[text] || '')
    var badge = CANDIDATE_SOURCE_BADGE[src] || ''
    return jsx('button', {
      type: 'button',
      className: on ? 'hc-chip hc-chip--on' : 'hc-chip',
      'data-models-editor-model-pick': text,
      // 逐条标来源：配置里手写的 / 供应商实时返回的，一眼分得清（不是混成一个数字）。
      'data-models-editor-model-pick-source': src,
      'data-models-editor-model-pick-group': 'live',
      title: badge ? text + '（来源：' + badge + '）' : text,
      disabled: busy,
      onClick: function () {
        setModelValue(text)
      },
      children: badge ? text + ' · ' + badge : text
    }, 'live-' + text)
  }
  /**
   * 主列表**全量渲染**（不再截前 24 条）：主列表现在只剩供应商实时返回的那些，
   * 条数就是验收口径（balance 37 / gemini 44），截断会让 DOM 数与真值对不上。
   * 高度由容器 `max-height + overflow:auto` 兜住，不会铺满整屏。
   */
  var candidateMainShown = candidateMain
  /** 配置独有项的单条（只读提示用，**不在**主候选容器内）。 */
  var candidateConfigChip = function (name) {
    var text = String(name)
    var isCurrent = fields.model.mode === FIELD_SET && fields.model.value === text
    return jsx('span', {
      'data-models-candidates-config-only-item': text,
      'data-models-candidates-config-only-current': isCurrent ? '1' : '0',
      style: {
        border: 'var(--hairline) solid currentColor',
        borderRadius: 'var(--r1, 4px)',
        padding: '0 var(--s1)',
        fontFamily: MONO,
        fontSize: 'var(--fs-xs)',
        fontWeight: isCurrent ? 700 : 400
      },
      title: '只在 config.yaml 里，供应商这次没返回（不会进候选主列表）',
      children: isCurrent ? text + '（当前值）' : text
    }, 'cfg-' + text)
  }
  var candidateChips = fields.provider.mode === FIELD_SET && candidates.length
    ? jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0, marginTop: 'var(--s1)' },
        children: [
          jsx('div', {
            'data-models-candidates-main': '1',
            'data-models-candidates-main-count': String(candidateMain.length),
            style: {
              display: 'flex',
              gap: 'var(--s1)',
              flexWrap: 'wrap',
              minWidth: 0,
              maxHeight: '11em',
              overflow: 'auto'
            },
            children: candidateMainShown.map(function (name) {
              return candidateChip(name)
            })
          }, 'main'),
          /**
           * ★ 「已配置 · 供应商未返回」：**不在主候选容器里**（主候选只数供应商实时返回的）。
           * 只读展示 + 精确计数，当前值若落在这批里明确标「当前值」。
           */
          candidateConfigOnly.length
            ? jsxs('div', {
                'data-models-candidates-config-only': '1',
                'data-models-candidates-config-only-count': String(candidateConfigOnly.length),
                style: { display: 'flex', gap: 'var(--s1)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0 },
                children: [
                  jsx('span', {
                    className: 'hc-muted',
                    style: { fontSize: 'var(--fs-xs)', fontWeight: 600 },
                    children: '另有 ' + candidateConfigOnly.length + ' 项已配置但供应商未返回（只读，不在候选主列表里）'
                  }, 'lbl')
                ].concat(candidateConfigOnly.map(function (name) {
                  return candidateConfigChip(name)
                }))
              }, 'cfgonly')
            : null
        ]
      }, 'candidates')
    : null

  /**
   * 候选来源说明。
   *
   * moa 的"模型"不是 OpenAI 风格的 model id，而是该 agent `config.yaml` 里
   * `moa.presets` 的**预设名**（宿主 `moa_loop.py`：`resolved_preset = agent.model`）。
   * 后端的 `source` 有三种取值，界面必须**如实**显示，不许把"没有预设"说成"有预设"：
   *   moa-presets        有预设，列全部
   *   moa-presets-empty  有 moa: 段但没预设（宿主回退内置默认配方）
   *   moa-presets-missing 压根没有 moa: 段（切过去用宿主内置配方，不是 root 的预设）
   */
  var candidatesNoteBlock = fields.provider.mode === FIELD_SET &&
    (candidatesSource || candidatesNote || candidatesLiveFailed || candidatesLiveInfo)
    ? jsxs('div', {
        'data-models-candidates-source': candidatesSource,
        'data-models-candidates-count': String(candidates.length),
        'data-models-candidates-main-count': String(candidateMain.length),
        'data-models-candidates-config-only-count': String(candidateConfigOnly.length),
        'data-models-candidates-live': candidatesLive ? '1' : '0',
        'data-models-candidates-live-count': String(candidatesLiveCount),
        'data-models-candidates-config-count': String(candidatesConfigCount),
        'data-models-candidates-live-error-kind': candidatesLiveErrorKind,
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0, marginTop: 'var(--s1)' },
        children: [
          jsxs('div', {
            style: { display: 'flex', gap: 'var(--s1)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0 },
            children: [
              jsx('span', {
                className: 'hc-chip hc-chip--off',
                'data-models-candidates-badge': candidatesSource,
                style: { fontFamily: MONO },
                children: candidatesSource || 'unknown'
              }, 'src'),
              candidatesKind === 'virtual'
                ? jsx(Tag, { off: true, title: '虚拟 provider：base_url 是本地标记，不联网、不需要 key', children: '虚拟 provider' }, 'v')
                : null,
              jsx('span', {
                className: 'hc-muted',
                style: { fontSize: 'var(--fs-xs)' },
                children: candidates.length
                  ? '候选 ' + num(candidateMain.length) + ' 个（供应商实时返回'
                    + (candidateConfigOnly.length ? '，另有 ' + num(candidateConfigOnly.length)
                      + ' 项已配置但供应商未返回，只读列出、不进候选' : '')
                    + '；点一个就填进模型框，也可手填）'
                  : '没有候选（可以直接手填）'
              }, 'n'),
              jsx('button', {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-models-candidates-refresh': candidatesLive ? '1' : '0',
                title: '重新向该 provider 拉取候选（请求带 live=1&refresh=1，绕过缓存）',
                disabled: busy || !fields.provider.value,
                onClick: function () {
                  candidatesNonceState[1](function (n) {
                    return n + 1
                  })
                },
                children: candidatesQuery.busy ? '刷新中…' : '刷新候选'
              }, 'refresh'),
              jsx('label', {
                className: 'hc-muted',
                style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', fontSize: 'var(--fs-xs)' },
                children: [
                  jsx('input', {
                    type: 'checkbox',
                    'data-models-candidates-live-toggle': candidatesLive ? '1' : '0',
                    checked: candidatesLive,
                    disabled: busy,
                    onChange: function (event) {
                      candidatesLiveState[1](event.target.checked !== false)
                    }
                  }, 'cb'),
                  jsx('span', { children: candidatesLive ? '实时拉取' : '只看配置' }, 't')
                ]
              }, 'livetoggle')
            ]
          }, 'badge'),
          // ★ 实时拉取失败 ⇒ 显式报错（error_kind + 后端原文），不许静默只剩配置项。
          candidatesLiveFailed
            ? jsxs('div', {
                'data-models-candidates-live-error': true,
                'data-models-candidates-live-error-kind': candidatesLiveErrorKind,
                role: 'alert',
                style: {
                  fontSize: 'var(--fs-xs)',
                  color: 'var(--ui-danger, inherit)',
                  border: 'var(--hairline) solid currentColor',
                  borderRadius: 'var(--r1, 4px)',
                  padding: 'var(--s1) var(--s2)',
                  overflowWrap: 'anywhere',
                  minWidth: 0
                },
                children: [
                  jsx('span', { children: '实时拉取失败 · ' + (candidatesLiveErrorKind || 'unknown') }, 'k'),
                  jsx('span', {
                    style: { display: 'block', fontWeight: 400 },
                    children: candidatesLiveError || '（后端没有给出原因）'
                  }, 'det'),
                  jsx('span', {
                    style: { display: 'block', fontWeight: 400 },
                    children: '下面这份候选是 config.yaml 里显式列出的，不是供应商当前真实可用的模型；'
                      + '点「刷新候选」重试，或按错误原因修好该 provider 的 base_url / 凭据。'
                  }, 'note2')
                ]
              }, 'liveerr')
            : null,
          candidatesNote
            ? jsx('span', {
                className: 'hc-muted',
                'data-models-candidates-note': true,
                style: { fontSize: 'var(--fs-xs)', minWidth: 0, overflowWrap: 'anywhere' },
                children: candidatesNote
              }, 'note')
            : null
        ]
      }, 'candnote')
    : null

  // ---- 思考强度（agent.reasoning_effort，scope=reasoning） -------------------
  // 后端已上线 scope=reasoning（`SCOPES` 含它；overview 给 `reasoning_effort_values`
  // 与 `reasoning_effort_writable`）。控件**按后端能力开关**：声明了就真能改、保存后
  // 从后端回读一致；没声明就禁用并注明「后端待支持」—— 绝不出现"能点、点了静默失败、
  // 或存了无效值还报成功"。默认「保持不变」⇒ 零写请求；只写这一个键，别的段一个字节不动。
  var REASONING_FALLBACK_CHOICES = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']
  var reasoningChoices = Array.isArray(props.reasoningValues) && props.reasoningValues.length
    ? props.reasoningValues.map(function (item) { return String(item) })
    : REASONING_FALLBACK_CHOICES
  var reasoningWritable = props.reasoningWritable === true
  var reasoningCurrent = mode === 'single' ? String(singleRow.reasoning_effort || '') : ''
  var reasoningOverrides = mode === 'single' && singleRow.reasoning && typeof singleRow.reasoning === 'object'
    ? singleRow.reasoning.overrides
    : null
  var reasoningOverrideNames = reasoningOverrides && typeof reasoningOverrides === 'object'
    ? Object.keys(reasoningOverrides)
    : []
  var reasoningRow = jsxs('div', {
    className: 'hc-models__reasoning',
    style: fieldRowStyle,
    children: [
      jsxs('span', {
        style: { display: 'inline-flex', gap: 'var(--s1)', alignItems: 'center', minWidth: 0 },
        children: [
          jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '思考强度' }, 'k'),
          jsx('span', {
            className: reasoningField.mode === FIELD_SET ? 'hc-chip hc-chip--on' : 'hc-chip hc-chip--off',
            'data-models-reasoning-mode': reasoningField.mode,
            title: reasoningWritable
              ? '选档位就写 agent.reasoning_effort；选回「保持不变」⇒ 零写请求'
              : '后端 scope 待支持：现在只读展示',
            children: reasoningField.mode === FIELD_SET ? '写入' : '保持不变'
          }, 'mode')
        ]
      }, 'k'),
      jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
        children: [
          jsx('select', {
            className: 'hc-select',
            'data-models-editor-reasoning': reasoningWritable ? 'enabled' : 'disabled',
            'data-models-reasoning-value': reasoningCurrent,
            'data-models-reasoning-writable': reasoningWritable ? '1' : '0',
            value: reasoningField.mode === FIELD_SET ? reasoningField.value : '',
            disabled: busy || !reasoningWritable,
            title: reasoningWritable
              ? '只写 agent.reasoning_effort 一个键；选「保持不变」不发任何请求'
              : '后端 scope 待支持：现在只读展示，不会写 agent.reasoning_effort',
            onChange: function (event) {
              setReasoningField(event.target.value)
            },
            children: [
              jsx('option', {
                value: '',
                children: '保持不变（当前：' + (reasoningCurrent || '未设置') + '）'
              }, 'keep')
            ].concat(reasoningChoices.map(function (name) {
              return jsx('option', { value: name, children: name }, name)
            }))
          }, 'select'),
          jsx('span', {
            className: 'hc-muted',
            'data-models-reasoning-note': true,
            style: { fontSize: 'var(--fs-xs)', minWidth: 0, overflowWrap: 'anywhere' },
            children: reasoningWritable
              ? (mode === 'batch'
                  ? '批量：只写你选的档位；某个 agent 现值已经相同就跳过（不算写请求）。改模型 / 备用链 / 辅助槽位都不会顺带碰它。'
                  : '只写 agent.reasoning_effort（当前「' + (reasoningCurrent || '未设置') + '」）。' +
                    (reasoningOverrideNames.length
                      ? '注意：这些模型有按模型覆盖（' + reasoningOverrideNames.join('、') + '），改全局对它们无效。'
                      : '') +
                    '选「保持不变」⇒ 零写请求。')
              : '后端待支持：agent.reasoning_effort 的写入 scope 还没上线（当前值是「' +
                (reasoningCurrent || '未设置') + '」）。本控件禁用，改它不会产生任何写请求。'
          }, 'note')
        ]
      }, 'v')
    ]
  }, 'reasoning')

  // ---- 解析摘要（发请求前先让人看清会发几个请求、跳过谁） -----------------
  var livePlan = currentPlan()
  var summary = jsxs('div', {
    className: 'hc-muted',
    'data-models-plan': String(livePlan.requests.length),
    style: { fontSize: 'var(--fs-xs)', display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0, overflowWrap: 'anywhere' },
    children: [
      jsx('span', {
        children: livePlan.error
          ? '现在发不出去：' + livePlan.error
          : '将发 ' + num(livePlan.requests.length) + ' 个写请求，覆盖 ' +
            num(livePlan.requests.reduce(function (sum, item) {
              return sum + item.agents.length
            }, 0)) + ' 个 agent；' +
            (livePlan.unchanged.length ? '「与现值相同、不写」' + num(livePlan.unchanged.length) + ' 个；' : '') +
            (livePlan.skipped.length ? '「跳过」' + num(livePlan.skipped.length) + ' 个' : '')
      }, 'line'),
      livePlan.requests.length
        ? jsx('span', {
            children: '要写的：' + livePlan.requests.map(function (item) {
              return (item.label || item.scope) + ' × ' + item.agents.length
            }).join('；')
          }, 'groups')
        : null,
      livePlan.skipped.length
        ? jsx('span', {
            children: '跳过：' + livePlan.skipped.map(function (item) {
              return item.agent + (item.slot ? '·' + item.slot : '') + '（' + item.reason + '）'
            }).join('；')
          }, 'skip')
        : null,
      livePlan.unchanged.length
        ? jsx('span', {
            children: '与现值相同、不会写：' + livePlan.unchanged.map(function (item) {
              return item.agent + (item.slot ? '·' + item.slot : '')
            }).join('、')
          }, 'same')
        : null
    ]
  }, 'plan')


  // ---- 预览表 -------------------------------------------------------------
  var previewBlock = plan
    ? jsxs('div', {
        className: 'hc-models__preview',
        'data-models-preview': String(plan.rows.length),
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, marginTop: 'var(--s3)' },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
            children: [
              jsx('div', { className: 'hc-block__title', children: '预览：将写入这些行（还没写）' }, 't'),
              jsx(Tag, { children: '可写 ' + num(plan.rows.filter(function (row) { return row.writable }).length) + ' / ' + num(plan.rows.length) }, 'w')
            ]
          }, 'head'),
          jsx('div', {
            className: 'hc-rows hc-rows--stack hc-models__pv-list',
            children: plan.rows.map(function (row) {
              return jsxs('div', {
                className: 'hc-row',
                'data-models-preview-row': row.agent,
                'data-writable': row.writable ? '1' : '0',
                'data-group': String(row.group),
                'data-slot': String(row.slot || ''),
                style: { display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: 'var(--s1)', minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' },
                children: [
                  jsxs('div', {
                    style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0 },
                    children: [
                      jsx('span', { style: { fontWeight: 600, fontSize: 'var(--fs-md)' }, children: (row.label || row.agent) + '（' + row.agent + '）' }, 'n'),
                      row.slot
                        ? jsx('span', { className: 'hc-chip hc-chip--off', style: { fontFamily: MONO }, title: '本次只改这个槽位', children: '槽位 ' + row.slot }, 'slot')
                        : jsx('span', { className: 'hc-chip hc-chip--off', style: { fontFamily: MONO }, children: row.target || '' }, 'target'),
                      row.writable
                        ? jsx(Tag, { children: '将写入' }, 'ok')
                        : jsx(Tag, { off: true, children: '会拒绝' }, 'no')
                    ]
                  }, 'h'),
                  row.changes.length
                    ? jsxs('div', {
                        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
                        children: row.changes.map(function (change) {
                          return jsxs('div', {
                            'data-models-preview-change': String(change.key || ''),
                            'data-to': String(change.to || ''),
                            style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'baseline', minWidth: 0 },
                            children: [
                              jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)', fontFamily: MONO }, children: String(change.key || '') }, 'k'),
                              jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-xs)' }, children: orDash(change.from) }, 'from'),
                              jsx('span', { className: 'hc-muted', children: '→' }, 'a'),
                              jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-xs)', fontWeight: 600 }, children: orDash(change.to) }, 'to')
                            ]
                          }, String(change.key || '') + String(change.from || '') + String(change.to || ''))
                        })
                      }, 'changes')
                    : jsx('div', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '与当前值相同（写入后内容不变）' }, 'same'),
                  row.warnings.length
                    ? jsx('div', {
                        className: 'hc-muted',
                        style: { fontSize: 'var(--fs-xs)', minWidth: 0, overflowWrap: 'anywhere' },
                        children: row.warnings.join('；')
                      }, 'warn')
                    : null,
                  row.error
                    ? jsx('div', { style: { fontSize: 'var(--fs-xs)', color: 'var(--ui-danger, inherit)' }, children: row.error }, 'err')
                    : null
                ]
              }, row.agent + '#' + String(row.group))
            })
          }, 'rows'),
          jsxs('div', {
            style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', marginTop: 'var(--s2)' },
            children: [
              asking
                ? jsxs('div', {
                    className: 'hc-up__confirm',
                    'data-models-editor-confirm': '1',
                    role: 'group',
                    'aria-label': '确认写入 ' + plan.rows.length + ' 行',
                    style: { flexWrap: 'wrap', minWidth: 0, maxWidth: '100%' },
                    children: [
                      jsx('span', {
                        className: 'hc-up__confirm-q',
                        children: '确认写入上面这张表？每个 agent 写前各自动快照、写后各自回读校验，' +
                          '不符就按快照字节还原（不会半截写入）。'
                      }, 'q'),
                      jsx('button', {
                        type: 'button',
                        className: 'hc-btn hc-btn--accent',
                        'data-models-editor-apply-yes': true,
                        disabled: busy,
                        onClick: performApply,
                        children: busy ? '正在写入…' : '确认写入'
                      }, 'yes'),
                      jsx('button', {
                        type: 'button',
                        className: 'hc-btn',
                        disabled: busy,
                        onClick: function () {
                          askState[1](false)
                        },
                        children: '取消'
                      }, 'no')
                    ]
                  }, 'ask')
                : jsx('button', {
                    type: 'button',
                    className: 'hc-btn hc-btn--accent',
                    'data-models-editor-apply-ask': true,
                    disabled: busy || !plan.rows.filter(function (row) { return row.writable }).length,
                    onClick: function () {
                      askState[1](true)
                    },
                    children: '下一步：确认写入'
                  }, 'askbtn'),
              jsx('button', {
                type: 'button',
                className: 'hc-btn',
                'data-models-preview-cancel': true,
                disabled: busy,
                onClick: function () {
                  planState[1](null)
                  askState[1](false)
                },
                children: '取消预览'
              }, 'cancel')
            ]
          }, 'acts')
        ]
      }, 'preview')
    : null


  // ---- 回执 ---------------------------------------------------------------
  var resultBlock = result
    ? jsxs('div', {
        className: 'hc-block',
        'data-models-result': result.rows.map(function (row) {
          return row.batch_id || ''
        }).filter(function (id, index, list) {
          return id && list.indexOf(id) === index
        }).join(','),
        style: { minWidth: 0, marginTop: 'var(--s3)' },
        children: [
          jsx('div', { className: 'hc-block__head', children: jsx('div', { className: 'hc-block__title', children: '写入结果（逐 agent 回读）' }, 't') }, 'head'),
          jsx('div', {
            // 「每一行是一整块内容」（标题行 + 说明行），不是表格的一行 ⇒ 走堆叠容器，
            // 不能吃 `.hc-rows .hc-row` 的 4 列网格（F-04）。
            className: 'hc-rows hc-rows--stack',
            children: result.rows.map(function (row) {
              var okCount = row.verify.filter(function (item) {
                return item && item.ok === true
              }).length
              return jsxs('div', {
                className: 'hc-row',
                'data-models-result-row': row.agent,
                'data-ok': row.ok ? '1' : '0',
                'data-changed': row.changed ? '1' : '0',
                style: { display: 'flex', flexDirection: 'column', alignItems: 'stretch', gap: 'var(--s1)', minWidth: 0, overflowWrap: 'anywhere' },
                children: [
                  jsxs('div', {
                    style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center' },
                    children: [
                      jsx('span', { style: { fontWeight: 600 }, children: (row.label || row.agent) + '（' + row.agent + '）' }, 'n'),
                      row.ok ? jsx(Tag, { children: '已写入' }, 'ok') : jsx(Tag, { off: true, children: '失败' }, 'no'),
                      jsx('span', {
                        className: 'hc-muted',
                        style: { fontFamily: MONO, fontSize: 'var(--fs-xs)', minWidth: 0, overflowWrap: 'anywhere' },
                        children: row.target || ''
                      }, 'v')
                    ]
                  }, 'h'),
                  jsx('div', {
                    className: 'hc-muted',
                    style: { fontSize: 'var(--fs-xs)' },
                    children: row.ok
                      ? (row.changed === false
                          ? '与磁盘上的值一致：这次没有写任何字节（也没做快照）'
                          : '回读 ' + okCount + '/' + row.verify.length + ' 项通过；快照 ' + orDash(row.snapshot_ts) +
                            (row.batch_id ? '；账本 ' + row.batch_id : ''))
                      : '没有写成：' + row.error
                  }, 'd')
                ]
              }, row.agent + '#' + (row.batch_id || '') + '#' + String(row.target || ''))
            })
          }, 'rows')
        ]
      }, 'result')
    : null

  // ---- 五段 scope 的按钮 ---------------------------------------------------
  var scopeDefs = [
    { id: 'model', label: '主模型', hint: '改 agent 的 model.provider / model.default' },
    { id: 'reasoning', label: '思考强度', hint: '只写 agent.reasoning_effort 一个键（scope=reasoning），默认保持不变' },
    { id: 'fallback', label: '备用首条', hint: '只改 fallback_providers 链的第一条（旧语义，保留）' },
    { id: 'fallbacks', label: '备用链（多条）', hint: '备用链整链：任意条数，可增删、调序、清空' },
    { id: 'auxiliary', label: '辅助槽位', hint: '按该 agent config.yaml 里真实存在的槽位逐个编辑' }
  ]
  var scopeTabs = jsxs('div', {
    style: fieldRowStyle,
    children: [
      jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '写入段' }, 'k'),
      jsxs('div', {
        style: { display: 'flex', gap: 'var(--s1)', flexWrap: 'wrap', minWidth: 0 },
        children: scopeDefs.map(function (item) {
          var on = scope === item.id
          return jsx('button', {
            type: 'button',
            className: on ? 'hc-btn hc-btn--tiny hc-btn--accent' : 'hc-btn hc-btn--tiny',
            'data-models-editor-scope': item.id,
            'aria-pressed': on ? 'true' : 'false',
            title: item.hint,
            disabled: busy,
            onClick: function () {
              pickScope(item.id)
            },
            children: item.label
          }, item.id)
        })
      }, 'v')
    ]
  }, 'scope')

  var providerRow = jsxs('div', {
    style: fieldRowStyle,
    children: [
      jsxs('span', {
        style: { display: 'inline-flex', gap: 'var(--s1)', alignItems: 'center', minWidth: 0 },
        children: [
          jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: 'provider' }, 'k'),
          modeChip('provider')
        ]
      }, 'k'),
      providerSelect
    ]
  }, 'provider')

  var modelRow = jsxs('div', {
    style: fieldRowStyle,
    children: [
      jsxs('span', {
        style: { display: 'inline-flex', gap: 'var(--s1)', alignItems: 'center', minWidth: 0 },
        children: [
          jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '模型' }, 'k'),
          modeChip('model')
        ]
      }, 'k'),
      jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
        children: [modelInput, modelKeepToggle, candidateChips]
      }, 'v')
    ]
  }, 'model')

  var scopeBody
  if (scope === 'reasoning') {
    scopeBody = jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
      children: [
        jsx('div', {
          className: 'hc-muted',
          style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
          children: reasoningWritable
            ? '写入段 = scope=reasoning：只改 agent.reasoning_effort（' +
              (mode === 'batch' ? '批量对每个选中 agent 各发一个请求，现值已相同就跳过）。' : '一个键）。')
            : '写入段 = scope=reasoning：后端还没声明这个 scope，本页只能只读展示。'
        }, 'hint'),
        reasoningRow
      ]
    }, 'reasoning')
  } else if (scope === 'fallbacks') {
    scopeBody = jsx(ChainEditor, {
      state: chain,
      options: providerOptions,
      busy: busy,
      narrow: narrow,
      onChange: setChain
    }, 'chain')
  } else if (scope === 'auxiliary') {
    scopeBody = jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
      children: [
        jsx('div', {
          className: 'hc-muted',
          'data-models-aux-count': String(auxViews.length),
          style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
          children: auxViews.length
            ? '这些槽位来自该 agent 的 config.yaml（' + num(auxViews.length) + ' 个）：' +
              auxViews.map(function (view) { return view.slot }).join(' / ') +
              '。只写你改过的槽位；key_env 只读。'
            : '选中的 agent 没有辅助槽位可编辑。'
        }, 'hint'),
        jsx(AuxEditor, {
          views: auxViews,
          state: aux,
          options: providerOptions,
          agent: refAgent,
          busy: busy,
          narrow: narrow,
          onField: setAuxField,
          onPair: setAuxPair,
          onReset: resetAuxSlot
        }, 'aux')
      ]
    }, 'auxbody')
  } else {
    scopeBody = jsxs('div', {
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
      children: [providerRow, modelRow, candidatesNoteBlock]
    }, 'fields')
  }

  return jsxs('div', {
    className: 'hc-block hc-models__editor',
    'data-models-editor': mode,
    'data-models-mode': mode,
    'data-models-scope': scope,
    style: { minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        className: 'hc-block__head',
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
        children: [
          jsx('div', {
            className: 'hc-block__title',
            children: mode === 'single' ? '单个 agent 编辑' : '批量编辑（' + ids.length + ' 个 agent）'
          }, 't'),
          jsx('span', {
            'data-models-editor-target': ids.join(','),
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-sm)', fontFamily: MONO, minWidth: 0, overflowWrap: 'anywhere' },
            children: mode === 'single'
              ? '目标 agent：' + singleLabel + '（' + ids[0] + '）'
              : '已选 ' + ids.length + ' 个 agent：' + ids.join('、')
          }, 'target'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-editor-close': true,
            disabled: busy,
            onClick: props.onClose,
            children: '收起编辑器'
          }, 'close')
        ]
      }, 'head'),

      // 单编辑态把该 agent 的当前值原样摆出来（base_url / api_mode / 凭据变量名），
      // 让人在改之前看得见「现在是什么」；批量态没有单一当前值，所以只在单个时出现。
      mode === 'single'
        ? jsx(CurrentModel, { row: singleRow, narrow: narrow }, 'current')
        : null,

      jsxs('div', {
        className: 'hc-models__fields',
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, marginTop: 'var(--s2)' },
        children: [scopeTabs, scopeBody, summary]
      }, 'fields'),

      jsxs('div', {
        style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center', marginTop: 'var(--s3)' },
        children: [
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--accent',
            'data-models-editor-preview-ask': true,
            disabled: busy || Boolean(livePlan.error),
            onClick: askPreview,
            children: busy ? '正在预览…' : '预览将写入什么'
          }, 'preview'),
          error
            ? jsx('span', {
                className: 'hc-muted',
                'data-models-editor-error': true,
                style: { fontSize: 'var(--fs-xs)', color: 'var(--ui-danger, inherit)', minWidth: 0, overflowWrap: 'anywhere' },
                children: error
              }, 'err')
            : null
        ]
      }, 'acts'),

      previewBlock,
      resultBlock
    ]
  })
}

/**
 * MoA 配方（``moa.presets``）面板：**真编辑器**。
 *
 * 后端契约（``dashboard/api_models.py`` 的 ``/models/moa/presets*``）：
 *   GET    /models/moa/presets?agent=X   每条预设的内容 + 引用明细 + 悬空红灯 + writable
 *   PUT    /models/moa/presets/{name}    改内容（reference_models 整条替换、顺序即 fan-out 顺序）
 *   POST   /models/moa/presets           新建；DELETE 删除（本组件暂不提供这两个入口）
 *
 * 三条纪律：
 *   1. **被引用的预设**（`moa.default_preset` / `model.default` / `fallback_providers[].model`）
 *      默认拒改：后端回 409 ``preset_referenced``，原文透出；要改必须显式勾「我知道它在被引用」
 *      （那时才带 ``allow_referenced_edit: true``）。**不许静默写入、不许静默跳过。**
 *   2. **悬空引用红灯**（写完指不到配方）由后端 409 ``dangling_reference`` 拦住，红字透出。
 *   3. 没改动 ⇒ 后端 ``changed: false``（不建快照、不落盘）；本组件不自己造请求。
 */
function MoaPresetPanel(props) {
  var info = props.info || null
  var moa = props.moa || null
  var busy = props.busy === true
  var narrow = props.narrow === true
  var providerOptions = Array.isArray(props.providerOptions) ? props.providerOptions : []
  var reasoningValues = Array.isArray(props.reasoningValues) ? props.reasoningValues : []

  var names = info && Array.isArray(info.models) ? info.models : []
  var source = info && info.source ? String(info.source) : ''
  var namesDefault = info && info.default_model ? String(info.default_model) : ''
  var disabledPresets = info && Array.isArray(info.disabled_models) ? info.disabled_models : []
  var rows = moa && Array.isArray(moa.presets) ? moa.presets : []
  var backendReady = Boolean(moa && moa.ok === true)
  var writable = Boolean(moa && moa.writable === true)
  var defaultPreset = String((moa && moa.default_preset) || namesDefault || '')
  var lights = moa && Array.isArray(moa.lights) ? moa.lights : []
  var dangling = Boolean(defaultPreset) && names.length > 0 && names.indexOf(defaultPreset) === -1
  var blockedReason = String((moa && moa.write_blocked_reason) || '')

  var pickState = useState('')
  var picked = pickState[0]
  var draftState = useState(null)
  var draft = draftState[0]
  var msgState = useState(null)
  var msg = msgState[0]
  var errState = useState('')
  var err = errState[0]
  var allowRefState = useState(false)
  var allowRef = allowRefState[0]
  var workingState = useState(false)
  var working = workingState[0] || busy

  var pickedRow = null
  rows.forEach(function (item) {
    if (item && item.name === picked) pickedRow = item
  })

  var toDraft = function (found) {
    if (!found) return null
    var refs = (Array.isArray(found.reference_models) ? found.reference_models : []).map(function (item) {
      var one = item && typeof item === 'object' ? item : {}
      return {
        provider: String(one.provider || ''),
        model: String(one.model || ''),
        enabled: one.enabled === false ? false : true,
        reasoning_effort: String(one.reasoning_effort || '')
      }
    })
    var agg = found.aggregator && typeof found.aggregator === 'object' ? found.aggregator : null
    return {
      name: String(found.name || ''),
      enabled: found.enabled === false ? false : true,
      refs: refs,
      hasAgg: Boolean(agg),
      aggProvider: agg ? String(agg.provider || '') : '',
      aggModel: agg ? String(agg.model || '') : '',
      aggReasoning: agg ? String(agg.reasoning_effort || '') : '',
      fanout: found.fanout === undefined || found.fanout === null ? '' : String(found.fanout),
      degraded: found.degraded_reference_policy === undefined || found.degraded_reference_policy === null
        ? ''
        : String(found.degraded_reference_policy)
    }
  }

  var pick = function (name) {
    pickState[1](name)
    msgState[1](null)
    errState[1]('')
    allowRefState[1](false)
    var found = null
    rows.forEach(function (item) {
      if (item && item.name === name) found = item
    })
    draftState[1](toDraft(found))
  }

  var patchDraft = function (next) {
    var base = draft || { refs: [] }
    draftState[1]({
      name: base.name,
      enabled: next.enabled === undefined ? base.enabled : next.enabled,
      refs: next.refs === undefined ? base.refs : next.refs,
      hasAgg: base.hasAgg,
      aggProvider: next.aggProvider === undefined ? base.aggProvider : next.aggProvider,
      aggModel: next.aggModel === undefined ? base.aggModel : next.aggModel,
      aggReasoning: next.aggReasoning === undefined ? base.aggReasoning : next.aggReasoning,
      fanout: base.fanout,
      degraded: base.degraded
    })
    msgState[1](null)
    errState[1]('')
  }

  var setRef = function (index, field, value) {
    var refs = (draft.refs || []).map(function (item, at) {
      if (at !== index) return item
      var next = {
        provider: item.provider,
        model: item.model,
        enabled: item.enabled,
        reasoning_effort: item.reasoning_effort
      }
      next[field] = value
      return next
    })
    patchDraft({ refs: refs })
  }

  var addRef = function () {
    var refs = (draft.refs || []).slice()
    refs.push({ provider: providerOptions[0] || '', model: '', enabled: true, reasoning_effort: '' })
    patchDraft({ refs: refs })
  }

  var removeRef = function (index) {
    var refs = (draft.refs || []).filter(function (item, at) {
      return at !== index
    })
    patchDraft({ refs: refs })
  }

  var moveRef = function (index, delta) {
    var refs = (draft.refs || []).slice()
    var to = index + delta
    if (to < 0 || to >= refs.length) return
    var tmp = refs[index]
    refs[index] = refs[to]
    refs[to] = tmp
    patchDraft({ refs: refs })
  }

  var save = function () {
    if (!draft || !draft.name) return
    workingState[1](true)
    errState[1]('')
    msgState[1](null)
    var body = {
      agent: props.agent,
      confirm: true,
      enabled: draft.enabled === true,
      reference_models: (draft.refs || []).map(function (item) {
        var one = { provider: item.provider, model: item.model, enabled: item.enabled === true }
        if (item.reasoning_effort) one.reasoning_effort = item.reasoning_effort
        return one
      })
    }
    if (draft.hasAgg) {
      body.aggregator = { provider: draft.aggProvider, model: draft.aggModel }
      if (draft.aggReasoning) body.aggregator.reasoning_effort = draft.aggReasoning
    }
    if (allowRef) body.allow_referenced_edit = true
    send('/models/moa/presets/' + encodeURIComponent(draft.name), body, { method: 'PUT' }).then(function (res) {
      workingState[1](false)
      if (res && res.ok) {
        var data = res.data || {}
        var verify = Array.isArray(data.verify) ? data.verify : []
        var bad = verify.filter(function (item) { return item && item.ok === false })
        msgState[1]({
          ok: bad.length === 0,
          title: data.changed === false ? '没有任何字段需要改（后端没写盘）' : '配方已写入并回读逐键校验',
          detail: data.changed === false
            ? '本次请求与磁盘内容一致：没建快照、没写 config.yaml。'
            : '校验 ' + num(verify.length) + ' 项，失败 ' + num(bad.length) + ' 项；快照 ' + String(data.snapshot_ts || '—'),
          verify: verify
        })
        // 只刷新列表：**不要**再调 pick() —— 那会把刚写好的 msg 清成 null，
        // 界面上就看不到「回读逐键校验通过」这句话了（draft 本来就是写进去的值）。
        refreshAll(CACHE_PREFIX)
      } else {
        errState[1]((res && res.code ? '[' + res.code + '] ' : '') + String((res && res.message) || '未知原因'))
      }
    })
  }

  var cellStyle = {
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--s1)',
    minWidth: 0,
    maxWidth: '100%',
    padding: 'var(--s2) 0',
    borderBottom: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)'
  }
  var refRowStyle = {
    display: 'grid',
    gridTemplateColumns: narrow ? 'minmax(0,1fr)' : 'minmax(0,1.1fr) minmax(0,1.4fr) minmax(0,0.7fr) minmax(0,0.9fr) auto',
    gap: 'var(--s1)',
    alignItems: 'center',
    minWidth: 0
  }

  var refList = draft
    ? jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
        children: [
          jsx('div', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
            children: 'reference_models（有序，顺序即 fan-out 顺序；每条可单独设思考强度）'
          }, 'h'),
          draft.refs.length
            ? jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
                children: draft.refs.map(function (item, index) {
                  return jsxs('div', {
                    'data-models-moa-ref': String(index),
                    style: refRowStyle,
                    children: [
                      jsx('select', {
                        className: 'hc-select',
                        'data-models-moa-ref-provider': String(index),
                        value: item.provider,
                        disabled: working || !writable,
                        onChange: function (event) { setRef(index, 'provider', event.target.value) },
                        children: providerOptions.map(function (name) {
                          return jsx('option', { value: name, children: name }, name)
                        })
                      }, 'p'),
                      jsx('input', {
                        className: 'hc-input',
                        'data-models-moa-ref-model': String(index),
                        value: item.model,
                        disabled: working || !writable,
                        placeholder: '模型名',
                        onChange: function (event) { setRef(index, 'model', event.target.value) }
                      }, 'm'),
                      jsx('select', {
                        className: 'hc-select',
                        'data-models-moa-ref-reasoning': String(index),
                        value: item.reasoning_effort,
                        disabled: working || !writable,
                        title: '该参考位的思考强度（可选；不选 = 不写这个键）',
                        onChange: function (event) { setRef(index, 'reasoning_effort', event.target.value) },
                        children: [jsx('option', { value: '', children: '思考强度：不设置' }, 'k')].concat(
                          reasoningValues.map(function (name) {
                            return jsx('option', {
                              value: name,
                              children: name === 'none' ? 'none（关闭思考）' : name
                            }, name)
                          })
                        )
                      }, 'r'),
                      jsx('label', {
                        className: 'hc-muted',
                        style: { display: 'inline-flex', gap: 'var(--s1)', alignItems: 'center', fontSize: 'var(--fs-xs)' },
                        children: [
                          jsx('input', {
                            type: 'checkbox',
                            'data-models-moa-ref-enabled': String(index),
                            checked: item.enabled !== false,
                            disabled: working || !writable,
                            onChange: function (event) { setRef(index, 'enabled', event.target.checked === true) }
                          }, 'c'),
                          jsx('span', { children: '启用' }, 't')
                        ]
                      }, 'e'),
                      jsxs('span', {
                        style: { display: 'inline-flex', gap: 'var(--s1)', flexWrap: 'wrap' },
                        children: [
                          jsx('button', {
                            type: 'button',
                            className: 'hc-btn hc-btn--tiny',
                            'data-models-moa-ref-up': String(index),
                            disabled: working || !writable || index === 0,
                            onClick: function () { moveRef(index, -1) },
                            children: '↑'
                          }, 'u'),
                          jsx('button', {
                            type: 'button',
                            className: 'hc-btn hc-btn--tiny',
                            'data-models-moa-ref-down': String(index),
                            disabled: working || !writable || index === draft.refs.length - 1,
                            onClick: function () { moveRef(index, 1) },
                            children: '↓'
                          }, 'd'),
                          jsx('button', {
                            type: 'button',
                            className: 'hc-btn hc-btn--tiny',
                            'data-models-moa-ref-del': String(index),
                            disabled: working || !writable,
                            onClick: function () { removeRef(index) },
                            children: '删除'
                          }, 'x')
                        ]
                      }, 'ops')
                    ]
                  }, String(index))
                })
              }, 'list')
            : jsx('div', {
                className: 'hc-muted',
                'data-models-moa-ref-empty': true,
                style: { fontSize: 'var(--fs-xs)' },
                children: '这个预设没有 reference_models（空列表会被写回空数组）。'
              }, 'empty'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-models-moa-ref-add': true,
            disabled: working || !writable,
            onClick: addRef,
            children: '加一条参考模型'
          }, 'add')
        ]
      }, 'refs')
    : null

  var aggBlock = draft && draft.hasAgg
    ? jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
        children: [
          jsx('div', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)' },
            children: 'aggregator（聚合器：汇总各参考位的答复）'
          }, 'h'),
          jsxs('div', {
            style: refRowStyle,
            children: [
              jsx('select', {
                className: 'hc-select',
                'data-models-moa-agg-provider': true,
                value: draft.aggProvider,
                disabled: working || !writable,
                onChange: function (event) { patchDraft({ aggProvider: event.target.value }) },
                children: providerOptions.map(function (name) {
                  return jsx('option', { value: name, children: name }, name)
                })
              }, 'p'),
              jsx('input', {
                className: 'hc-input',
                'data-models-moa-agg-model': true,
                value: draft.aggModel,
                disabled: working || !writable,
                onChange: function (event) { patchDraft({ aggModel: event.target.value }) }
              }, 'm'),
              jsx('select', {
                className: 'hc-select',
                'data-models-moa-agg-reasoning': true,
                value: draft.aggReasoning,
                disabled: working || !writable,
                onChange: function (event) { patchDraft({ aggReasoning: event.target.value }) },
                children: [jsx('option', { value: '', children: '思考强度：不设置' }, 'k')].concat(
                  reasoningValues.map(function (name) {
                    return jsx('option', {
                      value: name,
                      children: name === 'none' ? 'none（关闭思考）' : name
                    }, name)
                  })
                )
              }, 'r')
            ]
          }, 'row')
        ]
      }, 'agg')
    : draft
      ? jsx('div', {
          className: 'hc-muted',
          'data-models-moa-agg-absent': true,
          style: { fontSize: 'var(--fs-xs)' },
          children: '这个预设没有 aggregator 键：原样保留，不新增（要加请在 MoA 自己的入口里建）。'
        }, 'agg')
      : null

  return jsxs('div', {
    className: 'hc-models__moa',
    'data-models-moa-presets': String(names.length),
    'data-models-moa-source': source,
    'data-models-moa-default': defaultPreset,
    'data-models-moa-dangling': dangling ? '1' : '0',
    'data-models-moa-backend': backendReady ? 'ready' : 'pending',
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', minWidth: 0 },
        children: [
          jsx('div', { className: 'hc-block__title', children: 'MoA 配方（moa.presets）' }, 't'),
          jsx('span', {
            className: 'hc-chip hc-chip--off',
            'data-models-moa-badge': source,
            style: { fontFamily: MONO },
            children: source || 'unknown'
          }, 'src'),
          jsx(Tag, { children: num(names.length) + ' 个预设' }, 'n'),
          defaultPreset ? jsx(Tag, { off: true, title: 'moa.default_preset', children: '默认 ' + defaultPreset }, 'd') : null,
          backendReady
            ? jsx(Tag, { children: '后端已就绪' }, 'ok')
            : jsx(Tag, {
                off: true,
                title: 'GET /models/moa/presets 不可用：编辑控件禁用，一个请求都不会发',
                children: '后端待支持'
              }, 'pending')
        ]
      }, 'head'),
      info && info.note
        ? jsx('div', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', minWidth: 0, overflowWrap: 'anywhere' },
            children: String(info.note)
          }, 'note')
        : null,
      dangling
        ? jsx('div', {
            'data-models-moa-dangling-warn': true,
            style: {
              fontSize: 'var(--fs-xs)',
              color: 'var(--ui-danger, inherit)',
              border: 'var(--hairline) solid var(--ui-danger, currentColor)',
              borderRadius: 'var(--r-sm)',
              padding: 'var(--s1) var(--s2)',
              minWidth: 0,
              overflowWrap: 'anywhere'
            },
            children: '悬空引用：moa.default_preset = 「' + defaultPreset + '」不在该 agent 的预设里' +
              '（现有：' + names.join('、') + '）—— 宿主会回退到内置默认配方；这种配置下任何预设写入都会被后端 409 拒绝。'
          }, 'dangle')
        : null,
      blockedReason
        ? jsx('div', {
            'data-models-moa-blocked': true,
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
            children: blockedReason
          }, 'blocked')
        : null,
      names.length
        ? jsx('div', {
            className: 'hc-rows hc-models__moa-list',
            children: names.map(function (name) {
              var one = null
              rows.forEach(function (item) {
                if (item && item.name === name) one = item
              })
              var isDefault = name === defaultPreset
              var isReferenced = Boolean(one && one.referenced)
              var isDisabled = disabledPresets.indexOf(name) >= 0 || Boolean(one && one.enabled === false)
              return jsxs('div', {
                className: 'hc-row',
                'data-models-moa-preset': name,
                'data-models-moa-preset-referenced': isReferenced ? '1' : '0',
                'data-models-moa-preset-disabled': isDisabled ? '1' : '0',
                style: {
                  display: 'flex',
                  flexWrap: 'wrap',
                  gap: 'var(--s2)',
                  alignItems: 'center',
                  minWidth: 0,
                  maxWidth: '100%',
                  overflowWrap: 'anywhere'
                },
                children: [
                  jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-md)', fontWeight: 600 }, children: name }, 'n'),
                  isDefault ? jsx(Tag, { tone: 'hc-chip--on', children: 'default_preset' }, 'def') : null,
                  isDisabled ? jsx(Tag, { off: true, title: 'preset.enabled === false', children: '已禁用' }, 'off') : null,
                  isReferenced
                    ? jsx(Tag, {
                        off: true,
                        title: '被 moa.default_preset / model.default / fallback_providers[].model 引用：改会被 409 拒绝',
                        children: '被引用 · 默认拒改'
                      }, 'ref')
                    : null,
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn hc-btn--tiny',
                    'data-models-moa-edit': name,
                    disabled: working || !writable,
                    onClick: function () { pick(name) },
                    children: picked === name ? '正在编辑' : '编辑内容'
                  }, 'edit')
                ]
              }, name)
            })
          }, 'list')
        : jsx('div', {
            className: 'hc-muted',
            'data-models-moa-empty': true,
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
            children: source === 'moa-presets-missing'
              ? '这个 agent 的 config.yaml 里没有 moa: 段 —— 切到 moa 后用的是宿主内置默认配方。'
              : '有 moa: 段但没有任何预设：宿主会回退到内置默认配方。'
          }, 'empty'),
      // ---- 配方内容编辑器 -----------------------------------------------------
      jsxs('div', {
        'data-models-moa-editor': backendReady ? 'enabled' : 'disabled',
        style: {
          display: 'flex',
          flexDirection: 'column',
          gap: 'var(--s2)',
          minWidth: 0,
          padding: 'var(--s2)',
          border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
          borderRadius: 'var(--r-sm)'
        },
        children: [
          jsx('div', {
            className: 'hc-block__title',
            style: { fontSize: 'var(--fs-md)' },
            children: '配方内容编辑（reference_models / aggregator / enabled）'
          }, 't'),
          jsx('div', {
            className: 'hc-muted',
            'data-models-moa-editor-note': true,
            style: { fontSize: 'var(--fs-xs)', minWidth: 0, overflowWrap: 'anywhere' },
            children: backendReady
              ? '写的是该 agent 的 config.yaml 的 moa.presets.<名>（PUT /models/moa/presets/{name}，写完回读逐键校验）。'
              : '后端待支持：GET /models/moa/presets 没读到（旧进程或端点缺失）。这里的控件全部禁用 —— 点了不会发请求，也不会假装成功。'
          }, 'note'),
          jsx('select', {
            className: 'hc-select',
            'data-models-moa-editor-field': 'preset',
            disabled: working || !backendReady || !names.length,
            value: picked,
            onChange: function (event) { pick(event.target.value) },
            children: [jsx('option', { value: '', children: names.length ? '选一个预设…' : '没有预设可选' }, 'p0')].concat(
              names.map(function (name) {
                return jsx('option', { value: name, children: name }, name)
              })
            )
          }, 'preset'),
          draft ? refList : null,
          draft ? aggBlock : null,
          draft
            ? jsx('label', {
                className: 'hc-muted',
                style: { display: 'inline-flex', gap: 'var(--s1)', alignItems: 'center', fontSize: 'var(--fs-xs)' },
                children: [
                  jsx('input', {
                    type: 'checkbox',
                    'data-models-moa-editor-enabled': true,
                    checked: draft.enabled === true,
                    disabled: working || !writable,
                    onChange: function (event) { patchDraft({ enabled: event.target.checked === true }) }
                  }, 'c'),
                  jsx('span', { children: 'preset.enabled（关掉后该配方不参与）' }, 't')
                ]
              }, 'enabled')
            : null,
          pickedRow && pickedRow.referenced
            ? jsxs('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
                children: [
                  jsx('div', {
                    'data-models-moa-ref-warn': true,
                    style: {
                      fontSize: 'var(--fs-xs)',
                      color: 'var(--ui-danger, inherit)',
                      border: 'var(--hairline) solid var(--ui-danger, currentColor)',
                      borderRadius: 'var(--r-sm)',
                      padding: 'var(--s1) var(--s2)',
                      overflowWrap: 'anywhere'
                    },
                    children: '这个预设正被引用（' +
                      (Array.isArray(pickedRow.references) ? pickedRow.references.length : 1) +
                      ' 处）：后端会 409 拒绝修改，除非显式确认。'
                  }, 'w'),
                  jsx('label', {
                    className: 'hc-muted',
                    style: { display: 'inline-flex', gap: 'var(--s1)', alignItems: 'center', fontSize: 'var(--fs-xs)' },
                    children: [
                      jsx('input', {
                        type: 'checkbox',
                        'data-models-moa-allow-referenced': true,
                        checked: allowRef,
                        disabled: working,
                        onChange: function (event) { allowRefState[1](event.target.checked === true) }
                      }, 'c'),
                      jsx('span', { children: '我知道它在被引用，仍要改（allow_referenced_edit）' }, 't')
                    ]
                  }, 'a')
                ]
              }, 'refwarn')
            : null,
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny hc-btn--accent',
            'data-models-moa-editor-save': backendReady ? 'enabled' : 'disabled',
            disabled: working || !backendReady || !writable || !draft,
            title: writable ? '写入并回读逐键校验' : '配置里有悬空引用：后端会 409 拒绝',
            onClick: save,
            children: working ? '写入中…' : '保存配方'
          }, 'save'),
          err
            ? jsx('div', {
                'data-models-moa-error': true,
                style: { fontSize: 'var(--fs-xs)', color: 'var(--ui-danger, inherit)', overflowWrap: 'anywhere' },
                children: err
              }, 'err')
            : null,
          msg
            ? jsxs('div', {
                'data-models-moa-result': msg.ok ? 'ok' : 'fail',
                style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', fontSize: 'var(--fs-xs)', minWidth: 0 },
                children: [
                  jsx('span', { style: { fontWeight: 600 }, children: msg.title }, 't'),
                  jsx('span', { className: 'hc-muted', children: msg.detail }, 'd'),
                  (msg.verify || []).length
                    ? jsx('span', {
                        className: 'hc-muted',
                        style: { fontFamily: MONO, overflowWrap: 'anywhere' },
                        children: msg.verify.map(function (item) {
                          return (item.ok ? '✓ ' : '✗ ') + String(item.key || '')
                        }).join('  ')
                      }, 'v')
                    : null
                ]
              }, 'msg')
            : null
        ]
      }, 'editor')
    ]
  })
}

function SnapshotRow(props) {
  var row = props.row || {}
  var narrow = props.narrow
  var sizes = row.sizes && typeof row.sizes === 'object' ? row.sizes : {}
  var agents = Array.isArray(row.agents) ? row.agents : Array.isArray(row.profiles) ? row.profiles : []
  var size = 0
  Object.keys(sizes).forEach(function (key) {
    if (typeof sizes[key] === 'number') size += sizes[key]
  })
  var usable = agents.indexOf(props.agent) >= 0
  return jsxs('div', {
    className: 'hc-row',
    'data-models-snapshot': String(row.ts || ''),
    style: {
      display: 'flex',
      flexDirection: narrow ? 'column' : 'row',
      flexWrap: 'wrap',
      gap: 'var(--s2)',
      alignItems: narrow ? 'stretch' : 'center',
      minWidth: 0,
      maxWidth: '100%',
      overflowWrap: 'anywhere'
    },
    children: [
      jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-md)', flex: '1 1 150px', minWidth: 0 }, children: String(row.ts || '') }, 'ts'),
      jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)', flex: '1 1 120px', minWidth: 0 }, children: (row.operation || '—') + ' · ' + orDash(row.created_at) }, 'op'),
      jsx('span', { style: { fontSize: 'var(--fs-xs)', flex: '1 1 120px', minWidth: 0 }, children: agents.join(', ') || '—' }, 'ag'),
      jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)', flex: '0 1 auto' }, children: size ? num(size) + ' B' : '—' }, 'sz'),
      jsx('button', {
        type: 'button',
        className: 'hc-btn hc-btn--tiny',
        'data-models-rollback': String(row.ts || ''),
        disabled: !usable || props.busy === true,
        title: usable ? '把该 agent 还原到这一份快照' : '这一份快照里没有 ' + props.agent,
        onClick: function () {
          props.onAsk(row)
        },
        children: '回滚到这份'
      }, 'btn')
    ]
  })
}

/** 预设一行：名字 / 目标 / 应用 / 删除。 */
function PresetRow(props) {
  var row = props.row || {}
  var narrow = props.narrow
  return jsxs('div', {
    className: 'hc-row',
    'data-models-preset': String(row.name || ''),
    style: {
      display: 'flex',
      flexDirection: narrow ? 'column' : 'row',
      flexWrap: 'wrap',
      gap: 'var(--s2)',
      alignItems: narrow ? 'stretch' : 'center',
      minWidth: 0,
      maxWidth: '100%',
      overflowWrap: 'anywhere'
    },
    children: [
      jsxs('span', { style: { display: 'flex', gap: 'var(--s2)', alignItems: 'center', flex: '1 1 150px', minWidth: 0 }, children: [
        jsx('span', { style: { fontWeight: 600, fontSize: 'var(--fs-md)' }, children: String(row.name || '') }, 'n'),
        row.is_current ? jsx(Tag, { tone: 'hc-chip--on', children: '已生效' }, 'cur') : null
      ] }, 'name'),
      jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-xs)', flex: '2 1 200px', minWidth: 0 }, children: orDash(row.provider) + ' / ' + orDash(row.model) }, 'v'),
      jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)', flex: '1 1 120px', minWidth: 0 }, children: row.note || row.label || '—' }, 'note'),
      jsxs('span', { style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap' }, children: [
        jsx('button', {
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-models-preset-use': String(row.name || ''),
          disabled: props.busy === true,
          title: '把这组 provider / 模型填进上面的切换表单（不会直接写配置）',
          onClick: function () {
            props.onUse(row)
          },
          children: '填进表单'
        }, 'use'),
        jsx('button', {
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-models-preset-delete': String(row.name || ''),
          disabled: props.busy === true,
          onClick: function () {
            props.onDelete(row)
          },
          children: '删除'
        }, 'del')
      ] }, 'acts')
    ]
  })
}


/**
 * 新建预设的小表单：名字 + provider + 模型 + 备注。
 *
 * **它只写 `data/presets.json`**（后端 `POST /models/presets`），不碰任何 profile 的
 * config.yaml —— 所以这里不需要二次确认条，但提交前仍要三个字段都非空。
 */
function PresetForm(props) {
  var narrow = props.narrow
  var nameState = useState('')
  var providerState = useState('')
  var modelState = useState('')
  var noteState = useState('')
  var name = nameState[0]
  var provider = providerState[0]
  var model = modelState[0]
  var note = noteState[0]
  var ready = Boolean(name.trim() && provider && model.trim())

  var fieldStyle = {
    display: 'grid',
    gridTemplateColumns: narrow ? 'minmax(0, 1fr)' : 'minmax(0, 84px) minmax(0, 1fr)',
    gap: 'var(--s2)',
    alignItems: 'center',
    minWidth: 0,
    maxWidth: '100%'
  }

  return jsxs('div', {
    className: 'hc-models__preset-form',
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsx(BlockTitle, { hint: '只写 console 自己的 presets.json，不改任何 profile 配置' }, 'form-title'),
      jsxs('div', { style: fieldStyle, children: [
        jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '预设名' }, 'k'),
        jsx('input', {
          className: 'hc-select',
          'data-models-preset-name': true,
          type: 'text',
          value: name,
          placeholder: '字母开头，可含 . _ -（如 fast / cheap.v2）',
          onChange: function (event) {
            nameState[1](event.target.value)
          }
        }, 'v')
      ] }, 'name'),
      jsxs('div', { style: fieldStyle, children: [
        jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: 'provider' }, 'k'),
        jsx('select', {
          className: 'hc-select',
          'data-models-preset-provider': true,
          value: provider,
          onChange: function (event) {
            providerState[1](event.target.value)
          },
          children: [
            jsx('option', { value: '', children: '（选择 provider）' }, 'empty'),
            (props.providers || []).map(function (row) {
              var id = String(row.provider || '')
              return jsx('option', { value: id, children: id }, id)
            })
          ]
        }, 'v')
      ] }, 'provider'),
      jsxs('div', { style: fieldStyle, children: [
        jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '模型' }, 'k'),
        jsx('input', {
          className: 'hc-select',
          'data-models-preset-model': true,
          type: 'text',
          value: model,
          placeholder: '模型名（可手填）',
          style: { fontFamily: MONO },
          onChange: function (event) {
            modelState[1](event.target.value)
          }
        }, 'v')
      ] }, 'model'),
      jsxs('div', { style: fieldStyle, children: [
        jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: '备注' }, 'k'),
        jsx('input', {
          className: 'hc-select',
          'data-models-preset-note': true,
          type: 'text',
          value: note,
          placeholder: '可选：这条预设为什么存在',
          onChange: function (event) {
            noteState[1](event.target.value)
          }
        }, 'v')
      ] }, 'note'),
      jsx('div', {
        style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap' },
        children: jsx('button', {
          type: 'button',
          className: 'hc-btn' + (ready ? ' hc-btn--accent' : ''),
          'data-models-preset-create': true,
          disabled: !ready || props.busy === true,
          onClick: function () {
            props.onCreate({
              name: name.trim(),
              provider: provider,
              model: model.trim(),
              note: note.trim()
            })
            nameState[1]('')
            modelState[1]('')
            noteState[1]('')
          },
          children: props.busy === true ? '正在保存…' : '保存预设'
        }, 'save')
      }, 'btn')
    ]
  })
}

/**
 * 批量面板：**一次给一个 / 几个 / 全部 agent 设模型**（老板要的"方便直观快捷"）。
 *
 * 五步，一步都不许省（服务端每一步都有对应的第二道校验）：
 *   1. 勾 agent（列表里带每个 agent 的当前 provider/模型，选谁一目了然）；
 *   2. 选 provider + 模型（moa 也在列表里，它的"模型"= MoA 预设名）；
 *   3. **预览**：POST /models/batch/preview —— 纯读，逐 agent 列出「将写哪个字段、从什么变成什么」；
 *   4. **确认写入**：POST /models/batch/apply（带 confirm=true）—— 逐 agent 快照 + 写入 + 回读校验；
 *   5. **一键回滚**：POST /models/batch/rollback（带 batch_id）—— 逐 agent 按字节还原各自原值。
 *
 * 所有按钮在拿到服务端回包前都禁用；失败原话 + 错误码原样透出，不吞。
 */
// ---------------------------------------------------------------------------
// 历史与回滚：批量账本（`data/batches/*.json`）+ 逐 agent 快照
//
// 这部分是整改前的 BatchPanel 里**唯一不能丢**的能力（一键回滚整批 /
// 回滚到某一份快照），现在收进次级入口的一个 tab，逻辑原样保留：
// 两步确认、写完刷新、逐 agent 报结果。
// ---------------------------------------------------------------------------

function HistoryPanel(props) {
  var narrow = props.narrow === true
  var batches = Array.isArray(props.batches) ? props.batches : []
  var snapshots = Array.isArray(props.snapshots) ? props.snapshots : []
  var agent = String(props.agent || 'default')
  var busyState = useState(false)
  var busy = busyState[0] === true || props.busy === true
  var errorState = useState('')
  var error = errorState[0]
  var askBatchState = useState('')
  var askSnapshotState = useState(null)
  var resultState = useState(null)
  var result = resultState[0]

  var askBatch = function (batchId) {
    askSnapshotState[1](null)
    askBatchState[1](String(batchId || ''))
  }

  var runBatchRollback = function () {
    var batchId = askBatchState[0]
    if (!batchId) return
    busyState[1](true)
    askBatchState[1]('')
    errorState[1]('')
    send('/models/batch/rollback', { batch_id: batchId, confirm: true },
      { method: 'POST', timeoutMs: 120000 }).then(function (res) {
      busyState[1](false)
      if (res && res.ok) {
        var data = res.data && typeof res.data === 'object' ? res.data : {}
        resultState[1]({ kind: 'batch', rows: Array.isArray(data.results) ? data.results : [], batchId: batchId })
        refreshAll(CACHE_PREFIX)
      } else {
        errorState[1]('一键回滚失败：' + String((res && res.message) || '未知原因') +
          (res && res.code ? '（' + res.code + '）' : ''))
      }
    })
  }

  var runSnapshotRollback = function () {
    var snapshot = askSnapshotState[0]
    if (!snapshot) return
    busyState[1](true)
    askSnapshotState[1](null)
    errorState[1]('')
    send('/models/rollback', { agent: agent, snapshot: snapshot.dir || snapshot.ts, confirm: true },
      { method: 'POST', timeoutMs: 60000 }).then(function (res) {
      busyState[1](false)
      if (res && res.ok) {
        var data = res.data && typeof res.data === 'object' ? res.data : {}
        var restored = data.restored && typeof data.restored === 'object' ? data.restored : {}
        resultState[1]({
          kind: 'snapshot',
          batchId: '',
          rows: [{
            agent: agent,
            ok: true,
            restored_provider: restored.provider,
            restored_model: restored.model,
            snapshot_ts: snapshot.ts || ''
          }]
        })
        refreshAll(CACHE_PREFIX)
      } else {
        errorState[1]('回滚失败：' + String((res && res.message) || '未知原因') +
          (res && res.code ? '（' + res.code + '）' : ''))
      }
    })
  }

  var resultBlock = result
    ? jsxs('div', {
        className: 'hc-block',
        'data-models-batch-rollback-result': result.kind,
        style: { minWidth: 0, marginTop: 'var(--s3)' },
        children: [
          jsx('div', {
            className: 'hc-block__head',
            children: jsx('div', {
              className: 'hc-block__title',
              children: result.kind === 'batch' ? '整批回滚结果' : '快照回滚结果'
            }, 't')
          }, 'head'),
          jsx('div', {
            // 同上：回滚结果是「一个 agent 一整块」，用堆叠容器（F-04）。
            className: 'hc-rows hc-rows--stack',
            children: result.rows.map(function (row) {
              var ok = row.ok === true
              return jsxs('div', {
                className: 'hc-row',
                'data-models-batch-rollback-row': String(row.agent || ''),
                'data-ok': ok ? '1' : '0',
                style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0, overflowWrap: 'anywhere' },
                children: [
                  jsx('span', { style: { fontWeight: 600 }, children: String(row.agent || '') }, 'a'),
                  ok ? jsx(Tag, { children: '已还原' }, 'ok') : jsx(Tag, { off: true, children: '失败' }, 'no'),
                  jsx('span', {
                    className: 'hc-muted',
                    style: { fontSize: 'var(--fs-xs)', fontFamily: MONO },
                    children: ok
                      ? orDash(row.restored_provider) + ' / ' + orDash(row.restored_model) +
                        (row.snapshot_ts ? '（快照 ' + row.snapshot_ts + '）' : '')
                      : String(row.error || '未知原因')
                  }, 'd')
                ]
              }, String(row.agent || '') + ok)
            })
          }, 'rows')
        ]
      }, 'result')
    : null

  return jsxs('div', {
    'data-models-history': true,
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s4)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        className: 'hc-block',
        style: { minWidth: 0 },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
            children: [
              jsx('div', { className: 'hc-block__title', children: '批量账本' }, 't'),
              jsx(Tag, { children: num(batches.length) + ' 批' }, 'n'),
              jsx(Tag, { off: true, children: '一键回滚整批' }, 'r')
            ]
          }, 'head'),
          props.batchesError
            ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: '账本没读到：' + props.batchesError }, 'err')
            : batches.length
              ? jsx('div', {
                  // 批次账本的每一行同样是「一整块」（批次头 + 展开的确认/回滚区），走堆叠容器。
                  className: 'hc-rows hc-rows--stack',
                  children: batches.map(function (item) {
                    var id = String(item.batch_id || '')
                    var asking = askBatchState[0] === id
                    return jsxs('div', {
                      className: 'hc-row',
                      'data-models-batch-history': id,
                      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0, overflowWrap: 'anywhere' },
                      children: [
                        jsxs('div', {
                          style: { display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap', alignItems: 'center', minWidth: 0 },
                          children: [
                            jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-xs)' }, children: id }, 'id'),
                            jsx(Tag, { children: num(item.count || 0) + ' 个 agent' }, 'c'),
                            jsx(Tag, { off: true, children: 'ok ' + num(item.ok_count || 0) + ' / 失败 ' + num(item.failed_count || 0) }, 'k'),
                            jsx('span', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)' }, children: orDash(item.created_at) + ' · ' + orDash(item.scope) + ' → ' + orDash(item.provider) + ' / ' + orDash(item.model) }, 'meta'),
                            jsx('span', { style: { flex: '1 1 auto' } }, 'sp'),
                            asking
                              ? null
                              : jsx('button', {
                                  type: 'button',
                                  className: 'hc-btn hc-btn--tiny',
                                  'data-models-batch-history-rollback': id,
                                  disabled: busy,
                                  onClick: function () {
                                    askBatch(id)
                                  },
                                  children: '回滚这一批'
                                }, 'rb')
                          ]
                        }, 'head'),
                        asking
                          ? jsxs('div', {
                              className: 'hc-up__confirm',
                              style: { flexWrap: 'wrap', minWidth: 0 },
                              children: [
                                jsx('span', {
                                  className: 'hc-up__confirm-q',
                                  children: '确认把这一批 ' + num(item.count || 0) + ' 个 agent 全部还原到写前快照？'
                                }, 'q'),
                                jsx('button', {
                                  type: 'button',
                                  className: 'hc-btn hc-btn--accent',
                                  'data-models-batch-rollback-yes': id,
                                  disabled: busy,
                                  onClick: runBatchRollback,
                                  children: busy ? '正在还原…' : '确认回滚整批'
                                }, 'yes'),
                                jsx('button', {
                                  type: 'button',
                                  className: 'hc-btn',
                                  disabled: busy,
                                  onClick: function () {
                                    askBatchState[1]('')
                                  },
                                  children: '取消'
                                }, 'no')
                              ]
                            }, 'ask')
                          : null
                      ]
                    }, id)
                  })
                }, 'list')
              : jsx(Empty, {
                  title: '还没有批量写入的账本',
                  message: '每次批量写入都会在 console 的 data/batches/ 下记一份账本（逐 agent 快照 + 前后值）。',
                  hint: '先在「Agent 与当前模型」里选多个 agent 做一次批量编辑。'
                }, 'empty'),
          error
            ? jsx('div', { className: 'hc-muted', style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', marginTop: 'var(--s2)' }, children: error }, 'error')
            : null
        ]
      }, 'batches'),
      jsxs('div', {
        className: 'hc-block',
        style: { minWidth: 0 },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
            children: [
              jsx('div', { className: 'hc-block__title', children: '快照（' + agent + '）' }, 't'),
              jsx(Tag, { children: num(snapshots.length) + ' 份' }, 'n'),
              jsx(Tag, { off: true, children: '只允许回滚到已有快照' }, 'r')
            ]
          }, 'head'),
          askSnapshotState[0]
            ? jsxs('div', {
                className: 'hc-up__confirm',
                style: { flexWrap: 'wrap', minWidth: 0 },
                children: [
                  jsx('span', {
                    className: 'hc-up__confirm-q',
                    children: '确认把「' + agent + '」的 config.yaml 回滚到快照 ' + askSnapshotState[0].ts + '？这会覆盖它现在的配置。'
                  }, 'q'),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn hc-btn--accent',
                    'data-models-snapshot-rollback-yes': true,
                    disabled: busy,
                    onClick: runSnapshotRollback,
                    children: busy ? '正在还原…' : '确认回滚'
                  }, 'yes'),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn',
                    disabled: busy,
                    onClick: function () {
                      askSnapshotState[1](null)
                    },
                    children: '取消'
                  }, 'no')
                ]
              }, 'ask')
            : null,
          props.snapshotsError
            ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: '快照没读到：' + props.snapshotsError }, 'err')
            : snapshots.length
              ? jsx('div', {
                  className: 'hc-rows',
                  children: snapshots.map(function (item, index) {
                    return jsx('div', {
                      'data-models-snapshot': String(item.ts || ''),
                      children: jsx(SnapshotRow, {
                        row: item,
                        agent: agent,
                        narrow: narrow,
                        busy: busy,
                        onAsk: function (next) {
                          askSnapshotState[1](next)
                        }
                      }, String(item.ts || '') + '#' + index)
                    }, String(item.ts || '') + '#' + index)
                  })
                }, 'list')
              : jsx(Empty, {
                  title: '还没有快照',
                  message: '每次 apply / rollback 写前都会按字节复制一份 config.yaml。',
                  hint: '先做一次编辑，这里就会出现可回滚的版本。'
                }, 'empty')
        ]
      }, 'snapshots'),
      resultBlock
    ]
  })
}

/**
 * 模型分区（V6）：**Agent 列表即页面主体**，一个编辑器管单个与批量。
 *
 * 与 V5 的差别（本次整改的全部内容）：
 *   * 顶部「作用域 / provider / 模型」表单（TargetBanner + AgentPicker）删掉：
 *     agent 由列表勾选决定，不再有一个「先选 agent 再填表」的中间层；
 *   * 右侧「切换模型」卡片（SwitchPanel）删掉：它与批量面板是同一件事的两套表单；
 *   * 批量面板（BatchPanel）删掉：单个与批量合并成 **一个** AgentEditor
 *     （选 1 个 = 单编辑态，选 N 个 = 批量态，「保持不变」字段绝不覆盖别人的值）；
 *   * 布局改成整宽两栏（Agent 表 + 编辑器），不再有「上方只用左侧 1/4 宽」的大片空白；
 *   * provider 目录 / 模型库 / 历史与回滚 / 预设收进次级 tab，功能一个不丢。
 *
 * 数据仍然全部来自本插件自己的端点，写操作仍然两步确认 + 逐 agent 快照 + 回读校验。
 */
export function ModelsSection(props) {
  var narrow = useNarrow()
  var ready = props.ready !== false

  /** 勾选的 agent（`{id: true}`；顺序以概览行为准）。 */
  var selectedState = useState({})
  var selected = selectedState[0]
  /** 编辑器是否展开（选择清空时自动收起）。 */
  var openState = useState(false)
  var opened = openState[0] === true
  /** 次级入口。 */
  var tabState = useState('agents')
  var tab = tabState[0]
  /** 搜索框（只过滤列表，不改任何请求）。 */
  var searchState = useState('')
  var search = searchState[0]
  var actionState = useState({ busy: false, result: null })
  var action = actionState[0]

  var overviewQuery = useQuery(
    CACHE_OVERVIEW,
    function () {
      return get('/models/overview', { timeoutMs: 20000 })
    },
    { staleMs: 15000, enabled: ready }
  )
  var overview = payloadOf(overviewQuery)
  var allRows = overview && Array.isArray(overview.agents) ? overview.agents : []

  var selectedIds = []
  allRows.forEach(function (row) {
    var id = agentOf(row)
    if (id && selected[id]) selectedIds.push(id)
  })
  /** 次级 tab（provider 目录 / 模型库 / 预设）的作用域：跟着选择走，没选就用 default。 */
  var focusAgent = selectedIds.length ? selectedIds[0] : 'default'

  var providersQuery = useQuery(
    CACHE_PROVIDERS + '/' + focusAgent,
    function () {
      return get('/models/providers', { agent: focusAgent, all: '1', timeoutMs: 20000 })
    },
    { staleMs: 15000, enabled: ready && Boolean(focusAgent) }
  )
  /**
   * 模型库的实时拉取开关。
   *
   * `libLive` 默认 **true**：模型库要显示的是「供应商真实可用的模型」，不是配置里手写的那几条。
   * `libNonce` 是手动刷新的开关：点「刷新」⇒ nonce+1 ⇒ 缓存键变化 ⇒ 一定发新请求且带
   * `refresh=1`（后端无条件绕过 TTL 缓存），不是"把旧值再显示一遍"。
   */
  var libLiveState = useState(true)
  var libLive = libLiveState[0] !== false
  var libNonceState = useState(0)
  var libNonce = libNonceState[0]
  var libraryQuery = useQuery(
    CACHE_LIBRARY + '/' + focusAgent + '/' + (libLive ? 'live' : 'config') + '/' + libNonce,
    function () {
      var params = { agent: focusAgent }
      if (libLive) params.live = '1'
      if (libNonce > 0) params.refresh = '1'
      return get('/models/model-library', { params: params, timeoutMs: 60000 })
    },
    { staleMs: 30000, enabled: ready && Boolean(focusAgent) }
  )
  var snapshotsQuery = useQuery(
    CACHE_SNAPSHOTS,
    function () {
      return get('/models/snapshots')
    },
    { staleMs: 10000, enabled: ready }
  )
  var presetsQuery = useQuery(
    CACHE_PRESETS + '/' + focusAgent,
    function () {
      return get('/models/presets', { agent: focusAgent })
    },
    { staleMs: 30000, enabled: ready && Boolean(focusAgent) }
  )
  var batchesQuery = useQuery(
    CACHE_BATCHES,
    function () {
      return get('/models/batches', { params: { limit: '10' } })
    },
    { staleMs: 5000, enabled: ready }
  )
  /**
   * MoA 配方视图（**只读**）：`/models/models?provider=moa` 对该 agent 返回
   * `moa.presets` 的预设名 + `source` 三种状态 + `default_model` + `disabled_models`。
   * 不联网（moa 的 base_url 是 `moa://local` 本地标记）。
   */
  var moaQuery = useQuery(
    CACHE_MODELS + '/moa/' + focusAgent,
    function () {
      return get('/models/models', { params: { provider: 'moa' }, agent: focusAgent, timeoutMs: 20000 })
    },
    { staleMs: 30000, enabled: ready && Boolean(focusAgent) }
  )

  var providersBody = payloadOf(providersQuery)
  var providers = providersBody && Array.isArray(providersBody.providers) ? providersBody.providers : []
  var libraryBody = payloadOf(libraryQuery)
  var libraryGroups = libraryBody && Array.isArray(libraryBody.providers) ? libraryBody.providers : []
  var snapshotsBody = payloadOf(snapshotsQuery)
  var snapshots = snapshotsBody && Array.isArray(snapshotsBody.snapshots) ? snapshotsBody.snapshots : []
  var presetsBody = payloadOf(presetsQuery)
  var presets = presetsBody && Array.isArray(presetsBody.presets) ? presetsBody.presets : []
  var batchesBody = payloadOf(batchesQuery)
  var batches = batchesBody && Array.isArray(batchesBody.batches) ? batchesBody.batches : []
  var moaBody = payloadOf(moaQuery)
  var moaInfo = moaBody && typeof moaBody === 'object' ? moaBody : null
  /**
   * MoA **配方内容**（`GET /models/moa/presets?agent=X`）：每条预设的
   * `reference_models` / `aggregator` / `enabled` + 谁在引用 + 悬空引用红灯。
   * 与上面 `moaInfo`（只给预设名）是两件事：这里才有内容可编辑。
   */
  var moaPresetsQuery = useQuery(
    CACHE_MOA_PRESETS + '/' + focusAgent,
    function () {
      return get('/models/moa/presets', { params: { agent: focusAgent }, timeoutMs: 20000 })
    },
    { staleMs: 15000, enabled: ready && Boolean(focusAgent) }
  )
  var moaPresetsBody = payloadOf(moaPresetsQuery)
  var moaPresets = moaPresetsBody && typeof moaPresetsBody === 'object' ? moaPresetsBody : null
  /** 后端是否已经声明 `presets` 段（没声明 ⇒ MoA 配方编辑器保持禁用）。 */
  var moaBackendReady = Boolean(overview && Array.isArray(overview.scopes) && overview.scopes.indexOf('presets') >= 0)

  /**
   * 编辑器下拉里的 provider = **所有 agent 的并集**：
   * 每个 agent 行里 `providers`（它 config.yaml 声明过的）+ 它当前的 provider
   * + 当前作用域视角的 provider 目录（含虚拟 provider moa 与探到 key 的内置项）。
   * 只取一个 agent 的目录会漏掉别的 agent 声明过的 provider，批量就选不到。
   */
  var providerNames = []
  var seenProvider = {}
  var pushProvider = function (name) {
    var id = String(name || '').trim()
    if (!id || seenProvider[id.toLowerCase()]) return
    seenProvider[id.toLowerCase()] = true
    providerNames.push(id)
  }
  allRows.forEach(function (row) {
    if (row && row.provider) pushProvider(row.provider)
    var declared = row && Array.isArray(row.providers) ? row.providers : []
    declared.forEach(pushProvider)
  })
  providers.forEach(function (row) {
    if (row) pushProvider(row.provider)
  })

  var missingCount = allRows.filter(function (row) {
    return !row || !row.provider || !row.model
  }).length

  var filteredRows = allRows.filter(function (row) {
    var needle = search.trim().toLowerCase()
    if (!needle) return true
    var id = agentOf(row)
    var hay = [id, row.label, row.provider, row.model, row.base_url].join(' ').toLowerCase()
    return hay.indexOf(needle) >= 0
  })

  var signature = selectedIds.join(',')

  var toggle = function (id) {
    var next = {}
    Object.keys(selected).forEach(function (key) {
      next[key] = selected[key]
    })
    next[id] = !selected[id]
    selectedState[1](next)
    if (!next[id] && selectedIds.length <= 1) openState[1](false)
  }
  var selectAll = function () {
    var next = {}
    allRows.forEach(function (row) {
      var id = agentOf(row)
      if (id) next[id] = true
    })
    selectedState[1](next)
  }
  var selectNone = function () {
    selectedState[1]({})
    openState[1](false)
  }
  var selectMissing = function () {
    var next = {}
    allRows.forEach(function (row) {
      var id = agentOf(row)
      if (!id) return
      if (!row.provider || !row.model) next[id] = true
    })
    selectedState[1](next)
  }
  var editOne = function (id) {
    var next = {}
    next[id] = true
    selectedState[1](next)
    openState[1](true)
  }

  var deletePreset = function (preset) {
    actionState[1]({ busy: true, result: null })
    send('/models/presets/' + encodeURIComponent(preset.name), null, { method: 'DELETE' }).then(
      function (res) {
        actionState[1]({
          busy: false,
          result: res && res.ok
            ? { ok: true, title: '预设已删除', message: '删的是 ' + preset.name + '（只动了 presets.json）' }
            : {
                ok: false,
                title: '删除预设失败',
                message: String((res && res.message) || '未知原因') +
                  (res && res.code ? '（' + res.code + '）' : '')
              }
        })
        if (res && res.ok) refreshAll(CACHE_PREFIX)
      }
    )
  }

  var createPreset = function (draft) {
    actionState[1]({ busy: true, result: null })
    send('/models/presets', draft, { method: 'POST' }).then(function (res) {
      var body = res && res.ok && res.data ? res.data : {}
      actionState[1]({
        busy: false,
        result: res && res.ok
          ? {
              ok: true,
              title: '预设已保存',
              message: (body.name || draft.name) + ' → ' + draft.provider + ' / ' + draft.model +
                '（只写了 presets.json）'
            }
          : {
              ok: false,
              title: '保存预设失败',
              message: String((res && res.message) || '未知原因') +
                (res && res.code ? '（' + res.code + '）' : '')
            }
      })
      if (res && res.ok) refreshAll(CACHE_PREFIX)
    })
  }

  // ---- 三态 ---------------------------------------------------------------
  var content
  if (!ready || overviewQuery.status === 'loading') {
    content = jsx(Skeleton, { cards: 3, lines: 4, label: '正在读取模型配置' })
  } else if (overviewQuery.status === 'error') {
    content = jsx(ErrorState, {
      title: '模型配置没读到',
      message: overviewQuery.error,
      detail: '接口：/models/overview。这个分区读的是各 agent 自己的 config.yaml；' +
        '读不到通常是后端没挂上或 core/scope 不可用。'
    })
  } else if (!allRows.length) {
    content = jsx(Empty, {
      title: '一个 agent 都没有',
      message: 'Hermes 主目录下没有可枚举的 agent（default 之外还要求 profiles/<id>/ 带身份标记）。',
      hint: '先在「设置」或 CLI 里建一个 profile，再回来这里看它的模型。'
    })
  } else {
    var tabDefs = [
      { id: 'agents', label: 'Agent 与模型' },
      { id: 'history', label: '历史与回滚' },
      { id: 'providers', label: 'provider 目录' },
      { id: 'library', label: '模型库' },
      { id: 'presets', label: '预设' }
    ]
    var tabs = jsx('div', {
      className: 'hc-models__tabs',
      role: 'tablist',
      style: { display: 'flex', gap: 'var(--s1)', flexWrap: 'wrap', minWidth: 0 },
      children: tabDefs.map(function (item) {
        var on = tab === item.id
        return jsx('button', {
          type: 'button',
          className: on ? 'hc-btn hc-btn--tiny hc-btn--accent' : 'hc-btn hc-btn--tiny',
          role: 'tab',
          'aria-selected': on ? 'true' : 'false',
          'data-models-tab': item.id,
          onClick: function () {
            tabState[1](item.id)
          },
          children: item.label
        }, item.id)
      })
    }, 'tabs')

    var body
    if (tab === 'agents') {
      var table = jsxs('div', {
        className: 'hc-block hc-models__table',
        'data-models-table': true,
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsx(AgentTableHead, {
            narrow: narrow,
            total: filteredRows.length,
            count: selectedIds.length,
            missingCount: missingCount,
            busy: action.busy === true,
            onSelectAll: selectAll,
            onSelectNone: selectNone,
            onSelectMissing: selectMissing
          }, 'head'),
          filteredRows.length
            ? jsx('div', {
                className: 'hc-rows',
                children: filteredRows.map(function (row) {
                  var id = agentOf(row)
                  return jsx(AgentRow, {
                    row: row,
                    selected: Boolean(selected[id]),
                    narrow: narrow,
                    busy: action.busy === true,
                    onToggle: toggle,
                    onEdit: editOne
                  }, id)
                })
              }, 'rows')
            : jsx('div', {
                className: 'hc-muted',
                style: { fontSize: 'var(--fs-sm)', overflowWrap: 'anywhere', marginTop: 'var(--s2)' },
                children: '没有 agent 命中「' + search + '」：清空搜索框看全部。'
              }, 'noresult')
        ]
      }, 'table')

      var side
      if (selectedIds.length && opened) {
        side = jsxs('div', {
          className: 'hc-models__side',
          style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
          children: [
            jsx(SelectionBar, {
              count: selectedIds.length,
              busy: action.busy === true,
              onOpenEditor: function () {
                openState[1](true)
              },
              onSelectNone: selectNone
            }, 'bar'),
            jsx(AgentEditor, {
              ids: selectedIds,
              rows: allRows,
              providerNames: providerNames,
              // 思考强度的合法取值与可写性**照后端声明**（overview 的
              // reasoning_effort_values / reasoning_effort_writable + scopes），
              // 不在前端另抄一份常量，避免两边不一致。
              reasoningValues: overview && Array.isArray(overview.reasoning_effort_values)
                ? overview.reasoning_effort_values
                : [],
              reasoningWritable: Boolean(
                overview && overview.reasoning_effort_writable === true &&
                Array.isArray(overview.scopes) && overview.scopes.indexOf('reasoning') >= 0
              ),
              narrow: narrow,
              busy: action.busy === true,
              onClose: function () {
                openState[1](false)
              }
            }, 'editor-' + signature)
          ]
        }, 'side')
      } else if (selectedIds.length) {
        side = jsxs('div', {
          className: 'hc-block hc-models__side',
          style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
          children: [
            jsx(SelectionBar, {
              count: selectedIds.length,
              busy: action.busy === true,
              onOpenEditor: function () {
                openState[1](true)
              },
              onSelectNone: selectNone
            }, 'bar'),
            jsx('div', {
              className: 'hc-muted',
              style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere' },
              children: '点上面的按钮打开编辑器：选 1 个是单个编辑，选多个是批量编辑 —— 用的是同一个编辑器。'
            }, 'hint')
          ]
        }, 'side')
      } else {
        // 没有任何选择：不出右栏空壳，改成表格下面一行说明（整表占满整宽）
        side = jsx('div', {
          className: 'hc-muted hc-models__emptyhint',
          style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', marginTop: 'var(--s2)' },
          children: '勾一个 agent 做单个编辑、勾多个做批量编辑 —— 用的是同一个编辑器；' +
            '「保持不变」的字段按每个 agent 自己的现值处理，与现值相同就一个请求都不发。'
        }, 'side')
      }

      body = jsxs('div', {
        'data-models-main': true,
        style: {
          display: 'grid',
          // 没有选择时**整表占满整宽**（右栏不出空壳，避免大片空白）；有选择才分两栏
          gridTemplateColumns: narrow || !selectedIds.length
            ? 'minmax(0, 1fr)'
            : 'minmax(0, 1fr) minmax(0, 420px)',
          gap: 'var(--s4)',
          alignItems: 'start',
          minWidth: 0,
          maxWidth: '100%'
        },
        children: [table, side]
      }, 'main')

      body = jsxs('div', {
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
        children: [
          jsxs('div', {
            className: 'hc-cards',
            children: [
              jsx(Stat, { label: 'agent 总数', value: num(allRows.length), sub: '每行读自己的 config.yaml' }, 'a'),
              jsx(Stat, {
                label: '可切换 provider',
                value: providersQuery.status === 'loading' ? '—' : num(providers.length),
                sub: providersBody && providersBody.show_all ? '含未配置 key 的（all=1）' : '只列真能用的'
              }, 'p'),
              jsx(Stat, {
                label: '模型库',
                value: libraryQuery.status === 'loading'
                  ? '—'
                  : num(libraryBody && libraryBody.model_total ? libraryBody.model_total : 0),
                sub: '配置里显式列出的模型名'
              }, 'm'),
              jsx(Stat, {
                label: '快照',
                value: snapshotsQuery.status === 'loading' ? '—' : num(snapshots.length),
                sub: '写操作前自动生成'
              }, 's')
            ]
          }, 'stats'),
          body,
          jsx(Feedback, { result: action.result }, 'fb')
        ]
      }, 'agents-body')
    } else if (tab === 'history') {
      body = jsx(HistoryPanel, {
        narrow: narrow,
        agent: focusAgent,
        busy: action.busy === true,
        batches: batches,
        batchesError: batchesQuery.status === 'error' ? batchesQuery.error : '',
        snapshots: snapshots,
        snapshotsError: snapshotsQuery.status === 'error' ? snapshotsQuery.error : ''
      }, 'history')
    } else if (tab === 'providers') {
      body = jsxs('div', {
        className: 'hc-block',
        'data-models-providers': true,
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
            children: [
              jsx('div', { className: 'hc-block__title', children: 'provider 目录（' + focusAgent + '）' }, 't'),
              jsx(Tag, { off: true, children: '凭据只读' }, 'c'),
              jsx(Tag, { children: '共 ' + num(providers.length) + ' 个' }, 'n')
            ]
          }, 'head'),
          providersQuery.status === 'error'
            ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: 'provider 列表没读到：' + providersQuery.error }, 'err')
            : providers.length
              ? jsxs('div', {
                  className: 'hc-models__pv',
                  children: [
                    jsx(ProviderListHead, {}, 'head'),
                    jsx('div', {
                      className: 'hc-models__pv-list',
                      children: providers.map(function (item) {
                        return jsx(ProviderRow, { row: item }, String(item.provider || ''))
                      })
                    }, 'list')
                  ]
                }, 'pv')
              : jsx(Empty, {
                  title: '这个 agent 没有可用 provider',
                  message: 'config.yaml 里没有 providers: 声明，内置 registry 里也没有探到 key。',
                  hint: '先去该 agent 的 .env 配一个 *_API_KEY，或在 config.yaml 里声明 providers:。'
                }, 'empty'),
          jsx('div', {
            className: 'hc-muted',
            style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', marginTop: 'var(--s2)' },
            children: '凭据取值一律打码（后端 mask_sensitive）：这里只显示变量名与是否已配置。本分区不提供凭据写入入口。'
          }, 'note')
        ]
      }, 'providers')
    } else if (tab === 'library') {
      /**
       * 模型库：每个 provider 一行，**逐条标来源**。
       *
       * 三件事必须同时可见，否则就是"把两种来源混成一个数字"：
       *   1. 该 provider 的来源（配置文件 / 内置 / 虚拟）—— `provider_source`
       *   2. 配置里显式列出的模型数 —— `config_count`
       *   3. 供应商实时返回的模型数 —— `live_count`
       * 失败/跳过的 provider 单独一块说明（`live.error` / `skipped_reason`），绝不静默。
       */
      var libErrors = libraryBody && Array.isArray(libraryBody.errors) ? libraryBody.errors : []
      var libErrorBlock = libErrors.length
        ? jsxs('div', {
            'data-models-library-errors': String(libErrors.length),
            style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
            children: [
              jsx('div', {
                style: { fontWeight: 600, fontSize: 'var(--fs-md)' },
                children: num(libErrors.length) + ' 个 provider 拉取失败（不是空列表，也不是旧列表）'
              }, 't'),
              jsx('div', {
                style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
                children: libErrors.map(function (item) {
                  return jsx('div', {
                    'data-models-library-error': String(item.provider || ''),
                    style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap' },
                    children: [
                      jsx('span', { style: { fontFamily: MONO, fontWeight: 600 }, children: String(item.provider || '') }, 'p'),
                      jsx('span', { className: 'hc-chip hc-chip--off', children: String(item.error_kind || 'error') }, 'k'),
                      jsx('span', { className: 'hc-muted', children: String(item.error || '') }, 'e')
                    ]
                  }, String(item.provider || ''))
                })
              }, 'list')
            ]
          }, 'liberrors')
        : null
      body = jsxs('div', {
        className: 'hc-block',
        'data-models-library': true,
        'data-models-library-live': libLive ? '1' : '0',
        style: { minWidth: 0, maxWidth: '100%' },
        children: [
          jsxs('div', {
            className: 'hc-block__head',
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
            children: [
              jsx('div', { className: 'hc-block__title', children: '模型库（' + focusAgent + '）' }, 't'),
              jsx(Tag, { children: num(libraryGroups.length) + ' 个 provider' }, 'g'),
              jsx(Tag, {
                off: true,
                'data-models-library-config-total': String(libraryBody && libraryBody.config_total || 0),
                title: '配置里显式列出的模型名（providers.<name>.models）',
                children: '配置显式 ' + num(libraryBody && libraryBody.config_total || 0)
              }, 'cfg'),
              jsx(Tag, {
                'data-models-library-live-total': String(libraryBody && libraryBody.live_total || 0),
                title: '供应商 /models 实时返回的模型名（与你配置里写没写无关）',
                children: '实时拉取 ' + num(libraryBody && libraryBody.live_total || 0)
              }, 'live'),
              jsx('button', {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-models-library-refresh': true,
                disabled: action.busy === true || libraryQuery.busy === true,
                title: '绕过 60s 缓存，逐 provider 真向供应商 /models 发一次请求',
                onClick: function () {
                  libLiveState[1](true)
                  libNonceState[1](libNonce + 1)
                },
                children: libraryQuery.busy === true ? '正在向上游拉取…' : '刷新（实时拉取）'
              }, 'refresh'),
              jsx('button', {
                type: 'button',
                className: 'hc-btn hc-btn--tiny',
                'data-models-library-mode': libLive ? 'live' : 'config',
                title: '只读该 agent 的 config.yaml（不发任何外网请求）',
                onClick: function () {
                  libLiveState[1](!libLive)
                },
                children: libLive ? '只看配置（不联网）' : '改为实时拉取'
              }, 'mode')
            ]
          }, 'head'),
          libraryQuery.status === 'error'
            ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: '模型库没读到：' + libraryQuery.error }, 'err')
            : libraryGroups.length
              ? jsxs('div', {
                  style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
                  children: [
                    libErrorBlock
                  ].concat(libraryGroups.map(function (group) {
                    var names = Array.isArray(group.models) ? group.models : []
                    var entries = Array.isArray(group.model_entries) ? group.model_entries : []
                    var liveInfo = group.live && typeof group.live === 'object' ? group.live : {}
                    var liveOk = liveInfo.ok === true
                    var liveErr = String(liveInfo.error || '')
                    var skipped = String(group.skipped_live || '')
                    var statusChip = liveOk
                      ? jsx(Tag, {
                          tone: 'hc-chip--on',
                          'data-models-lib-status': 'live-ok',
                          children: '实时 ' + num(group.live_count || 0) + ' 个'
                        }, 'ok')
                      : liveErr
                        ? jsx(Tag, {
                            off: true,
                            'data-models-lib-status': 'live-error',
                            title: liveErr,
                            children: '拉取失败：' + String(liveInfo.error_kind || 'error')
                          }, 'bad')
                        : jsx(Tag, {
                            off: true,
                            'data-models-lib-status': skipped || 'not-requested',
                            title: String(group.skipped_reason || ''),
                            children: skipped === 'not_configured' ? '未配置（不联网）'
                              : skipped === 'local_no_http_endpoint' ? '本地 provider（不联网）'
                                : libLive ? '未拉取' : '只看配置'
                          }, 'skip')
                    return jsxs('div', {
                      className: 'hc-models__lib',
                      'data-models-lib': String(group.provider || ''),
                      'data-models-lib-provider-source': String(group.provider_source || ''),
                      'data-models-lib-source': String(group.source || ''),
                      'data-models-lib-live-ok': liveOk ? '1' : '0',
                      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s1)', minWidth: 0 },
                      children: [
                        jsxs('div', {
                          style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center', minWidth: 0 },
                          children: [
                            jsx('span', { style: { fontFamily: MONO, fontSize: 'var(--fs-md)', fontWeight: 600 }, children: String(group.provider || '') }, 'p'),
                            jsx(Tag, {
                              off: true,
                              'data-models-lib-provider-badge': String(group.provider_source || ''),
                              children: String(group.provider_source_label || group.provider_source || '未知')
                            }, 'src'),
                            jsx(Tag, { children: num(names.length) + ' 个模型' }, 'c'),
                            jsx(Tag, {
                              off: true,
                              'data-models-lib-config-count': String(group.config_count || 0),
                              title: '配置里显式列出的模型名个数',
                              children: '配置 ' + num(group.config_count || 0)
                            }, 'cfg'),
                            statusChip,
                            group.current ? jsx(Tag, { tone: 'hc-chip--on', children: '当前' }, 'cur') : null
                          ]
                        }, 'head'),
                        liveErr
                          ? jsx('div', {
                              className: 'hc-muted',
                              'data-models-lib-live-error': true,
                              style: { fontSize: 'var(--fs-xs)', overflowWrap: 'anywhere', color: 'var(--ui-danger, inherit)' },
                              children: '实时拉取失败：' + liveErr
                            }, 'liveerr')
                          : null,
                        names.length
                          ? jsx('div', {
                              style: { display: 'flex', gap: 'var(--s1)', flexWrap: 'wrap', minWidth: 0 },
                              children: names.map(function (name) {
                                var entry = null
                                for (var idx = 0; idx < entries.length; idx += 1) {
                                  if (entries[idx] && entries[idx].name === name) entry = entries[idx]
                                }
                                var kind = entry && entry.source ? String(entry.source) : ''
                                var label = kind === 'config' ? '配置显式'
                                  : kind === 'live' ? '实时拉取'
                                    : kind === 'config+live' ? '配置 + 实时' : ''
                                return jsxs('span', {
                                  className: 'hc-chip',
                                  'data-models-lib-model': name,
                                  'data-models-lib-model-source': kind,
                                  style: { fontFamily: MONO },
                                  title: name + (label ? ' · 来源：' + label : ''),
                                  children: [
                                    jsx('span', { children: name }, 'n'),
                                    label
                                      ? jsx('span', {
                                          style: { marginLeft: '0.35em', opacity: 0.75, fontFamily: 'inherit', fontSize: 'var(--fs-xs)' },
                                          children: label
                                        }, 's')
                                      : null
                                  ]
                                }, name)
                              })
                            }, 'list')
                          : jsx('div', {
                              className: 'hc-muted',
                              style: { fontSize: 'var(--fs-xs)' },
                              children: liveOk
                                ? '供应商返回了 0 个模型。'
                                : (libLive
                                    ? '这个 provider 没有可显示的模型：实时拉取失败（原因见上），配置里也没列出模型名。'
                                    : '这个 provider 在配置里没列出模型名（点「刷新（实时拉取）」向供应商要真实清单）。')
                            }, 'none')
                      ]
                    }, String(group.provider || ''))
                  }))
                })
              : jsx(Empty, {
                  title: '模型库是空的',
                  message: '这个 agent 的 provider 目录里一个 provider 都没有。',
                  hint: '先在 config.yaml 的 providers: 里声明，或点上方「刷新（实时拉取）」。'
                }, 'empty')
        ]
      }, 'library')
    } else {
      body = jsxs('div', {
        className: 'hc-block',
        'data-models-presets': true,
        style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
        children: [
          // ★ 两件不同的事，必须分开摆：MoA 配方（moa.presets）vs console 草稿（data/presets.json）
          jsx(MoaPresetPanel, {
            info: moaInfo,
            moa: moaPresets,
            agent: focusAgent,
            providerOptions: providerNames,
            reasoningValues: overview && Array.isArray(overview.reasoning_effort_values)
              ? overview.reasoning_effort_values
              : [],
            busy: action.busy === true,
            narrow: narrow
          }, 'moa'),
          jsxs('div', {
            className: 'hc-block__head',
            style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
            children: [
              jsx('div', { className: 'hc-block__title', children: 'console 草稿（' + focusAgent + '）' }, 't'),
              jsx(Tag, { children: num(presets.length) + ' 条' }, 'n'),
              jsx(Tag, {
                off: true,
                title: '这些是 console 自己的 data/presets.json，只是表单草稿，不是 MoA 配方',
                children: '不是 MoA 配方'
              }, 'r')
            ]
          }, 'head'),
          presetsQuery.status === 'error'
            ? jsx('div', { className: 'hc-muted', style: { overflowWrap: 'anywhere' }, children: '预设没读到：' + presetsQuery.error }, 'err')
            : presets.length
              ? jsx('div', {
                  className: 'hc-rows',
                  children: presets.map(function (item) {
                    return jsx('div', {
                      'data-models-preset': String(item.name || ''),
                      children: jsx(PresetRow, {
                        row: item,
                        narrow: narrow,
                        busy: action.busy === true,
                        onUse: function (preset) {
                          actionState[1]({
                            busy: false,
                            result: {
                              ok: true,
                              title: '预设：' + preset.name,
                              message: orDash(preset.provider) + ' / ' + orDash(preset.model) +
                                ' —— 去「Agent 与模型」勾选 agent 后填进编辑器即可。'
                            }
                          })
                        },
                        onDelete: deletePreset
                      }, 'row')
                    }, String(item.name || ''))
                  })
                }, 'list')
              : jsx(Empty, {
                  title: '没有预设',
                  message: '预设是「provider + 模型」的命名组合，存在 console 自己的数据目录里。',
                  hint: '用下面的表单建一条；它只写 presets.json，不动 profile 配置。'
                }, 'empty'),
          jsx(PresetForm, {
            narrow: narrow,
            providers: providers,
            busy: action.busy === true,
            onCreate: createPreset
          }, 'form')
        ]
      }, 'presets')
    }

    content = jsxs('div', {
      className: 'hc-models',
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
      children: [tabs, body]
    }, 'content')
  }

  return jsxs('div', {
    className: 'hc-pane__inner',
    style: { display: 'flex', flexDirection: 'column', gap: 'var(--s3)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsxs('div', {
        className: 'hc-block__head hc-models__toolbar',
        'data-models-toolbar': true,
        style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', alignItems: 'center' },
        children: [
          jsx('div', { className: 'hc-block__title', children: '模型' }, 't'),
          jsx(Tag, { off: true, title: 'apply / rollback 会改真实 profile 的 config.yaml', children: '高危写操作 · 需二次确认' }, 'w'),
          jsx('input', {
            className: 'hc-input',
            type: 'search',
            'data-models-search': true,
            value: search,
            placeholder: '搜 agent / provider / 模型',
            style: { flex: '1 1 200px', minWidth: 0, maxWidth: '320px' },
            onChange: function (event) {
              searchState[1](event.target.value)
            }
          }, 'search'),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            onClick: function () {
              refreshAll(CACHE_PREFIX)
            },
            disabled: action.busy === true,
            children: '刷新'
          }, 'reload')
        ]
      }, 'head'),
      content
    ]
  })
}
