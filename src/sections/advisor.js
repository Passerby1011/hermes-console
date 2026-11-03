/**
 * hermes-console · Advisor 审核台（C9b）。
 *
 * 本分区回答一个问题：「advisor 提了哪些建议，我采纳了哪些、拒了哪些、为什么，策略是什么」。
 * 数据全部来自后端 `/advisor/*`（它再去读 C9a 的 `plugins/advisor/audit.py` 台账），
 * 本文件**不自己拼数据、不读日志、不猜状态**。
 *
 * 六条规矩：
 *   1. **口径写脸上**：状态条四个数字各自带一句口径（数字来自台账哪一列），不让人猜
 *      「已采纳」是建议条数还是事件条数；
 *   2. **单向阀门只读**：`blocker_forced_delivery` 以只读徽标展示，并写明「程序强制，不可关闭」；
 *      本分区没有任何关闭 blocker 提示 / 降级 blocker 的入口（后端也会 400 拒掉）；
 *   3. **决定只写决定**：采纳/拒绝只 POST 一条决定，不执行建议内容、不改任何代码或配置；
 *      拒绝必须填原因，**前端拦一次、后端再拦一次**（两边都做，别指望对方）；
 *   4. **三态复用** lib/states.js：Skeleton（加载）/ Empty（还没有建议）/ ErrorState（含后端
 *      error.code 与原因），不另写一套；
 *   5. **窄窗折叠成卡片**：列表行在窄容器里从表格变卡片，任何一档都不出现横向滚动条
 *      （样式见 style.css 的 `.hc-adv__*` + `@container hc`）；
 *   6. 颜色只走 var(--ui-*)；本文件不出现字面色值（构建脚本会检查）。
 *
 * 自包含：不 import 其它分区（`autogroup.js` / `overview.js`）的任何内部实现 ——
 * 将来本分区要整体搬进模块化平台的 `modules/advisor/`，不能带走别人的私有函数。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { absDate, clip, num, relTime } from '../lib/format.js'
import { ProviderModelPicker } from '../lib/provider-model-picker.js'
import { useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 每页条数。 */
var PAGE_SIZE = 20

/** 缓存窗口（切分区回来不重拉；点决定后会主动 reload）。 */
var STALE_MS = 15000

/** 列表正文摘要的截断长度（全文在详情里，列表只给一眼能扫的量）。 */
var NOTE_CLIP = 96

var SEVERITY_LABEL = { nit: 'NIT', concern: 'CONCERN', blocker: 'BLOCKER' }

var STATUS_LABEL = {
  pending: '待处理',
  delivered: '已投递',
  accepted: '已采纳',
  rejected: '已拒绝',
  dropped: '已丢弃'
}

var EVENT_LABEL = {
  raised: '提出',
  delivered: '投递',
  held: '暂扣',
  reconfirmed: '再次提出',
  dropped: '丢弃',
  accepted: '采纳',
  rejected: '拒绝'
}

/** 筛选下拉：空串 = 全部。 */
var STATUS_FILTERS = [
  { id: '', label: '全部状态' },
  { id: 'pending', label: '待处理' },
  { id: 'delivered', label: '已投递' },
  { id: 'accepted', label: '已采纳' },
  { id: 'rejected', label: '已拒绝' },
  { id: 'dropped', label: '已丢弃' }
]

var SEVERITY_FILTERS = [
  { id: '', label: '全部 severity' },
  { id: 'nit', label: 'NIT' },
  { id: 'concern', label: 'CONCERN' },
  { id: 'blocker', label: 'BLOCKER' }
]

// ---------------------------------------------------------------------------
// 取数（全部走 lib/rest.js：失败不抛，返回 {ok:false, code, message}）
// ---------------------------------------------------------------------------
function fetchSummary(agent) {
  return get('/advisor/summary', { params: { agent: agent || '' } })
}

function fetchReviews(agent) {
  return get('/advisor/reviews', { params: { agent: agent || '', limit: 20 } })
}

function fetchList(filters, offset) {
  return get('/advisor/advice', {
    params: {
      agent: filters.agent,
      status: filters.status,
      severity: filters.severity,
      q: filters.q,
      limit: PAGE_SIZE,
      offset: offset
    }
  })
}

function fetchDetail(id) {
  return get('/advisor/advice/' + encodeURIComponent(id))
}

function fetchSettings(profile) {
  // 选哪个 profile 用 ?agent=：?profile= 会被宿主当成「二级 profile 路由」选择器重绑 scope，
  // 于是本插件的 API 宿主在该 scope 下不存在，宿主直接 404 Plugin not found。
  return get('/advisor/settings', { params: { agent: profile || 'default' } })
}

function fetchCoverage() {
  return get('/advisor/coverage')
}

function fetchModes(agent) {
  return get('/advisor/modes', { params: { agent: agent || '' } })
}

// ---------------------------------------------------------------------------
// 文案表（模式 / 评审结果 / 结构自检）
// ---------------------------------------------------------------------------
/** 评审流水的四种终态 + 飞行中。 */
var OUTCOME_LABEL = {
  advised: '有建议',
  no_advice: '无建议',
  error: '出错',
  skipped: '跳过',
  started: '只有开始（无终态）'
}

/** 错误归类 → 人话（后端也会给 readable，这里只是给徽标用的短标签）。 */
var ERROR_KIND_LABEL = {
  model_timeout: '模型超时',
  rate_limit: '被限流',
  auth: '鉴权失败',
  bad_request: '请求被拒',
  server_error: '上游 5xx',
  network: '网络不通',
  other: '其它错误'
}

/** 结构自检的项：名字 + 它到底检查什么（检测不到就直说，不替建议圆场）。 */
var STRUCTURE_FIELDS = [
  { id: 'target', label: '点名对象', hint: '正文里出现卡 id / 会话 id / comment N / 文件:行 —— 与插件闸门同一口径；引号原文只作参考，不算点名' },
  { id: 'goal', label: 'goal', hint: '行首有 goal: / 目标： 标签（插件 prompt 的四要素之一）' },
  { id: 'evidence', label: 'evidence', hint: '行首有 evidence: / 证据： 标签' },
  { id: 'impact', label: 'impact', hint: '行首有 impact: / 影响： 标签' },
  { id: 'next', label: 'next', hint: '行首有 next: / 下一步： 标签' }
]

/**
 * target 的三态文案：与后端 api_advisor._note_structure 的 target_state 一致。
 * 「只引了原话」不是「点了名」——插件闸门只认四种识别形态，引号原文会被拦下。
 */
var TARGET_STATE_LABEL = {
  named: '点名了',
  quoted_only: '只引了原话',
  none: '没点名'
}

/** 评审模式设置值的来源层（后端 values_detail 的 source，只有两层）。 */
var MODE_SETTING_SOURCE_LABEL = {
  default: '默认（settings.json 里没有 mode 键）',
  settings_file: '该 Agent 的 settings.json'
}

/** 本轮生效值是怎么来的：强制设定 / 最近一次评审记录 / 还没有评审记录。 */
var MODE_RESOLVED_SOURCE_LABEL = {
  forced: '设置强制',
  last_review: '最近一次评审记录',
  no_review: '还没有评审记录，按无目标推导',
  error: '读取失败'
}

/** 生效值的短标签（表格里用）。 */
var MODE_RESOLVED_LABEL_SHORT = {
  goal_aligned: '目标对齐',
  conservative: '保守',
  unknown: '未知'
}

function targetStateOf(structure) {
  if (!structure) return 'none'
  if (structure.target_state) return structure.target_state
  return structure.has_target === true ? 'named' : 'none'
}

function structureHas(structure, field) {
  if (!structure) return false
  if (field === 'target') return structure.has_target === true
  return (structure.labels || []).indexOf(field) > -1
}

// ---------------------------------------------------------------------------
// 小组件
// ---------------------------------------------------------------------------
/** severity 徽标：三档视觉强度不同（描边 → 实心描边 → 粗描边 + 加粗），不只靠颜色。 */
function SeverityBadge(props) {
  var severity = props.severity || 'concern'
  return jsx('span', {
    className: 'hc-adv__sev hc-adv__sev--' + severity,
    'data-severity': severity,
    title: 'advisor 判定的严重度：' + (SEVERITY_LABEL[severity] || severity),
    children: SEVERITY_LABEL[severity] || severity
  })
}

/** 每个 agent 的台账状态：有建议 / 有台账但无建议 / 没有台账文件。 */
var AGENT_STATE_LABEL = {
  has_advice: '有建议',
  no_advice: '有台账 · 无建议',
  no_ledger: '无台账文件'
}

// ---------------------------------------------------------------------------
// 覆盖面（GET /advisor/coverage）
// ---------------------------------------------------------------------------
/**
 * 状态条上的人话标签（含防御性态 ledger_no_delivery）。
 *
 * 口径（t_5dbc60b8）：这几个词说的是「有没有真产出 / 能不能投」，**不是开关开没开**。
 * 所以不再用「在用 / 有评审·无台账 / 装了未启用」这种把两件事混在一起的写法：
 *   active              台账有事件 + 允许注入          → 可用
 *   reviewing_only      启用 + 台账 0 条，但 sessions / 评审流水有记录 → 已启用 · 待首次产出
 *   installed_disabled  没启用，或启用了却完全零证据   → 未启用 / 无记录（卡片徽标按
 *                       enabled_in_config 再拆成「未启用」与「已启用 · 无记录」两档）
 */
var COVERAGE_LABEL = {
  active: '可用',
  reviewing_only: '已启用 · 待首次产出',
  installed_disabled: '未启用 / 无记录',
  not_installed: '未安装',
  ledger_no_delivery: '有台账 · 不能投递',
  unknown: '读失败'
}

/** 状态条的显示顺序：先说「可用」，再说「有问题」，最后「未安装」。 */
var COVERAGE_ORDER = ['active', 'reviewing_only', 'ledger_no_delivery', 'installed_disabled', 'not_installed']

var COVERAGE_CALIBER = {
  active: '台账有事件且 allow_gateway_injection=true（建议产得出、投得进）',
  reviewing_only: '已启用、在评审（sessions / 评审流水有记录），但台账还是 0 条 —— 台账要等插件在进程里真正写出第一条事件',
  ledger_no_delivery: '台账在长，但 allow_gateway_injection 不是 true，建议投不进会话',
  installed_disabled: '没启用（config 的 plugins.enabled 里没有 advisor），或启用了却完全零证据（sessions / 评审流水 / 台账全为 0）',
  not_installed: '该 profile 连 plugins/advisor 目录都没有'
}

var GOAL_SOURCE_LABEL = {
  kanban: '看板卡',
  user: '用户目标',
  first_request: '首个请求',
  unknown: '未知目标'
}

/** 目标来源：缺失/未知时显示「未知目标」，不报错。 */
function goalLabel(source) {
  return GOAL_SOURCE_LABEL[source] || GOAL_SOURCE_LABEL.unknown
}

/**
 * 卡片徽标文案（逐条对表，t_5dbc60b8）。
 *
 * **徽标算的是「有没有真产出」，不是「开关开没开」**：三个开关全开但台账 0 条，
 * 徽标写「部分可用」会让人以为坏了 —— 它其实只是「还没产出第一条」。
 * `installed_disabled` 一个后端态对应两种完全不同的现实，靠 `enabled_in_config` 拆开：
 *   enabled_in_config=true  → 已启用 · 无记录（启用了，但零证据）
 *   enabled_in_config=false → 未启用（插件根本不会被加载）
 */
var COVERAGE_SUMMARY_LABEL = {
  active: '可用',
  reviewing_only: '已启用 · 待首次产出',
  not_installed: '未安装',
  ledger_no_delivery: '有台账 · 不能投递',
  unknown: '读失败'
}

/** 徽标文案：`installed_disabled` 必须看 `enabled_in_config` 才分得清「未启用」和「无记录」。 */
function coverageSummaryLabel(row) {
  var status = (row && row.status) || 'unknown'
  if (status === 'installed_disabled') {
    return row && row.enabled_in_config ? '已启用 · 无记录' : '未启用'
  }
  return COVERAGE_SUMMARY_LABEL[status] || status
}

/** 徽标 tooltip：每个徽标自己说清「这个徽标在算什么」，别让人拿它当开关状态读。 */
function coverageBadgeTitle(row) {
  var status = (row && row.status) || 'unknown'
  if (status === 'installed_disabled') {
    return row && row.enabled_in_config
      ? '已启用 · 无记录：config 里启用了 advisor，但 sessions / 评审流水 / 台账都还是 0 —— 徽标算的是「有没有真产出」，不是开关开没开'
      : '未启用：config.yaml 的 plugins.enabled 里没有 advisor，插件不会被加载'
  }
  return COVERAGE_CALIBER[status] || ''
}

/** 状态点 / 总状态词的视觉档位：ok / partial / risk / none（只用 accent + warning 两档强调色）。 */
function coverageSummaryClass(status) {
  if (status === 'active') return 'ok'
  if (status === 'not_installed') return 'none'
  if (status === 'ledger_no_delivery' || status === 'unknown') return 'risk'
  return 'partial'
}

/**
 * 卡片第二行短描述：最多 4 个短词，用 · 分隔。
 *
 * `reviewOn` 是**评审开关的权威值**（settings.json 的实际 enabled，见
 * `settingsTruthByProfile`）；传 null 时才退回 coverage 行的字段。第二行不许和
 * 开关、徽标各说一套。
 */
function coverageFacts(row, reviewOn) {
  if (!row.plugin_installed) return ['未安装']
  var words = ['已安装']
  words.push(row.enabled_in_config ? '插件已启用' : '插件未启用')
  // 插件没启用时，评审/注入根本不会发生：写「评审已开启」会让第二行自相矛盾。
  if (!row.enabled_in_config) return words
  // 「启用但零证据」也算 installed_disabled（后端口径：0 sessions / 0 评审流水 / 0 台账）。
  // 这时第二行必须说「尚无评审记录」，否则写着「评审已开启」而徽标是「已启用 · 无记录」，
  // 用户会以为徽标错了。
  if (row.enabled_in_config && row.status === 'installed_disabled') {
    words.push('尚无评审记录')
    return words
  }
  words.push(reviewOn === true ? '评审已开启' : (reviewOn === false ? '评审已关闭'
    : (row.settings_enabled === false ? '评审已关闭' : '评审已开启')))
  words.push(row.injection_allowed ? '允许注入' : '关闭注入')
  return words
}

/**
 * 收起态主操作按状态收敛（硬要求，逐条对表）：
 *   未安装 → 安装；已装但插件未启用 → 启用插件；插件已启用但评审关 → 开启评审；
 *   已安装（前三项都就绪、但注入关 = 有台账投不出去）→ 同步副本；全开 → 无主操作（只留展开箭头）。
 *   已安装的 agent 收起态绝不出现「安装」。
 *   注：允许注入本身不靠这里，它在展开区是独立开关（老板明确要求可独立操作）；
 *   「同步副本」在展开区「其他操作」里也有，所以全开的行照样刷得动副本。
 */
function primaryAction(row, reviewOn) {
  if (!row.plugin_installed) return 'install'
  if (!row.enabled_in_config) return 'enable_plugin'
  var reviewOff = reviewOn === true ? false : (reviewOn === false ? true : row.settings_enabled === false)
  if (reviewOff) return 'enable_review'
  if (!row.injection_allowed) return 'sync'
  return ''
}

/** 覆盖状态徽标：只读状态词，纯色文字 + 极淡底（不再有带边框灰底）。 */
function CoverageBadge(props) {
  var row = props.row || { status: props.status }
  var status = row.status || 'unknown'
  var label = coverageSummaryLabel(row)
  return jsx('span', {
    className: 'hc-adv__cov-badge hc-adv__cov-badge--' + status,
    'data-coverage-status': status,
    'data-coverage-label': label,
    title: coverageBadgeTitle(row),
    children: label
  })
}

/**
 * 每个 profile 的「settings.json 实际值」——评审开关的唯一显示来源。
 *
 * 为什么不是直接用 coverage 行的 `settings_enabled`：那一列是 `/advisor/coverage` 自己
 * 直读 JSON 的结果，面板若只显示它，就**永远发现不了**「后端字段与文件不一致」
 * （本卡明确要求暴露，不许静默取一个）。
 *
 * `/advisor/modes` 不带 agent 时一次返回所有 profile 的落盘生效值（`settings[].enabled`），
 * 它走的是 advisor 自己的 `get_settings()`（ADVISOR_AUDIT_DIR 指到该 profile 后读
 * settings.json），与 `/advisor/coverage` 的直读 JSON 是**两条独立实现**。两条读到不一样，
 * 就说明有一边没如实反映文件 —— 这种不一致必须在界面上写出来，而不是挑一个显示。
 *
 * 返回 `{ profile: { enabled: bool, error: string } }`；某个 agent 没读到（报错/缺值）
 * 就不放进 map，调用侧据此显示「文件值未取到」，而不是拿面板字段冒充文件值。
 */
function settingsTruthByProfile(modesQuery) {
  var out = {}
  if (!modesQuery || modesQuery.status !== 'ok') return out
  var data = (modesQuery.data && modesQuery.data.data) || {}
  var settings = data.settings || []
  settings.forEach(function (row) {
    if (!row || !row.profile) return
    if (row.error) return
    if (row.enabled === undefined || row.enabled === null) return
    out[row.profile] = { enabled: row.enabled !== false, error: '' }
  })
  return out
}

