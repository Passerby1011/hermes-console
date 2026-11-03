/**
 * hermes-console · 远端后端分区（V5）：**一屏一表**，一个 agent 一块两行，凭据按需解密。
 *
 * 数据只来自本插件自己的 6 条端点（本文件不发任何别的请求）：
 *   GET  /remote/agents              全部 agent 的状态（动态枚举；地址/端口/模型/状态/
 *                                    凭据**元信息**，一个字符的凭据取值都没有）
 *   GET  /remote/status              某 agent（或全部）的状态（本文件用 /agents 一份就够）
 *   POST /remote/agent/start         拉起某 agent 的对外后端（危险写操作，必须二次确认）
 *   POST /remote/agent/stop          停掉某 agent 的对外后端（危险写操作，必须二次确认）
 *   GET  /remote/credentials         凭据配置状态（后端全掩码，只作"有没有配"的兜底口径）
 *   POST /remote/credentials/reveal  **显式**解一个字段（点「查看 / 复制」才发，一次只取一个字段）
 *
 * 八条不许退让的规则：
 *   1. **agent 列表不写死**：一行都不在这里。渲染的就是后端这次扫盘返回的 agents 数组；
 *      接口失败 ⇒ 错误态（带后端原话 + 重试），**绝不**回退成一份"我记得的 agent 名单"
 *      —— 那正是旧插件「列表里有 9 个、按钮只对 4 个有效」的根源。
 *   2. **信息密度优先**：每个 agent 一块，第一行给 状态 / 连接 / 名称 / 地址 / 端口 / 模型 /
 *      最近启动 / 最近停止 / 最近错误 / 启停操作，第二行给**六个凭据字段**（账号 / 密码 /
 *      Token / 登录有效期 / 后端地址 / 本机地址）每字段一格，各自「查看」「复制」。
 *      目标是 1440×900 一屏看全。不再是一行一个 agent 的横向 11 列表格：那种表为了塞下列宽，
 *      把凭据压成 1 列 170px（密码和 Token 只能二选一），地址和模型根本没地方放。
 *   3. **凭据默认遮蔽**：V4c 起轮询响应里**没有任何凭据取值**（连账号名都不给），
 *      所以本文件**不再**读 `row.username`（那字段已不存在，旧代码会渲染成 `undefined`）。
 *      每个机密字段默认显示占位符 `••••••••`；只有人点**那一个字段**的「查看」时，
 *      才 POST 一次 reveal，且请求体里只带这一个 `field`。
 *   4. **不预取、不缓存不必要的明文**：打开分区、刷新、切 agent 都不发 reveal；
 *      点「查看」解出来的明文只活在组件 state 里（随分区卸载消失），不进查询缓存、
 *      不进 localStorage、不进 URL、**不写任何日志**（本文件零 console 调用）、
 *      本分区也没有任何导出 / CSV 出口（明文不会被"顺手导出"）。
 *      点「复制」拿到的值只进剪贴板，**不落到屏幕上**（与「查看」是两条路）。
 *   5. **每个字段独立复制 + 明确反馈**：账号 / 密码 / Token / 后端地址各有自己的复制按钮
 *      （其余字段同样有），复制的是 reveal 回来的**真实取值**；成功按钮变「已复制」，
 *      失败变「复制失败」并在字段下方写出后端原话（不伪造成功、不把掩码当明文）。
 *   6. **启停必须二次确认**，确认条上写明**目标 agent 的中文名与 id**（还写端口），
 *      点「确认」之前一个请求都不发；执行完把后端回读的 state/端口/PID 显示出来。
 *   7. **三态齐备**：加载中是骨架屏；没有 agent 是空态（说清为什么空 + 下一步）；
 *      请求失败是错误态，后端 `error.message` 原样透出（rest.js 已抽出 message）。
 *   8. 颜色只走 `var(--ui-*)`（本文件零字面色值），间距只走 `var(--s*)`；
 *      窄窗（<900px）凭据区收成两列而不是横向滚动，文本一律 `overflow-wrap: anywhere`。
 *
 * `src/sections/remote.new.js`：**不存在**（本卡核查过插件树与全盘 find，也没有
 * `remote.*.js` 之类的遗留副本），因此没有"可复用的第二套实现"可捡，也没有遗留文件
 * 需要清理 —— 本分区**只有这一个**实现文件。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { refreshAll, useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 缓存键：前缀 `remote/` 让"操作成功后刷新"只重取本分区。 */
var AGENTS_KEY = 'remote/agents'
var CACHE_PREFIX = 'remote/'
/** 明文唯一出口（后端模块头写死的契约；路径相对本插件命名空间）。 */
var REVEAL_ENDPOINT = '/remote/credentials/reveal'

/** 窄窗断点：`max-width: 900px` 以下凭据区收成两列。 */
var NARROW_PX = 900
var NARROW_QUERY = '(max-width: 900px)'

/** 掩码占位符：不显示位数、不显示首尾。 */
var MASK = '••••••••'
/** 复制反馈停留时间（毫秒）。 */
var COPY_FLASH_MS = 1600

