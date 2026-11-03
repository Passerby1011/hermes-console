/**
 * hermes-console · M4b1 设置分区验收（真 React 19 + jsdom，与 probe-dom.mjs 同一套运行时）
 *
 * 为什么单独一个文件：设置分区的断言几乎全是「三态语义 / 掩码 / 乐观锁」这类只有真 DOM
 * + 真事件才能证明的事，混进 probe-dom 会让那份 127 项的验收越来越难读。
 *
 * 覆盖（对应卡片 9 条）：
 *   [1] state=enabled   → 渲染「已启用」，有可点的「关闭」
 *   [2] state=disabled  → 渲染「已被禁用」，有可点的「开启」
 *   [3] ★state=unavailable → 渲染「不在该 Agent 可用范围」，**没有任何可点的开关**，
 *       且整行不含「已启用」字样（本卡最关键断言）
 *   [4] 三种来源层文案各自正确（模块默认 / 全局 / 本 Agent 覆盖）
 *   [5] masked_keys 字段输入框 disabled，且一次真实提交后请求体里不含 "***"
 *   [6] 409 → 渲染「配置已被别处改动」+ 刷新按钮，且**不自动重试**
 *   [7] 500 → 把后端 error.message 原样显示
 *   [8] 切 agent → 重新拉取，两次不同 mock 响应，列表随之变化
 *   [9] src/ 零硬编码颜色复查 + 三态齐备（骨架/空/错）
 *   [+] catalog 容错：带 source/version/module 额外字段不崩；advisor 插件与 advisor 面板不混淆；
 *       无面板插件（hermes-auto-titler）标注「无控制台模块」并照常可看配置
 *
 * 跑法：node plugins/hermes-console/scripts/probe-settings.mjs
 * 打印 SETTINGS-UI-PROBE-OK / -FAILED，失败退出码非 0。
 */
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, rmdirSync, symlinkSync, unlinkSync } from 'node:fs'
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

// ------------------------------------------------- 临时模块树（junction 到宿主，绝不复制/安装）
const WORK = mkdtempSync(join(tmpdir(), 'hc-settings-'))
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

const React = requireFromWork('react')
const { createRoot } = requireFromWork('react-dom/client')
const { act } = React

// 每次 import 一份独立副本 = 独立模块级缓存（切 agent / 重挂载断言才能干净）
function freshEntry(tag) {
  const dest = join(WORK, 'entry_' + tag + '.js')
  copyFileSync(ENTRY, dest)
  return import(pathToFileURL(dest).href)
}

// --------------------------------------------------------------- 桩数据
const AGENTS = {
  agents: [
    { id: 'default', label: '小白', is_default: true, has_config: true },
    { id: 'xiaomo', label: '墨黎', is_default: false, has_config: true },
    { id: 'code-worker', label: 'code-worker', is_default: false, has_config: true }
  ],
  current: 'code-worker'
}

const MODULES_PAYLOAD = {
  overview: [
    { id: 'overview', label: '概览', nav_order: 10, section: 'overview', enabled: true, reason: '' },
    { id: 'weather', label: '天气', nav_order: 20, section: 'weather', enabled: true, reason: '' },
    { id: 'agents', label: 'Agent 总览', nav_order: 30, section: 'agents', enabled: true, reason: '' },
    { id: 'settings', label: '设置', nav_order: 70, section: 'settings', enabled: true, reason: '' }
  ]
}

/**
 * code-worker 的插件清单：三态齐备 + catalog 字段齐备 + 一个无面板的插件。
 * 关键点：`advisor` 插件（plugin:advisor）与 `advisor` 控制台面板（module:hermes-console/advisor）
 * 裸 id 相同 —— 界面必须能分开。
 */
const PLUGINS_CODE_WORKER = {
  ok: true,
  data: {
    agent: 'code-worker',
    config_digest: 'digest-code-worker',
    exists: true,
    path: 'D:/fake/profiles/code-worker/config.yaml',
    counts: { enabled: 2, disabled: 1, unavailable: 1 },
    skipped: [],
    plugins: [
      {
        plugin_id: 'advisor',
        state: 'enabled',
        in_enabled: true,
        in_disabled: false,
        source: 'enabled_list',
        has_settings: false,
        ref: 'plugin:advisor',
        display_name: 'Advisor',
        kind: 'plugin',
        origin: 'self-developed',
        version: '1.3.0',
        version_source: 'plugin.yaml',
        has_dashboard_module: true,
        module: 'hermes-console/advisor',
        module_id: 'advisor',
        module_ref: 'module:hermes-console/advisor',
        in_catalog: true,
        note: '原生插件：每轮 turn 后跑二次模型评审'
      },
      {
        plugin_id: 'hermes-auto-titler',
        state: 'disabled',
        in_enabled: false,
        in_disabled: true,
        source: 'disabled_list',
        has_settings: true,
        ref: 'plugin:hermes-auto-titler',
        display_name: 'Hermes Auto Titler',
        kind: 'plugin',
        origin: 'self-developed',
        version: '0.3.0',
        version_source: 'plugin.yaml',
        has_dashboard_module: false,
        module: null,
        module_id: null,
        module_ref: null,
        in_catalog: true,
        note: '原生插件：会话标题多轮跟踪。无控制台面板。'
      },
      {
        plugin_id: 'browserpaw',
        state: 'unavailable',
        in_enabled: false,
        in_disabled: false,
        source: 'absent',
        has_settings: false,
        ref: 'plugin:browserpaw',
        display_name: 'browserpaw',
        kind: 'plugin',
        origin: null,
        version: null,
        has_dashboard_module: false,
        module: null,
        module_ref: null,
        in_catalog: false,
        note: null
      }
    ],
    namespaces: { plugin: 'plugin:<plugin_id>', module: 'module:<console_module_id>' },
    catalog: { ok: true, path: 'D:/fake/dashboard/plugins_catalog.json', schema_version: 1, entries: 14, used: 2, error: null, dropped: [] },
    notes: []
  }
}