/** 覆盖状态条：五档各几个，压成**一行**（数字 + 档名，口径进 tooltip）。
 *
 * 原来每格都带一整句口径说明，五格排下来就是一块高墙；口径是「这个数字怎么算的」，
 * 需要时看 tooltip 就够，不必常驻。矩阵自己一行一个 agent，这里只留汇总。
 */
function CoverageStatusBar(props) {
  var query = props.query
  if (query.status === 'loading') {
    return jsx(Skeleton, { cards: 1, lines: 2, label: '正在读取 advisor 覆盖面' })
  }
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '覆盖面没读到',
      message: query.error,
      detail: '接口 /advisor/coverage',
      onRetry: query.reload,
      retryLabel: '重试覆盖面'
    })
  }
  var data = (query.data && query.data.data) || {}
  var counts = data.status_counts || {}
  var rows = data.profiles || []
  var reviewing = rows.filter(function (row) {
    return row.status === 'reviewing_only'
  })
  return jsxs('div', {
    className: 'hc-adv__coverage',
    'data-coverage-bar': rows.length,
    children: [
      jsxs('div', {
        className: 'hc-adv__cov-strip',
        children: COVERAGE_ORDER.map(function (status) {
          var count = counts[status] || 0
          return jsxs('div', {
            className: 'hc-adv__cov-cell hc-adv__cov-cell--' + status
              + (count === 0 ? ' hc-adv__cov-cell--zero' : ''),
            'data-coverage-count': status,
            title: COVERAGE_CALIBER[status] || '',
            children: [
              jsx('span', { className: 'hc-adv__cov-num', children: num(count) }, 'n'),
              jsx('span', { className: 'hc-adv__cov-label', children: COVERAGE_LABEL[status] }, 'l')
            ]
          }, status)
        })
      }, 'strip'),
      reviewing.length
        ? jsx('div', {
            className: 'hc-adv__cov-alert',
            role: 'status',
            'data-coverage-alert': reviewing.length,
            children:
              '插件已加载但台账为空：该 profile 的运行进程可能仍是旧代码，需重启该 profile 的 '
              + 'gateway/serve 才会写入台账。受影响：'
              + reviewing.map(function (row) {
                return row.profile
              }).join(' / ')
          }, 'alert')
        : null
    ]
  })
}

/** 覆盖矩阵（卡片式）：每个 agent 一张横向卡片——收起态一行状态 + 主操作，
 *  展开后是三个权限开关 + 次级动作 + 路径/版本/台账/诊断。写操作与权限开关同一套两段式。 */
var COVERAGE_ACTION_LABEL = {
  install: '安装', sync: '同步副本', enable_plugin: '启用插件', disable_plugin: '停用插件',
  enable_review: '启用评审', disable_review: '停用评审',
  enable_injection: '允许注入', disable_injection: '关闭注入', uninstall: '卸载'
}

/** 注入动作在确认框里必须原样说清的后果（不许只写「切换开关」）。 */
var INJECTION_EFFECT = {
  enable_injection:
    '允许 = 评审出的建议会投进会话（会打断/提示）；关闭 = 只记账不打扰。'
    + '这一步只改 config.yaml 的 plugins.entries.advisor.allow_gateway_injection，'
    + '不改「安装」「插件启用」「评审启用」任何一项。'
    + '注意：评审没开时打开注入没有意义（不会报错，但也不会有建议可投）。',
  disable_injection:
    '关闭 = 评审出的建议只写进台账、不再投进会话（只记账不打扰）。'
    + '这一步只改 config.yaml 的 plugins.entries.advisor.allow_gateway_injection，'
    + '「安装」「插件启用」「评审启用」都保持不变。'
}

function covYesNo(value) {
  return value ? '是' : '否'
}

function covReviewWord(value) {
  return value === false ? '关' : '开'
}

/** 动作二次确认框：先预览变更清单（会改什么 / 不会改什么），再确认执行并展示改前→改后。 */
function CoverageActionDialog(props) {
  var d = props.dialog || {}
  var plan = d.plan || {}
  var result = d.result || null
  var plans = plan.plans || []
  var changes = []
  plans.forEach(function (p) {
    (p.changes || []).forEach(function (c) {
      changes.push({ profile: p.profile, path: c.path, field: c.field, before: c.before, after: c.after })
    })
  })
  var untouched = plan.untouched || []
  var blocked = plan.blocked || []
  var label = COVERAGE_ACTION_LABEL[d.action] || d.action
  // 后端在 plan 里给每条 profile 的 notices（依赖关系提示）；去重后统一展示
  var notices = []
  plans.forEach(function (p) {
    (p.notices || []).forEach(function (n) {
      if (notices.indexOf(n) === -1) notices.push(n)
    })
    if (p.notice && notices.indexOf(p.notice) === -1) notices.push(p.notice)
  })
  var effect = INJECTION_EFFECT[d.action] || ''
  // 就地面板（不再盖表格）：渲染在被操作卡片的展开区内，被改的那一行始终在面板上方可见。
  return jsxs('div', {
    className: 'hc-adv__cov-panel',
    role: 'dialog',
    'data-cov-dialog': d.action,
    children: [
      jsx('div', {
        className: 'hc-adv__modal-title',
        children: '确认：' + label + '（' + (d.profile || ((d.profiles || [])[0]) || '?') + '）'
      }, 'title'),
        d.loading ? jsx('div', { className: 'hc-adv__hint', children: '正在生成变更清单…' }, 'loading') : null,
        d.error ? jsx('div', { className: 'hc-adv__formerr', role: 'alert', children: d.error }, 'err') : null,
        (!d.loading && !d.error && !result)
          ? jsxs('div', {
              className: 'hc-adv__modal-body',
              children: [
                jsx('div', { className: 'hc-adv__modal-sec', children: '将修改：' }, 'sec1'),
                plan.client_preview
                  ? jsx('div', {
                      className: 'hc-adv__hint',
                      'data-cov-client-preview': d.action,
                      children: '该动作没有后端预览（危险动作后端要求直接确认），以下是按同一口径在本机算出的影响范围。'
                    }, 'clientpreview')
                  : null,
                effect
                  ? jsx('div', {
                      className: 'hc-adv__gate-note',
                      'data-cov-effect': d.action,
                      children: effect
                    }, 'effect')
                  : null,
                notices.length
                  ? jsxs('div', {
                      'data-cov-notices': notices.length,
                      children: [
                        jsx('div', { className: 'hc-adv__modal-sec', children: '依赖与后果：' }, 'secn'),
                        jsx('ul', {
                          className: 'hc-adv__modal-list',
                          children: notices.map(function (n, i) {
                            return jsx('li', { children: n }, 'nt' + i)
                          })
                        }, 'notices')
                      ]
                    }, 'notices-wrap')
                  : null,
                changes.length
                  ? jsx('ul', {
                      className: 'hc-adv__modal-list',
                      'data-cov-changes': changes.length,
                      children: changes.map(function (c, i) {
                        return jsx('li', {
                          children: c.profile + ' · ' + c.path + ' · ' + c.field + '：'
                            + String(c.before) + ' → ' + String(c.after)
                        }, 'ch' + i)
                      })
                    }, 'changes')
                  : jsx('div', { className: 'hc-adv__hint', children: '（没有可修改项）' }, 'nochange'),
                blocked.length
                  ? jsxs('div', {
                      children: [
                        jsx('div', { className: 'hc-adv__modal-sec', children: '被跳过（不可用）：' }, 'secb'),
                        jsx('ul', {
                          className: 'hc-adv__modal-list',
                          children: blocked.map(function (b, i) {
                            return jsx('li', { children: b.profile + '：' + b.reason }, 'bl' + i)
                          })
                        }, 'blocked')
                      ]
                    }, 'blocked-wrap')
                  : null,
                jsxs('div', {
                  children: [
                    jsx('div', { className: 'hc-adv__modal-sec', children: '不会修改：' }, 'secu'),
                    jsx('div', {
                      className: 'hc-adv__hint',
                      'data-cov-untouched': untouched.length,
                      children: untouched.length ? untouched.join(' / ') : '（无其它 profile）'
                    }, 'untouched')
                  ]
                }, 'untouched-wrap'),
                d.action === 'uninstall'
                  ? jsx('div', {
                      className: 'hc-adv__formerr',
                      children: '注意：卸载会删除这些 profile 的 plugins/advisor 目录（不可撤销）。'
                    }, 'danger')
                  : null
              ]
            })
          : null,
        result
          ? jsxs('div', {
              className: 'hc-adv__modal-body',
              children: [
                jsx('div', { className: 'hc-adv__modal-sec', children: '执行结果（改前 → 改后）：' }, 'rsec'),
                jsx('ul', {
                  className: 'hc-adv__modal-list',
                  'data-cov-transitions': (result.transitions || []).length,
                  children: (result.transitions || []).map(function (t, i) {
                    var b = t.before || {}
                    var a = t.after || {}
                    return jsx('li', {
                      children: t.profile + '：' + b.status + '（安装 ' + covYesNo(b.plugin_installed)
                        + ' · 插件 ' + covYesNo(b.enabled_in_config) + ' · 评审 ' + covReviewWord(b.settings_enabled)
                        + '） → ' + a.status + '（安装 ' + covYesNo(a.plugin_installed)
                        + ' · 插件 ' + covYesNo(a.enabled_in_config) + ' · 评审 ' + covReviewWord(a.settings_enabled) + '）'
                    }, 'tr' + i)
                  })
                }, 'transitions'),
                jsx('div', {
                  className: 'hc-adv__hint',
                  children: result.untouched_unchanged
                    ? '未点名的 profile 全部零变化。'
                    : '警告：未点名的 profile 出现了变化（不应发生，请检查）。'
                }, 'rnote')
              ]
            })
          : null,
        jsxs('div', {
          className: 'hc-adv__modal-actions',
          children: [
            jsx('button', {
              type: 'button',
              className: 'hc-btn',
              disabled: props.busy,
              onClick: props.onCancel,
              children: result ? '关闭' : '取消'
            }, 'cancel'),
            (!result && !d.loading && !d.error && changes.length)
              ? jsx('button', {
                  type: 'button',
                  className: 'hc-btn hc-btn--accent',
                  'data-cov-confirm': 'yes',
                  disabled: props.busy,
                  onClick: props.onConfirm,
                  children: props.busy ? '执行中…' : '确认执行'
                }, 'confirm')
              : null
          ]
        }, 'actions')
    ]
  })
}

/** 权限开关：显示当前开/关，点击走同一套两段式（preview → 确认 → apply → 回读）。 */
function CoverageSwitch(props) {
  var action = props.on ? props.offAction : props.onAction
  var cls = 'hc-adv__cov-toggle' + (props.on ? ' hc-adv__cov-toggle--on' : '')
  return jsxs('div', {
    className: 'hc-adv__cov-switch',
    'data-cov-switch': props.id,
    'data-cov-switch-state': props.on ? 'on' : 'off',
    children: [
      jsx('button', {
        type: 'button',
        className: cls,
        'data-cov-action': action,
        'data-cov-profile': props.profile,
        disabled: !props.enabled || props.busy,
        title: props.enabled
          ? ('对 ' + props.profile + '：' + COVERAGE_ACTION_LABEL[action])
          : ('当前状态不允许（' + props.blockedHint + '）'),
        onClick: function () { props.onOpen(action, props.profile) },
        children: props.on ? '开' : '关'
      }, 'btn'),
      jsxs('div', {
        className: 'hc-adv__cov-switch-text',
        children: [
          jsx('span', { className: 'hc-adv__cov-switch-label', children: props.label }, 'l'),
          jsx('span', { className: 'hc-adv__cov-switch-hint', children: props.hint }, 'h')
        ]
      }, 'text')
    ]
  })
}

