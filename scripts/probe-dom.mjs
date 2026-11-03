/**
 * hermes-console · 真 DOM + 真 React 验收（比 probe-shell.mjs 更硬的一层）
 *
 * 与 probe-shell.mjs 的分工：
 *   - `probe-shell.mjs` 用迷你 hook 运行时「手算」组件树，覆盖三态/缓存/退化的分支逻辑；
 *   - 本文件用**真的 React 19 + react-dom/client 挂进 jsdom**，走真实调度、真实 effect、
 *     真实 DOM 事件，专门验证只有真 DOM 才能证明的事：
 *       1. 切分区时面板 DOM 节点被复用（不是卸载重建）→ 滚动位置保得住；
 *       2. 真 `change` 事件驱动的重渲染、真 effect 依赖比较（切回不重拉）；
 *       3. 卸载不抛（effect 清理正确）。
 *
 * 模块来源：宿主 `hermes-agent/node_modules`（react / react-dom / jsdom 都在里面），
 * 用一个临时目录里的 junction 接过去 —— 不复制、不安装、不改宿主任何文件。
 *
 * 跑法：node plugins/hermes-console/scripts/probe-dom.mjs
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
const WORK = mkdtempSync(join(tmpdir(), 'hc-dom-'))
mkdirSync(WORK, { recursive: true })
try {
  symlinkSync(HOST_NODE_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_NODE_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { JSDOM } = requireFromWork('jsdom')

// ---------------------------------------------------------------- jsdom
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

// ------------------------------------------------- 真 React / react-dom
const React = requireFromWork('react')
const { createRoot } = requireFromWork('react-dom/client')
const { act } = React

// 独立模块实例 = 独立缓存：拷一份到临时目录再 import
const entryCopy = join(WORK, 'entry_dom.js')
copyFileSync(ENTRY, entryCopy)
// 命名空间导入：既要 default（门牌 plugin），也要具名导出（injectConsoleStyles / STYLE_ATTR）。
// 注入函数必须能被二次调用 —— 「连调两次仍只有一个 <style>」这条幂等断言只能这样验。
const pluginApi = await import(pathToFileURL(entryCopy).href)
const plugin = pluginApi.default

const AGENTS = {
  agents: [
    { id: 'default', label: '小白', is_default: true, has_config: true },
    { id: 'code-worker', label: 'code-worker', is_default: false, has_config: true },
    { id: 'work', label: '牛马', is_default: false, has_config: true },
    { id: 'worker', label: '力工', is_default: false, has_config: true },
    { id: 'xiaomo', label: '墨黎', is_default: false, has_config: true },
    { id: 'cad-worker', label: 'cad-worker', is_default: false, has_config: true },
    { id: 'media-worker', label: 'media-worker', is_default: false, has_config: true },
    { id: 'apm-worker', label: 'apm-worker', is_default: false, has_config: true },
    { id: 'research-worker', label: 'research-worker', is_default: false, has_config: true }
  ],
  current: 'code-worker'
}

const restCalls = []
const store = {}

// C3：概览分区真的读 5 个路由。桩数据按后端真实响应形状给（scope/calibers/counts）。
function overviewFixture(pathname, path) {
  if (pathname === '/overview/system') {
    return {
      ok: true,
      scope: { kind: 'host', agent: 'code-worker', note: '宿主级' },
      calibers: { 'memory.percent': 'psutil.virtual_memory().percent' },
      data: {
        os: 'Windows',
        cpu_count: 16,
        cpu_percent: 12.5,
        uptime_seconds: 300000,
        memory: { total: 34359738368, used: 17179869184, percent: 50 },
        disk: { percent: 61 }
      }
    }
  }
  if (pathname === '/overview/analytics') {
    return {
      ok: true,
      scope: { kind: 'profile', agent: 'code-worker' },
      calibers: {},
      data: {
        period_days: 30,
        totals: { total_input: 1000000, total_output: 200000, total_cache_read: 50000000, total_api_calls: 1234 }
      }
    }
  }
  if (pathname === '/overview/cron') {
    return {
      ok: true,
      scope: { kind: 'profile', agent: 'code-worker' },
      calibers: {},
      counts: { total: 3, enabled: 2, paused: 1 },
      data: [
        { id: 'j1', name: '每小时巡检', enabled: true, next_run_at: '2026-10-28T14:00:00+08:00', profile: 'code-worker' },
        { id: 'j2', name: '每日备份', enabled: true, next_run_at: '2026-10-29T03:00:00+08:00', profile: 'code-worker' },
        { id: 'j3', name: 'oai.sb 额度监控', enabled: true, paused_at: 1793160000, next_run_at: '2026-10-28T15:00:00+08:00', profile: 'default' },
        { id: 'j4', name: '抓取失败的任务', enabled: true, next_run_at: null, profile: 'work', last_error: 'HTTP 500 from upstream' }
      ]
    }
  }
  if (pathname === '/overview/layout') {
    return {
      ok: true,
      scope: { kind: 'shared_file', agent: 'code-worker' },
      data: {
        path: 'D:/hermes-data/hermes/plugins/home-dashboard/layout.json',
        exists: true,
        mtime: 1789755473,
        layout: {
          version: 1,
          widgets: [
            { id: 'clock', gx: 0, gy: 0, gw: 6, gh: 2 },
            { id: 'clock', gx: 6, gy: 0, gw: 3, gh: 2 },
            { id: 'clock', gx: 9, gy: 0, gw: 3, gh: 2 },
            { id: 'gateway', gx: 0, gy: 2, gw: 4, gh: 2 },
            { id: 'gateway', gx: 4, gy: 2, gw: 4, gh: 2 },
            { id: 'tokens', gx: 8, gy: 2, gw: 4, gh: 2 }
          ]
        }
      }
    }
  }
  if (pathname === '/overview/sessions') {
    // 会话 id 必须带 agent：项目分组页「全部 agent」会把每个 profile 的会话合并到一张表，
    // 桩里 9 个 agent 都返回同一批 sess-0..19 就会撞 key（React 会报 duplicate key 警告）。
    const agentMatch = /[?&]agent=([^&]*)/.exec(path || '')
    const agent = agentMatch ? decodeURIComponent(agentMatch[1]) : 'code-worker'
    const rows = []
    for (let i = 0; i < 20; i += 1) {
      rows.push({
        id: 'sess-' + agent + '-' + i,
        title: '会话 ' + i,
        message_count: 10 + i,
        last_active: Math.floor(Date.now() / 1000) - 300 * (i + 1),
        profile: agent
      })
    }
    return {
      ok: true,
      scope: { kind: 'profile', agent: 'code-worker' },
      calibers: {},
      data: { sessions: rows, total: 42, limit: 20, offset: 0, storage: {} }
    }
  }
  return null
}

// 天气桩（形状照抄 dashboard/api_weather.py；逐小时第 1 条 pop=null 验证「—」）
function weatherFixture(pathname) {
  const units = { temp: '℃', humidity: '%', wind: 'km/h', pm25: 'μg/m³', pop: '%', aqi: '' }
  const sources = [
    { source: 'caiyun', label: '彩云天气', calls: 3, ok: true },
    { source: 'qweather', label: '和风天气', calls: 3, ok: false }
  ]
  if (pathname === '/weather/now') {
    return {
      ok: true,
      place: '家',
      place_resolved: { name: '家', resolved: true, lat: 31.721276, lng: 117.149643 },
      scope: { kind: 'global', agent: 'code-worker' },
      source: 'caiyun',
      source_label: '彩云天气',
      fetched_at: '2026-10-28T13:54:13+08:00',
      fetched_at_ms: 1793166853876,
      sources,
      units,
      realtime: { source: '彩云', name: '家', temp: 18.83, feels: 16.7, humidity: 46, wind_kmh: 44, desc: '☁️阴', aqi_chn: 39 },
      today: { date: '2026-10-28', text: '阵雨', min: 12.4, max: 19.1 },
      tomorrow: { date: '2026-10-29', text: '多云', min: 13, max: 20.5 },
      minutely: '未来两小时无降水',
      warnings: [],
      air: { pm25: 14, pm25_unit: 'μg/m³', aqi_chn: 39, aqi_level: '优' },
      partial_errors: []
    }
  }
  if (pathname === '/weather/hourly') {
    const hourly = []
    for (let i = 0; i < 24; i += 1) {
      const hh = String((13 + i) % 24).padStart(2, '0')
      hourly.push({ dt: '2026-10-28T' + hh + ':00+08:00', temp: 18.8 - i * 0.3, desc: '☁️阴', pop: i === 0 ? null : 10 + i })
    }
    return {
      ok: true,
      place: '家',
      scope: { kind: 'global', agent: 'code-worker' },
      source: 'caiyun',
      source_label: '彩云天气',
      fetched_at: '2026-10-28T13:54:16+08:00',
      fetched_at_ms: 1793166856426,
      sources,
      units,
      hours: 24,
      hourly
    }
  }
  if (pathname === '/weather/daily') {
    const daily = []
    for (let i = 0; i < 7; i += 1) {
      const row = { date: '2026-10-' + String(28 + i).padStart(2, '0'), text: '多云', min: 12.4 + i * 0.2, max: 19.1 + i * 0.3 }
      if (i === 0) row.pop = 60
      daily.push(row)
    }
    return {
      ok: true,
      place: '家',
      scope: { kind: 'global', agent: 'code-worker' },
      source: 'open-meteo',
      source_label: 'Open-Meteo',
      fetched_at: '2026-10-28T13:54:15+08:00',
      fetched_at_ms: 1793166855242,
      sources,
      units,
      days: 7,
      daily
    }
  }
  if (pathname === '/weather/at') {
    return {
      ok: true,
      place: '家',
      scope: { kind: 'global', agent: 'code-worker' },
      source: 'caiyun',
      source_label: '彩云天气',
      fetched_at: '2026-10-28T13:54:16+08:00',
      fetched_at_ms: 1793166856570,
      sources,
      units,
      hours: 6,
      text: '📍家 6小时后(19时): 15°C 多云'
    }
  }
  return null
}

// C7：Agent 总览。9 行真实形状的桩（xiaomo 故意做成 available:false，验失败行）
function agentsOverviewFixture() {
  const now = Math.floor(Date.now() / 1000)
  const mk = (id, label, extra) =>
    Object.assign(
      {
        id,
        label,
        is_default: id === 'default',
        available: true,
        unavailable_reason: null,
        gateway: { running: true, pid: 86104, port: null, mode: 'multiplexed', state: 'running', note: '由 default 的多路复用网关承载' },
        sessions_total: 10,
        sessions_active: 0,
        last_active_at: now - 3600,
        cron_total: 0,
        cron_enabled: 0,
        cron_paused: 0,
        model: 'deepseek-v4.1-flash',
        plugins_enabled_count: 3,
        data_at: now,
        elapsed_ms: 12,
        field_errors: {}
      },
      extra || {}
    )
  return {
    ok: true,
    generated_at: now,
    elapsed_ms: 1800,
    agent_count: 9,
    home: 'D:/hermes-data/hermes',
    gateway: { mode: 'multiplex', source: 'stub', error: null },
    calibers: {
      sessions_total: '该 profile 的 state.db 里会话总数（exclude_children）',
      sessions_active: '最近 300 秒内有活动且未结束的会话数',
      last_active_at: '最近一次会话活动时间戳',
      cron_total: 'cron/jobs.json 全部任务数',
      cron_enabled: 'enabled 且未暂停',
      cron_paused: 'paused_at 非空',
      model: 'config.yaml 的 model.default',
      plugins_enabled_count: 'plugins.enabled 列表长度',
      data_at: '本行取数时刻'
    },
    agents: [
      mk('default', '小白', { sessions_total: 154, cron_total: 12, cron_enabled: 11, last_active_at: now - 120 }),
      mk('apm-worker', 'apm-worker', { sessions_total: 23, plugins_enabled_count: 2, last_active_at: now - 86400 }),
      mk('cad-worker', 'cad-worker', { sessions_total: 2, plugins_enabled_count: 4, last_active_at: now - 5 * 86400 }),
      mk('code-worker', 'code-worker', { sessions_total: 49, sessions_active: 2, last_active_at: now - 30 }),
      mk('media-worker', 'media-worker', { sessions_total: 12, plugins_enabled_count: 4, last_active_at: now - 3 * 86400 }),
      mk('research-worker', 'research-worker', { sessions_total: 9, last_active_at: now - 4 * 86400 }),
      mk('work', '牛马', { sessions_total: 14, model: '长庚', plugins_enabled_count: 19, last_active_at: now - 600 }),
      mk('worker', '力工', { sessions_total: 510, plugins_enabled_count: 17, last_active_at: now - 2 * 86400 }),
      mk('xiaomo', '墨黎', {
        available: false,
        unavailable_reason: '数据文件不可读（权限/占用）：模拟 state.db 被占用',
        sessions_total: null,
        sessions_active: null,
        last_active_at: null,
        cron_total: 11,
        cron_enabled: 11,
        field_errors: { sessions_total: '数据文件不可读（权限/占用）' }
      })
    ]
  }
}

function agentSessionsFixture(limit) {
  const now = Math.floor(Date.now() / 1000)
  const rows = []
  for (let i = 0; i < limit; i += 1) {
    rows.push({
      id: 'sess-' + i,
      title: '会话 ' + i,
      message_count: 10 + i,
      last_active: now - 600 * (i + 1),
      ended_at: null,
      is_active: false
    })
  }
  return { ok: true, agent: 'default', data: { sessions: rows, total: 42, limit, offset: 0, storage: {} }, calibers: {} }
}

function agentCronFixture(limit) {
  const rows = []
  for (let i = 0; i < limit; i += 1) {
    rows.push({
      id: 'job-' + i,
      name: '任务 ' + i,
      enabled: true,
      paused_at: i === 1 ? 1793160000 : null,
      next_run_at: Math.floor(Date.now() / 1000) + 3600 * (i + 1)
    })
  }
  return { ok: true, agent: 'default', data: { jobs: rows, total: 12, limit, offset: 0, counts: { total: 12, enabled: 11, paused: 1 } }, calibers: {} }
}

// C8b：项目分组。桩数据按 dashboard/api_autogroup.py 的真实响应形状给
// （status 的 data.suggested / batches_total / rules_total；suggestions 的 data.items[].basis）。
// scan 是异步 job：POST 立刻回 202+job_id，GET /autogroup/job/{id} 第一枪 running、第二枪 done。
const agState = { suggestions: [], scanMode: 'ok', polls: 0, jobId: 'job-probe0001' }

function agSuggestion(i) {
  const rule = i % 5 !== 4
  return {
    session_id: 'ag-sess-' + i,
    title: (rule ? '[规则] ' : '[模型] ') + '会话 ' + i,
    profile: i % 2 ? 'default' : 'code-worker',
    message_count: 10 + i,
    last_activity_at: Math.floor(Date.now() / 1000) - 3600 * (i + 1),
    project_slug: 'kanban-cron',
    project_name: '看板与定时调度',
    project_path: 'D:/hermes-data/projects/kanban-cron',
    project_exists: true,
    confidence: rule ? 0.73 : 0.81,
    source: rule ? 'rule' : 'llm',
    basis: rule
      ? {
          kind: 'rule',
          keywords: ['看板', 'cron', '调度'],
          confidence: 0.73,
          reason: '规则命中：看板、cron、调度',
          label: '规则命中：看板、cron、调度'
        }
      : {
          kind: 'llm',
          keywords: [],
          confidence: 0.81,
          reason: '首条用户消息在讲定时任务编排',
          label: '模型判断（置信度 0.81）'
        },
    status: 'suggested',
    batch_id: null,
    updated_at: Math.floor(Date.now() / 1000) - 600 * i,
    applied_at: null
  }
}

agState.suggestions = Array.from({ length: 25 }, (_, i) => agSuggestion(i))

function autogroupFixture(pathname, path) {
  if (pathname === '/autogroup/status') {
    return {
      ok: true,
      data: {
        store: 'D:/hermes-data/hermes/auto-group/assignments.db',
        store_exists: true,
        suggested: agState.suggestions.length,
        applied: 3,
        rejected: 1,
        by_project: {},
        last_batch: null,
        batches: { total: 2, undone: 0, active: 2 },
        batches_total: 2,
        rules_projects: 17,
        rules_total: 98,
        projects_total: 17
      },
      notes: [],
      home: 'D:/hermes-data/hermes'
    }
  }
  if (pathname === '/autogroup/entities') {
    // 项目实体（ProjectRail 的输入）：slug/name/path/status/dir/assignments 五个字段都要有。
    return {
      ok: true,
      data: {
        root: 'D:/hermes-data/projects',
        projects: [
          {
            slug: 'kanban-cron',
            name: '看板与定时调度',
            path: 'D:/hermes-data/projects/kanban-cron',
            status: 'active',
            dir: { exists: true, empty: false },
            assignments: { applied: 2, suggested: 23, total: 25 }
          },
          {
            slug: 'console-ui',
            name: '控制台界面',
            path: 'D:/hermes-data/projects/console-ui',
            status: 'active',
            dir: { exists: true, empty: false },
            assignments: { applied: 0, suggested: 0, total: 0 }
          },
          {
            slug: 'legacy-misc',
            name: '杂项归档',
            path: 'D:/hermes-data/projects/legacy-misc',
            status: 'active',
            dir: { exists: false, empty: false },
            assignments: { applied: 0, suggested: 0, total: 0 }
          }
        ]
      },
      notes: [],
      home: 'D:/hermes-data/hermes'
    }
  }
  if (pathname === '/autogroup/suggestions') {
    const lm = /limit=(\d+)/.exec(path)
    const om = /offset=(\d+)/.exec(path)
    const sm = /status=([a-z]+)/.exec(path)
    const status = sm ? sm[1] : 'suggested'
    const limit = lm ? Number(lm[1]) : 20
    const offset = om ? Number(om[1]) : 0
    // status=applied 是「归属现状」：项目分组页用它算「已分组」，必须只回 applied 行，
    // 而且 session_id 要落在会话区真有的那些 id 上（否则已分组数永远是 0）。
    if (status === 'applied') {
      const applied = [
        {
          session_id: 'sess-code-worker-0',
          title: '会话 0',
          profile: 'code-worker',
          project_slug: 'kanban-cron',
          project_name: '看板与定时调度',
          status: 'applied',
          batch_id: 'batch-probe0001',
          applied_at: Math.floor(Date.now() / 1000) - 600
        },
        {
          session_id: 'sess-code-worker-1',
          title: '会话 1',
          profile: 'code-worker',
          project_slug: 'kanban-cron',
          project_name: '看板与定时调度',
          status: 'applied',
          batch_id: 'batch-probe0001',
          applied_at: Math.floor(Date.now() / 1000) - 1200
        }
      ]
      return {
        ok: true,
        data: { status: 'applied', total: applied.length, limit, offset, items: applied.slice(offset, offset + limit) },
        notes: []
      }
    }
    return {
      ok: true,
      data: {
        status: 'suggested',
        total: agState.suggestions.length,
        limit,
        offset,
        items: agState.suggestions.slice(offset, offset + limit)
      },
      notes: agState.suggestions.length ? [] : ['归属表里没有 status=suggested 的建议——先点「扫描并生成建议」']
    }
  }
  if (pathname === '/autogroup/scan') {
    agState.polls = 0
    return {
      ok: true,
      job_id: agState.jobId,
      data: { job_id: agState.jobId, job: { id: agState.jobId, state: 'running' }, poll: '/autogroup/job/' + agState.jobId },
      notes: ['扫描在后台进行']
    }
  }
  if (/^\/autogroup\/job\//.test(pathname)) {
    agState.polls += 1
    const id = pathname.split('/').pop()
    if (agState.scanMode === 'fail') {
      const error = { code: 'scan_failed', message: 'RuntimeError：探针模拟：state.db 读失败' }
      return {
        ok: true,
        state: 'failed',
        progress: { done: 0, total: 0 },
        error,
        data: { id, state: 'failed', progress: { done: 0, total: 0 }, result: null, error },
        notes: []
      }
    }
    if (agState.polls < 2) {
      return {
        ok: true,
        state: 'running',
        progress: { done: 12, total: 40 },
        error: null,
        data: { id, state: 'running', progress: { done: 12, total: 40 }, result: null, error: null },
        notes: []
      }
    }
    const result = {
      scanned: 40,
      matched: 36,
      unmatched: 4,
      saved: 36,
      suggested_total: 36,
      applied: 3,
      rejected: 1,
      projects_available: ['kanban-cron'],
      rule_keywords: 98,
      llm: { used: false, code: 'llm_unavailable', message: 'dashboard 进程里没有宿主的 agent ctx' }
    }
    return {
      ok: true,
      state: 'done',
      progress: { done: 40, total: 40 },
      error: null,
      data: { id, state: 'done', progress: { done: 40, total: 40 }, result, error: null },
      notes: []
    }
  }
  return null
}

const baseCtx = {
  rest(path, opts) {
    restCalls.push({ path, method: (opts && opts.method) || 'GET' })
    const pathname = path.split('?')[0]
    // M4a：导航从 /modules 取。原生 DOM 里也走真实路径（六个模块全启用，按 nav_order 排），
    // 这样 probe-dom 验的是「动态导航」而不是降级清单。
    if (pathname === '/modules') {
      return Promise.resolve({
        ok: true,
        data: {
          agent: (/agent=([^&]*)/.exec(path) || [])[1] || 'code-worker',
          modules: [
            { id: 'overview', label: '概览', nav_order: 10, section: 'overview', enabled: true, reason: '' },
            { id: 'weather', label: '天气', nav_order: 20, section: 'weather', enabled: true, reason: '' },
            { id: 'agents', label: 'Agent 总览', nav_order: 30, section: 'agents', enabled: true, reason: '' },
            { id: 'autogroup', label: '项目分组', nav_order: 40, section: 'autogroup', enabled: true, reason: '' },
            { id: 'advisor', label: 'Advisor', nav_order: 50, section: 'advisor', enabled: true, reason: '' },
            { id: 'upgrade', label: '升级', nav_order: 60, section: 'upgrade', enabled: true, reason: '' }
          ],
          health: { loaded: [], failed: {} }
        }
      })
    }
    // C8b：项目分组（在 /agents 通用桩之前判定，避免被前缀吃掉）
    if (pathname.indexOf('/autogroup') === 0) {
      const ag = autogroupFixture(pathname, path)
      if (ag) return Promise.resolve(ag)
    }
    // C7 的三条路由必须先于 /agents 的通用桩匹配
    if (pathname === '/agents/overview') return Promise.resolve(agentsOverviewFixture())
    if (/^\/agents\/[^/]+\/sessions$/.test(pathname)) {
      const m = /limit=(\d+)/.exec(path)
      return Promise.resolve(agentSessionsFixture(m ? Number(m[1]) : 5))
    }
    if (/^\/agents\/[^/]+\/cron$/.test(pathname)) {
      const m = /limit=(\d+)/.exec(path)
      return Promise.resolve(agentCronFixture(m ? Number(m[1]) : 5))
    }
    if (path.indexOf('/agents') === 0) return Promise.resolve(AGENTS)
    if (path.indexOf('/weather') === 0) {
      const wx = weatherFixture(pathname)
      if (wx) return Promise.resolve(wx)
    }
    if (path.indexOf('/overview') === 0) {
      const fixture = overviewFixture(pathname, path)
      if (fixture) return Promise.resolve(fixture)
    }
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
check('routes 贡献拿到组件', Boolean(page) && typeof page.render === 'function')

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

console.log('\n[F] 真 React 19 + jsdom')
check('根节点是真实 DOM（.hc-root 存在）', Boolean(q('.hc-root')))
check(
  '左栏 6 个分区按钮（真实 <button>）',
  qa('.hc-nav__item').length === 6 && qa('.hc-nav__item')[0].tagName === 'BUTTON'
)
// M4a：导航不是硬编码，是 /modules 的启用集，且按 nav_order 排
check(
  '导航顺序 = 后端 nav_order（概览 → 天气 → Agent 总览 …）',
  qa('.hc-nav__item').map((b) => b.textContent.trim()).join('|') ===
    ['概览', '天气', 'Agent 总览', '项目分组', 'Advisor', '升级'].join('|'),
  qa('.hc-nav__item').map((b) => b.textContent.trim()).join('|')
)
check(
  '导航清单真的来自 GET /modules?agent=…（真实请求记录）',
  restCalls.some((c) => c.path.split('?')[0] === '/modules' && /agent=code-worker/.test(c.path)),
  JSON.stringify(restCalls.filter((c) => c.path.indexOf('/modules') === 0).map((c) => c.path))
)
check('正常路径下没有回退提示（.hc-notice 不在 DOM）', qa('.hc-notice').length === 0)
check('作用域是真实 <select>', Boolean(q('.hc-select')) && q('.hc-select').tagName === 'SELECT')
check('9 个 <option> 被渲染进 DOM', qa('.hc-select option').length === 9, String(qa('.hc-select option').length))
check('默认选中 code-worker（后端 current）', q('.hc-select').value === 'code-worker', q('.hc-select').value)
check('作用域写进了 storage', store['scope.agent'] === 'code-worker', JSON.stringify(store))
check(
  '概览渲染 4 个统计卡（真实 DOM）',
  qa('.hc-pane .hc-card').length === 4,
  'cards=' + qa('.hc-pane .hc-card').length
)
check(
  '统计卡数字进了 DOM（系统 50% / cron 2 / 3 启用）',
  /50%/.test(container.textContent) && /2\s*\/\s*3 启用/.test(container.textContent),
  container.textContent.slice(0, 200)
)
check('cron 4 行 + 会话 20 行 = 24 行', qa('.hc-row').length === 24, 'rows=' + qa('.hc-row').length)
check('会话分页器是真的 <button>', qa('.hc-pager button').length === 2 && qa('.hc-pager button')[0].tagName === 'BUTTON')
check('第一页的「上一页」被禁用（真 disabled 属性）', qa('.hc-pager button')[0].disabled === true)
check('骨架屏已经从 DOM 消失（数据到了）', qa('.hc-skel').length === 0)

// ═══════════════════════════════════════════ [G] 样式自注入 + 计算样式（C6 验收）
// 桌面端不读 manifest.css，样式只能是 JS 自己插进来的 <style>；插了不等于生效，
// 所以这里断言的是 **getComputedStyle 算出来的值**，不是 className 有没有写对。
console.log('\n[G] 样式自注入 + 计算样式（桌面端唯一生效路径）')
const styleInit = document.querySelectorAll('style[data-hermes-console-style]').length
check('模块求值时已自动注入 <style data-hermes-console-style>', styleInit === 1, 'style nodes=' + styleInit)

// 幂等：连调两次注入函数，DOM 里仍然只有一份 <style>
const again1 = pluginApi.injectConsoleStyles()
const again2 = pluginApi.injectConsoleStyles()
const styleAfter = document.querySelectorAll('style[data-hermes-console-style]').length
check('连续两次调用注入函数后仍然只有 1 个 <style>', styleAfter === 1, 'style nodes=' + styleAfter)
check('注入返回的就是页面上那个节点（不是新建的孤儿）', again1 === again2 && again1 === document.querySelector('style[data-hermes-console-style]'))

const styleText = document.querySelector('style[data-hermes-console-style]').textContent
check('注入的文本里有类名规则（证明 bundle 里真的带样式）', styleText.indexOf('.hc-card {') !== -1 && styleText.indexOf('.hc-cards {') !== -1)
check('注入的文本里没有残留外链 @import（否则按页面 URL 解析会 404）', !/@import\s+["'(]/.test(styleText))
check('注入的文本里没有字面色值', !/#[0-9a-fA-F]{3,8}\s*[;)}\s]/.test(styleText) && !/\brgba?\(/.test(styleText) && !/\bhsla?\(/.test(styleText))

const _cs = (el) => window.getComputedStyle(el)
const cardsBox = q('.hc-pane .hc-cards')
const firstCard = q('.hc-pane .hc-card')
const cronRow = qa('.hc-pane .hc-rows--cron .hc-row')[0]
console.log('  · .hc-cards display            =', JSON.stringify(_cs(cardsBox).display))
console.log('  · .hc-cards gap                =', JSON.stringify(_cs(cardsBox).gap))
console.log('  · .hc-card borderRadius        =', JSON.stringify(_cs(firstCard).borderRadius))
console.log('  · .hc-card padding             =', JSON.stringify(_cs(firstCard).padding))
console.log('  · .hc-card__num fontSize       =', JSON.stringify(_cs(firstCard.querySelector('.hc-card__num')).fontSize))
console.log('  · .hc-card__unit fontSize      =', JSON.stringify(_cs(firstCard.querySelector('.hc-card__unit')).fontSize))
console.log('  · cron .hc-row display         =', JSON.stringify(_cs(cronRow).display))
console.log('  · cron .hc-row minHeight       =', JSON.stringify(_cs(cronRow).minHeight))
console.log('  · cron .hc-rows__head display  =', JSON.stringify(_cs(q('.hc-pane .hc-rows--cron .hc-rows__head')).display))

check('统计卡容器 display === grid', _cs(cardsBox).display === 'grid', _cs(cardsBox).display)
check('统计卡 borderRadius === 10px（--r-md 已落成字面量）', _cs(firstCard).borderRadius === '10px', _cs(firstCard).borderRadius)
check('统计卡 padding === 16px（卡内 --s4）', _cs(firstCard).padding === '16px', _cs(firstCard).padding)
check('统计卡之间 gap === 16px（卡间 --s4）', _cs(cardsBox).gap === '16px', _cs(cardsBox).gap)
check('主数值 fontSize === 20px（--fs-xl）', _cs(firstCard.querySelector('.hc-card__num')).fontSize === '20px', _cs(firstCard.querySelector('.hc-card__num')).fontSize)
check('单位 fontSize === 12px（--fs-sm，比主值弱一档）', _cs(firstCard.querySelector('.hc-card__unit')).fontSize === '12px', _cs(firstCard.querySelector('.hc-card__unit')).fontSize)
check('定时任务行 display === grid', _cs(cronRow).display === 'grid', _cs(cronRow).display)
check('定时任务行高 === 34px（--row-h）', _cs(cronRow).minHeight === '34px', _cs(cronRow).minHeight)
check('表头也是 grid（与数据行共享列定义）', _cs(q('.hc-pane .hc-rows--cron .hc-rows__head')).display === 'grid')
check('表头列定义与数据行一致（同一份 --row-cols）', _cs(q('.hc-pane .hc-rows--cron .hc-rows__head')).gridTemplateColumns === _cs(cronRow).gridTemplateColumns,
  _cs(cronRow).gridTemplateColumns)

const activeNav = q('.hc-nav__item--active')
const idleNav = qa('.hc-nav__item').find((n) => !n.classList.contains('hc-nav__item--active'))
check('选中分区：左描边宽 === 2px（--nav-mark）', _cs(activeNav).borderLeftWidth === '2px', _cs(activeNav).borderLeftWidth)
check('选中分区：文字权重 600（未选中 400）', _cs(activeNav).fontWeight === '600' && _cs(idleNav).fontWeight !== '600',
  _cs(activeNav).fontWeight + ' / ' + _cs(idleNav).fontWeight)

const widgetBox = q('.hc-pane .hc-widgets')
check('看板挂件是卡片栅格（display grid）', Boolean(widgetBox) && _cs(widgetBox).display === 'grid', widgetBox ? _cs(widgetBox).display : 'no .hc-widgets')
check('挂件每格都有名字 + 次数（不是一串 ×N 文本）',
  qa('.hc-pane .hc-widget').length >= 1 &&
    qa('.hc-pane .hc-widget').every((w) => w.querySelector('.hc-widget__label') && w.querySelector('.hc-widget__num')),
  'widgets=' + qa('.hc-pane .hc-widget').length)


// 真点击「天气」
const weatherBtn = qa('.hc-nav__item').find((b) => /天气/.test(b.textContent))
const overviewPaneBefore = q('.hc-pane')
await act(async () => {
  weatherBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)

check('点击后只剩当前分区面板（旧面板已卸载）', qa('.hc-pane').length === 1, String(qa('.hc-pane').length))
// t_a67a2bc2：切分区改为「只挂载当前分区」。旧断言（面板复用 / idle / aria-hidden）测的是
// 「挂载后一直挂着、只切 visibility」那套；本卡要求按需挂载/卸载（实测旧行为 DOM 574 → 9816），
// 所以这里改成断言「旧面板真的被卸载了」。
check('概览面板已被卸载（不是同一个 DOM 节点）', q('.hc-pane') !== overviewPaneBefore)
check('当前面板是天气（标题跟着换）', /天气/.test(q('.hc-pane .hc-pane__title').textContent),
  q('.hc-pane .hc-pane__title').textContent)
check('DOM 里没有隐藏的旧分区（无 .hc-pane--idle、无 aria-hidden 面板）',
  qa('.hc-pane--idle').length === 0 && qa('.hc-pane').every((p) => p.getAttribute('aria-hidden') !== 'true'))
check('导航选中态跟着换到天气', /天气/.test(q('.hc-nav__item--active').textContent))
check('顶栏标题跟着换', /天气/.test(q('.hc-topbar__title').textContent), q('.hc-topbar__title').textContent)
check('nav.section 记到 storage', store['nav.section'] === 'weather', JSON.stringify(store))

// 天气分区的真 DOM 断言（等 effect + 三次请求落地）
await act(flush)
await act(flush)
const wxPane = qa('.hc-pane')[0]
check('当前天气卡进了真 DOM（.hc-wx-now）', Boolean(wxPane.querySelector('.hc-wx-now')))
check('温度按后端原值渲染（18.8℃）', wxPane.querySelector('.hc-wx-now__temp').textContent === '18.8℃',
  wxPane.querySelector('.hc-wx-now__temp').textContent)
check('单位标在界面上（46% / 44km/h / 14μg/m³）',
  /46%/.test(wxPane.textContent) && /44km\/h/.test(wxPane.textContent) && /14μg\/m³/.test(wxPane.textContent),
  wxPane.textContent.slice(0, 200))
check('来源 + 更新时间在真 DOM 里', /来源 彩云天气/.test(wxPane.textContent) &&
  /更新 \d\d-\d\d \d\d:\d\d（/.test(wxPane.textContent))
check('降级过程在真 DOM 里（和风 ×空）', /和风天气 ×空/.test(wxPane.textContent))
check('逐小时 24 个真 DOM 节点 + 可横向滚动容器',
  wxPane.querySelectorAll('.hc-wx-hour').length === 24 && Boolean(wxPane.querySelector('.hc-wx-hours')),
  String(wxPane.querySelectorAll('.hc-wx-hour').length))
check('逐小时 pop 为 null 的那格显示「降水 —」', /降水 —/.test(wxPane.textContent))
check('多日 7 个真 DOM 行', wxPane.querySelectorAll('.hc-wx-day').length === 7,
  String(wxPane.querySelectorAll('.hc-wx-day').length))
check('「N 小时后」结果内联在 DOM 里', /6小时后\(19时\): 15°C 多云/.test(wxPane.textContent))
check('城市输入框是真实 <input>', Boolean(wxPane.querySelector('input.hc-wx-input')) &&
  wxPane.querySelector('input.hc-wx-input').tagName === 'INPUT')

// 真点击 48h：结果重取、选中态换
const seg48 = Array.from(wxPane.querySelectorAll('.hc-wx-seg__item')).find((b) => /48h/.test(b.textContent))
await act(async () => {
  seg48.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check('点 48h 后选中态是真 DOM 的 accent 类', Array.from(wxPane.querySelectorAll('.hc-wx-seg__item--on')).map((b) => b.textContent).join('') === '48h',
  Array.from(wxPane.querySelectorAll('.hc-wx-seg__item--on')).map((b) => b.textContent).join(''))
check('点 48h 后确实发了 /weather/at?hours=48',
  restCalls.filter((c) => c.path.indexOf('/weather/at') === 0 && /hours=48/.test(c.path)).length >= 1,
  JSON.stringify(restCalls.filter((c) => c.path.indexOf('/weather/at') === 0).map((c) => c.path)))

// 真在输入框里打字（input 事件）+ 点「查询」
const wxInput = wxPane.querySelector('input.hc-wx-input')
const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
await act(async () => {
  setter.call(wxInput, '上海')
  wxInput.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await act(flush)
const applyBtn = Array.from(wxPane.querySelectorAll('.hc-btn')).find((b) => b.textContent === '查询')
check('查询按钮是真的 <button>', Boolean(applyBtn) && applyBtn.tagName === 'BUTTON')
await act(async () => {
  applyBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check('按城市查询后请求带 place=上海（不写死肥西）',
  restCalls.filter((c) => c.path.indexOf('/weather/now') === 0 && /%E4%B8%8A%E6%B5%B7/.test(c.path)).length >= 1,
  JSON.stringify(restCalls.filter((c) => c.path.indexOf('/weather/now') === 0).map((c) => c.path)))


// 切回概览：真 effect 不重跑 → 请求数不变
const overviewCallsBefore = restCalls.filter((c) => c.path.indexOf('/overview') === 0).length
const overviewBtn = qa('.hc-nav__item').find((b) => /概览/.test(b.textContent))
await act(async () => {
  overviewBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
const overviewCallsAfter = restCalls.filter((c) => c.path.indexOf('/overview') === 0).length
check(
  '切走再切回没有重新请求 /overview（真 effect 也没重跑）',
  overviewCallsBefore === overviewCallsAfter && overviewCallsBefore > 0,
  'before=' + overviewCallsBefore + ' after=' + overviewCallsAfter
)
// 概览一次取齐的端点清单（写死清单而不是次数：次数会被 analytics 换窗口等正常行为带偏）。
const overviewPathsSeen = Array.from(
  new Set(
    restCalls
      .filter((c) => c.path.indexOf('/overview') === 0)
      .map((c) => c.path.split('?')[0])
  )
).sort()
check(
  '概览取齐 8 个端点（自检 + layout/system/analytics/cron/sessions/status/logs）',
  JSON.stringify(overviewPathsSeen) ===
    JSON.stringify([
      '/overview',
      '/overview/analytics',
      '/overview/cron',
      '/overview/layout',
      '/overview/logs',
      '/overview/sessions',
      '/overview/status',
      '/overview/system'
    ]),
  JSON.stringify(overviewPathsSeen)
)
check('切回之后概览仍是有数据的卡片（没有被骨架屏替换）', qa('.hc-pane .hc-card').length === 4)

// ═══════════════════════════════════════════ [H] Agent 总览（C7 验收）
console.log('\n[H] Agent 总览：9 行 / 失败行标记 / 排序 / 展开懒加载')
const agentsBtn = qa('.hc-nav__item').find((b) => /Agent 总览/.test(b.textContent))
check('导航里有「Agent 总览」入口', Boolean(agentsBtn))
await act(async () => {
  agentsBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)

/** 等到某个选择器出现（React 的 effect + promise 需要几轮宏任务，固定次数容易假失败）。 */
async function waitFor(pane, selector, rounds) {
  for (let i = 0; i < (rounds || 8); i += 1) {
    if (pane && pane.querySelector(selector)) return true
    await act(flush)
  }
  return Boolean(pane && pane.querySelector(selector))
}

