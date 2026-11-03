/**
 * hermes-console · C2 外壳行为验收（真的求值构建产物，不是语法检查）
 *
 * 做的事：
 *  1. 把 dashboard/dist/index.js 原字节拷进桩树，用最小 node_modules 桩提供
 *     react / react/jsx-runtime（迷你 hook 运行时：state/effect 槽位 + deps 比较）；
 *  2. import 产物 → register(fakeCtx{rest,storage}) → 真的把组件树展开成宿主节点树；
 *  3. 用「点击」真实回调的方式验证：分区切换、缓存命中、作用域切换的局部骨架、
 *     持久化写入、窄窗类名存在；
 *  4. 再拷一份产物到 index_fail.js（独立模块实例 = 独立缓存），让 /agents 抛
 *     503，验证选择器退化成单选项 + 一行原因且外壳照常渲染（不白屏）。
 *  5. 另外直接跑 src/lib/rest.js 与 src/lib/format.js 的纯函数断言。
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..')
// 桩树落系统临时目录：插件目录里不留探针残渣
const STUB = join(tmpdir(), 'hc-shell-probe-' + process.pid)
const ENTRY = join(REPO, 'dashboard/dist/index.js')
const CSS = join(REPO, 'dashboard/dist/style.css')
const JS = join(REPO, 'dashboard/dist/index.js')

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

// ---------------------------------------------------------------- 桩树
rmSync(STUB, { recursive: true, force: true })
mkdirSync(join(STUB, 'node_modules', 'react', 'jsx-runtime'), { recursive: true })
writeFileSync(
  join(STUB, 'node_modules', 'react', 'package.json'),
  JSON.stringify({
    name: 'react',
    type: 'module',
    exports: { '.': './index.js', './jsx-runtime': './jsx-runtime/index.js' }
  })
)
writeFileSync(
  join(STUB, 'node_modules', 'react', 'index.js'),
  [
    'const rt = globalThis.__HC_REACT__',
    'export const useState = (...a) => rt.useState(...a)',
    'export const useEffect = (...a) => rt.useEffect(...a)',
    'export const useMemo = (fn) => fn()',
    'export const useCallback = (fn) => fn',
    'export const useRef = (v) => rt.useRef(v)',
    // cloneElement：概览把四个卡片函数的结果放进同一个数组，需要一处补 key。
    // 迷你运行时里元素就是 {type, props, key}，克隆只是合并 props。
    'export const cloneElement = (el, extra) => ({ ...el, props: { ...el.props, ...extra }, key: extra && extra.key !== undefined ? extra.key : el.key })',
    'export default { useState, useEffect, useMemo, useCallback, useRef, cloneElement }'
  ].join('\n')
)
writeFileSync(
  join(STUB, 'node_modules', 'react', 'jsx-runtime', 'index.js'),
  [
    'export function jsx(type, props, key) { return { type, props: props || {}, key } }',
    'export const jsxs = jsx',
    'export const Fragment = Symbol("Fragment")'
  ].join('\n')
)

// ------------------------------------------------- 迷你 hook 运行时
const pendingEffects = []
const comps = new Map()
let current = '<root>'
let hookIdx = 0

function record() {
  let rec = comps.get(current)
  if (!rec) {
    rec = { state: [], refs: [], effects: [] }
    comps.set(current, rec)
  }
  return rec
}

function withComp(name, fn) {
  const prev = [current, hookIdx]
  current = name
  hookIdx = 0
  try {
    return fn()
  } finally {
    ;[current, hookIdx] = prev
  }
}

function useState(init) {
  const rec = record()
  const i = hookIdx++
  if (!(i in rec.state)) rec.state[i] = typeof init === 'function' ? init() : init
  return [
    rec.state[i],
    (next) => {
      rec.state[i] = typeof next === 'function' ? next(rec.state[i]) : next
    }
  ]
}

function useEffect(fn, deps) {
  const rec = record()
  const i = hookIdx++
  const prev = rec.effects[i]
  const same =
    prev && deps && prev.deps && deps.length === prev.deps.length && deps.every((d, k) => d === prev.deps[k])
  if (same) return
  pendingEffects.push(() => {
    if (prev && typeof prev.cleanup === 'function') prev.cleanup()
    const cleanup = fn()
    rec.effects[i] = { deps: deps ? deps.slice() : null, cleanup: typeof cleanup === 'function' ? cleanup : null }
  })
}

function useRef(v) {
  const rec = record()
  const i = hookIdx++
  if (!(i in rec.refs)) rec.refs[i] = { current: v }
  return rec.refs[i]
}

globalThis.__HC_REACT__ = { useState, useEffect, useRef }

// ------------------------------------------------------------- 展开 / 查询
function expand(node) {
  if (node === null || node === undefined || node === false || node === true) return null
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(expand).filter((x) => x !== null)
  if (typeof node.type === 'function') {
    return withComp(node.type.name || 'anon', () => expand(node.type(node.props || {})))
  }
  const props = node.props || {}
  return { type: node.type, props, key: node.key, children: expand(props.children) }
}

function flatten(tree, out = []) {
  if (!tree) return out
  if (Array.isArray(tree)) {
    tree.forEach((t) => flatten(t, out))
    return out
  }
  if (typeof tree === 'string') return out
  out.push(tree)
  flatten(tree.children, out)
  return out
}

function textOf(tree) {
  if (!tree) return ''
  if (Array.isArray(tree)) return tree.map(textOf).join(' ')
  if (typeof tree === 'string') return tree
  let inner = textOf(tree.children)
  // select 的当前值来自 option 的子节点，按钮文字来自 span 子节点，都在 children 里
  return inner
}

function classesOf(node) {
  return (node.props && node.props.className) || ''
}

function findByClass(nodes, needle) {
  return nodes.filter((n) => classesOf(n).split(/\s+/).includes(needle))
}

/**
 * 取「某个分区面板」的子树节点。
 *
 * 分区面板切走是 visibility 隐藏、节点仍挂在树上，所以像 `.hc-wx-seg__item` 这种被多个
 * 分区复用的样式类（概览的 token 窗口 7/30/90 天也用它）在全树查询里会混在一起。
 * 断言必须按面板限定，否则查到的数字取决于「别的分区碰巧渲染了什么」。
 */
function subtreeOf(nodes, anchorClass) {
  const pane = findByClass(nodes, 'hc-pane').find((n) =>
    flatten(n).some((x) => classesOf(x).split(/\s+/).includes(anchorClass))
  )
  return pane ? flatten(pane) : []
}

// ------------------------------------------------------------ 天气桩数据（C4）
/**
 * 天气桩：形状**照抄** `dashboard/api_weather.py` 的真实响应（含 source/source_label/
 * fetched_at/sources/units）。故意留两个坑验证"拿不到就显示 —"：
 *   - 逐小时第 1 条 pop 是 null（彩云不保证有降水概率）；
 *   - 多日第 2 条没有 pop 键（和风 7d 只有降水量，没有概率）。
 */