function CoverageMatrix(props) {
  var query = props.query
  var onChanged = props.onChanged || function () {}
  var selected = props.selected || ''
  var dlgState = useState(null)   // { action, profile, profiles, loading, plan, result, error }
  var dialog = dlgState[0]
  var setDialog = dlgState[1]
  var busyState = useState(false)
  var busy = busyState[0]
  var setBusy = busyState[1]
  var openState = useState({})    // profile -> true/false（展开态）
  var openCards = openState[0]
  var setOpenCards = openState[1]
  // 评审开关的真值来源：一次拿到**所有** profile 的 settings.json 生效值
  // （/advisor/modes 不带 agent 就是全量）。hooks 必须无条件调用，所以放在 early return 之前。
  var truthQuery = useQuery('advisor.modes|', function () { return fetchModes('') }, { staleMs: STALE_MS })

  if (query.status === 'loading') return jsx(Skeleton, { lines: 6, label: '正在读取覆盖矩阵' })
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '覆盖矩阵没读到',
      message: query.error,
      detail: '接口 /advisor/coverage',
      onRetry: query.reload,
      retryLabel: '重试矩阵'
    })
  }
  var data = (query.data && query.data.data) || {}
  var rows = data.profiles || []
  if (!rows.length) {
    return jsx(Empty, { title: '没有 profile', hint: '主目录下没有扫描到任何 profile 目录' })
  }
  var truthByProfile = settingsTruthByProfile(truthQuery)

  function ensureOpen(profile) {
    setOpenCards(function (m) {
      if (m[profile]) return m
      var n = Object.assign({}, m); n[profile] = true; return n
    })
  }
  function toggleCard(profile) {
    setOpenCards(function (m) { var n = Object.assign({}, m); n[profile] = !m[profile]; return n })
  }

  /**
   * 覆盖矩阵的写操作。
   *
   * 为什么不直接 `send()`：桌面端的 `ctx.rest` 会经 `profileScoped()` 自动带上
   * `?profile=<当前 agent>`。实测（真登录态、同一会话）：
   *   无 profile        → 200
   *   ?profile=default  → 200
   *   ?profile=work     → 404 {"detail":"Plugin not found"}
   * 宿主的 profile 闸门（hermes_cli/web_server_dashboard.py::_plugin_api_request_refused）
   * 会拒绝「二级 profile 去用启动 profile 的 user 插件」，而本插件正是装在根
   * plugins/ 下的 user 插件，所以只要请求被绑到别的 agent 就必然 404。
   *
   * 本插件的自作用域参数是 `?agent=`，从不依赖 `?profile=`。所以这里直接走
   * Electron 桥发的裸请求（不带 profile），避开那道闸门。
   * 失败时把**原始错误 + 实际 URL** 带进界面，不再笼统显示成「接口不存在」。
   */
  var COVERAGE_ACTION_PATH = '/api/plugins/hermes-console/advisor/coverage/action'

  async function coverageAction(body) {
    var bridge = typeof window !== 'undefined' && window.hermesDesktop ? window.hermesDesktop.api : null
    if (typeof bridge !== 'function') {
      return {
        ok: false,
        message: COVERAGE_ACTION_PATH + ' · 桌面桥不可用（window.hermesDesktop.api 缺失）'
      }
    }
    try {
      var data = await bridge({
        path: COVERAGE_ACTION_PATH,
        method: 'POST',
        body: body
      })
      if (data && data.ok === false) {
        var err = data.error || {}
        return {
          ok: false,
          message: COVERAGE_ACTION_PATH + ' · 后端拒绝：'
            + (err.code || '?') + ' ' + (err.message || JSON.stringify(data).slice(0, 200))
        }
      }
      return { ok: true, data: data }
    } catch (e) {
      return {
        ok: false,
        message: COVERAGE_ACTION_PATH + ' · 请求失败：' + String((e && e.message) || e)
      }
    }
  }

  function openDialog(action, profile) {
    if (!profile) return
    ensureOpen(profile)
    // 卸载走**客户端预览**（零请求）：后端把「卸载」当危险动作，连 mode=preview 都要求
    // confirm=true（否则 400 invalid_argument），所以界面上不可能拿到后端 plan。
    // 这里按同一份字段口径（目录 / 改前 / 改后）自己算出来给人看，用户点「确认执行」
    // 才发那一次 apply+confirm=true 的请求——两步式不破，后端契约也不用动。
    if (action === 'uninstall') {
      var target = null
      rows.forEach(function (r) { if (r.profile === profile) target = r })
      var others = rows.filter(function (r) { return r.profile !== profile })
        .map(function (r) { return r.profile })
      setDialog({
        action: action, profile: profile, profiles: [profile], loading: false, result: null, error: '',
        plan: {
          client_preview: true,
          plans: [{
            profile: profile,
            changes: [{
              path: (target && target.plugin_dir) || '（该 profile 的 plugins/advisor）',
              field: '（删除目录）',
              before: '存在',
              after: '删除'
            }]
          }],
          untouched: others
        }
      })
      return
    }
    setDialog({ action: action, profile: profile, profiles: [profile], loading: true, plan: null, result: null, error: '' })
    coverageAction({ action: action, profiles: [profile], mode: 'preview' }).then(function (res) {
      if (!res || res.ok === false) {
        setDialog({
          action: action, profile: profile, profiles: [profile], loading: false, plan: null, result: null,
          error: (res && res.message) || '预览失败'
        })
        return
      }
      var body = res.data || {}
      var inner = body.data || body
      setDialog({ action: action, profile: profile, profiles: [profile], loading: false, plan: inner, result: null, error: '' })
    })
  }
  function confirmDialog() {
    if (!dialog) return
    setBusy(true)
    coverageAction({
      action: dialog.action, profiles: [dialog.profile], mode: 'apply', confirm: true
    }).then(function (res) {
      setBusy(false)
      if (!res || res.ok === false) {
        setDialog(Object.assign({}, dialog, { error: (res && res.message) || '执行失败' }))
        return
      }
      var body = res.data || {}
      var inner = body.data || body
      setDialog(Object.assign({}, dialog, { result: inner, error: '' }))
      onChanged()
      if (query.reload) query.reload()
      // 评审开关的真值来自 `advisor.modes|`（全量 profile 的 settings.json 生效值）。
      // 写完必须让它重取：否则「开启评审」之后同一张卡里的「关闭评审」仍是 disabled，
      // 界面会一直停在写之前的状态（用户以为没生效）。
      truthQuery.reload()
    })
  }

  var cards = rows.map(function (row) {
    var expanded = !!openCards[row.profile]
    var busyForRow = busy && dialog && dialog.profile === row.profile
    var sumCls = coverageSummaryClass(row.status)
    // 评审开关：**以 settings.json 的实际值为准**（fileEnabled），拿不到才退回 coverage 行的字段。
    // 两者不一致 = 后端有一边没如实反映文件 → 界面上直接标出来（见展开区的提示行）。
    var truth = truthByProfile[row.profile] || null
    var fileEnabled = truth ? truth.enabled : null
    var panelEnabled = row.settings_enabled !== false
    var settingsMismatch = fileEnabled !== null && fileEnabled !== panelEnabled
    var reviewOn = fileEnabled !== null ? fileEnabled : panelEnabled
    var primary = primaryAction(row, reviewOn)
    var facts = coverageFacts(row, reviewOn)
    return jsxs('div', {
      className: 'hc-adv__cov-card' + (expanded ? ' hc-adv__cov-card--open' : '')
        + (selected && selected === row.profile ? ' hc-adv__cov-card--selected' : ''),
      'data-coverage-profile': row.profile,
      'data-coverage-status': row.status,
      'data-cov-primary': primary || 'none',
      'data-cov-settings-mismatch': settingsMismatch ? 'yes' : 'no',
      // 诊断原文挂在卡片上（tooltip 里也有）：收起态一行不显示它，但「需重启 gateway/serve」
      // 这类可操作结论必须仍能从这一行取到，而不是只有点开详情才看得见。
      'data-cov-diagnosis': row.diagnosis || '',
      children: [
        // ---- 收起态：**一行**（名字 + 状态徽标 + 一行事实 + 主操作 + 展开箭头）----
        jsxs('div', {
          className: 'hc-adv__cov-card-head',
          children: [
            jsx('span', {
              className: 'hc-adv__cov-dot hc-adv__cov-dot--' + sumCls,
              'aria-hidden': 'true',
              title: row.diagnosis || ''
            }, 'dot'),
            jsx('span', { className: 'hc-adv__cov-name', title: row.profile_home || '', children: row.profile }, 'name'),
            jsx(CoverageBadge, { row: row }, 'sum'),
            jsx('span', {
              className: 'hc-adv__cov-facts',
              children: facts.map(function (w, i) {
                return jsx('span', { className: 'hc-adv__cov-fact', children: w }, 'f' + i)
              })
            }, 'facts'),
            jsxs('div', {
              className: 'hc-adv__cov-side',
              children: [
                busyForRow
                  ? jsx('span', { className: 'hc-adv__cov-running', 'data-cov-running': row.profile, children: '执行中…' }, 'running')
                  : (primary
                      ? jsx('button', {
                          type: 'button',
                          className: 'hc-btn hc-btn--accent hc-btn--sm hc-adv__cov-primary',
                          'data-cov-action': primary,
                          'data-cov-profile': row.profile,
                          disabled: busy,
                          title: '对 ' + row.profile + ' 执行：' + COVERAGE_ACTION_LABEL[primary],
                          onClick: function () { openDialog(primary, row.profile) },
                          children: COVERAGE_ACTION_LABEL[primary]
                        }, 'primary')
                      : null),
                jsx('button', {
                  type: 'button',
                  className: 'hc-adv__cov-expand',
                  'data-cov-expand': row.profile,
                  'aria-expanded': expanded,
                  title: expanded ? '收起四维详情' : '展开：插件运行 / 参与评审 / 允许注入 / 其它操作',
                  onClick: function () { toggleCard(row.profile) },
                  children: expanded ? '▴' : '▾'
                }, 'expand')
              ]
            }, 'side')
          ]
        }, 'head'),
        expanded
          ? jsxs('div', {
              className: 'hc-adv__cov-body',
              'data-cov-body': row.profile,
              children: [
                jsxs('div', {
                  className: 'hc-adv__cov-section',
                  children: [
                    jsx('div', { className: 'hc-adv__cov-sec-title', children: '权限（四个维度独立、互不联动）' }, 't'),
                    jsx(CoverageSwitch, {
                      id: 'plugin', label: '插件运行', hint: '该 Agent 是否加载 Advisor 插件',
                      on: !!row.enabled_in_config, enabled: !!row.plugin_installed, blockedHint: '先安装',
                      onAction: 'enable_plugin', offAction: 'disable_plugin',
                      profile: row.profile, busy: busy, onOpen: openDialog
                    }, 'sw-plugin'),
                    jsx(CoverageSwitch, {
                      id: 'review', label: '参与评审', hint: '是否收集该 Agent 的评审数据',
                      on: reviewOn, enabled: !!row.plugin_installed && !!row.enabled_in_config, blockedHint: '先启用插件',
                      onAction: 'enable_review', offAction: 'disable_review',
                      profile: row.profile, busy: busy, onOpen: openDialog
                    }, 'sw-review'),
                    jsx(CoverageSwitch, {
                      id: 'injection', label: '允许注入', hint: '是否允许建议进入该 Agent 上下文',
                      on: !!row.injection_allowed, enabled: !!row.plugin_installed, blockedHint: '先安装',
                      onAction: 'enable_injection', offAction: 'disable_injection',
                      profile: row.profile, busy: busy, onOpen: openDialog
                    }, 'sw-injection'),
                    // 「参与评审」这个开关的取值必须能追到文件：写出 settings.json 的 enabled 值；
                    // 后端两条读盘路径给出不同答案时，这里变成警告条（不许静默挑一个显示）。
                    jsx('div', {
                      className: settingsMismatch ? 'hc-adv__cov-alert' : 'hc-adv__hint',
                      'data-cov-settings-note': row.profile,
                      'data-cov-settings-file': fileEnabled === null ? 'unknown' : String(fileEnabled),
                      'data-cov-settings-panel': panelEnabled ? 'true' : 'false',
                      title: row.settings_path || '',
                      children: settingsMismatch
                        ? ('文件与面板不一致：settings.json 里 enabled=' + (fileEnabled ? 'true' : 'false')
                          + '，而 /advisor/coverage 给的是 ' + (panelEnabled ? 'true' : 'false')
                          + ' —— 开关按文件值显示，请核对后端。文件：' + (row.settings_path || '—'))
                        : (fileEnabled === null
                          ? ('评审开关取值：没读到该 agent 的 settings.json（/advisor/modes 没给值），'
                            + '按 /advisor/coverage 的字段显示 —— 这不是文件真值。文件：' + (row.settings_path || '—'))
                          : (row.settings_exists === false
                            ? ('评审开关取值：settings.json 还不存在，enabled 用默认值 ' + (fileEnabled ? 'true' : 'false')
                              + '（文件：' + (row.settings_path || '—') + '）')
                            : ('评审开关取值：settings.json → enabled=' + (fileEnabled ? 'true' : 'false')
                              + '（' + (row.settings_path || '—') + '）')))
                    }, 'sw-review-truth')
                  ]
                }, 'sec-perm'),
                jsxs('div', {
                  className: 'hc-adv__cov-section',
                  children: [
                    jsx('div', { className: 'hc-adv__cov-sec-title', children: '其他操作' }, 't'),
                    jsxs('div', {
                      className: 'hc-adv__cov-ops',
                      children: [
                        // 「同步副本」放在这里而不是只做收起态主操作：全开的行没有主操作，
                        // 若只在那里出现，健康行就永远刷新不了副本（六类动作会缺一类）。
                        jsx('button', {
                          type: 'button', className: 'hc-btn hc-btn--sm',
                          'data-cov-action': 'sync', 'data-cov-profile': row.profile,
                          disabled: !row.plugin_installed || busy,
                          title: row.plugin_installed ? '用插件源目录覆盖该 profile 的运行期副本（不动 config / 台账）' : '未安装',
                          onClick: function () { openDialog('sync', row.profile) },
                          children: '同步副本'
                        }, 'op-sync'),
                        jsx('button', {
                          type: 'button', className: 'hc-btn hc-btn--sm',
                          'data-cov-action': 'disable_plugin', 'data-cov-profile': row.profile,
                          disabled: !row.plugin_installed || !row.enabled_in_config || busy,
                          title: row.plugin_installed ? '从 config 的 plugins.enabled 里去掉 advisor' : '未安装',
                          onClick: function () { openDialog('disable_plugin', row.profile) },
                          children: '停用插件'
                        }, 'op-plugin'),
                        jsx('button', {
                          type: 'button', className: 'hc-btn hc-btn--sm',
                          'data-cov-action': 'disable_review', 'data-cov-profile': row.profile,
                          disabled: !row.plugin_installed || !row.enabled_in_config || !reviewOn || busy,
                          title: reviewOn ? '把该 profile 的 settings.json enabled 置 false' : '评审本来就关着',
                          onClick: function () { openDialog('disable_review', row.profile) },
                          children: '关闭评审'
                        }, 'op-review'),
                        jsx('button', {
                          type: 'button', className: 'hc-btn hc-btn--sm',
                          'data-cov-action': 'disable_injection', 'data-cov-profile': row.profile,
                          disabled: !row.plugin_installed || !row.injection_allowed || busy,
                          title: row.injection_allowed ? '关闭建议注入（只改 allow_gateway_injection）' : '注入本来就关着',
                          onClick: function () { openDialog('disable_injection', row.profile) },
                          children: '关闭注入'
                        }, 'op-injection'),
                        jsx('button', {
                          type: 'button', className: 'hc-btn hc-btn--sm hc-btn--danger',
                          'data-cov-action': 'uninstall', 'data-cov-profile': row.profile,
                          disabled: !row.plugin_installed || busy,
                          title: row.plugin_installed ? '危险：删除该 profile 的 plugins/advisor 目录（不可撤销）' : '未安装',
                          onClick: function () { openDialog('uninstall', row.profile) },
                          children: '卸载'
                        }, 'op-uninstall')
                      ]
                    }, 'ops')
                  ]
                }, 'sec-ops'),
                jsx('details', {
                  className: 'hc-adv__cov-detail',
                  'data-coverage-diagnosis': row.status,
                  children: [
                    jsx('summary', { children: '路径 / 版本 / 台账 / 诊断' }, 'sum'),
                    jsxs('div', {
                      className: 'hc-adv__cov-detail-body',
                      children: [
                        jsx('div', { children: '插件目录：' + (row.plugin_dir || '—') + '　版本：' + (row.plugin_version || '?') }, 'd1'),
                        jsx('div', { children: 'config：' + (row.config_path || '—') + (row.config_error ? '（' + row.config_error + '）' : '') }, 'd2'),
                        jsx('div', { children: 'settings：' + (row.settings_path || '—') }, 'd3'),
                        jsx('div', { children: '台账：' + num(row.ledger_events) + ' 事件 · ' + num(row.ledger_advice) + ' 建议　sessions：' + num(row.session_files) + ' 个 · 暂扣 ' + num(row.held_notes) }, 'd4'),
                        jsx('div', { className: 'hc-adv__cov-diag', children: row.diagnosis || '' }, 'd5')
                      ]
                    }, 'body')
                  ]
                }, 'detail'),
                (dialog && dialog.profile === row.profile)
                  ? jsx(CoverageActionDialog, {
                      dialog: dialog, busy: busy,
                      onCancel: function () { setDialog(null) }, onConfirm: confirmDialog
                    }, 'panel')
                  : null
              ]
            })
          : null
      ]
    }, 'cov-' + row.profile)
  })

  return jsxs('div', {
    className: 'hc-adv__cov',
    'data-coverage-rows': rows.length,
    children: [
      jsxs('div', { className: 'hc-adv__cov-cards', children: cards }, 'cov-cards')
    ]
  })
}

function AgentStrip(props) {
  var agents = props.agents || []
  if (!agents.length) return null
  return jsxs('div', {
    className: 'hc-adv__agent-table',
    'data-agent-strip': agents.length,
    children: [
      jsxs('div', {
        className: 'hc-adv__agent-row hc-adv__agent-row--head',
        children: [
          jsx('span', { children: 'Agent' }, 'a'),
          jsx('span', { children: '台账状态' }, 'b'),
          jsx('span', { children: '建议' }, 'c'),
          jsx('span', { children: '评审（有 / 无 / 错 / 跳过）' }, 'd')
        ]
      }, 'agent-head')
    ].concat(agents.map(function (agent) {
      var label = AGENT_STATE_LABEL[agent.state] || agent.state
      var reviews = agent.review_outcomes || {}
      return jsxs('div', {
        className: 'hc-adv__agent-row',
        'data-agent': agent.profile,
        'data-agent-state': agent.state,
        title: agent.ledger || '',
        children: [
          jsx('span', { className: 'hc-adv__profile', children: agent.profile }, 'n'),
          jsx('span', { className: 'hc-adv__hint', children: label }, 's'),
          jsx('span', { className: 'hc-adv__hint', children: num(agent.advice_total || 0) + ' 条' }, 't'),
          jsx('span', {
            className: 'hc-adv__hint',
            children: num(agent.review_total || 0) + ' 次（'
              + num(reviews.advised || 0) + ' / ' + num(reviews.no_advice || 0) + ' / '
              + num(reviews.error || 0) + ' / ' + num(reviews.skipped || 0) + '）'
          }, 'r')
        ]
      }, 'agent-' + agent.profile)
    }))
  })
}

/** 状态药丸。 */
function StatusPill(props) {
  var status = props.status || 'pending'
  return jsx('span', {
    className: 'hc-adv__pill hc-adv__pill--' + status,
    'data-status': status,
    children: STATUS_LABEL[status] || status
  })
}

/** 状态卡：数字 + 单位 + 口径（口径永远在最下面、最弱一档）。 */
function StatCard(props) {
  return jsxs('div', {
    className: 'hc-adv__stat',
    'data-stat': props.id,
    children: [
      jsx('div', { className: 'hc-adv__stat-label', children: props.label }),
      jsxs('div', {
        className: 'hc-adv__stat-value',
        children: [
          jsx('span', { className: 'hc-adv__stat-num', children: num(props.value) }),
          props.unit ? jsx('span', { className: 'hc-adv__stat-unit', children: props.unit }) : null
        ]
      }),
      props.sub ? jsx('div', { className: 'hc-adv__stat-sub', children: props.sub }) : null,
      jsx('div', { className: 'hc-adv__stat-caliber', title: props.caliber, children: props.caliber })
    ]
  })
}

/** 最近 7 天趋势：一行小字 + 7 根细柱（柱高按「当天新增」归一化）。 */
function TrendStrip(props) {
  var trend = props.trend || []
  if (!trend.length) return null
  var peak = 1
  trend.forEach(function (day) {
    if (day.raised > peak) peak = day.raised
  })
  var raised = 0
  var decided = 0
  trend.forEach(function (day) {
    raised += day.raised
    decided += day.decided
  })
  return jsxs('div', {
    className: 'hc-adv__trend',
    'data-trend-days': trend.length,
    children: [
      jsx('span', {
        className: 'hc-adv__trend-text',
        children:
          '最近 ' + trend.length + ' 天：新增 ' + num(raised) + ' 条 · 决定 ' + num(decided) + ' 条'
      }),
      jsx('span', {
        className: 'hc-adv__trend-bars',
        'aria-hidden': 'true',
        children: trend.map(function (day) {
          var height = Math.max(8, Math.round((day.raised / peak) * 100))
          return jsx(
            'span',
            {
              className: 'hc-adv__trend-day',
              'data-date': day.date,
              title: day.date + '：新增 ' + num(day.raised) + ' · 投递 ' + num(day.delivered) + ' · 决定 ' + num(day.decided),
              style: { height: height + '%' }
            },
            day.date
          )
        })
      })
    ]
  })
}

