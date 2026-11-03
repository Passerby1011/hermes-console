/**
 * hermes-console · 上下文引擎「按 agent 解析版本 + 实际生效引擎」真浏览器验收（t_361868b4）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-vcc-engine.mjs --agent research-worker
 *     node plugins/hermes-console/scripts/probe-vcc-engine.mjs --agent default
 *
 * 三层都是真的（与 probe-vcc-conv.mjs 同一套路）：
 *   1. 真产物：默认 ``dashboard/dist/index.js``（= ``desktop/plugin.js`` = ``desktop-plugins/hermes-console/plugin.js``）；
 *   2. 真后端：``scripts/models_batch_fixture.py --home <真 home>`` 加载真实 ``plugin_api.py``；本探针只发 GET；
 *   3. 真浏览器：Playwright + Chromium(msedge)，1440×900。
 *
 * 断言（对应卡片验收 1/2/4/5）：
 *   A. 面板上该 agent 的插件版本 == 后端 ``engine_version_actual`` == 磁盘 ``plugin.yaml``（独立读盘对照）；
 *   B. 与全局不一致的 agent 有「与全局 X 不一致」徽标，一致的没有；
 *   C. 「实际生效引擎（日志）」显示后端证据（research-worker 有真实 fallback 行），无记录就明说无记录；
 *   D. 切换页显示本 agent 目录的版本；未安装引擎的 agent 目标按钮禁用（后端另有 409，见 probe_vcc.py）；
 *   E. 900 宽无横向滚动、零 console error / 零 pageerror。
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const REAL_HOME = process.env.HERMES_HOME_REAL || 'D:/hermes-data/hermes'
const FIXTURE_PORT = Number(process.env.HC_VCC_FIXTURE_PORT || 9195)
const SITE_PORT = Number(process.env.HC_VCC_SITE_PORT || 9194)
const API = '/api/plugins/hermes-console'

const argOf = (name) => {
  const index = process.argv.indexOf(name)
  return index > -1 ? process.argv[index + 1] : ''
}
const SHOTS = resolve(argOf('--shots') || join(PLUGIN, 'scripts', '_probe_out', 'vcc-conv'))
const BUNDLE = resolve(argOf('--bundle') || join(DIST, 'index.js'))
// 夹具里先把作用域钉到某个 agent（截图才稳定；真面板上这个值是用户自己选的）
const SCOPE_AGENT = argOf('--agent') || 'default'

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-vcc-conv-'))
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

// ---- 产物：默认 dist，可用 --bundle 指到临时构建（并行推进时不重写 dist） ----------
if (!existsSync(BUNDLE)) throw new Error('产物不存在：' + BUNDLE)
copyFileSync(BUNDLE, join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

const HARNESS = `
import plugin, { injectConsoleStyles } from './entry.js'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

const store = { 'scope.agent': '${SCOPE_AGENT}' }
const pages = []
const requests = []
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
    requests.push({ method, path, status: res.status })
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
window.__hcStore = store
const host = document.getElementById('root')
createRoot(host).render(createElement(pages[0].render))
window.__hcBooted = true
`
writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')

function runEsbuild(args) {
  return new Promise((res, rej) => {
    const child = spawn(process.execPath, [join(HOST_MODULES, 'esbuild', 'bin', 'esbuild'), ...args], {
      cwd: WORK,
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let err = ''
    child.stderr.on('data', (c) => { err += c })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild ' + code + '\n' + err))))
  })
}

async function buildHarness() {
  await runEsbuild([
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
  ])
}

function startSite() {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · 上下文压缩（对话下钻验收夹具）</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head>
<body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script>
</body></html>`

  const server = createServer((req, res) => {
    const safe = (fn) => {
      try {
        if (!res.writableEnded && !res.destroyed) fn()
      } catch (e) { /* 客户端已断开 */ }
    }
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: url, method: req.method, headers: req.headers },
        (up) => safe(() => { res.writeHead(up.statusCode || 502, up.headers); up.pipe(res) })
      )
      upstream.on('error', (e) => safe(() => {
        res.writeHead(502, { 'content-type': 'text/plain' })
        res.end('upstream error: ' + e.message)
      }))
      req.pipe(upstream)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      safe(() => { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(html) })
      return
    }
    const map = {
      '/style.css': join(WORK, 'style.css'),
      '/theme.css': themeCss,
      '/harness.bundle.js': join(WORK, 'harness.bundle.js')
    }
    const file = map[url.split('?')[0]]
    if (file && existsSync(file)) {
      const body = readFileSync(file)
      safe(() => {
        res.writeHead(200, { 'content-type': url.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8' })
        res.end(body)
      })
      return
    }
    safe(() => { res.writeHead(404, { 'content-type': 'text/plain' }); res.end('not found: ' + url) })
  })
  return new Promise((res) => server.listen(SITE_PORT, '127.0.0.1', () => res(server)))
}