function weatherFixture(pathname) {
  const units = { temp: '℃', humidity: '%', wind: 'km/h', pm25: 'μg/m³', pop: '%', aqi: '' }
  const sources = [
    { source: 'caiyun', label: '彩云天气', calls: 3, ok: true },
    { source: 'qweather', label: '和风天气', calls: 3, ok: false },
    { source: 'open-meteo', label: 'Open-Meteo', calls: 1, ok: true }
  ]
  if (pathname === '/weather/now') {
    return {
      ok: true,
      place: '家',
      place_resolved: { name: '家', resolved: true, lat: 31.721276, lng: 117.149643, qweather_id: '101220104' },
      scope: { kind: 'global', agent: 'code-worker', note: '天气是全局数据' },
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
      warnings: ['大风蓝色预警'],
      air: { pm25: 14, pm25_unit: 'μg/m³', aqi_chn: 39, aqi_level: '优', source: 'caiyun' },
      partial_errors: []
    }
  }
  if (pathname === '/weather/hourly') {
    const hourly = []
    for (let i = 0; i < 24; i += 1) {
      const hh = String((13 + i) % 24).padStart(2, '0')
      hourly.push({
        dt: '2026-10-28T' + hh + ':00+08:00',
        temp: 18.8 - i * 0.3,
        desc: i % 3 === 0 ? '☁️阴' : '⛅多云',
        pop: i === 0 ? null : 10 + i
      })
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
      const row = {
        date: '2026-10-' + String(28 + i).padStart(2, '0'),
        text: i % 2 === 0 ? '阵雨' : '多云',
        min: 12.4 + i * 0.2,
        max: 19.1 + i * 0.3
      }
      if (i === 0) row.pop = 60
      if (i > 1) row.pop = 20 + i
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

// ------------------------------------------------------------ 概览分区桩数据
// C3 起概览分区真的读 5 个路由，桩数据按后端的真实响应形状给
// （含 scope / calibers / counts，缺字段就真的会渲染成「—」）。
function overviewFixture(pathname) {
  if (pathname === '/overview/system') {
    return {
      ok: true,
      scope: { kind: 'host', agent: 'code-worker', note: '宿主级指标' },
      source: 'hermes_cli.web_routers.status.get_system_stats',
      calibers: { 'memory.percent': 'psutil.virtual_memory().percent' },
      data: {
        os: 'Windows',
        cpu_count: 16,
        cpu_percent: 12.5,
        uptime_seconds: 300000,
        memory: { total: 34359738368, used: 17179869184, percent: 50 },
        disk: { percent: 61 },
        process: { rss: 104857600 },
        hermes_version: '0.22.5'
      }
    }
  }
  if (pathname === '/overview/analytics') {
    return {
      ok: true,
      scope: { kind: 'profile', agent: 'code-worker', note: '按 agent 取数' },
      source: 'hermes_cli.web_routers.analytics.get_usage_analytics',
      calibers: { 'totals.total_input': 'sessions 表按 started_at 归属' },
      data: {
        period_days: 30,
        totals: {
          total_input: 1000000,
          total_output: 200000,
          total_cache_read: 50000000,
          total_api_calls: 1234,
          total_sessions: 42,
          total_estimated_cost: 0.5
        }
      }
    }
  }
  if (pathname === '/overview/cron') {
    return {
      ok: true,
      scope: { kind: 'profile', agent: 'code-worker', note: '按 agent 取数' },
      source: 'hermes_cli.web_routers.cron.list_cron_jobs',
      calibers: { 'counts.enabled': 'enabled 且未暂停' },
      counts: { total: 3, enabled: 2, paused: 1 },
      data: [
        { id: 'j1', name: '每小时巡检', enabled: true, next_run_at: '2026-10-28T14:00:00+08:00', profile: 'code-worker' },
        { id: 'j2', name: '每日备份', enabled: true, next_run_at: '2026-10-29T03:00:00+08:00', profile: 'code-worker' },
        {
          id: 'j3',
          name: '已暂停任务',
          enabled: true,
          paused_at: '2026-10-27T10:00:00+08:00',
          profile: 'code-worker'
        }
      ]
    }
  }
  if (pathname === '/overview/layout') {
    return {
      ok: true,
      scope: { kind: 'shared_file', agent: 'code-worker', note: '单一数据文件' },
      source: 'layout:home-dashboard/layout.json',
      data: {
        path: 'D:/hermes-data/hermes/plugins/home-dashboard/layout.json',
        exists: true,
        mtime: 1789755473,
        widget_count: 2,
        layout: {
          version: 1,
          widgets: [
            { id: 'clock', gx: 0, gy: 0, gw: 6, gh: 2 },
            { id: 'cron', gx: 6, gy: 0, gw: 6, gh: 2 }
          ]
        }
      }
    }
  }
  if (pathname === '/overview/sessions') {
    const rows = []
    for (let i = 0; i < 20; i += 1) {
      rows.push({
        id: 'sess-' + i,
        title: '会话 ' + i,
        message_count: 10 + i,
        last_active: Math.floor(Date.now() / 1000) - 300 * (i + 1),
        profile: 'code-worker'
      })
    }
    return {
      ok: true,
      scope: { kind: 'profile', agent: 'code-worker', note: '按 agent 取数' },
      source: 'hermes_cli.web_routers.sessions.get_sessions',
      calibers: { total: '不含子会话' },
      data: { sessions: rows, total: 42, limit: 20, offset: 0, storage: {} }
    }
  }
  return null
}

// ------------------------------------------------------------ 路由桩
/**
 * M4a：`/modules` 的默认桩 —— 十个模块**全启用**，字段照抄各模块目录下的 manifest
 * （id / label / nav_order / section）。
 * 全启用 ⇒ 正常路径下导航仍是 10 个 tab（V3 起含 vcc「上下文引擎」，V4 起含 remote「远端后端」，
 * V5 起含 models「模型」），
 * 原有断言（导航 ≥5 项）不受影响；具体的「只启用 3 个 / 全关 / 缺组件 / 拉取失败」由各用例显式给 `options.modules`。
 */
const DEFAULT_MODULES = [
  { id: 'overview', label: '概览', nav_order: 10, section: 'overview', enabled: true, reason: '' },
  { id: 'weather', label: '天气', nav_order: 20, section: 'weather', enabled: true, reason: '' },
  { id: 'models', label: '模型', nav_order: 25, section: 'models', enabled: true, reason: '' },
  { id: 'agents', label: 'Agent 总览', nav_order: 30, section: 'agents', enabled: true, reason: '' },
  { id: 'autogroup', label: '项目分组', nav_order: 40, section: 'autogroup', enabled: true, reason: '' },
  { id: 'autotitler', label: '自动命名', nav_order: 45, section: 'autotitler', enabled: true, reason: '' },
  { id: 'advisor', label: 'Advisor', nav_order: 50, section: 'advisor', enabled: true, reason: '' },
  { id: 'upgrade', label: '升级', nav_order: 60, section: 'upgrade', enabled: true, reason: '' },
  { id: 'settings', label: '设置', nav_order: 70, section: 'settings', enabled: true, reason: '' },
  { id: 'vcc', label: '上下文引擎', nav_order: 80, section: 'vcc', enabled: true, reason: '' },
  { id: 'remote', label: '远端后端', nav_order: 90, section: 'remote', enabled: true, reason: '' }
]

function makeCtx(options) {
  const calls = []
  const writes = []
  const store = Object.assign({}, options.storage || {})
  const ctx = {
    rest(path, opts) {
      calls.push({ path, method: (opts && opts.method) || 'GET' })
      // M4a：模块清单。`options.modules` 可以是数组（所有 agent 同一份），
      // 也可以是 (agent) => 数组（验证切作用域后重排）；`failModules` 让这一路 reject；
      // `modulesShape: 'bad'` 伪造「200 但没有 modules 数组」的响应。
      if (path.indexOf('/modules') === 0) {
        if (options.failModules) {
          return Promise.reject(new Error(options.failModules))
        }
        const agentMatch = /[?&]agent=([^&]*)/.exec(path)
        const agent = agentMatch ? decodeURIComponent(agentMatch[1]) : 'default'
        if (options.modulesShape === 'bad') {
          return Promise.resolve({ ok: true, data: { agent } })
        }
        const list = typeof options.modules === 'function' ? options.modules(agent) : options.modules
        return Promise.resolve({
          ok: true,
          data: { agent, modules: list || DEFAULT_MODULES, health: { loaded: [], failed: {} } }
        })
      }
      if (options.failAgents && path.indexOf('/agents') === 0) {
        return Promise.reject(new Error('503: {"detail":"backend down"}'))
      }
      if (path.indexOf('/agents') === 0) {
        return Promise.resolve({
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
        })
      }
      const pathname = path.split('?')[0]
      if (path.indexOf('/weather') === 0) {
        if (options.failWeather && options.failWeather.indexOf(pathname) !== -1) {
          return Promise.reject(new Error('503: {"detail":"weather source down"}'))
        }
        const fixture = weatherFixture(pathname)
        if (fixture) return Promise.resolve(fixture)
      }
      if (path.indexOf('/overview') === 0) {
        if (options.failOverviewTimeout && options.failOverviewTimeout.indexOf(pathname) !== -1) {
          // 复刻宿主 fetch 超时的原始文案（rest.js 会把它归成 code=timeout）
          return Promise.reject(new Error('Timed out connecting to Hermes backend'))
        }
        if (options.failOverview && options.failOverview.indexOf(pathname) !== -1) {
          return Promise.reject(new Error('503: {"detail":"overview source down"}'))
        }
        const fixture = overviewFixture(pathname)
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
        writes.push({ key, value })
      },
      remove(key) {
        delete store[key]
      }
    },
    registerMany(list) {
      ctx.contributions.push(...list)
    },
    contributions: []
  }
  return { ctx, calls, writes, store }
}

async function mount(entryFile, options) {
  // 每个用例一套干净的组件槽位 + 待跑副作用（否则上一轮的 useState 会被复用）
  comps.clear()
  pendingEffects.length = 0
  copyFileSync(ENTRY, join(STUB, entryFile))
  const mod = await import(pathToFileURL(join(STUB, entryFile)).href + '?v=' + Date.now())
  const plugin = mod.default
  const harness = makeCtx(options || {})
  plugin.register(harness.ctx)
  const route = harness.ctx.contributions.find((c) => c.area === 'routes')
  if (!route) throw new Error('没有 routes 贡献')

  const state = { last: null }
  /**
   * 展开一次组件树，然后跑本轮排队的副作用。
   * `settleMs` 是**展开前**的等待：上一轮副作用发出的 Promise 需要时间落地，
   * 先等再展开才能看到它们的结果（React 里由 re-render 完成，这里手动做）。
   */
  async function render(settleMs) {
    if (settleMs) await new Promise((r) => setTimeout(r, settleMs))
    const tree = expand(route.render())
    const nodes = flatten(tree)
    const text = textOf(tree)
    state.last = { tree, nodes, text }
    const queued = pendingEffects.splice(0)
    queued.forEach((fn) => fn())
    return state.last
  }
  return { plugin, harness, route, render }
}

// ═══════════════════════════════════════════════ 用例 A：正常路径
console.log('\n[A] 正常路径（9 agent + 三个分区）')
const A = await mount('index_a.js', {})
check('routes 贡献指向 /console', A.route.data && A.route.data.path === '/console')

// a1：挂载那一帧 —— 还没任何数据，但外壳必须已经在
const a1 = await A.render(0)
check('首次渲染就有外壳（不白屏）', classesOf(a1.nodes[0]).split(/\s+/).includes('hc-root'))
// C7 加了「Agent 总览」，导航从 3 项变 4 项：断言标签集合而不是死数字，
// 再加一个分区就不用改这里（数字只作长度下限）。
const navLabels1 = findByClass(a1.nodes, 'hc-nav__item').map((n) => textOf(n).trim())
check(
  '导航含概览/天气/升级/Agent 总览/项目分组五个分区',
  navLabels1.length >= 5 && ['概览', '天气', '升级', 'Agent 总览', '项目分组'].every((want) => navLabels1.some((t) => t.indexOf(want) !== -1)),
  'nav items=' + navLabels1.length + ' ' + JSON.stringify(navLabels1)
)
check('加载中只有骨架屏（概览面板）', findByClass(a1.nodes, 'hc-skel').length >= 1)
check('加载中不出现「加载中…」字样', !/加载中…/.test(a1.text))
check('顶栏有作用域选择器', findByClass(a1.nodes, 'hc-select').length === 1)

// a2：/agents 落地
const a2 = await A.render(40)
const select2 = findByClass(a2.nodes, 'hc-select')[0]
check('选择器弹出 9 个 agent', select2.children.filter((n) => n && n.type === 'option').length === 9)
check(
  '默认作用域 = 后端 current（code-worker）',
  select2.props.value === 'code-worker',
  'value=' + select2.props.value
)
check(
  '作用域已持久化到 storage（scope.agent）',
  A.harness.store['scope.agent'] === 'code-worker',
  JSON.stringify(A.harness.store)
)
check('这一步概览还在骨架屏（作用域刚确定，请求刚发出）', findByClass(a2.nodes, 'hc-skel').length >= 1)

// a2b：概览数据落地（C3：真实统计卡 + 只读列表）
const a3 = await A.render(40)
check(
  '概览渲染 4 个统计卡（系统 / token / cron / 会话）',
  findByClass(a3.nodes, 'hc-card').length === 4,
  'cards=' + findByClass(a3.nodes, 'hc-card').length
)
check(
  '每个统计卡都标了口径',
  findByClass(a3.nodes, 'hc-card__caliber').length === 4 && /口径：/.test(a3.text)
)
check(
  '系统卡用宿主内存读数（50%）+ 精确值',
  /50%/.test(a3.text) && /已用 16 GB \/ 共 32 GB/.test(a3.text),
  a3.text.slice(0, 160)
)
check(
  'token 卡把缓存读入单列（不并入总量）',
  /缓存读入 50,000,000（单列，未并入）/.test(a3.text)
)
check('cron 卡数字 = counts（2 / 3 启用）', /2 \/ 3 启用/.test(a3.text), a3.text.slice(0, 200))
check('会话卡数字 = total（42 个）', /42/.test(a3.text) && /最近活动/.test(a3.text))
check('看板布局只读渲染 2 个挂件', findByClass(a3.nodes, 'hc-widget').length === 2)
check(
  'cron 3 行 + 会话 20 行 = 23 行',
  findByClass(a3.nodes, 'hc-row').length === 23,
  'rows=' + findByClass(a3.nodes, 'hc-row').length
)
check('会话分页器显示区间与总数', /第 1–20 条 \/ 共 42 条/.test(a3.text))
check('概览不再显示「还没接上真实数据」占位', !/还没接上真实数据/.test(a3.text))
const activeNav = findByClass(a3.nodes, 'hc-nav__item--active')
check('选中态类只在概览一项上', activeNav.length === 1 && /概览/.test(textOf(activeNav[0])))

// 点击「天气」
const weatherBtn = findByClass(a3.nodes, 'hc-nav__item').find((n) => /天气/.test(textOf(n)))
check('导航按钮有 onClick 回调', Boolean(weatherBtn && typeof weatherBtn.props.onClick === 'function'))
weatherBtn.props.onClick()
const a4 = await A.render(40)
// t_a67a2bc2：切分区改为「只挂载当前分区」，切走即卸载（旧行为是所有访问过的面板都留着、
// 只切 visibility，DOM 从 574 涨到 9816）。滚动位置不再跨切换保留，这是本卡明示接受的取舍。
check('切到天气后只剩 1 个面板（旧面板已卸载）', findByClass(a4.nodes, 'hc-pane').length === 1,
  String(findByClass(a4.nodes, 'hc-pane').length))
check(
  '剩下那个面板就是天气（标题跟着换）',
  findByClass(a4.nodes, 'hc-pane__title').map(textOf).join('|') === '天气',
  findByClass(a4.nodes, 'hc-pane__title').map(textOf).join('|')
)
check(
  '卸载后的面板不带 idle / aria-hidden（DOM 里没有隐藏的旧分区）',
  findByClass(a4.nodes, 'hc-pane--idle').length === 0 &&
    findByClass(a4.nodes, 'hc-pane').filter((n) => n.props['aria-hidden'] === 'true').length === 0
)
check('页面记忆（nav.section）写进去了', A.harness.store['nav.section'] === 'weather')

// ---- C4：天气分区（当前卡 / 逐小时 / 多日 / N 小时后 / 城市输入）
// 切到天气那一刻三个查询才刚发出（骨架屏），再多跑几轮副作用让数据落地。
const a4w = await A.render(80)
const wxTemp = findByClass(a4w.nodes, 'hc-wx-now__temp').map(textOf).join('')
check('当前卡温度照抄后端（18.83 → 18.8℃）', wxTemp === '18.8℃', wxTemp)
check(
  '当前卡天气现象照抄工具输出（不合并、不翻译）',
  /阴/.test(findByClass(a4w.nodes, 'hc-wx-now__desc').map(textOf).join('')),
  findByClass(a4w.nodes, 'hc-wx-now__desc').map(textOf).join('')
)
const wxMetricText = findByClass(a4w.nodes, 'hc-wx-metric').map(textOf).join(' | ')
check('指标带单位：体感 ℃ / 湿度 % / 风速 km/h / PM2.5 μg/m³',
  /16\.7℃/.test(wxMetricText) && /46%/.test(wxMetricText) && /44km\/h/.test(wxMetricText) && /14μg\/m³/.test(wxMetricText),
  wxMetricText.slice(0, 200))
check('PM2.5 同时给出 AQI 与等级（不把 AQI 冒充 PM2.5）', /AQI 39（优）/.test(wxMetricText), wxMetricText.slice(0, 200))
const stampText = findByClass(a4w.nodes, 'hc-wx-stamp').map(textOf).join(' ')
check('来源 + 更新时间都渲染了', /来源 彩云天气/.test(stampText) && /更新 \d\d-\d\d \d\d:\d\d（/.test(stampText),
  stampText.slice(0, 160))
check('降级过程可见（和风 ×空 / 彩云 ✓）', /和风天气 ×空/.test(stampText) && /彩云天气 ✓/.test(stampText),
  stampText.slice(0, 160))
check('逐小时 24 项（换行铺满，不再横向滚动）', findByClass(a4w.nodes, 'hc-wx-hour').length === 24 &&
  findByClass(a4w.nodes, 'hc-wx-hours').length === 1,
  'hours=' + findByClass(a4w.nodes, 'hc-wx-hour').length)
check('逐小时缺降水概率的那一格显示「降水 —」', /降水 —/.test(a4w.text))
check('多日 7 行', findByClass(a4w.nodes, 'hc-wx-day').length === 7,
  'days=' + findByClass(a4w.nodes, 'hc-wx-day').length)
check('多日行有日期/周几、现象、最高最低（带 ℃）', /10-28/.test(a4w.text) && /周/.test(a4w.text) && /~ 19\.1℃/.test(a4w.text),
  a4w.text.slice(a4w.text.indexOf('多日'), a4w.text.indexOf('多日') + 160))
check('「N 小时后」内联显示后端原话', /6小时后\(19时\): 15°C 多云/.test(a4w.text))
// 分段控件按面板限定：概览的 token 窗口（7/30/90 天）复用同一套 .hc-wx-seg__item。
const wxNodes = subtreeOf(a4w.nodes, 'hc-wx-now')
check('定位到天气分区面板（.hc-pane 内含 .hc-wx-now）', wxNodes.length > 0, 'nodes=' + wxNodes.length)
check('「N 小时后」5 个选项（1/6/12/24/48h）', findByClass(wxNodes, 'hc-wx-seg__item').length === 5,
  'opts=' + findByClass(wxNodes, 'hc-wx-seg__item').length + ' ' +
    JSON.stringify(findByClass(wxNodes, 'hc-wx-seg__item').map(textOf)))
check('城市输入框 + 查询/复位按钮都在', findByClass(a4w.nodes, 'hc-wx-input').length === 1 &&
  findByClass(a4w.nodes, 'hc-btn').filter((n) => /查询|复位/.test(textOf(n))).length === 2)

// 点 24h：重新请求且带 hours=24
const seg24 = findByClass(wxNodes, 'hc-wx-seg__item').find((n) => /24h/.test(textOf(n)))
check('24h 选项有 onClick', Boolean(seg24 && typeof seg24.props.onClick === 'function'))
seg24.props.onClick()
const a4b = await A.render(40)
const atCall = A.harness.calls.filter((c) => c.path.indexOf('/weather/at') === 0).pop()
check('切到 24h 后重取 /weather/at 且 hours=24', /hours=24/.test(atCall.path), atCall.path)
const wxNodesB = subtreeOf(a4b.nodes, 'hc-wx-now')
check('24h 选中态跟着换', findByClass(wxNodesB, 'hc-wx-seg__item--on').map(textOf).join('') === '24h',
  findByClass(wxNodesB, 'hc-wx-seg__item--on').map(textOf).join(''))

// 输入城市 → 查询：请求带 place，且 key 变化触发重取
const wxInput = findByClass(a4b.nodes, 'hc-wx-input')[0]
wxInput.props.onChange({ target: { value: '北京' } })
const a4c = await A.render(2)
const applyBtn = findByClass(a4c.nodes, 'hc-btn').find((n) => textOf(n) === '查询')
check('查询按钮存在且可点', Boolean(applyBtn && typeof applyBtn.props.onClick === 'function'))
applyBtn.props.onClick()
const a4d = await A.render(40)
const nowCall = A.harness.calls.filter((c) => c.path.indexOf('/weather/now') === 0).pop()
check('按城市查询后请求带 place=北京（不写死肥西）', /place=/.test(nowCall.path) && /%E5%8C%97%E4%BA%AC/.test(nowCall.path),
  nowCall.path)
check('输入框里保留用户输入', findByClass(a4d.nodes, 'hc-wx-input')[0].props.value === '北京')

// 切回概览：不应重新请求 /overview
const overviewCallsBefore = A.harness.calls.filter((c) => c.path.indexOf('/overview') === 0).length
const overviewBtn = findByClass(a4.nodes, 'hc-nav__item').find((n) => /概览/.test(textOf(n)))
overviewBtn.props.onClick()
const a5 = await A.render(40)
const overviewCallsAfter = A.harness.calls.filter((c) => c.path.indexOf('/overview') === 0).length
check(
  '切回概览没有重新拉数据（缓存命中）',
  overviewCallsBefore === overviewCallsAfter && overviewCallsBefore > 0,
  'before=' + overviewCallsBefore + ' after=' + overviewCallsAfter
)
// 「概览一次取齐」的口径：端点清单写死（新增数据源时这里必须同步改），
// 而不是写死调用次数——次数会因为 analytics 换窗口、重试等正常原因变化。
const overviewPaths = Array.from(
  new Set(
    A.harness.calls
      .filter((c) => c.path.indexOf('/overview') === 0)
      .map((c) => c.path.split('?')[0])
  )
).sort()
check(
  '概览取齐 8 个端点（自检 + layout/system/analytics/cron/sessions/status/logs）',
  JSON.stringify(overviewPaths) ===
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
  JSON.stringify(overviewPaths)
)
check('切回后概览也不是 idle（只剩当前面板，没有隐藏的残骸）',
  findByClass(a5.nodes, 'hc-pane--idle').length === 0 &&
    findByClass(a5.nodes, 'hc-pane__title').map(textOf).join('|') === '概览',
  findByClass(a5.nodes, 'hc-pane__title').map(textOf).join('|'))

// 切作用域：只有受影响的面板回骨架屏
const select5 = findByClass(a5.nodes, 'hc-select')[0]
select5.props.onChange({ target: { value: 'xiaomo' } })
const a6 = await A.render(0)
check('切作用域立刻写 storage', A.harness.store['scope.agent'] === 'xiaomo', JSON.stringify(A.harness.store))
check('受影响面板回骨架屏', findByClass(a6.nodes, 'hc-skel').length >= 1)
check('顶栏/导航不闪（仍在，且标题还是概览）', findByClass(a6.nodes, 'hc-nav__item').length >= 5 && /概览/.test(a6.text))
check('顶部连接状态仍是已连接', findByClass(a6.nodes, 'hc-conn--ok').length === 1)
const a7 = await A.render(40)
const scopedCall = A.harness.calls.filter((c) => c.path.indexOf('/overview') === 0).pop()
check('新作用域的请求带上 agent=xiaomo', /agent=xiaomo/.test(scopedCall.path), scopedCall.path)
check(
  '新作用域数据到位后统计卡仍齐全（4 张，且不是占位）',
  findByClass(a7.nodes, 'hc-card').length === 4 && !/还没接上真实数据/.test(a7.text),
  'cards=' + findByClass(a7.nodes, 'hc-card').length
)
const scopedChip = findByClass(a7.nodes, 'hc-chip').map(textOf).join(' | ')
check('内容区显示了当前作用域（切换可见）', /作用域 xiaomo/.test(scopedChip), scopedChip)

// ═══════════════════════════════════════════════ 用例 B：/agents 失败
console.log('\n[B] /agents 失败（503）—— 退化但不能白屏')
const B = await mount('index_b.js', { failAgents: true })
const b1 = await B.render(0)
check('外壳照样渲染出根节点', classesOf(b1.nodes[0]).split(/\s+/).includes('hc-root'))
check('导航五个分区仍在（不白屏、不空壳）', findByClass(b1.nodes, 'hc-nav__item').length >= 5)
const b2 = await B.render(60)
const selectB = findByClass(b2.nodes, 'hc-select')[0]
check('选择器退化成 1 个选项', selectB.children.filter((n) => n && n.type === 'option').length === 1)
check('退化项文案说明是当前作用域', /当前/.test(textOf(selectB.children[0])), JSON.stringify(textOf(selectB.children[0])))
check('有一行原因（含后端原话 503）', /agent 列表没读到/.test(b2.text) && /503/.test(b2.text), b2.text.slice(0, 220))
check('连接状态点变为失败', findByClass(b2.nodes, 'hc-conn--error').length === 1)
const b3 = await B.render(60)
check(
  '分区内容照常渲染（统计卡在，不是错误页）',
  findByClass(b3.nodes, 'hc-card').length === 4 && findByClass(b3.nodes, 'hc-state--error').length === 0,
  'cards=' + findByClass(b3.nodes, 'hc-card').length
)
const refreshOnly = findByClass(b3.nodes, 'hc-btn').find((n) => /刷新/.test(textOf(n)))
check('刷新按钮可用（没有被禁用）', Boolean(refreshOnly) && !refreshOnly.props.disabled)

// 刷新反馈：点了要看得出发生了事，但不能变成转圈/锁按钮
const refreshBtn = findByClass(b3.nodes, 'hc-btn').find((n) => /刷新/.test(textOf(n)))
refreshBtn.props.onClick()
const b4 = await B.render(0)
const refreshBtn2 = findByClass(b4.nodes, 'hc-btn').find((n) => /刷新|已刷新/.test(textOf(n)))
check(
  '点刷新后有可见反馈（文字变「已刷新」）',
  /已刷新/.test(textOf(refreshBtn2)) && classesOf(refreshBtn2).split(/\s+/).includes('hc-btn--done'),
  textOf(refreshBtn2)
)
check('刷新后按钮仍可点（没有进 loading 锁死）', !refreshBtn2.props.disabled)

// ═══════════════════════════════════════════════ 用例 C：失效的 nav.section
console.log('\n[C] 存过的 nav.section 已失效（分区被删）—— 不能变成空面板')
const C = await mount('index_c.js', { storage: { 'nav.section': 'gone-section', 'scope.agent': 'work' } })
const c1 = await C.render(0)
check('外壳仍然渲染', classesOf(c1.nodes[0]).split(/\s+/).includes('hc-root'))
check('内容区有面板（不是空白容器）', findByClass(c1.nodes, 'hc-pane').length === 1)
check('回落到第一个分区（概览）', /概览/.test(c1.text) && findByClass(c1.nodes, 'hc-nav__item--active').length === 1)
check('上次存的作用域被沿用（work）', C.harness.store['scope.agent'] === 'work', JSON.stringify(C.harness.store))
const c2 = await C.render(40)
check('作用域沿用后分区进入骨架屏（不是空白）', findByClass(c2.nodes, 'hc-skel').length >= 1)
const c3 = await C.render(40)
check(
  '失效分区未阻断数据加载',
  findByClass(c3.nodes, 'hc-card').length === 4,
  'cards=' + findByClass(c3.nodes, 'hc-card').length
)

// ═══════════════════════════════════════════════ 用例 E：概览单个数据源坏掉
console.log('\n[E] 概览只有 cron 这一路 503 —— 其它卡照常，坏的那张显示「—」+ 原因')
const E = await mount('index_e.js', { failOverview: ['/overview/cron'] })
await E.render(0)
await E.render(40)
const e1 = await E.render(40)
check('其它三个统计卡仍然有数字', findByClass(e1.nodes, 'hc-card').length === 4)
check(
  'cron 卡进错误态：标签 + 原因（不是 0，不是空白）',
  findByClass(e1.nodes, 'hc-card').some((n) => {
    const text = textOf(n)
    return /cron 任务数/.test(text) && /没读到/.test(text)
  }),
  e1.text.slice(0, 240)
)
// 统计卡内的错误态 + 区块内的错误态：两处都要说清原因（C6 起统计卡错误改走 ErrorState）
check('cron 卡与 cron 区块都进错误态（各一处）', findByClass(e1.nodes, 'hc-state--error').length === 2,
  'errors=' + findByClass(e1.nodes, 'hc-state--error').length)
check('错误态保留后端原话（503）', /503/.test(e1.text))
check('系统卡没被连累（仍是 50%）', /50%/.test(e1.text))
check(
  '会话列表照常渲染 20 行',
  findByClass(e1.nodes, 'hc-row').length === 20,
  'rows=' + findByClass(e1.nodes, 'hc-row').length
)

// ═══════════════════════════════════════════════ 用例 F：天气单个路由坏掉（C4）
console.log('\n[F] 天气实况路 503 —— 当前卡进错误态，逐时/逐日照常')
const F = await mount('index_f.js', { failWeather: ['/weather/now'] })
const f0 = await F.render(0)
const fWeatherBtn = findByClass(f0.nodes, 'hc-nav__item').find((n) => /天气/.test(textOf(n)))
fWeatherBtn.props.onClick()
await F.render(40)
const f1 = await F.render(80)
check('当前卡进错误态（不是白屏、不是 0）', findByClass(f1.nodes, 'hc-state--error').length === 1,
  'errors=' + findByClass(f1.nodes, 'hc-state--error').length)
check('错误态保留后端原话（503）', /503/.test(f1.text))
check('逐小时照常 24 项（实况坏了不连累）', findByClass(f1.nodes, 'hc-wx-hour').length === 24,
  'hours=' + findByClass(f1.nodes, 'hc-wx-hour').length)
check('多日照常 7 行', findByClass(f1.nodes, 'hc-wx-day').length === 7,
  'days=' + findByClass(f1.nodes, 'hc-wx-day').length)
check('「N 小时后」照常显示', /6小时后\(19时\): 15°C 多云/.test(f1.text))

// ═══════════════════════════════════════════════ 用例 G：analytics 慢/失败（C6 验收第 3 条）
// analytics 实测 2.4–4.2s。这段时间面板必须是**骨架屏**（不是「—」+「读取中」三个字），
// 超时必须落到 ErrorState 并说清原因，不能一直挂在加载态。
console.log('\n[G] token/调用量：加载中 = 骨架屏；超时 = ErrorState + 原因')
const G = await mount('index_g.js', {})
const g0 = await G.render(0)
const g0Skels = findByClass(g0.nodes, 'hc-skel-card')
check('加载中的统计卡是骨架屏（4 张 .hc-skel-card）', g0Skels.length === 4, 'skel cards=' + g0Skels.length)
check('骨架屏带 aria-busy（读屏知道在加载）', g0Skels.every((n) => n.props['aria-busy'] === 'true'))
check('加载中不出现「读取中」三个字', !/读取中/.test(g0.text), g0.text.slice(0, 160))
check('加载中不出现「—」冒充读数（取不到就说取不到，不是先给破折号）',
  !findByClass(g0.nodes, 'hc-card__num').length)

const Gt = await mount('index_gt.js', { failOverviewTimeout: ['/overview/analytics'] })
await Gt.render(0)
await Gt.render(40)
const gt = await Gt.render(40)
const timeoutCard = findByClass(gt.nodes, 'hc-card').find((n) => /token \/ 调用量/.test(textOf(n)))
check('analytics 超时后 token 卡进 ErrorState（不是一直挂骨架屏）',
  findByClass(gt.nodes, 'hc-card--error').some((n) => /token \/ 调用量/.test(textOf(n))) &&
    findByClass(gt.nodes, 'hc-state--error').length >= 1,
  'error cards=' + findByClass(gt.nodes, 'hc-card--error').length)
check('超时原因写清「取数超时」，不是「连不上后端」', /取数超时/.test(textOf(timeoutCard || {props:{}})), textOf(timeoutCard || {props:{}}).slice(0, 200))
check('超时卡仍留在统计卡栅格里（布局不被顶歪）', findByClass(gt.nodes, 'hc-card').length === 4)
check('其它三张卡照常有数字', /50%/.test(gt.text) && findByClass(gt.nodes, 'hc-skel-card').length === 0)

// ═══════════════════════════════════════════════ 用例 M：M4a 导航动态化（/modules）
// 导航不再硬编码：只渲染 enabled===true、按 nav_order 升序、组件缺失给占位页、
// 拉不到就回退 FALLBACK_SECTIONS 并贴后端原话。这一组全部用独立模块实例（独立缓存）。
console.log('\n[M] M4a 导航动态化（/modules → tab）')

/** 多跑几轮 render 让 effect 里发出的 Promise 落地（迷你运行时不会自动 re-render）。 */
async function settle(probe, rounds) {
  let last = null
  for (let i = 0; i < (rounds || 3); i += 1) last = await probe.render(40)
  return last
}

/** 只启用前 n 个模块（其余 enabled:false），并**故意打乱数组顺序**验证按 nav_order 排序。 */
function enabledSubset(ids) {
  const order = ['overview', 'weather', 'agents', 'autogroup', 'advisor', 'upgrade']
  return order
    .map((id) => DEFAULT_MODULES.find((m) => m.id === id))
    .filter((m) => m)
    .map((m) =>
      Object.assign({}, m, {
        enabled: ids.indexOf(m.id) !== -1,
        reason: ids.indexOf(m.id) !== -1 ? '' : '被 agent 设置关掉'
      })
    )
    .reverse() // 后端顺序无所谓，前端必须按 nav_order 自己排
}

// M1：只启用 3 个（overview/weather/agents），乱序返回 —— 导航必须只有 3 项且按 nav_order
const M1 = await mount('index_m1.js', { modules: enabledSubset(['overview', 'weather', 'agents']) })
const m1Loading = await M1.render(0)
const m1 = await settle(M1)
const m1Labels = findByClass(m1.nodes, 'hc-nav__item').map((n) => textOf(n).trim())
check(
  '/modules 只启用 3 个 ⇒ 导航恰好 3 个 tab',
  m1Labels.length === 3,
  'nav=' + m1Labels.length + ' ' + JSON.stringify(m1Labels)
)
check(
  '导航按 nav_order（10/20/30）升序，不跟后端数组顺序',
  JSON.stringify(m1Labels) === JSON.stringify(['概览', '天气', 'Agent 总览']),
  JSON.stringify(m1Labels)
)
const m1Call = M1.harness.calls.filter((c) => c.path.indexOf('/modules') === 0).pop()
check('模块清单请求带当前作用域 agent', Boolean(m1Call) && /agent=code-worker/.test(m1Call.path), m1Call && m1Call.path)
check('只启用 3 个时不出现未启用的分区（项目分组 / Advisor / 升级）',
  !/项目分组/.test(m1.text) && !/Advisor/.test(m1.text) && !/升级/.test(m1.text),
  m1Labels.join(','))
check('正常路径下没有回退提示', findByClass(m1.nodes, 'hc-notice').length === 0)
check('加载中导航是骨架条（不闪空白）', findByClass(m1Loading.nodes, 'hc-nav__skel').length === 1,
  'skel=' + findByClass(m1Loading.nodes, 'hc-nav__skel').length)
check('加载中导航仍有项（不把面板搞空）', findByClass(m1Loading.nodes, 'hc-nav__item').length >= 1,
  'items=' + findByClass(m1Loading.nodes, 'hc-nav__item').length)

// M2：全部关闭 —— 导航为空但必须有空态文案（不是白屏）
const M2 = await mount('index_m2.js', { modules: enabledSubset([]) })
await M2.render(0)
const m2 = await settle(M2)
check('全关时外壳照常渲染（不白屏）', classesOf(m2.nodes[0]).split(/\s+/).includes('hc-root'))
check('全关时导航一个 tab 都没有', findByClass(m2.nodes, 'hc-nav__item').length === 0,
  'items=' + findByClass(m2.nodes, 'hc-nav__item').length)
check('全关时导航区显示「没有已启用模块」（不是空白）',
  findByClass(m2.nodes, 'hc-nav__empty').length === 1 &&
    /没有已启用模块/.test(textOf(findByClass(m2.nodes, 'hc-nav__empty')[0])),
  'nav-empty=' + findByClass(m2.nodes, 'hc-nav__empty').map(textOf).join('|'))
check('全关时内容区给空态（不是空白容器）',
  findByClass(m2.nodes, 'hc-content').length === 1 &&
    findByClass(m2.nodes, 'hc-state').length >= 1 &&
    /没有已启用模块/.test(m2.text))
check('全关时不进错误态（这是配置结果，不是故障）', findByClass(m2.nodes, 'hc-state--error').length === 0)

// M3：后端启用了本地没打包的 id —— 渲染占位页，文案点名该 id，不许抛错/白屏
const M3 = await mount('index_m3.js', {
  modules: [{ id: 'nonexistent', label: '未来模块', nav_order: 10, section: 'nonexistent', enabled: true, reason: '' }]
})
await M3.render(0)
const m3 = await settle(M3)
check('本地没组件的 id 也照样出一个 tab（模块开关生效的证明）',
  findByClass(m3.nodes, 'hc-nav__item').length === 1,
  'items=' + findByClass(m3.nodes, 'hc-nav__item').length)
check('该 tab 用后端的 label', /未来模块/.test(m3.text))
check('面板位置渲染占位页（不是空面板 / 不是错误态）',
  findByClass(m3.nodes, 'hc-state').length === 1 &&
    findByClass(m3.nodes, 'hc-state--error').length === 0,
  'states=' + findByClass(m3.nodes, 'hc-state').length)
check('占位文案点名是哪个 id', /该模块已启用，但前端未打包对应分区（id=nonexistent）/.test(m3.text),
  m3.text.slice(0, 240))
check('占位页不说成「加载失败」也没有骨架屏', !/加载失败/.test(m3.text) && findByClass(m3.nodes, 'hc-skel').length === 0)

// M4：/modules 请求失败 —— 回退 FALLBACK_SECTIONS 全量导航 + 带后端原话的可关闭提示
const M4 = await mount('index_m4.js', { failModules: '503: {"detail":"registry down：manifest 扫描失败"}' })
await M4.render(0)
const m4 = await settle(M4)
const m4Labels = findByClass(m4.nodes, 'hc-nav__item').map((n) => textOf(n).trim())
check('拉取失败 ⇒ 回退为 11 项全量导航（不是 0 项）', m4Labels.length === 11,
  'nav=' + m4Labels.length + ' ' + JSON.stringify(m4Labels))
check('回退顺序 = manifest nav_order 顺序（与后端清单同序，首帧即稳定）',
  JSON.stringify(m4Labels) === JSON.stringify(['概览', '天气', '模型', 'Agent 总览', '项目分组', '自动命名', 'Advisor', '升级', '设置', '上下文引擎', '远端后端']),
  JSON.stringify(m4Labels))
check('显示「模块列表拉取失败，已回退到完整导航」', /模块列表拉取失败，已回退到完整导航/.test(m4.text),
  m4.text.slice(0, 200))
check('提示里原样展示后端 message（不是自己编原因）',
  /registry down：manifest 扫描失败/.test(m4.text) && /503/.test(m4.text),
  findByClass(m4.nodes, 'hc-notice').map(textOf).join(' | '))
check('提示在导航区（左栏）且带关闭按钮',
  findByClass(m4.nodes, 'hc-rail').some((n) => textOf(n).indexOf('已回退到完整导航') !== -1) &&
    findByClass(m4.nodes, 'hc-notice__close').length === 1)
const m4Close = findByClass(m4.nodes, 'hc-notice__close')[0]
m4Close.props.onClick()
const m4b = await M4.render(0)
check('提示可关闭（点了就消失）', findByClass(m4b.nodes, 'hc-notice').length === 0)
check('关闭提示后回退导航还在（11 项）', findByClass(m4b.nodes, 'hc-nav__item').length === 11)
check('回退路径的面板照常渲染（概览统计卡在）', findByClass(m4b.nodes, 'hc-card').length === 4,
  'cards=' + findByClass(m4b.nodes, 'hc-card').length)

// M4b：200 但结构不对（没有 modules 数组）也要回退，而不是当成「零个模块」把面板搞空
const M4c = await mount('index_m4c.js', { modulesShape: 'bad' })
await M4c.render(0)
const m4c = await settle(M4c)
check('结构不对 ⇒ 回退全量导航', findByClass(m4c.nodes, 'hc-nav__item').length === 11,
  'items=' + findByClass(m4c.nodes, 'hc-nav__item').length)
check('结构不对 ⇒ 提示说清是结构问题（不说成「零个模块」）',
  /结构不对/.test(m4c.text) && !/没有已启用模块/.test(m4c.text), m4c.text.slice(0, 200))

// M5：切 agent 后按新启用集重排（两次不同的 mock 响应）
const M5 = await mount('index_m5.js', {
  modules(agent) {
    if (agent === 'default') return enabledSubset(['agents', 'advisor'])
    return enabledSubset(['overview', 'weather'])
  }
})
await M5.render(0)
const m5a = await settle(M5)
const m5aLabels = findByClass(m5a.nodes, 'hc-nav__item').map((n) => textOf(n).trim())
check('code-worker 的启用集 = 概览 + 天气', JSON.stringify(m5aLabels) === JSON.stringify(['概览', '天气']),
  JSON.stringify(m5aLabels))
const m5Select = findByClass(m5a.nodes, 'hc-select')[0]
m5Select.props.onChange({ target: { value: 'default' } })
const m5b = await M5.render(0)
check('切作用域的瞬间导航不塌（保留上一份清单 + 骨架条）',
  findByClass(m5b.nodes, 'hc-nav__item').length === 2 &&
    findByClass(m5b.nodes, 'hc-nav__skel').length === 1,
  'items=' + findByClass(m5b.nodes, 'hc-nav__item').length + ' skel=' + findByClass(m5b.nodes, 'hc-nav__skel').length)
const m5c = await settle(M5)
const m5cLabels = findByClass(m5c.nodes, 'hc-nav__item').map((n) => textOf(n).trim())
check('切到 default 后导航按新启用集重排（Agent 总览 + Advisor）',
  JSON.stringify(m5cLabels) === JSON.stringify(['Agent 总览', 'Advisor']),
  JSON.stringify(m5cLabels))
const m5Call = M5.harness.calls.filter((c) => c.path.indexOf('/modules') === 0).pop()
check('切作用域后重新请求 /modules 且带 agent=default', /agent=default/.test(m5Call.path), m5Call.path)
check('重排后旧启用集的分区不残留在导航里', !/天气/.test(m5cLabels.join(',')) && !/概览/.test(m5cLabels.join(',')),
  JSON.stringify(m5cLabels))
// ignoresScope 语义不许改坏：manifest 里没这个字段，必须按**分区 id**从 FALLBACK_SECTIONS 继承。
// 当前激活的是 Agent 总览（一个 ignoresScope 模块），面板标题旁必须写「全部 agent」，不能写「作用域 x」。
const m5Chips = findByClass(m5c.nodes, 'hc-chip').map(textOf)
check('后端来的 ignoresScope 模块（Agent 总览）标题旁写「全部 agent」而不是「作用域」',
  m5Chips.indexOf('全部 agent') !== -1 && !m5Chips.some((t) => /^作用域 /.test(t)),
  JSON.stringify(m5Chips))

// 作用域无关性：同一批模块给两个 agent，概览的 chip 必须跟着作用域变，Agent 总览的不能变
const M7 = await mount('index_m7.js', { modules: enabledSubset(['overview', 'agents']) })
await M7.render(0)
const m7a = await settle(M7)
const m7aSelect = findByClass(m7a.nodes, 'hc-select')[0]
m7aSelect.props.onChange({ target: { value: 'work' } })
await M7.render(0)
const m7b = await settle(M7)
const m7Chips = findByClass(m7b.nodes, 'hc-chip').map(textOf)
check('切作用域后概览的 chip 跟着变（作用域 work）', m7Chips.indexOf('作用域 work') !== -1,
  JSON.stringify(m7Chips))
// 点开 Agent 总览（切走即卸载概览，所以这里只能断言「当前这一个面板」的 chip）。
// 它自己那一格必须是「全部 agent」——作用域变了也不跟着变。
const m7AgentsBtn = findByClass(m7b.nodes, 'hc-nav__item').find((n) => /Agent 总览/.test(textOf(n)))
m7AgentsBtn.props.onClick()
const m7c = await M7.render(0)
const m7Panes = findByClass(m7c.nodes, 'hc-pane').map((pane) => findByClass(flatten([pane]), 'hc-chip').map(textOf))
check('切到 Agent 总览后只剩 1 个面板（概览已卸载）', m7Panes.length === 1, JSON.stringify(m7Panes))
const agentsPaneChips = m7Panes.find((list) => list.indexOf('全部 agent') !== -1) || []
check('切作用域后 Agent 总览自己那一格仍是「全部 agent」（语义没被改坏）',
  agentsPaneChips.indexOf('全部 agent') !== -1 && !agentsPaneChips.some((t) => /^作用域 /.test(t)),
  JSON.stringify(m7Panes))
// 切回概览：作用域 chip 仍写「作用域 work」（缓存命中，不重拉；切走只卸载不丢数据）
const m7BackBtn = findByClass(m7c.nodes, 'hc-nav__item').find((n) => /概览/.test(textOf(n)))
m7BackBtn.props.onClick()
const m7d = await M7.render(0)
const m7dPanes = findByClass(m7d.nodes, 'hc-pane').map((pane) => findByClass(flatten([pane]), 'hc-chip').map(textOf))
check('切回概览只剩 1 个面板且仍写着「作用域 work」',
  m7dPanes.length === 1 && m7dPanes[0].indexOf('作用域 work') !== -1, JSON.stringify(m7dPanes))

// M6：零硬编码颜色复查（src/ 全量，与 build.mjs 的规则同一套）
function walkSrc(dir, out) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) walkSrc(full, out)
    else if (/\.(css|js)$/.test(entry.name)) out.push(full)
  }
  return out
}
const COLOR_RES = [/#[0-9a-fA-F]{3,8}\b/g, /\brgba?\s*\(/g, /\bhsla?\s*\(/g]
const colorHits = []
for (const file of walkSrc(join(REPO, 'src'), [])) {
  const text = readFileSync(file, 'utf8')
  for (const re of COLOR_RES) {
    re.lastIndex = 0
    let hit
    while ((hit = re.exec(text)) !== null) colorHits.push(file.replace(/\\/g, '/') + ':' + hit[0])
  }
}
check('src/ 零硬编码颜色（#hex / rgb() / hsl() 命中 0）', colorHits.length === 0,
  colorHits.slice(0, 6).join(', '))
check('导航骨架条与回退提示的样式进了产物（.hc-nav__skel / .hc-notice 存在）',
  /\.hc-nav__skel-bar\s*\{/.test(readFileSync(CSS, 'utf8')) &&
    /\.hc-notice\s*\{/.test(readFileSync(CSS, 'utf8')))

// M8：桩里的 `DEFAULT_MODULES` 不许和后端 manifest 漂移 —— 否则探针只是在验证自己的
// 抄写本，而真实后端改了 label / nav_order 时探针照样全绿。这里直接读 dashboard/modules
// 下的真 manifest 逐字段比对（本卡不改后端，所以是只读比对）。
const realManifests = readdirSync(join(REPO, 'dashboard/modules'), { withFileTypes: true })
  .filter((e) => e.isDirectory())
  .map((e) => join(REPO, 'dashboard/modules', e.name, 'manifest.json'))
  .filter((file) => existsSync(file))
  .map((file) => JSON.parse(readFileSync(file, 'utf8')))
  .sort((a, b) => a.nav_order - b.nav_order)
check('backend manifest 发现 11 个模块（M4b1 起含 settings，V3 起含 vcc，V4 起含 remote，V5 起含 models，自动命名面板起含 autotitler/45）', realManifests.length === 11,
  'found=' + realManifests.length + ' ' + realManifests.map((m) => m.id).join(','))
check('探针桩与后端 manifest 的 id 集合一致（不多不少）',
  JSON.stringify(DEFAULT_MODULES.map((m) => m.id).sort()) ===
    JSON.stringify(realManifests.map((m) => m.id).sort()),
  JSON.stringify(DEFAULT_MODULES.map((m) => m.id)))
const driftFields = []
for (const real of realManifests) {
  const stub = DEFAULT_MODULES.find((m) => m.id === real.id)
  if (!stub) continue
  for (const field of ['label', 'nav_order', 'section']) {
    if (stub[field] !== real[field]) driftFields.push(real.id + '.' + field + ' stub=' + stub[field] + ' real=' + real[field])
  }
}
check('探针桩的 label / nav_order / section 与 manifest 逐字段相同（不漂移）',
  driftFields.length === 0, driftFields.join('; '))

// 用**真实 manifest 的启用结论**渲染：默认启用集 = 读 default_enabled 为 true 的那些。
// 清单故意写死（按 nav_order）：默认启用是产品决定，谁翻转 default_enabled 就该在这里
// 同步改一次；让探针自动跟随就会让「某个分区被静默关掉」永远绿。
const EXPECTED_DEFAULT_ENABLED = [
  'overview',
  'weather',
  'models',
  'agents',
  'autogroup',
  'autotitler',
  'advisor',
  'upgrade',
  'settings',
  'vcc',
  'remote'
]
const realEnabled = realManifests
  .filter((m) => m.default_enabled === true)
  .map((m) => m.id)
check('后端默认启用集 = 11 个模块（nav_order 10..90，含 autotitler/45）',
  JSON.stringify(realEnabled) === JSON.stringify(EXPECTED_DEFAULT_ENABLED),
  JSON.stringify(realEnabled))
const M8 = await mount('index_m8.js', {
  modules: realManifests.map((m) =>
    Object.assign(
      { id: m.id, label: m.label, nav_order: m.nav_order, section: m.section, enabled: realEnabled.indexOf(m.id) !== -1, reason: '' },
      {}
    )
  )
})
await M8.render(0)
const m8 = await settle(M8)
const m8Labels = findByClass(m8.nodes, 'hc-nav__item').map((n) => textOf(n).trim())
check('用真实 manifest 载荷渲染 ⇒ 出全部默认启用的 11 个 tab（按 nav_order 10→90）',
  JSON.stringify(m8Labels) === JSON.stringify(['概览', '天气', '模型', 'Agent 总览', '项目分组', '自动命名', 'Advisor', '升级', '设置', '上下文引擎', '远端后端']),
  JSON.stringify(m8Labels))

// ═══════════════════════════════════════════════ 用例 D：纯函数
console.log('\n[D] lib/rest.js + lib/format.js 纯函数')
copyFileSync(join(REPO, 'src/lib/rest.js'), join(STUB, 'rest.mjs'))
copyFileSync(join(REPO, 'src/lib/format.js'), join(STUB, 'format.mjs'))
const rest = await import(pathToFileURL(join(STUB, 'rest.mjs')).href)
const fmt = await import(pathToFileURL(join(STUB, 'format.mjs')).href)

let seen = null
rest.bindRest({
  rest(path, opts) {
    seen = { path, opts }
    if (path.indexOf('/boom') === 0) return Promise.reject(new Error('422: {"detail":"agent 必须是字符串"}'))
    if (path.indexOf('/net') === 0) return Promise.reject(new Error('Timed out connecting to Hermes backend'))
    if (path.indexOf('/resolve') === 0) return Promise.resolve({ ok: false, error: { message: '没有这个分区' } })
    if (path.indexOf('/degraded') === 0) {
      return Promise.resolve({
        ok: false,
        scope: { kind: 'profile', agent: 'code-worker' },
        error: { code: 'timeout', message: '取数超过 8 秒仍未返回，已放弃本次读取' }
      })
    }
    return Promise.resolve({ ok: true, items: [1, 2] })
  }
})

const g = await rest.get('/overview', { agent: 'xiaomo' })
check('get 成功返回 {ok:true,data}', g.ok === true && Array.isArray(g.data.items))
check('get 自动拼 agent 参数', seen.path === '/overview?agent=xiaomo', seen.path)
check('GET 用的 method 是 GET', seen.opts.method === 'GET')

const withExtra = rest.withQuery('/x', { a: 1, b: '', c: null })
check('withQuery 跳过空值', withExtra === '/x?a=1', withExtra)

const boom = await rest.get('/boom')
check('非 2xx（reject "422: {...}"）被归一成 {ok:false}', boom.ok === false)
check('保留状态码 422', boom.code === '422', boom.code)
check('抽出后端 detail 原文', /agent 必须是字符串/.test(boom.message), boom.message)
check('提示是「参数被拒绝」而不是「未知错误」', /请求参数被后端拒绝/.test(boom.message), boom.message)

const net = await rest.get('/net')
// 宿主 fetch 的超时文案（"Timed out connecting to Hermes backend"）必须归成 timeout 而不是 network：
// 两者对应完全不同的排查方向（接口算得慢 vs 网关没起来）。
check(
  '宿主 fetch 超时归成 code=timeout（不是 network）',
  net.ok === false && net.code === 'timeout',
  net.code
)
check('超时文案是「取数超时」，不是「连不上后端」', /取数超时/.test(net.message), net.message)
check('网络失败保留底层原话', /Timed out/.test(net.message), net.message)

const resolvedError = await rest.get('/resolve')
check('resolve 出 {ok:false} 的老路径也归一', resolvedError.ok === false && /没有这个分区/.test(resolvedError.message))

const degraded = await rest.get('/degraded')
check(
  '结构化降级（200 + ok:false + error.code）按后端的 code 说话',
  degraded.ok === false && degraded.code === 'timeout' && /取数超时/.test(degraded.message),
  degraded.message
)
check(
  '降级不再被说成「连不上后端」（那是假话）',
  !/连不上后端/.test(degraded.message),
  degraded.message
)
check('降级保留后端原话', /取数超过 8 秒/.test(degraded.message), degraded.message)

check('compact 进位不改数值（343432426 → 3.4亿）', fmt.compact(343432426) === '3.4亿', fmt.compact(343432426))
check('compact 万级（1509287 → 150.9万）', fmt.compact(1509287) === '150.9万', fmt.compact(1509287))
check('compact 小数字仍走千分位', fmt.compact(1234) === '1,234', fmt.compact(1234))
check('compact 空值 → —（不是 0）', fmt.compact(null) === '—' && fmt.compact(undefined) === '—')
check('duration 秒 → 天/小时（300000 → 3 天 11 小时）', fmt.duration(300000) === '3 天 11 小时', fmt.duration(300000))
check('duration 空值 → —', fmt.duration(null) === '—')

let bodyGuard = null
rest.bindRest({
  rest(path, opts) {
    bodyGuard = { path, opts }
    return Promise.resolve({ ok: true })
  }
})
const bad = await rest.send('/layout', JSON.stringify({ a: 1 }))
check('body 传字符串被拒（不发起请求）', bad.ok === false && bad.code === 'bad_body', bad.code)
check('被拒时确实没调用 ctx.rest', bodyGuard === null)
const good = await rest.send('/layout', { a: 1 }, { method: 'PUT' })
check('body 传对象照常发出', good.ok === true && bodyGuard.opts.body.a === 1 && bodyGuard.opts.method === 'PUT')

const now = Date.parse('2026-10-28T12:00:00+08:00')
check('relTime 分钟', fmt.relTime(now - 3 * 60 * 1000, now) === '3 分钟前', fmt.relTime(now - 3 * 60 * 1000, now))
check('relTime 小时', fmt.relTime(now - 2 * 3600 * 1000, now) === '2 小时前', fmt.relTime(now - 2 * 3600 * 1000, now))
check('relTime 刚刚', fmt.relTime(now - 5000, now) === '刚刚', fmt.relTime(now - 5000, now))
check('relTime 秒级 epoch 也认', fmt.relTime(Math.floor((now - 60000) / 1000), now) === '1 分钟前', fmt.relTime(Math.floor((now - 60000) / 1000), now))
check('relTime 解析不了返回 —', fmt.relTime('不是时间') === '—')
check('num 千分位', fmt.num(1234567) === '1,234,567', fmt.num(1234567))
check('num 非数字返回 —', fmt.num(null) === '—')
check('bytes KB/MB', fmt.bytes(1536) === '1.5 KB' && fmt.bytes(5 * 1024 * 1024) === '5 MB', fmt.bytes(1536) + ' / ' + fmt.bytes(5 * 1024 * 1024))
check('bytes 小值', fmt.bytes(800) === '800 B', fmt.bytes(800))
check('pct 分母 0 返回 —', fmt.pct(1, 0) === '—')

// ═══════════════════════════════════════════════ 用例 E：CSS 不变量
console.log('\n[E] dist/style.css 关键规则')
const css = readFileSync(CSS, 'utf8')
check('tokens 挂在 .hc-root 上（不污染 :root）', /\.hc-root\s*\{[^}]*--s1:\s*4px/.test(css))
check('间距 4/8 栅格变量齐全', ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'].every((v) => css.indexOf(v + ':') !== -1))
check('字号变量齐全', ['--fs-xs', '--fs-sm', '--fs-md', '--fs-lg', '--fs-xl'].every((v) => css.indexOf(v + ':') !== -1))
check('圆角变量齐全', ['--r-sm', '--r-md', '--r-lg'].every((v) => css.indexOf(v + ':') !== -1))
check('选中态有左描边', /\.hc-nav__item--active\s*\{[^}]*border-left-color:\s*var\(--ui-accent/.test(css))
check('选中态同时提亮文字', /\.hc-nav__item--active\s*\{[^}]*color:\s*var\(--ui-text-primary/.test(css))
check('窄窗容器查询存在', /@container hc \(max-width: 899px\)/.test(css))
// C7：Agent 总览表在窄容器必须收敛列（隐藏「模型」「插件」两列 + 同步收窄 --ag-cols，否则留空档）
// t_a67a2bc2：断点从 899px 提到 1099px —— 900px 视口的内容宽只有 ~648px，写 899 时 900 宽度下
// 仍按 8 列排，实测 28 个元素右边界溢出视口、「插件/操作」列要横向拖才看得见。
check(
  '窄窗 Agent 总览隐藏「模型」「插件」两列',
  /@container hc \(max-width: 1099px\)[\s\S]*?\.hc-ag__cell--wide[\s\S]{0,80}?display:\s*none/.test(css)
)
check(
  '窄窗 Agent 总览列定义同步收窄（--ag-cols 8 列 → 6 列）',
  /--ag-cols:\s*76px/.test(css) &&
    /@container hc \(max-width: 1099px\)[\s\S]*?--ag-cols:\s*68px minmax\(0,\s*1fr\)\s+56px\s+88px\s+84px\s+176px/.test(css)
)
check(
  'Agent 总览表头与行共用同一份列定义（不错位）',
  (css.match(/grid-template-columns:\s*var\(--ag-cols\)/g) || []).length >= 2
)
check('Agent 总览行有独立边框/悬停态（表格不是裸 div）', /\.hc-ag__row:hover\s*\{/.test(css) && /\.hc-ag__item\s*\+\s*\.hc-ag__item\s*\{/.test(css))
check('窄窗隐藏导航文字', /@container hc[^}]*--s3\)/s.test(css) && /\.hc-nav__label[^}]*display:\s*none/.test(css))
check('有 :focus-visible 焦点环', /\.hc-root :focus-visible\s*\{[^}]*outline:\s*2px solid var\(--ui-accent/.test(css))
check('idle 面板用 visibility 隐藏（不是 display:none）', /\.hc-pane--idle\s*\{[^}]*visibility:\s*hidden/.test(css))
check('没有 data-theme 分支（跟随宿主）', !/data-theme/.test(css))
check('没有任何阴影（卡片靠 stroke 立起来）', !/box-shadow/.test(css))

// C6：样式自注入 + 视觉打磨的 CSS 不变量（这些规则决定「不是一堆裸文本」这件事）
console.log('\n[H] C6 视觉打磨的 CSS 不变量')
check('统计卡是响应式栅格（display:grid + 三档列数）',
  /\.hc-cards\s*\{[^}]*display:\s*grid[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/.test(css) &&
    /@container hc \(min-width:\s*900px\)[\s\S]{0,200}\.hc-cards\s*\{[^}]*repeat\(2,\s*minmax\(0,\s*1fr\)\)/.test(css) &&
    /@container hc \(min-width:\s*1200px\)[\s\S]{0,200}\.hc-cards\s*\{[^}]*repeat\(4,\s*minmax\(0,\s*1fr\)\)/.test(css))
check('统计卡：--s4 padding + 1px stroke-secondary + --r-md',
  /\.hc-card\s*\{[^}]*padding:\s*var\(--s4\)[^}]*border:\s*var\(--hairline\) solid var\(--ui-stroke-secondary[^}]*border-radius:\s*var\(--r-md\)/s.test(css))
check('数字层级三档：--fs-xl / --fs-sm / --fs-xs',
  /\.hc-card__num\s*\{[^}]*font-size:\s*var\(--fs-xl\)[^}]*color:\s*var\(--ui-text-primary/.test(css) &&
    /\.hc-card__unit\s*\{[^}]*font-size:\s*var\(--fs-sm\)[^}]*color:\s*var\(--ui-text-quaternary/.test(css) &&
    /\.hc-card__caliber\s*\{[^}]*font-size:\s*var\(--fs-xs\)/.test(css))
check('定时任务用定宽列（--row-cols 四列，不是 auto）',
  /\.hc-rows--cron\s*\{[^}]*--row-cols:\s*minmax\(0,\s*1fr\)\s+\d+px\s+\d+px\s+\d+px/.test(css))
check('数据行与表头共用同一份列定义', /\.hc-rows--cron \.hc-row\s*\{[^}]*grid-template-columns:\s*var\(--row-cols\)/.test(css))
check('行高用 --row-h，hover 有底色变化',
  /\.hc-row\s*\{[^}]*min-height:\s*var\(--row-h\)/s.test(css) && /\.hc-row:hover\s*\{[^}]*background:/.test(css))
check('看板挂件是小卡片栅格（auto-fill + 分母/次数）',
  /\.hc-widgets\s*\{[^}]*display:\s*grid[^}]*auto-fill/.test(css) && /\.hc-widget__num\s*\{/.test(css))
check('状态色三档：secondary / warning / quaternary',
  /\.hc-status\s*\{[^}]*color:\s*var\(--ui-text-secondary/.test(css) &&
    /\.hc-status--warn\s*\{[^}]*color:\s*var\(--ui-warning/.test(css) &&
    /\.hc-status--off\s*\{[^}]*color:\s*var\(--ui-text-quaternary/.test(css))
check('状态不只靠颜色（左圆点形状 + 虚线，色盲也能读）',
  /\.hc-status::before\s*\{/.test(css) && /\.hc-status--off::before\s*\{[^}]*border-style:\s*dashed/.test(css))
check('窄窗（<900px）隐藏「作用域」列',
  /@container hc \(max-width:\s*899px\)[\s\S]*?\.hc-row__scope\s*\{\s*display:\s*none/.test(css))
check('窄窗 cron 列数收敛（4 列 → 3 列）',
  /@container hc \(max-width:\s*899px\)[\s\S]*?\.hc-rows--cron\s*\{[^}]*--row-cols:\s*minmax\(0,\s*1fr\)\s+\d+px\s+\d+px;\s*\}/.test(css))
check('窄窗不横向滚动（内容面板 overflow-x: hidden）', /\.hc-pane\s*\{[^}]*overflow-x:\s*hidden/.test(css))
check('窄窗会话列表也收敛列数（第 4 列收起，行高不折行）',
  /@container hc \(max-width:\s*899px\)[\s\S]*?\.hc-sessions \.hc-row > :nth-child\(4\)\s*\{\s*display:\s*none/.test(css))
check('骨架屏呼吸动画存在（加载态的视觉，而不是一行文字）', /@keyframes hc-breathe/.test(css))
check('注入标记与类名同处一份产物（桌面端才拿得到样式）',
  readFileSync(JS, 'utf8').indexOf('data-hermes-console-style') !== -1 &&
    readFileSync(JS, 'utf8').indexOf('.hc-card {') !== -1)

// ═══════════════════════════════════════════════ 汇总
// 清理本进程的桩目录（里面只有自己写的假 react，rmtree 安全）
rmSync(STUB, { recursive: true, force: true })

console.log('\n=== 汇总 ===')
console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
if (failures.length) {
  failures.forEach((f) => console.log('  ! ' + f))
  process.exitCode = 1
} else {
  console.log('SHELL-PROBE-OK')
}