/** 顶部状态条：待处理 / 已采纳 / 已拒绝 / 总计（四个数字，各带口径）+ 7 天趋势。 */
function StatusBar(props) {
  var query = props.query
  if (query.status === 'loading') return jsx(Skeleton, { cards: 1, lines: 2, label: '正在读取建议统计' })
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '建议统计没读到',
      message: query.error,
      detail: '接口 /advisor/summary',
      onRetry: query.reload,
      retryLabel: '重试统计'
    })
  }
  var payload = (query.data && query.data.data) || {}
  var notes = (query.data && query.data.notes) || []
  var bySeverity = payload.by_severity || {}
  return jsxs('div', {
    className: 'hc-adv__status',
    children: [
      jsxs('div', {
        className: 'hc-adv__stats',
        children: [
          jsx(StatCard, {
            id: 'pending',
            label: '待处理',
            value: payload.pending,
            unit: '条',
            sub: '已投递 ' + num(payload.delivered) + ' 条 · 已丢弃 ' + num(payload.dropped) + ' 条',
            caliber: '待处理 = 台账里最新事件仍是 raised / held / reconfirmed 的建议条数（还没被投递或决定）'
          }),
          jsx(StatCard, {
            id: 'accepted',
            label: '已采纳',
            value: payload.accepted,
            unit: '条',
            sub: '在审核台点过「采纳」的建议',
            caliber: '已采纳 = 最新事件是 accepted 的建议条数（只记录决定，不执行建议内容）'
          }),
          jsx(StatCard, {
            id: 'rejected',
            label: '已拒绝',
            value: payload.rejected,
            unit: '条',
            sub: '每条都带拒绝原因',
            caliber: '已拒绝 = 最新事件是 rejected 的建议条数（拒绝必须填原因，原因进台账）'
          }),
          jsx(StatCard, {
            id: 'total',
            label: '总计',
            value: payload.total,
            unit: '条建议',
            sub:
              'NIT ' + num(bySeverity.nit) + ' · CONCERN ' + num(bySeverity.concern) + ' · BLOCKER ' +
              num(bySeverity.blocker),
            caliber: '总计 = 台账 ' + num(payload.events) + ' 条事件折叠后的建议条数（按 advice id 去重）'
          })
        ]
      }),
      jsx(TrendStrip, { trend: payload.trend }),
      notes.length
        ? jsx('div', {
            className: 'hc-adv__notes',
            children: notes.map(function (note, i) {
              return jsx('div', { className: 'hc-adv__note', children: note }, 'n' + i)
            })
          })
        : null
    ]
  })
}

/** 建议正文的结构自检：五格，检测到就是实线 + ✓，没检测到就是虚线 + 缺（形状 + 文字双通道）。 */
function NoteStructure(props) {
  var structure = props.structure
  if (!structure) return null
  var missing = structure.missing || []
  var targetState = targetStateOf(structure)
  var quotes = structure.quotes || []
  return jsxs('div', {
    className: 'hc-adv__struct',
    'data-structure-complete': structure.complete ? 'yes' : 'no',
    'data-structure-missing': missing.join(','),
    'data-target-state': targetState,
    children: [
      jsxs('div', {
        className: 'hc-adv__struct-head',
        children: [
          jsx('span', { className: 'hc-adv__struct-title', children: '结构自检' }),
          jsx('span', {
            className: 'hc-adv__hint',
            children: structure.complete
              ? '五项都有：能看出它在评什么、依据是什么'
              : '缺 ' + num(missing.length) + ' 项：' + missing.join(' / ')
          })
        ]
      }),
      jsxs('div', {
        className: 'hc-adv__struct-cells',
        children: STRUCTURE_FIELDS.map(function (field) {
          var has = structureHas(structure, field.id)
          // target 一列走三态：点了名 / 只引了原话 / 没点名。引号不算点名（闸门不认）。
          var mark = has ? '✓' : '缺'
          var cellProps = {
            className: 'hc-adv__struct-cell' + (has ? ' hc-adv__struct-cell--yes' : ' hc-adv__struct-cell--no'),
            'data-struct-field': field.id,
            'data-struct-has': has ? 'yes' : 'no',
            title: field.hint,
            children: [
              jsx('span', { className: 'hc-adv__struct-mark', children: mark }),
              jsx('span', { children: field.label })
            ]
          }
          if (field.id === 'target') {
            cellProps['data-struct-state'] = targetState
            cellProps.children = [
              jsx('span', {
                className: 'hc-adv__struct-mark',
                // 引号不是「缺」也不是「✓」：单独一个引号标记，配合右侧文字说清三态
                children: targetState === 'named' ? '✓' : (targetState === 'quoted_only' ? '“”' : '缺')
              }),
              jsx('span', {
                children: targetState === 'named'
                  ? field.label
                  : field.label + '（' + (TARGET_STATE_LABEL[targetState] || targetState) + '）'
              })
            ]
          }
          return jsxs('span', cellProps, 'st-' + field.id)
        })
      }),
      targetState === 'named'
        ? jsx('div', {
            className: 'hc-adv__hint',
            children: '检测到的对象引用：' + (structure.targets || []).map(function (one) {
              return one.kind + '「' + one.text + '」'
            }).join(' · ')
          })
        : targetState === 'quoted_only'
          ? jsx('div', {
              className: 'hc-adv__hint',
              'data-quote-only': 'yes',
              children: '正文里只引了原话（' + quotes.map(function (one) {
                return '「' + one.text.replace(/^[「“"]|[」”"]$/g, '') + '」'
              }).join(' · ') + '），没有点名对象：插件闸门只认卡 id / 会话 id / comment N / 文件:行，'
                + '引号原文不算点名 —— 这条会被拦下、不会投递。'
            })
          : jsx('div', {
              className: 'hc-adv__hint',
              children: '正文里没有任何对象引用（卡 id / 会话 id / comment N / 文件:行）——'
                + '这条建议没点名它在评哪件事。'
            }),
      jsx('div', {
        className: 'hc-adv__hint',
        children: '检测只认客观标记（插件 prompt 要求 goal / evidence / impact / next 四要素 + 点名对象），'
          + '不替建议补勾；target 的口径与插件闸门一致（闸门是约束，本面板只是展示），'
          + '四要素的**强制**在插件本体（runtime.py::_required_elements），不在本面板。'
      })
    ]
  })
}

/** 三种评审模式的差异 + 两个提示词分支 + 源码证据（**全页只写一遍**）。
 *
 * 这里刻意不再放「当前设置值 / 当前生效值 / 来源层」三行徽标，也不复述「手动设定为
 * target 会怎样」：设置值由设置表单里那个唯一的 `mode` 下拉表达，本轮生效值是它右边的
 * 只读 chip（同一个字段、同一处改，只有一个可编辑入口），来源层是同一行的来源 chip。
 * 本块只保留三种模式各自「什么时候生效 / 判据是什么」这件有效信息，外加默认收起的证据。
 */
function ModeGuide(props) {
  var data = props.data || {}
  var mode = props.mode || 'auto'
  var rules = data.modes || []
  var resolvedModes = data.resolved_modes || []
  var prompt = data.prompt || {}
  if (!rules.length) return null
  return jsxs('details', {
    className: 'hc-adv__modeguide',
    'data-modes-guide': mode,
    children: [
      jsx('summary', { className: 'hc-adv__modeguide-sum', children: '三种模式的区别' }, 'sum'),
      jsxs('div', {
        className: 'hc-adv__modeguide-body',
        children: [
          jsxs('div', {
            className: 'hc-adv__mode-table',
            'data-mode-rows': rules.length,
            children: rules.map(function (rule) {
              return jsxs('div', {
                className: 'hc-adv__mode-row' + (rule.id === mode ? ' hc-adv__mode-row--now' : ''),
                'data-mode': rule.id,
                'data-mode-rule': rule.id,
                'data-mode-current': rule.id === mode ? 'yes' : 'no',
                title: rule.source || '',
                children: [
                  jsx('span', { className: 'hc-adv__mode-name', children: rule.label }, 'n'),
                  jsx('span', { className: 'hc-adv__mode-when', children: rule.when }, 'w'),
                  jsx('span', { className: 'hc-adv__mode-crit', children: rule.criterion }, 'c')
                ]
              }, 'mode-' + rule.id)
            })
          }, 'mtable'),
          resolvedModes.length
            ? jsxs('div', {
                className: 'hc-adv__mode-branches',
                'data-mode-branches': resolvedModes.length,
                children: [
                  jsx('div', {
                    className: 'hc-adv__mode-ev-title',
                    children: '两个提示词分支（生效值落在哪个分支，看它）'
                  }, 'bt')
                ].concat(resolvedModes.map(function (branch) {
                  return jsxs('div', {
                    className: 'hc-adv__mode-branch'
                      + (branch.id === data.resolved_mode ? ' hc-adv__mode-branch--now' : ''),
                    'data-mode-branch': branch.id,
                    title: branch.source || '',
                    children: [
                      jsx('span', { className: 'hc-adv__mode-name', children: branch.label }, 'bl'),
                      jsx('span', {
                        className: 'hc-adv__mode-crit',
                        children: '标记句：' + (branch.marker || '（读不到）')
                      }, 'bm')
                    ]
                  }, 'branch-' + branch.id)
                }))
              }, 'mbranches')
            : null,
          jsxs('details', {
            className: 'hc-adv__mode-evidence',
            children: [
              jsx('summary', { children: '证据（读的是插件源码，不是猜）' }, 'es'),
              jsxs('div', {
                className: 'hc-adv__mode-ev-body',
                children: [
                  jsx('div', {
                    className: 'hc-adv__hint',
                    children: '提示词文件 ' + (prompt.prompt_file || '?')
                      + '　·　' + num(prompt.prompt_chars) + ' 字符'
                      + '　·　sha256 ' + String(prompt.prompt_sha256 || '').slice(0, 16) + '…'
                      + '　·　段落 ' + (prompt.prompt_sections || []).join(' ')
                  }, 'e1'),
                  jsx('div', {
                    className: 'hc-adv__hint',
                    children: '无目标分支首行：' + (prompt.unknown_branch_head || '?')
                      + '（含 CONSERVATIVE：' + (prompt.unknown_branch_says_conservative ? '是' : '否') + '）'
                  }, 'e2'),
                  jsx('div', {
                    className: 'hc-adv__hint',
                    children: '有目标分支首行：' + (prompt.known_branch_head || '?')
                      + '（带验收项：' + (prompt.known_branch_mentions_acceptance ? '是' : '否') + '）'
                  }, 'e3'),
                  jsx('div', {
                    className: 'hc-adv__hint',
                    'data-prompt-forced-target': prompt.forced_target_says_mode_target ? 'yes' : 'no',
                    children: '强制目标对齐 + 无目标分支首行：' + (prompt.forced_target_branch_head || '?')
                      + '（含 mode=target：' + (prompt.forced_target_says_mode_target ? '是' : '否') + '）'
                  }, 'e4')
                ]
              }, 'eb')
            ]
          }, 'mevidence')
        ]
      }, 'body')
    ]
  })
}

/**
 * 诊断（默认折叠成一行摘要）。
 *
 * 原来这里是裸文本堆叠：绝对路径、ISO 时间戳、逐条失败明细全摊在页面上，一屏读不出
 * 「到底哪几件事不对」。摘要只给**计数**（几个 profile 没台账 / 几次出错 / 几次超时 /
 * 几次无终态 / 几条日志旁证），点开才是流水明细与日志旁证原文。
 *
 * 计数全部来自已取到的接口数据，取不到就说取不到（不写 0 冒充「没问题」）。
 */
function DiagnosticsPanel(props) {
  var reviews = props.reviews || {}
  var coverageRows = props.coverageRows || []
  var reviewsOk = reviews.status === 'ok'
  var data = (reviews.data && reviews.data.data) || {}
  var errors = data.errors || {}
  var kinds = errors.by_kind || {}
  var noLedger = coverageRows.filter(function (row) {
    return row.plugin_installed && (row.ledger_events || 0) === 0
  })
  var errorCount = errors.count || 0
  var timeoutCount = kinds.model_timeout || 0
  var otherErrors = errorCount - timeoutCount
  var inFlight = data.in_flight_count || 0
  var logCount = (data.log_evidence || {}).count || 0
  var chips = []
  if (reviews.status === 'loading') {
    chips.push({ id: 'loading', text: '正在读取诊断…', tone: 'muted' })
  } else if (reviews.status === 'error') {
    chips.push({ id: 'error', text: '诊断没读到：' + (reviews.error || '未知原因'), tone: 'warn' })
  } else {
    chips.push({
      id: 'no-ledger',
      text: num(noLedger.length) + ' 个 profile 无台账',
      tone: noLedger.length ? 'warn' : 'muted',
      title: noLedger.length
        ? '已装插件、但台账还是 0 条：' + noLedger.map(function (r) { return r.profile }).join(' / ')
        : '已装插件的 profile 都有台账记录'
    })
    chips.push({
      id: 'timeout',
      text: num(timeoutCount) + ' 次模型超时',
      tone: timeoutCount ? 'warn' : 'muted',
      title: '台账 reviews.jsonl 里归类为 model_timeout 的评审'
    })
    if (otherErrors > 0) {
      chips.push({
        id: 'errors',
        text: num(otherErrors) + ' 次其它出错',
        tone: 'warn',
        title: '按类：' + Object.keys(kinds).map(function (kind) {
          return (ERROR_KIND_LABEL[kind] || kind) + ' ' + kinds[kind]
        }).join(' · ')
      })
    }
    chips.push({
      id: 'inflight',
      text: num(inFlight) + ' 次无终态',
      tone: inFlight ? 'warn' : 'muted',
      title: '只写了 started、没有终态的评审（进程先退出）：结果不会出现，重跑才有'
    })
    chips.push({
      id: 'log',
      text: num(logCount) + ' 条日志旁证',
      tone: logCount ? 'warn' : 'muted',
      title: (data.log_evidence || {}).path || '日志里 advisor 评审失败的条数（不在台账里）'
    })
    if (!reviewsOk) chips = [{ id: 'error', text: '诊断没读到', tone: 'warn' }]
    if (!noLedger.length && !errorCount && !inFlight && !logCount) {
      chips = [{ id: 'clean', text: '没有异常：台账、评审流水、日志都干净', tone: 'ok' }]
    }
  }
  var hasBody = reviewsOk && (errorCount > 0 || inFlight > 0 || logCount > 0)
  return jsxs('details', {
    className: 'hc-adv__diag',
    'data-adv-diagnostics': reviewsOk ? (errorCount + inFlight + logCount > 0 ? 'warn' : 'ok') : 'pending',
    children: [
      jsxs('summary', {
        className: 'hc-adv__diag-sum',
        title: '点开展开评审流水明细、错误归类与日志旁证（默认收起，不占版面）',
        children: [
          jsx('span', { className: 'hc-adv__diag-title', children: '诊断' }, 't')
        ].concat(chips.map(function (chip) {
          return jsx('span', {
            className: 'hc-adv__diag-chip hc-adv__diag-chip--' + chip.tone,
            'data-diag-chip': chip.id,
            title: chip.title || '',
            children: chip.text
          }, 'c-' + chip.id)
        }))
      }, 'sum'),
      hasBody
        ? jsx('div', {
            className: 'hc-adv__diag-body',
            children: jsx(ReviewTrail, { query: reviews }, 'trail')
          }, 'body')
        : jsx('div', {
            className: 'hc-adv__diag-body',
            children: jsx('div', {
              className: 'hc-adv__hint',
              children: reviewsOk
                ? '台账里没有出错记录、没有无终态评审、日志里也没有失败 —— 没有可展开的明细。'
                : '评审流水没读到，明细无从展开。'
            })
          }, 'empty')
    ]
  })
}

