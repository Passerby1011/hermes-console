/**
 * ⚠️ 已过期（V5 DOM）：本脚本断言的是整改前的模型分区 DOM
 * （`data-models-provider-picker` / `data-models-target` / `data-models-ask` 等）。
 * 布局整改（t_e691ae8c）按验收要求**删掉了**那些重复表单，这些 hook 已不存在，
 * 因此本脚本现在会在等待选择器处超时 —— 那是**脚本过期**，不是产品回归。
 * 现行验收脚本：`scripts/probe-models-layout.mjs`（同一批后端路径 + 新布局）。
 *
 * hermes-console · 「模型」分区（V5）真 React 19 + jsdom 冒烟测试。
 *
 * 跑法：node plugins/hermes-console/scripts/probe-models.mjs
 *
 * 本文件只证明**只有真 DOM 才能证明**的事（与 probe_models.py 分工：那边验后端契约）：
 *   1. 「模型」分区真的挂进导航、面板真的渲染出当前模型 / provider / 快照 / 预设；
 *   2. ★点「下一步：确认切换」时**一个 POST 都没发**（二次确认不是摆设）；
 *   3. ★点「确认切换」后才发 POST /models/apply，且请求体里 **agent 是单个值**、
 *      **confirm === true**、provider/model 就是对比条上显示的那两个；
 *   4. ★回滚同样两步走（点「回滚到这份」不发请求，点「确认回滚」才发）；
 *   5. 三态：加载中是骨架屏、失败是错误态（后端原话透出）、空态说清为什么空；
 *   6. 凭据只读：页面上找不到任何明文 key，只有变量名与「已配置/未配置」。
 *
 * 模块来源：宿主 `hermes-agent/node_modules`（react / react-dom / jsdom 都在里面），
 * 用临时目录里的 junction 接过去 —— 不复制、不安装、不改宿主任何文件。
 */
