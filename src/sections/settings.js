/**
 * hermes-console · 设置分区（M4b1）：按 agent 的插件三态 + 插件配置（来源层 / 脱敏只读 / 乐观锁）。
 *
 * 数据全部来自 M4b0 的三条端点（本文件不发任何别的请求）：
 *   GET  /agents/<agent>/plugins                    → 三态清单（state / counts / config_digest）
 *   GET  /agents/<agent>/plugins/<plugin>/settings  → 已脱敏值 + 每键来源 + writable/readonly
 *   PUT  /agents/<agent>/plugins/<plugin>/settings  → {key, value, expected_config} 写单项
 *   GET  /agents/<agent>/modules                    → 关联控制台面板（module:<...> 命名空间）
 *   PUT  /agents/<agent>/plugins/<plugin>           → {enabled, expected_config} 启停（core.plugin_switch 口径）
 *
 * 六条不许退让的规则（第 6 条「找得到」是后补的：清单 50+ 行时，插件在清单里也必须翻得到）：
 *   1. **三态显式**：`enabled` / `disabled` / `unavailable` 三种文案 + 三种动作，绝不压成一个开关。
 *      `unavailable` **不渲染任何开关**，也不许出现「已启用」字样（它只是「两份名单里都没有」）。
 *   2. **命名空间**：插件是 `plugin:<id>`，控制台面板是 `module:hermes-console/<id>`。
 *      两边 id 允许重名（真实例子：插件 `advisor` vs 面板 `advisor`），界面上必须一眼可分，
 *      内部 key 一律带前缀，不许拿裸 id 当唯一标识。
 *   3. **脱敏只读**：`masked_keys` 里的输入框 disabled 且显示 `***`；提交路径上有第二道闸门
 *      （buildPutBody 见到 `***` 直接拒绝发请求）—— 掩码永远上不了 PUT。
 *   4. **乐观锁**：GET 记下 `expected_config`，PUT 带上；409 `config_changed` 不静默重试，
 *      只提示「配置已被别处改动，请刷新后重试」+ 刷新按钮。
 *   5. **失败原样透出**：后端 4xx/5xx 的 `error.message` 进 ErrorState，不自己编原因，
 *      也**不翻转界面状态假装成功**（启停 / 保存失败后行状态保持原样）。
 *   6. **找得到**：左列顶部有搜索框（匹配 id / 显示名 / catalog 的 `aliases` 中文别名）
 *      与「只看可配置」开关。搜索**只过滤已经拿到的清单**：不发请求、不改三态、不动计数，
 *      命中数单独显示（「命中 1 / 共 51」不能被读成「只有 1 个插件」）。
 *
 * 容错：catalog 字段（`ref` / `display_name` / `kind` / `origin` / `version` / `has_dashboard_module`
 * / `module` / `module_ref` / `in_catalog` / `note` / `aliases`）**有就用、缺就降级**，多出未知字段直接忽略。
 * 后端只给 M4b0 的六个键时本页照常渲染 —— 不为某个字段缺失而崩。
 *
 * 颜色一律来自 CSS 变量（style.css 里的 `.hc-set*` 只用 var(--ui-*)），本文件零字面色值。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'

import { useQuery, refresh } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { readPref, writePref } from '../lib/storage.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 脱敏占位符（与后端 `api_console_settings.MASK_PLACEHOLDER` 同字面量）。 */
export var MASK_PLACEHOLDER = '***'

/** 页内选中的 agent 记在这个偏好键下（名字空间由宿主补 `hermes.plugin.hermes-console.`）。 */
export var PICK_KEY = 'settings.agent'

/** 毫秒时钟（`performance.now()` 更稳，没有就用 Date）。 */
function nowMs() {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now()
}

/**
 * 一个「跨渲染保留、改它不触发重渲」的引用（计时器起点用；直接包装 useRef，不自己造 hooks）。
 */
function useRefSafe(initial) {
  return useRef(initial)
}

/** 三态 → 界面文案。**不许合并**，三种语义必须一眼可分。 */
export var STATE_TEXT = {
  enabled: '已启用',
  disabled: '已被禁用',
  unavailable: '不在该 Agent 可用范围'
}

/** 三态 → 一句话说明（为什么是这个状态、能不能动）。 */
export var STATE_HINT = {
  enabled: '这个 Agent 的 plugins.enabled 列表里有它，当前生效。关闭会把它加进 plugins.disabled。',
  disabled: '这个 Agent 的 plugins.disabled 列表里有它，当前被停用。开启会把它从黑名单里摘掉。',
  unavailable: '该 Agent 的 enabled 列表里没有它，启用需改动允许列表（本页不替你改允许清单）。'
}

/** 来源层 → 界面文案。 */
export var SOURCE_TEXT = {
  module_default: '模块默认',
  global: '全局',
  agent: '本 Agent 覆盖'
}

/** 未知来源层：原样显示后端给的值，不自己编一个「未知」。 */
function sourceLabel(source) {
  var value = typeof source === 'string' ? source : ''
  if (SOURCE_TEXT[value]) return SOURCE_TEXT[value]
  return value || '来源未标注'
}

/** 插件在界面上的唯一 key：**裸 id 不许当标识**（会和同名面板串）。 */
export function pluginKey(pluginId) {
  return 'plugin:' + pluginId
}

/**
 * 搜索命中：`plugin_id` / `display_name` / `aliases`（catalog 声明的中文别名）/
 * `ref` / `module_ref` / `kind`，大小写不敏感。
 *
 * 为什么把 `aliases` 算进来：老板用**他嘴里的名字**找插件（「自动对话命名」），
 * 而插件 id 与显示名常常是英文。空 `needle` ⇒ 全部命中（不筛）。
 * **`note` 故意不参与匹配**：备注是整段散文，拿它做关键词会把「命中」变成噪声
 * （实测搜「命名」时 file-policy 因为备注里出现这个词被一起命中）。
 * 后端不给 `aliases` 时按缺省处理（不崩、不报错）。
 */
export function pluginMatches(item, needle) {
  if (!needle) return true
  if (!item || typeof item !== 'object') return false
  var hay = []
  ;['plugin_id', 'display_name', 'ref', 'module_ref', 'kind'].forEach(function (key) {
    if (typeof item[key] === 'string' && item[key]) hay.push(item[key])
  })
  if (Array.isArray(item.aliases)) {
    item.aliases.forEach(function (alias) {
      if (typeof alias === 'string' && alias) hay.push(alias)
    })
  }
  return hay.join(' ').toLowerCase().indexOf(needle) >= 0
}

/** 面板的命名空间 key（`module:<console>/<id>`）。 */
export function moduleKey(ref) {
  return 'module:' + String(ref)
}

/** 嵌套 mapping 拍平成点分键（与后端 `_flatten` 同口径：空 mapping 不产键，list 算叶子）。 */
function flattenTree(tree, prefix, out) {
  var flat = out || {}
  var head = prefix || ''
  if (!tree || typeof tree !== 'object' || Array.isArray(tree)) return flat
  Object.keys(tree).forEach(function (name) {
    var value = tree[name]
    var path = head ? head + '.' + name : name
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      if (Object.keys(value).length) flattenTree(value, path, flat)
      return
    }
    flat[path] = value
  })
  return flat
}

