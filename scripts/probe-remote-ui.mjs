/**
 * hermes-console · 远端后端分区（V4b）**真浏览器验收 + 截图**
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-remote-ui.mjs
 *     node plugins/hermes-console/scripts/probe-remote-ui.mjs --shots D:/tmp/shots
 *
 * 它验的是"老板能在桌面上看见什么"，所以三层都是真的：
 *
 *   1. **真产物**：``dashboard/dist/index.js``（= ``desktop/plugin.js``，同字节）原样加载，
 *      不是另一份手写实现；只在外面套一层 harness 入口（把 react / react-dom 一起 bundle，
 *      否则会拿到第二份 React、hooks 直接炸）。
 *   2. **真后端**：``scripts/remote_ui_fixture.py`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``，只把 ``api_remote._home`` 指到沙盒、把启动器换成
 *      "在该端口起一个只会回 /api/status 的小服务"（端口真的在听，判活/探活/回读都是真的）。
 *   3. **真浏览器**：Playwright + Chromium，1440×900（桌面窗口尺寸），宿主主题 CSS 直接
 *      引用桌面端那份 ``main-*.css``，所以配色/字体与桌面端一致。
 *
 * 覆盖的验收项（每条都有断言，不靠"我看了一眼"）：
 *   A. 一屏看到 ≥3 个 agent 的**状态与关键字段**（端口/PID/账号/最近启动/最近停止/最近错误）；
 *   B. 复制用户名成功（真读剪贴板比对）；
 *   C. 密码**默认掩码**：页面文本里没有明文；点「查看」才出现明文，点「隐藏」回到掩码；
 *      点「复制」把明文送进剪贴板但**屏幕上仍是掩码**；
 *   D. 启停二次确认：点「启动」只出确认条（后端**零请求**），点「确认」才发；
 *      执行完回执里带后端回读的 state/端口/PID，行状态跟着变。
 *   E. 全程零 console error / 零 pageerror。
 */