/** 评审流水与错误：504 翻成人话 + 次数 + 时间 + 飞行中 + 日志旁证。 */
function ReviewTrail(props) {
  var query = props.query
  if (query.status === 'loading') return jsx(Skeleton, { lines: 5, label: '正在读取评审流水' })
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '评审流水没读到',
      message: query.error,
      detail: '接口 /advisor/reviews',
      onRetry: query.reload,
      retryLabel: '重试流水'
    })
  }
  var data = (query.data && query.data.data) || {}
  var errors = data.errors || {}
  var inFlight = data.in_flight || []
  var log = data.log_evidence || {}
  var records = data.items || []
  var notes = (query.data && query.data.notes) || []
  return jsxs('div', {
    className: 'hc-adv__trail',
    'data-trail-total': num(data.total || 0),
    'data-trail-errors': num(errors.count || 0),
    'data-trail-inflight': num(data.in_flight_count || 0),
    'data-trail-log-failures': num(log.count || 0),
    children: [
      jsxs('div', {
        className: 'hc-adv__block-head',
        children: [
          jsx('span', { className: 'hc-adv__block-title', children: '评审流水与错误' }),
          jsx('span', { className: 'hc-chip', children: '评审 ' + num(data.total || 0) + ' 次' }),
          jsx('span', {
            className: 'hc-chip' + (errors.count ? ' hc-chip--warn' : ''),
            title: '台账 reviews.jsonl 里 outcome=error 的记录',
            children: '出错 ' + num(errors.count || 0) + ' 次'
          }),
          jsx('span', {
            className: 'hc-chip' + (data.in_flight_count ? ' hc-chip--warn' : ''),
            title: '只写了 started、没有终态的评审（进程先退出）',
            children: '飞行中 ' + num(data.in_flight_count || 0) + ' 次'
          }),
          jsx('span', {
            className: 'hc-chip' + (log.count ? ' hc-chip--warn' : ''),
            title: log.path || '',
            children: '日志旁证 ' + num(log.count || 0) + ' 条'
          })
        ]
      }),
      // ---- 台账里的错误（带归类 + 人话 + 建议）----
      errors.count
        ? jsxs('div', {
            className: 'hc-adv__err-list',
            'data-error-kinds': Object.keys(errors.by_kind || {}).join(','),
            children: [
              jsx('div', {
                className: 'hc-adv__err-head',
                children: '台账里的评审错误：' + num(errors.count) + ' 次（首次 '
                  + absDate(errors.first_at) + ' · 最近 ' + absDate(errors.last_at) + '）'
              }, 'err-head')
            ].concat((errors.items || []).map(function (item, i) {
              return jsxs(
                'div',
                {
                  className: 'hc-adv__err-item',
                  'data-error-kind': item.kind,
                  children: [
                    jsxs('div', {
                      className: 'hc-adv__err-title',
                      children: [
                        jsx('span', { className: 'hc-adv__err-kind', children: item.label || ERROR_KIND_LABEL[item.kind] || item.kind }),
                        jsx('span', { className: 'hc-adv__event-ts', children: absDate(item.ts) }),
                        jsx('span', { className: 'hc-adv__hint', children: item.profile + ' · ' + item.review_id })
                      ]
                    }),
                    jsx('div', { className: 'hc-adv__err-read', children: item.readable || '' }),
                    jsx('div', {
                      className: 'hc-adv__err-raw',
                      children: '原文：' + (item.error || '（空）')
                    }),
                    (item.hints || []).length
                      ? jsx('ul', {
                          className: 'hc-adv__err-hints',
                          children: item.hints.map(function (hint, hi) {
                            return jsx('li', { children: hint }, 'h' + hi)
                          })
                        })
                      : null
                  ]
                },
                'err' + i
              )
            }))
          })
        : jsx('div', {
            className: 'hc-adv__hint',
            children: '台账里没有出错记录（reviews.jsonl 的 outcome=error 为空）。'
              + '如果下面的日志旁证里有失败，说明那段失败发生在台账写入之前，只在日志里。'
          }),
      // ---- 日志旁证（历史 504 只留在这里）----
      log.count
        ? jsxs('div', {
            className: 'hc-adv__loglist',
            'data-log-path': log.path || '',
            children: [
              jsx('div', {
                className: 'hc-adv__log-head',
                children: '日志旁证（不是台账）：' + log.path + '　·　' + num(log.count) + ' 条 · '
                  + absDate(log.first_at) + ' ~ ' + absDate(log.last_at)
                  + '　·　扫描 ' + num(log.scanned_lines) + ' 行' + (log.truncated ? '（只扫尾部）' : '')
              }, 'log-head')
            ].concat((log.failures || []).slice(0, 6).map(function (item, i) {
              return jsxs(
                'div',
                {
                  className: 'hc-adv__err-item hc-adv__err-item--log',
                  'data-error-kind': item.kind,
                  children: [
                    jsxs('div', {
                      className: 'hc-adv__err-title',
                      children: [
                        jsx('span', { className: 'hc-adv__err-kind', children: item.label }),
                        jsx('span', { className: 'hc-adv__event-ts', children: absDate(item.ts) }),
                        jsx('span', {
                          className: 'hc-adv__hint',
                          title: item.turn_id || '',
                          children: (item.turn_id || '').split(':').slice(-1)[0] || '—'
                        })
                      ]
                    }),
                    jsx('div', { className: 'hc-adv__err-read', children: item.readable }),
                    jsx('div', { className: 'hc-adv__err-raw', children: '原文：' + item.error }),
                    (item.hints || []).length
                      ? jsx('ul', {
                          className: 'hc-adv__err-hints',
                          children: item.hints.map(function (hint, hi) {
                            return jsx('li', { children: hint }, 'lh' + hi)
                          })
                        })
                      : null
                  ]
                },
                'log' + i
              )
            }))
          })
        : null,
      // ---- 飞行中（有开始、无终态）：可折叠真表格 ----
      inFlight.length
        ? jsxs('details', {
            className: 'hc-adv__inflight',
            'data-inflight-count': num(inFlight.length),
            children: [
              jsx('summary', {
                className: 'hc-adv__inflight-head',
                children: '只有开始、没有终态的评审：' + num(inFlight.length)
                  + ' 次（不是「无建议」，也不是 404 —— 结果不会出现，重跑才有）'
              }, 'inflight-sum'),
              jsxs('div', {
                className: 'hc-adv__mini-table',
                children: [
                  jsxs('div', {
                    className: 'hc-adv__mini-row hc-adv__mini-row--head',
                    children: [
                      jsx('span', { children: '时间' }, 'h0'),
                      jsx('span', { children: 'Agent' }, 'h1'),
                      jsx('span', { children: '模型' }, 'h2'),
                      jsx('span', { children: 'review_id' }, 'h3')
                    ]
                  }, 'inflight-thead')
                ].concat(inFlight.slice(0, 20).map(function (row, i) {
                  return jsxs('div', {
                    className: 'hc-adv__mini-row',
                    children: [
                      jsx('span', { className: 'hc-adv__event-ts', children: absDate(row.ts) }, 't'),
                      jsx('span', { className: 'hc-adv__profile', children: row.profile }, 'p'),
                      jsx('span', { className: 'hc-adv__hint', children: row.model || '（模型未知）' }, 'm'),
                      jsx('span', { className: 'hc-adv__hint', children: row.review_id || '' }, 'r')
                    ]
                  }, 'if' + i)
                }))
              }, 'inflight-body')
            ]
          }, 'inflight')
        : null,
      // ---- 最近几条记录（outcome / 模型 / 耗时）：可折叠真表格 ----
      records.length
        ? jsxs('details', {
            className: 'hc-adv__trail-rows',
            children: [
              jsx('summary', {
                className: 'hc-adv__inflight-head',
                children: '最近评审记录：' + num(records.length) + ' 条（时间 / 结论 / Agent / 模型·耗时）'
              }, 'records-sum'),
              jsxs('div', {
                className: 'hc-adv__mini-table',
                children: [
                  jsxs('div', {
                    className: 'hc-adv__mini-row hc-adv__mini-row--head',
                    children: [
                      jsx('span', { children: '时间' }, 'h0'),
                      jsx('span', { children: '结论' }, 'h1'),
                      jsx('span', { children: 'Agent' }, 'h2'),
                      jsx('span', { children: '模型 · 耗时 · 建议' }, 'h3')
                    ]
                  }, 'records-thead')
                ].concat(records.slice(0, 20).map(function (row, i) {
                  return jsxs('div', {
                    className: 'hc-adv__mini-row',
                    'data-outcome': row.outcome || '',
                    children: [
                      jsx('span', { className: 'hc-adv__event-ts', children: absDate(row.ts) }, 't'),
                      jsx('span', {
                        className: 'hc-adv__outcome hc-adv__outcome--' + (row.outcome || 'other'),
                        children: OUTCOME_LABEL[row.outcome] || row.outcome
                      }, 'o'),
                      jsx('span', { className: 'hc-adv__profile', children: row.profile_source || row.profile }, 'p'),
                      jsx('span', {
                        className: 'hc-adv__hint',
                        children: (row.model || '—') + ' · ' + num(row.duration_ms) + 'ms · 建议 '
                          + num(row.advice_count) + ' 条'
                      }, 'm')
                    ]
                  }, 'tr' + i)
                }))
              }, 'records-body')
            ]
          }, 'records')
        : null,
      notes.length
        ? jsx('div', {
            className: 'hc-adv__notes',
            children: notes.map(function (note, i) {
              return jsx('div', { className: 'hc-adv__note', children: note }, 'tn' + i)
            })
          })
        : null
    ]
  })
}

/** 建议列表的一行：时间 / severity / 正文摘要 / 状态 / 来源（profile + 会话短 id）/ 详情。 */
function AdviceRow(props) {
  var item = props.item
  return jsxs('div', {
    className: 'hc-adv__row' + (props.active ? ' hc-adv__row--active' : ''),
    'data-id': item.id,
    'data-status': item.status || '',
    'data-severity': item.severity || '',
    children: [
      jsx('div', {
        className: 'hc-adv__cell hc-adv__cell--time',
        title: absDate(item.last_seen_at),
        children: relTime(item.last_seen_at)
      }),
      jsx('div', { className: 'hc-adv__cell hc-adv__cell--sev', children: jsx(SeverityBadge, { severity: item.severity }) }),
      jsx('div', {
        className: 'hc-adv__cell hc-adv__cell--note',
        title: item.note || '',
        children: jsxs('div', {
          className: 'hc-adv__note-wrap',
          children: [
            item.note_structure && item.note_structure.has_target === false
              ? jsx('span', {
                  className: 'hc-adv__nontarget'
                    + (targetStateOf(item.note_structure) === 'quoted_only' ? ' hc-adv__nontarget--quote' : ''),
                  'data-no-target': targetStateOf(item.note_structure) === 'quoted_only' ? 'quote' : 'yes',
                  title: targetStateOf(item.note_structure) === 'quoted_only'
                    ? '正文里只引了原话（引号），没有点名对象：插件闸门只认卡 id / 会话 id / comment N / 文件:行，'
                      + '引号原文不算点名 —— 这条会被拦下、不会投递。详情里有完整结构自检。'
                    : '正文里没有任何对象引用（卡 id / 会话 id / comment N / 文件:行）：'
                      + '这条建议没点名它在评哪件事。详情里有完整结构自检。',
                  children: targetStateOf(item.note_structure) === 'quoted_only' ? '仅引用' : '未点名'
                }, 'nt')
              : null,
            jsx('span', { className: 'hc-adv__note-text', children: clip(item.note, NOTE_CLIP) }, 'nt-text')
          ]
        })
      }),
      jsx('div', { className: 'hc-adv__cell hc-adv__cell--status', children: jsx(StatusPill, { status: item.status }) }),
      jsxs('div', {
        className: 'hc-adv__cell hc-adv__cell--source',
        title: '来源：' + (item.profile || '未知 agent') + ' · 台账文件 ' + (item.profile_source || '?')
          + ' · 会话 ' + (item.session_id || '（无会话 id）'),
        children: [
          jsx('span', { className: 'hc-adv__profile', children: item.profile || '—' }),
          jsx('span', { className: 'hc-adv__session', children: item.session_short ? '· ' + item.session_short : '' }),
          item.profile_source && item.profile_source !== item.profile
            ? jsx('span', {
                className: 'hc-adv__profsrc',
                title: '这条建议是从 ' + item.profile_source + ' 的台账文件读出来的（与它自报的 profile 不同）',
                children: '文件:' + item.profile_source
              })
            : null,
          jsx('span', {
            className: 'hc-adv__goal' + (item.goal_known ? '' : ' hc-adv__goal--unknown'),
            'data-goal-source': item.goal_source || 'unknown',
            title: '目标来源：' + goalLabel(item.goal_source)
              + (item.goal_ref ? ' · ' + item.goal_ref : ''),
            children: goalLabel(item.goal_source) + (item.goal_ref ? ' ' + item.goal_ref : '')
          })
        ]
      }),
      jsx('div', {
        className: 'hc-adv__cell hc-adv__cell--ops',
        children: jsx('button', {
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-row-toggle': item.id,
          title: props.active ? '收起详情（同一行再点一次）' : '展开这条建议的详情与决定表单',
          onClick: function () {
            // 收起/展开必须**真的切换**：原来这里无条件 onOpen(id)，
            // 于是「收起」按钮点下去只是把同一个 id 再设一遍，详情永远关不掉。
            props.onToggle(item.id)
          },
          children: props.active ? '收起' : '详情'
        })
      })
    ]
  })
}

/** 决定表单：采纳 / 拒绝（拒绝必须填原因，前端先拦一次）。 */
function DecisionForm(props) {
  var item = props.item
  var state = useState({ mode: '', reason: '', error: '', busy: false })
  var form = state[0]
  var setForm = state[1]

  function submit(decision, reason) {
    var trimmed = (reason || '').trim()
    if (decision === 'rejected' && !trimmed) {
      // 前端拦一次（后端还会 400 一次）：两边都做，别指望对方
      setForm({ mode: 'rejected', reason: form.reason, error: '拒绝必须填写原因（否则这条拒绝没法复查）', busy: false })
      return
    }
    setForm({ mode: form.mode, reason: form.reason, error: '', busy: true })
    send('/advisor/decision', { id: item.id, decision: decision, reason: trimmed }).then(function (res) {
      if (!res || res.ok === false) {
        setForm({ mode: form.mode, reason: form.reason, error: (res && res.message) || '提交失败', busy: false })
        return
      }
      setForm({ mode: '', reason: '', error: '', busy: false })
      props.onDecided(item.id)
    })
  }

  if (item.decision) {
    return jsxs('div', {
      className: 'hc-adv__decision',
      'data-decided': item.decision,
      children: [
        jsxs('div', {
          className: 'hc-adv__decision-head',
          children: [
            jsx('span', { className: 'hc-adv__decision-label', children: '已决定' }),
            jsx(StatusPill, { status: item.decision }),
            jsx('span', { className: 'hc-adv__hint', children: relTime(item.decided_at) + '（' + absDate(item.decided_at) + '）' })
          ]
        }),
        item.reason
          ? jsx('div', { className: 'hc-adv__decision-reason', children: '原因：' + item.reason })
          : jsx('div', { className: 'hc-adv__decision-reason', children: '采纳没有附原因（可选）' })
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-adv__decision',
    children: [
      jsxs('div', {
        className: 'hc-adv__decision-head',
        children: [
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--accent',
            disabled: form.busy,
            title: '只记录「已采纳」这个决定，不执行建议内容、不改任何代码或配置',
            onClick: function () {
              submit('accepted', '')
            },
            children: '采纳'
          }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn',
            disabled: form.busy,
            title: '拒绝必须填原因：原因会写进台账，方便以后复查为什么没采纳',
            onClick: function () {
              setForm({ mode: form.mode === 'rejected' ? '' : 'rejected', reason: form.reason, error: '', busy: false })
            },
            children: form.mode === 'rejected' ? '取消拒绝' : '拒绝'
          }),
          jsx('span', {
            className: 'hc-adv__hint',
            children: '决定只写台账，不会触发任何自动执行'
          })
        ]
      }),
      form.mode === 'rejected'
        ? jsxs('div', {
            className: 'hc-adv__reason',
            children: [
              jsx('textarea', {
                className: 'hc-adv__textarea',
                rows: 2,
                value: form.reason,
                placeholder: '为什么拒绝这条建议？（必填，会写进台账）',
                'aria-label': '拒绝原因（必填）',
                onChange: function (event) {
                  setForm({ mode: 'rejected', reason: event.target.value, error: '', busy: form.busy })
                }
              }),
              jsxs('div', {
                className: 'hc-adv__reason-actions',
                children: [
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn hc-btn--accent',
                    disabled: form.busy,
                    onClick: function () {
                      submit('rejected', form.reason)
                    },
                    children: form.busy ? '提交中…' : '提交拒绝'
                  }),
                  jsx('span', { className: 'hc-adv__hint', children: '必填：空原因会被后端 400 拒掉' })
                ]
              })
            ]
          })
        : null,
      form.error
        ? jsx('div', { className: 'hc-adv__formerr', role: 'alert', children: form.error })
        : null
    ]
  })
}

/** 详情：全文 / 首次出现 / 出现次数 / 投递次数 / 事件时间线 / 决定与原因。 */
function DetailPanel(props) {
  var id = props.id
  var nonce = props.nonce || 0
  var query = useQuery('advisor.detail|' + id + '|' + nonce, function () {
    return fetchDetail(id)
  })
  if (query.status === 'loading') return jsx(Skeleton, { lines: 6, label: '正在读取建议详情' })
  if (query.status === 'error') {
    return jsx(ErrorState, {
      title: '建议详情没读到',
      message: query.error,
      detail: '接口 /advisor/advice/' + id,
      onRetry: query.reload,
      retryLabel: '重试详情'
    })
  }
  var item = (query.data && query.data.data) || {}
  var events = item.events || []
  return jsxs('div', {
    className: 'hc-adv__detail',
    'data-detail-id': item.id || id,
    children: [
      jsxs('div', {
        className: 'hc-adv__detail-head',
        children: [
          jsx('span', { className: 'hc-adv__detail-title', children: '建议详情' }),
          jsx(SeverityBadge, { severity: item.severity }),
          jsx(StatusPill, { status: item.status }),
          jsx('span', { className: 'hc-chip', children: 'id ' + (item.id || id) }),
          jsx('span', {
            className: 'hc-chip',
            title: '来源会话：' + (item.session_id || '（无）'),
            children: (item.profile || '未知 agent') + (item.session_short ? ' · ' + item.session_short : '')
          })
        ]
      }),
      jsx('div', { className: 'hc-adv__detail-note', children: item.note || '（这条建议没有正文）' }),
      jsx(NoteStructure, { structure: item.note_structure }),
      jsxs('div', {
        className: 'hc-adv__facts',
        children: [
          jsx('span', { className: 'hc-adv__fact', children: '首次出现 ' + absDate(item.first_raised_at) }),
          jsx('span', { className: 'hc-adv__fact', children: '最近一次 ' + absDate(item.last_seen_at) }),
          jsx('span', { className: 'hc-adv__fact', children: '出现 ' + num(item.raise_count) + ' 次' }),
          jsx('span', { className: 'hc-adv__fact', children: '投递 ' + num(item.deliver_count) + ' 次' }),
          jsx('span', { className: 'hc-adv__fact', children: '暂扣 ' + num(item.held_count) + ' 次' }),
          jsx('span', { className: 'hc-adv__fact', children: '事件 ' + num(item.event_count) + ' 条' }),
          jsx('span', {
            className: 'hc-adv__fact hc-adv__fact--goal',
            'data-goal-source': item.goal_source || 'unknown',
            title: 'advisor 判定这条建议时对齐的目标从哪来：看板卡 / 用户目标 / 首个请求 / 未知',
            children: '目标来源：' + goalLabel(item.goal_source)
              + (item.goal_ref ? '（' + item.goal_ref + '）' : '')
          })
        ]
      }),
      jsxs('div', {
        className: 'hc-adv__timeline',
        children: [
          jsx('div', { className: 'hc-adv__timeline-title', children: '事件时间线（台账原文顺序）' }),
          events.map(function (event, index) {
            return jsxs(
              'div',
              {
                className: 'hc-adv__event',
                'data-event': event.event,
                children: [
                  jsx('span', { className: 'hc-adv__event-ts', children: absDate(event.ts) }),
                  jsx('span', {
                    className: 'hc-adv__event-name',
                    children: EVENT_LABEL[event.event] || event.event
                  }),
                  jsx('span', {
                    className: 'hc-adv__event-meta',
                    children: (event.actor || '') + (event.reason ? ' · ' + event.reason : '')
                  })
                ]
              },
              'e' + index
            )
          })
        ]
      }),
      jsx(DecisionForm, { item: item, onDecided: props.onDecided })
    ]
  })
}

