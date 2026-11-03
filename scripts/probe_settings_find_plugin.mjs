#!/usr/bin/env node
/**
 * hermes-console · 「自动对话命名插件在设置页找得到吗」真浏览器验收 + 截图
 *
 * 跑法（仓库根目录下，或任意目录，脚本自己定位）::
 *
 *     node plugins/hermes-console/scripts/probe_settings_find_plugin.mjs
 *     node plugins/hermes-console/scripts/probe_settings_find_plugin.mjs --shots D:/tmp/shots
 *
 * 这张卡（t_8ab8f9bd）问的是「老板在设置页看不到自动对话命名插件」。所以本探针**只验两件事**：
 *
 *   [A] 它**在**（不是插件缺失）：真实 home 的 `GET /agents/default/plugins` 里就有
 *       `hermes-auto-titler`（state=enabled / in_catalog=true / has_settings=true），
 *       设置页左列**真的渲染了这一行**；只是 51 行、名字是英文、没有搜索框 ⇒ 翻不到。
 *   [B] 补完可见性后**找得到、改得动**：左列顶部搜索框打「命名」⇒ 只剩它这一行；
 *       点「配置」⇒ 右列是它的 23 个可写项；改一项（every_n_turns 2→3）⇒ 保存 ⇒
 *       **从 config.yaml 回读为 3**（不是前端自说自话），再「恢复为全局值」⇒ 回读为默认。
 *
 * 三层都是真的：
 *
 *   1. **真源码**：esbuild 现打 `src/main.js`（= 统一构建时 `node scripts/build.mjs` 的输入）。
 *      本卡明令**不许跑 build.mjs**（四个 worker 并行会互相覆盖 dist/index.js），所以本探针
 *      **不读也不写** `dashboard/dist/`，直接在临时目录里从 `src/` 出包再加载。
 *   2. **真后端**：`scripts/settings_ui_fixture.py` 加载**真实的** `dashboard/plugin_api.py`。
 *      [A] 段直打**真实 HERMES_HOME**（设置分区是纯读）；[B] 段直打**沙盒 home**
 *      （`config.yaml` 是真实那份的逐字节副本 + `plugins/` 是指向真实插件目录的 junction），
 *      写入只落在沙盒副本上 —— 真实 home 的 config.yaml 在前后各抓一次 sha256，必须一致。
 *   3. **真浏览器**：Playwright + Chromium（本机用系统 Edge，同为 Chromium 内核），1440×900。
 *
 * 硬约束：本探针**不新建任何 dashboard 模块**，也不伪造插件行 —— 界面上的每一行都来自
 * `GET /agents/<agent>/plugins` 的真实响应。
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const HOME = resolve(PLUGIN, '..', '..') // <home>
const SRC_MAIN = join(PLUGIN, 'src', 'main.js')
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const ESBUILD = process.env.HERMES_ESBUILD || join(HOST_MODULES, '@esbuild', 'win32-x64', 'esbuild.exe')
const API = '/api/plugins/hermes-console'

const REAL_FIXTURE_PORT = Number(process.env.HC_FIND_FIXTURE_PORT || 9195)
const REAL_SITE_PORT = Number(process.env.HC_FIND_SITE_PORT || 9194)
const SBX_FIXTURE_PORT = Number(process.env.HC_FIND_SBX_FIXTURE_PORT || 9197)
const SBX_SITE_PORT = Number(process.env.HC_FIND_SBX_SITE_PORT || 9196)

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'find-plugin')

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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const sha256 = (file) => createHash('sha256').update(readFileSync(file)).digest('hex')

function shaOrMissing(file) {
  try {
    return sha256(file)
  } catch (e) {
    return '<missing>'
  }
}

/** 真实 home 的 config.yaml + 每个 profile 的 config.yaml 的 sha256 快照（生产零污染用）。 */
function productionSnapshot() {
  const out = { [join(HOME, 'config.yaml')]: shaOrMissing(join(HOME, 'config.yaml')) }
  const profilesRoot = join(HOME, 'profiles')
  if (existsSync(profilesRoot)) {
    for (const name of readdirSync(profilesRoot)) {
      const file = join(profilesRoot, name, 'config.yaml')
      if (existsSync(file)) out[file] = shaOrMissing(file)
    }
  }
  return out
}