/** 后端 `credential_fields` 缺 `label` 时的字段名兜底（顺序以后端为准）。 */
var FIELD_LABELS = {
  username: '账号',
  password: '密码',
  secret: 'Token',
  ttl_seconds: '登录有效期',
  base_url: '后端地址',
  local_url: '本机地址'
}

/**
 * 订阅窗口宽度是否窄于 :data:`NARROW_PX`。
 * 没有 `matchMedia`（探针里的极简环境）时按"宽"处理：布局不会崩，只是不折叠。
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

/** 行数据取 `agent`（旧插件字段名是 `profile`，两个都认）。 */
function agentIdOf(row) {
  var source = row || {}
  return typeof source.agent === 'string' ? source.agent : typeof source.profile === 'string' ? source.profile : ''
}

/** 状态文案：运行中 / 已停止（`state` 缺失时按 running 推断，再缺就"未知"）。 */
function stateOf(row) {
  var source = row || {}
  if (source.state === 'running' || source.running === true) return { id: 'running', text: '运行中' }
  if (source.state === 'stopped' || source.running === false) return { id: 'stopped', text: '已停止' }
  return { id: 'unknown', text: '未知' }
}

/**
 * 连接状态（后端 `conn`）：运行中只是"进程在听"，能不能连上是另一维。
 * 没有 `conn`（旧后端）时按 running / probe 推断，不编造。
 */
function connOf(row) {
  var source = row || {}
  var raw = typeof source.conn === 'string' ? source.conn : ''
  if (raw === 'connected') return { id: 'connected', text: '可连', tone: 'ok' }
  if (raw === 'auth_required') return { id: 'auth_required', text: '可连·需登录', tone: 'warn' }
  if (raw === 'unreachable') return { id: 'unreachable', text: '连不上', tone: 'warn' }
  if (raw === 'stopped') return { id: 'stopped', text: '未启动', tone: 'off' }
  if (raw === 'unknown') return { id: 'unknown', text: '未知', tone: 'off' }
  if (stateOf(source).id !== 'running') return { id: 'stopped', text: '未启动', tone: 'off' }
  var probe = source.probe
  if (probe && probe.ok === false) return { id: 'unreachable', text: '连不上', tone: 'warn' }
  if (probe && probe.auth_required) return { id: 'auth_required', text: '可连·需登录', tone: 'warn' }
  if (probe && probe.ok) return { id: 'connected', text: '可连', tone: 'ok' }
  return { id: 'unknown', text: '未知', tone: 'off' }
}

/** 时间戳 → 表里那一档（`2026-10-29 20:41:03` ⇒ `10-29 20:41`；解析不了就原样给）。 */
function stampText(value) {
  var text = value === null || value === undefined ? '' : String(value)
  if (!text) return '—'
  var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(text)
  return m ? m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : text
}

/** 文本 → 定长展示（超长截断，title 里保留全文）。 */
function clip(value, n) {
  var text = value === null || value === undefined ? '' : String(value)
  if (text.length <= n) return text
  return text.slice(0, n - 1) + '…'
}

/** 端口 / 模型 / 地址这类"没有就不编"的值。 */
function dash(value) {
  if (value === null || value === undefined) return '—'
  var text = String(value).trim()
  return text ? text : '—'
}

/**
 * 凭据字段清单（后端 `credential_fields`，V5 起才有）。
 *
 * 后端给什么就渲染什么 —— 本文件**不写死**字段表，这样后端加字段界面自动跟上。
 * 老后端（没有这份清单）不给值也不装：只提示一句"后端没给字段清单"，
 * 绝不用 `row.username` 之类的旧字段猜。
 * @returns {Array<object>}
 */
function fieldsOf(row) {
  var source = row || {}
  var list = Array.isArray(source.credential_fields) ? source.credential_fields : []
  return list.filter(function (field) {
    return field && typeof field === 'object' && typeof field.field === 'string' && field.field
  })
}

/**
 * 复制到剪贴板：优先异步 Clipboard API，失败退回隐藏 textarea + execCommand。
 * 永不抛 —— 复制失败只是提示"复制失败"，不该把面板打成错误态。
 * @param {string} text
 * @returns {Promise<boolean>}
 */
function copyText(text) {
  var value = text === null || text === undefined ? '' : String(text)
  if (!value) return Promise.resolve(false)
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      return navigator.clipboard.writeText(value).then(function () {
        return true
      }).catch(function () {
        return legacyCopy(value)
      })
    }
  } catch (e) {
    // 落到下面的兜底
  }
  return Promise.resolve(legacyCopy(value))
}

/** execCommand 兜底（宿主禁了 Clipboard API 时唯一可行的路）。 */
function legacyCopy(value) {
  try {
    if (typeof document === 'undefined' || !document.body) return false
    var area = document.createElement('textarea')
    area.value = value
    area.setAttribute('readonly', 'readonly')
    area.style.position = 'absolute'
    area.style.left = '-9999px'
    document.body.appendChild(area)
    area.select()
    var ok = document.execCommand ? document.execCommand('copy') : false
    document.body.removeChild(area)
    return Boolean(ok)
  } catch (e) {
    return false
  }
}

