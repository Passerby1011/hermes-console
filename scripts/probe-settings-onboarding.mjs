/**
 * hermes-console · 设置分区「先框架、后按需加载」验收（真 React 19 + jsdom）
 *
 * 对应卡片 t_8146f2a9 的第 1、2 条交付：
 *   [1] 首屏先出框架 + **页面内 agent 列表**（不是靠外壳顶部作用域选择器）；
 *       未选中 agent 时是「骨架 + 引导」态 —— 不是空列表，也不是 ErrorState。
 *   [2] 选中哪个 agent 才加载它的插件配置（按需），**不整批预取**：
 *       点一个 agent 只发那一个的 /agents/<id>/plugins，且切换时不回头拉别人。
 *   [3] 页内选择**不改顶部作用域**（作用域是全局的，本页只管自己）；
 *       点「不选」回到引导态且**零请求**。
 *   [4] 耗时可见：列表标题旁给出这一次请求的毫秒数（data-elapsed-ms）。
 *
 * 跑法：node plugins/hermes-console/scripts/probe-settings-onboarding.mjs
 * 打印 SETTINGS-ONBOARDING-PROBE-OK / -FAILED，失败退出码非 0。
 */
import { copyFileSync, mkdirSync, mkdtempSync, rmdirSync, rmSync, symlinkSync, unlinkSync } from 'node:fs'
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

// ------------------------------------------------- 临时模块树（junction 到宿主，不复制）
const WORK = mkdtempSync(join(tmpdir(), 'hc-set-onb-'))
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

function freshEntry(tag) {
  const dest = join(WORK, 'entry_' + tag + '.js')
  copyFileSync(ENTRY, dest)
  return import(pathToFileURL(dest).href)
}

// ------------------------------------------------------------------ 桩数据
const AGENTS = {
  agents: [
    { id: 'default', label: '小白', is_default: true, has_config: true },
    { id: 'xiaomo', label: '墨黎', is_default: false, has_config: true },
    { id: 'code-worker', label: 'code-worker', is_default: false, has_config: true },
    { id: 'media-worker', label: 'media-worker', is_default: false, has_config: true }
  ],
  current: 'code-worker'
}

const MODULES_PAYLOAD = [
  { id: 'overview', label: '概览', nav_order: 10, section: 'overview', enabled: true, reason: '' },
  { id: 'settings', label: '设置', nav_order: 70, section: 'settings', enabled: true, reason: '' }
]

function pluginRow(id, state, hasSettings) {
  return {
    plugin_id: id,
    state: state,
    in_enabled: state === 'enabled',
    in_disabled: state === 'disabled',
    source: state === 'enabled' ? 'enabled_list' : state === 'disabled' ? 'disabled_list' : 'absent',
    has_settings: hasSettings !== false,
    ref: 'plugin:' + id,
    display_name: id,
    kind: 'plugin',
    version: '1.0.0',
    in_catalog: true
  }
}

function listFor(agent) {
  return {
    ok: true,
    data: {
      agent: agent,
      config_digest: 'digest-' + agent,
      exists: true,
      path: 'D:/fake/profiles/' + agent + '/config.yaml',
      counts: { enabled: 1, disabled: 1, unavailable: 1 },
      skipped: [],
      plugins: [
        pluginRow('advisor', 'enabled', false),
        pluginRow('hermes-auto-titler', 'disabled', true),
        pluginRow('browserpaw', 'unavailable', false)
      ]
    }
  }
}

function settingsFor(agent) {
  return {
    ok: true,
    data: {
      agent: agent,
      plugin_id: 'hermes-auto-titler',
      settings: { every_n_turns: agent === 'xiaomo' ? 7 : 2 },
      sources: { every_n_turns: 'global' },
      writable_keys: ['every_n_turns'],
      readonly_keys: [],
      masked_keys: [],
      expected_config: 'digest-' + agent,
      notes: []
    }
  }
}

