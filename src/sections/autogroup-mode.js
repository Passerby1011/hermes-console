/**
 * hermes-console · 项目分组「模式」面板（t_f2338eb2）。
 *
 * 本文件只做一件事：把后端 `POST /autogroup/plan → /apply → /undo` 三段契约（t_5e74252e）
 * 变成人能用的一段界面。**判定、写入、回滚全在后端**，这里只负责：
 *
 *   ① 显式选模式：A「只归类未分组（安全）」/ B「全量重建分组体系（破坏性）」；
 *      B 必须有红字破坏性提示，且和 A 长得不一样；
 *   ② 先预览（**零写入**）→ 显示「这次会动什么」：扫描范围 / 变更数 / 逐条新旧映射 diff /
 *      B 模式下被抛弃的旧项目清单；
 *   ③ 确认应用（B 需二次确认）→ 逐条回读结果（成功 N / 失败 M，失败原因可展开）；
 *   ④ 整批撤销（按 op_id）→ 贴三库哈希是否回到应用前。
 *
 * 五条硬规矩（改这个文件时别丢）：
 *   1. **预览零写入**：`/autogroup/preview` 是纯只读的，界面上按钮就叫「先预览」，不叫「开始分组」；
 *      预览体里的 `will_apply / skipped_low_confidence` 是后端算的计划，不是已经发生的事；
 *   2. **确认才写**：`/apply` 必须带 `confirm:true`；B 模式再叠一层界面二次确认；
 *   3. **模式决定扫描口径**：切模式立刻作废手上的预览（A 扫未分组 / B 扫全部）；
 *      不许出现「按 A 预览、按 B 应用」这种口径漂移；
 *   4. **失败原样说**：`plan_stale` / `plan_expired` 一律提示「数据变了，重新预览」，
 *      不自己编原因；`verify_mismatch` 说明「已自动回滚」；
 *   5. 颜色只走 var(--ui-*)：本文件不出现字面色值（构建脚本会检查），key 一律第三参数
 *      （React 19 dev 对 props.key 会报 console error）。
 */
import { jsx as jsxRuntime, jsxs as jsxsRuntime } from 'react/jsx-runtime'
import { useState } from 'react'

import { num } from '../lib/format.js'
import { get, send } from '../lib/rest.js'
import { ErrorState } from '../lib/states.js'

/** 扫描 job 轮询：间隔 2s、总上限 4 分钟（全量重建时逐条调模型，比关键词扫描慢）。 */
var SCAN_POLL_MS = 2000
var SCAN_POLL_TIMEOUT_MS = 240000

/** 后端错误 → 一句可读文本（优先 `error.message`，其次顶层 `message`）。 */
function errorText(res, fallback) {
  if (!res) return fallback
  if (res.error && res.error.message) return res.error.message
  if (res.message) return res.message
  return fallback
}

/** React 19 dev 对「props 里带 key」会告警：统一把 key 提到第三参数。 */
function withoutKey(props) {
  if (!props || !Object.prototype.hasOwnProperty.call(props, 'key')) return null
  var next = {}
  Object.keys(props).forEach(function (name) {
    if (name !== 'key') next[name] = props[name]
  })
  return next
}

function jsx(type, props, key) {
  var cleaned = withoutKey(props)
  if (cleaned) return jsxRuntime(type, cleaned, props.key)
  return jsxRuntime(type, props, key)
}

function jsxs(type, props, key) {
  var cleaned = withoutKey(props)
  if (cleaned) return jsxsRuntime(type, cleaned, props.key)
  return jsxsRuntime(type, props, key)
}

export var MODE_UNGROUPED_ONLY = 'ungrouped_only'
export var MODE_REBUILD_ALL = 'rebuild_all'
export var MODES = [MODE_UNGROUPED_ONLY, MODE_REBUILD_ALL]

/** 模式名（界面上要能直接读出「选了哪个」）。 */
export var MODE_LABEL = {
  ungrouped_only: '只归类未分组（安全）',
  rebuild_all: '全量重建分组体系（破坏性）'
}

/** 短名（表格/状态条里用）。 */
export var MODE_PLAIN = { ungrouped_only: '只归类未分组', rebuild_all: '全量重建' }

/** B 模式的破坏性提示：必须在 900/1350 两个宽度都完整可见（不截断、不省略号）。 */
export var REBUILD_WARNING =
  '破坏性操作：会重新考量全部可见会话、重建整个项目结构，并抛弃旧分组 —— 新体系里没有会话的旧项目会被删除项目实体（目录保留）。应用前按字节备份三库，可整批撤销。'

/** 计划候选上限（后端钳到 1..500，默认 200）。 */
export var PLAN_LIMIT = 200

var CHANGE_LABEL = {
  assigned: '新归入',
  moved: '换项目',
  unchanged: '不变（不写）',
  pending_new_project: '待建新项目（未确认）',
  unmatched: '判不出（不写）'
}

var CHANGE_TONE = {
  assigned: 'ok',
  moved: 'warn',
  unchanged: 'mute',
  pending_new_project: 'warn',
  unmatched: 'mute'
}