/** 状态小标（形状 + 文字双通道，不靠颜色单独表达）。 */
function StatusTag(props) {
  var state = stateOf(props.row)
  var tone = state.id === 'running' ? ' hc-status--ok' : state.id === 'stopped' ? ' hc-status--off' : ''
  return jsx('span', {
    className: 'hc-status' + tone,
    'data-remote-state': state.id,
    children: state.text
  })
}

/** 连接小标（与状态分开：在跑但连不上是真实存在的第四种情况）。 */
function ConnTag(props) {
  var conn = connOf(props.row)
  var tone = conn.tone === 'warn' ? ' hc-status--warn' : conn.tone === 'ok' ? ' hc-status--ok' : ' hc-status--off'
  return jsx('span', {
    className: 'hc-status' + tone,
    'data-remote-conn': conn.id,
    children: conn.text
  })
}

/** 统一定宽的小标签（计数 / 来源）。 */
function Tag(props) {
  return jsx('span', {
    className: 'hc-chip' + (props.off ? ' hc-chip--off' : ''),
    title: props.title || '',
    children: props.children
  })
}

/** 行内小按钮（每格最多三个）。 */
function TinyButton(props) {
  return jsx('button', {
    type: 'button',
    className: 'hc-btn hc-btn--tiny' + (props.accent ? ' hc-btn--accent' : '') + (props.danger ? ' hc-btn--danger' : ''),
    disabled: Boolean(props.disabled),
    title: props.title || '',
    onClick: props.onClick,
    'data-remote-action': props.action || undefined,
    'data-remote-agent': props.agent || undefined,
    'data-remote-field': props.field || undefined,
    children: props.children
  })
}

/** `标签 值` 的小单元（第一行里成串排，窄了自动换行）。 */
function Kv(props) {
  return jsxs('span', {
    className: 'hc-rt__kv',
    style: { display: 'inline-flex', alignItems: 'baseline', gap: 'var(--s1)', minWidth: 0, maxWidth: '100%' },
    children: [
      jsx('span', { className: 'hc-rt__id', children: props.label }, 'k'),
      jsx(
        'span',
        {
          className: 'hc-rt__text',
          title: props.title || props.value || '',
          style: props.width ? { maxWidth: props.width } : undefined,
          children: props.value
        },
        'v'
      )
    ]
  })
}

/**
 * 二次确认条：**它出现时一个请求都还没发**。
 * 文案必须点名目标 agent（中文名 + id），否则"确认启停"就成了盲签。
 */
function ConfirmBar(props) {
  var verb = props.kind === 'start' ? '启动' : '停止'
  var question =
    '确认' + verb + '「' + (props.label || props.agent) + '」（' + props.agent + '）的对外后端' +
    (props.kind === 'start' && props.port ? '？端口 ' + props.port : '？')
  return jsxs('div', {
    // 与升级分区共用同一套确认条样式（.hc-up__confirm），不新造视觉
    className: 'hc-up__confirm hc-rt__confirm',
    'data-remote-confirm': props.kind + ':' + props.agent,
    role: 'group',
    'aria-label': question,
    children: [
      jsx('span', { className: 'hc-up__confirm-q', children: question }, 'q'),
      jsx(
        'button',
        {
          type: 'button',
          className: 'hc-btn hc-btn--accent',
          'data-remote-confirm-yes': props.kind + ':' + props.agent,
          onClick: props.onConfirm,
          children: '确认' + verb
        },
        'yes'
      ),
      jsx('button', { type: 'button', className: 'hc-btn', onClick: props.onCancel, children: '取消' }, 'no')
    ]
  })
}

/** 操作结果条：成功一句话（含回读的端口/PID）；失败给后端原话 + 错误码（不吞、不美化）。 */
function Feedback(props) {
  var result = props.result
  if (!result) return null
  return jsxs('div', {
    className: 'hc-up__feedback hc-rt__feedback' + (result.ok ? ' hc-up__feedback--ok' : ' hc-up__feedback--error'),
    role: 'status',
    'data-remote-feedback': result.ok ? 'ok' : 'error',
    children: [
      jsx('span', { className: 'hc-up__feedback-title', children: result.title }, 'title'),
      jsx('span', { className: 'hc-up__feedback-text', children: result.message }, 'text')
    ]
  })
}

/** 复制按钮的文案（1.6s 后由上层清掉 → 回到「复制」）。 */
function copyLabel(copied, idle) {
  if (!copied) return idle || '复制'
  return copied.ok ? '已复制' : '复制失败'
}