/** 逐项设置表：**生效值 + 来源层 + 落盘值 + 默认值**一行一项。
 *
 * 来源层只有两层（default / settings_file）+ 一个只读常量（forced_constant），
 * 后端 ``values_detail`` 给的就是这三样；不编造 plugin_config / host_global 那种层
 * （advisor 的设置不从 config.yaml 读，宿主层在下面单独只读展示）。
 */
function SettingsTable(props) {
  var detail = props.detail || []
  if (!detail.length) return null
  return jsxs('div', {
    className: 'hc-adv__stable',
    'data-settings-rows': detail.length,
    children: [
      jsxs('div', {
        className: 'hc-adv__stable-row hc-adv__stable-row--head',
        children: [
          jsx('span', { children: '键' }),
          jsx('span', { children: '生效值' }),
          jsx('span', { children: '来源层' }),
          jsx('span', { children: '落盘值' }),
          jsx('span', { children: '默认值' })
        ]
      }, 'stable-head')
    ].concat(detail.map(function (row) {
      return jsxs(
        'div',
        {
          className: 'hc-adv__stable-row' + (row.readonly ? ' hc-adv__stable-row--ro' : ''),
          'data-settings-key': row.key,
          'data-settings-source': row.source,
          children: [
            jsx('span', { className: 'hc-adv__stable-key', title: row.key, children: row.key }),
            jsx('span', {
              className: 'hc-adv__stable-value',
              'data-settings-value': String(row.value),
              children: String(row.value)
            }),
            jsx('span', {
              className: 'hc-adv__stable-src hc-adv__stable-src--' + row.source,
              title: row.source === 'settings_file'
                ? '这个 profile 的 settings.json 里显式写了它'
                : (row.source === 'default' ? '文件里没有这个键，值来自代码默认值'
                  : '程序常量（单向阀门），不可写'),
              children: row.source === 'settings_file' ? 'settings.json'
                : (row.source === 'default' ? 'default' : '强制常量')
            }),
            jsx('span', {
              className: 'hc-adv__stable-disk',
              children: row.on_disk_present ? String(row.on_disk) : '（未落盘）'
            }),
            jsx('span', { className: 'hc-adv__stable-default', children: String(row.default) })
          ]
        },
        'sd-' + row.key
      )
    }))
  })
}

/**
 * 「跟随全局」到底跟到哪：来源层 + 实际会生效的那一组。
 *
 * 后端 ``settings.model_pair`` 是只读事实（该 agent 的 config.yaml → model 段）：
 *   * 设置里 provider/model 都空 ⇒ 运行时评审调用**不带** model/provider，宿主按该 agent
 *     config.yaml 的主模型调用 —— 所以界面必须把「跟到的是哪一组」写出来；
 *   * 任一项非空 ⇒ 该 Agent 独立配置（settings.json），另一项留空时跟随该 provider 默认。
 *
 * 输入用**当前表单值**（可能是待保存的草稿），但来源层永远按「落盘后会不会覆盖」讲，
 * 并把「有未保存改动」单独标出来，不许把草稿说成已生效。
 *
 * @param {{source?: string, provider?: string, model?: string,
 *          effective_provider?: string, effective_model?: string}} pair
 * @param {string} provider 表单里的 provider（草稿值）
 * @param {string} model 表单里的 model（草稿值）
 * @param {boolean} pending provider/model 是否有未保存改动
 * @returns {{kind: string, effective: string, text: string}}
 */
function describeModelPair(pair, provider, model, pending) {
  var p = pair || {}
  var draftProvider = String(provider || '')
  var draftModel = String(model || '')
  var inheritProvider = String(p.provider || '')
  var inheritModel = String(p.model || '')
  var kind = draftProvider || draftModel
    ? 'settings_file'
    : (inheritProvider || inheritModel ? 'config_yaml' : 'none')
  var effectiveProvider = draftProvider || inheritProvider
  var effectiveModel = draftModel || inheritModel
  var head = kind === 'settings_file'
    ? '来源层：该 Agent 独立配置（settings.json）。'
    : (kind === 'config_yaml'
      ? '来源层：跟随全局 ⇒ 该 Agent 的 config.yaml（model 段）。'
      : '来源层：无可用默认 —— 该 Agent 的 config.yaml 没有 model 段，宿主会用会话当前模型。')
  var tail = kind === 'settings_file'
    ? (draftModel
      ? '评审调用会带 provider=' + draftProvider + ' / model=' + draftModel + '。'
      : 'model 留空 ⇒ 跟随该 provider 的默认模型（provider=' + draftProvider + '）。')
    : (kind === 'config_yaml'
      ? '评审调用不带 provider/model，所以实际会用 ' + (inheritProvider || '（未写 provider）')
        + ' / ' + (inheritModel || '（未写 model）') + '；要覆盖就在上面选一组。'
      : '要指定评审模型就在上面选一组 provider / model。')
  return {
    kind: kind,
    // 一处都没解析出来时别印「? / ?」——那看起来像模板变量没填，而不是「没有可用默认」。
    effective: effectiveProvider || effectiveModel
      ? (effectiveProvider || '?') + ' / ' + (effectiveModel || '?')
      : '未指定（用会话当前模型）',
    text: head + tail + (pending ? '（当前有未保存改动，保存后按草稿生效）' : '')
  }
}

/** 把后端 ``values`` 归一成表单形状（数字键统一成 Number，其余原样）。 */
function normalize(values) {
  var source = values || {}
  var interval = Number(source.min_interval_seconds)
  return {
    enabled: source.enabled === true,
    concern_default: source.concern_default || 'hold',
    min_interval_seconds: isFinite(interval) ? interval : 0,
    model: source.model || '',
    provider: source.provider || '',
    // 评审模式（提示词）：auto / target / conservative，缺省 auto（= 改造前行为）
    mode: source.mode || 'auto'
  }
}

/** 表单与 baseline 的差异：**只返回改动过的键**（未改动的键一个都不进请求体）。 */
function diffKeys(form, baseline) {
  var patch = {}
  var changed = []
  if (!form || !baseline) return { patch: patch, changed: changed }
  Object.keys(form).forEach(function (key) {
    var a = form[key]
    var b = baseline[key]
    if (key === 'min_interval_seconds') {
      a = Number(a)
      b = Number(b)
    }
    if (a !== b) {
      patch[key] = a
      changed.push(key)
    }
  })
  return { patch: patch, changed: changed }
}

/** 来源层短标签：设置项旁的小 chip（长解释进 tooltip，不占版面）。 */
var SOURCE_SHORT = { default: '默认值', settings_file: 'settings.json', forced_constant: '强制常量' }

var SOURCE_TITLE = {
  default: '该 agent 的 settings.json 里没有这个键，值来自代码默认值',
  settings_file: '该 agent 的 settings.json 里显式写了它',
  forced_constant: '程序常量（单向阀门），不可写'
}

/** 来源小 chip：只在**偏离常规**时出现（常规 = 该 agent 的 settings.json 里写了）。
 *
 * 五个字段各挂一个同样的 `settings.json` chip 是纯噪声（评审一眼就看出来）：
 * 常规情况由设置卡片标题旁那一个「来源 settings.json」说明，这里只标例外
 * （默认值 / 强制常量），或者说清「这个 agent 的 settings.json 里没有它」。
 */
function SourceChip(props) {
  var source = props.source || 'default'
  var force = props.always === true
  if (!force && source === 'settings_file') return null
  return jsx('span', {
    className: 'hc-adv__srcchip hc-adv__srcchip--' + source,
    'data-source-chip': source,
    'data-mode-fact': props.fact || undefined,
    title: props.title || SOURCE_TITLE[source] || '',
    children: SOURCE_SHORT[source] || source
  }, 'src')
}

/**
 * 设置表单的一行：**标签 / 唯一可编辑控件 / 只读读数 + 来源 chip**。
 *
 * 「每项设置只有一个可编辑入口」这条规矩靠这个组件落地：三个格子固定，
 * 控件只出现在中间那格，右边那格只能是只读文字或 chip。
 */
function SettingsRow(props) {
  return jsxs('div', {
    className: 'hc-adv__srow' + (props.wide ? ' hc-adv__srow--wide' : ''),
    'data-field-key': props.fieldKey,
    'data-field-source': props.source || '',
    children: [
      jsx('div', { className: 'hc-adv__srow-label', children: props.label }, 'l'),
      jsx('div', { className: 'hc-adv__srow-control', children: props.children }, 'c'),
      jsxs('div', {
        className: 'hc-adv__srow-meta',
        children: (props.meta || []).map(function (item, i) {
          return jsx('span', {
            className: 'hc-adv__srow-read' + (item.tone ? ' hc-adv__srow-read--' + item.tone : ''),
            title: item.title || '',
            'data-mode-fact': item.fact || undefined,
            children: item.text
          }, 'r' + i)
        }).concat([jsx(SourceChip, { source: props.source, title: props.sourceTitle }, 'src')])
      }, 'm')
    ]
  })
}

/**
 * 评审模式：**一个下拉（唯一可编辑入口）** + 两个只读读数（本轮生效值 / 来源层）。
 *
 * 三种模式的差异只由 `ModeGuide`（默认折叠）说一遍，这里不复述；「当前设置值」
 * 就是下拉自己选中的那一项，不再另起一行徽标重复它。
 */
function ModeField(props) {
  var mode = props.mode || 'auto'
  var data = props.data || {}
  var pending = props.pending
  var source = props.settingSource || 'default'
  var resolvedSource = data.resolved_mode_source || ''
  return jsxs('div', {
    className: 'hc-adv__srow hc-adv__srow--mode',
    'data-field-key': 'mode',
    'data-field-source': source,
    'data-mode-field': 'mode',
    // 这一行里**没有**第二个 agent 选择器：切换 agent 只走分区顶部那唯一一个
    'data-modes-agent-picker': 'no',
    children: [
      jsx('div', { className: 'hc-adv__srow-label', children: '评审模式' }, 'l'),
      jsxs('div', {
        className: 'hc-adv__srow-control',
        children: [
          jsx('select', {
            className: 'hc-select',
            'data-settings-field': 'mode',
            // 这个 select 本身就是「当前设置值」的唯一呈现：不再另起一行徽标复述它
            'data-mode-fact': 'setting',
            value: mode,
            onChange: function (event) {
              props.onChange(event.target.value)
            },
            children: [
              jsx('option', { value: 'auto', children: 'auto · 自动（按目标决定）' }, 'm-auto'),
              jsx('option', { value: 'target', children: 'target · 目标对齐（强制）' }, 'm-target'),
              jsx('option', { value: 'conservative', children: 'conservative · 保守（强制）' }, 'm-cons')
            ]
          }, 'sel')
        ]
      }, 'c'),
      jsxs('div', {
        className: 'hc-adv__srow-meta',
        children: [
          jsx('span', {
            className: 'hc-adv__srow-read hc-adv__srow-read--accent',
            'data-mode-fact': 'resolved',
            title: '本轮生效值由插件在每次评审时按该 turn 的目标推导（' +
              (MODE_RESOLVED_SOURCE_LABEL[resolvedSource] || '还没有评审记录') + '）',
            children: '生效：' + (data.resolved_label || '未知')
          }, 'resolved'),
          pending
            ? jsx('span', { className: 'hc-adv__srow-read hc-adv__srow-read--pending', 'data-mode-fact': 'pending', children: '待保存' }, 'p')
            : null,
          // 「可切换（按 agent）」由设置卡片标题旁那一个 chip 说，这一行不再重复
          jsx(SourceChip, {
            source: source,
            fact: 'source',
            always: true,
            title: 'mode 写在 ' + (props.path || '该 agent 的 settings.json')
          }, 'src')
        ]
      }, 'm')
    ]
  })
}

/**
 * 设置区：**一张表单** = 五项设置各**一个**可编辑入口 + 底部固定的保存/还原。
 *
 * 三条硬规矩保留（后来者最容易「顺手优化」掉的地方）：
 *   1. **没有批量开关**：一次只改一个 agent —— 作用域由上面那个唯一的 Agent 选择器
 *      决定（本组件不再自带选择器，否则就是第二个切换入口）。
 *   2. **显示生效值**：保存以后端回读的 ``values`` 重填表单与 baseline，不拿本地
 *      state 冒充已生效；落盘原文只在「技术详情」（默认收起）里查得到。
 *   3. 只读的 ``blocker_forced_delivery`` 只出现一次、只读一次；表单从不提交它。
 *
 * 版面收敛（本卡）：
 *   * 每一项设置 = 一行：标签 / 控件 / 只读读数 + 来源 chip。来源不再靠整张表；
 *   * 底部 ``SettingsTable`` 降级成默认收起的「技术详情」，只作可查性，不与表单并列；
 *   * 宿主层与白名单说明、批量入口说明这类开发备注全部收进「帮助」折叠区。
 */