/**
 * 预览体 → 「扫描范围」四个数 + 「计划变更」+ 「逐条 diff」。
 *
 * t_3c2817b1 重接线后预览来自 `POST /autogroup/preview`（不再有 `/autogroup/plan` 的
 * `candidates / items[].from_slug / changes / diff / dropped_projects` 那一套）。新形态：
 *
 *   `{count, will_apply, skipped_low_confidence, sync_cwd, min_confidence, items[], kanban[], notes[]}`
 *   每项 `{session_id, title, profile, project_slug, project_name, target_path, current_cwd,
 *          confidence, basis{kind,label,reason}, source, kanban, will_apply, will_sync_cwd, skip_reason}`
 *
 * 所以：
 *   * 「可见会话」= `count`（后端给的候选条数，**不是**自己数的行数）；
 *   * 「已分组」= `count - ungrouped`；未分组 = A 模式下 `count`、B 模式下 `count - 已分组`
 *     —— 后端口径里已 applied 的会话在 A 模式被排除、在 B 模式进候选，这里按 `current_cwd`
 *     与 `basis` 无法反推，因此**只用后端给的两个数**，不自己猜：`grouped = 0` 时写「后端未单列」。
 *   * 「看板排除」= `kanban.length`（后端硬排除，应用时也不会写）。
 */
export function planScope(plan) {
  var data = plan || {}
  var items = data.items || []
  var count = typeof data.count === 'number' ? data.count : items.length
  var willApply = typeof data.will_apply === 'number' ? data.will_apply : 0
  var skipped = typeof data.skipped_low_confidence === 'number' ? data.skipped_low_confidence : 0
  return {
    total: count,
    grouped: Math.max(0, count - willApply),
    ungrouped: willApply,
    kanban: (data.kanban || []).length,
    rejected: 0,
    candidates: count,
    willApply: willApply,
    skipped: skipped,
    minConfidence: data.min_confidence
  }
}

/** 预览体 → 「计划变更」三个数（应用时真会写几条 / 跳过几条 / 几个项目会没有目录）。 */
export function planChanges(plan) {
  var data = plan || {}
  var items = data.items || []
  var willWrite = typeof data.will_apply === 'number' ? data.will_apply : 0
  var skipped = typeof data.skipped_low_confidence === 'number' ? data.skipped_low_confidence : 0
  var missingPaths = {}
  items.forEach(function (item) {
    if (item.will_apply && !item.target_path) missingPaths[item.project_slug || '—'] = true
  })
  return {
    will_write: willWrite,
    unchanged: 0,
    unmatched: skipped,
    move: 0,
    new_projects: 0,
    dropped_projects: 0,
    missing_project_path: Object.keys(missingPaths)
  }
}

/** 预览体 → 逐条 diff 行（会话 → 目标项目 → 会写/会跳过）。 */
export function planDiffRows(plan) {
  return ((plan && plan.items) || []).map(function (item) {
    return {
      session_id: item.session_id,
      title: item.title || '',
      from: item.current_cwd || '',
      to: item.project_name || item.project_slug || '',
      change: item.will_apply
        ? (item.skip_reason ? 'unchanged' : 'assigned')
        : 'unmatched',
      will_apply: item.will_apply === true,
      skip_reason: item.skip_reason || '',
      confidence: item.confidence,
      basis: item.basis || {}
    }
  })
}

function slugText(slug) {
  return slug || '（未分组）'
}

/** 一行 diff：会话 → 原项目 → 新项目。 */
function DiffRow(props) {
  var row = props.row || {}
  var tone = CHANGE_TONE[row.change] || 'mute'
  return jsxs('div', {
    className: 'hc-agg2__diff-row hc-agg2__diff-row--' + tone,
    'data-agg2-diff-row': row.session_id,
    'data-from': row.from || '',
    'data-to': row.to || '',
    'data-change': row.change || '',
    children: [
      jsxs('div', {
        key: 's',
        className: 'hc-agg2__diff-sess',
        children: [
          jsx('span', { key: 'id', className: 'hc-agg2__diff-id', children: row.session_id }),
          jsx('span', { key: 't', className: 'hc-agg2__diff-title', title: row.title || '', children: row.title || '' })
        ]
      }),
      jsx('div', {
        key: 'f',
        className: 'hc-agg2__diff-from',
        'data-agg2-diff-from': row.session_id,
        children: slugText(row.from)
      }),
      jsx('div', {
        key: 't',
        className: 'hc-agg2__diff-to',
        'data-agg2-diff-to': row.session_id,
        children: slugText(row.to)
      }),
      jsx('div', {
        key: 'c',
        className: 'hc-agg2__diff-change hc-agg2__diff-change--' + tone,
        children: CHANGE_LABEL[row.change] || row.change || ''
      })
    ]
  })
}

/**
 * 模式面板本体。
 *
 * props：
 *   mode     当前模式（父组件持有，抽屉里的旧入口也要按它收口）
 *   onMode   切模式
 *   onChanged 应用/撤销成功后让父组件重读归属表、项目表、会话列表
 */