/**
 * 一个凭据字段 = 一格：`标签 [占位/明文/地址] [查看|隐藏] [复制]`。
 *
 * 三种形态（全部由后端元信息驱动，本文件不猜）：
 *   * `configured === false`      —— 只写"未配置"，**不给按钮**（点也只会 409）；
 *   * `secret === false`（地址）  —— 轮询响应里本来就有值，直接显示，只给「复制」；
 *   * 机密字段                    —— 默认 `••••••••`；点「查看」才 reveal 这一个字段。
 * 只存哈希（`hash_only` / `revealable === false`）的密码：写明"仅哈希"，按钮禁用，
 * 因为后端只会回 409 `not_revealable` —— 与其让人点了才看到红字，不如先说清楚。
 */
function FieldCell(props) {
  var field = props.field || {}
  var agent = props.agent
  var id = typeof field.field === 'string' ? field.field : ''
  var key = agent + ':' + id
  var label = field.label || FIELD_LABELS[id] || id
  var configured = field.configured === true
  var secret = field.secret !== false
  var revealable = field.revealable !== false && field.hash_only !== true
  var busy = props.busy === key
  var copied = props.copied && props.copied.key === key ? props.copied : null
  var notice = props.notice && props.notice.key === key ? props.notice : null
  var shown = props.value ? String(props.value) : ''
  var publicValue = !secret && field.field ? dash(props.publicValue) : ''

  var copyButton = jsx(
    TinyButton,
    {
      action: 'copy-' + id,
      agent: agent,
      field: id,
      disabled: busy || !revealable,
      title: '复制真实取值到剪贴板（点这一刻才向解密接口取一次；不进日志、不写 CSV）',
      onClick: function () {
        props.onCopy(agent, id)
      },
      children: copyLabel(copied, '复制')
    },
    'copy'
  )

  var body = null
  if (configured && !secret) {
    // 地址字段：值本来就随轮询回来（不是机密），直接摆出来
    body = jsxs(
      'span',
      {
        style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', minWidth: 0, flexWrap: 'wrap' },
        children: [
          jsx(
            'span',
            {
              className: 'hc-rt__text',
              title: publicValue,
              style: { maxWidth: '100%', fontVariantNumeric: 'tabular-nums' },
              children: publicValue
            },
            'v'
          ),
          copyButton
        ]
      },
      'body'
    )
  } else if (configured && shown) {
    // 已解密：明文只在这块 <code> 里，点「隐藏」立刻回到掩码
    body = jsxs(
      'span',
      {
        style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', minWidth: 0, flexWrap: 'wrap' },
        children: [
          jsx(
            'code',
            {
              className: 'hc-rt__secret-text',
              style: { maxWidth: '100%' },
              title: '明文只在本机界面显示；不进缓存、不写日志',
              children: shown
            },
            'v'
          ),
          jsx(
            TinyButton,
            {
              action: 'hide',
              agent: agent,
              field: id,
              title: '重新遮蔽',
              onClick: function () {
                props.onHide(agent, id)
              },
              children: '隐藏'
            },
            'hide'
          ),
          copyButton
        ]
      },
      'body'
    )
  } else if (configured) {
    body = jsxs(
      'span',
      {
        style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', minWidth: 0, flexWrap: 'wrap' },
        children: [
          jsx('span', { className: 'hc-rt__mask', 'aria-label': label + '已遮蔽', children: MASK }, 'mask'),
          jsx(
            TinyButton,
            {
              action: 'reveal',
              agent: agent,
              field: id,
              disabled: busy || !revealable,
              title: revealable
                ? '解密显示这一个字段（只发这一次请求；明文不进缓存、不写日志）'
                : '该字段只存了哈希，后端没有明文可给（reveal 会回 409 not_revealable）',
              onClick: function () {
                props.onReveal(agent, id)
              },
              children: busy ? '解密中…' : '查看'
            },
            'reveal'
          ),
          copyButton
        ]
      },
      'body'
    )
  }

  // 一格 = 一行：`标签 值/占位 [查看] [复制]`（两行会把 5 个 agent 顶出一屏）。
  return jsxs(
    'div',
    {
      'data-remote-field-row': key,
      'data-remote-field': id,
      'data-remote-masked': configured && secret && !shown ? key : undefined,
      'data-remote-secret': shown ? key : undefined,
      style: {
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--s1)',
        flexWrap: 'wrap',
        minWidth: 0,
        maxWidth: '100%',
        padding: '2px var(--s2)',
        border: 'var(--hairline) solid var(--ui-stroke-secondary, transparent)',
        borderRadius: 'var(--r-sm)',
        background: 'var(--ui-bg-secondary, transparent)',
        overflowWrap: 'anywhere'
      },
      children: [
        jsxs(
          'span',
          {
            style: { display: 'inline-flex', alignItems: 'baseline', gap: 'var(--s1)', minWidth: 0 },
            children: [
              jsx('span', { className: 'hc-rt__id', title: field.key || id, children: label }, 'label'),
              !configured ? jsx('span', { className: 'hc-rt__none', children: '未配置' }, 'none') : null,
              configured && field.hash_only === true
                ? jsx('span', { className: 'hc-rt__none', title: '只配了 PASSWORD_HASH，没有可显示的明文', children: '仅哈希' }, 'hash')
                : null,
              configured && field.source === 'shared'
                ? jsx('span', { className: 'hc-rt__none', title: '继承自顶层 .env（共享给所有 agent）', children: '共享' }, 'src')
                : null
            ]
          },
          'head'
        ),
        body,
        notice
          ? jsx(
              'span',
              {
                className: 'hc-rt__err',
                title: notice.raw || notice.text,
                'data-remote-notice': key,
                style: { flexBasis: '100%' },
                children: clip(notice.text, 64)
              },
              'notice'
            )
          : null
      ]
    },
    key
  )
}