import { spawn } from 'node:child_process'
import { copyFileSync, createWriteStream, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
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
const FIXTURE_PORT = Number(process.env.HC_FIXTURE_PORT || 9188)
const SITE_PORT = Number(process.env.HC_SITE_PORT || 9187)
const API = '/api/plugins/hermes-console'
/** 哨兵凭据：与 remote_ui_fixture.py 里的一致（这里只用来断言，不打印明文）。 */
const DEMO_USER = 'hermes-demo'
const DEMO_USER_BETA = 'work-account'
const DEMO_PASSWORD = 'demo-pass-9f3a1c2b4d5e6f708192a3b4'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'remote-ui')

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) {
    pass += 1
    console.log('  PASS  ' + name + (extra ? '  — ' + extra : ''))
  } else {
    failures.push(name + (extra ? '  — ' + extra : ''))
    console.log('  FAIL  ' + name + (extra ? '  — ' + extra : ''))
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// --------------------------------------------------------------- 临时工作区
const WORK = mkdtempSync(join(tmpdir(), 'hc-remote-ui-'))
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

// 产物拷进工作区：这样 esbuild 才能从工作区的 node_modules 解析 react
copyFileSync(join(DIST, 'index.js'), join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

const ENTRY = `
import plugin, { injectConsoleStyles } from './entry.js'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

const store = {}
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
    requests.push(method + ' ' + path)
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    if (!res.ok) {
      const err = new Error(res.status + ': ' + text)
      throw err
    }
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
const host = document.getElementById('root')
createRoot(host).render(createElement(pages[0].render))
window.__hcBooted = true
`

writeFileSync(join(WORK, 'harness.js'), ENTRY, 'utf8')

function runEsbuild(args) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(process.execPath, [join(HOST_MODULES, 'esbuild', 'bin', 'esbuild'), ...args], {
      cwd: WORK,
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let err = ''
    child.stderr.on('data', (chunk) => {
      err += chunk
    })
    child.on('exit', (code) => (code === 0 ? resolvePromise() : reject(new Error('esbuild ' + code + '\n' + err))))
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

// ------------------------------------------------------------- 静态站 + 反代
function startSite() {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · 远端后端（验收夹具）</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head>
<body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script>
</body></html>`

  const server = createServer((req, res) => {
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: url, method: req.method, headers: req.headers },
        (up) => {
          res.writeHead(up.statusCode || 502, up.headers)
          up.pipe(res)
        }
      )
      upstream.on('error', (e) => {
        res.writeHead(502, { 'content-type': 'text/plain' })
        res.end('upstream error: ' + e.message)
      })
      req.pipe(upstream)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      res.end(html)
      return
    }
    const map = {
      '/style.css': join(WORK, 'style.css'),
      '/theme.css': themeCss,
      '/harness.bundle.js': join(WORK, 'harness.bundle.js')
    }
    const file = map[url.split('?')[0]]
    if (file && existsSync(file)) {
      res.writeHead(200, { 'content-type': url.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8' })
      res.end(readFileSync(file))
      return
    }
    res.writeHead(404, { 'content-type': 'text/plain' })
    res.end('not found: ' + url)
  })
  return new Promise((resolvePromise) => server.listen(SITE_PORT, '127.0.0.1', () => resolvePromise(server)))
}

/**
 * 起浏览器：优先用 Playwright 自带的 Chromium；本机没装（ms-playwright 里只有 ffmpeg）
 * 就退到系统装的 Edge / Chrome —— 验收要的是"真浏览器"，用哪个内核不影响结论。
 */
async function launchBrowser() {
  const attempts = [
    () => chromium.launch({ headless: true }),
    () => chromium.launch({ headless: true, channel: 'msedge' }),
    () => chromium.launch({ headless: true, channel: 'chrome' })
  ]
  const systemChrome = [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'
  ].find((p) => existsSync(p))
  if (systemChrome) attempts.push(() => chromium.launch({ headless: true, executablePath: systemChrome }))
  const errors = []
  for (const attempt of attempts) {
    try {
      return await attempt()
    } catch (e) {
      errors.push(String((e && e.message) || e).split('\n')[0])
    }
  }
  throw new Error('起不了浏览器：' + errors.join(' | '))
}

async function waitHttp(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url)
      if (res.ok) return await res.json()
    } catch (e) {
      // 还没起来
    }
    await sleep(300)
  }
  throw new Error('等不到 ' + url)
}

// ------------------------------------------------------------------- 主流程
async function main() {
  console.log('[0] 环境')
  console.log('        plugin  :', PLUGIN.replace(/\\/g, '/'))
  console.log('        artifact:', join(DIST, 'index.js'), statSync(join(DIST, 'index.js')).size, 'B')
  console.log('        shots   :', SHOTS.replace(/\\/g, '/'))

  await buildHarness()
  console.log('        harness : bundle ok')

  const home = join(WORK, 'home')
  const fixtureLog = createWriteStream(join(WORK, 'fixture.log'))
  const fixture = spawn(PYTHON, [join(HERE, 'remote_ui_fixture.py'), '--home', home, '--port', String(FIXTURE_PORT)], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe']
  })
  fixture.stdout.pipe(fixtureLog)
  fixture.stderr.pipe(fixtureLog)

  const site = await startSite()
  const origin = `http://127.0.0.1:${SITE_PORT}`
  let browser
  try {
    await waitHttp(`http://127.0.0.1:${FIXTURE_PORT}/api/plugins/hermes-console/health`, 60000)
    const agents = await (await fetch(`http://127.0.0.1:${FIXTURE_PORT}${API}/remote/agents`)).json()
    console.log('        夹具 agent 数 :', agents.count)

    browser = await launchBrowser()
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 1,
      permissions: ['clipboard-read', 'clipboard-write']
    })
    const page = await context.newPage()
    const consoleErrors = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text())
    })
    page.on('pageerror', (err) => consoleErrors.push('pageerror: ' + err.message))

    console.log('\n[1] 加载真产物（dist/index.js）+ 真后端，进「远端后端」分区')
    await page.goto(origin + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => window.__hcBooted === true, null, { timeout: 30000 })
    await page.getByRole('button', { name: /远端后端/ }).first().click()
    await page.waitForSelector('[data-remote-view="backends"]', { timeout: 20000 })
    await page.waitForSelector('[data-remote-row]', { timeout: 20000 })
    const rows = await page.$$('[data-remote-row]')
    console.log('        渲染行数 :', rows.length)
    check('★一屏一张表：渲染出 ≥3 行（不是"一屏两个大卡片"）', rows.length >= 3, rows.length + ' 行')

    // ---- 一屏能看全几行（桌面 1440×900）
    const inView = await page.evaluate(() => {
      const height = window.innerHeight
      return Array.from(document.querySelectorAll('[data-remote-row]')).filter((node) => {
        const box = node.getBoundingClientRect()
        return box.top >= 0 && box.bottom <= height && box.height > 0
      }).length
    })
    console.log('        首屏完整可见行数 :', inView)
    check('★首屏（1440×900）完整看到 ≥3 个 agent 的状态与关键字段', inView >= 3, inView + ' 行')

    // ---- 关键字段逐个检查（以 alpha 行为样板）
    const cellText = async (agent) =>
      page.evaluate((target) => {
        const row = document.querySelector('[data-remote-row="' + target + '"]')
        if (!row) return null
        const text = (sel) => {
          const node = row.querySelector(sel)
          return node ? node.textContent.trim() : null
        }
        return {
          state: text('.hc-rt__cell--status'),
          port: text('.hc-rt__cell--port'),
          pid: text('.hc-rt__cell--pid'),
          conn: text('.hc-rt__cell--conn'),
          username: text('[data-remote-username]'),
          masked: Boolean(row.querySelector('[data-remote-masked]')),
          started: text('.hc-rt__cell--time'),
          err: text('.hc-rt__cell--err')
        }
      }, agent)

    const alpha = await cellText('alpha')
    console.log('        alpha 行 :', JSON.stringify(alpha))
    check('★状态列有字（运行中/已停止）', Boolean(alpha && alpha.state), alpha && alpha.state)
    check('★端口列有数字', Boolean(alpha && /^\d+$/.test(alpha.port || '')), alpha && alpha.port)
    check('★PID 列有值（在跑的 agent）', Boolean(alpha && /^\d+$/.test(alpha.pid || '')), alpha && alpha.pid)
    check('★连接列有字（可连/可连·需登录/未启动）', Boolean(alpha && alpha.conn), alpha && alpha.conn)
    check('★账号列显示账号名（明文，可复制）', alpha && alpha.username === DEMO_USER, alpha && alpha.username)
    check('★密码列默认是掩码（不是明文、不是空）', Boolean(alpha && alpha.masked), alpha && String(alpha.masked))
    check('★最近启动列有时间', Boolean(alpha && /\d{2}-\d{2} \d{2}:\d{2}/.test(alpha.started || '')), alpha && alpha.started)
    const delta = await cellText('delta')
    check('★最近错误列把失败原因写出来（不是空白）',
      Boolean(delta && delta.err && delta.err !== '—'), delta && delta.err)

    // ---- 页面文本里不许有明文密码
    const bodyText = await page.evaluate(() => document.body.innerText)
    check('★整页文本里没有明文密码（默认遮蔽，不是"渲染好再遮"）', !bodyText.includes(DEMO_PASSWORD))

    await page.screenshot({ path: join(SHOTS, '01-remote-table.png') })

    console.log('\n[2] 密码：默认掩码 → 点「查看」才解密 → 「隐藏」回到掩码')
    await page.click('[data-remote-row="alpha"] [data-remote-action="reveal"]')
    await page.waitForSelector('[data-remote-row="alpha"] [data-remote-secret]', { timeout: 10000 })
    const revealed = (await page.textContent('[data-remote-row="alpha"] [data-remote-secret] code')).trim()
    check('★点「查看」后显示的就是真密码', revealed === DEMO_PASSWORD, '值已比对（不打印）')
    const afterRevealText = await page.evaluate(() => document.body.innerText)
    check('解密只在被点的那一行发生（别的行仍是掩码）',
      (await page.$$('[data-remote-secret]')).length === 1)
    await page.screenshot({ path: join(SHOTS, '02-password-revealed.png') })
    await page.click('[data-remote-row="alpha"] [data-remote-action="hide"]')
    await page.waitForSelector('[data-remote-row="alpha"] [data-remote-masked]', { timeout: 10000 })
    const hiddenText = await page.evaluate(() => document.body.innerText)
    check('★点「隐藏」后明文从页面上消失', !hiddenText.includes(DEMO_PASSWORD))
    check('（对照）解密期间页面文本里确实出现过明文', afterRevealText.includes(DEMO_PASSWORD))

    console.log('\n[3] 复制：账号一键复制 / 密码复制但不显示')
    await page.click('[data-remote-row="alpha"] [data-remote-action="copy-username"]')
    await sleep(300)
    const clipUser = await page.evaluate(() => navigator.clipboard.readText())
    check('★复制账号：剪贴板里就是账号名', clipUser === DEMO_USER, '值已比对（不打印）')
    const copyLabel = await page.textContent('[data-remote-row="alpha"] [data-remote-action="copy-username"]')
    check('复制后有反馈（按钮变「已复制」）', copyLabel.trim() === '已复制', copyLabel.trim())

    const betaRowUser = (await cellText('beta')).username
    check('独立凭据的 agent 显示自己的账号名', betaRowUser === DEMO_USER_BETA, betaRowUser)
    await page.click('[data-remote-row="beta"] [data-remote-action="copy-password"]')
    await sleep(600)
    const clipPassword = await page.evaluate(() => navigator.clipboard.readText())
    check('★复制密码：剪贴板里是真密码', clipPassword === DEMO_PASSWORD, '值已比对（不打印）')
    const betaMasked = await page.evaluate(
      () => Boolean(document.querySelector('[data-remote-row="beta"] [data-remote-masked]')))
    check('★复制密码不把明文留在屏幕上（仍是掩码）', betaMasked)
    const textAfterCopy = await page.evaluate(() => document.body.innerText)
    check('★复制密码后整页文本仍无明文', !textAfterCopy.includes(DEMO_PASSWORD))

    console.log('\n[4] 启停：二次确认 + 结果回读')
    await fetch(`http://127.0.0.1:${FIXTURE_PORT}/__log/reset`, { method: 'POST' })
    await page.click('[data-remote-row="beta"] [data-remote-action="start"]')
    await page.waitForSelector('[data-remote-confirm="start:beta"]', { timeout: 10000 })
    const confirmText = await page.textContent('[data-remote-confirm="start:beta"]')
    console.log('        确认条 :', confirmText.replace(/\s+/g, ' ').trim())
    check('确认条点名了 agent 的中文名与 id', confirmText.includes('工作号') && confirmText.includes('beta'))
    const logAfterAsk = await (await fetch(`http://127.0.0.1:${FIXTURE_PORT}/__log`)).json()
    check('★点「启动」到「确认」之间**一个请求都没发**',
      !logAfterAsk.requests.some((r) => r.path.includes('/remote/agent/start')),
      JSON.stringify(logAfterAsk.requests.slice(-3)))
    await page.screenshot({ path: join(SHOTS, '03-confirm-bar.png') })

    await page.click('[data-remote-confirm-yes="start:beta"]')
    await page.waitForSelector('[data-remote-agent-row="beta"] [data-remote-feedback="ok"]', { timeout: 20000 })
    const feedback = (await page.textContent('[data-remote-agent-row="beta"] [data-remote-feedback]')).replace(/\s+/g, ' ').trim()
    console.log('        回执   :', feedback)
    check('★执行后有回执，且回执里带后端回读（端口/PID）', /端口\s*\d+/.test(feedback) && /PID\s*\d+/.test(feedback), feedback)
    const logAfterConfirm = await (await fetch(`http://127.0.0.1:${FIXTURE_PORT}/__log`)).json()
    check('★点「确认」后才真的发了 start 请求',
      logAfterConfirm.requests.some((r) => r.path.includes('/remote/agent/start')),
      JSON.stringify(logAfterConfirm.requests.slice(-3)))
    check('启停真的走到了启动器（_run_script 被调用一次）',
      logAfterConfirm.calls.some((call) => call[0] === 'start'), JSON.stringify(logAfterConfirm.calls))
    await sleep(1200)
    const betaAfter = await cellText('beta')
    check('★回读后行状态变成运行中', betaAfter.state === '运行中', betaAfter.state)
    await page.screenshot({ path: join(SHOTS, '04-after-start.png') })

    await page.click('[data-remote-row="alpha"] [data-remote-action="stop"]')
    await page.waitForSelector('[data-remote-confirm="stop:alpha"]', { timeout: 10000 })
    await page.click('[data-remote-confirm-yes="stop:alpha"]')
    await page.waitForSelector('[data-remote-agent-row="alpha"] [data-remote-feedback="ok"]', { timeout: 20000 })
    await sleep(1200)
    const alphaAfter = await cellText('alpha')
    check('★停止后行状态变回已停止', alphaAfter.state === '已停止', alphaAfter.state)
    check('★停止后"最近停止"列被填上（事件台账真的写了一条）',
      /\d{2}-\d{2} \d{2}:\d{2}/.test(alphaAfter.started || '') &&
      Boolean(await page.evaluate(() => {
        const row = document.querySelector('[data-remote-row="alpha"]')
        const cells = row ? row.querySelectorAll('.hc-rt__cell--time') : []
        return cells.length === 2 && /\d{2}-\d{2} \d{2}:\d{2}/.test(cells[1].textContent)
      })))

    console.log('\n[5] 窄窗（900px 以下）收列，不横向滚动')
    await page.setViewportSize({ width: 820, height: 900 })
    await sleep(500)
    const narrow = await page.evaluate(() => {
      const doc = document.documentElement
      const hiddenPort = document.querySelector('[data-remote-row] .hc-rt__cell--port')
      return {
        overflow: doc.scrollWidth - doc.clientWidth,
        portHidden: hiddenPort ? getComputedStyle(hiddenPort).display === 'none' : false,
        metaShown: Boolean(document.querySelector('.hc-rt__meta') && getComputedStyle(document.querySelector('.hc-rt__meta')).display !== 'none')
      }
    })
    check('窄窗不出现横向滚动条', narrow.overflow <= 1, 'scrollWidth-clientWidth=' + narrow.overflow)
    check('窄窗把端口/PID/时间收进名称下面（列收敛，不是挤爆）', narrow.portHidden && narrow.metaShown)
    await page.screenshot({ path: join(SHOTS, '05-narrow.png') })
    await page.setViewportSize({ width: 1440, height: 900 })

    check('★全程零 console error / 零 pageerror', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))

    console.log('\n[6] 截图')
    for (const name of readdirSync(SHOTS).filter((f) => f.endsWith('.png')).sort()) {
      console.log('        %s  %d B', name, statSync(join(SHOTS, name)).size)
    }
  } finally {
    if (browser) await browser.close().catch(() => {})
    site.close()
    fixture.kill()
    fixtureLog.end()
  }
}

main()
  .then(() => {
    console.log('\nPASS=%d FAIL=%d TOTAL=%d', pass, failures.length, pass + failures.length)
    if (failures.length) {
      console.log('REMOTE-UI-PROBE-FAILED')
      for (const item of failures) console.log('  -', item)
      process.exitCode = 1
    } else {
      console.log('REMOTE-UI-PROBE-OK')
    }
  })
  .catch((err) => {
    console.error('探针异常：', err && err.stack ? err.stack : err)
    process.exitCode = 2
  })
  .finally(() => {
    setTimeout(() => {
      try {
        rmSync(WORK, { recursive: true, force: true })
      } catch (e) {
        // 工作区清不掉不影响结论
      }
    }, 200)
  })