import { copyFileSync, mkdirSync, mkdtempSync, rmdirSync, symlinkSync, unlinkSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..')
const ENTRY = join(REPO, 'dashboard/dist/index.js')
const HOST_NODE_MODULES =
  process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'

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
const WORK = mkdtempSync(join(tmpdir(), 'hc-models-'))
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

// 独立模块实例 = 独立缓存：拷一份到临时目录再 import（多实例不共享 query 缓存）
const entryCopy = join(WORK, 'entry_models.js')
copyFileSync(ENTRY, entryCopy)
const pluginApi = await import(pathToFileURL(entryCopy).href)
const plugin = pluginApi.default

// ---------------------------------------------------------------- 测试桩数据
/** 两个 agent：code-worker 用 balance/glm-5.3，xiaomo 用 balance/deepseek。 */
const OVERVIEW = {
  ok: true,
  read_only: true,
  count: 2,
  agents: [
    {
      agent: 'code-worker',
      profile: 'code-worker',
      label: 'code-worker',
      config_path: 'D:/fake/profiles/code-worker/config.yaml',
      config_exists: true,
      model: 'glm-5.3',
      provider: 'balance',
      base_url: 'https://balance.example/v1',
      api_mode: 'chat_completions',
      key_env: 'HERMES_CUSTOM_BALANCE_API_KEY',
      key_present: true,
      current: { provider: 'balance', model: 'glm-5.3' },
      fallbacks: [{ provider: 'balance', model: 'deepseek-v4-flash' }],
      fallback_count: 1,
      provider_count: 2,
      providers: ['balance', 'gemini'],
      missing: [],
      error: ''
    },
    {
      agent: 'xiaomo',
      profile: 'xiaomo',
      label: '墨黎',
      config_path: 'D:/fake/profiles/xiaomo/config.yaml',
      config_exists: true,
      model: 'deepseek-v4-flash',
      provider: 'balance',
      base_url: 'https://balance.example/v1',
      api_mode: 'chat_completions',
      key_env: 'HERMES_CUSTOM_BALANCE_API_KEY',
      key_present: true,
      current: { provider: 'balance', model: 'deepseek-v4-flash' },
      fallbacks: [],
      fallback_count: 0,
      provider_count: 1,
      providers: ['balance'],
      missing: [],
      error: ''
    }
  ],
  rows: []
}
OVERVIEW.rows = OVERVIEW.agents

const PROVIDERS = {
  ok: true,
  agent: 'code-worker',
  profile: 'code-worker',
  label: 'code-worker',
  show_all: true,
  count: 2,
  current: { provider: 'balance', model: 'glm-5.3' },
  providers: [
    {
      provider: 'balance',
      label: 'balance',
      base_url: 'https://balance.example/v1',
      kind: 'provider',
      source: 'config',
      configured: true,
      configured_reason: 'config',
      current: true,
      model: 'deepseek-v4-flash',
      key_env: 'HERMES_CUSTOM_BALANCE_API_KEY',
      key_envs: ['HERMES_CUSTOM_BALANCE_API_KEY'],
      key_present: true,
      credentials: { masked_keys: [] },
      declared_in_config: true
    },
    {
      provider: 'gemini',
      label: 'Gemini',
      base_url: 'https://gemini.example/v1',
      kind: 'provider',
      source: 'config',
      configured: true,
      configured_reason: 'config',
      current: false,
      model: 'gemini-3-pro',
      key_env: 'HERMES_CUSTOM_GEMINI_API_KEY',
      key_envs: ['HERMES_CUSTOM_GEMINI_API_KEY'],
      key_present: true,
      // ★打码后的凭据：真值永远不会出现，这里用 *** 代表后端形状
      credentials: { api_key: '***', masked_keys: ['api_key'] },
      declared_in_config: true
    }
  ]
}

const LIBRARY = {
  ok: true,
  agent: 'code-worker',
  count: 2,
  model_total: 4,
  live: false,
  providers: [
    {
      provider: 'balance',
      label: 'balance',
      base_url: 'https://balance.example/v1',
      configured: true,
      current: true,
      key_env: 'HERMES_CUSTOM_BALANCE_API_KEY',
      key_present: true,
      models: ['deepseek-v4-flash', 'glm-5.3'],
      model_count: 2,
      source: 'config',
      default_model: 'deepseek-v4-flash'
    },
    {
      provider: 'gemini',
      label: 'Gemini',
      base_url: 'https://gemini.example/v1',
      configured: true,
      current: false,
      key_env: 'HERMES_CUSTOM_GEMINI_API_KEY',
      key_present: true,
      models: ['gemini-3-flash', 'gemini-3-pro'],
      model_count: 2,
      source: 'config',
      default_model: 'gemini-3-pro'
    }
  ]
}

const SNAPSHOTS = {
  ok: true,
  snapshot_root: 'D:/fake/plugins/hermes-console/data/snapshots',
  count: 1,
  snapshots: [
    {
      ts: '20261029-140000',
      dir: 'D:/fake/plugins/hermes-console/data/snapshots/20261029-140000',
      agents: ['code-worker'],
      profiles: ['code-worker'],
      files: { 'code-worker': 'D:/fake/.../code-worker.yaml' },
      sizes: { 'code-worker': 2048 },
      operation: 'apply',
      agent: 'code-worker',
      note: '手动切换',
      created_at: '2026-10-29T14:00:00',
      sha256: 'deadbeef'
    }
  ]
}

const PRESETS = {
  ok: true,
  path: 'D:/fake/plugins/hermes-console/data/presets.json',
  version: 1,
  count: 1,
  agent: 'code-worker',
  presets: [
    {
      name: 'fast',
      scope: 'model',
      provider: 'balance',
      model: 'deepseek-v4-flash',
      label: 'deepseek-v4-flash',
      note: '快模型',
      updated_at: '2026-10-29T14:00:00',
      is_current: false
    }
  ]
}

const APPLY_OK = {
  ok: true,
  operation: 'apply',
  agent: 'code-worker',
  label: 'code-worker',
  scope: 'model',
  provider: 'gemini',
  model: 'gemini-3-pro',
  applied: { provider: 'gemini', model: 'gemini-3-pro', base_url: 'https://gemini.example/v1' },
  previous: { provider: 'balance', model: 'glm-5.3' },
  snapshot_path: 'D:/fake/plugins/hermes-console/data/snapshots/20261029-150000/code-worker.yaml',
  snapshot_ts: '20261029-150000',
  verify: [{ key: 'model.provider', expected: 'gemini', actual: 'gemini', ok: true }],
  verified: true,
  config_path: 'D:/fake/profiles/code-worker/config.yaml'
}

const ROLLBACK_OK = {
  ok: true,
  operation: 'rollback',
  agent: 'code-worker',
  label: 'code-worker',
  snapshot_ts: '20261029-140000',
  snapshot_path: 'D:/fake/.../code-worker.yaml',
  restored: { provider: 'balance', model: 'glm-5.3' },
  previous: { provider: 'gemini', model: 'gemini-3-pro' },
  verified: true,
  config_path: 'D:/fake/profiles/code-worker/config.yaml'
}

const AGENTS = {
  agents: [
    { id: 'code-worker', label: 'code-worker', is_default: false, has_config: true },
    { id: 'xiaomo', label: '墨黎', is_default: false, has_config: true }
  ],
  current: 'code-worker'
}

const restCalls = []
const store = { 'nav.section': 'models', 'scope.agent': 'code-worker' }

/** 每个用例可覆盖的失败注入（`options` 里给）。 */
function makeCtx(options) {
  const opts = options || {}
  return {
    rest(path, req) {
      restCalls.push({ path: path, method: (req && req.method) || 'GET', body: (req && req.body) || null })
      const pathname = path.split('?')[0]
      if (pathname === '/modules') {
        if (opts.failModules) return Promise.reject(new Error(opts.failModules))
        return Promise.resolve({
          ok: true,
          data: {
            agent: 'code-worker',
            modules: [
              { id: 'overview', label: '概览', nav_order: 10, section: 'overview', enabled: true, reason: '' },
              { id: 'models', label: '模型', nav_order: 25, section: 'models', enabled: true, reason: '' }
            ],
            health: { loaded: [], failed: {} }
          }
        })
      }
      if (path.indexOf('/agents') === 0) return Promise.resolve(AGENTS)
      if (pathname === '/models/overview') {
        if (opts.overviewFails) {
          return Promise.reject(new Error('503: {"ok":false,"error":{"code":"upstream_error","message":"core/scope 不可用"}}'))
        }
        if (opts.overviewEmpty) return Promise.resolve({ ok: true, count: 0, agents: [], rows: [] })
        return Promise.resolve(OVERVIEW)
      }
      if (pathname === '/models/providers' || pathname.indexOf('/models/providers/') === 0) {
        if (opts.providersEmpty) return Promise.resolve(Object.assign({}, PROVIDERS, { count: 0, providers: [] }))
        return Promise.resolve(PROVIDERS)
      }
      if (pathname === '/models/model-library') return Promise.resolve(LIBRARY)
      if (pathname === '/models/snapshots') return Promise.resolve(SNAPSHOTS)
      if (pathname === '/models/presets' && (!req || (req && req.method) !== 'POST')) {
        return Promise.resolve(PRESETS)
      }
      if (pathname === '/models/apply') return Promise.resolve(APPLY_OK)
      if (pathname === '/models/rollback') return Promise.resolve(ROLLBACK_OK)
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
}

plugin.register(
  Object.assign({}, makeCtx({}), {
    registerMany() {}
  })
)

const contributions = []
plugin.register(
  Object.assign({}, makeCtx({}), {
    registerMany(list) {
      contributions.push(...list)
    }
  })
)
const page = contributions.find((c) => c.area === 'routes')
check('routes 贡献拿到组件', Boolean(page) && typeof page.render === 'function')

const container = document.getElementById('root')
const root = createRoot(container)
const flush = () => new Promise((r) => setTimeout(r, 80))
const Page = function Page() {
  return page.render()
}
await act(async () => {
  root.render(React.createElement(Page))
})
await act(flush)

const q = (sel) => container.querySelector(sel)
const qa = (sel) => Array.from(container.querySelectorAll(sel))
const text = () => container.textContent || ''
const navBtn = (re) => qa('.hc-nav__item').find((b) => re.test(b.textContent))

console.log('\n[A] 导航入口 + 分区挂载')
const modelsNav = navBtn(/模型/)
check('导航里有「模型」入口（真实 <button>）', Boolean(modelsNav) && modelsNav.tagName === 'BUTTON')
check('后端 /modules 里给出了模型模块（10 项桩里的第 2 项按 nav_order 排在天气之后）',
  qa('.hc-nav > li').length === 2, 'li=' + qa('.hc-nav > li').length)

await act(async () => {
  modelsNav.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)

const pane = qa('.hc-pane').find((p) => p.querySelector('.hc-models'))
check('分区面板真的挂载（.hc-models 在 DOM 里）', Boolean(pane))
check('顶栏标题切到「模型」', /模型/.test(q('.hc-topbar__title').textContent),
  q('.hc-topbar__title').textContent)
check('nav.section 记到 storage', store['nav.section'] === 'models', JSON.stringify(store['nav.section']))

const paneQ = (sel) => (pane ? pane.querySelector(sel) : null)
const paneQA = (sel) => (pane ? Array.from(pane.querySelectorAll(sel)) : [])
const paneText = () => (pane ? pane.textContent : '')

console.log('\n[B] 目标 agent + 当前模型')
const banner = paneQ('[data-models-target]')
check('★常驻目标 agent 横幅（data-models-target=code-worker）',
  Boolean(banner) && banner.getAttribute('data-models-target') === 'code-worker',
  banner ? banner.getAttribute('data-models-target') : 'no banner')
check('★横幅里点名中文名 + id', /code-worker（code-worker）/.test(paneText()), paneText().slice(0, 120))
check('当前模型卡片渲染出 provider=balance / 模型=glm-5.3',
  /balance/.test(paneText()) && /glm-5\.3/.test(paneText()))
check('凭据变量名可见（HERMES_CUSTOM_BALANCE_API_KEY）',
  /HERMES_CUSTOM_BALANCE_API_KEY/.test(paneText()))
check('★凭据只读：页面上没有 "sk-"、没有明文 key',
  paneText().indexOf('sk-') === -1 && paneText().indexOf('***') === -1,
  'text 里既无 sk- 也无真值（*** 只在后端 JSON 里，界面显示"已配置"）')
check('「已配置/未配置」是布尔档，不是取值', /变量已配置|变量未配置/.test(paneText()))

console.log('\n[C] ★二次确认：未点确认前一个 POST 都不发')
const postsBefore = restCalls.filter((c) => c.method === 'POST').length
check('挂载后没有任何 POST（读操作只有 GET）', postsBefore === 0, 'POST=' + postsBefore)

const providerPicker = paneQ('[data-models-provider-picker]')
check('provider 下拉存在且默认是当前 provider',
  Boolean(providerPicker) && providerPicker.value === 'balance',
  providerPicker ? providerPicker.value : 'no select')

await act(async () => {
  providerPicker.value = 'gemini'
  providerPicker.dispatchEvent(new window.Event('change', { bubbles: true }))
})
await act(flush)

const modelInput = paneQ('[data-models-model-input]')
await act(async () => {
  // React 的受控 <input> 用内部 value tracker 判脏；直接赋 .value 会被它当成"没变"而吞掉事件。
  // 必须走原型上的原生 setter，再派发 input 事件（React 19 的 onChange 监听的就是它）。
  const setNativeValue = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value'
  ).set
  setNativeValue.call(modelInput, 'gemini-3-pro')
  modelInput.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await act(flush)
check('模型输入框接受手填值（受控组件真的更新了）', modelInput.value === 'gemini-3-pro',
  modelInput.value)

const diffRows = paneQA('[data-models-diff]')
check('★对比条渲染出「当前 → 目标」（两行：provider / 模型）', diffRows.length === 2,
  'rows=' + diffRows.length)
check('★对比条里旧值是 balance / glm-5.3，新值是 gemini / gemini-3-pro',
  /balance/.test(diffRows[0].textContent) && /gemini/.test(diffRows[0].textContent) &&
    /glm-5\.3/.test(diffRows[1].textContent) && /gemini-3-pro/.test(diffRows[1].textContent),
  diffRows.map((r) => r.textContent.trim()).join(' | '))

const askBtn = paneQ('[data-models-ask]')
check('「下一步：确认切换」按钮可用（对比条已变）', Boolean(askBtn) && askBtn.disabled === false,
  askBtn ? 'disabled=' + askBtn.disabled : 'no button')

await act(async () => {
  askBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)

const postsAfterAsk = restCalls.filter((c) => c.method === 'POST').length
check('★★点了「下一步」之后仍然**没有任何 POST**（二次确认不是摆设）', postsAfterAsk === 0,
  'POST=' + postsAfterAsk)
const confirmBar = paneQ('.hc-up__confirm')
check('★确认条出现了（.hc-up__confirm）', Boolean(confirmBar))
check('★确认条点名目标 agent（code-worker）与新旧模型',
  /code-worker/.test(confirmBar.textContent) && /gemini-3-pro/.test(confirmBar.textContent) &&
    /glm-5\.3/.test(confirmBar.textContent),
  confirmBar.textContent.trim().slice(0, 160))
check('确认条上有「确认切换」按钮（data-models-confirm-yes）',
  Boolean(paneQ('[data-models-confirm-yes]')))

console.log('\n[D] ★确认后才发 apply，且请求体合规')
const yesBtn = paneQ('[data-models-confirm-yes]')
await act(async () => {
  yesBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)

const applyCalls = restCalls.filter((c) => c.path.split('?')[0] === '/models/apply')
check('★点了「确认切换」后才发 POST /models/apply，且只发一次', applyCalls.length === 1,
  'calls=' + applyCalls.length)
const applyBody = applyCalls.length ? applyCalls[0].body || {} : {}
console.log('        apply body :', JSON.stringify(applyBody))
check('★请求体 agent 是**单个字符串**（不是数组、不带 targets）',
  applyBody.agent === 'code-worker' && applyBody.targets === undefined,
  JSON.stringify(applyBody.agent) + ' targets=' + String(applyBody.targets))
check('★请求体带 confirm === true（服务端第二道确认）', applyBody.confirm === true,
  String(applyBody.confirm))
check('★请求体的 provider/model 与对比条上的目标一致',
  applyBody.provider === 'gemini' && applyBody.model === 'gemini-3-pro',
  applyBody.provider + '/' + applyBody.model)
check('请求体 scope 是单值 model', applyBody.scope === 'model', String(applyBody.scope))
check('成功后出现结果条（点名已切换 + 快照路径）',
  /已切换/.test(paneText()) && /snapshot/i.test(paneText()),
  (function () {
    var fb = paneQ('[data-models-feedback]')
    return fb ? fb.textContent.trim().slice(0, 200) : 'no [data-models-feedback]（结果条没渲染）'
  })())

console.log('\n[E] ★回滚也是两步')
const postsBeforeRollback = restCalls.filter((c) => c.method === 'POST').length
const rollbackBtn = paneQ('[data-models-rollback]')
check('快照行带「回滚到这份」按钮', Boolean(rollbackBtn) && rollbackBtn.disabled === false)
await act(async () => {
  rollbackBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
const postsAfterRollbackAsk = restCalls.filter((c) => c.method === 'POST').length
check('★点「回滚到这份」后仍未发 POST（要再确认一次）',
  postsAfterRollbackAsk === postsBeforeRollback,
  'before=' + postsBeforeRollback + ' after=' + postsAfterRollbackAsk)
const rollbackYes = paneQ('[data-models-rollback-yes]')
check('★回滚确认条出现且点名快照与 agent',
  Boolean(rollbackYes) && /20261029-140000/.test(paneText()) && /code-worker/.test(paneText()),
  rollbackYes ? 'ok' : 'no confirm button')
await act(async () => {
  rollbackYes.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const rollbackCalls = restCalls.filter((c) => c.path.split('?')[0] === '/models/rollback')
check('★确认后发 POST /models/rollback，且带 confirm=true + 单个 agent',
  rollbackCalls.length === 1 && (rollbackCalls[0].body || {}).confirm === true &&
    (rollbackCalls[0].body || {}).agent === 'code-worker',
  JSON.stringify(rollbackCalls[0] ? rollbackCalls[0].body : null))
check('回滚成功后有结果条（已回滚 + 还原值）', /已回滚/.test(paneText()))

console.log('\n[F] 预设表单（只写 presets.json）')
const presetName = paneQ('[data-models-preset-name]')
const presetProvider = paneQ('[data-models-preset-provider]')
const presetModel = paneQ('[data-models-preset-model]')
const presetCreate = paneQ('[data-models-preset-create]')
check('预设表单三件套都在（名字 / provider / 模型）',
  Boolean(presetName) && Boolean(presetProvider) && Boolean(presetModel),
  'name=' + Boolean(presetName) + ' provider=' + Boolean(presetProvider) +
    ' model=' + Boolean(presetModel))
check('字段没填满时保存按钮禁用（不许建空预设）',
  Boolean(presetCreate) && presetCreate.disabled === true,
  presetCreate ? 'disabled=' + presetCreate.disabled : 'no button')

const setNative = (el, value) =>
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(el, value)
const postsBeforePreset = restCalls.filter((c) => c.method === 'POST').length
await act(async () => {
  setNative(presetName, 'probe-preset')
  presetName.dispatchEvent(new window.Event('input', { bubbles: true }))
  presetProvider.value = 'gemini'
  presetProvider.dispatchEvent(new window.Event('change', { bubbles: true }))
  setNative(presetModel, 'gemini-3-flash')
  presetModel.dispatchEvent(new window.Event('input', { bubbles: true }))
})
await act(flush)
const presetBtn2 = paneQ('[data-models-preset-create]')
check('填满后保存按钮可用', Boolean(presetBtn2) && presetBtn2.disabled === false,
  presetBtn2 ? 'disabled=' + presetBtn2.disabled : 'no button')
await act(async () => {
  presetBtn2.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(flush)
await act(flush)
const presetCalls = restCalls
  .filter((c) => c.path.split('?')[0] === '/models/presets' && c.method === 'POST')
check('★点保存后发 POST /models/presets（一次）', presetCalls.length === 1,
  'calls=' + presetCalls.length)
const presetBody = presetCalls.length ? presetCalls[0].body || {} : {}
check('★预设请求体只含 name/provider/model/note（没有 agent、没有 confirm）',
  presetBody.name === 'probe-preset' && presetBody.provider === 'gemini' &&
    presetBody.model === 'gemini-3-flash' && presetBody.agent === undefined &&
    presetBody.confirm === undefined,
  JSON.stringify(presetBody))
check('建预设**不会**触发 apply（没有写 profile 配置）',
  restCalls.filter((c) => c.path.split('?')[0] === '/models/apply').length === 1,
  'apply 仍是最早那 1 次')

console.log('\n[G] 三态（骨架屏 / 空态 / 错误态）')
// 每个用例用独立模块实例（独立缓存），三态才互不影响。
async function mountFresh(tag, options) {
  const copy = join(WORK, 'entry_' + tag + '.js')
  copyFileSync(ENTRY, copy)
  const api = await import(pathToFileURL(copy).href)
  const contribs = []
  api.default.register(
    Object.assign({}, makeCtx(options), {
      registerMany(list) {
        contribs.push(...list)
      }
    })
  )
  const host = document.createElement('div')
  document.body.appendChild(host)
  const page2 = contribs.find((c) => c.area === 'routes')
  const root2 = createRoot(host)
  await act(async () => {
    root2.render(React.createElement(function Page2() {
      return page2.render()
    }))
  })
  await act(flush)
  await act(flush)
  // 切到「模型」分区
  const btn = Array.from(host.querySelectorAll('.hc-nav__item')).find((b) => /模型/.test(b.textContent))
  if (btn) {
    await act(async () => {
      btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
    })
    await act(flush)
    await act(flush)
  }
  return {
    host: host,
    text: () => host.textContent || '',
    q: (sel) => host.querySelector(sel),
    qa: (sel) => Array.from(host.querySelectorAll(sel)),
    unmount: async () => {
      await act(async () => {
        root2.unmount()
      })
    }
  }
}

const errView = await mountFresh('err', { overviewFails: true })
check('★错误态：渲染 ErrorState（.hc-state--error）不是白屏',
  Boolean(errView.q('.hc-state--error')), errView.text().slice(0, 120))
check('★错误态：后端原话透出（core/scope 不可用）', /core\/scope 不可用/.test(errView.text()),
  errView.text().slice(0, 200))
check('★错误态：给出接口名，便于定位', /\/models\/overview/.test(errView.text()))
await errView.unmount()

const emptyView = await mountFresh('empty', { overviewEmpty: true })
check('★空态：没有 agent 时渲染空态（.hc-state）而不是骨架屏',
  Boolean(emptyView.q('.hc-state')) && !emptyView.q('.hc-skel'),
  emptyView.text().slice(0, 120))
check('★空态：说清为什么空（没有可枚举的 agent）', /一个 agent 都没有/.test(emptyView.text()),
  emptyView.text().slice(0, 200))
await emptyView.unmount()

const skeletonView = await mountFresh('skeleton', { overviewFails: false })
check('正常路径下不会停在骨架屏（数据到了就渲染真内容）',
  !skeletonView.q('.hc-skel') && Boolean(skeletonView.q('.hc-models')),
  'skel=' + Boolean(skeletonView.q('.hc-skel')))
await skeletonView.unmount()

console.log('\n[G] 汇总')
const total = pass + failures.length
console.log('PASS=' + pass + ' FAIL=' + failures.length + ' TOTAL=' + total)
try {
  unlinkSync(entryCopy)
} catch (e) {
  /* 临时文件删不掉不影响结论 */
}
try {
  unlinkSync(join(WORK, 'node_modules'))
} catch (e) {
  /* junction 在部分平台需要 rmdir */
  try {
    rmdirSync(join(WORK, 'node_modules'))
  } catch (e2) {
    /* ignore */
  }
}
if (failures.length) {
  console.log('MODELS-UI-PROBE-FAILED')
  failures.forEach((f) => console.log('  - ' + f))
  process.exitCode = 1
} else {
  console.log('MODELS-UI-PROBE-OK')
}
