/**
 * hermes-console · Advisor 审核台真 DOM 验收（C9b：真 React 19 + jsdom）
 *
 * 与 probe-dom.mjs 的分工：那边验外壳与其它分区，这边只验 Advisor 分区：
 * 导航入口 / 状态条四个数字 + 口径 / 建议列表行 / severity 三档徽标 / 分页 /
 * 详情 + 事件时间线 / 采纳 / 拒绝（缺原因被前端拦住，不发请求）/ 策略设置（含只读阀门徽标）/
 * 三态切换（Skeleton → Empty → ErrorState）/ 窄窗折叠（无横向滚动条）。
 *
 * 跑法：node plugins/hermes-console/scripts/probe-advisor.mjs
 */
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, rmdirSync, symlinkSync, unlinkSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..')
const ENTRY = join(REPO, 'dashboard/dist/index.js')
const HOST_NODE_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) {
    pass += 1
    console.log('  PASS ' + name + (extra ? '  — ' + extra : ''))
  } else {
    failures.push(name + (extra ? '  — ' + extra : ''))
    console.log('  FAIL ' + name + (extra ? '  — ' + extra : ''))
  }
}

// ------------------------------------------------- 临时模块树（junction 到宿主）
const WORK = mkdtempSync(join(tmpdir(), 'hc-adv-'))
mkdirSync(WORK, { recursive: true })
try {
  symlinkSync(HOST_NODE_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_NODE_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { JSDOM } = requireFromWork('jsdom')

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  pretendToBeVisual: true,
  url: 'http://localhost/'
})
const { window } = dom
globalThis.window = window
globalThis.document = window.document
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true })
globalThis.HTMLElement = window.HTMLElement
globalThis.Element = window.Element
globalThis.Node = window.Node
globalThis.Event = window.Event
globalThis.MouseEvent = window.MouseEvent
globalThis.getComputedStyle = window.getComputedStyle
globalThis.requestAnimationFrame = window.requestAnimationFrame
globalThis.cancelAnimationFrame = window.cancelAnimationFrame
globalThis.IS_REACT_ACT_ENVIRONMENT = true

const React = requireFromWork('react')
const { createRoot } = requireFromWork('react-dom/client')
const { act } = React

const entryCopy = join(WORK, 'entry_adv.js')
copyFileSync(ENTRY, entryCopy)
const pluginApi = await import(pathToFileURL(entryCopy).href)
const plugin = pluginApi.default

const AGENTS = {
  agents: [
    { id: 'default', label: '小白', is_default: true, has_config: true },
    { id: 'code-worker', label: 'code-worker', is_default: false, has_config: true }
  ],
  current: 'default'
}

// ═══════════════════════════════════════════ Advisor 桩（形状照抄 dashboard/api_advisor.py）
const NOW = Date.now()
const iso = (msAgo) => new Date(NOW - msAgo).toISOString()

const advState = {
  items: [],
  settings: { enabled: true, concern_default: 'hold', min_interval_seconds: 0, model: '', provider: '' },
  mode: 'ok', // ok | slow | error503
  decisions: [],
  settingsPuts: [],
  detailCalls: []
}

function mkItem(i) {
  const severity = i % 7 === 3 ? 'blocker' : i % 3 === 0 ? 'nit' : 'concern'
  // 目标来源四档轮转：kanban 带卡号、user / first_request 不带 ref、i%4==3 干脆没有字段
  // （后端此时返回 goal_source='unknown'，面板必须显示「未知目标」而不是报错）
  const goal = [
    { goal_source: 'kanban', goal_ref: 't_6c6fffd3' },
    { goal_source: 'user', goal_ref: '' },
    { goal_source: 'first_request', goal_ref: '' },
    {}
  ][i % 4]
  return Object.assign({
    id: 'adv' + String(i).padStart(6, '0'),
    note:
      '第 ' + i + ' 条建议：这条 note 故意写得很长，用来验证列表里的摘要是截断的、详情里才是全文；' +
      '同时验证窄窗下它会折行而不是把整行撑出横向滚动条。' +
      '补充第二段：如果你在详情里读到这句话，说明详情给的是全文（列表只给前 ' + '96 字）；' +
      '这段文字的存在本身就是「摘要 vs 全文」的判据，请不要为了好看把它删掉。' +
      '补充第三段：长句还要能折行，overflow-wrap: anywhere 才能保证窄窗不溢出。',
    severity: severity,
    status: i % 5 === 0 ? 'delivered' : 'pending',
    profile: i % 2 ? 'code-worker' : 'default',
    // 台账文件来源：每 10 条故意与自报 profile 错开一次，验证「文件:xxx」提示会显示
    profile_source: i % 10 === 0 ? 'work' : (i % 2 ? 'code-worker' : 'default'),
    session_id: 'local-abcdef0123456789' + i,
    session_short: 'local-ab',
    turn_id: 'turn-' + i,
    first_raised_at: iso(3600 * 1000 * (i + 1)),
    last_seen_at: iso(600 * 1000 * (i + 1)),
    raise_count: 1 + (i % 3),
    deliver_count: i % 2,
    held_count: i % 2,
    reconfirmed_count: 0,
    event_count: 2,
    decision: null,
    reason: '',
    decided_at: null,
    actor: 'program',
    source: 'runtime'
  }, goal, {
    goal_known: Boolean(goal.goal_source),
    goal_source: goal.goal_source || 'unknown',
    goal_ref: goal.goal_ref || ''
  })
}
advState.items = Array.from({ length: 25 }, (_, i) => mkItem(i))

function summaryPayload() {
  return {
    ok: true,
    data: {
      total: 25,
      pending: 18,
      delivered: 5,
      accepted: 2,
      rejected: 1,
      dropped: 0,
      by_severity: { nit: 9, concern: 13, blocker: 3 },
      trend: Array.from({ length: 7 }, (_, i) => ({
        date: '2026-10-2' + (2 + (i % 8)),
        raised: i + 1,
        delivered: i % 2,
        decided: i % 3
      })),
      trend_days: 7,
      events: 62,
      ledger: 'D:/hermes-data/hermes/advisor/audit/advice.jsonl',
      ledger_exists: true
    },
    notes: []
  }
}