function startFixture() {
  const child = spawn(PYTHON, [join(HERE, 'models_batch_fixture.py'), '--port', String(FIXTURE_PORT), '--home', REAL_HOME], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' })
  })
  let out = ''
  child.stdout.on('data', (c) => { out += c })
  child.stderr.on('data', (c) => { out += c })
  return { child, log: () => out }
}

async function waitForFixture(fixture) {
  const url = 'http://127.0.0.1:' + FIXTURE_PORT + API + '/health'
  for (let i = 0; i < 200; i += 1) {
    try {
      const res = await fetch(url)
      if (res.ok) return true
    } catch (e) { /* 还没起来 */ }
    await sleep(250)
  }
  throw new Error('后端夹具没起来：\n' + fixture.log())
}

const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

try {
  console.log('[env] bundle :', BUNDLE)
  console.log('[env] shots  :', SHOTS)
  console.log('[env] home   :', REAL_HOME)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)
  // ---- 后端事实：每个 agent 的版本（面板要显示的）与磁盘实际 --------------------
  const overviewUrl = 'http://127.0.0.1:' + FIXTURE_PORT + API + '/vcc/overview?recent=1'
  const overview = await (await fetch(overviewUrl)).json()
  const allRows = Array.isArray(overview.profiles) ? overview.profiles : []
  check('后端：/vcc/overview 返回 9 个 agent', allRows.length === 9, 'n=' + allRows.length)
  check('后端：engine_version_global 非空', Boolean(overview.engine_version_global),
    String(overview.engine_version_global))
  check('后端：每行都有 engine_version_actual / engine_installed / engine_mismatch',
    allRows.every((r) => 'engine_version_actual' in r && 'engine_installed' in r && 'engine_mismatch' in r),
    JSON.stringify(allRows.map((r) => r.agent + '=' + r.engine_version_actual).slice(0, 4)))
  // 逐 agent 与磁盘对照（独立读 plugin.yaml，不信任后端自述）
  const fs = await import('node:fs')
  const diskOf = (agent) => {
    const home = agent === 'default' ? REAL_HOME : join(REAL_HOME, 'profiles', agent)
    const p = join(home, 'plugins', 'hermes-vcc-cn', 'plugin.yaml')
    if (!fs.existsSync(p)) return null
    const m = /^version:\s*(\S+)/m.exec(fs.readFileSync(p, 'utf8'))
    return m ? m[1].replace(/['"]/g, '') : ''
  }
  const mismatched = allRows.filter((r) => diskOf(r.agent) !== r.engine_version_actual)
  check('★后端每行版本 == 磁盘 plugin.yaml 实际（9/9）', mismatched.length === 0,
    mismatched.map((r) => r.agent + ': api=' + r.engine_version_actual + ' disk=' + diskOf(r.agent)).join('; '))

  const scopeRow = allRows.filter((r) => r.agent === SCOPE_AGENT)[0]
  check('作用域 agent 在 overview 里有一行', Boolean(scopeRow), SCOPE_AGENT)

  site = await startSite()
  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  const shotDir = SHOTS
  const shot = async (name) => {
    await page.screenshot({ path: join(shotDir, name), fullPage: false })
  }

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })

  await page.click('.hc-nav__item:has-text("上下文引擎")')
  await page.waitForSelector('[data-vcc-view]', { timeout: 30000 })

  // ---- A. 引擎概览：版本按 agent 自己的目录显示 ---------------------------------
  await page.click('[data-vcc-tab="overview"]')
  await page.waitForSelector('[data-vcc-agent]', { timeout: 30000 })
  const card = page.locator('[data-vcc-agent="' + SCOPE_AGENT + '"]').first()
  const cardText = (await card.innerText()).replace(/\s+/g, ' ')
  const shownVersion = await card.locator('[data-vcc-engine-version]').first().getAttribute('data-vcc-engine-version')
  check('★面板该 agent 的插件版本 == 后端 engine_version_actual == 磁盘',
    shownVersion === String(scopeRow.engine_version_actual) && shownVersion === diskOf(SCOPE_AGENT),
    'panel=' + shownVersion + ' api=' + scopeRow.engine_version_actual + ' disk=' + diskOf(SCOPE_AGENT))
  const mismatchChip = await card.locator('[data-vcc-engine-mismatch]').count()
  check('与全局不一致的 agent 有「与全局 X 不一致」徽标（一致时没有）',
    scopeRow.engine_mismatch ? mismatchChip === 1 : mismatchChip === 0,
    'api.mismatch=' + scopeRow.engine_mismatch + ' chips=' + mismatchChip)
  check('卡片里有「引擎插件（本 agent 目录）」并带该 agent 自己的路径',
    cardText.includes('引擎插件（本 agent 目录）') && cardText.includes('profiles\\' + SCOPE_AGENT + '\\plugins')
    || (SCOPE_AGENT === 'default' && cardText.includes('hermes\\plugins\\hermes-vcc-cn')),
    cardText.slice(0, 160))
  // ★ 实际生效引擎（日志证据）：research-worker 有真实回退行
  const effText = await card.locator('span:has-text("实际生效引擎（日志）")').first().locator('xpath=following-sibling::span[1]').innerText().catch(() => '')
  const expectEff = scopeRow.effective_engine
  check('★「实际生效引擎（日志）」显示后端给的证据（无记录就明说无记录）',
    expectEff
      ? effText.includes(String(expectEff.engine)) && effText.includes(String(expectEff.ts || '').slice(0, 10))
      : /没有引擎生效记录/.test(cardText),
    'ui=' + effText.replace(/\s+/g, ' ').slice(0, 120) + ' api=' + JSON.stringify(expectEff))
  check('概览没有把全局版本冒充成该 agent 的版本（卡片版本徽标 ≠ 全局时也照实显示）',
    overview.engine_version_global !== shownVersion || scopeRow.engine_mismatch === false,
    'global=' + overview.engine_version_global + ' panel=' + shownVersion)
  await shot('vcc-engine-overview-' + SCOPE_AGENT + '.png')

  // 全局版本徽标存在（说明面板明确区分「全局」与「各 agent」）
  const globalChip = await page.locator('[data-vcc-global-version]').first().getAttribute('data-vcc-global-version')
  check('顶栏有「全局 X」徽标且等于后端 engine_version_global',
    globalChip === String(overview.engine_version_global), String(globalChip))

  // ---- B. 引擎切换页：实际生效引擎 + 与全局对比 + 未安装禁点 --------------------
  await page.click('[data-vcc-tab="engine"]')
  await page.waitForSelector('[data-vcc-view="engine"] .hc-card', { timeout: 30000 })
  await page.waitForTimeout(800)
  const engineText = (await page.locator('[data-vcc-view="engine"]').first().innerText()).replace(/\s+/g, ' ')
  check('切换页显示「实际生效引擎（日志）」',
    engineText.includes('实际生效引擎（日志）'), engineText.slice(0, 120))
  check('切换页显示「引擎插件（本 agent 目录）」的版本与磁盘一致',
    engineText.includes('引擎插件（本 agent 目录）') && engineText.includes(diskOf(SCOPE_AGENT) || '未安装'),
    engineText.slice(0, 200))
  const targetBtn = page.locator('[data-vcc-engine-choice="' + overview.engine_target + '"]').first()
  const disabled = await targetBtn.isDisabled().catch(() => null)
  const isCurrent = scopeRow.engine === overview.engine_target
  check('未安装引擎的 agent：目标引擎按钮禁用且写明原因（已装则不禁用）',
    scopeRow.engine_installed === false ? disabled === true : (isCurrent ? disabled === true : disabled !== true),
    'installed=' + scopeRow.engine_installed + ' current=' + scopeRow.engine + ' disabled=' + disabled)
  await shot('vcc-engine-switch-' + SCOPE_AGENT + '.png')

  // ---- C. 900 宽无横向滚动 ----------------------------------------------------
  await page.setViewportSize({ width: 900, height: 900 })
  await page.waitForTimeout(400)
  const narrow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth
  }))
  check('900 宽无横向滚动', narrow.scroll <= narrow.client + 1, JSON.stringify(narrow))
  await shot('vcc-engine-narrow-' + SCOPE_AGENT + '.png')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.waitForTimeout(200)

  // ---- D. 全程无报错 ----------------------------------------------------------
  check('零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  console.log('[info] 该 agent 卡片文本：' + cardText.slice(0, 400))
  console.log('[info] 截图目录：' + SHOTS)
} catch (error) {
  failures.push('探针异常：' + (error && error.message ? error.message : String(error)))
  console.log('  FAIL 探针异常：' + (error && error.stack ? error.stack : error))
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) await new Promise((res) => site.close(res))
  if (fixture && fixture.child) fixture.child.kill()
  if (failures.length) console.log('\n[fixture log]\n' + (fixture ? fixture.log() : ''))
}

console.log('\n' + pass + ' PASS / ' + failures.length + ' FAIL')
if (failures.length) {
  failures.forEach((item) => console.log('  FAILED: ' + item))
  process.exit(1)
}