/** 结构化值（list / 对象）：界面给只读展示，写回交给配置文件（避免把 list 写成字符串）。 */
function isStructural(value) {
  return value !== null && typeof value === 'object'
}

/** 标量 → 输入框里的文本。 */
function textOf(value) {
  if (value === null || value === undefined) return ''
  if (isStructural(value)) return JSON.stringify(value)
  return String(value)
}

/**
 * 输入框文本 → 提交值。类型跟原值走，避免把 `3` 写成 `"3"` 改坏配置。
 * @returns {{value?: any, error?: string}}
 */
function coerceValue(text, original) {
  var raw = String(text === undefined || text === null ? '' : text)
  if (typeof original === 'number') {
    var num = Number(raw.trim())
    if (!raw.trim() || isNaN(num)) return { error: '这一项是数字：请输入数字（例如 3 / -1.5）' }
    return { value: num }
  }
  if (typeof original === 'boolean') {
    var flag = raw.trim().toLowerCase()
    if (flag === 'true' || flag === '1' || flag === 'yes' || flag === 'on') return { value: true }
    if (flag === 'false' || flag === '0' || flag === 'no' || flag === 'off') return { value: false }
    return { error: '这一项是布尔值：请输入 true 或 false' }
  }
  return { value: raw }
}

/**
 * PUT 请求体的**唯一**构造函数：掩码在这里被拦下（第二道闸门，后端还有一道 400）。
 * @returns {{body?: object, error?: string}}
 */
export function buildPutBody(key, value, expectedConfig) {
  if (typeof value === 'string' && value.trim() === MASK_PLACEHOLDER) {
    return {
      error:
        '不能把脱敏占位符 ' +
        MASK_PLACEHOLDER +
        ' 提交上去（那是界面展示用的掩码，写回等于拿掩码覆盖真密钥）。要改这一项请到配置文件改。'
    }
  }
  var body = { key: key, value: value }
  if (expectedConfig) body.expected_config = expectedConfig
  return { body: body }
}

/** 把插件 id 拼进本插件的命名空间路径（plugin id 可能含 `/`，如 `web/tavily`）。 */
function pluginPath(agent, pluginId) {
  return '/agents/' + encodeURIComponent(agent) + '/plugins/' + pluginId
}

/** 从 /modules（或 /agents/<id>/modules）取该 agent 的控制台面板，按 `section`/`id` 索引。 */
function panelFor(pluginId, modules) {
  var list = modules || []
  for (var i = 0; i < list.length; i += 1) {
    var item = list[i] || {}
    if (item.section === pluginId || (!item.section && item.id === pluginId)) return item
  }
  return null
}

/**
 * 一行的「关联面板」文案：先认后端 catalog 字段（`module_ref` / `module` / `has_dashboard_module`），
 * 缺了才按 `/agents/<id>/modules` 的实测结果推；都拿不到就明说「面板信息未读到」——
 * 绝不猜成「有面板」，也绝不把「没有面板」当成「插件不存在」。
 * @returns {{text: string, has: boolean}}
 */
export function panelLabel(item, modules) {
  var source = item || {}
  if (typeof source.module_ref === 'string' && source.module_ref) {
    return { text: source.module_ref, has: true }
  }
  if (typeof source.module === 'string' && source.module) {
    return { text: moduleKey(source.module), has: true }
  }
  if (source.has_dashboard_module === false) {
    return { text: '无控制台模块', has: false }
  }
  var panel = modules ? panelFor(String(source.plugin_id || ''), modules) : null
  if (panel) return { text: moduleKey('hermes-console/' + (panel.section || panel.id)), has: true }
  if (!modules) return { text: '面板信息未读到', has: false }
  return { text: '无控制台模块', has: false }
}

/** 三态行的动作：只有 enabled/disabled 有动作，unavailable 恒 null。 */
export function rowAction(state) {
  if (state === 'enabled') return { next: false, label: '关闭' }
  if (state === 'disabled') return { next: true, label: '开启' }
  return null
}

/**
 * 点击是否落在行内**自己的**交互控件上（按钮 / 输入框 / 链接 / 标签 / 开关）。
 *
 * 为什么要有这个判断：整行现在可点（点行 = 选中该插件），但行里还有「关闭 / 开启 / 配置」
 * 和配置输入框。若行点击不排除它们，一次「关闭」会同时触发「选中」—— 选中态与开关动作
 * 互相污染，而且老板会看到右栏莫名其妙换了一个插件。
 *
 * 用 `closest` 而不是比较 `event.target === button`：按钮里还有文字节点 / 图标 span，
 * 事件目标常常是子节点。
 */
export function isInteractiveTarget(target) {
  if (!target || typeof target.closest !== 'function') return false
  return Boolean(
    target.closest('button, input, textarea, select, a, label, [role="switch"], [contenteditable="true"]')
  )
}

/** 一行是不是「有可改的键」：后端 has_settings 为真才算。 */
export function rowConfigurable(item) {
  return Boolean(item && typeof item === 'object' && item.has_settings)
}

/** rest.js 返回 `{ok, data:<HTTP body>}`，而后端 body 又是 `{ok, data:{…}}`：取里层（口径同 shell.js）。 */
function unwrap(payload) {
  if (payload && typeof payload === 'object' && payload.data && typeof payload.data === 'object') return payload.data
  return payload && typeof payload === 'object' ? payload : null
}

/** 小工具：给「按 key 记账」的 map 做函数式写（不改原对象，React 才能看到变化）。 */
function withEntry(map, key, value) {
  var next = {}
  Object.keys(map).forEach(function (name) {
    if (name !== key) next[name] = map[name]
  })
  if (value !== undefined) next[key] = value
  return next
}

/**
 * 页面内的 agent 列表（**本页自己的一份**，不依赖外壳顶部的作用域选择器）。
 *
 * 为什么页内还要一份：老板的诉求是「点开设置先看到框架和 agent 列表，选谁才读谁」。
 * 顶部作用域是**全局**的（切了会影响所有分区），而设置页要看哪个 agent 的插件是一页之内的
 * 局部决定 —— 混在一起会出现「我只是想看看 code-worker 的配置，结果整页作用域都变了」。
 *
 * 数据用 `useQuery('agents', …)`：与 `lib/scope.js` 同一个缓存键，所以外壳已经拉过就直接命中，
 * 不会多一次请求；外壳还没拉到时这里自己拉一次（并发去重由 query.js 负责）。
 */
export function useAgentList() {
  var query = useQuery('agents', function () {
    return get('/agents')
  }, { staleMs: 60000 })
  var payload = query.status === 'ok' ? unwrap(query.data) : null
  var rows = []
  if (payload && Array.isArray(payload.agents)) {
    rows = payload.agents.filter(function (item) {
      return item && typeof item.id === 'string' && item.id
    })
  } else if (payload && Array.isArray(payload.items)) {
    // 老形状兜底（后端改用 items 时不至于把列表变成空）
    rows = payload.items.filter(function (item) {
      return item && typeof item.id === 'string' && item.id
    })
  }
  return {
    status: query.status,
    error: query.status === 'error' ? query.error : '',
    rows: rows,
    current: payload && typeof payload.current === 'string' ? payload.current : ''
  }
}