function coveragePayload() {
  // 形状照抄 dashboard/api_advisor.py::_coverage_row（每个 profile 一行 + 状态 + 诊断）
  const rows = [
    {
      profile: 'default', plugin_installed: true, plugin_version: '1.4.0',
      enabled_in_config: true, injection_allowed: true, audit_dir_exists: true,
      ledger_exists: true, ledger_events: 62, ledger_advice: 25,
      session_files: 28, held_notes: 16, settings_enabled: false,
      status: 'active', diagnosis: 'default：在记账（台账 62 条事件 / 25 条建议）且允许投递。'
    },
    {
      profile: 'work', plugin_installed: true, plugin_version: '1.4.0',
      enabled_in_config: true, injection_allowed: false, audit_dir_exists: false,
      ledger_exists: false, ledger_events: 0, ledger_advice: 0,
      session_files: 6, held_notes: 5, settings_enabled: true,
      status: 'reviewing_only',
      diagnosis:
        'work：插件已加载但台账为空：该 profile 的运行进程可能仍是旧代码，'
        + '需重启该 profile 的 gateway/serve 才会写入台账。证据：sessions 有 6 个文件'
    },
    {
      profile: 'xiaomo', plugin_installed: false, plugin_version: '',
      enabled_in_config: false, injection_allowed: false, audit_dir_exists: false,
      ledger_exists: false, ledger_events: 0, ledger_advice: 0,
      session_files: 0, held_notes: 0, settings_enabled: true,
      status: 'not_installed', diagnosis: 'xiaomo：没装 advisor 插件，所以它不可能产出评审。'
    }
  ]
  return {
    ok: true,
    data: {
      profiles: rows,
      status_counts: { active: 1, reviewing_only: 1, ledger_no_delivery: 0,
        installed_disabled: 0, not_installed: 1 },
      statuses: ['active', 'reviewing_only', 'installed_disabled', 'not_installed'],
      total: rows.length,
      known_agents: ['default', 'work', 'xiaomo'],
      read_only: true
    },
    notes: ['合计 3 个 profile：active 1 / reviewing_only 1 / installed_disabled 0 / not_installed 1']
  }
}

function detailPayload(id) {
  const item = advState.items.find((row) => row.id === id) || advState.items[0]
  return {
    ok: true,
    data: Object.assign({}, item, {
      events: [
        { event: 'raised', ts: item.first_raised_at, actor: 'program', source: 'runtime', severity: item.severity, decision: null, reason: '', profile: item.profile },
        { event: 'held', ts: item.first_raised_at, actor: 'program', source: 'runtime', severity: item.severity, decision: null, reason: '', profile: item.profile },
        { event: 'reconfirmed', ts: item.last_seen_at, actor: 'program', source: 'runtime', severity: item.severity, decision: null, reason: '', profile: item.profile },
        { event: 'delivered', ts: item.last_seen_at, actor: 'program', source: 'runtime', severity: item.severity, decision: null, reason: '', profile: item.profile }
      ]
    }),
    notes: []
  }
}

const restCalls = []
const store = {}

/**
 * 设置端点选哪个 profile 只看 `?agent=`（回归 t_43f17bdb）。
 * `?profile=` 是宿主 gateway 的「二级 profile 路由」选择器：带上它整条请求会被重绑到那个
 * profile 的 config scope，本插件的 API 宿主在那里不存在 → 宿主 404 Plugin not found。
 * 所以这里遇到 `?profile=` 就返回 'legacy-profile'，让断言直接红，而不是悄悄当默认 profile。
 */
function settingsScopeOf(path) {
  const agent = /[?&]agent=([^&]*)/.exec(path)
  if (agent) return decodeURIComponent(agent[1])
  return /[?&]profile=/.test(path) ? 'legacy-profile' : ''
}