function makeCtx() {
  const calls = []
  const store = {}
  return {
    calls,
    store,
    ctx: {
      rest(path, apiOpts) {
        const method = (apiOpts && apiOpts.method) || 'GET'
        calls.push({ path: path, method: method })
        const pathname = path.split('?')[0]
        if (pathname === '/modules') {
          return Promise.resolve({ ok: true, data: { agent: 'code-worker', modules: MODULES_PAYLOAD, health: { loaded: [], failed: {} } } })
        }
        if (pathname === '/agents') return Promise.resolve(AGENTS)
        const listMatch = /^\/agents\/([^/]+)\/plugins$/.exec(pathname)
        if (listMatch) return Promise.resolve(listFor(decodeURIComponent(listMatch[1])))
        const modMatch = /^\/agents\/([^/]+)\/modules$/.exec(pathname)
        if (modMatch) {
          return Promise.resolve({ ok: true, data: { agent: decodeURIComponent(modMatch[1]), modules: MODULES_PAYLOAD, health: { loaded: [], failed: {} } } })
        }
        const setMatch = /^\/agents\/([^/]+)\/plugins\/([^/]+)\/settings$/.exec(pathname)
        if (setMatch) return Promise.resolve(settingsFor(decodeURIComponent(setMatch[1])))
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
}

async function mount(tag) {
  const pluginApi = await freshEntry(tag)
  const made = makeCtx()
  const contributions = []
  pluginApi.default.register(
    Object.assign({}, made.ctx, {
      registerMany(list) {
        contributions.push(...list)
      }
    })
  )
  const page = contributions.find((c) => c.area === 'routes')
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  const flush = () => new Promise((r) => setTimeout(r, 60))
  await act(async () => {
    root.render(React.createElement(page.render))
  })
  await act(flush)
  return { root, container, calls: made.calls, store: made.store, flush }
}

const q = (c, s) => c.querySelector(s)
const qa = (c, s) => Array.from(c.querySelectorAll(s))
const setText = (c) => {
  const node = q(c, '.hc-set')
  return node ? node.textContent || '' : ''
}

async function openSettings(view) {
  const btn = qa(view.container, '.hc-nav__item').find((n) => /设置/.test(n.textContent || ''))
  if (!btn) return false
  await act(async () => {
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(view.flush)
  await act(view.flush)
  return Boolean(q(view.container, '.hc-set'))
}

const pluginCalls = (view) => view.calls.filter((c) => /^\/agents\/[^/]+\/plugins$/.test(c.path))
const agentsCalled = (view) =>
  pluginCalls(view).map((c) => decodeURIComponent(c.path.split('/')[2]))

// ══════════════════════════════════════════════ [A] 首屏框架 + 页内 agent 列表
console.log('\n[A] 首屏先出框架 + 页面内 agent 列表（不靠顶部作用域选择器）')

const A = await mount('a')
const opened = await openSettings(A)
check('设置分区能进入', opened, setText(A.container).slice(0, 80))

const bar = q(A.container, '[data-agent-bar]')
check('★页面内有一排 agent 列表（data-agent-bar 存在）', Boolean(bar))
const chips = qa(A.container, '[data-agent-pick]')
check('★列表里每个 agent 都是一颗可点的按钮（4 个）', chips.length === 4, 'chips=' + chips.length)
check('页内列表的按钮是 <button>，不是纯文本', chips.every((n) => n.tagName === 'BUTTON'))
check('页内列表出现「本页内选择」这句边界说明', /本页内选择/.test(bar ? bar.textContent || '' : ''))

const scopeSelect = q(A.container, '.hc-select')
check('顶部作用域选择器仍然存在（两者不是一回事）', Boolean(scopeSelect) && scopeSelect.tagName === 'SELECT')

// ══════════════════════════════════════════════ [B] 按需加载：只读选中的那一个
console.log('\n[B] 按需加载：点谁只读谁，不整批预取')

const calledBefore = agentsCalled(A)
check(
  '★首屏只读了一个 agent 的插件（不是 4 个都预取）',
  new Set(calledBefore).size <= 1,
  'called=' + JSON.stringify(Array.from(new Set(calledBefore)))
)

const xiaomoChip = q(A.container, '[data-agent-pick="xiaomo"]')
check('能找到 xiaomo 那颗按钮', Boolean(xiaomoChip))
await act(async () => {
  xiaomoChip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(A.flush)
await act(A.flush)

const calledAfter = agentsCalled(A)
check(
  '★点了 xiaomo 之后确实发了 /agents/xiaomo/plugins',
  calledAfter.indexOf('xiaomo') !== -1,
  'called=' + JSON.stringify(calledAfter)
)
check(
  '★点 xiaomo 没有顺手把别的 agent 也拉一遍（不整批预取）',
  new Set(calledAfter).size <= 2,
  'distinct=' + JSON.stringify(Array.from(new Set(calledAfter)))
)
check(
  '切过去的按钮被标成选中态（aria-pressed=true）',
  q(A.container, '[data-agent-pick="xiaomo"]').getAttribute('aria-pressed') === 'true'
)
check(
  '另一颗按钮没被标成选中（同一时刻只有一个选中）',
  qa(A.container, '[data-agent-pick][aria-pressed="true"]').length === 1,
  'pressed=' + qa(A.container, '[data-agent-pick][aria-pressed="true"]').length
)
check(
  '★页内选择不改顶部作用域（仍是 code-worker）',
  scopeSelect.value === 'code-worker',
  'scope=' + scopeSelect.value
)
check(
  '页内选择与顶部作用域不同时，界面明说「顶部作用域：…」',
  /顶部作用域：code-worker/.test(setText(A.container)),
  setText(A.container).slice(0, 120)
)

// 插件列表跟着换（xiaomo 的数据）
await act(A.flush)
check(
  '切过去后插件列表来自新 agent（三行都在）',
  qa(A.container, '.hc-set__row').length === 3,
  'rows=' + qa(A.container, '.hc-set__row').length
)

// ══════════════════════════════════════════════ [C] 未选中 = 骨架 + 引导态
console.log('\n[C] 未选中 agent：骨架 + 引导态（不是空列表，也不是 ErrorState）')

const clearBtn = q(A.container, '[data-agent-clear]')
check('有「不选」按钮（可回到引导态）', Boolean(clearBtn))
const callsBeforeClear = pluginCalls(A).length
await act(async () => {
  clearBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(A.flush)
await act(A.flush)

check('★清空后**零新增插件请求**（没选就不读）', pluginCalls(A).length === callsBeforeClear,
  'before=' + callsBeforeClear + ' after=' + pluginCalls(A).length)

const guide = q(A.container, '[data-guide-state]')
check('★出现引导态容器（data-guide-state）', Boolean(guide), guide ? guide.getAttribute('data-guide-state') : 'none')
check('引导态里说的是「选一个 Agent」', /选一个 Agent/.test(setText(A.container)), setText(A.container).slice(0, 120))
check('★引导态里有骨架（.hc-skel），不是空白', Boolean(q(A.container, '.hc-set__guide .hc-skel')))
check(
  '★引导态**不是** ErrorState（没有 hc-state--error）',
  !q(A.container, '.hc-set__guide .hc-state--error')
)
check(
  '★引导态**不是**空列表（没有 .hc-set__list 里的 0 行）',
  !q(A.container, '.hc-set__list')
)
check('引导态说清了「不再整批拉取」的原因', /整批/.test(setText(A.container)))
check(
  '未选中时列表列的状态是 no-agent（探针可断言）',
  q(A.container, '.hc-set__col--list').getAttribute('data-list-state') === 'no-agent'
)

// ══════════════════════════════════════════════ [D] 耗时可见
console.log('\n[D] 耗时可见（明确耗时，不只说"已加载"）')

// 先选一个 agent（上一步是引导态，没有请求也就没有耗时）
await act(async () => {
  q(A.container, '[data-agent-pick="code-worker"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(A.flush)
await act(A.flush)

const elapsed = q(A.container, '[data-elapsed-ms]')
check('★列表标题旁有耗时标签（data-elapsed-ms）', Boolean(elapsed), elapsed ? elapsed.getAttribute('data-elapsed-ms') : 'none')
check(
  '耗时是数字（毫秒），不是占位文字',
  Boolean(elapsed) && /^\d+$/.test(elapsed.getAttribute('data-elapsed-ms') || ''),
  elapsed ? elapsed.getAttribute('data-elapsed-ms') : 'none'
)
check(
  '耗时标签标明了是哪个 agent 的那一次',
  Boolean(q(A.container, '[data-elapsed-agent="code-worker"]')),
  (elapsed || { getAttribute: () => 'none' }).getAttribute('data-elapsed-agent')
)

// 再选一次，确认耗时随请求刷新
await act(async () => {
  q(A.container, '[data-agent-pick="default"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
})
await act(A.flush)
await act(A.flush)
check(
  '选了 default 后耗时标签跟着换成 default 那次',
  Boolean(q(A.container, '[data-elapsed-agent="default"]')),
  'agent=' + (q(A.container, '[data-elapsed-ms]') || { getAttribute: () => 'none' }).getAttribute('data-elapsed-agent')
)
check('default 的插件列表也读回来了', agentsCalled(A).indexOf('default') !== -1, JSON.stringify(agentsCalled(A)))

// ══════════════════════════════════════════════ [E] 刷新：并发不互相踩
console.log('\n[E] 逐个 agent 刷新：每颗按钮各读自己的，零 5xx 抖动')

let errorSeen = false
for (const id of ['default', 'xiaomo', 'code-worker', 'media-worker']) {
  const chip = q(A.container, '[data-agent-pick="' + id + '"]')
  await act(async () => {
    chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(A.flush)
  if (!q(A.container, '.hc-set__row')) errorSeen = true
}
check('★四个 agent 轮流点一遍，每次都能渲染出插件行', !errorSeen)
check(
  '★全程没出现 ErrorState（没有假报错）',
  !q(A.container, '.hc-set .hc-state--error'),
  setText(A.container).slice(0, 160)
)
check(
  '四个 agent 都被点过一遍（每颗按钮真的能读自己的）',
  ['default', 'xiaomo', 'code-worker', 'media-worker'].every((id) => agentsCalled(A).indexOf(id) !== -1),
  JSON.stringify(Array.from(new Set(agentsCalled(A))))
)

// ---------------------------------------------------------------- 收尾
console.log('\n[F] 清理')
try {
  for (const view of [A]) {
    try {
      await act(async () => {
        view.root.unmount()
      })
    } catch (e) {
      /* 卸载失败不该掩盖断言 */
    }
  }
  if (process.platform === 'win32') rmdirSync(join(WORK, 'node_modules'))
  else unlinkSync(join(WORK, 'node_modules'))
  rmSync(WORK, { recursive: true, force: true })
  check('临时桩目录已清理（宿主 node_modules 未被触碰）', true)
} catch (e) {
  check('临时桩目录已清理（宿主 node_modules 未被触碰）', false, String(e))
}

console.log('\n=== 汇总 ===')
console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
if (failures.length) {
  for (const item of failures) console.log('  FAIL ' + item)
  console.log('SETTINGS-ONBOARDING-PROBE-FAILED')
  process.exit(1)
}
console.log('SETTINGS-ONBOARDING-PROBE-OK')