/** xiaomo 的清单：内容不同（本卡第 8 条靠它证明「切 agent 真的重拉」）。 */
const PLUGINS_XIAOMO = {
  ok: true,
  data: {
    agent: 'xiaomo',
    config_digest: 'digest-xiaomo',
    exists: true,
    path: 'D:/fake/profiles/xiaomo/config.yaml',
    counts: { enabled: 1, disabled: 1, unavailable: 0 },
    skipped: [],
    plugins: [
      {
        plugin_id: 'hermes-auto-titler',
        state: 'enabled',
        in_enabled: true,
        in_disabled: false,
        source: 'enabled_list',
        has_settings: true,
        ref: 'plugin:hermes-auto-titler',
        display_name: 'Hermes Auto Titler',
        version: '0.3.0',
        has_dashboard_module: false,
        module_ref: null,
        in_catalog: true
      },
      {
        plugin_id: 'xiaomo-vitality',
        state: 'disabled',
        in_enabled: false,
        in_disabled: true,
        source: 'disabled_list',
        has_settings: false,
        ref: 'plugin:xiaomo-vitality',
        display_name: 'xiaomo-vitality',
        version: null,
        has_dashboard_module: false,
        module_ref: null,
        in_catalog: false
      }
    ],
    catalog: { ok: true, entries: 14, used: 1, error: null },
    notes: []
  }
}

/** 设置视图：三层来源各一 + 一个脱敏键 + 一个只读键 + 一个结构化值。 */
const SETTINGS_CODE_WORKER = {
  ok: true,
  data: {
    agent: 'code-worker',
    plugin_id: 'hermes-auto-titler',
    settings: {
      every_n_turns: 2,
      strategy: 'conservative',
      title_style: 'concise',
      api_key: '***',
      locked_key: true,
      approved_model_registry: []
    },
    masked_keys: ['api_key'],
    sources: {
      every_n_turns: 'module_default',
      strategy: 'global',
      title_style: 'agent',
      api_key: 'global',
      locked_key: 'module_default',
      approved_model_registry: 'agent'
    },
    writable_keys: ['every_n_turns', 'strategy', 'title_style'],
    readonly_keys: ['locked_key'],
    program_constants: {},
    path: 'D:/fake/profiles/code-worker/config.yaml',
    exists: true,
    expected_config: 'digest-code-worker',
    config_digest: 'digest-code-worker',
    notes: []
  }
}

const SETTINGS_XIAOMO = {
  ok: true,
  data: {
    agent: 'xiaomo',
    plugin_id: 'hermes-auto-titler',
    settings: { every_n_turns: 7, strategy: 'aggressive' },
    masked_keys: [],
    sources: { every_n_turns: 'agent', strategy: 'agent' },
    writable_keys: ['every_n_turns', 'strategy'],
    readonly_keys: [],
    path: 'D:/fake/profiles/xiaomo/config.yaml',
    exists: true,
    expected_config: 'digest-xiaomo',
    config_digest: 'digest-xiaomo',
    notes: []
  }
}