let agPane = qa('.hc-pane').find((p) => p.querySelector('.hc-ag'))
const agReady = await waitFor(agPane, '.hc-ag__item')
check('Agent 总览数据到位（不是骨架屏）', agReady)
const agItems = Array.from(agPane.querySelectorAll('.hc-ag__item'))
check('渲染出 9 行（真实 DOM 节点）', agItems.length === 9, 'rows=' + agItems.length)
check(
  '9 行的 data-agent-id 与后端枚举一致',
  agItems.map((el) => el.getAttribute('data-agent-id')).sort().join(',') ===
    AGENTS.agents.map((a) => a.id).sort().join(','),
  agItems.map((el) => el.getAttribute('data-agent-id')).join(',')
)
const unavailable = agPane.querySelectorAll('.hc-ag__item[data-unavailable="true"]')
check('失败行带 data-unavailable 标记（恰好 1 行）', unavailable.length === 1, 'unavailable=' + unavailable.length)
check('失败行是 xiaomo', unavailable.length === 1 && unavailable[0].getAttribute('data-agent-id') === 'xiaomo',
  unavailable.length ? unavailable[0].getAttribute('data-agent-id') : '')
check('失败行显示了原因 + 单独重试按钮',
  unavailable.length === 1 &&
    /模拟 state\.db 被占用/.test(unavailable[0].textContent) &&
    Array.from(unavailable[0].querySelectorAll('button')).some((b) => /重试这一行/.test(b.textContent)))