function SettingsPanel(props) {
  var profile = props.profile || 'default'
  var query = useQuery('advisor.settings|' + profile, function () {
    return fetchSettings(profile)
  }, { staleMs: STALE_MS })
  // 评审模式的**生效值/来源层**由 /advisor/modes 给：按 agent 读 settings.json +
  // 该 agent 最近一次评审记录。设置值本身和其余四项走同一个 PUT，所以这里只读。
  var modeQuery = useQuery('advisor.modes|' + profile, function () {
    return fetchModes(profile)
  }, { staleMs: STALE_MS })
  var formState = useState(null)
  var form = formState[0]
  var setForm = formState[1]
  // baseline = 后端回读的生效值（保存成功后更新）。表单只跟它比差异，没改的键一个都不发。
  var baselineState = useState(null)
  var baseline = baselineState[0]
  var setBaseline = baselineState[1]
  var saveState = useState({ busy: false, error: '', done: '', applied: null, written: [], skipped: [] })
  var save = saveState[0]
  var setSave = saveState[1]

  var data = (query.data && query.data.data) || {}
  var values = data.values || null
  var forced = data.forced || {}
  var detail = data.values_detail || []
  var hostLayers = data.host_layers || {}
  // 「跟随全局」跟到哪（只读，来自该 agent 的 config.yaml → model 段）
  var modelPair = data.model_pair || {}
  var pairDraft = form
    ? describeModelPair(
      modelPair, form.provider, form.model,
      !baseline || form.provider !== baseline.provider || form.model !== baseline.model
    )
    : { kind: '', effective: '', text: '' }
  var modeData = (modeQuery.data && modeQuery.data.data) || {}
  var modeDetail = (detail.filter(function (row) { return row.key === 'mode' })[0]) || {}
  var modeSettingSource = modeDetail.source || (modeData.setting_source || 'default')
  // 逐项来源（每行右侧那个 chip 的取值来源）：后端 values_detail 是唯一事实，
  // 缺行才退回「默认层」——绝不编造第三层。
  var sourceOf = function (key) {
    var row = detail.filter(function (item) { return item.key === key })[0]
    return (row && row.source) || 'default'
  }

  // 切 profile 时 form 与 baseline 一起清空重来（否则会把上一个 agent 的草稿带过去）
  useEffect(
    function () {
      setForm(null)
      setBaseline(null)
      setSave({ busy: false, error: '', done: '', applied: null, written: [], skipped: [] })
    },
    [profile]
  )
  useEffect(
    function () {
      if (!values || baseline) return
      var fresh = normalize(values)
      setBaseline(fresh)
      setForm(fresh)
    },
    [values, baseline]
  )

  if (query.status === 'loading') return jsx(Skeleton, { lines: 4, label: '正在读取策略设置' })
  if (query.status === 'error') {
    // 错误态就地显示：不再放第二个 ProfilePicker（那会变成第二个切换入口），
    // 只写明「当前是哪个 agent 读不到」，换 agent 用上面那个唯一的选择器。
    return jsxs('div', {
      className: 'hc-adv__settings',
      'data-settings-profile': profile,
      children: [
        jsx(ErrorState, {
          title: '策略设置没读到（' + profile + '）',
          message: query.error,
          detail: '接口 /advisor/settings?agent=' + profile + '　·　换 agent 请用上面的 Agent 选择器',
          onRetry: query.reload,
          retryLabel: '重试设置'
        })
      ]
    })
  }

  function update(patch) {
    setForm(Object.assign({}, form, patch))
    setSave({ busy: false, error: '', done: '', applied: null, written: [], skipped: [] })
  }

  function submit() {
    var interval = Number(form.min_interval_seconds)
    if (!isFinite(interval) || interval < 0) {
      setSave({
        busy: false, error: 'min_interval_seconds 必须是不小于 0 的数字（后端也会 400）',
        done: '', applied: null, written: [], skipped: []
      })
      return
    }
    var diff = diffKeys(form, baseline)
    // 没改动就**不发请求**：未改动的键零写请求，这是硬要求，不是优化
    if (!diff.changed.length) {
      setSave({
        busy: false, error: '', done: '没有改动：未发送任何写请求（' + profile + ' 的 settings.json 未动）',
        applied: null, written: [], skipped: []
      })
      return
    }
    setSave({ busy: true, error: '', done: '', applied: null, written: [], skipped: [] })
    send(
      '/advisor/settings?agent=' + encodeURIComponent(profile),
      diff.patch,
      { method: 'PUT' }
    ).then(function (res) {
      if (!res || res.ok === false) {
        setSave({
          busy: false,
          error: (res && res.message) || '保存失败',
          done: '', applied: null, written: [], skipped: []
        })
        return
      }
      var body = res.data || {}
      // res.data 是**整个响应体**（{ok, data, notes}），设置本体在它的 .data 里。
      // 直接取 res.data.values 永远是 undefined，回读就会退化成「本地默认值」——
      // 表单于是显示 auto/enabled=false，而后端其实是刚写进去的值（真机上就是
      // 「保存了但界面回退」）。这里显式下钻一层，读不到才退回响应体本身。
      var payload = body.data || body
      // 以**后端回读值**为准重填表单与 baseline：提交值 ≠ 生效值时，界面必须显示生效的那个
      var readback = normalize(payload.values || {})
      setBaseline(readback)
      setForm(readback)
      var written = payload.written_keys || []
      var skipped = payload.skipped_keys || []
      setSave({
        busy: false, error: '',
        done: payload.no_change
          ? '后端判定无变化：没有写入任何东西（' + profile + '）'
          : '已保存并回读确认：' + profile + '（写入 ' + written.join(' / ') + '）',
        applied: payload.applied || null,
        written: written,
        skipped: skipped
      })
      query.reload()
      // 模式块的生效值/来源层也要重取：刚写进去的 mode 必须立刻在「生效值」上体现，
      // 而且必须来自后端回读，不是本地 form。
      modeQuery.reload()
      if (props.onSaved) props.onSaved()
    })
  }

  function reset() {
    if (!baseline) return
    setForm(Object.assign({}, baseline))
    setSave({ busy: false, error: '', done: '已还原为后端生效值（未发送任何请求）', applied: null, written: [], skipped: [] })
  }

  var diffNow = diffKeys(form, baseline)
  var dirty = diffNow.changed
  var enabledPending = Boolean(form && baseline && form.enabled !== baseline.enabled)

  return jsxs('div', {
    className: 'hc-adv__settings',
    'data-settings-profile': profile,
    children: [
      jsxs('div', {
        className: 'hc-adv__block-head',
        children: [
          // 不再重复写「策略设置」标题：外层 `data-block="detail-settings"` 已经有一个
          // （`advisor.js` 主渲染里 `'dt'` 那个）。同一张卡上两行同名标题就是「双层套娃」，
          // 这里只留读状态与来源的 chip。
          jsx('span', { className: 'hc-chip', children: profile }, 'p'),
          jsx('span', {
            className: 'hc-adv__mode-fixed',
            'data-modes-switchable-chip': 'yes',
            title: 'mode 是设置项（auto / target / conservative），按 agent 独立保存在它自己的 settings.json 里',
            children: '可切换（按 agent）'
          }, 'sc'),
          // 这一张卡里所有字段的默认来源都是「该 agent 的 settings.json」：在这里说一次，
          // 表单行里只标偏离常规的（默认值 / 强制常量），不必每行挂一个同样的 chip。
          jsx(SourceChip, {
            source: data.source === 'file' ? 'settings_file' : 'default',
            always: true,
            title: data.path || ''
          }, 'cardsrc'),
          jsx('span', {
            className: 'hc-chip' + (dirty.length ? ' hc-chip--warn' : ''),
            'data-settings-dirty': dirty.length ? dirty.join(',') : '',
            title: dirty.length
              ? '只提交这些键：' + dirty.join(' / ') + '（其余一个都不写）'
              : '没有改动：保存按钮不发请求',
            children: dirty.length ? '待保存 ' + dirty.length + ' 项' : '无改动'
          }, 'd')
        ]
      }, 'head'),
      // 总闸：enabled=false 必须一眼看出来；要打开必须先看到后果
      jsx('div', {
        className: 'hc-adv__gate' + (data.enabled === false ? ' hc-adv__gate--off' : ' hc-adv__gate--on')
          + (enabledPending ? ' hc-adv__gate--pending' : ''),
        role: 'status',
        'data-gate-enabled': data.enabled === false ? 'false' : 'true',
        'data-gate-dirty': enabledPending ? 'yes' : 'no',
        children: data.enabled === false ? data.enabled_notice : (data.enabled_notice || '')
      }, 'gate'),
      enabledPending
        ? jsx('div', {
            className: 'hc-adv__gate-note',
            'data-gate-change': form.enabled ? 'turn-on' : 'turn-off',
            children: form.enabled
              ? '待保存：打开 enabled。' + (data.enabled_effect || '')
              : '待保存：关闭 enabled —— 保存后 advisor 不再评审任何 turn（建议仍会留在台账里）。'
          }, 'gate-note')
        : null,
      form
        ? jsxs('div', {
            className: 'hc-adv__form',
            children: [
              jsx(SettingsRow, {
                fieldKey: 'enabled',
                label: '启用评审',
                source: sourceOf('enabled'),
                meta: [{
                  text: data.enabled === false ? '当前：关' : '当前：开',
                  tone: data.enabled === false ? 'warn' : ''
                }],
                children: jsxs('label', {
                  className: 'hc-adv__switch',
                  children: [
                    jsx('input', {
                      type: 'checkbox',
                      'data-settings-field': 'enabled',
                      checked: form.enabled === true,
                      onChange: function (event) {
                        update({ enabled: event.target.checked })
                      }
                    }),
                    jsx('span', {
                      className: 'hc-adv__switch-text',
                      children: form.enabled === true ? '开' : '关'
                    })
                  ]
                })
              }, 'row-enabled'),
              // 模式：一行设置（唯一入口）+ 一段「三种模式的区别」（默认收起）。
              // 外层带 data-modes-* ：探针用它读「设置值 / 生效值 / 来源层」的**唯一**读数点。
              jsxs('div', {
                className: 'hc-adv__modeblock',
                'data-modes-switchable': modeData.switchable ? 'yes' : 'no',
                'data-modes-setting': modeData.current_setting || '',
                'data-modes-current': modeData.resolved_mode || '',
                'data-modes-resolved': modeData.resolved_mode || '',
                'data-modes-source': modeSettingSource,
                'data-modes-agent': profile,
                // 这个块里**没有**第二个 agent 选择器：切换 agent 只走上面那一个
                'data-modes-agent-picker': 'no',
                'data-mode-current-label': modeData.current_setting_label || '',
                children: [
                  jsx(ModeField, {
                    mode: form.mode,
                    pending: form.mode !== baseline.mode,
                    data: modeData,
                    agent: profile,
                    path: data.path,
                    settingSource: modeSettingSource,
                    onChange: function (value) {
                      update({ mode: value })
                    }
                  }, 'row-mode'),
                  // 三种模式的差异只在这里写一遍（默认收起）；设置值本身由上面那个下拉表达
                  jsx(ModeGuide, { data: modeData, mode: form.mode }, 'mode-guide')
                ]
              }, 'mode-block'),
              jsx(SettingsRow, {
                fieldKey: 'concern_default',
                label: 'CONCERN 默认投递',
                source: sourceOf('concern_default'),
                meta: [{ text: form.concern_default === 'hold' ? '先扣住' : '直接交付' }],
                children: jsx('select', {
                  className: 'hc-select',
                  'data-settings-field': 'concern_default',
                  value: form.concern_default,
                  onChange: function (event) {
                    update({ concern_default: event.target.value })
                  },
                  children: [
                    jsx('option', { value: 'hold', children: 'hold · 先扣住' }, 'hold'),
                    jsx('option', { value: 'deliver', children: 'deliver · 直接交付' }, 'deliver')
                  ]
                })
              }, 'row-concern'),
              jsx(SettingsRow, {
                fieldKey: 'min_interval_seconds',
                label: '最小间隔',
                source: sourceOf('min_interval_seconds'),
                meta: [{ text: '秒' }],
                children: jsx('input', {
                  type: 'number',
                  min: '0',
                  step: '1',
                  className: 'hc-adv__input',
                  'data-settings-field': 'min_interval_seconds',
                  value: String(form.min_interval_seconds),
                  onChange: function (event) {
                    update({ min_interval_seconds: event.target.value })
                  }
                })
              }, 'row-interval'),
              jsxs('div', {
                className: 'hc-adv__srow hc-adv__srow--wide',
                'data-field-key': 'model',
                'data-field-source': pairDraft.kind || '',
                children: [
                  jsx('div', { className: 'hc-adv__srow-label', children: '评审模型' }, 'l'),
                  jsxs('div', {
                    className: 'hc-adv__srow-control',
                    children: [
                      // 两级联动（共享组件 ProviderModelPicker）：先按该 agent 的可用 provider 筛，
                      // 再按选中的 provider 筛模型；provider 变了旧 model 立即清空。
                      // 落盘仍走同一个 PUT /advisor/settings?agent=，保存后由后端回读。
                      jsx(ProviderModelPicker, {
                        agent: profile,
                        value: { provider: form.provider, model: form.model },
                        allowFollow: true,
                        allowEmptyModel: true,
                        sourceKind: form.provider ? 'agent' : 'global',
                        idPrefix: 'advisor',
                        disabled: save.busy,
                        onChange: function (next) {
                          update({ provider: next.provider, model: next.model })
                        }
                      }, 'picker'),
                      // 来源层：跟随全局 / 该 Agent 独立配置，以及「跟到的是哪一组」（一行，不重复）
                      jsx('div', {
                        className: 'hc-adv__hint hc-adv__pair',
                        'data-advisor-model-pair': pairDraft.kind || '',
                        'data-advisor-model-effective': pairDraft.effective || '',
                        'data-advisor-model-inherit': (modelPair.provider || '') + '/' + (modelPair.model || ''),
                        'data-advisor-model-inherit-path': modelPair.config_path || '',
                        title: modelPair.note || '',
                        children: pairDraft.text
                      }, 'pair')
                    ]
                  }, 'c'),
                  jsxs('div', {
                    className: 'hc-adv__srow-meta',
                    children: [
                      jsx('span', {
                        className: 'hc-adv__srow-read',
                        title: '当前实际会用的那一组（provider / model）',
                        children: pairDraft.effective || '—'
                      }, 'eff'),
                      jsx(SourceChip, {
                        source: pairDraft.kind === 'settings_file' ? 'settings_file'
                          : (pairDraft.kind === 'config_yaml' ? 'default' : 'default'),
                        title: pairDraft.kind === 'settings_file'
                          ? '该 Agent 独立配置（settings.json 里写了 provider / model）'
                          : (pairDraft.kind === 'config_yaml'
                            ? '跟随全局：该 Agent 的 config.yaml → model 段'
                            : '无可用默认：config.yaml 没有 model 段，宿主用会话当前模型')
                      }, 'src')
                    ]
                  }, 'm')
                ]
              }, 'row-model'),
              // ---- 提交动作固定在表单底部：上面是待保存内容，下面是提交 ----
              jsxs('div', {
                className: 'hc-adv__form-actions',
                children: [
                  jsx('span', {
                    className: 'hc-adv__hint hc-adv__form-note',
                    children: dirty.length
                      ? '待保存：' + dirty.join(' / ') + '（只发这些键）'
                      : '没有改动：保存按钮不发请求'
                  }, 'note'),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn',
                    disabled: save.busy || !dirty.length,
                    onClick: reset,
                    children: '还原'
                  }, 'reset'),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn hc-btn--accent',
                    disabled: save.busy,
                    'data-settings-save': dirty.length ? 'dirty' : 'clean',
                    title: dirty.length
                      ? '只提交改动过的键：' + dirty.join(' / ')
                      : '没有改动：点了也不会发请求',
                    onClick: submit,
                    children: save.busy ? '保存中…' : (dirty.length ? '保存设置（' + dirty.length + ' 项）' : '保存设置')
                  }, 'save')
                ]
              }, 'actions'),
              save.error
                ? jsx('div', { className: 'hc-adv__formerr', role: 'alert', children: save.error }, 'err')
                : null,
              save.done
                ? jsx('div', { className: 'hc-adv__formsaved', 'data-save-done': 'yes', children: save.done }, 'done')
                : null,
              save.written && save.written.length
                ? jsx('div', {
                    className: 'hc-adv__hint',
                    'data-save-written': save.written.join(','),
                    'data-save-skipped': (save.skipped || []).join(','),
                    children: '本次写入：' + save.written.join(' / ')
                      + ((save.skipped || []).length
                        ? '　·　未改动（没写）：' + save.skipped.join(' / ')
                        : '')
                  }, 'written')
                : null
            ]
          })
        : null,
      // ---- 技术详情 + 只读事实：**一个**折叠区（原来是两个 ▸，内容都是「对账/只读」，
      // 合起来放；默认收起）。保留可查性（生效值 / 来源层 / 落盘值 / 默认值 / 宿主层 /
      // 单向阀门 / 写入规则），但不再与表单并列争夺注意力 —— 表单已经能改这五项。
      jsxs('details', {
        className: 'hc-adv__tech hc-adv__help',
        'data-settings-tech': 'collapsed',
        'data-settings-help': 'collapsed',
        children: [
          jsx('summary', {
            children: '技术详情与只读事实：生效值 / 来源层 / 落盘值 / 默认值 · 宿主层 · 单向阀门 · 写入规则'
          }, 'sum'),
          detail.length
            ? jsxs('div', {
                className: 'hc-adv__tech-body',
                children: [
                  jsx(SettingsTable, { detail: detail }, 'table'),
                  jsx('div', {
                    className: 'hc-adv__hint',
                    'data-settings-path': data.path || '',
                    children: '读写文件：' + (data.path || '（未知路径）')
                      + '　·　当前 agent：' + profile
                  }, 'path')
                ]
              }, 'tech-body')
            : null,
          jsxs('div', {
            className: 'hc-adv__help-body',
            children: [
              jsxs('div', {
                className: 'hc-adv__hostlayers',
                'data-host-layers': 'readonly',
                children: [
                  jsx('div', { className: 'hc-adv__mode-ev-title', children: '宿主层（只读，本面板不代改 config.yaml）' }, 't'),
                  jsxs('div', {
                    className: 'hc-adv__hostlayer-row',
                    children: [
                      jsx('span', { className: 'hc-adv__stable-key', children: 'plugins.enabled' }, 'k'),
                      jsx('span', {
                        className: 'hc-adv__stable-value',
                        'data-host-enabled': (hostLayers.plugins_enabled || {}).value ? 'yes' : 'no',
                        children: (hostLayers.plugins_enabled || {}).value ? '含 advisor' : '不含 advisor'
                      }, 'v'),
                      jsx('span', { className: 'hc-adv__hint', title: (hostLayers.plugins_enabled || {}).key || '', children: '决定插件会不会被加载' }, 'h')
                    ]
                  }, 'r1'),
                  jsxs('div', {
                    className: 'hc-adv__hostlayer-row',
                    children: [
                      jsx('span', {
                        className: 'hc-adv__stable-key',
                        title: (hostLayers.allow_gateway_injection || {}).key || '',
                        children: 'allow_gateway_injection'
                      }, 'k'),
                      jsx('span', {
                        className: 'hc-adv__stable-value',
                        'data-host-injection': (hostLayers.allow_gateway_injection || {}).value ? 'yes' : 'no',
                        children: (hostLayers.allow_gateway_injection || {}).value ? 'true' : '不是 true'
                      }, 'v'),
                      jsx('span', { className: 'hc-adv__hint', children: '决定建议投不投得进会话' }, 'h')
                    ]
                  }, 'r2'),
                  jsx('div', {
                    className: 'hc-adv__hint',
                    'data-host-config': hostLayers.config_path || '',
                    children: hostLayers.note || ''
                  }, 'note')
                ]
              }, 'host'),
              jsxs('div', {
                className: 'hc-adv__readonly',
                'data-readonly': 'blocker_forced_delivery',
                children: [
                  jsx('span', { className: 'hc-adv__readonly-label', children: 'blocker_forced_delivery' }, 'l'),
                  jsx('span', {
                    className: 'hc-adv__readonly-value',
                    children: forced.blocker_forced_delivery === false ? 'false' : 'true'
                  }, 'v'),
                  jsx('span', { className: 'hc-adv__readonly-note', children: '程序强制，不可关闭' }, 'n'),
                  jsx('span', {
                    className: 'hc-adv__hint',
                    children: '单向阀门：blocker 永远强制交付。这里没有关闭 blocker 提示或降级 blocker 的开关，它也不在可写白名单里。'
                  }, 'h')
                ]
              }, 'readonly'),
              jsx('div', {
                className: 'hc-adv__hint',
                children: '写入规则：只写后端白名单字段（' + (data.writable || []).join(' / ') + '），'
                  + '未改动的键一个都不发；一次只改一个 agent（上面那个 Agent 选择器），没有「全部打开」这种批量入口。'
              }, 'rules')
            ]
          }, 'help-body')
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// 分区主体
// ---------------------------------------------------------------------------
/** 唯一的 Agent 作用域选择器：同时决定覆盖矩阵高亮 / 建议列表范围 / 设置编辑对象。
 *
 * 原来这三件事各有一个选择器（外壳顶栏的「作用域」是另一个分区的概念、设置区自己的
 * `ProfilePicker`、模式块里又写一个只读 chip），用户看到的是「同一个东西让我选两遍」。
 * 现在只留这一个：它在哪儿改，上面三个地方就一起变；`全部 agent` 时只影响前两项
 * （没有具体 agent 就没有可编辑的设置对象，设置卡片会写明这一点，而不是偷偷编辑别的）。
 */
function ScopeBar(props) {
  var profiles = props.profiles || []
  var value = props.value || ''
  var coverage = props.coverage || []
  return jsxs('div', {
    className: 'hc-adv__scope',
    'data-adv-scope': value || 'all',
    children: [
      jsxs('label', {
        className: 'hc-adv__scope-pick',
        children: [
          jsx('span', { className: 'hc-adv__scope-label', children: 'Agent' }, 'l'),
          jsx('select', {
            className: 'hc-select',
            'data-filter': 'agent',
            value: value,
            title: '本面板的唯一 Agent 作用域：同时决定覆盖矩阵高亮 / 建议列表范围 / 策略设置编辑对象。'
              + '（右上角那个「作用域」是外壳的导航作用域，管的是整个控制台读哪个 profile 的配置，与本面板的列表无关。）',
            onChange: function (event) {
              props.onChange(event.target.value)
            },
            children: [jsx('option', { value: '', children: '全部 agent' }, 'agent-all')]
              .concat(profiles.map(function (name) {
                return jsx('option', { value: name, children: name }, 'agent-' + name)
              }))
          }, 'sel')
        ]
      }, 'pick'),
      jsxs('div', {
        className: 'hc-adv__scope-facts',
        children: [
          jsx('span', {
            className: 'hc-adv__scope-fact',
            title: '覆盖矩阵里一共扫到多少个 profile',
            children: num(coverage.length) + ' 个 agent'
          }, 'f1'),
          jsx('span', {
            className: 'hc-adv__scope-hint',
            children: value
              ? '下面三处都跟着它：矩阵高亮、建议列表、策略设置'
              : '列表看全部 agent；选一个具体 agent 才能编辑它的设置'
          }, 'hint')
        ]
      }, 'facts')
    ]
  })
}

export function AdvisorSection() {
  // 唯一的 Agent 选择器默认落在一个**具体** agent 上（优先 default）：这样打开页面就有
  // 高亮的矩阵行 + 定范围的列表 + 可编辑的设置，三处天然一致。「全部 agent」是它的一个
  // 选项（只看列表/矩阵汇总），不是另一套作用域。
  var filterState = useState({ status: '', severity: '', q: '', agent: 'default' })
  var filters = filterState[0]
  var setFilters = filterState[1]
  var inputState = useState('')
  var qInput = inputState[0]
  var setQInput = inputState[1]
  var offsetState = useState(0)
  var offset = offsetState[0]
  var setOffset = offsetState[1]
  var openState = useState('')
  var openId = openState[0]
  var setOpenId = openState[1]
  var nonceState = useState(0)
  var detailNonce = nonceState[0]
  var setDetailNonce = nonceState[1]

  var coverageQuery = useQuery('advisor.coverage', fetchCoverage, { staleMs: STALE_MS })
  var summaryQuery = useQuery('advisor.summary|' + filters.agent, function () {
    return fetchSummary(filters.agent)
  }, { staleMs: STALE_MS })
  // 诊断（评审流水 / 错误归类 / 飞行中 / 日志旁证）**跨全部 agent**：它回答的是
  // 「这套评审在整机上健康吗」，不是「当前这个 agent 的建议长什么样」。所以它的
  // 取数不带 agent（与覆盖矩阵同口径），而不是跟着唯一的 Agent 选择器走。
  var reviewsQuery = useQuery('advisor.reviews|', function () {
    return fetchReviews('')
  }, { staleMs: STALE_MS })
  var listQuery = useQuery(
    'advisor.list|' + filters.agent + '|' + filters.status + '|' + filters.severity
      + '|' + filters.q + '|' + offset,
    function () {
      return fetchList(filters, offset)
    },
    { staleMs: STALE_MS }
  )

  var coverageRows = (coverageQuery.data && coverageQuery.data.data && coverageQuery.data.data.profiles) || []
  var listPayload = (listQuery.data && listQuery.data.data) || {}
  // 唯一选择器的选项 = 覆盖矩阵扫到的 profile **∪** 台账里出现过的 agent。
  // 只取前者的坑：台账里可能有覆盖矩阵没扫到的 agent（例如 plugins/advisor 已卸载、
  // 但历史建议还在台账里），那些 agent 的建议就再也筛不出来了。
  var profiles = []
  coverageRows.forEach(function (row) {
    if (row && row.profile && profiles.indexOf(row.profile) === -1) profiles.push(row.profile)
  })
  ;(listPayload.known_agents || []).forEach(function (name) {
    if (name && profiles.indexOf(name) === -1) profiles.push(name)
  })
  if (filters.agent && profiles.indexOf(filters.agent) === -1) profiles = profiles.concat([filters.agent])

  function applyFilters(patch) {
    var next = Object.assign({}, filters, patch)
    setFilters(next)
    setOffset(0)
    // 换 agent = 换对象：详情属于旧 agent 的一条建议，留着会让人以为它属于新 agent。
    if (Object.prototype.hasOwnProperty.call(patch, 'agent')) setOpenId('')
  }

  function reloadAll() {
    coverageQuery.reload()
    summaryQuery.reload()
    listQuery.reload()
    reviewsQuery.reload()
  }

  function onDecided(id) {
    // 决定写完：列表 / 统计 / 这条详情全部重取（缓存 key 变一次，旧缓存不参与）
    listQuery.reload()
    summaryQuery.reload()
    setOpenId(id)
    setDetailNonce(detailNonce + 1)
  }

  var payload = listPayload
  var items = payload.items || []
  var total = typeof payload.total === 'number' ? payload.total : items.length
  // 列表接口的 notes 前若干条是**同一份** per-agent 口径说明（`_state_note`），上面的统计条
  // （StatusBar）已经在渲染 summary 的 notes，再原样打印一遍就是同一段绝对路径说两次。
  // 这里只保留「统计条没说过」的补充（评审出错 / 无终态 / 日志旁证）。
  var summaryNotes = (summaryQuery.data && summaryQuery.data.notes) || []
  var coverageNotes = (coverageQuery.data && coverageQuery.data.notes) || []
  var notes = ((listQuery.data && listQuery.data.notes) || []).filter(function (note) {
    if (summaryNotes.indexOf(note) !== -1) return false
    // 同一 profile 的口径说明：即使措辞有细微差别也算同一条（前缀 = "<profile>："）
    var head = String(note).split('：')[0] + '：'
    return !summaryNotes.some(function (known) {
      return String(known).indexOf(head) === 0
    })
  })
  var first = total === 0 ? 0 : offset + 1
  var last = offset + items.length

  function renderList() {
    if (listQuery.status === 'loading') return jsx(Skeleton, { lines: 6, label: '正在读取建议列表' })
    if (listQuery.status === 'error') {
      return jsx(ErrorState, {
        title: '建议列表没读到',
        message: listQuery.error,
        detail: '接口 /advisor/advice',
        onRetry: listQuery.reload,
        retryLabel: '重试列表'
      })
    }
    if (!items.length) {
      return jsx(Empty, {
        title: '还没有建议',
        hint:
          '当前范围内没有建议。上面覆盖矩阵里每个 agent 的状态可以区分三种情况：' +
          '有建议 / 有台账但无建议（评审跑过、没发现问题）/ 无台账文件（该 agent 从未评审，' +
          '或它的插件副本还没有审计层）。如果只是筛选太窄，先把「全部状态」选回来。'
      })
    }
    return jsxs('div', {
      className: 'hc-adv__table',
      children: [
        jsxs('div', {
          className: 'hc-adv__head',
          children: [
            jsx('div', { className: 'hc-adv__cell hc-adv__cell--time', children: '时间' }),
            jsx('div', { className: 'hc-adv__cell hc-adv__cell--sev', children: '严重度' }),
            jsx('div', { className: 'hc-adv__cell hc-adv__cell--note', children: '建议摘要' }),
            jsx('div', { className: 'hc-adv__cell hc-adv__cell--status', children: '状态' }),
            jsx('div', { className: 'hc-adv__cell hc-adv__cell--source', children: '来源' }),
            jsx('div', { className: 'hc-adv__cell hc-adv__cell--ops', children: '' })
          ]
        }),
        items.map(function (item) {
          return jsx(
            AdviceRow,
            {
              item: item,
              active: openId === item.id,
              onToggle: function (id) {
                setOpenId(openId === id ? '' : id)
              }
            },
            item.id
          )
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-adv',
    children: [
      jsx(ScopeBar, {
        profiles: profiles,
        value: filters.agent,
        coverage: coverageRows,
        onChange: function (name) {
          applyFilters({ agent: name })
        }
      }, 'scope'),
      // 诊断默认只占一行（几个 profile 无台账 / 几次超时 / 几次无终态），点开才是明细
      jsx(DiagnosticsPanel, { reviews: reviewsQuery, coverageRows: coverageRows }, 'diag'),
      jsx(CoverageStatusBar, { query: coverageQuery }, 'covbar'),
      jsxs('div', {
        className: 'hc-adv__block',
        'data-block': 'coverage',
        children: [
          jsxs('div', {
            className: 'hc-adv__block-head',
            children: [
              jsx('span', { className: 'hc-adv__block-title', children: '覆盖矩阵' }, 'ct'),
              jsx('span', {
                className: 'hc-chip',
                children: num(coverageRows.length) + ' 个 agent'
              }, 'cc'),
              jsx('span', {
                className: 'hc-chip',
                title: '一行一个 agent：收起态是「名字 + 状态 + 主操作」，点箭头展开四个独立维度'
                  + '（插件运行 / 参与评审 / 允许注入 / 其它操作），互不联动，均需二次确认',
                children: '点箭头展开四维'
              }, 'cd')
            ]
          }, 'cov-head'),
          jsx(CoverageMatrix, {
            query: coverageQuery,
            selected: filters.agent,
            onChanged: reloadAll
          }, 'cov-matrix')
        ]
      }),
      jsx(StatusBar, { query: summaryQuery }),
      jsxs('div', {
        className: 'hc-adv__block',
        'data-block': 'list',
        children: [
          jsxs('div', {
            className: 'hc-adv__block-head',
            children: [
              jsx('span', { className: 'hc-adv__block-title', children: '建议列表' }, 't'),
              jsx('span', {
                className: 'hc-chip',
                children: total === 0 ? '0 条' : '第 ' + num(first) + '–' + num(last) + ' 条 / 共 ' + num(total) + ' 条'
              }, 'c'),
              jsx('span', {
                className: 'hc-chip',
                title: '读的是 advisor 台账（按 profile 分文件后聚合）；范围由上面那个 Agent 选择器决定',
                children: filters.agent ? filters.agent : '全部 agent 聚合'
              }, 'a')
            ]
          }, 'list-head'),
          jsxs('div', {
            className: 'hc-adv__filters',
            children: [
              jsxs('label', {
                className: 'hc-adv__filter',
                children: [
                  jsx('span', { className: 'hc-adv__filter-label', children: '状态' }, 'l'),
                  jsx('select', {
                    className: 'hc-select',
                    'data-filter': 'status',
                    value: filters.status,
                    onChange: function (event) {
                      applyFilters({ status: event.target.value })
                    },
                    children: STATUS_FILTERS.map(function (option) {
                      return jsx('option', { value: option.id, children: option.label }, option.id || 'all')
                    })
                  }, 's')
                ]
              }),
              jsxs('label', {
                className: 'hc-adv__filter',
                children: [
                  jsx('span', { className: 'hc-adv__filter-label', children: '严重度' }, 'l'),
                  jsx('select', {
                    className: 'hc-select',
                    'data-filter': 'severity',
                    value: filters.severity,
                    onChange: function (event) {
                      applyFilters({ severity: event.target.value })
                    },
                    children: SEVERITY_FILTERS.map(function (option) {
                      return jsx('option', { value: option.id, children: option.label }, option.id || 'all')
                    })
                  }, 's')
                ]
              }),
              jsxs('label', {
                className: 'hc-adv__filter hc-adv__filter--grow',
                children: [
                  jsx('span', { className: 'hc-adv__filter-label', children: '关键词' }, 'l'),
                  jsx('input', {
                    type: 'text',
                    className: 'hc-adv__input',
                    'data-filter': 'q',
                    value: qInput,
                    placeholder: '正文 / id / 会话 / 原因',
                    onChange: function (event) {
                      setQInput(event.target.value)
                    },
                    onKeyDown: function (event) {
                      if (event.key === 'Enter') applyFilters({ q: qInput })
                    }
                  }, 'i')
                ]
              }),
              jsx('button', {
                type: 'button',
                className: 'hc-btn',
                onClick: function () {
                  applyFilters({ q: qInput })
                },
                children: '搜索'
              }, 'search'),
              jsx('button', {
                type: 'button',
                className: 'hc-btn',
                disabled: summaryQuery.busy || listQuery.busy,
                title: '重新读取统计与当前页（不触发任何写操作）',
                onClick: reloadAll,
                children: '刷新数据'
              }, 'refresh')
            ]
          }),
          // 对账口径（覆盖口径附注 + 每个 agent 的建议/评审计数）默认收起：
          // 原来这是**两个**折叠项（覆盖区一个、列表区一个），内容都是「对账用的统计口径」，
          // 合并成一个，别再让页面从上到下散着一排 ▸。
          (payload.agents && payload.agents.length) || coverageNotes.length
            ? jsxs('details', {
                className: 'hc-adv__ledger',
                'data-ledger-agents': (payload.agents || []).length,
                'data-coverage-notes': coverageNotes.length,
                children: [
                  jsx('summary', {
                    children: '口径对账：覆盖口径附注（' + num(coverageNotes.length) + ' 条）'
                      + ' + 每个 agent 的建议 / 评审计数'
                      + ((payload.agents || []).length ? '（' + num(payload.agents.length) + ' 个 agent）' : '')
                  }, 'sum'),
                  coverageNotes.length
                    ? jsx('div', {
                        className: 'hc-adv__notes',
                        children: coverageNotes.map(function (note, i) {
                          return jsx('div', { className: 'hc-adv__note', children: note }, 'cn' + i)
                        })
                      }, 'cov-notes')
                    : null,
                  payload.agents && payload.agents.length
                    ? jsx(AgentStrip, { agents: payload.agents }, 'strip')
                    : null
                ]
              }, 'ledger')
            : null,
          renderList(),
          total > PAGE_SIZE
            ? jsxs('div', {
                className: 'hc-adv__pager',
                children: [
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn',
                    disabled: offset <= 0,
                    onClick: function () {
                      setOffset(Math.max(0, offset - PAGE_SIZE))
                    },
                    children: '上一页'
                  }, 'prev'),
                  jsx('span', {
                    className: 'hc-adv__hint',
                    children:
                      '第 ' + num(first) + '–' + num(last) + ' 条 / 共 ' + num(total) + ' 条 · 每页 ' + PAGE_SIZE + ' 条'
                  }, 'pos'),
                  jsx('button', {
                    type: 'button',
                    className: 'hc-btn',
                    disabled: offset + PAGE_SIZE >= total,
                    onClick: function () {
                      setOffset(offset + PAGE_SIZE)
                    },
                    children: '下一页'
                  }, 'next')
                ]
              })
            : null,
          notes.length
            ? jsx('div', {
                className: 'hc-adv__notes',
                children: notes.map(function (note, i) {
                  return jsx('div', { className: 'hc-adv__note', children: note }, 'ln' + i)
                })
              })
            : null
        ]
      }),
      // 详情紧贴列表下方：文案写「点上面列表右端的详情」，这里就在它上面，不再倒错
      openId
        ? jsx(DetailPanel, { id: openId, nonce: detailNonce, onDecided: onDecided }, 'detail-' + openId)
        : null,
      jsxs('div', {
        className: 'hc-adv__block hc-adv__detail-settings',
        'data-block': 'detail-settings',
        children: [
          jsxs('div', {
            className: 'hc-adv__block-head',
            children: [
              jsx('span', { className: 'hc-adv__block-title', children: '策略设置' }, 'dt'),
              jsx('span', {
                className: 'hc-chip',
                title: '编辑对象就是上面那个 Agent 选择器选中的 agent；一次只改一个',
                children: filters.agent || '先选一个 agent'
              }, 'dc')
            ]
          }, 'dh'),
          filters.agent
            ? jsx(SettingsPanel, {
                profile: filters.agent,
                // 保存成功后让诊断区与矩阵一起重取（评审开关真值来自 /advisor/modes）
                onSaved: function () {
                  reviewsQuery.reload()
                  coverageQuery.reload()
                }
              }, 'settings')
            : jsx('div', {
                className: 'hc-adv__settings hc-adv__settings--idle',
                'data-settings-profile': '',
                children: jsx('div', {
                  className: 'hc-adv__hint',
                  children: '设置是按 agent 落盘的：把上面的 Agent 选择器切到一个具体 agent，这里就会显示它的五项设置'
                    + '（启用评审 / 评审模式 / CONCERN 默认投递 / 最小间隔 / 评审模型）。'
                    + '「全部 agent」只是列表范围，没有可编辑对象。'
                })
              }, 'settings-idle')
        ]
      }, 'detail-settings')
    ]
  })
}