// --------------------------------------------------------------- 桩 ctx
function makeCtx(options) {
  const opts = options || {}
  const calls = []
  const store = Object.assign({}, opts.storage || {})
  return {
    calls,
    ctx: {
      rest(path, apiOpts) {
        const method = (apiOpts && apiOpts.method) || 'GET'
        calls.push({ path, method, body: apiOpts && apiOpts.body })
        const pathname = path.split('?')[0]

        if (pathname === '/modules') {
          return Promise.resolve({ ok: true, data: { agent: 'code-worker', modules: MODULES_PAYLOAD.overview, health: { loaded: [], failed: {} } } })
        }
        if (pathname === '/agents') return Promise.resolve(AGENTS)

        // 插件清单：按 agent 分别给不同 mock（这是第 8 条的桩）
        const listMatch = /^\/agents\/([^/]+)\/plugins$/.exec(pathname)
        if (listMatch) {
          if (opts.failPlugins) return Promise.reject(new Error(opts.failPlugins))
          const agent = decodeURIComponent(listMatch[1])
          return Promise.resolve(agent === 'xiaomo' ? PLUGINS_XIAOMO : PLUGINS_CODE_WORKER)
        }

        // 模块清单（设置分区自己那份）
        const modMatch = /^\/agents\/([^/]+)\/modules$/.exec(pathname)
        if (modMatch) {
          return Promise.resolve({ ok: true, data: { agent: decodeURIComponent(modMatch[1]), modules: MODULES_PAYLOAD.overview, health: { loaded: [], failed: {} } } })
        }

        // 写设置（PUT .../settings）
        if (method === 'PUT' && /\/plugins\/[^/]+\/settings$/.test(pathname)) {
          if (opts.putStatus === 409) {
            return Promise.reject(
              new Error('409: {"ok":false,"error":{"code":"config_changed","message":"配置已变化（乐观锁生效）：提交的 expected_config=\'x\'，当前 y。本次未写入任何内容，请重新读取后重试"}}')
            )
          }
          if (opts.putStatus === 500) {
            return Promise.reject(new Error('500: {"ok":false,"error":{"code":"write_not_applied","message":"写入返回成功但回读不一致（期望 5/int，实际 2/int）：已中止，请人工核对"}}'))
          }
          return Promise.resolve({ ok: true, data: { key: 'x', value: 1, verified: true } })
        }

        // 启停（PUT .../plugins/<plugin>）
        if (method === 'PUT' && /\/plugins\/[^/]+$/.test(pathname)) {
          if (opts.toggleStatus === 500) {
            return Promise.reject(new Error('500: {"ok":false,"error":{"code":"upstream_error","message":"切换插件失败：PluginSwitchAdmissionError: 官方准入通道不可用"}}'))
          }
          if (opts.toggleStatus === 404) {
            // 真实后端当前就是这个响应（启停端点还没建）
            return Promise.reject(new Error('404: {"detail":"Not Found"}'))
          }
          return Promise.resolve({ ok: true, data: { ok: true, changed: true } })
        }

        // 读设置
        const setMatch = /^\/agents\/([^/]+)\/plugins\/([^/]+)\/settings$/.exec(pathname)
        if (setMatch) {
          if (opts.failSettings) return Promise.reject(new Error(opts.failSettings))
          const agent = decodeURIComponent(setMatch[1])
          return Promise.resolve(agent === 'xiaomo' ? SETTINGS_XIAOMO : SETTINGS_CODE_WORKER)
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
    },
    store
  }
}

// ------------------------------------------------------------ 挂载助手
async function mountSettings(tag, options) {
  const pluginApi = await freshEntry(tag)
  const { ctx, calls, store } = makeCtx(options)
  const contributions = []
  pluginApi.default.register(
    Object.assign({}, ctx, {
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
  const Page = function Page() {
    return page.render()
  }
  await act(async () => {
    root.render(React.createElement(Page))
  })
  await act(flush)
  return { root, container, calls, store, pluginApi, flush }
}

/** 等到容器里出现某个选择器（真 effect 是异步的，最多等 rounds 轮）。 */
async function waitFor(container, selector, flush, rounds) {
  const max = rounds || 10
  for (let i = 0; i < max; i += 1) {
    if (container.querySelector(selector)) return true
    await act(flush)
    await act(flush)
  }
  return Boolean(container.querySelector(selector))
}

const q = (container, sel) => container.querySelector(sel)
const qa = (container, sel) => Array.from(container.querySelectorAll(sel))
const text = (container) => container.textContent || ''
/** 设置分区**自己**的文本：整个容器里还有其它分区（概览…），拿全文做否定断言会误判。 */
const setText = (container) => {
  const node = container.querySelector('.hc-set')
  return node ? node.textContent || '' : ''
}

/** 进入设置分区（点导航里的「设置」）。 */
async function openSettings(view) {
  const btn = qa(view.container, '.hc-nav__item').find((n) => /设置/.test(n.textContent || ''))
  if (!btn) return false
  await act(async () => {
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(view.flush)
  await waitFor(view.container, '.hc-set', view.flush)
  return Boolean(q(view.container, '.hc-set'))
}

// ══════════════════════════════════════════════ 用例 A：三态
console.log('\n[A] 三态显式：enabled / disabled / unavailable 绝不压成一个开关')

const A = await mountSettings('a', {})
const aOpened = await openSettings(A)
check('设置分区能从导航进入（shell.js 已登记 settings）', aOpened, text(A.container).slice(0, 160))
check('设置分区拉的是 /agents/<agent>/plugins，不是模块清单', A.calls.some((c) => /^\/agents\/[^/]+\/plugins$/.test(c.path)), JSON.stringify(A.calls.map((c) => c.path)))

const rows = qa(A.container, '.hc-set__row')
check('三行插件全部渲染出来（含无面板、unavailable 的）', rows.length === 3, 'rows=' + rows.length)

const rowOf = (pluginId) => qa(A.container, '.hc-set__row').find((n) => n.getAttribute('data-plugin-id') === pluginId) || null

const enabledRow = rowOf('advisor')
const disabledRow = rowOf('hermes-auto-titler')
const unavailableRow = rowOf('browserpaw')

check('enabled 行存在', Boolean(enabledRow))
check('【1】enabled 行渲染「已启用」', Boolean(enabledRow) && /已启用/.test(enabledRow.textContent || ''), enabledRow ? enabledRow.querySelector('.hc-set__state').textContent : 'no row')
const enabledToggle = enabledRow ? enabledRow.querySelector('[data-toggle-action]') : null
check('【1】enabled 行有可点的「关闭」开关', Boolean(enabledToggle) && enabledToggle.getAttribute('data-toggle-action') === 'disable' && /关闭/.test(enabledToggle.textContent || ''), enabledToggle ? enabledToggle.textContent : 'none')

check('【2】disabled 行渲染「已被禁用」', Boolean(disabledRow) && /已被禁用/.test(disabledRow.textContent || ''), disabledRow ? disabledRow.querySelector('.hc-set__state').textContent : 'no row')
const disabledToggle = disabledRow ? disabledRow.querySelector('[data-toggle-action]') : null
check('【2】disabled 行有可点的「开启」开关', Boolean(disabledToggle) && disabledToggle.getAttribute('data-toggle-action') === 'enable' && /开启/.test(disabledToggle.textContent || ''), disabledToggle ? disabledToggle.textContent : 'none')

// ------- 第 3 条：本卡最关键断言
const unavailableText = unavailableRow ? unavailableRow.textContent || '' : ''
check('【3】unavailable 行渲染「不在该 Agent 可用范围」', /不在该 Agent 可用范围/.test(unavailableText), unavailableRow ? unavailableRow.querySelector('.hc-set__state').textContent : 'no row')
check(
  '【3】★unavailable 行没有任何可点的开关（button 数为 0）',
  Boolean(unavailableRow) && unavailableRow.querySelectorAll('button').length === 0,
  'buttons=' + (unavailableRow ? unavailableRow.querySelectorAll('button').length : 'no row')
)
check(
  '【3】★unavailable 行不出现「已启用」字样（绝不被渲染成已开启）',
  Boolean(unavailableRow) && !/已启用/.test(unavailableText),
  unavailableText.slice(0, 120)
)
check(
  '【3】unavailable 行说明为什么不能开（点名允许列表）',
  /enabled 列表里没有它/.test(unavailableText) && /允许列表/.test(unavailableText),
  unavailableText.slice(0, 200)
)
check(
  '【3】unavailable 行的 data-plugin-state 就是 unavailable（不是 enabled）',
  Boolean(unavailableRow) && unavailableRow.getAttribute('data-plugin-state') === 'unavailable',
  unavailableRow ? unavailableRow.getAttribute('data-plugin-state') : 'no row'
)
check(
  '【3】三态的行样式各不相同（实线 / 虚线 / 点线），不靠颜色单通道',
  Boolean(enabledRow) && Boolean(disabledRow) && Boolean(unavailableRow) &&
    enabledRow.className.indexOf('hc-set__row--enabled') !== -1 &&
    disabledRow.className.indexOf('hc-set__row--disabled') !== -1 &&
    unavailableRow.className.indexOf('hc-set__row--unavailable') !== -1,
  [enabledRow && enabledRow.className, disabledRow && disabledRow.className, unavailableRow && unavailableRow.className].join(' | ')
)

// ══════════════════════════════════════════════ 用例 B：命名空间 / 面板
console.log('\n[B] 命名空间：插件 plugin:* 与面板 module:* 一眼可分；无面板插件照常显示')

check('advisor 插件行标 plugin:advisor', Boolean(enabledRow) && /plugin:advisor/.test(enabledRow.textContent || ''), enabledRow ? enabledRow.querySelector('.hc-set__kind').textContent : '')
check(
  'advisor 行同时标出它的面板 module:hermes-console/advisor（同裸 id 不混淆）',
  Boolean(enabledRow) && /module:hermes-console\/advisor/.test(enabledRow.textContent || ''),
  enabledRow ? enabledRow.textContent.slice(0, 200) : ''
)
check(
  '插件 kind 标识与面板 chip 是两个不同元素（不是同一段文字）',
  Boolean(enabledRow) && enabledRow.querySelectorAll('.hc-set__kind').length === 1 &&
    Array.from(enabledRow.querySelectorAll('.hc-chip')).some((n) => /module:/.test(n.textContent || ''))
)
check(
  '无控制台模块的插件明确标注「无控制台模块」',
  Boolean(disabledRow) && /无控制台模块/.test(disabledRow.textContent || ''),
  disabledRow ? disabledRow.textContent.slice(0, 220) : ''
)
check(
  '无面板插件照常出现、照常可看配置（有「配置」按钮）',
  Boolean(disabledRow) && Boolean(disabledRow.querySelector('[data-select-plugin]')),
  disabledRow ? String(disabledRow.querySelector('[data-select-plugin]')) : 'none'
)
check('catalog 版本号显示出来了（1.3.0 / 0.3.0）', /版本 1\.3\.0/.test(text(A.container)) && /版本 0\.3\.0/.test(text(A.container)), '')
check('catalog 来源显示出来了（self-developed）', /来源 self-developed/.test(text(A.container)))
check('不在自制清单的插件有标注（browserpaw → 不在自制清单）', Boolean(unavailableRow) && /不在自制清单/.test(unavailableRow.textContent || ''), unavailableRow ? unavailableRow.textContent.slice(0, 200) : '')

// ══════════════════════════════════════════════ 用例 C：来源层 / 脱敏 / 只读
console.log('\n[C] 配置来源层 + 脱敏只读')
await waitFor(A.container, '.hc-set__field', A.flush)
const fields = qa(A.container, '.hc-set__field')
check('配置项渲染出来了（6 项）', fields.length === 6, 'fields=' + fields.length)

const fieldOf = (key) => qa(A.container, '.hc-set__field').find((n) => n.getAttribute('data-setting-key') === key) || null
const srcText = (key) => {
  const field = fieldOf(key)
  const node = field ? field.querySelector('.hc-set__src') : null
  return node ? node.textContent.trim() : ''
}

check('【4】module_default → 「模块默认」', srcText('every_n_turns') === '模块默认', srcText('every_n_turns'))
check('【4】global → 「全局」', srcText('strategy') === '全局', srcText('strategy'))
check('【4】agent → 「本 Agent 覆盖」', srcText('title_style') === '本 Agent 覆盖', srcText('title_style'))
check(
  '【4】三种来源样式各不相同（点线 / 虚线 / 描边加粗）',
  fieldOf('every_n_turns') && fieldOf('strategy') && fieldOf('title_style') &&
    fieldOf('every_n_turns').querySelector('.hc-set__src').className.indexOf('--module_default') !== -1 &&
    fieldOf('strategy').querySelector('.hc-set__src').className.indexOf('--global') !== -1 &&
    fieldOf('title_style').querySelector('.hc-set__src').className.indexOf('--agent') !== -1
)

// ------- 第 5 条：脱敏只读
const maskedField = fieldOf('api_key')
const maskedInput = maskedField ? maskedField.querySelector('input') : null
check('【5】masked_keys 字段存在', Boolean(maskedField))
check('【5】脱敏字段的值显示为 ***', Boolean(maskedInput) && maskedInput.value === '***', maskedInput ? maskedInput.value : 'none')
check('【5】脱敏字段输入框 disabled（真 disabled 属性）', Boolean(maskedInput) && maskedInput.disabled === true)
check('【5】脱敏字段旁边注明「已脱敏，请到配置文件修改」', Boolean(maskedField) && /已脱敏，请到配置文件修改/.test(maskedField.textContent || ''), maskedField ? maskedField.textContent.slice(0, 160) : '')
check('【5】脱敏字段没有保存按钮（根本不给提交入口）', Boolean(maskedField) && maskedField.querySelectorAll('button').length === 0, 'buttons=' + (maskedField ? maskedField.querySelectorAll('button').length : 0))

const readonlyField = fieldOf('locked_key')
check('manifest 只读键也 disabled 且说明原因', Boolean(readonlyField) && readonlyField.querySelector('input').disabled === true && /只读/.test(readonlyField.textContent || ''))
const structuralField = fieldOf('approved_model_registry')
check('结构化值（list）也给只读展示，不把 list 当字符串提交', Boolean(structuralField) && structuralField.querySelector('input').disabled === true && /结构化值/.test(structuralField.textContent || ''))

// ------- 第 5 条的后半：真实提交，断言请求体里没有 "***"
const numericInput = fieldOf('every_n_turns') ? fieldOf('every_n_turns').querySelector('input') : null
check('可写项输入框 enabled', Boolean(numericInput) && numericInput.disabled === false)
if (numericInput) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    setter.call(numericInput, '5')
    numericInput.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
  await act(A.flush)
  const saveBtn = fieldOf('every_n_turns').querySelector('[data-field-save]')
  check('可写项有「保存」按钮', Boolean(saveBtn))
  const before = A.calls.length
  await act(async () => {
    saveBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(A.flush)
  const puts = A.calls.slice(before).filter((c) => c.method === 'PUT')
  check('【5】点保存真的发出了 PUT（不是假装成功）', puts.length === 1, 'puts=' + puts.length + ' ' + JSON.stringify(puts))
  const bodyText = puts.length ? JSON.stringify(puts[0].body) : ''
  check('【5】★请求体里不含 "***"', puts.length > 0 && bodyText.indexOf('***') === -1, bodyText)
  check('【5】请求体形状是 {key,value,expected_config}', puts.length > 0 && puts[0].body.key === 'every_n_turns' && puts[0].body.value === 5 && puts[0].body.expected_config === 'digest-code-worker', bodyText)
  check('数字项按数字提交（5），不是字符串 "5"', puts.length > 0 && typeof puts[0].body.value === 'number', puts.length ? typeof puts[0].body.value : 'no put')
}

// 再验证：就算有人把 *** 塞进草稿，前端也不会发出去（buildPutBody 的闸门）。
// 这一条必须用**真实提交路径**打：把 `***` 写进一个可写的字符串字段再点保存 ——
// 只有这样才能证明闸门真的在链路上（变异测试里，跳过这一步的断言抓不到闸门失效）。
const stringField = fieldOf('strategy')
const stringInput = stringField ? stringField.querySelector('input') : null
const stringSave = stringField ? stringField.querySelector('[data-field-save]') : null
check('【5】可写的字符串字段可以输入（拿 strategy 当载体）', Boolean(stringInput) && stringInput.disabled === false && Boolean(stringSave))
if (stringInput && stringSave) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    setter.call(stringInput, '***')
    stringInput.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
  await act(A.flush)
  const putsBefore = A.calls.filter((c) => c.method === 'PUT').length
  await act(async () => {
    stringSave.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(A.flush)
  await act(A.flush)
  const putsAfter = A.calls.filter((c) => c.method === 'PUT').length
  check('【5】★把 *** 输入到可写字段再保存：一个请求都不发（闸门在链路上，不是只在源码里）',
    putsAfter === putsBefore, 'puts before=%d after=%d' % (putsBefore, putsAfter))
  const gateText = (fieldOf('strategy') || { textContent: '' }).textContent || ''
  check('【5】被拦下时界面说清原因（掩码写回等于覆盖真密钥）', /脱敏占位符|不能把脱敏占位符/.test(gateText), gateText.slice(0, 200))
}

// buildPutBody 的源码/产物存在性（第二道保险：后端还有一道 400）
const settingsSrc = readFileSync(join(REPO, 'src/sections/settings.js'), 'utf8')
check(
  '【5】源码里有 "***" 闸门（buildPutBody 见到掩码直接拒绝发请求）',
  /MASK_PLACEHOLDER/.test(settingsSrc) && /buildPutBody/.test(settingsSrc) && /不能把脱敏占位符/.test(settingsSrc)
)
check(
  '【5】产物里也带了这道闸门（不是只在源码里）',
  readFileSync(ENTRY, 'utf8').indexOf('不能把脱敏占位符') !== -1
)

// ══════════════════════════════════════════════ 用例 D：409 / 500
console.log('\n[D] 乐观锁 409 与后端 500 原样透出')

const D409 = await mountSettings('d409', { putStatus: 409 })
await openSettings(D409)
await waitFor(D409.container, '.hc-set__field', D409.flush)
const d409Input = q(D409.container, '.hc-set__field [data-field-input]')
const d409Save = q(D409.container, '.hc-set__field [data-field-save]')
if (d409Input && d409Save) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    setter.call(d409Input, '9')
    d409Input.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
  await act(D409.flush)
  const before409 = D409.calls.filter((c) => c.method === 'PUT').length
  await act(async () => {
    d409Save.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(D409.flush)
  await act(D409.flush)
  const after409 = D409.calls.filter((c) => c.method === 'PUT').length
  check('【6】409 后渲染「配置已被别处改动，请刷新后重试」', /配置已被别处改动，请刷新后重试/.test(setText(D409.container)), setText(D409.container).slice(0, 200))
  check('【6】提示挂在 .hc-set__conflict 块上且 role=alert', Boolean(q(D409.container, '.hc-set__conflict[role="alert"]')))
  check('【6】有刷新按钮（可点的 <button>）', Boolean(q(D409.container, '[data-conflict-refresh]')) && q(D409.container, '[data-conflict-refresh]').tagName === 'BUTTON')
  check('【6】提示里保留后端原话（乐观锁生效）', /乐观锁生效/.test(setText(D409.container)), setText(D409.container).slice(0, 240))
  check('【6】★409 只发了一次 PUT，没有静默重试', after409 - before409 === 1, 'puts=' + (after409 - before409))
  const refreshBtn = q(D409.container, '[data-conflict-refresh]')
  const beforeRefresh = D409.calls.length
  if (refreshBtn) {
    await act(async () => {
      refreshBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
    })
    await act(D409.flush)
    await act(D409.flush)
  }
  check('【6】点刷新会重新拉数据（真的重发请求）', D409.calls.length > beforeRefresh, 'calls before=' + beforeRefresh + ' after=' + D409.calls.length)
  check('【6】刷新后冲突提示消失', !q(D409.container, '.hc-set__conflict'), setText(D409.container).slice(0, 160))
}

const D500 = await mountSettings('d500', { putStatus: 500 })
await openSettings(D500)
await waitFor(D500.container, '.hc-set__field', D500.flush)
const d500Input = q(D500.container, '.hc-set__field [data-field-input]')
const d500Save = q(D500.container, '.hc-set__field [data-field-save]')
if (d500Input && d500Save) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    setter.call(d500Input, '3')
    d500Input.dispatchEvent(new window.Event('input', { bubbles: true }))
  })
  await act(D500.flush)
  await act(async () => {
    d500Save.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(D500.flush)
  await act(D500.flush)
  const dText = setText(D500.container)
  check('【7】500 时把 error.message 原样显示（回读不一致那句）', /写入返回成功但回读不一致/.test(dText), dText.slice(0, 300))
  check('【7】错误态用 ErrorState（.hc-state--error 在 DOM）', qa(D500.container, '.hc-set .hc-state--error').length >= 1, 'errStates=' + qa(D500.container, '.hc-set .hc-state--error').length)
  check('【7】没有自己编原因（不说「保存成功」）', !/保存成功/.test(dText))
  check('【7】后端错误码也带出来了（write_not_applied）', /write_not_applied/.test(dText), dText.slice(0, 320))
}

// 启停失败：后端原话必须挂在**那一行**上，而且界面状态不许翻转（不许假装切换成功）。
// 真实后端目前还没有 PUT /agents/<agent>/plugins/<plugin> 这个端点（实测 404），
// 所以这条断言同时是「点了不会假装成功」的证据。
const DT = await mountSettings('dtoggle', { toggleStatus: 500 })
await openSettings(DT)
await waitFor(DT.container, '.hc-set__row', DT.flush)
const dtTargetRow = qa(DT.container, '.hc-set__row').find((n) => n.getAttribute('data-plugin-id') === 'hermes-auto-titler')
const dtBtn = dtTargetRow ? dtTargetRow.querySelector('[data-toggle-action]') : null
check('启停：disabled 行给出「开启」按钮（动作存在）', Boolean(dtBtn) && /开启/.test(dtBtn.textContent || ''))
if (dtBtn) {
  await act(async () => {
    dtBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(DT.flush)
  await act(DT.flush)
}
const dtPut = DT.calls.filter((c) => c.method === 'PUT')
check('启停：点「开启」真的发了 PUT /agents/<agent>/plugins/<plugin> 并带 enabled + expected_config',
  dtPut.length === 1 && /\/agents\/[^/]+\/plugins\/hermes-auto-titler$/.test(dtPut[0].path) &&
    dtPut[0].body.enabled === true && dtPut[0].body.expected_config === 'digest-code-worker',
  JSON.stringify(dtPut))
const dtRowAfter = qa(DT.container, '.hc-set__row').find((n) => n.getAttribute('data-plugin-id') === 'hermes-auto-titler')
const dtRowText = dtRowAfter ? dtRowAfter.textContent || '' : ''
check('启停失败：原样透出后端 message（准入通道不可用那句）', /官方准入通道不可用/.test(text(DT.container)), text(DT.container).slice(0, 200))
check('启停失败：界面状态没被翻转（该行仍是「已被禁用」，不写「已启用」）',
  Boolean(dtRowAfter) && /已被禁用/.test(dtRowText) && !/已启用/.test(dtRowText) &&
    dtRowAfter.getAttribute('data-plugin-state') === 'disabled',
  dtRowText.slice(0, 160))

// 404：真实后端当前就是这个情况（启停端点还没随 M4b0 建）。必须说明白，不许假装成功。
const D404 = await mountSettings('dtoggle404', { toggleStatus: 404 })
await openSettings(D404)
await waitFor(D404.container, '.hc-set__row', D404.flush)
const d404Row = qa(D404.container, '.hc-set__row').find((n) => n.getAttribute('data-plugin-id') === 'hermes-auto-titler')
const d404Btn = d404Row ? d404Row.querySelector('[data-toggle-action]') : null
if (d404Btn) {
  await act(async () => {
    d404Btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(D404.flush)
  await act(D404.flush)
}
const d404RowAfter = qa(D404.container, '.hc-set__row').find((n) => n.getAttribute('data-plugin-id') === 'hermes-auto-titler')
const d404Text = d404RowAfter ? d404RowAfter.textContent || '' : ''
check('404（后端还没有启停端点）：明说「这次切换没有生效」，不假装成功',
  /后端还没有插件启停端点/.test(d404Text) && /没有生效/.test(d404Text), d404Text.slice(0, 240))
check('404：行状态仍是「已被禁用」（没有乐观地翻成已启用）',
  Boolean(d404RowAfter) && /已被禁用/.test(d404Text) && !/已启用/.test(d404Text), d404Text.slice(0, 160))

// ══════════════════════════════════════════════ 用例 E：切 agent
console.log('\n[E] 切作用域：插件列表与配置跟着该 agent 重拉')
const E = await mountSettings('e', {})
await openSettings(E)
const eRowsBefore = qa(E.container, '.hc-set__row').map((n) => n.getAttribute('data-plugin-id'))
check('code-worker 的列表有 advisor / hermes-auto-titler / browserpaw', eRowsBefore.join(',') === 'advisor,hermes-auto-titler,browserpaw', eRowsBefore.join(','))
await waitFor(E.container, '.hc-set__field', E.flush)
const cwTurnsInput = q(E.container, '[data-field-input="every_n_turns"]')
check('code-worker 的配置项 every_n_turns = 2', Boolean(cwTurnsInput) && cwTurnsInput.value === '2', cwTurnsInput ? cwTurnsInput.value : 'none')

const scopeSelect = q(E.container, '.hc-select')
check('顶部作用域是真实 <select>', Boolean(scopeSelect) && scopeSelect.tagName === 'SELECT')
if (scopeSelect) {
  await act(async () => {
    scopeSelect.value = 'xiaomo'
    scopeSelect.dispatchEvent(new window.Event('change', { bubbles: true }))
  })
  await act(E.flush)
  await act(E.flush)
  await waitFor(E.container, '.hc-set__row', E.flush)
}
const eRowsAfter = qa(E.container, '.hc-set__row').map((n) => n.getAttribute('data-plugin-id'))
check('【8】切到 xiaomo 后列表真的变了（两次不同 mock 响应）', eRowsAfter.join(',') === 'hermes-auto-titler,xiaomo-vitality', eRowsAfter.join(','))
check('【8】切 agent 后确实重发了 /agents/xiaomo/plugins', E.calls.some((c) => c.path === '/agents/xiaomo/plugins'), JSON.stringify(E.calls.filter((c) => /plugins$/.test(c.path)).map((c) => c.path)))
await waitFor(E.container, '.hc-set__field', E.flush)
const xiaomoTurnsInput = q(E.container, '[data-field-input="every_n_turns"]')
check('【8】切 agent 后配置值也换成 xiaomo 的（7，不是上一个 agent 的 2）', Boolean(xiaomoTurnsInput) && xiaomoTurnsInput.value === '7', xiaomoTurnsInput ? xiaomoTurnsInput.value : 'none')
check('【8】xiaomo 的行状态跟着变（hermes-auto-titler 在这是 enabled）',
  qa(E.container, '.hc-set__row').some((n) => n.getAttribute('data-plugin-id') === 'hermes-auto-titler' && /已启用/.test(n.textContent || '')),
  qa(E.container, '.hc-set__row').map((n) => n.getAttribute('data-plugin-id') + '=' + (n.querySelector('.hc-set__state') || {}).textContent).join(' | '))
check('【8】切 agent 后不再显示上一个 agent 才有的插件（browserpaw 消失）', !qa(E.container, '.hc-set__row').some((n) => n.getAttribute('data-plugin-id') === 'browserpaw'))

// ══════════════════════════════════════════════ 用例 F：三态齐备 + 容错
console.log('\n[F] 三态齐备（骨架 / 空 / 错）+ catalog 字段容错')

// 空态：清单里一个插件都没有
const emptyApi = await freshEntry('fempty')
{
  const contributions = []
  const calls = []
  emptyApi.default.register(
    Object.assign({}, {
      rest(path, apiOpts) {
        const method = (apiOpts && apiOpts.method) || 'GET'
        calls.push({ path, method })
        const pathname = path.split('?')[0]
        if (pathname === '/modules' || /^\/agents\/[^/]+\/modules$/.test(pathname)) {
          return Promise.resolve({ ok: true, data: { modules: MODULES_PAYLOAD.overview, agent: 'code-worker' } })
        }
        if (pathname === '/agents') return Promise.resolve(AGENTS)
        if (/^\/agents\/[^/]+\/plugins$/.test(pathname)) {
          return Promise.resolve({ ok: true, data: { agent: 'code-worker', plugins: [], counts: { enabled: 0, disabled: 0, unavailable: 0 }, exists: false, path: 'D:/fake/none.yaml', config_digest: 'd', skipped: [], notes: [] } })
        }
        return Promise.resolve({ ok: true, stub: true })
      },
      storage: { get: (k, f) => f, set() {}, remove() {} },
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
    root.render(React.createElement(function Page() { return page.render() }))
  })
  await act(flush)
  const view = { container, root, calls, flush }
  await openSettings(view)
  await waitFor(container, '.hc-set', flush)
  check('【三态】空态：没有任何插件时给空态（不是空白）', Boolean(q(container, '.hc-state')), text(container).slice(0, 200))
  check('【三态】空态说清为什么空 + 下一步', /plugins\.entries/.test(text(container)) || /没有可看的东西|没有插件/.test(text(container)), text(container).slice(0, 240))
  check('【三态】空态给了一个动作按钮', Boolean(q(container, '.hc-state button')))
  await act(async () => { root.unmount() })
  container.remove()
}

const FErr = await mountSettings('ferr', { failPlugins: '500: {"ok":false,"error":{"code":"upstream_error","message":"列举插件失败：RuntimeError: config 核心不可用"}}' })
await openSettings(FErr)
await waitFor(FErr.container, '.hc-state--error', FErr.flush)
check('【三态】错误态：后端失败渲染 ErrorState（不是白屏）', qa(FErr.container, '.hc-state--error').length >= 1, text(FErr.container).slice(0, 200))
check('【三态】错误态原样显示后端 message', /列举插件失败：RuntimeError: config 核心不可用/.test(text(FErr.container)), text(FErr.container).slice(0, 260))
check('【三态】错误态有重试按钮', Boolean(q(FErr.container, '.hc-state button')))
const retryBtn = q(FErr.container, '.hc-state button')
const beforeRetry = FErr.calls.length
if (retryBtn) {
  await act(async () => {
    retryBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  })
  await act(FErr.flush)
  await act(FErr.flush)
}
check('【三态】点重试真的重发请求', FErr.calls.length > beforeRetry, 'before=' + beforeRetry + ' after=' + FErr.calls.length)

// 容错：后端只给 M4b0 的六个键（没有 catalog 字段）时也必须照常渲染
{
  const bareApi = await freshEntry('bare')
  const contributions = []
  bareApi.default.register(
    Object.assign({}, {
      rest(path, apiOpts) {
        const method = (apiOpts && apiOpts.method) || 'GET'
        const pathname = path.split('?')[0]
        if (pathname === '/modules' || /^\/agents\/[^/]+\/modules$/.test(pathname)) {
          return Promise.resolve({ ok: true, data: { modules: MODULES_PAYLOAD.overview, agent: 'code-worker' } })
        }
        if (pathname === '/agents') return Promise.resolve(AGENTS)
        if (/^\/agents\/[^/]+\/plugins$/.test(pathname)) {
          return Promise.resolve({
            ok: true,
            data: {
              agent: 'code-worker',
              config_digest: 'bare-digest',
              exists: true,
              path: 'D:/fake/profiles/code-worker/config.yaml',
              counts: { enabled: 1, disabled: 0, unavailable: 0 },
              skipped: [],
              plugins: [
                // 只有 M4b0 的六个键，一个 catalog 字段都没有
                { plugin_id: 'bare-plugin', state: 'enabled', in_enabled: true, in_disabled: false, source: 'enabled_list', has_settings: false }
              ],
              notes: []
            }
          })
        }
        if (/\/settings$/.test(pathname)) {
          return Promise.resolve({ ok: true, data: { settings: {}, masked_keys: [], sources: {}, writable_keys: [], readonly_keys: [], notes: [], expected_config: 'x' } })
        }
        return Promise.resolve({ ok: true, stub: true })
      },
      storage: { get: (k, f) => f, set() {}, remove() {} },
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
    root.render(React.createElement(function Page() { return page.render() }))
  })
  await act(flush)
  const view = { container, root, calls: [], flush }
  await openSettings(view)
  await waitFor(container, '.hc-set', flush)
  const bareRow = q(container, '.hc-set__row')
  check('【容错】后端只有 M4b0 六键时页面照常渲染（没有 catalog 字段也不崩）', Boolean(bareRow), text(container).slice(0, 200))
  check('【容错】缺 ref 时按 plugin:<id> 生成命名空间标识', Boolean(bareRow) && /plugin:bare-plugin/.test(bareRow.textContent || ''), bareRow ? bareRow.textContent.slice(0, 160) : '')
  check('【容错】缺 catalog 字段时也能判出面板（按 /modules 匹配）或明说没有', Boolean(bareRow) && /(module:|无控制台模块|面板信息未读到)/.test(bareRow.textContent || ''), bareRow ? bareRow.textContent.slice(0, 200) : '')
  await act(async () => { root.unmount() })
  container.remove()
}

// 容错：多出未知字段也不崩
{
  const extraApi = await freshEntry('extra')
  const contributions = []
  extraApi.default.register(
    Object.assign({}, {
      rest(path) {
        const pathname = path.split('?')[0]
        if (pathname === '/modules' || /^\/agents\/[^/]+\/modules$/.test(pathname)) {
          return Promise.resolve({ ok: true, data: { modules: MODULES_PAYLOAD.overview, agent: 'code-worker' } })
        }
        if (pathname === '/agents') return Promise.resolve(AGENTS)
        if (/^\/agents\/[^/]+\/plugins$/.test(pathname)) {
          return Promise.resolve({
            ok: true,
            data: {
              agent: 'code-worker',
              config_digest: 'x',
              exists: true,
              plugins: [
                Object.assign(
                  { plugin_id: 'unknown-fields', state: 'unavailable', in_enabled: false, in_disabled: false, source: 'absent', has_settings: false },
                  // 后端将来可能加的未知字段 + 嵌套结构
                  { future_field: { nested: [1, 2, 3] }, another_one: 'whatever', catalog_v2: { x: true } }
                )
              ],
              counts: { enabled: 0, disabled: 0, unavailable: 1 }
            }
          })
        }
        return Promise.resolve({ ok: true, stub: true })
      },
      storage: { get: (k, f) => f, set() {}, remove() {} },
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
    root.render(React.createElement(function Page() { return page.render() }))
  })
  await act(flush)
  await openSettings({ container, root, calls: [], flush })
  await waitFor(container, '.hc-set', flush)
  check('【容错】后端多返回未知字段时不崩（照常渲染该行）', Boolean(q(container, '.hc-set__row')), text(container).slice(0, 200))
  // 断言范围收在**那一行**上：分区里还有计数条（「已启用 0」是计数，不是状态文案），
  // 拿整块文本找「已启用」会把计数条算进来 —— 那是探针写错，不是产品错。
  const extraRow = q(container, '.hc-set__row')
  const extraRowText = extraRow ? extraRow.textContent || '' : ''
  check('【容错】多字段下 unavailable 语义仍然正确（该行不出「已启用」、仍给第三态文案、仍无开关）',
    Boolean(extraRow) && !/已启用/.test(extraRowText) &&
      /不在该 Agent 可用范围/.test(extraRowText) &&
      extraRow.querySelectorAll('button').length === 0,
    extraRowText.slice(0, 200))
  await act(async () => { root.unmount() })
  container.remove()
}

// ══════════════════════════════════════════════ 用例 G：窄窗 / 颜色
console.log('\n[G] 视觉规范：窄窗不横向滚动 / 零硬编码颜色')

const css = readFileSync(join(REPO, 'dashboard/dist/style.css'), 'utf8')
check('设置分区样式进了产物（.hc-set 规则存在）', /\.hc-set\s*\{/.test(css))
check('三态行样式各自存在（solid / dashed / dotted）',
  /\.hc-set__row--enabled\s*\{[^}]*border-style:\s*solid/.test(css) &&
  /\.hc-set__row--disabled\s*\{[^}]*border-style:\s*dashed/.test(css) &&
  /\.hc-set__row--unavailable\s*\{[^}]*border-style:\s*dotted/.test(css))
check('来源层三种样式各自存在',
  /\.hc-set__src--module_default\s*\{[^}]*border-style:\s*dotted/.test(css) &&
  /\.hc-set__src--global\s*\{[^}]*border-style:\s*dashed/.test(css) &&
  /\.hc-set__src--agent\s*\{/.test(css))
check('窄窗 <900px 有专门的设置分区规则（不横向滚动）', /@container hc \(max-width:\s*899px\)[\s\S]*?\.hc-set__input\s*\{\s*flex:\s*1 1 100%/.test(css))
check('设置分区网格在窄窗是单列（minmax(0,1fr)，不会撑出横向滚动）',
  /\.hc-set__grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/.test(css))
check('设置分区所有容器都 min-width: 0（grid/flex 子项不撑破）', (css.match(/\.hc-set__[a-z]+\s*\{[^}]*min-width:\s*0/g) || []).length >= 4,
  'hits=' + (css.match(/\.hc-set__[a-z]+\s*\{[^}]*min-width:\s*0/g) || []).length)

// 零硬编码颜色：src/ 全量扫 + 产物扫（口径与 build.mjs 一致）
function walk(dir, out) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) walk(full, out)
    else out.push(full)
  }
  return out
}
const COLOR_RES = [/#[0-9a-fA-F]{3,8}\b/g, /\brgba?\s*\(/g, /\bhsla?\s*\(/g]
const colorHits = []
for (const file of walk(join(REPO, 'src'), [])) {
  if (!/\.(css|js)$/.test(file)) continue
  const body = readFileSync(file, 'utf8')
  for (const re of COLOR_RES) {
    re.lastIndex = 0
    let hit
    while ((hit = re.exec(body)) !== null) colorHits.push(file.replace(/\\/g, '/') + ':' + hit[0])
  }
}
check('【9】src/ 零硬编码颜色（#hex / rgb() / hsl() 命中 0）', colorHits.length === 0, colorHits.slice(0, 6).join(', '))
check('【9】settings.js 自己也不含字面色值', !/#[0-9a-fA-F]{3,8}\b/.test(readFileSync(join(REPO, 'src/sections/settings.js'), 'utf8')) && !/\brgba?\s*\(/.test(readFileSync(join(REPO, 'src/sections/settings.js'), 'utf8')))

// ══════════════════════════════════════════════ 收尾
console.log('\n[H] 清理')
for (const view of [A, D409, D500, DT, D404, E, FErr]) {
  try {
    await act(async () => {
      view.root.unmount()
    })
  } catch (e) {
    /* 卸载失败不该掩盖前面的断言结果 */
  }
}
try {
  // junction 必须用 rmdir/unlink 摘链接本身 —— rmtree 会走进去删宿主的 node_modules
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
  failures.forEach((f) => console.log('  ! ' + f))
  console.log('SETTINGS-UI-PROBE-FAILED')
  process.exitCode = 1
} else {
  console.log('SETTINGS-UI-PROBE-OK')
}