check('失败行的会话数显示「—」而不是 0',
  unavailable.length === 1 && unavailable[0].querySelector('.hc-ag__cell--sessions').textContent === '—',
  unavailable.length ? unavailable[0].querySelector('.hc-ag__cell--sessions').textContent : '')
check('分区标题写明「本页显示全部 9 个 agent」',
  /本页显示全部 9 个 agent/.test(agPane.textContent), agPane.textContent.slice(0, 120))
check('本分区不显示「作用域 x」chip（忽略顶部选择器）',
  !/作用域 code-worker|作用域 xiaomo/.test(agPane.textContent) && /全部 agent/.test(agPane.textContent))

check('默认排序=最近活动倒序（最新的是 code-worker）',
  agItems[0].getAttribute('data-agent-id') === 'code-worker',
  agItems.map((el) => el.getAttribute('data-agent-id')).join(','))

const sortSelect = agPane.querySelector('.hc-ag__sort .hc-select')
check('排序是真实 <select>', Boolean(sortSelect) && sortSelect.tagName === 'SELECT')
await act(async () => {
  sortSelect.value = 'sessions'
  sortSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
const bySessions = Array.from(agPane.querySelectorAll('.hc-ag__item')).map((el) => el.getAttribute('data-agent-id'))
check('按会话数排序：worker(510) 第一', bySessions[0] === 'worker', bySessions.join(','))
await act(async () => {
  sortSelect.value = 'name'
  sortSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)
const byName = Array.from(agPane.querySelectorAll('.hc-ag__item')).map((el) => el.getAttribute('data-agent-id'))
check('按名称排序：apm-worker 第一', byName[0] === 'apm-worker', byName.join(','))

const callsBeforeExpand = restCalls.filter((c) => /^\/agents\/[^/]+\/(sessions|cron)$/.test(c.path.split('?')[0])).length
check('展开前没有预取任何下钻数据（懒加载）', callsBeforeExpand === 0, 'calls=' + callsBeforeExpand)

const codeRow = Array.from(agPane.querySelectorAll('.hc-ag__item')).find(
  (el) => el.getAttribute('data-agent-id') === 'code-worker'
)
const drillBtn = Array.from(codeRow.querySelectorAll('button')).find((b) => b.textContent === '下钻')
check('每行有「下钻」按钮', Boolean(drillBtn))
await act(async () => {
  drillBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
const detailReady = await waitFor(codeRow, '.hc-ag__detail')
const detail = codeRow.querySelector('.hc-ag__detail')
check('展开后出现详情面板（.hc-ag__detail）', detailReady && Boolean(detail))
check('详情里有会话列表（5 条）',
  detail.querySelectorAll('.hc-ag__detail-panel')[0].querySelectorAll('.hc-ag__list-row').length === 5,
  String(detail.querySelectorAll('.hc-ag__detail-panel')[0].querySelectorAll('.hc-ag__list-row').length))
check('详情里有 cron 列表（5 条，含「已暂停」标记）',
  detail.querySelectorAll('.hc-ag__detail-panel')[1].querySelectorAll('.hc-ag__list-row').length === 5 &&
    /已暂停/.test(detail.textContent))
check('展开确实发了 /agents/code-worker/sessions 与 /cron',
  restCalls.some((c) => /^\/agents\/code-worker\/sessions\?limit=5$/.test(c.path)) &&
    restCalls.some((c) => /^\/agents\/code-worker\/cron\?limit=5$/.test(c.path)),
  JSON.stringify(restCalls.filter((c) => /\/agents\/[^/]+\//.test(c.path)).map((c) => c.path)))
check('操作列 4 个按钮都是真实 <button>',
  Array.from(codeRow.querySelectorAll('.hc-ag__cell--ops button')).length === 4 &&
    Array.from(codeRow.querySelectorAll('.hc-ag__cell--ops button')).every((b) => b.tagName === 'BUTTON'))

// 回到概览，交还后面「切作用域」那段断言所需的初始状态
await act(async () => {
  qa('.hc-nav__item').find((b) => /^概览/.test(b.textContent)).dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)

// 真改 <select>：只有受影响分区进骨架屏
const selectEl = q('.hc-select')
await act(async () => {
  selectEl.value = 'xiaomo'
  selectEl.dispatchEvent(new window.Event('change', { bubbles: true }))
})
check('切作用域写入 storage', store['scope.agent'] === 'xiaomo', JSON.stringify(store))
check('顶栏与导航没有重建（标题仍是概览）', Boolean(q('.hc-topbar__title')) && qa('.hc-nav__item').length === 6)
check('连接状态仍显示已连接', Boolean(q('.hc-conn--ok')))
await act(flush)
await act(flush)
const scopedOverviewCall = restCalls.filter((c) => c.path.indexOf('/overview') === 0).pop()
check('新作用域的请求真的发出去了（带 agent=xiaomo）', /agent=xiaomo/.test(scopedOverviewCall.path), scopedOverviewCall.path)
check('新作用域数据到位后回到有数据的卡片', qa('.hc-pane .hc-card').length === 4)
check('内容区 chip 显示当前作用域', /作用域 xiaomo/.test(container.textContent))

// 键盘可达性：导航按钮与 select 都在可聚焦集合里
const focusables = qa('button, select, [tabindex]:not([tabindex="-1"])')
check(
  '可聚焦元素包含分区按钮 + 作用域 select',
  focusables.filter((el) => el.tagName === 'BUTTON').length >= 4 && focusables.some((el) => el.tagName === 'SELECT'),
  'focusables=' + focusables.length
)

// ═══════════════════════════════════════════ [I] 项目分组（t_1dea3c49 重排后的结构）
//
// 这一段历史断言的是 C8b 那版「三个数字卡 + 20 条/页 + 分页器 + 忽略占位 + 不许有勾选框」。
// t_1dea3c49 把整页重排成「状态条 + 项目条 + 会话区 + 检查器 + 折叠抽屉」：
//   · 状态条从 3 个数字卡变成 5 格（项目 / 已分组 / 未分组 / 待确认 / 批次）；
//   · 建议表搬进默认收起的「待确认建议」抽屉，**分页器被有意去掉**（25 条一次全渲染）；
//   · 勾选框从「只读不许有」变成「必须有多选」（检查器里的批量归入靠它）。
// 所以这里断的是新结构；更深的验收（真后端 + 真浏览器 + 900/1350 两宽度 + 写操作回读）
// 在 scripts/probe-autogroup-ui.mjs，别在这份 jsdom 探针里重复造一遍。
console.log('\n[I] 项目分组：入口 / 状态条 / 折叠区 / 建议表 / 扫描轮询 / 空态 / 失败态')
const aggBtn = qa('.hc-nav__item').find((b) => /项目分组/.test(b.textContent))
check('导航里出现「项目分组」入口', Boolean(aggBtn))
await act(async () => {
  aggBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
check(
  '点击后切到该分区（导航选中态 + 顶栏标题都换成项目分组）',
  /项目分组/.test(q('.hc-nav__item--active').textContent) && /项目分组/.test(q('.hc-topbar__title').textContent),
  q('.hc-topbar__title').textContent
)
check('nav.section 记到 storage', store['nav.section'] === 'autogroup', JSON.stringify(store))
const aggPane = qa('.hc-pane').find((p) => p.querySelector('.hc-agg'))
check('分区面板真的挂载（.hc-agg 在 DOM 里）', Boolean(aggPane))
const aggReady = await waitFor(aggPane, '[data-agg2-strip]')
check('状态条数据到位（不是骨架屏）', aggReady)

// ---- ① 状态条：5 格，数字分别来自 /autogroup/status 与 /autogroup/entities
const aggStrip = aggPane.querySelector('[data-agg2-strip]')
const aggMetrics = Array.from(aggStrip.querySelectorAll('[data-agg2-metric]'))
check(
  '状态条 5 格：项目 / 已分组 / 未分组 / 待确认 / 批次',
  aggMetrics.map((el) => el.getAttribute('data-agg2-metric')).join('|') ===
    'projects|grouped|ungrouped|suggested|batches',
  aggMetrics.map((el) => el.getAttribute('data-agg2-metric')).join('|')
)
const metricVal = (id) => {
  const el = aggStrip.querySelector('[data-agg2-metric="' + id + '"]')
  return el ? el.getAttribute('data-agg2-metric-value') : '(missing)'
}
check('项目数来自 /autogroup/entities（3 个）', metricVal('projects') === '3', metricVal('projects'))
check('待确认 = /autogroup/status 的 suggested（25）', metricVal('suggested') === '25', metricVal('suggested'))
check('批次 = /autogroup/status 的 batches_total（2）', metricVal('batches') === '2', metricVal('batches'))
check(
  '每格都带口径 title（不是光秃秃五个数）',
  aggMetrics.every((el) => (el.getAttribute('title') || '').length > 8),
  aggMetrics.map((el) => (el.getAttribute('title') || '').slice(0, 10)).join(' / ')
)
check(
  '状态条读的是 /autogroup/status + /autogroup/entities',
  restCalls.some((c) => c.path.split('?')[0] === '/autogroup/status') &&
    restCalls.some((c) => c.path.split('?')[0] === '/autogroup/entities')
)
const aggSessionRows = aggPane.querySelectorAll('.hc-agg2__sess').length
check(
  '已分组 + 未分组 = 会话区可见会话数（两个数同源，不各算一套）',
  aggSessionRows > 0 && Number(metricVal('grouped')) + Number(metricVal('ungrouped')) === aggSessionRows,
  'grouped=' + metricVal('grouped') + ' ungrouped=' + metricVal('ungrouped') + ' rows=' + aggSessionRows
)
check('已分组数 = 归属表 applied 行命中的会话数（2）', metricVal('grouped') === '2', metricVal('grouped'))

// ---- ② 折叠区：6 个抽屉默认全收起（扫描/AI/模型/自动模式/规则/撤销各只一个入口）
// 「模型」抽屉是 t_3c2817b1 加的项目分组 AI 两级配置入口（第 6 个），此处条数随之更新。
const aggDrawers = Array.from(aggPane.querySelectorAll('[data-agg2-drawer]'))
check(
  '折叠区 6 个抽屉，默认全部收起',
  aggDrawers.length === 6 && aggDrawers.every((d) => !d.hasAttribute('open')),
  'drawers=' + aggDrawers.length + ' open=' + aggDrawers.filter((d) => d.hasAttribute('open')).length
)
check(
  '「待确认建议」区块只有一个（抽屉唯一，标题里提到的入口提示不算区块）',
  aggPane.querySelectorAll('[data-agg2-drawer="suggested"]').length === 1 &&
    aggPane.querySelectorAll('[data-agg2-suggested-table]').length === 1,
  'drawers=' + aggPane.querySelectorAll('[data-agg2-drawer="suggested"]').length +
    ' tables=' + aggPane.querySelectorAll('[data-agg2-suggested-table]').length
)

// ---- ③ 建议表：整表渲染（不分页），行字段齐全，多选可点
const aggTable = aggPane.querySelector('[data-agg2-suggested-table]')
const aggRows = () => Array.from(aggPane.querySelectorAll('.hc-agg__row'))
check(
  '建议表整表渲染 25 行（分页器已随 t_1dea3c49 移除，整表进抽屉，一条不藏）',
  Boolean(aggTable) &&
    aggTable.getAttribute('data-agg2-suggested-table') === '25' &&
    aggRows().length === 25,
  'table=' + (aggTable && aggTable.getAttribute('data-agg2-suggested-table')) + ' rows=' + aggRows().length
)
check(
  '行 = 会话标题 + agent + 建议项目 + 判定依据 + 时间（五个字段都在）',
  aggRows().every(
    (r) =>
      r.querySelector('.hc-agg__title') &&
      r.querySelector('.hc-agg__cell--agent') &&
      r.querySelector('.hc-agg__project') &&
      r.querySelector('.hc-agg__cell--basis') &&
      r.querySelector('.hc-agg__cell--time')
  )
)
check(
  'rule 行显示命中的关键词（规则命中：看板、cron、调度）',
  /规则命中：看板、cron、调度/.test(aggPane.textContent)
)
check(
  'llm 行显示置信度与理由（置信度 81% + 理由原文）',
  /模型判断（置信度 81%）/.test(aggPane.textContent) &&
    /首条用户消息在讲定时任务编排/.test(aggPane.textContent)
)
const aggBoxes = Array.from(aggPane.querySelectorAll('[data-agg-select]'))
check(
  '每行都有勾选框且可点（多选，批量归入用）',
  aggBoxes.length === 25 && aggBoxes.every((b) => b.disabled === false),
  'boxes=' + aggBoxes.length + ' disabled=' + aggBoxes.filter((b) => b.disabled).length
)
check(
  '每行都有内联「切到…」下拉（写归属表，可撤销）',
  aggPane.querySelectorAll('.hc-select[data-agg-switch]').length === 25,
  String(aggPane.querySelectorAll('.hc-select[data-agg-switch]').length)
)
check(
  '每行都有真实动作按钮「同意并归入」（不是 disabled 占位）',
  aggRows().every((r) => {
    const b = r.querySelector('.hc-agg__cell--ops button')
    return b && b.tagName === 'BUTTON' && /同意并归入/.test(b.textContent) && b.disabled === false
  })
)

// ---- ④ 扫描：唯一入口 + 真 POST + 轮询 + 完成摘要 + 刷新
const aggScanBtn = () => aggPane.querySelector('[data-agg2-scan]')
check(
  '扫描入口只有一个（折叠区抽屉里的主按钮）',
  aggPane.querySelectorAll('[data-agg2-scan]').length === 1 &&
    // t_8754de3f 有意变更：按钮文案随勾选变（未勾选 = 分析全部未分组 N 条）
    /分析全部未分组 \d+ 条/.test(aggScanBtn().textContent),
  String(aggPane.querySelectorAll('[data-agg2-scan]').length)
)
const aggPostsBefore = restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/scan').length
const aggStatusBefore = restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/status').length
const aggSuggBefore = restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/suggestions').length
const aggJobCalls = () => restCalls.filter((c) => /^\/autogroup\/job\//.test(c.path.split('?')[0]))
await act(async () => {
  aggScanBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
check('点击后按钮立刻禁用（真 disabled 属性）', aggScanBtn().disabled === true)
await act(flush)
const aggPostsAfter = restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/scan').length
check('发出了 POST /autogroup/scan', aggPostsAfter === aggPostsBefore + 1, 'posts=' + (aggPostsAfter - aggPostsBefore))
check(
  '扫描请求是 POST（不是 GET）',
  restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/scan').every((c) => c.method === 'POST')
)
check('第一枪轮询 GET /autogroup/job/{id}', aggJobCalls().length === 1, JSON.stringify(aggJobCalls().map((c) => c.path)))
check('job 还在跑时按钮仍然禁用（防重复触发）', aggScanBtn().disabled === true)
check(
  '进度文案在界面上（已处理 12 / 40 + job id）',
  /已处理 12 \/ 40/.test(aggPane.textContent) && /job-probe0001/.test(aggPane.textContent),
  aggPane.querySelector('.hc-agg__hint') ? aggPane.querySelector('.hc-agg__hint').textContent : ''
)
// 轮询间隔 ≥2s：等 2.2s 后必须有第二次 GET
await act(async () => {
  await new Promise((r) => setTimeout(r, 2200))
})
await act(flush)
await act(flush)
check('轮询继续（≥2 次 GET /autogroup/job/，间隔 ≥2s）', aggJobCalls().length >= 2, 'polls=' + aggJobCalls().length)
check(
  'job 完成后按钮恢复可用（文字回到「分析全部未分组 N 条」）',
  aggScanBtn().disabled === false && /分析全部未分组 \d+ 条/.test(aggScanBtn().textContent),
  aggScanBtn().textContent
)
check(
  '完成后刷新了状态条与列表（又各发了一次 GET）',
  restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/status').length > aggStatusBefore &&
    restCalls.filter((c) => c.path.split('?')[0] === '/autogroup/suggestions').length > aggSuggBefore
)
check(
  '扫描结果摘要进了 DOM（扫描 40 / 命中 36 / 未命中 4 / 落库 36）',
  /扫描 40 个会话 · 命中 36 · 未命中 4 · 落库 36 条/.test(aggPane.textContent),
  aggPane.querySelector('.hc-agg__scan-result')
    ? aggPane.querySelector('.hc-agg__scan-result').textContent.slice(0, 120)
    : '(no .hc-agg__scan-result)'
)
check('完成后列表回到有数据的行（不是空白）', aggRows().length > 0, 'rows=' + aggRows().length)

// ---- ⑤ 扫描失败：ErrorState + 后端 message 原样
agState.scanMode = 'fail'
await act(async () => {
  aggScanBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const aggScanErr = await waitFor(aggPane, '.hc-state--error')
check('扫描失败渲染 ErrorState', aggScanErr && Boolean(aggPane.querySelector('.hc-state--error')))
check(
  'ErrorState 带后端给的 error.message（原话，不自己编）',
  /RuntimeError：探针模拟：state\.db 读失败/.test(aggPane.textContent),
  aggPane.querySelector('.hc-state--error') ? aggPane.querySelector('.hc-state--error').textContent : ''
)
check(
  'ErrorState 标题是「扫描失败」+ 带 job id',
  /扫描失败/.test(aggPane.textContent) && /job-probe0001/.test(aggPane.textContent)
)
check('失败后按钮恢复可用（能重试）', aggScanBtn().disabled === false)
agState.scanMode = 'ok'

// ---- ⑦ >200 条（t_4d192e1b）：后端单次上限 200，建议 500 条时三个数字必须口径一致
//
// 旧行为：limit 固定 200 却整表渲染、徽标用行数算 → 状态条 500 / 徽标 200 / 明细 200 行。
// 新行为：总数只认后端 data.total（状态条与徽标同值），明细按 offset 追加，抽屉里明写
// 「显示 X / 共 N 条」，剩余条数挂在「加载更多」上，点完能取全 500 条。
const aggCounts = () => aggPane.querySelector('[data-agg2-suggested-counts]')
const aggLoaded = () => {
  const el = aggPane.querySelector('[data-agg2-suggested-loaded]')
  return el ? el.getAttribute('data-agg2-suggested-loaded') : '(missing)'
}
const aggTotal = () => {
  const el = aggPane.querySelector('[data-agg2-suggested-total]')
  return el ? el.getAttribute('data-agg2-suggested-total') : '(missing)'
}
const aggMoreBtn = () => aggPane.querySelector('[data-agg2-suggested-more]')
const aggBadge = () => {
  const head = aggPane.querySelector('[data-agg2-drawer-head="suggested"]')
  const chip = head ? head.querySelector('.hc-chip') : null
  return chip ? chip.textContent : '(missing)'
}

// 25 ≤ 200 的小场景：显示 = 总数，没有「加载更多」（不该凭空多出一个按钮）
check(
  '≤200 条时抽屉明写「显示 25 / 共 25 条」（不是只给一个裸数字）',
  Boolean(aggCounts()) &&
    aggLoaded() === '25' &&
    aggTotal() === '25' &&
    /显示 25 \/ 共 25 条/.test(aggCounts().textContent),
  aggCounts() ? aggCounts().textContent : '(no counts)'
)
check('≤200 条时没有「加载更多」按钮（已经取完，不造多余入口）', aggMoreBtn() === null)
check(
  '状态条 = 抽屉徽标 = 总数字段（三个数字同源，不是各算一套）',
  metricVal('suggested') === aggTotal() && aggBadge().replace(/[^0-9]/g, '') === aggTotal(),
  'status=' + metricVal('suggested') + ' badge=' + aggBadge() + ' total=' + aggTotal()
)

// 500 条：把桩数据换成 500 条，点工具条「刷新」重读
const aggKeepMany = agState.suggestions
agState.suggestions = Array.from({ length: 500 }, (_, i) => agSuggestion(i))
const aggRefresh = () => aggPane.querySelector('[data-agg2-refresh]')
await act(async () => {
  aggRefresh().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check(
  '500 条时状态条 = 后端总数 500（不是明细行数 200）',
  metricVal('suggested') === '500',
  metricVal('suggested')
)
check(
  '500 条时抽屉徽标 = 500 条（与状态条同一个数）',
  aggBadge().replace(/[^0-9]/g, '') === '500',
  aggBadge()
)
check(
  '500 条时抽屉写「显示 200 / 共 500 条」（差额写在明面上）',
  aggLoaded() === '200' && aggTotal() === '500' && /显示 200 \/ 共 500 条/.test(aggCounts().textContent),
  aggCounts().textContent
)
check(
  '500 条时明细先渲染 200 行（后端单次上限），表属性与行数一致',
  aggTable.getAttribute('data-agg2-suggested-table') === '200' && aggRows().length === 200,
  'table=' + aggTable.getAttribute('data-agg2-suggested-table') + ' rows=' + aggRows().length
)
check(
  '「加载更多」写明还剩 300 条（剩余量不是黑箱）',
  Boolean(aggMoreBtn()) &&
    aggMoreBtn().getAttribute('data-agg2-suggested-remaining') === '300' &&
    /还有 300 条/.test(aggMoreBtn().textContent),
  aggMoreBtn() ? aggMoreBtn().textContent : '(no more button)'
)
check(
  '500 条场景没有「口径不一致」告警（status 与明细接口本来就同源）',
  aggPane.querySelector('[data-agg2-count-mismatch]') === null
)
// 点两次「加载更多」：200 → 400 → 500，取完按钮消失
for (const expect of ['400', '500']) {
  await act(async () => {
    aggMoreBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(flush)
  await act(flush)
  check(
    '点「加载更多」后明细涨到 ' + expect + ' 行',
    aggRows().length === Number(expect) && aggLoaded() === expect,
    'rows=' + aggRows().length + ' loaded=' + aggLoaded()
  )
}
check(
  '取满 500 条后按钮消失（不留一个点了没反应的按钮）',
  aggMoreBtn() === null,
  aggMoreBtn() ? aggMoreBtn().textContent : '(gone)'
)
check(
  '取满后抽屉写「显示 500 / 共 500 条」，状态条与徽标仍是 500',
  /显示 500 \/ 共 500 条/.test(aggCounts().textContent) &&
    metricVal('suggested') === '500' &&
    aggBadge().replace(/[^0-9]/g, '') === '500',
  aggCounts().textContent + ' / status=' + metricVal('suggested') + ' / badge=' + aggBadge()
)
// 刷新 = 重新读第一页：追加页必须作废，否则会留下 300 条「上一轮」的行
await act(async () => {
  aggRefresh().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
check(
  '刷新后回到「显示 200 / 共 500 条」（追加页被作废，不残留旧行）',
  aggRows().length === 200 && aggLoaded() === '200' && aggTotal() === '500' && Boolean(aggMoreBtn()),
  'rows=' + aggRows().length + ' loaded=' + aggLoaded() + ' total=' + aggTotal()
)
agState.suggestions = aggKeepMany

// ---- ⑥ 空态：把桩数据清空 + 点工具条「刷新」→ 必须是 Empty 而不是空白页
const aggKeep = agState.suggestions
agState.suggestions = []
const aggRefreshBtn = () => aggPane.querySelector('[data-agg2-refresh]')
check('工具条「刷新」按钮存在且可点', Boolean(aggRefreshBtn()) && aggRefreshBtn().disabled === false)
await act(async () => {
  aggRefreshBtn().dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const aggEmpty = aggPane.querySelector('.hc-state:not(.hc-state--error)')
check('无建议时是 Empty 状态块（不是空白）', Boolean(aggEmpty), aggPane.textContent.slice(0, 80))
check('空态标题说「还没有待确认建议」', Boolean(aggEmpty) && /还没有待确认建议/.test(aggEmpty.textContent))
check(
  '空态写清下一步（点「分析…」按钮）',
  Boolean(aggEmpty) && /分析/.test(aggEmpty.textContent),
  aggEmpty ? aggEmpty.textContent.slice(0, 160) : ''
)
check('空态时待确认数字跟着归零（不是留在 25）', metricVal('suggested') === '0', metricVal('suggested'))
agState.suggestions = aggKeep

// 真 React 卸载：不能有异常（effect 清理正确）
let unmountOk = true
try {
  await act(async () => {
    root.unmount()
  })
} catch (e) {
  unmountOk = false
}
check('卸载不抛（effect 清理正确）', unmountOk && container.innerHTML === '')

// 收尾：junction 必须用 rmdir/unlink 删链接本身 —— rmtree 会走进去删掉宿主的 node_modules。
// 链接摘掉之后再删整个临时目录（里面只有本进程写的 entry 副本）。
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
  console.log('DOM-PROBE-OK')
}