/**
 * 一个 agent 的一块：第一行是"这行是谁、在哪、什么模型、什么状态、能不能连、什么时候启停过"，
 * 第二行是六个凭据字段（每格独立查看/复制）。**没有任何内容来自写死的名单** ——
 * 全部读自后端返回的这一条。
 */
function AgentBlock(props) {
  var row = props.row || {}
  var agent = agentIdOf(row)
  var state = stateOf(row)
  var running = state.id === 'running'
  var pending = props.pending
  var action = props.action
  var narrow = props.narrow
  var err = row.last_error && typeof row.last_error === 'object' ? row.last_error : null
  var fields = fieldsOf(row)
  var busyAction = action && action.busy && action.agent === agent

  var baseUrl = row.base_url ? String(row.base_url) : ''
  var model = row.model ? String(row.model) : ''
  var provider = row.model_provider ? String(row.model_provider) : ''

  var ops = jsxs(
    'span',
    {
      className: 'hc-rt__cell--ops',
      style: { display: 'inline-flex', alignItems: 'center', gap: 'var(--s1)', marginLeft: 'auto' },
      children: [
        jsx(
          TinyButton,
          {
            action: 'start',
            agent: agent,
            disabled: busyAction || running,
            title: running ? '这个 agent 的对外后端已经在跑' : '拉起该 agent 的对外后端（需二次确认）',
            onClick: function () {
              props.onAsk(agent, 'start')
            },
            children: busyAction && action.kind === 'start' ? '启动中…' : '启动'
          },
          'start'
        ),
        jsx(
          TinyButton,
          {
            action: 'stop',
            agent: agent,
            // 危险档（红字）：停掉一个正在跑的对外后端是破坏性操作，视觉上要和「启动」（次档描边）分开。
            danger: true,
            disabled: busyAction || !running,
            title: running ? '停掉该 agent 的对外后端（需二次确认）' : '这个 agent 的对外后端没在跑',
            onClick: function () {
              props.onAsk(agent, 'stop')
            },
            children: busyAction && action.kind === 'stop' ? '停止中…' : '停止'
          },
          'stop'
        )
      ]
    },
    'ops'
  )

  var line = jsxs(
    'div',
    {
      className: 'hc-rt__row',
      'data-remote-line': 'summary',
      style: {
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        gap: 'var(--s1) var(--s2)',
        minWidth: 0,
        padding: 'var(--s2) var(--s3) var(--s1)'
      },
      children: [
        jsx(StatusTag, { row: row }, 'state'),
        jsxs(
          'span',
          {
            style: { display: 'inline-flex', alignItems: 'baseline', gap: 'var(--s1)', minWidth: 0 },
            children: [
              jsx(
                'span',
                { className: 'hc-rt__label', title: String(row.label || agent), children: String(row.label || agent) },
                'label'
              ),
              jsx('span', { className: 'hc-rt__id', title: agent, children: agent }, 'id')
            ]
          },
          'name'
        ),
        jsx(ConnTag, { row: row }, 'conn'),
        jsx(Kv, { label: '地址', value: dash(baseUrl), title: baseUrl, width: narrow ? '100%' : '260px' }, 'url'),
        jsx(Kv, { label: '端口', value: dash(row.port) }, 'port'),
        jsx(Kv, { label: 'PID', value: dash(row.pid) }, 'pid'),
        jsx(
          Kv,
          {
            label: '模型',
            value: model ? clip(model, 34) : '—',
            title: model ? model + (provider ? '（' + provider + '）' : '') : '该 agent 没配模型（回落到顶层 config.yaml）',
            width: '240px'
          },
          'model'
        ),
        jsx(Kv, { label: '启', value: stampText(row.last_started_at), title: String(row.last_started_at || '') }, 'ls'),
        jsx(Kv, { label: '停', value: stampText(row.last_stopped_at), title: String(row.last_stopped_at || '') }, 'lp'),
        err
          ? jsx(
              Kv,
              {
                label: '错',
                value: clip(err.message, 28),
                title: String(err.at || '') + ' ' + String(err.action || '') + '：' + String(err.message || ''),
                width: '220px'
              },
              'err'
            )
          : null,
        ops
      ]
    },
    'line'
  )

  var creds = fields.length
    ? jsx(
        'div',
        {
          'data-remote-creds': agent,
          style: {
            display: 'grid',
            gridTemplateColumns: narrow ? 'repeat(2, minmax(0, 1fr))' : 'repeat(3, minmax(0, 1fr))',
            gap: 'var(--s1) var(--s2)',
            padding: '0 var(--s3) var(--s2)',
            minWidth: 0
          },
          children: fields.map(function (field) {
            var id = field.field
            var shown = props.revealed[agent] ? props.revealed[agent][id] : ''
            return jsx(
              FieldCell,
              {
                agent: agent,
                field: field,
                value: shown,
                publicValue: row[id],
                busy: props.busy,
                copied: props.copied,
                notice: props.notice,
                onReveal: props.onReveal,
                onHide: props.onHide,
                onCopy: props.onCopy
              },
              agent + ':' + id
            )
          })
        },
        'creds'
      )
    : jsx(
        'div',
        {
          className: 'hc-rt__none',
          'data-remote-creds': 'missing',
          style: { padding: '0 var(--s3) var(--s2)' },
          children: '后端没给凭据字段清单（credential_fields）：请确认 dashboard/api_remote.py 是 V5 及以上'
        },
        'creds'
      )

  var children = [line, creds]
  if (pending && pending.agent === agent) {
    children.push(
      jsx(
        ConfirmBar,
        {
          kind: pending.kind,
          agent: agent,
          label: String(row.label || agent),
          port: row.port,
          onConfirm: function () {
            props.onConfirm(agent, pending.kind)
          },
          onCancel: props.onCancel
        },
        'confirm'
      )
    )
  }
  if (action && action.result && action.result.agent === agent) {
    children.push(jsx(Feedback, { result: action.result }, 'feedback'))
  }

  return jsxs('div', {
    className: 'hc-rt__item' + (err ? ' hc-rt__item--bad' : ''),
    'data-remote-agent-row': agent,
    'data-remote-row': agent,
    style: { minWidth: 0 },
    children: children
  })
}