function advisorFixture(path, opts) {
  const pathname = path.split('?')[0]
  const method = (opts && opts.method) || 'GET'
  if (advState.mode === 'slow') {
    // 慢响应（不是永不响应）：骨架屏看得见，inflight 也会正常结算，后面的用例不会被堵住
    return new Promise((resolve) => {
      setTimeout(() => resolve({ ok: true, data: { total: 0, items: [], limit: 20, offset: 0, count: 0 }, notes: [] }), 400)
    })
  }
  if (advState.mode === 'error503') {
    const body = {
      ok: false,
      error: {
        code: 'upstream_error',
        message: 'advisor 台账模块不可用，本分区暂时读不到建议（其它分区不受影响）：探针模拟 import 失败'
      }
    }
    const failure = new Error('503: ' + JSON.stringify(body))
    return Promise.reject(failure)
  }
  if (pathname === '/advisor/summary') return Promise.resolve(summaryPayload())
  if (pathname === '/advisor/coverage') return Promise.resolve(coveragePayload())
  if (pathname === '/advisor/advice') {
    const lm = /limit=(\d+)/.exec(path)
    const om = /offset=(\d+)/.exec(path)
    const limit = lm ? Number(lm[1]) : 20
    const offset = om ? Number(om[1]) : 0
    return Promise.resolve({
      ok: true,
      data: {
        status: null,
        severity: null,
        q: '',
        total: advState.items.length,
        limit: limit,
        offset: offset,
        count: advState.items.slice(offset, offset + limit).length,
        items: advState.items.slice(offset, offset + limit),
        ledger: 'D:/hermes-data/hermes/advisor/audit/advice.jsonl',
        agents: [],
        known_agents: ['default', 'code-worker', 'work']
      },
      notes: []
    })
  }
  if (/^\/advisor\/advice\/[^/]+$/.test(pathname)) {
    const id = pathname.split('/').pop()
    advState.detailCalls.push(id)
    return Promise.resolve(detailPayload(id))
  }
  if (pathname === '/advisor/decision' && method === 'POST') {
    const body = (opts && opts.body) || {}
    advState.decisions.push(body)
    const target = advState.items.find((row) => row.id === body.id)
    if (target) {
      target.decision = body.decision
      target.status = body.decision
      target.reason = body.reason || ''
      target.decided_at = new Date().toISOString()
    }
    return Promise.resolve({
      ok: true,
      data: { id: body.id, decision: body.decision, reason: body.reason || '', executed: false },
      notes: ['只记录了决定，没有执行建议内容']
    })
  }
  if (pathname === '/advisor/settings' && method === 'GET') {
    const profile = settingsScopeOf(path)
    const stored = profile === 'default' ? Object.assign({}, advState.settings) : {}
    const writableKeys = ['concern_default', 'enabled', 'min_interval_seconds', 'model', 'provider']
    const defaults = { enabled: true, concern_default: 'hold', min_interval_seconds: 0, model: '', provider: '' }
    const values = Object.assign({}, advState.settings)
    return Promise.resolve({
      ok: true,
      data: {
        profile: profile,
        settings: Object.assign({}, advState.settings, {
          schema_version: 1,
          blocker_forced_delivery: true,
          readonly_fields: ['blocker_forced_delivery']
        }),
        writable: writableKeys,
        readonly: ['blocker_forced_delivery'],
        forced: { blocker_forced_delivery: true },
        path: 'D:/hermes-data/hermes/profiles/' + profile + '/advisor/audit/settings.json',
        path_exists: profile !== 'default',
        source: profile === 'default' ? 'file' : 'defaults',
        stored: stored,
        defaults: defaults,
        profiles: ['default', 'work', 'code-worker'],
        values: values,
        // 逐项来源层（与后端 values_detail 同形）
        values_detail: writableKeys.map(function (key) {
          return {
            key: key,
            value: values[key],
            source: Object.prototype.hasOwnProperty.call(stored, key) ? 'settings_file' : 'default',
            on_disk: stored[key] === undefined ? null : stored[key],
            on_disk_present: Object.prototype.hasOwnProperty.call(stored, key),
            default: defaults[key],
            writable: true,
            readonly: false
          }
        }).concat([{
          key: 'blocker_forced_delivery', value: true, source: 'forced_constant',
          on_disk: null, on_disk_present: false, default: true, writable: false, readonly: true
        }]),
        layers: ['default', 'settings_file'],
        host_layers: {
          config_path: 'D:/hermes-data/hermes/config.yaml',
          config_error: '',
          plugins_enabled: { value: true, key: 'plugins.enabled', writable: false },
          allow_gateway_injection: { value: false, key: 'plugins.entries.advisor.allow_gateway_injection', writable: false },
          note: '宿主层只读'
        },
        enabled: advState.settings.enabled !== false,
        enabled_notice: advState.settings.enabled === false
          ? '当前 enabled=false：advisor 不会对任何 turn 做评审。'
          : '当前 enabled=true：每个 turn 结束后都会调用一次评审模型。',
        enabled_effect: '打开后：每个 turn 结束都会调用评审模型；投递取决于 allow_gateway_injection。'
      },
      notes: ['blocker_forced_delivery 是程序常量（单向阀门）']
    })
  }
  if (pathname === '/advisor/settings' && method === 'PUT') {
    const body = (opts && opts.body) || {}
    const profile = settingsScopeOf(path)
    advState.settingsPuts.push(Object.assign({ profile: profile }, body))
    if (body.min_interval_seconds === 999) {
      const failure = new Error(
        '400: ' + JSON.stringify({ ok: false, error: { code: 'invalid_argument', message: '设置未写入：探针模拟被后端拒绝' } })
      )
      return Promise.reject(failure)
    }
    advState.settings = Object.assign({}, advState.settings, body)
    return Promise.resolve({
      ok: true,
      data: {
        profile: profile,
        settings: Object.assign({}, advState.settings, { blocker_forced_delivery: true }),
        writable: ['concern_default', 'enabled', 'min_interval_seconds', 'model', 'provider'],
        readonly: ['blocker_forced_delivery'],
        forced: { blocker_forced_delivery: true },
        path: 'D:/hermes-data/hermes/profiles/' + profile + '/advisor/audit/settings.json',
        path_exists: true,
        source: 'file',
        stored: Object.assign({ schema_version: 1 }, advState.settings),
        defaults: { enabled: true, concern_default: 'hold', min_interval_seconds: 0, model: '', provider: '' },
        profiles: ['default', 'work', 'code-worker'],
        values: Object.assign({}, advState.settings),
        applied: body
      },
      notes: []
    })
  }
  return null
}

const baseCtx = {
  rest(path, opts) {
    restCalls.push({ path, method: (opts && opts.method) || 'GET' })
    if (path.indexOf('/advisor') === 0) {
      const hit = advisorFixture(path, opts)
      if (hit) return hit
    }
    if (path.indexOf('/agents') === 0) return Promise.resolve(AGENTS)
    return Promise.resolve({ ok: true, stub: true })
  },
  storage: {
    get(key, fallback) {
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : fallback
    },
    set(key, value) {
      store[key] = value
    },
    remove(key) {
      delete store[key]
    }
  },
  registerMany() {}
}

const contributions = []
plugin.register(
  Object.assign({}, baseCtx, {
    registerMany(list) {
      contributions.push(...list)
    }
  })
)
const page = contributions.find((c) => c.area === 'routes')

const container = document.getElementById('root')
const root = createRoot(container)
const flush = () => new Promise((r) => setTimeout(r, 60))
const Page = function Page() {
  return page.render()
}
await act(async () => {
  root.render(React.createElement(Page))
})
await act(flush)

const q = (sel) => container.querySelector(sel)
const qa = (sel) => Array.from(container.querySelectorAll(sel))
async function waitFor(pane, selector, rounds) {
  for (let i = 0; i < (rounds || 10); i += 1) {
    if (pane && pane.querySelector(selector)) return true
    await act(flush)
  }
  return Boolean(pane && pane.querySelector(selector))
}

console.log('\n[A] 导航入口 + 分区挂载')
const navBtn = qa('.hc-nav__item').find((b) => /Advisor/.test(b.textContent))
check('导航里有「Advisor」入口（真实 <button>）', Boolean(navBtn) && navBtn.tagName === 'BUTTON')
check('Advisor 是顶级分区（.hc-nav 的直接子项，不塞在别的分区里）',
  qa('.hc-nav > li').some((li) => li.textContent.indexOf('Advisor') !== -1)
    && qa('.hc-nav > li').length >= 10,
  qa('.hc-nav > li').map((li) => li.textContent.trim()).join(' | '))