export function SettingsSection(props) {
  // 顶部作用域只作**默认参考**（首次没选过时用来解释“当前作用域是谁”），选中谁由页内决定。
  var scopeAgent = props.agent || ''
  var ready = props.ready !== false
  var agentList = useAgentList()

  // 「先框架、后按需加载」：第一帧只画框架 + agent 列表，插件配置的请求放到下一帧再发。
  // 这样即便后端慢，用户先看到的是**可点的界面**，而不是一个占满屏幕的骨架/错误。
  var paintState = useState(false)
  var painted = paintState[0]
  useEffect(function () {
    var raf =
      typeof requestAnimationFrame === 'function'
        ? requestAnimationFrame
        : function (fn) {
            return setTimeout(fn, 0)
          }
    var cancel =
      typeof cancelAnimationFrame === 'function'
        ? cancelAnimationFrame
        : function (handle) {
            clearTimeout(handle)
          }
    var handle = raf(function () {
      paintState[1](true)
    })
    return function () {
      cancel(handle)
    }
  }, [])

  // 页内选中的 agent。三态语义（**关键**）：
  //   * 偏好里**没有**这个键 ⇒ 还没在本页选过 ⇒ 沿用顶部作用域那一个（用户预期就是"当前是谁看谁"，
  //     而且只读这一个，不是整批）；
  //   * 偏好里是空串 ⇒ 用户明确点了「不选」⇒ 引导态（一个请求都不发）；
  //   * 偏好里是某个 id ⇒ 本页只看它。
  var pickState = useState(function () {
    var stored = readPref(PICK_KEY, null)
    return stored === null || stored === undefined ? null : String(stored)
  })
  var pickedPref = pickState[0]
  function pickAgent(id) {
    var next = String(id || '')
    pickState[1](next)
    writePref(PICK_KEY, next)
  }

  var agent = pickedPref === null ? scopeAgent : pickedPref
  // 存过的 agent 可能已被删（profile 被移除）：不静默换成别的，而是落到引导态并说明。
  var agentMissing = Boolean(agent) && agentList.status === 'ok' && agentList.rows.length > 0 &&
    !agentList.rows.some(function (row) { return row.id === agent })

  // 没有 agent 就**一个插件请求都不发**：`/agents/` + 空 id 会变成 400/404，界面就"未加载先报错"了。
  var wantPlugins = ready && painted && Boolean(agent) && !agentMissing

  // 耗时：从发请求到拿到响应（毫秒）。给老板一个「这次到底多慢」的实数，而不是只说"已加载"。
  // 按 agent 分开记（同名 agent 的刷新会覆盖自己的那格，不会串到别人头上）。
  var timingState = useState({})
  var timings = timingState[0]
  var startedAt = useRefSafe({})

  function recordTiming(name, started) {
    var elapsed = Math.max(0, nowMs() - started)
    timingState[1](function (prev) {
      var next = {}
      Object.keys(prev).forEach(function (key) {
        next[key] = prev[key]
      })
      next[name] = Math.round(elapsed)
      return next
    })
  }

  var pluginsQuery = useQuery(
    'settings-plugins|' + agent,
    function () {
      var name = agent
      startedAt.current[name] = nowMs()
      return get('/agents/' + encodeURIComponent(name) + '/plugins').then(function (result) {
        recordTiming(name, startedAt.current[name])
        return result
      })
    },
    { staleMs: 30000, enabled: wantPlugins }
  )
  var modulesQuery = useQuery(
    'settings-modules|' + agent,
    function () {
      return get('/agents/' + encodeURIComponent(agent) + '/modules')
    },
    { staleMs: 60000, enabled: wantPlugins }
  )

  var listData = pluginsQuery.status === 'ok' ? unwrap(pluginsQuery.data) : null
  var items = listData && Array.isArray(listData.plugins) ? listData.plugins : []
  var counts = listData && listData.counts && typeof listData.counts === 'object' ? listData.counts : {}
  var moduleData = modulesQuery.status === 'ok' ? unwrap(modulesQuery.data) : null
  var modules = moduleData && Array.isArray(moduleData.modules) ? moduleData.modules : null
  var digest = listData && typeof listData.config_digest === 'string' ? listData.config_digest : ''

  var selectedState = useState('')
  var wanted = selectedState[0]
  var editable = items.filter(function (item) {
    return rowConfigurable(item)
  })
  // 选中项的解析（**不许把不可配置的插件当成「没选中」**）：
  //   * 用户点过某一行 ⇒ 就是它（哪怕 has_settings=false，右栏也要给出「为什么没有可改的键」）；
  //   * 没点过 / 点过的插件已从清单消失 ⇒ 落到第一个可配置插件（首屏不至于空白）；
  //   * 一个可配置的都没有 ⇒ 空。
  var wantedItem = null
  if (wanted) {
    for (var wi = 0; wi < items.length; wi += 1) {
      if (items[wi] && items[wi].plugin_id === wanted) {
        wantedItem = items[wi]
        break
      }
    }
  }
  var selectedItem = wantedItem || editable[0] || null
  var selected = selectedItem && typeof selectedItem.plugin_id === 'string' ? selectedItem.plugin_id : ''
  // 只有 has_settings=true 才发 GET .../settings：46 个 has_settings=false 的插件后端会返回
  // 空结构（实测 settings={} / writable_keys=[]），发这一次请求既没有东西可看、又白等一个往返。
  var selectedHasSettings = rowConfigurable(selectedItem)

  function selectPlugin(id) {
    selectedState[1](String(id || ''))
  }

  var settingsQuery = useQuery(
    'settings-plugin|' + agent + '|' + selected,
    function () {
      return get(pluginPath(agent, selected) + '/settings')
    },
    { staleMs: 30000, enabled: wantPlugins && Boolean(selected) && selectedHasSettings }
  )

  // 输入草稿 / 每项错误 / 冲突提示 / 进行中：key 里带 agent+plugin，
  // 所以切 agent 后**读不到上一个 agent 的草稿**（不是先显示旧值再跳）。
  var ctx = agent + '|' + selected
  var draftsState = useState({})
  var drafts = draftsState[0]
  var errorsState = useState({})
  var errors = errorsState[0]
  var conflictState = useState({})
  var conflicts = conflictState[0]
  var pendingState = useState({})
  var pending = pendingState[0]
  var conflict = conflicts[ctx] || ''
  var pluginsError = pluginsQuery.status === 'error' ? pluginsQuery.error : ''

  // ---- 找插件：搜索框 + 「只看可配置」----
  // 为什么有这一段：本 agent 的清单动辄 50+ 行（真实数据 default = 51 行），而且插件 id /
  // 显示名常常是英文（老板说的是「自动对话命名」，catalog 里原来写的是 Hermes Auto Titler），
  // 于是「插件明明在清单里、老板却找不到」——这不是插件缺失，是**列表翻不到**。
  // 搜索只过滤**本页已经拿到的清单**（不发新请求、不改三态、不猜含义）。
  var filterState = useState('')
  var query = filterState[0]
  var onlyConfigState = useState(false)
  var onlyConfig = onlyConfigState[0]
  var needle = String(query || '').trim().toLowerCase()
  var visibleItems = items.filter(function (item) {
    if (!item || typeof item !== 'object') return false
    if (onlyConfig && !item.has_settings) return false
    return pluginMatches(item, needle)
  })
  var filtering = Boolean(needle) || onlyConfig

  function reloadAll() {
    conflictState[1](withEntry(conflicts, ctx, ''))
    if (pluginsQuery.reload) pluginsQuery.reload()
    if (settingsQuery.reload) settingsQuery.reload()
  }

  /** 启停一个插件。**不翻转界面状态**：成功后重新拉清单，失败把后端原话挂到这一行上。 */
  function togglePlugin(item, next) {
    var action = rowAction(item.state)
    if (!action) return
    var booking = 'toggle#' + item.plugin_id
    pendingState[1](withEntry(pending, booking, true))
    var body = { enabled: next }
    if (digest) body.expected_config = digest
    send(pluginPath(agent, item.plugin_id), body, { method: 'PUT' }).then(function (result) {
      pendingState[1](withEntry(pending, booking, ''))
      if (result.ok) {
        errorsState[1](withEntry(errors, booking, ''))
        refresh('settings-plugins|' + agent)
        refresh('settings-plugin|' + agent + '|' + selected)
        return
      }
      if (result.code === '409') {
        // 乐观锁：不静默重试，也不翻转界面状态
        conflictState[1](withEntry(conflicts, ctx, result.message))
        return
      }
      if (result.code === '404') {
        // 后端还没有启停端点（本卡不许改 dashboard/）：这不是用户操作问题，要说清楚。
        errorsState[1](
          withEntry(
            errors,
            booking,
            '后端还没有插件启停端点，所以这次切换没有生效（本页不发假请求，也不会假装成功）。' +
              '后端原话：' +
              result.message
          )
        )
        return
      }
      errorsState[1](withEntry(errors, booking, result.message))
    })
  }

  /** 写一项设置：body 只能经 buildPutBody 生成（掩码在那里被拦下）。token = 该 settings GET 回来的乐观锁令牌。 */
  function writeSetting(key, value, mode, token) {
    var booking = key + '@' + mode
    var built = buildPutBody(key, value, token || digest)
    if (built.error) {
      errorsState[1](withEntry(errors, booking, built.error))
      return
    }
    pendingState[1](withEntry(pending, key, true))
    send(pluginPath(agent, selected) + '/settings', built.body, { method: 'PUT' }).then(function (result) {
      pendingState[1](withEntry(pending, key, ''))
      if (result.ok) {
        draftsState[1](withEntry(drafts, ctx + '#' + key, undefined))
        errorsState[1](withEntry(errors, booking, ''))
        refresh('settings-plugin|' + agent + '|' + selected)
        refresh('settings-plugins|' + agent)
        return
      }
      if (result.code === '409') {
        conflictState[1](withEntry(conflicts, ctx, result.message))
        return
      }
      errorsState[1](withEntry(errors, booking, result.message))
    })
  }

  // 不再整页 return 骨架/错误：框架（标题 + agent 列表 + 两列）先出，状态只落在**对应那一列**里。
  // 「未加载」只有骨架，「没读到」才是错误，两者不许混为一谈。
  var pluginsLoading = pluginsQuery.status === 'loading'
  var settingsLoading = settingsQuery.status === 'loading'

  var countsRow = jsxs(
    'div',
    {
      className: 'hc-set__counts',
      children: Object.keys(STATE_TEXT).map(function (state) {
        var value = typeof counts[state] === 'number' ? counts[state] : 0
        return jsxs(
          'span',
          {
            className: 'hc-chip hc-set__count hc-set__count--' + state,
            'data-count-state': state,
            children: [STATE_TEXT[state], ' ', String(value)]
          },
          state
        )
      })
    },
    'counts'
  )

  var listHead = jsxs(
    'div',
    {
      className: 'hc-set__listhead',
      children: [
      jsx('span', { className: 'hc-set__listtitle', children: '插件' }, 'title'),
      jsx(
        'span',
        {
          className: 'hc-chip',
          title: '本页的插件 id 都是 plugin:<id>；控制台面板是 module:hermes-console/<id>，两者可能同名但不是一个东西',
          children: '命名空间 plugin:*'
        },
        'ns'
      ),
      // 耗时给实数：老板问的是「到底多慢」，所以把这次请求的毫秒数直接写在标题旁边。
      agent && typeof timings[agent] === 'number'
        ? jsx(
            'span',
            {
              className: 'hc-chip hc-set__elapsed',
              'data-elapsed-agent': agent,
              'data-elapsed-ms': String(timings[agent]),
              title: '这一次 GET /agents/' + agent + '/plugins 的往返耗时（浏览器侧计时，含后端处理）',
              children: '读回 ' + timings[agent] + ' ms'
            },
            'elapsed'
          )
        : null,
      agent && pluginsQuery.busy
        ? jsx('span', { className: 'hc-chip', 'data-refreshing': '1', children: '刷新中' }, 'busy')
        : null
      ]
    },
    'listhead'
  )

  var rows = visibleItems.map(function (item) {
    var pluginId = typeof item.plugin_id === 'string' ? item.plugin_id : ''
    var state = item.state
    var text = STATE_TEXT[state] || String(state || '状态未知')
    var action = rowAction(state)
    var pendingKey = 'toggle#' + pluginId
    var isSelected = pluginId === selected
    var panel = panelLabel(item, modules)
    var chips = [
      jsx(
        'span',
        {
          className: 'hc-set__kind',
          title: '这是 Hermes 插件本体（plugin:<id>），不是控制台里的同名分区',
          children: typeof item.ref === 'string' && item.ref ? item.ref : pluginKey(pluginId)
        },
        'kind'
      ),
      jsx(
        'span',
        {
          className: 'hc-chip' + (panel.has ? '' : ' hc-chip--off'),
          'data-panel-kind': panel.has ? 'module' : 'none',
          title: '该插件关联的控制台面板（module:<console>/<id>）；没有面板的插件照样能在这里看状态 / 改配置',
          children: panel.text
        },
        'panel'
      )
    ]
    if (typeof item.version === 'string' && item.version) {
      chips.push(jsx('span', { className: 'hc-chip', children: '版本 ' + item.version }, 'version'))
    }
    // catalog 的 `origin`（自制 / 第三方）与三态的 `source`（enabled_list / disabled_list / absent）
    // 是两回事：前者说插件出处，后者说这份名单。两个都有就都显示，别互相盖掉。
    if (typeof item.origin === 'string' && item.origin) {
      chips.push(jsx('span', { className: 'hc-chip', children: '来源 ' + item.origin }, 'origin'))
    } else if (item.in_catalog === false) {
      chips.push(jsx('span', { className: 'hc-chip hc-chip--off', children: '不在自制清单' }, 'origin'))
    }
    if (typeof item.kind === 'string' && item.kind) {
      chips.push(jsx('span', { className: 'hc-chip', children: item.kind }, 'kindval'))
    }

    var controls = []
    if (action) {
      controls.push(
        jsx(
          'button',
          {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-toggle-action': action.next ? 'enable' : 'disable',
            'data-toggle-plugin': pluginId,
            disabled: Boolean(pending[pendingKey]),
            title: STATE_HINT[state] || '',
            onClick: function (event) {
              // 行本身可点（点行 = 选中）：启停按钮绝不能顺带把选中态改掉。
              if (event && typeof event.stopPropagation === 'function') event.stopPropagation()
              togglePlugin(item, action.next)
            },
            children: pending[pendingKey] ? '处理中' : action.label
          },
          'toggle'
        )
      )
    } else {
      // 三态之外的 state：不猜含义，也不给动作（给一个点了会假装成功的按钮更糟）
      controls.push(
        jsx(
          'span',
          { className: 'hc-set__warn', 'data-toggle-blocked': pluginId, children: '无可用开关' },
          'blocked'
        )
      )
    }
    if (item.has_settings) {
      controls.push(
        jsx(
          'button',
          {
            type: 'button',
            className: 'hc-btn hc-btn--tiny' + (isSelected ? ' hc-btn--done' : ''),
            'data-select-plugin': pluginId,
            onClick: function (event) {
              // 同上：这个按钮只负责「选中」，不要冒泡再触发一次行点击。
              if (event && typeof event.stopPropagation === 'function') event.stopPropagation()
              selectPlugin(pluginId)
            },
            children: isSelected ? '正在看配置' : '配置'
          },
          'config'
        )
      )
    } else {
      // has_settings=false：**仍然可以选中**（整行可点），但先说清楚为什么没有配置入口。
      // 不给一个点了没反应的「配置」按钮，也不假装它可配置。
      controls.push(
        jsx(
          'span',
          {
            className: 'hc-chip hc-chip--off',
            'data-no-settings': pluginId,
            title:
              '后端 has_settings=false：plugins.entries.' +
              pluginId +
              '.settings 里没有写过值，插件包也没有声明 config_schema。点这一行可以在右栏看到这条说明。',
            children: '无可配置键'
          },
          'nosettings'
        )
      )
    }

    return jsxs(
      'li',
      {
        className:
          'hc-set__row hc-set__row--clickable hc-set__row--' +
          (state || 'unknown') +
          (isSelected ? ' hc-set__row--selected' : ''),
        'data-plugin-id': pluginId,
        'data-plugin-state': state,
        'data-plugin-key': pluginKey(pluginId),
        'data-plugin-configurable': item.has_settings ? '1' : '0',
        // 整行可点：点行 = 选中该插件（右栏看它的配置，或看「为什么没有可改的键」）。
        // 行内按钮 / 输入框自己处理点击（stopPropagation + isInteractiveTarget 双保险），
        // 所以「关闭 / 开启 / 配置 / 改值」不会顺带改选中态。
        role: 'button',
        tabIndex: 0,
        'aria-pressed': isSelected ? 'true' : 'false',
        'aria-label':
          (typeof item.display_name === 'string' && item.display_name ? item.display_name + '（' + pluginId + '）' : pluginId) +
          '：' +
          text +
          (item.has_settings ? '，可配置' : '，没有可配置的键'),
        title: item.has_settings
          ? '点这一行：在右栏看 / 改 ' + pluginId + ' 的配置'
          : '点这一行：右栏说明为什么 ' + pluginId + ' 没有可改的键',
        onClick: function (event) {
          if (isInteractiveTarget(event && event.target)) return
          selectPlugin(pluginId)
        },
        onKeyDown: function (event) {
          if (!event) return
          var key = event.key
          if (key !== 'Enter' && key !== ' ' && key !== 'Spacebar') return
          if (isInteractiveTarget(event.target)) return
          event.preventDefault()
          selectPlugin(pluginId)
        },
        children: [
          jsxs(
            'div',
            {
              className: 'hc-set__row-head',
              children: [
                jsx('span', {
                  className: 'hc-set__name',
                  children:
                    typeof item.display_name === 'string' && item.display_name && item.display_name !== pluginId
                      ? [
                          jsx('span', { className: 'hc-set__display', children: item.display_name }, 'display'),
                          jsx('span', { className: 'hc-set__id', children: pluginId }, 'id')
                        ]
                      : jsx('span', { className: 'hc-set__id', children: pluginId }, 'id')
                }, 'name'),
                jsx('span', {
                  className: 'hc-set__state hc-set__state--' + (state || 'unknown'),
                  'data-state-text': text,
                  children: text
                }, 'state')
              ]
            },
            'head'
          ),
          jsx('div', { className: 'hc-set__row-chips', children: chips }, 'chips'),
          jsx(
            'div',
            {
              className: 'hc-set__row-hint',
              children:
                STATE_HINT[state] ||
                '后端给了一个三态之外的 state（' + String(state) + '）：本页不猜它的含义'
            },
            'hint'
          ),
          typeof item.note === 'string' && item.note
            ? jsx('div', { className: 'hc-set__row-note', title: item.note, children: item.note }, 'note')
            : null,
          controls.length ? jsx('div', { className: 'hc-set__row-actions', children: controls }, 'actions') : null,
          errors[pendingKey]
            ? jsx(ErrorState, { title: '切换没成功', message: errors[pendingKey] }, 'error')
            : null
        ]
      },
      pluginKey(pluginId) || 'plugin'
    )
  })

  // 搜索条：只过滤**本页已拿到的清单**（不发请求）。三态计数照旧，这里只报命中数，
  // 免得「命中 1 / 共 51」被误读成「这个 agent 只有 1 个插件」。
  var filterBar = jsxs(
    'div',
    {
      className: 'hc-set__filter',
      'data-plugin-filter-bar': '1',
      style: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--s2)', minWidth: 0 },
      children: [
        jsx(
        'input',
        {
          className: 'hc-set__input',
          type: 'search',
          value: query,
          'data-plugin-filter': '1',
          placeholder: '搜插件：id / 名字 / 别名（例：命名、titler）',
          'aria-label': '按插件 id、名字或别名过滤',
          style: { flex: '1 1 220px' },
          onChange: function (event) {
            filterState[1](event.target.value)
          }
        },
        'input'
      ),
      jsx(
        'button',
        {
          type: 'button',
          className: 'hc-btn hc-btn--tiny' + (onlyConfig ? ' hc-btn--done' : ''),
          'data-only-config': onlyConfig ? '1' : '0',
          title: '只列出后端 has_settings=true 的插件（只有这些带「配置」按钮，能改值）',
          onClick: function () {
            onlyConfigState[1](!onlyConfig)
          },
          children: '只看可配置 ' + String(editable.length)
        },
        'onlyconfig'
      ),
      jsx(
        'span',
        {
          className: 'hc-chip',
          'data-filter-count': String(visibleItems.length),
          'data-filter-total': String(items.length),
          children: '命中 ' + String(visibleItems.length) + ' / 共 ' + String(items.length)
        },
        'count'
      ),
      filtering
        ? jsx(
            'button',
            {
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              'data-filter-clear': '1',
              onClick: function () {
                filterState[1]('')
                onlyConfigState[1](false)
              },
              children: '清空'
            },
            'clear'
          )
        : null
      ]
    },
    'filter'
  )

  var listCol = jsxs(
    'div',
    {
      className: 'hc-set__col hc-set__col--list',
      'data-list-state': !agent ? 'no-agent' : pluginsLoading ? 'loading' : pluginsError ? 'error' : 'ready',
      children: [
      listHead,
      countsRow,
      agent && !pluginsLoading && !pluginsError ? filterBar : null,
      // 未选中 agent：**骨架 + 引导**（不是空列表、也不是 ErrorState）。
      // 骨架占住"插件行"的形状，让老板一眼看出"这里马上会有一张插件表，只是还没选 agent"。
      !agent
        ? jsxs(
            'div',
            {
              className: 'hc-set__guide',
              'data-guide-state': agentMissing ? 'agent-missing' : 'no-agent',
              style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
              children: [
                jsx(Skeleton, { lines: 4, label: '选中 Agent 后这里会出现它的插件清单' }, 'skel'),
                jsx(
                  Empty,
                  {
                    title: agentMissing
                      ? '之前选中的 Agent「' + agent + '」已经不在了'
                      : '选一个 Agent，才读它的插件配置',
                    hint: agentMissing
                      ? '上面那一排里没有这个 id（profile 可能被删了）。选另一个 Agent 继续 —— 本页不会自动替你换一个，免得你以为看的是原来那个。'
                      : '本页只读你选中的那一个 Agent 的插件与配置，不再整批预取所有 Agent（那是之前“设置加载非常慢”的主因）。没选之前一个插件请求都不会发。'
                  },
                  'guide'
                )
              ]
            },
            'guide'
          )
        : pluginsLoading
        ? jsx(Skeleton, { lines: 6, label: '正在读取 ' + agent + ' 的插件配置' }, 'skel')
        : pluginsError
        ? jsx(
            ErrorState,
            {
              title: '插件列表没读到',
              message: pluginsError,
              onRetry: pluginsQuery.reload
            },
            'error'
          )
        : listData && typeof listData.path === 'string' && listData.exists === false
        ? jsx(
            'div',
            { className: 'hc-set__note', children: '该 agent 没有自己的 config.yaml（' + listData.path + '）：清单为空是正常的，写配置会被后端拒绝。' },
            'nocfg'
          )
        : null,
      agent && !pluginsLoading && !pluginsError
        ? visibleItems.length
          ? jsx('ul', { className: 'hc-set__list', children: rows }, 'list')
          : items.length
          ? jsx(
              Empty,
              {
                title: '没有插件命中这个筛选',
                hint:
                  '本 agent 的清单里一共 ' +
                  String(items.length) +
                  ' 个插件，但当前筛选（' +
                  (needle ? '关键词「' + needle + '」' : '只看可配置') +
                  '）一个都没命中。三态计数没变 —— 这里只是过滤了显示，没有隐藏或改动任何插件。',
                action: {
                  label: '清空筛选',
                  onClick: function () {
                    filterState[1]('')
                    onlyConfigState[1](false)
                  }
                }
              },
              'nomatch'
            )
          : jsx(
              Empty,
              {
                title: '这个 agent 的配置里没有插件',
                hint:
                  '该 agent 的 config.yaml 里 plugins.entries / enabled / disabled 都是空的，所以这里没有可看的东西。' +
                  '先在配置文件里写一个插件条目，或切到 default agent 看全局清单。',
                action: { label: '重新读取', onClick: pluginsQuery.reload }
              },
              'empty'
            )
        : null,
      agent && !pluginsLoading && !pluginsError && listData && Array.isArray(listData.skipped) && listData.skipped.length
        ? jsx(
            'div',
            {
              className: 'hc-set__note',
              children:
                '后端跳过了 ' +
                String(listData.skipped.length) +
                ' 个无法用官方文法表示的插件 id：' +
                listData.skipped
                  .map(function (entry) {
                    return String(entry.plugin_id) + '（' + String(entry.reason) + '）'
                  })
                  .join('；')
            },
            'skipped'
          )
        : null
      ]
    },
    'listcol'
  )

  var detail = null
  if (!agent) {
    detail = jsx(Empty, {
      title: '选中 Agent 后这里显示插件配置',
      hint: '左列会列出该 Agent 的插件三态；带「配置」按钮的插件才可改值。未选中之前不发任何插件配置请求。'
    })
  } else if (pluginsLoading) {
    detail = jsx(Empty, {
      title: '等插件清单读回来',
      hint: '插件配置是按插件单独读的：先拿到清单，再读选中那一个的配置（不整批拉取）。'
    })
  } else if (!selected) {
    detail = jsx(Empty, {
      title: '还没有可改配置的插件',
      hint: '左侧列表里带「配置」按钮的插件才能改值（后端 has_settings 为 true）。当前这个 agent 一个都没有。'
    })
  } else if (!selectedHasSettings) {
    // 点了一个 has_settings=false 的插件：**必须给出明确原因**，不许什么都不发生。
    // 这一段不发 GET .../settings：后端对这类插件返回的是空结构（实测 settings={} /
    // writable_keys=[]），再等一个往返只会让「点了没反应」更久。
    detail = jsxs('div', {
      className: 'hc-set__col hc-set__col--detail',
      'data-noconfig': selected,
      children: [
        jsxs(
          'div',
          {
            className: 'hc-set__detailhead',
            children: [
              jsx('span', { className: 'hc-set__listtitle', children: pluginKey(selected) }, 'title'),
              jsx(
                'span',
                {
                  className: 'hc-chip hc-chip--off',
                  'data-noconfig-chip': '1',
                  children: '没有可配置的键'
                },
                'chip'
              ),
              jsx(
                'span',
                {
                  className: 'hc-chip',
                  'data-noconfig-state': selectedItem && selectedItem.state ? String(selectedItem.state) : 'unknown',
                  children:
                    '三态：' +
                    (STATE_TEXT[selectedItem && selectedItem.state] || String((selectedItem && selectedItem.state) || '状态未知'))
                },
                'state'
              )
            ]
          },
          'head'
        ),
        jsx(
          'div',
          {
            className: 'hc-set__note',
            'data-noconfig-reason': '1',
            children:
              '后端 has_settings=false：plugins.entries.' +
              selected +
              '.settings 里没有写过值，插件包也没有声明 config_schema ⇒ 没有可改的键。' +
              '这不是「界面没加载」—— 本 agent 的插件清单已经读到了（共 ' +
              String(items.length) +
              ' 个），只是这个插件确实没有配置项。'
          },
          'reason'
        ),
        jsx('div', {
          className: 'hc-set__note',
          children:
            (STATE_HINT[selectedItem && selectedItem.state] || '') +
            (panelLabel(selectedItem || {}, modules).has
              ? ' 它有关联的控制台面板：' + panelLabel(selectedItem || {}, modules).text + '。'
              : ' 它没有关联的控制台面板。')
        }, 'hint'),
        jsx(Empty, {
          title: '这个插件没有可配置的键',
          hint:
            '三态（启用 / 禁用）仍然可以在左侧这一行操作，也可以点「' +
            pluginKey(selected) +
            '」以外的其它行看别的插件。要让这里出现可改的键，得给插件包加 config_schema，或先在配置文件里写入 plugins.entries.' +
            selected +
            '.settings —— 那属于改插件/改配置，本页不做。',
          action: editable.length
            ? {
                label: '看第一个可配置的插件（' + pluginKey(editable[0].plugin_id) + '）',
                onClick: function () {
                  selectPlugin(editable[0].plugin_id)
                }
              }
            : null
        }, 'empty')
      ]
    })
  } else if (settingsLoading) {
    detail = jsx(Skeleton, { lines: 5, label: '正在读取 ' + selected + ' 的配置' })
  } else if (settingsQuery.status === 'error') {
    detail = jsx(ErrorState, {
      title: '这个插件的配置没读到',
      message: settingsQuery.error,
      onRetry: settingsQuery.reload
    })
  } else {
    var view = settingsQuery.data && typeof settingsQuery.data === 'object' ? unwrap(settingsQuery.data) : null
    view = view || {}
    var flat = flattenTree(view.settings || {}, '', {})
    var sources = view.sources && typeof view.sources === 'object' ? view.sources : {}
    var maskedKeys = Array.isArray(view.masked_keys) ? view.masked_keys : []
    var readonlyKeys = Array.isArray(view.readonly_keys) ? view.readonly_keys : []
    var writableKeys = Array.isArray(view.writable_keys) ? view.writable_keys : []
    var keys = Object.keys(flat).sort()
    var expected = typeof view.expected_config === 'string' ? view.expected_config : digest

    var fields = keys.map(function (key) {
      var value = flat[key]
      var masked = maskedKeys.indexOf(key) !== -1
      var readonly = readonlyKeys.indexOf(key) !== -1
      var structural = isStructural(value)
      var writable = !masked && !readonly && !structural && writableKeys.indexOf(key) !== -1
      var draftKey = ctx + '#' + key
      var shown = Object.prototype.hasOwnProperty.call(drafts, draftKey) ? drafts[draftKey] : textOf(value)
      var busy = Boolean(pending[key])
      var savedFor = key + '@save'
      var resetFor = key + '@reset'

      var head = [
        jsx('span', { className: 'hc-set__key', children: key }, 'key'),
        jsx(
          'span',
          {
            className: 'hc-set__src hc-set__src--' + (sources[key] || 'unknown'),
            'data-source': sources[key] || 'unknown',
            title: '这一项的值来自哪一层：模块默认 < 全局 < 本 Agent 覆盖',
            children: sourceLabel(sources[key])
          },
          'src'
        )
      ]
      if (masked) {
        head.push(jsx('span', { className: 'hc-set__ro', children: '已脱敏，请到配置文件修改' }, 'ro'))
      }
      if (readonly) {
        head.push(jsx('span', { className: 'hc-set__ro', children: '只读（写入会被后端拒绝）' }, 'ro2'))
      }
      if (structural) {
        head.push(
          jsx('span', { className: 'hc-set__ro', children: '结构化值（列表 / 对象）：请在配置文件里改' }, 'ro3')
        )
      }
      if (!writable && !masked && !readonly && !structural) {
        head.push(jsx('span', { className: 'hc-set__ro', children: '不在可写键列表里' }, 'ro4'))
      }

      // 不可写时**必须说清原因**（不是「灰一下就算了」）：同一个原因同时进可见标签、
      // 输入框 title、data-locked-reason，探针与人都能核对。
      var lockedReason = ''
      if (masked) lockedReason = '已脱敏：界面只读，改值请到配置文件'
      else if (readonly) lockedReason = '只读：写入会被后端拒绝'
      else if (structural) lockedReason = '结构化值（列表 / 对象）：请在配置文件里改'
      else if (!writable) lockedReason = '不在可写键列表里：后端没有把这个键算作可写'

      return jsxs(
        'div',
        {
          className: 'hc-set__field' + (writable ? '' : ' hc-set__field--locked'),
          'data-setting-key': key,
          'data-source': sources[key] || 'unknown',
          'data-masked': masked ? '1' : '0',
          children: [
            jsx('div', { className: 'hc-set__field-head', children: head }, 'head'),
            jsx('div', {
              className: 'hc-set__field-body',
              children: [
                jsx('input', {
                  className: 'hc-set__input',
                  type: 'text',
                  value: shown,
                  // 可写 ⇒ 必须真的能编辑；不可写 ⇒ 禁用 + 说清原因（lockedReason）。
                  disabled: !writable,
                  readOnly: !writable,
                  title: writable ? '这一项可写：改完点「保存」' : lockedReason,
                  'data-locked-reason': lockedReason,
                  'aria-disabled': writable ? 'false' : 'true',
                  'aria-label': key,
                  'data-field-input': key,
                  placeholder: masked ? MASK_PLACEHOLDER : '',
                  onChange: function (event) {
                    draftsState[1](withEntry(drafts, draftKey, event.target.value))
                  }
                }, 'input'),
                writable
                  ? jsx(
                      'button',
                      {
                        type: 'button',
                        className: 'hc-btn hc-btn--tiny',
                        'data-field-save': key,
                        disabled: busy,
                        onClick: function () {
                          var coerced = coerceValue(shown, value)
                          if (coerced.error) {
                            errorsState[1](withEntry(errors, savedFor, coerced.error))
                            return
                          }
                          writeSetting(key, coerced.value, 'save', expected)
                        },
                        children: busy ? '写入中' : '保存'
                      },
                      'save'
                    )
                  : null,
                writable
                  ? jsx(
                      'button',
                      {
                        type: 'button',
                        className: 'hc-btn hc-btn--tiny',
                        'data-field-reset': key,
                        disabled: busy,
                        title: '写一个 null：按合并规则回落到全局 / 模块默认层（不是删键）',
                        onClick: function () {
                          writeSetting(key, null, 'reset', expected)
                        },
                        children: '恢复为全局值'
                      },
                      'reset'
                    )
                  : null
              ]
            }, 'body'),
            errors[savedFor]
              ? jsx(ErrorState, { title: '这一项没写进去', message: errors[savedFor] }, 'err-save')
              : null,
            errors[resetFor]
              ? jsx(ErrorState, { title: '恢复默认没成功', message: errors[resetFor] }, 'err-reset')
              : null
          ]
        },
        key
      )
    })

    detail = jsxs('div', {
      className: 'hc-set__col hc-set__col--detail',
      children: [
        jsxs(
          'div',
          {
            className: 'hc-set__detailhead',
            children: [
              jsx('span', { className: 'hc-set__listtitle', children: pluginKey(selected) + ' 的配置' }, 'title'),
              jsx(
                'span',
                {
                  className: 'hc-chip',
                  title: '写入用 PUT /agents/<agent>/plugins/<plugin>/settings，带 expected_config 乐观锁',
                  children: keys.length ? keys.length + ' 项' : '没有可显示的项'
                },
                'count'
              )
            ]
          },
          'head'
        ),
        conflict
          ? jsxs(
              'div',
              {
                className: 'hc-set__conflict',
                role: 'alert',
                'data-conflict': '1',
                children: [
                  jsx('div', { className: 'hc-set__conflict-title', children: '配置已被别处改动，请刷新后重试' }, 'title'),
                  jsx('div', { className: 'hc-set__conflict-detail', children: conflict }, 'detail'),
                  jsx(
                    'button',
                    {
                      type: 'button',
                      className: 'hc-btn',
                      'data-conflict-refresh': '1',
                      onClick: reloadAll,
                      children: '刷新'
                    },
                    'refresh'
                  )
                ]
              },
              'conflict'
            )
          : null,
        Array.isArray(view.notes) && view.notes.length
          ? jsx(
              'ul',
              {
                className: 'hc-set__notes',
                children: view.notes.map(function (note, i) {
                  return jsx('li', { className: 'hc-set__note', children: String(note) }, 'note' + i)
                })
              },
              'notes'
            )
          : null,
        maskedKeys.length
          ? jsx(
              'div',
              {
                className: 'hc-set__note',
                children:
                  '有 ' +
                  maskedKeys.length +
                  ' 个字段已脱敏（' +
                  maskedKeys.join('、') +
                  '）：界面只读，改值请到配置文件。掩码不会被发进任何写请求。'
              },
              'masked'
            )
          : null,
        keys.length
          ? jsx('div', { className: 'hc-set__fields', children: fields }, 'fields')
          : jsx(
              Empty,
              {
                title: '这个插件没有可显示的配置项',
                hint:
                  '后端返回的 settings 是空的（该插件没写过 settings，manifest 也没声明 config_schema 默认值）。' +
                  '切到 default agent 可以看到全局层的值。',
                action: { label: '重新读取', onClick: settingsQuery.reload }
              },
              'nokeys'
            )
      ]
    })
  }

  // ── 页内 agent 列表：**首屏就有**，不等任何插件请求 ───────────────────────────
  // 这一排是「设置页自己的一份 agent 列表」，不靠外壳顶部的作用域选择器：点谁只影响本页
  // （作用域是全局的，混在一起会出现"我只是想看看 code-worker，结果整页作用域都变了"）。
  var agentChips = agentList.rows.map(function (row) {
    var id = String(row.id)
    var isPicked = id === agent
    var label = row.label && row.label !== id ? row.label + '（' + id + '）' : id
    var ms = typeof timings[id] === 'number' ? timings[id] : null
    return jsxs(
      'button',
      {
        type: 'button',
        className: 'hc-btn hc-btn--tiny' + (isPicked ? ' hc-btn--done' : ''),
        'data-agent-pick': id,
        'data-agent-picked': isPicked ? '1' : '0',
        'aria-pressed': isPicked ? 'true' : 'false',
        title: isPicked ? '本页正在看这个 Agent 的插件配置' : '点它：只加载这个 Agent 的插件配置',
        onClick: function () {
          pickAgent(id)
        },
        children: [
          isPicked ? '● ' : '',
          label,
          ms === null ? '' : ' · ' + ms + 'ms'
        ]
      },
      id
    )
  })

  var agentBar = jsxs('div', {
    className: 'hc-set__agents',
    'data-agent-bar': '1',
    // 样式走内联 + CSS 变量：本卡只允许改 settings.js，不能动 style.css，所以这里不新增样式表规则。
    style: {
      display: 'flex',
      flexDirection: 'column',
      gap: 'var(--s2)',
      minWidth: 0,
      padding: 'var(--s3)',
      border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
      borderRadius: 'var(--r-md)'
    },
    children: [
      jsxs(
        'div',
        {
          className: 'hc-set__listhead',
          children: [
          jsx('span', { className: 'hc-set__listtitle', children: 'Agent' }, 'title'),
          jsx(
            'span',
            {
              className: 'hc-chip',
              title: '这一排是设置页自己的一份列表，只决定本页读哪个 Agent；顶部作用域选择器管的是所有分区',
              children: '本页内选择'
            },
            'hint'
          ),
          scopeAgent && scopeAgent !== agent
            ? jsx(
                'span',
                {
                  className: 'hc-chip hc-set__scopehint',
                  'data-scope-agent': scopeAgent,
                  children: '顶部作用域：' + scopeAgent
                },
                'scope'
              )
            : null,
          agent
            ? jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-btn hc-btn--tiny',
                  'data-agent-clear': '1',
                  title: '清空本页选择，回到引导态（不会发任何请求）',
                  onClick: function () {
                    pickAgent('')
                  },
                  children: '不选'
                },
                'clear'
              )
            : null
          ]
        },
        'barhead'
      ),
      // 三种状态都显式：列表读到 → 一排可点的 agent；还在读 → 骨架条；没读到 → 原话 + 重试。
      // 只有"没读到"才是 ErrorState；"还在读"绝不显示成错误（那正是老板看到的假报错）。
      agentList.status === 'loading'
        ? jsx(Skeleton, { lines: 1, label: '正在读取 agent 列表' }, 'skel')
        : agentList.error
        ? jsx(
            ErrorState,
            {
              title: 'Agent 列表没读到',
              message: agentList.error,
              onRetry: function () {
                refresh('agents')
              }
            },
            'error'
          )
        : agentChips.length
        ? jsx(
            'div',
            {
              className: 'hc-set__agentchips',
              'data-agent-chips': '1',
              style: { display: 'flex', flexWrap: 'wrap', gap: 'var(--s2)', minWidth: 0 },
              children: agentChips
            },
            'chips'
          )
        : jsx(
            'div',
            {
              className: 'hc-set__note',
              children: '后端没返回任何 agent（/agents 是空数组）。没有 agent 就没有可读的插件配置 —— 先确认后端起在哪个 profile 上。'
            },
            'empty'
          )
    ]
  })

  // 网格的两个格子都要有 key（数组子元素），detail 是六种分支里选出来的一个元素，
  // 这里统一套一层带 key 的容器，避免在每个分支上各写一遍 key。
  var detailCell = jsx(
    'div',
    {
      className: 'hc-set__detailwrap',
      style: { display: 'flex', flexDirection: 'column', gap: 'var(--s2)', minWidth: 0 },
      children: detail
    },
    'detail'
  )

  return jsx('div', {
    className: 'hc-set',
    children: [
      jsx('div', { className: 'hc-set__agentswrap', style: { minWidth: 0 }, children: agentBar }, 'agents'),
      jsx('div', { className: 'hc-set__grid', children: [listCol, detailCell] }, 'grid')
    ]
  })
}