// ------------------------------------------------------------------ 临时工作区
const WORK = mkdtempSync(join(tmpdir(), 'hc-find-plugin-'))
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

/** 从**源码**出包（不碰 dashboard/dist/）。 */
function buildEntryFromSource() {
  return new Promise((res, rej) => {
    const args = [
      SRC_MAIN,
      '--bundle',
      '--format=esm',
      '--platform=browser',
      '--target=chrome110',
      '--jsx=automatic',
      '--loader:.css=text',
      '--external:react',
      '--external:react/jsx-runtime',
      '--external:@hermes/plugin-sdk',
      '--legal-comments=none',
      '--charset=utf8',
      '--outfile=' + join(WORK, 'entry.js')
    ]
    const child = spawn(ESBUILD, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    let err = ''
    child.stdout.on('data', () => {})
    child.stderr.on('data', (c) => {
      err += c
    })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild 从 src 出包失败：' + code + '\n' + err))))
  })
}

const HARNESS = `
import plugin, { injectConsoleStyles } from './entry.js'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

const store = {}
const pages = []
const requests = []
const statuses = []
const ctx = {
  rest: async (path, opts) => {
    const options = opts || {}
    const method = options.method || 'GET'
    const init = { method, headers: {} }
    if (options.body !== undefined) {
      init.headers['content-type'] = 'application/json'
      init.body = JSON.stringify(options.body)
    }
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    requests.push({ method, path, status: res.status, body: options.body === undefined ? null : options.body })
    statuses.push(res.status)
    if (!res.ok) throw new Error(res.status + ': ' + text)
    return text ? JSON.parse(text) : null
  },
  storage: {
    get: (key, fallback) => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : fallback),
    set: (key, value) => { store[key] = value },
    remove: (key) => { delete store[key] }
  },
  registerMany: (items) => {
    for (const item of items || []) if (item.area === 'routes') pages.push(item)
  }
}
plugin.register(ctx)
injectConsoleStyles()
window.__hcRequests = requests
window.__hcStatuses = statuses
const host = document.getElementById('root')
createRoot(host).render(createElement(pages[0].render))
window.__hcBooted = true
`

async function buildHarness() {
  writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')
  await new Promise((res, rej) => {
    const child = spawn(ESBUILD, [
      join(WORK, 'harness.js'),
      '--bundle',
      '--format=esm',
      '--platform=browser',
      '--target=chrome110',
      '--jsx=automatic',
      '--loader:.css=text',
      '--legal-comments=none',
      '--charset=utf8',
      '--outfile=' + join(WORK, 'harness.bundle.js')
    ], { cwd: WORK, stdio: ['ignore', 'pipe', 'pipe'] })
    let err = ''
    child.stderr.on('data', (c) => {
      err += c
    })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild harness 失败：' + code + '\n' + err))))
  })
}

// ------------------------------------------------------- 静态站 + 反代（每个 home 一套）
function startSite(sitePort, fixturePort) {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · 设置（找插件验收）</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head>
<body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script>
</body></html>`

  const server = createServer((req, res) => {
    const safe = (fn) => {
      try {
        if (!res.writableEnded && !res.destroyed) fn()
      } catch (e) {
        /* 客户端已断开 */
      }
    }
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: fixturePort, path: url, method: req.method, headers: req.headers },
        (up) => {
          safe(() => {
            res.writeHead(up.statusCode || 502, up.headers)
            up.pipe(res)
          })
        }
      )
      upstream.on('error', (e) => {
        safe(() => {
          res.writeHead(502, { 'content-type': 'text/plain' })
          res.end('upstream error: ' + e.message)
        })
      })
      req.pipe(upstream)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      safe(() => {
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
        res.end(html)
      })
      return
    }
    const map = { '/theme.css': themeCss, '/harness.bundle.js': join(WORK, 'harness.bundle.js') }
    const file = map[url.split('?')[0]]
    if (file && existsSync(file)) {
      safe(() => {
        res.writeHead(200, {
          'content-type': url.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8'
        })
        res.end(readFileSync(file))
      })
      return
    }
    safe(() => {
      res.writeHead(404, { 'content-type': 'text/plain' })
      res.end('not found: ' + url)
    })
  })
  return new Promise((res) => server.listen(sitePort, '127.0.0.1', () => res(server)))
}

// ------------------------------------------------------------------ 后端夹具进程
function startFixture(port, homeOverride) {
  const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' })
  if (homeOverride) env.HERMES_HOME = homeOverride
  const child = spawn(PYTHON, [join(HERE, 'settings_ui_fixture.py'), '--port', String(port)], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env
  })
  let out = ''
  child.stdout.on('data', (c) => {
    out += c
  })
  child.stderr.on('data', (c) => {
    out += c
  })
  return { child, log: () => out }
}

async function waitForFixture(fixture, port) {
  const url = 'http://127.0.0.1:' + port + API + '/health'
  for (let i = 0; i < 160; i += 1) {
    try {
      const res = await fetch(url)
      if (res.ok) return true
    } catch (e) {
      /* 还没起来 */
    }
    await sleep(250)
  }
  throw new Error('后端夹具没起来：\n' + fixture.log())
}

/** 沙盒 home：真实 config.yaml 的逐字节副本 + 指向真实 plugins/ 的 junction（插件本体只读）。 */
function makeSandbox() {
  const box = mkdtempSync(join(tmpdir(), 'hc-find-sbx-'))
  copyFileSync(join(HOME, 'config.yaml'), join(box, 'config.yaml'))
  symlinkSync(join(HOME, 'plugins'), join(box, 'plugins'), 'junction')
  return box
}

// ---------------------------------------------------------------------- 主流程
const consoleErrors = []
const pageErrors = []
const children = []
const productionBefore = productionSnapshot()
let browser = null

async function openSettings(sitePort) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))
  await page.goto('http://127.0.0.1:' + sitePort + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await page.click('.hc-nav__item:has-text("设置")')
  await page.waitForSelector('[data-agent-bar]', { timeout: 20000 })
  return page
}

async function pickAgent(page, agent) {
  await page.waitForSelector('[data-agent-pick="' + agent + '"]', { timeout: 20000 })
  await page.click('[data-agent-pick="' + agent + '"]')
  await page.waitForSelector('.hc-set__row', { timeout: 30000 })
  await sleep(300)
}

try {
  console.log('[env] src     :', SRC_MAIN)
  console.log('[env] shots   :', SHOTS)
  console.log('[env] 注意    : 本探针不读不写 dashboard/dist/（本卡禁止并行 build）')
  await buildEntryFromSource()
  await buildHarness()

  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })

  // =========================================================== [A] 真实 home：它在
  const realFixture = startFixture(REAL_FIXTURE_PORT, null)
  children.push(realFixture.child)
  await waitForFixture(realFixture, REAL_FIXTURE_PORT)
  const realSite = await startSite(REAL_SITE_PORT, REAL_FIXTURE_PORT)

  const apiList = await (await fetch('http://127.0.0.1:' + REAL_FIXTURE_PORT + API + '/agents/default/plugins')).json()
  const listed = (apiList.data || apiList).plugins || []
  const titlerApi = listed.find((item) => item.plugin_id === 'hermes-auto-titler')
  console.log('\n[A] 真实 home：后端清单里有没有它（GET /agents/default/plugins）')
  check('★后端清单里就有 hermes-auto-titler（不是插件缺失）', Boolean(titlerApi), '共 ' + listed.length + ' 个插件')
  if (titlerApi) {
    console.log('        ' + JSON.stringify({
      state: titlerApi.state,
      in_catalog: titlerApi.in_catalog,
      in_enabled: titlerApi.in_enabled,
      has_settings: titlerApi.has_settings,
      display_name: titlerApi.display_name,
      aliases: titlerApi.aliases,
      version: titlerApi.version
    }))
  }
  check('它的三态是 enabled（这个 agent 的允许列表里有它）', Boolean(titlerApi) && titlerApi.state === 'enabled', String(titlerApi && titlerApi.state))
  check('它 in_catalog=true（在 dashboard/plugins_catalog.json 里）', Boolean(titlerApi) && titlerApi.in_catalog === true)
  check('它 has_settings=true（plugin.yaml 的 config_schema 声明了可写项）', Boolean(titlerApi) && titlerApi.has_settings === true)
  check('★catalog 声明了中文别名（老板说的「自动对话命名」能找到）',
    Boolean(titlerApi) && Array.isArray(titlerApi.aliases) && titlerApi.aliases.indexOf('自动对话命名') !== -1,
    JSON.stringify(titlerApi && titlerApi.aliases))

  const pageA = await openSettings(REAL_SITE_PORT)
  await pickAgent(pageA, 'default')

  const before = await pageA.evaluate(() => ({
    rows: document.querySelectorAll('.hc-set__row').length,
    hasFilter: Boolean(document.querySelector('[data-plugin-filter]')),
    hasRow: Boolean(document.querySelector('.hc-set__row[data-plugin-id="hermes-auto-titler"]')),
    filterCount: document.querySelector('[data-filter-count]')
      ? document.querySelector('[data-filter-count]').getAttribute('data-filter-count')
      : null
  }))
  check('设置页左列真的渲染了 50+ 行（清单很长，这是「翻不到」的根因）', before.rows > 40, 'rows=' + before.rows)
  check('未筛选时这一行也在 DOM 里（插件确实在清单里）', before.hasRow)
  check('★左列顶部有搜索框（本卡补的可见性入口）', before.hasFilter)
  check('未筛选时命中数 = 总行数', String(before.filterCount) === String(before.rows), before.filterCount + ' / ' + before.rows)

  // 打「命名」——老板嘴里的名字
  await pageA.fill('[data-plugin-filter]', '命名')
  await sleep(400)
  const hit = await pageA.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('.hc-set__row'))
    return {
      rows: rows.length,
      ids: rows.map((r) => r.getAttribute('data-plugin-id')),
      name: rows.length ? (rows[0].querySelector('.hc-set__display') || {}).textContent || '' : '',
      count: document.querySelector('[data-filter-count]')
        ? document.querySelector('[data-filter-count]').getAttribute('data-filter-count')
        : null
    }
  })
  check('★搜「命名」只命中自动对话命名这一个插件', hit.rows === 1 && hit.ids[0] === 'hermes-auto-titler', JSON.stringify(hit.ids))
  check('★行标题显示中文名「自动对话命名」', hit.name.indexOf('自动对话命名') !== -1, hit.name)
  check('命中数标签同步（命中 1 / 共 ' + before.rows + '）', String(hit.count) === '1', String(hit.count))
  await pageA.screenshot({ path: join(SHOTS, '01-search-hit.png') })

  // 点「配置」→ 右列是它的可写项
  await pageA.click('[data-select-plugin="hermes-auto-titler"]')
  await pageA.waitForSelector('[data-field-input="every_n_turns"]', { timeout: 20000 })
  await sleep(300)
  const panel = await pageA.evaluate(() => {
    const title = document.querySelector('.hc-set__col--detail .hc-set__listtitle')
    const fields = Array.from(document.querySelectorAll('[data-setting-key]'))
    const every = document.querySelector('[data-setting-key="every_n_turns"]')
    return {
      title: title ? title.textContent : '',
      fields: fields.length,
      keys: fields.map((f) => f.getAttribute('data-setting-key')),
      everyValue: every ? every.querySelector('[data-field-input]').value : null,
      everySource: every ? every.getAttribute('data-source') : null,
      saveBtn: Boolean(document.querySelector('[data-field-save="every_n_turns"]'))
    }
  })
  check('★右列是 plugin:hermes-auto-titler 的配置（不是别的插件）', panel.title.indexOf('plugin:hermes-auto-titler') !== -1, panel.title)
  check('★右列有 20+ 个可写项（开关 / 触发时机 / 用哪个模型都在里面）', panel.fields >= 20, 'fields=' + panel.fields)
  check('「触发时机」every_n_turns 在列（当前值 2，来自模块默认）',
    panel.everyValue === '2' && panel.everySource === 'module_default', panel.everyValue + ' / ' + panel.everySource)
  check('有「保存」按钮（可写，不是只读摆设）', panel.saveBtn)
  await pageA.screenshot({ path: join(SHOTS, '02-titler-config.png') })

  // 「只看可配置」
  await pageA.fill('[data-plugin-filter]', '')
  await pageA.click('[data-only-config]')
  await sleep(400)
  const onlyCfg = await pageA.evaluate(() => ({
    rows: Array.from(document.querySelectorAll('.hc-set__row')).map((r) => r.getAttribute('data-plugin-id')),
    flag: document.querySelector('[data-only-config]').getAttribute('data-only-config')
  }))
  check('★「只看可配置」把它留在列表里（它是少数真的可配置的插件）',
    onlyCfg.flag === '1' && onlyCfg.rows.indexOf('hermes-auto-titler') !== -1,
    'rows=' + onlyCfg.rows.length + ' ' + JSON.stringify(onlyCfg.rows))
  await pageA.screenshot({ path: join(SHOTS, '03-only-configurable.png') })

  check('★[A] 段零 console error', consoleErrors.length === 0, JSON.stringify(consoleErrors))
  check('★[A] 段零 pageerror', pageErrors.length === 0, JSON.stringify(pageErrors))
  await pageA.close()
  realSite.close()
  realFixture.child.kill()

  // ================================================== [B] 沙盒 home：改一项并回读
  const sandbox = makeSandbox()
  const sandboxConfig = join(sandbox, 'config.yaml')
  const sandboxBefore = sha256(sandboxConfig)
  console.log('\n[B] 沙盒 home（真实 config.yaml 的逐字节副本）：改一项并回读')
  console.log('        sandbox:', sandbox)
  const sbxFixture = startFixture(SBX_FIXTURE_PORT, sandbox)
  children.push(sbxFixture.child)
  await waitForFixture(sbxFixture, SBX_FIXTURE_PORT)
  const sbxSite = await startSite(SBX_SITE_PORT, SBX_FIXTURE_PORT)

  const pageB = await openSettings(SBX_SITE_PORT)
  await pickAgent(pageB, 'default')
  await pageB.fill('[data-plugin-filter]', '命名')
  await sleep(400)
  await pageB.click('[data-select-plugin="hermes-auto-titler"]')
  await pageB.waitForSelector('[data-field-input="every_n_turns"]', { timeout: 20000 })
  await sleep(300)

  await pageB.fill('[data-field-input="every_n_turns"]', '3')
  await pageB.click('[data-field-save="every_n_turns"]')
  // 等回读：值 = 3，且来源不再是「模块默认」。
  // 注意：沙盒里 `default` agent 的配置**就是**那份 config.yaml（没有 profile 覆盖层），
  // 所以写入后来源是「全局」（global）而不是「本 Agent 覆盖」（agent）——两者都算真的落了库，
  // 这里只断言「不再是 module_default」。
  const saved = await pageB
    .waitForFunction(
      () => {
        const field = document.querySelector('[data-setting-key="every_n_turns"]')
        if (!field) return false
        const value = field.querySelector('[data-field-input]').value
        const source = field.getAttribute('data-source')
        return value === '3' && source !== 'module_default'
      },
      { timeout: 20000 }
    )
    .then(() => true)
    .catch(() => false)
  const savedSource = await pageB.evaluate(() => {
    const field = document.querySelector('[data-setting-key="every_n_turns"]')
    return field ? field.getAttribute('data-source') : null
  })
  check('★UI 里保存后回读：every_n_turns=3 且来源不再是模块默认', saved, 'source=' + savedSource)

  const diskText = readFileSync(sandboxConfig, 'utf8')
  const diskHas = /every_n_turns:\s*3\b/.test(diskText)
  check('★config.yaml 里真的落了库（文件里出现 every_n_turns: 3）', diskHas)
  const sbxAfter = sha256(sandboxConfig)
  check('沙盒 config.yaml 确实被改写了（sha256 变化）', sbxAfter !== sandboxBefore)
  await pageB.screenshot({ path: join(SHOTS, '04-saved-and-read-back.png') })

  // 后端二次回读（不经前端）：PUT 之后再 GET 一次
  const reGet = await (await fetch('http://127.0.0.1:' + SBX_FIXTURE_PORT + API + '/agents/default/plugins/hermes-auto-titler/settings')).json()
  const reGetBody = reGet.data || reGet
  const reGetValue = Number((reGetBody.settings || {}).every_n_turns)
  const reGetSource = (reGetBody.sources || {}).every_n_turns
  check('★后端 GET 二次回读也是 3（不是前端自说自话）',
    reGetValue === 3 && reGetSource && reGetSource !== 'module_default',
    JSON.stringify({ value: reGetValue, source: reGetSource }))

  // 恢复为全局值（写 null → 回落模块默认）
  await pageB.click('[data-field-reset="every_n_turns"]')
  const restored = await pageB
    .waitForFunction(
      () => {
        const field = document.querySelector('[data-setting-key="every_n_turns"]')
        if (!field) return false
        return field.querySelector('[data-field-input]').value === '2' && field.getAttribute('data-source') === 'module_default'
      },
      { timeout: 20000 }
    )
    .then(() => true)
    .catch(() => false)
  check('★「恢复为全局值」后回读：every_n_turns=2、来源回到模块默认', restored)
  await pageB.screenshot({ path: join(SHOTS, '05-restored.png') })
  await pageB.close()
  sbxSite.close()
  sbxFixture.child.kill()

  // ============================================== [C] 生产零污染 + 汇总
  const productionAfter = productionSnapshot()
  const drifted = Object.keys(productionBefore).filter((file) => productionBefore[file] !== productionAfter[file])
  check('★生产零污染：真实 config.yaml 与全部 profiles/*/config.yaml 一个字节都没变', drifted.length === 0, JSON.stringify(drifted))
  check('★全程零 console error', consoleErrors.length === 0, JSON.stringify(consoleErrors))
  check('★全程零 pageerror', pageErrors.length === 0, JSON.stringify(pageErrors))

  console.log('\n=== 汇总 ===')
  console.log('截图：' + SHOTS)
  for (const name of readdirSync(SHOTS).sort()) console.log('  ' + name)
  console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
  if (failures.length) {
    for (const item of failures) console.log('  FAIL ' + item)
    process.exitCode = 1
  } else {
    console.log('FIND-PLUGIN-PROBE-OK')
  }
} catch (err) {
  console.error('探针异常：', err && err.stack ? err.stack : err)
  process.exitCode = 1
} finally {
  for (const child of children) {
    try {
      child.kill()
    } catch (e) {
      /* 已退出 */
    }
  }
  if (browser) {
    try {
      await browser.close()
    } catch (e) {
      /* 已关闭 */
    }
  }
}