/**
 * 分区入口。`agent` 是顶部作用域 —— 本分区**不吃**它（远端后端要一屏看全所有 agent，
 * 与「Agent 总览」同口径），外壳里已把它标成 `ignoresScope`。
 */
export function RemoteSection(props) {
  var narrow = useNarrow()
  var ready = props.ready !== false
  var agentsQuery = useQuery(
    AGENTS_KEY,
    function () {
      return get('/remote/agents', { timeoutMs: 20000 })
    },
    { staleMs: 15000, enabled: ready }
  )

  /** 待确认的动作：`{agent, kind}`；非空时渲染确认条，**一个请求都没发**。 */
  var pendingState = useState(null)
  var pending = pendingState[0]
  /** 动作状态：`{agent, kind, busy, result}`。 */
  var actionState = useState(null)
  var action = actionState[0]
  /** 已解密的明文：`{agent: {field: '...'}}`。**只在本组件内存里**，不进查询缓存。 */
  var revealedState = useState({})
  var revealed = revealedState[0]
  /**
   * 明文代际计数：**任何"全局清空"都会 +1**（点「刷新」、启停成功、分区卸载）。
   * 还在飞的 reveal 响应回来时若代际已变，就整条丢弃 —— 否则会出现"点了刷新，
   * 半秒后上一次的明文又冒出来"（屏幕上显示着一份已经不是当前状态的值）。
   * 放在 state 里的对象上（而不是模块变量），卸载重挂时自然归零。
   */
  var revealGen = useState(function () {
    return { n: 0 }
  })[0]
  /** 正在解密的字段（`agent:field`，按钮显示"解密中…"）。 */
  var busyState = useState('')
  var busy = busyState[0]
  /** 复制反馈：`{key, ok}`，1.6s 后清掉。 */
  var copiedState = useState(null)
  var copied = copiedState[0]
  /** 解密/复制的失败原话：`{key, text}`，点下一次时清掉。 */
  var noticeState = useState(null)
  var notice = noticeState[0]

  useEffect(function () {
    if (!copied) return undefined
    var timer = setTimeout(function () {
      copiedState[1](null)
    }, COPY_FLASH_MS)
    return function () {
      clearTimeout(timer)
    }
  }, [copied])

  // 分区卸载：明文随组件一起消失；同时代际 +1，让还在飞的 reveal 响应被丢弃（不再 setState）
  useEffect(function () {
    return function () {
      revealGen.n += 1
    }
  }, [])

  var flashCopied = function (key, ok) {
    copiedState[1]({ key: key, ok: Boolean(ok) })
  }

  /**
   * 清掉某个 agent 的明文：给了 `field` 就只清那一个，否则清掉该 agent 全部。
   * 只动组件 state —— 明文从来没进过别的地方，所以这里也就没有别的地方要清。
   */
  var hideField = function (agent, field) {
    revealedState[1](function (prev) {
      var source = prev || {}
      var kept = source[agent] || {}
      var next = {}
      Object.keys(source).forEach(function (key) {
        if (key === agent) return
        next[key] = source[key]
      })
      if (!field) return next
      var rest = {}
      Object.keys(kept).forEach(function (name) {
        if (name !== field) rest[name] = kept[name]
      })
      if (Object.keys(rest).length) next[agent] = rest
      return next
    })
  }

  var hideAll = function () {
    // 代际 +1：这一刻之后回来的 reveal 响应一律作废（见 revealGen 的注释）
    revealGen.n += 1
    revealedState[1]({})
  }

  /**
   * 向唯一的明文出口要**一个字段**的值。不写日志、不落缓存、不碰查询缓存。
   * @returns {Promise<{ok: boolean, value: string}>}
   */
  var requestValue = function (agent, field) {
    var key = agent + ':' + field
    var gen = revealGen.n
    busyState[1](key)
    noticeState[1](null)
    return send(REVEAL_ENDPOINT, { agent: agent, field: field, confirm: true }, { method: 'POST' }).then(function (res) {
      if (gen !== revealGen.n) {
        // 晚到的响应：这期间用户已经刷新 / 清空过 —— 明文整条丢弃（不显示、不报错、不动新一轮的 busy）
        busyState[1](function (prev) {
          return prev === key ? '' : prev
        })
        return { ok: false, value: '', stale: true }
      }
      busyState[1]('')
      if (
        res &&
        res.ok &&
        res.data &&
        typeof res.data === 'object' &&
        typeof res.data.value === 'string' &&
        res.data.value
      ) {
        return { ok: true, value: res.data.value }
      }
      // 失败：后端原话 + 后端错误码（not_configured / not_revealable / …），不含任何取值
      // 展示时把端点路径去掉（"· /remote/credentials/reveal" 占掉半行，对人没用），
      // 完整原文仍在 title 里；明文一个字节都不会出现在这里。
      var raw = String((res && res.message) || '未知原因')
      var shownText = raw.split(' · ' + REVEAL_ENDPOINT).join('')
      noticeState[1]({ key: key, text: shownText, raw: raw })
      return { ok: false, value: '' }
    })
  }

  /** 点「查看」：解这一个字段并显示（其余字段不动）。 */
  var revealField = function (agent, field) {
    requestValue(agent, field).then(function (out) {
      if (!out.ok || out.stale) return
      revealedState[1](function (prev) {
        var next = {}
        Object.keys(prev || {}).forEach(function (key) {
          next[key] = prev[key]
        })
        next[agent] = next[agent] || {}
        next[agent][field] = out.value
        return next
      })
    })
  }

  /**
   * 点「复制」：取**真实取值**进剪贴板，屏幕上仍是掩码（除非这个字段本来就显示着）。
   * 成功/失败都给明确反馈；失败同时把后端原话写在字段下面。
   */
  var copyField = function (agent, field) {
    var key = agent + ':' + field
    var known = revealed[agent] ? revealed[agent][field] : ''
    if (known) {
      copyText(known).then(function (ok) {
        flashCopied(key, ok)
      })
      return
    }
    requestValue(agent, field).then(function (out) {
      // 晚到的响应（期间刷新过）：不写剪贴板、也不假报「复制失败」
      if (out.stale) return
      if (!out.ok) {
        flashCopied(key, false)
        return
      }
      copyText(out.value).then(function (ok) {
        flashCopied(key, ok)
      })
    })
  }

  var perform = function (agent, kind) {
    actionState[1]({ agent: agent, kind: kind, busy: true, result: null })
    pendingState[1](null)
    var request = kind === 'stop' ? '/remote/agent/stop' : '/remote/agent/start'
    send(request, { agent: agent }, { method: 'POST' }).then(function (res) {
      var result
      if (res && res.ok) {
        var body = res.data && typeof res.data === 'object' ? res.data : {}
        var inner = body.result && typeof body.result === 'object' ? body.result : {}
        // 结果回读：把后端执行完**重新读到的** state/端口/PID 直接写进回执
        var back = body.status && typeof body.status === 'object' ? body.status : {}
        var readback = []
        if (back.state) readback.push(back.state === 'running' ? '运行中' : '已停止')
        if (back.port !== null && back.port !== undefined) readback.push('端口 ' + String(back.port))
        if (back.pid !== null && back.pid !== undefined) readback.push('PID ' + String(back.pid))
        result = {
          ok: true,
          agent: agent,
          title: (kind === 'stop' ? '已停止' : '已启动') + '「' + (body.label || agent) + '」（' + agent + '）',
          message: inner.already === true
            ? '这个 agent 的对外后端本来就是这个状态，没有重复操作。'
            : '回读：' + (readback.length ? readback.join(' · ') : '已执行，状态见这一行')
        }
        // 启停会改端口/状态：之前解出来的明文一律清掉，别让旧值留在屏幕上冒充当前值
        hideAll()
        // 只刷新本分区的缓存：外壳与其它分区不重拉
        refreshAll(CACHE_PREFIX)
      } else {
        result = {
          ok: false,
          agent: agent,
          title: '操作没有完成',
          message: String((res && res.message) || '未知原因') + (res && res.code ? '（' + res.code + '）' : '')
        }
      }
      actionState[1]({ agent: agent, kind: kind, busy: false, result: result })
    })
  }

  var body = payloadOf(agentsQuery)
  var rows = body && Array.isArray(body.agents) ? body.agents : []
  var running = rows.filter(function (row) {
    return stateOf(row).id === 'running'
  }).length
  var configured = rows.filter(function (row) {
    return row && row.credentials && row.credentials.configured === true
  }).length
  var withErrors = rows.filter(function (row) {
    return Boolean(row && row.last_error)
  }).length
  var revealedCount = Object.keys(revealed || {}).reduce(function (total, agent) {
    return total + Object.keys(revealed[agent] || {}).length
  }, 0)

  var content
  if (!ready || agentsQuery.status === 'loading') {
    content = jsx(Skeleton, { cards: 2, lines: 5, label: '正在读取远端后端状态' })
  } else if (agentsQuery.status === 'error') {
    content = jsx(ErrorState, {
      title: '远端后端状态没读到',
      message: agentsQuery.error,
      detail: '接口：/remote/agents',
      onRetry: agentsQuery.reload
    })
  } else if (!rows.length) {
    content = jsx(Empty, {
      title: '还没有任何 agent',
      hint:
        '后端扫 profiles/ 得到 0 个 agent，所以没有可启停的对外后端。' +
        '先在 Hermes 里建一个 profile，再回来刷新。',
      action: { label: '重新读取', onClick: agentsQuery.reload }
    })
  } else {
    content = jsx('div', {
      className: 'hc-rt__table',
      children: jsx('div', {
        className: 'hc-rt__body',
        'data-remote-body': 'agents',
        children: rows.map(function (row) {
          var agent = agentIdOf(row)
          return jsx(
            AgentBlock,
            {
              row: row,
              narrow: narrow,
              pending: pending,
              action: action,
              revealed: revealed,
              busy: busy,
              copied: copied,
              notice: notice,
              onAsk: function (target, kind) {
                pendingState[1]({ agent: target, kind: kind })
              },
              onConfirm: perform,
              onCancel: function () {
                pendingState[1](null)
              },
              onReveal: revealField,
              onHide: hideField,
              onCopy: copyField
            },
            agent
          )
        })
      }, 'body')
    })
  }

  return jsxs('div', {
    className: 'hc-rt',
    'data-remote-view': 'backends',
    'data-remote-narrow': narrow ? 'true' : 'false',
    'data-remote-narrow-px': String(NARROW_PX),
    children: [
      jsxs('div', {
        className: 'hc-block__head hc-rt__head-line',
        children: [
          jsx('h3', { className: 'hc-block__title', children: '对外可连的 hermes serve 后端' }, 'title'),
          jsxs(
            'span',
            {
              className: 'hc-rt__summary',
              'data-remote-summary': String(rows.length),
              children: [
                jsx(Tag, { children: '共 ' + rows.length + ' 个' }, 'all'),
                jsx(Tag, { off: running === 0, children: '在跑 ' + running }, 'run'),
                jsx(Tag, { off: configured === 0, children: '凭据 ' + configured }, 'cred'),
                jsx(Tag, { off: withErrors === 0, children: '有错 ' + withErrors }, 'err'),
                revealedCount
                  ? jsx(Tag, { title: '当前屏幕上显示着明文；点「隐藏」或刷新即清空', children: '明文 ' + revealedCount }, 'open')
                  : null
              ]
            },
            'summary'
          ),
          revealedCount
            ? jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-btn hc-btn--tiny',
                  'data-remote-action': 'hide-all',
                  onClick: hideAll,
                  children: '全部遮蔽'
                },
                'hideall'
              )
            : null,
          jsx(
            'button',
            {
              type: 'button',
              className: 'hc-btn hc-btn--tiny hc-rt__refresh',
              'data-remote-action': 'refresh',
              onClick: function () {
                // 刷新就清掉屏幕上的明文：别让"上一次解出来的值"继续挂着冒充当前值
                hideAll()
                refreshAll(CACHE_PREFIX)
              },
              children: agentsQuery.busy ? '读取中…' : '刷新'
            },
            'refresh'
          )
        ]
      }, 'head'),
      jsx('div', {
        className: 'hc-rt__hint',
        children:
          '一块一个 agent：第一行是 状态 / 连接 / 地址 / 端口 / 模型 / 最近启停与错误，第二行是六个凭据字段。' +
          '账号·密码·Token·登录有效期默认遮蔽，点该字段的「查看」才向后端要一次明文（一次只取一个字段；' +
          '不进缓存、不写日志、不导出 CSV）；「复制」把真实取值送进剪贴板、屏幕上仍是掩码。' +
          '启停是真实进程操作：点一次只出现确认条，再点「确认」才执行。'
      }, 'hint'),
      jsx('div', { className: 'hc-rt__main', children: content }, 'main')
    ]
  })
}