export function AutogroupModePanel(props) {
  var mode = props.mode || MODE_UNGROUPED_ONLY
  var onMode = props.onMode
  var onChanged = props.onChanged

  var planState = useState({ phase: 'idle', error: '', errorCode: '', data: null, request: null })
  var plan = planState[0]
  var setPlan = planState[1]
  var applyState = useState({ phase: 'idle', error: '', errorCode: '', data: null })
  var applied = applyState[0]
  var setApplied = applyState[1]
  var confirmState = useState(false)
  var needConfirm = confirmState[0]
  var setNeedConfirm = confirmState[1]
  var undoState = useState({ phase: 'idle', error: '', data: null })
  var undo = undoState[0]
  var setUndo = undoState[1]

  var rebuild = mode === MODE_REBUILD_ALL
  var busy = plan.phase === 'loading' || applied.phase === 'busy' || undo.phase === 'busy'
  var planData = plan.phase === 'ready' ? plan.data : null
  var scope = planData ? planScope(planData) : null
  var changes = (planData && planChanges(planData)) || {}
  /**
   * 有活可干 = 要写归属（预览里的 will_apply > 0）。
   * 计划契约下线后不再有「要建项目 / 要删项目」两个维度：
   *   * 建项目走独立的 `POST /autogroup/projects`（AI 分类抽屉里的「新建项目候选」）；
   *   * B 模式（全量重建）现在靠 `scan {scope:'all'}` 重算归属，不再删项目实体。
   */
  var hasWork = (changes.will_write || 0) > 0
  var diffRows = (planData && planDiffRows(planData)) || []
  var dropped = []
  var notes = (planData && planData.notes) || []
  var scanInfo = planData && planData.scan
  /** 预览的「标识」：只用于 data-* 钩子与二次确认框的 key（计划契约已下线，不再有 plan_id）。 */
  var planKey = (plan.request && plan.request.scope) || 'preview'

  /** 切模式：手上的计划立刻作废（plan_id 只对应一种 mode，不能拿来混用）。 */
  function pickMode(next) {
    if (!next || next === mode) return
    setPlan({ phase: 'idle', error: '', errorCode: '', data: null, request: null })
    setApplied({ phase: 'idle', error: '', errorCode: '', data: null })
    setUndo({ phase: 'idle', error: '', data: null })
    setNeedConfirm(false)
    if (typeof onMode === 'function') onMode(next)
  }

  /**
   * 先预览：**零写入**。
   *
   * 旧实现 `POST /autogroup/plan`（计划契约）已下线，后端在 `POST /autogroup/apply` 里明确
   * 400 `plan_flow_removed` 并指向「全量重建请用 POST /autogroup/scan {scope:'all'} 重新扫描」。
   * 所以这里按后端指的路走两段：
   *   ① `POST /autogroup/scan {scope, ai, limit}` + 轮询 → 候选会被判成 suggested 行
   *      （**只写建议表，不动任何 applied 归属 / cwd / 目录**）；
   *   ② `POST /autogroup/preview` → 200 + 零写入，给出「会写 N 条 / 跳过 M 条及原因」。
   *
   * 预览体仍然是零写入：`preview.will_apply` 只是后端按阈值算出来的**计划**，
   * 真正写库必须再点「确认应用」→ `POST /autogroup/apply {confirm:true}`。
   */
  function preview() {
    if (busy) return
    var body = { scope: rebuild ? 'all' : 'ungrouped', limit: PLAN_LIMIT, ai: rebuild }
    setPlan({ phase: 'loading', error: '', errorCode: '', data: null, request: body })
    setApplied({ phase: 'idle', error: '', errorCode: '', data: null })
    setUndo({ phase: 'idle', error: '', data: null })
    setNeedConfirm(false)
    send('/autogroup/scan', body).then(function (res) {
      if (!res || res.ok === false) {
        setPlan({
          phase: 'error',
          error: errorText(res, '扫描失败（后端没有给原因）'),
          errorCode: (res && (res.code || (res.error && res.error.code))) || '',
          data: null,
          request: body
        })
        return
      }
      var payload = res.data || {}
      var jobId = payload.job_id || (payload.job && payload.job.id) || ''
      if (!jobId) {
        setPlan({
          phase: 'error',
          error: '后端没有返回 job_id：' + JSON.stringify(payload).slice(0, 160),
          errorCode: 'no_job_id',
          data: null,
          request: body
        })
        return
      }
      pollScan(jobId, body, Date.now())
    })
  }

  /** 轮询扫描 job，done 之后立刻拉一次零写入预览。 */
  function pollScan(jobId, body, startedAt) {
    get('/autogroup/job/' + encodeURIComponent(jobId)).then(function (res) {
      if (!res || res.ok === false) {
        setPlan({
          phase: 'error',
          error: errorText(res, '查询扫描进度失败'),
          errorCode: (res && (res.code || (res.error && res.error.code))) || '',
          data: null,
          request: body
        })
        return
      }
      var payload = res.data || {}
      var job = payload.data || payload
      var state = payload.state || job.state || ''
      if (state === 'failed') {
        var failure = payload.error || job.error || {}
        setPlan({
          phase: 'error',
          error: failure.message || '扫描失败（后端没给原因）',
          errorCode: failure.code || 'scan_failed',
          data: null,
          request: body
        })
        return
      }
      if (state !== 'done') {
        if (Date.now() - startedAt > SCAN_POLL_TIMEOUT_MS) {
          setPlan({
            phase: 'error',
            error: '扫描超过 4 分钟仍未完成（job ' + jobId + '），请稍后重试',
            errorCode: 'scan_timeout',
            data: null,
            request: body
          })
          return
        }
        setTimeout(function () { pollScan(jobId, body, startedAt) }, SCAN_POLL_MS)
        return
      }
      var result = job.result || {}
      fetchPreview(body, result)
    })
  }

  /** 扫描完成 → 拉零写入预览，并把 scan 的统计一起带进预览视图。 */
  function fetchPreview(body, scanResult) {
    send('/autogroup/preview', { sync_cwd: true }).then(function (res) {
      if (!res || res.ok === false) {
        setPlan({
          phase: 'error',
          error: errorText(res, '预览失败（后端没有给原因）'),
          errorCode: (res && (res.code || (res.error && res.error.code))) || '',
          data: null,
          request: body
        })
        return
      }
      var payload = res.data || {}
      var data = payload.data || payload
      setPlan({
        phase: 'ready',
        error: '',
        errorCode: '',
        data: Object.assign({}, data, {
          notes: (payload.notes || []).concat(scanResult.notes || []),
          scan: {
            scanned: scanResult.scanned,
            matched: scanResult.matched,
            saved: scanResult.saved,
            empty: !!scanResult.empty,
            empty_reason: scanResult.empty_reason || null,
            llm: scanResult.llm || null,
            elapsed_ms: scanResult.elapsed_ms
          }
        }),
        request: body
      })
    })
  }

  /** 确认应用：B 模式先出二次确认；真正写库只走 applyNow()。 */
  function askApply() {
    if (!planData || applied.phase === 'busy') return
    if (rebuild && !needConfirm) {
      setNeedConfirm(true)
      return
    }
    applyNow()
  }

  /**
   * 真写：`POST /autogroup/apply {confirm:true}` —— 后端**唯一**写路径。
   *
   * 不带 plan_id（计划契约已下线，带了会被 400 `plan_flow_removed`）。
   * 只有界面勾掉/取消的条目才传 `session_ids`；不传 = 全部待确认建议。
   * B 模式（全量重建）已在上面叠了红字警示 + 二次确认，这里不再重问。
   */
  function applyNow() {
    if (!planData || applied.phase === 'busy') return
    setNeedConfirm(false)
    setApplied({ phase: 'busy', error: '', errorCode: '', data: null })
    var body = { confirm: true, sync_cwd: body_syncCwd() }
    var keep = (planData.items || [])
      .filter(function (item) { return item.will_apply === true })
      .map(function (item) { return item.session_id })
    if (keep.length) body.session_ids = keep
    send('/autogroup/apply', body).then(function (res) {
      if (!res || res.ok === false) {
        var code = (res && (res.error && res.error.code)) || ''
        var status = (res && res.status) || ''
        var stale = code === 'plan_stale' || code === 'plan_expired' || status === '409'
        setApplied({
          phase: 'error',
          error: stale
            ? '数据变了（' + (code || 'stale') + '）：请重新「先预览」，用新的预览结果再确认'
            : errorText(res, '应用失败（后端没有给原因）'),
          errorCode: code,
          data: null
        })
        if (stale) setPlan({ phase: 'idle', error: '', errorCode: '', data: null, request: plan.request })
        return
      }
      var payload = res.data || {}
      var data = payload.data || payload
      setApplied({ phase: 'done', error: '', errorCode: '', data: Object.assign({}, data, { notes: payload.notes || [] }) })
      setUndo({ phase: 'idle', error: '', data: null })
      if (typeof onChanged === 'function') onChanged()
    })
  }

  /** 本次是否同步 cwd（预览时的选择要在应用时保持一致，不偷偷改口径）。 */
  function body_syncCwd() {
    return (plan.request && plan.request.sync_cwd !== false)
  }

  /** 整批撤销：POST /autogroup/undo {op_id}。 */
  function undoOp(opId) {
    if (!opId || undo.phase === 'busy') return
    setUndo({ phase: 'busy', error: '', data: null })
    send('/autogroup/undo', { op_id: opId }).then(function (res) {
      if (!res || res.ok === false) {
        setUndo({ phase: 'error', error: errorText(res, '撤销失败（后端没有给原因）'), data: null })
        return
      }
      var payload = res.data || {}
      var data = payload.data || payload
      setUndo({ phase: 'done', error: '', data: Object.assign({}, data, { notes: payload.notes || [] }) })
      if (typeof onChanged === 'function') onChanged()
    })
  }

  // ------------------------------------------------------------------ 子块

  function renderModes() {
    return jsx('div', {
      className: 'hc-agg2__modes',
      role: 'radiogroup',
      'aria-label': '分组模式',
      children: MODES.map(function (item) {
        var active = item === mode
        return jsxs(
          'label',
          {
            className:
              'hc-agg2__mode-opt' +
              (active ? ' hc-agg2__mode-opt--on' : '') +
              (item === MODE_REBUILD_ALL ? ' hc-agg2__mode-opt--danger' : ''),
            'data-agg2-mode-opt': item,
            'data-agg2-mode-opt-active': active ? '1' : '0',
            title:
              item === MODE_UNGROUPED_ONLY
                ? 'A：只给未分组的会话归类；已分组会话一行都不动（安全）'
                : 'B：全部可见会话统一考量，重建项目体系；新体系里没有会话的旧项目会被抛弃（破坏性，可整批撤销）',
            children: [
              jsx('input', {
                key: 'radio',
                type: 'radio',
                name: 'hc-agg2-mode',
                'data-agg2-mode': item,
                'data-agg2-mode-checked': active ? '1' : '0',
                checked: active,
                onChange: function () {
                  pickMode(item)
                }
              }),
              jsx('span', { key: 'text', className: 'hc-agg2__mode-opt-text', children: MODE_LABEL[item] })
            ]
          },
          item
        )
      })
    })
  }

  function renderPlan() {
    if (plan.phase === 'loading') {
      return jsx('div', {
        className: 'hc-agg2__plan-loading',
        'data-agg2-plan-loading': '1',
        children: '正在生成计划（只读，零写入）…'
      })
    }
    if (plan.phase === 'error') {
      return jsx(ErrorState, {
        title: '预览没生成',
        message: plan.error,
        onRetry: preview,
        retryLabel: '重新预览'
      })
    }
    if (!planData) return null
    return jsxs('div', {
      className: 'hc-agg2__plan',
      'data-agg2-plan': planKey,
      'data-agg2-plan-mode': rebuild ? MODE_REBUILD_ALL : MODE_UNGROUPED_ONLY,
      'data-agg2-plan-zero-write': '1',
      'data-agg2-plan-request': JSON.stringify(plan.request || {}),
      children: [
        jsxs('div', {
          key: 'facts',
          className: 'hc-agg2__plan-facts',
          children: [
            jsxs('div', {
              key: 'mode',
              className: 'hc-agg2__plan-fact',
              'data-agg2-plan-fact': 'mode',
              children: [
                jsx('span', { key: 'k', className: 'hc-agg2__plan-k', children: '当前模式' }),
                jsx('span', {
                  key: 'v',
                  className: 'hc-agg2__plan-v' + (rebuild ? ' hc-agg2__plan-v--danger' : ''),
                  'data-agg2-plan-mode-label': rebuild ? MODE_REBUILD_ALL : MODE_UNGROUPED_ONLY,
                  children: MODE_PLAIN[rebuild ? MODE_REBUILD_ALL : MODE_UNGROUPED_ONLY] +
                    ' · scope=' + ((plan.request && plan.request.scope) || 'ungrouped')
                })
              ]
            }),
            jsxs('div', {
              key: 'scope',
              className: 'hc-agg2__plan-fact',
              'data-agg2-plan-scope': String(scope.total),
              'data-agg2-plan-grouped': String(scope.grouped),
              'data-agg2-plan-ungrouped': String(scope.ungrouped),
              'data-agg2-plan-kanban': String(scope.kanban),
              children: [
                jsx('span', { key: 'k', className: 'hc-agg2__plan-k', children: '候选与范围' }),
                jsx('span', {
                  key: 'v',
                  className: 'hc-agg2__plan-v',
                  children:
                    '待确认建议 ' + num(scope.total) + ' 条（其中会写入 ' + num(scope.ungrouped) +
                    ' 条、跳过 ' + num(scope.skipped) + ' 条）· 看板硬排除 ' + num(scope.kanban) + ' 条' +
                    (scope.minConfidence !== undefined ? ' · 置信度阈值 ' + scope.minConfidence : '')
                })
              ]
            }),
            jsxs('div', {
              key: 'changes',
              className: 'hc-agg2__plan-fact',
              'data-agg2-plan-changes': String(changes.will_write || 0),
              'data-agg2-plan-new': String(changes.new_projects || 0),
              'data-agg2-plan-dropped': String(changes.dropped_projects || 0),
              'data-agg2-plan-write': String(changes.will_write || 0),
              children: [
                jsx('span', { key: 'k', className: 'hc-agg2__plan-k', children: '本次会写什么' }),
                jsx('span', {
                  key: 'v',
                  className: 'hc-agg2__plan-v',
                  children:
                    '改归属 ' + num(changes.will_write) + ' 条' +
                    '（低于阈值 / 判不出跳过 ' + num(changes.unmatched) + ' 条）' +
                    ((changes.missing_project_path || []).length
                      ? ' · 这些项目没有目录，同步 cwd 会跳过：' + changes.missing_project_path.join('、')
                      : '')
                })
              ]
            }),
            jsxs('div', {
              key: 'zw',
              className: 'hc-agg2__plan-fact',
              children: [
                jsx('span', { key: 'k', className: 'hc-agg2__plan-k', children: '写入' }),
                jsx('span', {
                  key: 'v',
                  className: 'hc-agg2__plan-v hc-agg2__plan-v--ok',
                  'data-agg2-plan-zero-write-note': '1',
                  children:
                    '预览阶段零写入（预览是只读的：不动 applied 归属、不动 cwd、不建目录）；只有点「确认应用」才写库'
                })
              ]
            }),
            scanInfo
              ? jsxs('div', {
                  key: 'scan',
                  className: 'hc-agg2__plan-fact',
                  'data-agg2-plan-scan': '1',
                  children: [
                    jsx('span', { key: 'k', className: 'hc-agg2__plan-k', children: '本次扫描' }),
                    jsx('span', {
                      key: 'v',
                      className: 'hc-agg2__plan-v',
                      children: scanInfo.empty
                        ? (scanInfo.empty_reason || '没有可归类的会话') +
                          '（已扫描 ' + num(scanInfo.scanned) + ' 条，命中 ' + num(scanInfo.matched) + ' 条）'
                        : '扫描 ' + num(scanInfo.scanned) + ' 条 · 命中 ' + num(scanInfo.matched) +
                          ' · 落库 ' + num(scanInfo.saved) + ' 条' +
                          (scanInfo.llm && scanInfo.llm.code
                            ? ' · 模型 ' + scanInfo.llm.code +
                              (scanInfo.llm.provider ? '（' + scanInfo.llm.provider + '/' + scanInfo.llm.model + '）' : '')
                            : '') +
                          (scanInfo.elapsed_ms ? ' · 耗时 ' + num(Math.round(scanInfo.elapsed_ms / 100) / 10) + 's' : '')
                    })
                  ]
                })
              : null
          ]
        }),
        jsxs('div', {
          key: 'diff',
          className: 'hc-agg2__diff',
          'data-agg2-diff': String(diffRows.length),
          children: [
            jsxs('div', {
              key: 'head',
              className: 'hc-agg2__diff-head',
              children: [
                jsx('span', { key: 'a', className: 'hc-agg2__diff-col', children: '会话' }),
                jsx('span', { key: 'b', className: 'hc-agg2__diff-col', children: '原项目' }),
                jsx('span', { key: 'c', className: 'hc-agg2__diff-col', children: '新项目' }),
                jsx('span', { key: 'd', className: 'hc-agg2__diff-col', children: '变化' })
              ]
            }),
            jsx('div', {
              key: 'rows',
              className: 'hc-agg2__diff-rows',
              children: diffRows.length
                ? diffRows.map(function (row, index) {
                    return jsx(DiffRow, { row: row }, row.session_id || 'd' + index)
                  })
                : jsx('div', { className: 'hc-agg2__note', children: '这次没有可比较的会话（候选 0 条）' })
            })
          ]
        }),
        rebuild
          ? jsxs('div', {
              key: 'dropped',
              className: 'hc-agg2__dropped',
              'data-agg2-dropped': String(dropped.length),
              children: [
                jsx('div', {
                  key: 't',
                  className: 'hc-agg2__dropped-title',
                  children: '会被抛弃的旧项目 ' + num(dropped.length) + ' 个（新体系里没有它们的会话）'
                }),
                jsx('div', {
                  key: 'list',
                  className: 'hc-agg2__dropped-list',
                  children: dropped.length
                    ? dropped.map(function (item) {
                        return jsxs(
                          'span',
                          {
                            className: 'hc-agg2__dropped-item',
                            'data-agg2-dropped-item': item.slug,
                            'data-agg2-dropped-will-delete': item.will_delete ? '1' : '0',
                            title: item.blocked_reason || '',
                            children: [
                              jsx('span', { key: 'n', className: 'hc-agg2__dropped-name', children: item.name || item.slug }),
                              jsx('span', {
                                key: 'c',
                                className: 'hc-agg2__dropped-count',
                                children:
                                  '现有归属 ' + num(item.assignments_now || 0) + ' 条' +
                                  (item.will_delete ? '' : ' · 本次不删（' + (item.blocked_reason || '受限') + '）')
                              })
                            ]
                          },
                          item.slug
                        )
                      })
                    : jsx('span', { className: 'hc-agg2__note', children: '没有要抛弃的旧项目' })
                })
              ]
            })
          : null,
        jsxs('div', {
          key: 'apply',
          className: 'hc-agg2__apply',
          children: [
            jsx('button', {
              key: 'go',
              type: 'button',
              className: 'hc-btn ' + (rebuild ? 'hc-btn--danger' : 'hc-btn--accent'),
              'data-agg2-apply': planKey,
              disabled: busy || applied.phase === 'done' || !hasWork,
              title: !hasWork
                ? '这次没有要写的归属行（预览里会写入 0 条）'
                : rebuild
                  ? 'B 模式会重算全部可见会话的归属：先出二次确认'
                  : '按这份预览写入归属（已分组会话一行不动）',
              onClick: askApply,
              children: applied.phase === 'done' ? '已应用' : '确认应用'
            }),
            jsx('span', {
              key: 'hint',
              className: 'hc-agg2__apply-hint',
              'data-agg2-apply-hint': rebuild ? 'rebuild' : 'safe',
              children: rebuild
                ? 'B 模式需要二次确认：点了会再问一次，确认后才写库'
                : 'A 模式：只给未分组会话归类，已分组会话的归属一个字段都不动'
            }),
            jsx('button', {
              key: 'again',
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              'data-agg2-plan-repreview': '1',
              disabled: busy,
              title: '重新跑一次扫描 + 只读预览（数据变了要重预览）',
              onClick: preview,
              children: '重新预览'
            })
          ]
        }),
        needConfirm && rebuild
          ? jsxs('div', {
              key: 'confirm',
              className: 'hc-agg2__confirm',
              'data-agg2-apply-confirm-box': planKey,
              children: [
                jsx('span', {
                  key: 't',
                  className: 'hc-agg2__confirm-text',
                  children:
                    '二次确认（破坏性）：将按全量重建口径写入 ' + num(changes.will_write || 0) +
                    ' 条归属，重新考量全部可见会话。项目实体不会被删除（目录保留），' +
                    '但已有归属会被改写。确定重建？'
                }),
                jsx('button', {
                  key: 'y',
                  type: 'button',
                  className: 'hc-btn hc-btn--danger',
                  'data-agg2-apply-confirm': planKey,
                  disabled: applied.phase === 'busy',
                  onClick: applyNow,
                  children: applied.phase === 'busy' ? '写入中…' : '确认重建并写入'
                }),
                jsx('button', {
                  key: 'n',
                  type: 'button',
                  className: 'hc-btn hc-btn--tiny',
                  'data-agg2-apply-cancel': '1',
                  onClick: function () {
                    setNeedConfirm(false)
                  },
                  children: '取消'
                })
              ]
            })
          : null,
        notes.length
          ? jsx('div', {
              key: 'notes',
              className: 'hc-agg2__notes',
              'data-agg2-plan-notes': String(notes.length),
              children: notes.map(function (note, index) {
                return jsx('div', { className: 'hc-agg2__note', children: note }, 'pn' + index)
              })
            })
          : null
      ]
    })
  }

  function renderApplied() {
    if (applied.phase === 'idle' || applied.phase === 'busy') {
      if (applied.phase !== 'busy') return null
      return jsx('div', { className: 'hc-agg2__plan-loading', 'data-agg2-apply-busy': '1', children: '正在写入并逐条回读…' })
    }
    if (applied.phase === 'error') {
      return jsx(ErrorState, {
        title: '应用没成功',
        message: applied.error,
        onRetry: function () {
          preview()
        },
        retryLabel: '重新预览'
      })
    }
    var data = applied.data || {}
    var failed = data.failed_items || []
    var written = data.sessions || []
    var planItems = (planData && planData.items) || []
    var toOf = {}
    planItems.forEach(function (item) {
      toOf[item.session_id] = item.to_slug
    })
    return jsxs('div', {
      className: 'hc-agg2__result',
      'data-agg2-result': data.op_id || '',
      'data-agg2-result-written': String(data.written || 0),
      'data-agg2-result-failed': String(failed.length),
      'data-agg2-result-mode': data.mode || '',
      children: [
        jsxs('div', {
          key: 'head',
          className: 'hc-agg2__result-head',
          children: [
            jsx('span', {
              key: 't',
              className: 'hc-agg2__result-title',
              children: '已应用（' + (data.mode_label || MODE_PLAIN[data.mode] || data.mode) + '）'
            }),
            jsx('span', {
              key: 'ok',
              className: 'hc-agg2__result-chip hc-agg2__result-chip--ok',
              'data-agg2-result-ok': String(data.written || 0),
              children: '成功 ' + num(data.written || 0) + ' 条'
            }),
            jsx('span', {
              key: 'bad',
              className: 'hc-agg2__result-chip' + (failed.length ? ' hc-agg2__result-chip--bad' : ''),
              'data-agg2-result-bad': String(failed.length),
              children: '失败 ' + num(failed.length) + ' 条'
            }),
            jsx('span', {
              key: 'op',
              className: 'hc-agg2__result-op',
              'data-agg2-result-op': data.op_id || '',
              children: 'op_id ' + (data.op_id || '—')
            }),
            jsx('span', {
              key: 'rb',
              className: 'hc-agg2__result-op',
              'data-agg2-result-readback': String((data.readback || {}).checked || 0),
              children:
                '回读 ' + num((data.readback || {}).checked || 0) + ' 条 · 不符 ' +
                num(((data.readback || {}).mismatched || []).length + ((data.readback || {}).cwd_mismatched || []).length) + ' 条'
            })
          ]
        }),
        jsxs('div', {
          key: 'rows',
          className: 'hc-agg2__result-rows',
          'data-agg2-result-rows': String(written.length + failed.length),
          children: [
            written.map(function (sessionId) {
              return jsxs(
                'div',
                {
                  className: 'hc-agg2__result-row',
                  'data-agg2-result-row': sessionId,
                  'data-agg2-result-row-ok': '1',
                  children: [
                    jsx('span', { key: 's', className: 'hc-agg2__diff-id', children: sessionId }),
                    jsx('span', { key: 'a', className: 'hc-agg2__result-arrow', children: '→' }),
                    jsx('span', { key: 't', className: 'hc-agg2__diff-to', children: slugText(toOf[sessionId]) }),
                    jsx('span', { key: 'k', className: 'hc-agg2__result-ok', children: '已写入并回读一致' })
                  ]
                },
                'ok-' + sessionId
              )
            }),
            failed.map(function (item, index) {
              return jsxs(
                'div',
                {
                  className: 'hc-agg2__result-row hc-agg2__result-row--bad',
                  'data-agg2-result-row': item.session_id || 'f' + index,
                  'data-agg2-result-row-ok': '0',
                  children: [
                    jsx('span', { key: 's', className: 'hc-agg2__diff-id', children: item.session_id || '（未知会话）' }),
                    jsx('span', { key: 'k', className: 'hc-agg2__result-bad', children: '未写入：' + (item.why || '后端没给原因') })
                  ]
                },
                'bad-' + (item.session_id || index)
              )
            })
          ]
        }),
        failed.length
          ? jsxs('details', {
              key: 'why',
              className: 'hc-agg2__failed',
              'data-agg2-failed': String(failed.length),
              children: [
                jsx('summary', {
                  key: 's',
                  className: 'hc-agg2__failed-summary',
                  children: '展开失败原因（' + num(failed.length) + ' 条）'
                }),
                jsx('div', {
                  key: 'b',
                  className: 'hc-agg2__failed-body',
                  children: failed.map(function (item, index) {
                    return jsx(
                      'div',
                      {
                        className: 'hc-agg2__failed-row',
                        'data-agg2-failed-row': item.session_id || 'f' + index,
                        children: (item.session_id || '（未知会话）') + '：' + (item.why || '后端没给原因')
                      },
                      'fw' + index
                    )
                  })
                })
              ]
            })
          : null,
        jsxs('div', {
          key: 'undo',
          className: 'hc-agg2__undo',
          children: [
            jsx('button', {
              key: 'b',
              type: 'button',
              className: 'hc-btn',
              'data-agg2-undo': data.op_id || '',
              disabled: !data.op_id || undo.phase === 'busy' || undo.phase === 'done',
              title: '按 op_id 整批撤销：归属行逐字段还原、cwd 还原、本次新建的项目实体删掉、被抛弃的旧项目写回',
              onClick: function () {
                undoOp(data.op_id)
              },
              children: undo.phase === 'busy' ? '撤销中…' : undo.phase === 'done' ? '已撤销' : '整批撤销'
            }),
            jsx('span', {
              key: 'h',
              className: 'hc-agg2__apply-hint',
              children: '撤销按 op_id 整批回滚；撤销完再回读归属表与项目表核对'
            })
          ]
        }),
        undo.phase === 'error'
          ? jsx('div', { key: 'ue', className: 'hc-agg2__result-error', 'data-agg2-undo-error': '1', children: undo.error })
          : null,
        undo.phase === 'done'
          ? jsxs('div', {
              key: 'ur',
              className: 'hc-agg2__undo-result',
              'data-agg2-undo-result': (undo.data || {}).op_id || '',
              'data-agg2-undo-byte-identical': (undo.data || {}).byte_identical ? '1' : '0',
              children: [
                jsx('div', {
                  key: 't',
                  className: 'hc-agg2__undo-title',
                  children:
                    '已整批撤销 ' + ((undo.data || {}).op_id || '') + '：还原 ' +
                    num(((undo.data || {}).sessions || []).length) + ' 条归属、' +
                    num(((undo.data || {}).projects || []).length) + ' 个项目动作'
                }),
                jsx('div', {
                  key: 'h',
                  className: 'hc-agg2__note',
                  'data-agg2-undo-hash': JSON.stringify(((undo.data || {}).hashes || {}).content_match || {}),
                  children:
                    (undo.data || {}).byte_identical
                      ? '三库内容哈希与应用前一致，projects.db / assignments.db 逐字节回到应用前（byte_identical=true）'
                      : '撤销完成，但三库哈希没有全部回到应用前：' +
                        JSON.stringify(((undo.data || {}).hashes || {}).content_match || {})
                }),
                ((undo.data || {}).notes || []).length
                  ? jsx('div', {
                      key: 'n',
                      className: 'hc-agg2__notes',
                      children: ((undo.data || {}).notes || []).map(function (note, index) {
                        return jsx('div', { className: 'hc-agg2__note', children: note }, 'un' + index)
                      })
                    })
                  : null
              ]
            })
          : null
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-agg2__mode' + (rebuild ? ' hc-agg2__mode--danger' : ''),
    'data-agg2-mode-panel': '1',
    'data-agg2-mode-active': mode,
    children: [
      jsxs('div', {
        key: 'row',
        className: 'hc-agg2__mode-row',
        children: [
          jsx('span', { key: 'l', className: 'hc-agg2__mode-label', children: '模式' }),
          renderModes(),
          jsx('button', {
            key: 'p',
            type: 'button',
            className: 'hc-btn ' + (rebuild ? 'hc-btn--danger' : 'hc-btn--accent'),
            'data-agg2-plan-preview': mode,
            disabled: busy,
            title: '只读预览（零写入，可以随便点）；先看清会动什么再确认',
            onClick: preview,
            children: plan.phase === 'loading' ? '预览中…' : planData ? '重新预览' : '先预览'
          }),
          jsx('span', {
            key: 'z',
            className: 'hc-agg2__mode-zero',
            'data-agg2-mode-zero-write': '1',
            children: '「先预览」零写入：只算计划，不改归属 / 项目 / cwd'
          })
        ]
      }),
      rebuild
        ? jsx('div', {
            key: 'warn',
            className: 'hc-agg2__mode-warn',
            'data-agg2-mode-warn': '1',
            role: 'alert',
            children: REBUILD_WARNING
          })
        : jsx('div', {
            key: 'safe',
            className: 'hc-agg2__mode-safe',
            'data-agg2-mode-safe': '1',
            children: '安全模式：只给未分组会话归类，已分组会话的归属一个字段都不动。'
          }),
      jsx('div', { key: 'plan', className: 'hc-agg2__mode-body', children: renderPlan() }),
      jsx('div', { key: 'applied', className: 'hc-agg2__mode-body', children: renderApplied() })
    ]
  })
}