await act(async () => {
  navBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
const pane = qa('.hc-pane').find((p) => p.querySelector('.hc-adv'))
check('分区面板真的挂载（.hc-adv 在 DOM 里）', Boolean(pane))
check('顶栏标题切到 Advisor', /Advisor/.test(q('.hc-topbar__title').textContent), q('.hc-topbar__title').textContent)
check('nav.section 记到 storage', store['nav.section'] === 'advisor', JSON.stringify(store))

console.log('\n[B] 顶部状态条：四个数字 + 口径 + 7 天趋势')
check('状态条数据到位（不是骨架屏）', await waitFor(pane, '.hc-adv__stat'))
const stats = Array.from(pane.querySelectorAll('.hc-adv__stat'))
check('四个数字卡：待处理 / 已采纳 / 已拒绝 / 总计', stats.length === 4,
  stats.map((el) => el.getAttribute('data-stat')).join(','))
check('数字来自 /summary（18 / 2 / 1 / 25）',
  stats.map((el) => el.querySelector('.hc-adv__stat-num').textContent).join('|') === '18|2|1|25',
  stats.map((el) => el.querySelector('.hc-adv__stat-num').textContent).join('|'))
check('每个数字都带口径（不是光秃秃四个数）',
  stats.every((el) => el.querySelector('.hc-adv__stat-caliber').textContent.length > 12),
  stats.map((el) => el.querySelector('.hc-adv__stat-caliber').textContent.slice(0, 10)).join(' / '))
check('severity 分布在「总计」卡的副标题里（NIT 9 / CONCERN 13 / BLOCKER 3）',
  /NIT 9 · CONCERN 13 · BLOCKER 3/.test(pane.textContent))
check('7 天趋势：7 根柱子 + 一行小字',
  pane.querySelectorAll('.hc-adv__trend-day').length === 7 &&
    /最近 7 天：新增 \d+ 条 · 决定 \d+ 条/.test(pane.textContent),
  String(pane.querySelectorAll('.hc-adv__trend-day').length))

console.log('\n[C] 建议列表')
const rows = () => Array.from(pane.querySelectorAll('.hc-adv__row'))
check('列表渲染 20 行（每页 20）', rows().length === 20, 'rows=' + rows().length)
check('行 = 时间 + severity 徽标 + 摘要 + 状态 + 来源（五个字段都在）',
  rows().every((r) =>
    r.querySelector('.hc-adv__cell--time') &&
    r.querySelector('.hc-adv__cell--sev .hc-adv__sev') &&
    r.querySelector('.hc-adv__cell--note') &&
    r.querySelector('.hc-adv__cell--status .hc-adv__pill') &&
    r.querySelector('.hc-adv__cell--source .hc-adv__profile')))
check('来源写明 profile + 会话短 id', /code-worker|default/.test(pane.textContent) && /local-ab/.test(pane.textContent))
check('列表行显示 profile_source（台账文件来源，与建议自报 profile 分开）',
  rows().some((r) => {
    const cell = r.querySelector('.hc-adv__cell--source')
    return cell && /文件:/.test(cell.textContent)
  }),
  rows()[0] ? rows()[0].querySelector('.hc-adv__cell--source').textContent.slice(0, 60) : '缺失')
const agentFilter = pane.querySelector('[data-filter="agent"]')
check('建议列表有 profile（agent）筛选器', Boolean(agentFilter) &&
  agentFilter.options.length > 1,
  agentFilter ? Array.from(agentFilter.options).map((o) => o.value).join(',') : '缺失')
await act(async () => {
  agentFilter.value = 'code-worker'
  agentFilter.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
await act(flush)
check('切 profile 筛选后请求带 agent=code-worker',
  restCalls.some((c) => c.path.indexOf('/advisor/advice?') === 0 && /agent=code-worker/.test(c.path)),
  restCalls.filter((c) => c.path.indexOf('/advisor/advice?') === 0).map((c) => c.path).slice(-2).join(' | '))
await act(async () => {
  agentFilter.value = ''
  agentFilter.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
await act(flush)
const sevClasses = new Set(Array.from(pane.querySelectorAll('.hc-adv__sev')).map((el) => el.className))
check('severity 三档用不同视觉强度（class 不同）',
  sevClasses.has('hc-adv__sev hc-adv__sev--nit') &&
    sevClasses.has('hc-adv__sev hc-adv__sev--concern') &&
    sevClasses.has('hc-adv__sev hc-adv__sev--blocker'),
  Array.from(sevClasses).join(' / '))
check('摘要被截断（列表里不是全文）',
  rows()[0].querySelector('.hc-adv__note-text').textContent.length < 120,
  String(rows()[0].querySelector('.hc-adv__note-text').textContent.length) + ' 字')
check('分页器出现（25 条 > 20/页）', Boolean(pane.querySelector('.hc-adv__pager')))
await act(async () => {
  Array.from(pane.querySelectorAll('.hc-adv__pager button')).find((b) => /下一页/.test(b.textContent))
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check('点下一页后请求带 offset=20',
  restCalls.some((c) => /^\/advisor\/advice\?.*offset=20/.test(c.path)),
  JSON.stringify(restCalls.filter((c) => c.path.indexOf('/advisor/advice') === 0).map((c) => c.path).slice(-3)))
check('第二页只剩 5 行', rows().length === 5, 'rows=' + rows().length)
await act(async () => {
  Array.from(pane.querySelectorAll('.hc-adv__pager button')).find((b) => /上一页/.test(b.textContent))
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)

console.log('\n[D] 详情：全文 / 计数 / 事件时间线')
const detailBtn = Array.from(rows()[0].querySelectorAll('button')).find((b) => /详情/.test(b.textContent))
check('行内有「详情」按钮（真实 <button>）', Boolean(detailBtn) && detailBtn.tagName === 'BUTTON')
const firstId = rows()[0].getAttribute('data-id')
await act(async () => {
  detailBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
const detailReady = await waitFor(pane, '.hc-adv__detail')
check('详情面板出现', detailReady && Boolean(pane.querySelector('.hc-adv__detail')))
check('详情真的去拉了 /advisor/advice/{id}', advState.detailCalls.indexOf(firstId) !== -1,
  advState.detailCalls.join(','))
const detail = pane.querySelector('.hc-adv__detail')
check('详情给全文（不截断）',
  detail.querySelector('.hc-adv__detail-note').textContent.length > 120,
  String(detail.querySelector('.hc-adv__detail-note').textContent.length) + ' 字')
check('详情给首次出现 / 出现次数 / 投递次数',
  /首次出现/.test(detail.textContent) && /出现 \d+ 次/.test(detail.textContent) && /投递 \d+ 次/.test(detail.textContent),
  detail.querySelector('.hc-adv__facts').textContent.slice(0, 80))
check('事件时间线 4 条（raised/held/reconfirmed/delivered）',
  Array.from(detail.querySelectorAll('.hc-adv__event')).map((el) => el.getAttribute('data-event')).join(',') ===
    'raised,held,reconfirmed,delivered',
  Array.from(detail.querySelectorAll('.hc-adv__event')).map((el) => el.getAttribute('data-event')).join(','))

console.log('\n[E] 拒绝必须填原因（前端先拦一次）')
const rejectBtn = () => Array.from(pane.querySelectorAll('.hc-adv__decision-head button')).find((b) => /^拒绝$/.test(b.textContent))
await act(async () => {
  rejectBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
check('点「拒绝」后出现原因输入框（textarea）', Boolean(pane.querySelector('textarea.hc-adv__textarea')))
const beforePosts = advState.decisions.length
const submitReject = () => Array.from(pane.querySelectorAll('.hc-adv__reason-actions button')).find((b) => /提交拒绝/.test(b.textContent))
await act(async () => {
  submitReject().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
check('空原因点提交 → 不发 POST', advState.decisions.length === beforePosts,
  'posts=' + (advState.decisions.length - beforePosts))
check('空原因给出行内校验文案（不是静默失败）',
  /拒绝必须填写原因/.test((pane.querySelector('.hc-adv__formerr') || {}).textContent || ''),
  (pane.querySelector('.hc-adv__formerr') || {}).textContent || '（没有 .hc-adv__formerr）')

console.log('\n[F] 拒绝（带原因）+ 采纳：只写决定')
const textarea = pane.querySelector('textarea.hc-adv__textarea')
const setValue = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
const setInputValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
await act(async () => {
  setValue.call(textarea, '这条与本机无关，暂不处理')
  textarea.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await act(flush)
await act(async () => {
  submitReject().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const rejectPost = advState.decisions[advState.decisions.length - 1] || {}
check('填了原因后真的 POST /advisor/decision',
  advState.decisions.length === beforePosts + 1 &&
    restCalls.some((c) => c.path.split('?')[0] === '/advisor/decision' && c.method === 'POST'),
  JSON.stringify(rejectPost))
check('POST 体里 decision=rejected 且 reason 原样带上',
  rejectPost.decision === 'rejected' && rejectPost.reason === '这条与本机无关，暂不处理' && rejectPost.id === firstId,
  JSON.stringify(rejectPost))
const decided = await waitFor(pane, '.hc-adv__decision[data-decided="rejected"]')
check('决定后详情显示「已决定 / 已拒绝 + 原因」',
  decided && /原因：这条与本机无关，暂不处理/.test(pane.textContent),
  decided ? pane.querySelector('.hc-adv__decision').textContent.slice(0, 120) : '未渲染')
check('决定后列表与统计被重新拉取',
  restCalls.filter((c) => c.path.split('?')[0] === '/advisor/summary').length >= 2,
  'summary calls=' + restCalls.filter((c) => c.path.split('?')[0] === '/advisor/summary').length)

const secondRow = rows().find((r) => r.getAttribute('data-id') !== firstId)
await act(async () => {
  Array.from(secondRow.querySelectorAll('button')).find((b) => /详情/.test(b.textContent))
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
const secondReady = await waitFor(pane, '.hc-adv__detail')
const acceptBtn = () => Array.from(pane.querySelectorAll('.hc-adv__decision-head button')).find((b) => /^采纳$/.test(b.textContent))
check('另一条详情里「采纳」按钮存在', secondReady && Boolean(acceptBtn()))
await act(async () => {
  acceptBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const acceptPost = advState.decisions[advState.decisions.length - 1] || {}
check('采纳 → POST decision=accepted（reason 可为空）',
  acceptPost.decision === 'accepted' && acceptPost.reason === '',
  JSON.stringify(acceptPost))
check('采纳后显示「已决定」', Boolean(pane.querySelector('.hc-adv__decision[data-decided="accepted"]')))

console.log('\n[G] 策略设置 + 只读阀门徽标')
// 版面收敛（t_22813006）：设置卡片的编辑对象 = 唯一的 Agent 选择器。上一段 [C] 把
// 选择器复位成「全部 agent」（只看列表、没有可编辑对象），所以这里先选一个具体 agent。
const agentPickerForSettings = pane.querySelector('[data-filter="agent"]')
if (agentPickerForSettings && agentPickerForSettings.value !== 'default') {
  await act(async () => {
    agentPickerForSettings.value = 'default'
    agentPickerForSettings.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  await act(flush)
  await act(flush)
}
const settings = pane.querySelector('.hc-adv__settings')
check('设置区在 DOM 里', Boolean(settings))
if (settings && !settings.querySelector('[data-settings-field="enabled"]')) {
  console.log('        [diag settings] ' + String(settings.className) + ' · '
    + String(settings.textContent || '').replace(/\s+/g, ' ').slice(0, 300))
  console.log('        [diag settings html] ' + String(settings.outerHTML || '').slice(0, 900))
}
check('五个设置项的可编辑控件都在（enabled / mode / concern_default / min_interval_seconds / model·provider）',
  Boolean(settings.querySelector('[data-settings-field="enabled"]')) &&
    Boolean(settings.querySelector('[data-settings-field="mode"]')) &&
    Boolean(settings.querySelector('[data-settings-field="concern_default"]')) &&
    settings.querySelectorAll('[data-settings-field="min_interval_seconds"]').length === 1 &&
    Boolean(settings.querySelector('[data-pmp]')),
  'fields=' + Array.from(settings.querySelectorAll('[data-settings-field]'))
    .map((el) => el.getAttribute('data-settings-field')).join(','))
// 版面收敛（t_22813006）：只读阀门从设置卡正文移进「帮助与只读事实」折叠区 ——
// 它仍然只出现一次、只读一次、没有任何可点控件，只是不再和可写的五项并列抢注意力。
const readonly = pane.querySelector('.hc-adv__help [data-readonly="blocker_forced_delivery"]')
check('blocker_forced_delivery 以只读徽标展示（在帮助折叠区里，只出现一次）', Boolean(readonly) &&
  pane.querySelectorAll('[data-readonly="blocker_forced_delivery"]').length === 1,
  readonly ? readonly.textContent.slice(0, 80) : '缺失')
check('徽标写明「程序强制，不可关闭」', Boolean(readonly) && /程序强制，不可关闭/.test(readonly.textContent))
check('徽标里没有任何可点控件（不是开关）', Boolean(readonly) &&
  readonly.querySelectorAll('input, button, select, textarea').length === 0)
check('分区里没有可操作的 blocker 开关（控件层面，不靠文案）',
  Array.from(pane.querySelectorAll('input, select, button, textarea')).every(
    (el) => !/blocker/i.test(
      (el.getAttribute('name') || '') + (el.getAttribute('data-field') || '') +
      (el.getAttribute('data-setting') || '') + (el.getAttribute('title') || '')
    )
  ))
const checkbox = settings.querySelector('input[type="checkbox"]')
await act(async () => {
  checkbox.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
const saveBtn = () => Array.from(settings.querySelectorAll('button')).find((b) => /保存设置/.test(b.textContent))
await act(async () => {
  saveBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const put = advState.settingsPuts[advState.settingsPuts.length - 1] || {}
check('保存走 PUT /advisor/settings', restCalls.some((c) => c.path.split('?')[0] === '/advisor/settings' && c.method === 'PUT'))
// 契约变更（t_56b4ee48）：只发改动过的键 —— 未改动的键零写请求。
check('PUT 体只含改动过的键（enabled 一个；未改动的四个键不进请求体）',
  put.enabled === false && Object.keys(put).filter((k) => k !== 'profile').length === 1 &&
    !('blocker_forced_delivery' in put),
  JSON.stringify(put))
check('保存成功给出「已保存（回读确认）」', /已保存/.test(settings.textContent))

// 后端拒绝时把原因原样显示
const numberInput = settings.querySelector('input[type="number"]')
await act(async () => {
  setInputValue.call(numberInput, '999')
  numberInput.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await act(flush)
await act(async () => {
  saveBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check('后端 400 时把原话显示在设置区（不静默）',
  /探针模拟被后端拒绝/.test(pane.textContent),
  (pane.querySelector('.hc-adv__formerr') || {}).textContent || '（没有 .hc-adv__formerr）')

console.log('\n[H] 三态：Skeleton → Empty → ErrorState')
const refreshBtn = () => Array.from(pane.querySelectorAll('.hc-adv__filters button')).find((b) => /刷新数据/.test(b.textContent))
const statusSelect = pane.querySelector('select[data-filter="status"]')
advState.mode = 'slow'
await act(async () => {
  // 换筛选 = 新的缓存 key（没有缓存 → 真的进 loading），此时才该看到骨架屏
  statusSelect.value = 'accepted'
  statusSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
check('换筛选时列表进骨架屏（.hc-skel）', pane.querySelectorAll('.hc-skel').length >= 1,
  'skeletons=' + pane.querySelectorAll('.hc-skel').length)
// 等慢响应落地，再回到「全部状态」（否则 inflight 还没结算，下一次刷新会被去重掉）
await act(async () => {
  await new Promise((r) => setTimeout(r, 500))
})
await act(flush)
advState.mode = 'ok'
await act(async () => {
  statusSelect.value = ''
  statusSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
await act(flush)
advState.mode = 'ok'
advState.items = []
await act(async () => {
  refreshBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const empty = pane.querySelector('[data-block="list"] .hc-state:not(.hc-state--error)')
check('无建议时是 Empty 状态块', Boolean(empty), empty ? empty.textContent.slice(0, 60) : '缺失')
check('Empty 标题是「还没有建议」', Boolean(empty) && /还没有建议/.test(empty.textContent))
advState.items = Array.from({ length: 25 }, (_, i) => mkItem(i))
advState.mode = 'error503'
await act(async () => {
  refreshBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const err = pane.querySelector('[data-block="list"] .hc-state--error')
check('后端 503 时渲染 ErrorState', Boolean(err))
check('ErrorState 带后端 error.code 与原因',
  Boolean(err) && /upstream_error/.test(err.textContent) && /探针模拟 import 失败/.test(err.textContent),
  err ? err.textContent.slice(0, 160) : '缺失')
advState.mode = 'ok'
await act(async () => {
  refreshBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check('恢复后回到有数据的列表', rows().length > 0, 'rows=' + rows().length)

console.log('\n[J] 覆盖面：状态条 + 矩阵 + 自动诊断')
const covBar = pane.querySelector('[data-coverage-bar]')
check('覆盖状态条在 DOM 里', Boolean(covBar), covBar ? covBar.getAttribute('data-coverage-bar') : '缺失')
const covCells = Array.from(pane.querySelectorAll('[data-coverage-count]'))
check('状态条五档各一个格子', covCells.length === 5,
  covCells.map((el) => el.getAttribute('data-coverage-count')).join(','))
check('每格都有数字（1/1/0/0/1）',
  covCells.map((el) => el.querySelector('.hc-adv__cov-num').textContent).join('|') === '1|1|0|0|1',
  covCells.map((el) => el.querySelector('.hc-adv__cov-num').textContent).join('|'))
check('每格都带口径说明（tooltip）', covCells.every((el) => String(el.getAttribute('title') || '').length > 10),
  covCells.map((el) => String(el.getAttribute('title') || '').slice(0, 14)).join(' | '))
const covAlert = pane.querySelector('[data-coverage-alert]')
check('reviewing_only 有整幅可操作提示', Boolean(covAlert) && /gateway\/serve/.test(covAlert.textContent),
  covAlert ? covAlert.textContent.slice(0, 80) : '缺失')

const covRows = Array.from(pane.querySelectorAll('[data-coverage-profile]'))
check('覆盖矩阵每个 agent 一行（3 行）', covRows.length === 3,
  covRows.map((el) => el.getAttribute('data-coverage-profile')).join(','))
check('矩阵行带状态属性（可用来配色）',
  covRows.every((el) => Boolean(el.getAttribute('data-coverage-status'))),
  covRows.map((el) => el.getAttribute('data-coverage-status')).join(','))
const reviewRow = covRows.find((el) => el.getAttribute('data-coverage-status') === 'reviewing_only')
// 收起态一行不再塞整句诊断（那正是「卡片过高」的成因）：可操作结论挂在卡片的
// data-cov-diagnosis 与状态点 tooltip 上，展开区「路径 / 版本 / 台账 / 诊断」里仍有全文。
check('reviewing_only 行仍带「重启 gateway/serve」这条可操作结论',
  Boolean(reviewRow) && /gateway\/serve/.test(String(reviewRow.getAttribute('data-cov-diagnosis') || '')),
  reviewRow ? String(reviewRow.getAttribute('data-cov-diagnosis')).slice(0, 90) : '缺失')
const notInstalledRow = covRows.find((el) => el.getAttribute('data-coverage-status') === 'not_installed')
check('not_installed 行说明「没装插件」（诊断原文 + 徽标「未安装」）',
  Boolean(notInstalledRow)
    && (/没装/.test(String(notInstalledRow.getAttribute('data-cov-diagnosis') || ''))
      || /未安装/.test(notInstalledRow.textContent)),
  notInstalledRow ? String(notInstalledRow.getAttribute('data-cov-diagnosis')).slice(0, 60) : '缺失')
check('矩阵里没有字面色值（状态靠类 + 边框样式区分）',
  Array.from(pane.querySelectorAll('[data-coverage-status]')).every(
    (el) => !/#[0-9a-fA-F]{3,8}\b|\brgba?\(/.test(el.getAttribute('style') || '')))
// 「评审关」这件事在收起态由 facts 行的「评审已关闭」承担（原来那张卡片的
// 「review off」英文标记已随卡片样式一起废弃）。
check('active 行写明「评审已关闭」（settings.json 里 enabled=false）',
  /评审已关闭/.test((covRows.find((el) => el.getAttribute('data-coverage-status') === 'active') || {}).textContent || ''),
  (covRows.find((el) => el.getAttribute('data-coverage-status') === 'active') || {}).textContent?.slice(-80) || '缺失')

console.log('\n[K] 目标来源：列表与详情都显示 goal_source（缺失时「未知目标」）')
// 版面收敛后「换 agent 会关掉详情」（详情属于旧 agent 的那条建议）：[G] 刚切过 agent，
// 所以这里先重新打开一条详情，才能断「详情里也有目标来源事实行」。
if (!pane.querySelector('[data-detail-id]')) {
  const detailBtn2 = Array.from(pane.querySelectorAll('.hc-adv__row .hc-adv__cell--ops button'))
    .find((b) => /详情/.test(b.textContent))
  if (detailBtn2) {
    await act(async () => {
      detailBtn2.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
    })
    await waitFor(pane, '[data-detail-id]')
    await act(flush)
  }
}
const goalChips = Array.from(pane.querySelectorAll('[data-goal-source]'))
check('列表/详情里出现目标来源标记', goalChips.length > 0, 'count=' + goalChips.length)
check('目标来源文字是四种之一（看板卡/用户目标/首个请求/未知目标）',
  goalChips.every((el) => /看板卡|用户目标|首个请求|未知目标/.test(el.textContent)),
  goalChips.map((el) => el.textContent.slice(0, 12)).join(' | '))
check('四档都真的渲染出来了（不只是 unknown）',
  ['kanban', 'user', 'first_request', 'unknown'].every(
    (name) => goalChips.some((el) => el.getAttribute('data-goal-source') === name)),
  Array.from(new Set(goalChips.map((el) => el.getAttribute('data-goal-source')))).join(','))
check('kanban 档带卡号 ref（goal_ref 显示）',
  goalChips.some((el) => el.getAttribute('data-goal-source') === 'kanban'
    && /t_6c6fffd3/.test(el.textContent)))
const detailFacts = pane.querySelector('.hc-adv__facts')
check('详情里有「目标来源」事实行（含 goal_ref）',
  Boolean(detailFacts) && /目标来源：/.test(detailFacts.textContent),
  detailFacts ? detailFacts.textContent.slice(-60) : '缺失')

console.log('\n[L] 设置按 profile：唯一 Agent 选择器 + 切换 + 逐项来源')
// 版面收敛（t_22813006）：设置区不再自带 ProfilePicker —— 分区顶部那一个 Agent
// 选择器同时决定矩阵高亮 / 列表范围 / 设置对象。这里断的就是「只有一个入口」。
const pickers = pane.querySelectorAll('[data-filter="settings-profile"]')
check('设置区不再有第二个 Agent 选择器（ProfilePicker 计数 = 0）', pickers.length === 0,
  'count=' + pickers.length)
const agentSelects = Array.from(pane.querySelectorAll('[data-filter="agent"]'))
check('分区里只有一个 Agent 选择器（唯一切换入口）', agentSelects.length === 1,
  'count=' + agentSelects.length + ' values=' + agentSelects.map((el) => el.value).join(','))
const picker = agentSelects[0]
check('选择器默认停在具体 agent（default）', picker && picker.value === 'default',
  picker ? picker.value : '缺失')
check('选择器列出所有 agent（≥3 个，含「全部 agent」）', picker && picker.options.length >= 4,
  picker ? Array.from(picker.options).map((o) => o.value).join(',') : '缺失')
// 契约变更（t_56b4ee48 + t_22813006）：逐项来源层表降级成「技术详情」折叠区，
// 仍然逐项列出（生效值 / 来源层 / 落盘值 / 默认值），只是默认收起。
const applied = pane.querySelector('[data-settings-tech] .hc-adv__stable')
const appliedRows = applied ? Array.from(applied.querySelectorAll('[data-settings-key]')) : []
check('技术详情里仍有逐项设置表（生效值 + 来源层 + 落盘值 + 默认值）', Boolean(applied) &&
  appliedRows.length === 6, applied ? String(appliedRows.length) + ' 行' : '缺失')
check('那张表默认收起（<details> 无 open）', Boolean(pane.querySelector('[data-settings-tech]')) &&
  !pane.querySelector('[data-settings-tech]').hasAttribute('open'))
check('技术详情里没有任何可编辑控件（纯文本对账）',
  Boolean(pane.querySelector('[data-settings-tech]')) &&
  pane.querySelector('[data-settings-tech]').querySelectorAll('input, select, textarea').length === 0)
check('每行都标了来源层，且带「落盘」对照', Boolean(applied) &&
  appliedRows.every((r) => Boolean(r.getAttribute('data-settings-source'))) &&
  /落盘值/.test(applied.textContent),
  applied ? applied.textContent.replace(/\s+/g, ' ').slice(0, 80) : '缺失')
await act(async () => {
  picker.value = 'work'
  picker.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
await act(flush)
const settingsBox = pane.querySelector('.hc-adv__settings')
check('切到 work 后重新按 profile 取设置',
  restCalls.some((c) => c.path.indexOf('/advisor/settings') === 0 && /agent=work/.test(c.path)),
  restCalls.filter((c) => c.path.indexOf('/advisor/settings') === 0).map((c) => c.path).slice(-3).join(' | '))
// 回归（t_43f17bdb）：设置端点**绝不能**再带 ?profile= —— 宿主会把它当二级 profile 路由指令，
// 整条请求被重绑到该 profile 的 config scope，本插件的 API 宿主在那里不存在 → 404 Plugin not found。
check('设置请求一律带 ?agent=、不带 ?profile=',
  restCalls.filter((c) => c.path.indexOf('/advisor/settings') === 0).length > 0 &&
  restCalls.filter((c) => c.path.indexOf('/advisor/settings') === 0).every((c) => !/[?&]profile=/.test(c.path)),
  restCalls.filter((c) => c.path.indexOf('/advisor/settings') === 0).map((c) => c.path).slice(-3).join(' | '))
check('切换后设置卡片的 data-settings-profile 跟着切', settingsBox &&
  settingsBox.getAttribute('data-settings-profile') === 'work',
  settingsBox ? settingsBox.getAttribute('data-settings-profile') : '缺失')
check('切换后技术详情里写明当前读写的文件与 agent', (() => {
  const tech = pane.querySelector('[data-settings-tech]')
  return Boolean(tech) && /work/.test(tech.textContent) && /settings\.json/.test(tech.textContent)
})(), (pane.querySelector('[data-settings-tech]') || {}).textContent?.slice(0, 120) || '缺失')
const saveBtn2 = () => Array.from(pane.querySelectorAll('.hc-adv__settings button')).find((b) => /保存设置/.test(b.textContent))
// 契约变更（t_56b4ee48）：没改动就不发请求，所以要先改一个值再保存。
const intervalInput2 = settingsBox.querySelector('input[type="number"]')
await act(async () => {
  setInputValue.call(intervalInput2, '77')
  intervalInput2.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await act(flush)
await act(async () => {
  saveBtn2().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const lastPut = advState.settingsPuts[advState.settingsPuts.length - 1] || {}
check('PUT 带上 agent=work（只改这一个 agent）', lastPut.profile === 'work', JSON.stringify(lastPut))
check('PUT 体里仍然没有 blocker_forced_delivery', !('blocker_forced_delivery' in lastPut), JSON.stringify(lastPut))

console.log('\n[I] 窄窗：折叠成卡片 + 无横向滚动条')
const styleText = document.querySelector('style[data-hermes-console-style]').textContent
// 一个分区一个 <900px 容器查询块，**块的数量会随分区增加而增长**（V3 起有 7 个），
// 所以不能"取最后一个块"（追加新分区就会取错）。改成：把所有 <900px 块切开，
// 取**含 `.hc-adv` 的那一块**——断言的是"Advisor 的窄窗规则真的写在 <900px 容器查询里"。
const narrowBlocks = styleText.split('@container hc (max-width: 899px)').slice(1)
const narrowCss = narrowBlocks.filter((block) => block.indexOf('.hc-adv') !== -1).join('\n')
check('样式里有 <900px 的容器查询块', narrowBlocks.length > 0,
  'blocks=' + narrowBlocks.length)
check('Advisor 的窄窗规则在 <900px 容器查询块里（不是写在全局）', narrowCss.length > 0,
  'advBlocks=' + narrowBlocks.filter((block) => block.indexOf('.hc-adv') !== -1).length)
check('窄窗把列表行折叠成卡片（.hc-adv__row 在块内被改成 flex-wrap）',
  /\.hc-adv__row \{[\s\S]*?flex-wrap: wrap/.test(narrowCss), narrowCss.slice(0, 100))
check('窄窗收起表头（.hc-adv__head display:none）', /\.hc-adv__head \{\s*display: none/.test(narrowCss))
check('窄窗里摘要独占一行并允许折行',
  /\.hc-adv__cell--note \{[\s\S]*?flex: 1 1 100%/.test(narrowCss) &&
    /\.hc-adv__note-text \{[\s\S]*?overflow-wrap: anywhere/.test(narrowCss))
const advCss = styleText.slice(styleText.indexOf('.hc-adv {'))
check('advisor 样式里没有任何 overflow-x: auto/scroll（不产生横向滚动条）',
  !/\.hc-adv[^{]*\{[^}]*overflow-x:\s*(auto|scroll)/.test(advCss))
check('列表列定义全部用 minmax(0, …)（定宽列会撑破窄窗）',
  /--adv-cols:[^;]*minmax\(0, 1fr\)/.test(advCss) && !/--adv-cols:[^;]*\d+px 1fr/.test(advCss))
check('行内文字允许任意处折行（overflow-wrap: anywhere）',
  /\.hc-adv__detail-note \{[\s\S]*?overflow-wrap: anywhere/.test(advCss))
const cs = (el) => window.getComputedStyle(el)
check('列表行在宽容器里是 grid（表头/数据行共享列定义）',
  cs(rows()[0]).display === 'grid', cs(rows()[0]).display)
check('列表容器没有设置 overflow-x 滚动',
  !['auto', 'scroll'].includes(cs(pane.querySelector('.hc-adv__table')).overflowX),
  cs(pane.querySelector('.hc-adv__table')).overflowX)

let unmountOk = true
try {
  await act(async () => {
    root.unmount()
  })
} catch (e) {
  unmountOk = false
}
check('卸载不抛（effect 清理正确）', unmountOk && container.innerHTML === '')

try {
  if (process.platform === 'win32') rmdirSync(join(WORK, 'node_modules'))
  else unlinkSync(join(WORK, 'node_modules'))
  rmSync(WORK, { recursive: true, force: true })
} catch (e) {
  console.log('  (清理临时目录失败，手动删：' + WORK + ')')
}

console.log('\n=== 汇总 ===')
console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
if (failures.length) {
  failures.forEach((f) => console.log('  ! ' + f))
  process.exitCode = 1
} else {
  console.log('ADVISOR-DOM-PROBE-OK')
}
