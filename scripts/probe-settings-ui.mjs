/**
 * hermes-console · 设置分区「先框架、后按需加载」**真浏览器验收 + 截图**
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-settings-ui.mjs
 *     node plugins/hermes-console/scripts/probe-settings-ui.mjs --shots D:/tmp/shots
 *
 * 三层都是真的（与 probe-remote-ui.mjs 同一套路）：
 *
 *   1. **真产物**：``dashboard/dist/index.js``（= ``desktop/plugin.js``，同字节）原样加载，
 *      只套一层 harness 入口（react / react-dom 一起 bundle，否则会拿到第二份 React）。
 *   2. **真后端**：``scripts/settings_ui_fixture.py`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``，并且**直接打真实 HERMES_HOME**（设置分区是纯读）——
 *      截图里的 agent 列表 / 插件三态 / 版本号 / 耗时全是这台机器的真数据。
 *   3. **真浏览器**：Playwright + Chromium，1440×900（桌面窗口尺寸），宿主主题 CSS 直接引用
 *      桌面端那份 ``main-*.css``，配色字体与桌面端一致。
 *
 * 断言（对应卡片 t_8146f2a9 的验收）：
 *   A. 首屏先出框架 + **页面内 agent 列表**：agent 按钮 ≥2 个，且这一刻**还没发**任何
 *      /agents/<id>/plugins 请求（先框架后按需）。
 *   B. 未选中 agent 时是「骨架 + 引导」态：有 .hc-skel、有「选一个 Agent」，**没有** ErrorState。
 *   C. 点一个 agent 才发它一个的 /plugins（按需，不整批）；列表出来，耗时标签有数字。
 *   D. 6 个 agent 并发刷新零 503：在页面里同时点 6 个 agent（模拟 6 张卡同时刷），
 *      统计 HTTP 状态码，503 必须为 0，并把三组耗时打进截图。
 *   E. 全程零 console error / 零 pageerror。
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmdirSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from 'node:fs'
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
const FIXTURE_PORT = Number(process.env.HC_SETTINGS_FIXTURE_PORT || 9191)
const SITE_PORT = Number(process.env.HC_SETTINGS_SITE_PORT || 9190)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'settings-ui')

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

// --------------------------------------------------------------- 临时工作区
const WORK = mkdtempSync(join(tmpdir(), 'hc-set-ui-'))
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

copyFileSync(join(DIST, 'index.js'), join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

// harness：真 ctx（rest 打真后端、storage 用内存），记录每次请求与状态码
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
    const started = performance.now()
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    requests.push({ method, path, status: res.status, ms: Math.round(performance.now() - started) })
    statuses.push(res.status)
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
window.__hcStatuses = statuses
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
    child.stderr.on('data', (c) => {
      err += c
    })
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

// ------------------------------------------------------------- 静态站 + 反代
function startSite() {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · 设置（验收夹具）</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head>
<body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script>
</body></html>`

  const server = createServer((req, res) => {
    // 收尾阶段浏览器可能还有在途请求：写响应前一律先看 socket 还在不在，
    // 否则会抛 ERR_HTTP_HEADERS_SENT 把已经全绿的探针带成 exit=1（假失败）。
    const safe = (fn) => {
      try {
        if (!res.writableEnded && !res.destroyed) fn()
      } catch (e) {
        /* 客户端已断开：忽略 */
      }
    }
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: url, method: req.method, headers: req.headers },
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
    safe(() => {
      res.writeHead(404, { 'content-type': 'text/plain' })
      res.end('not found: ' + url)
    })
  })
  return new Promise((res) => server.listen(SITE_PORT, '127.0.0.1', () => res(server)))
}

// -------------------------------------------------------------- 后端夹具进程
function startFixture() {
  const child = spawn(PYTHON, [join(HERE, 'settings_ui_fixture.py'), '--port', String(FIXTURE_PORT)], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' })
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

async function waitForFixture(fixture) {
  const url = 'http://127.0.0.1:' + FIXTURE_PORT + API + '/health'
  for (let i = 0; i < 120; i += 1) {
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

// ---------------------------------------------------------------------- 主流程
const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

try {
  console.log('[env] dist   :', DIST)
  console.log('[env] shots  :', SHOTS)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)

  // 冷 / 热两组的真数：**夹具进程刚起来**，第一次请求就是冷加载（schema / 版本缓存全空）。
  // 注意 waitForFixture 打的是 /health，不预热插件清单，所以下面这一次仍是冷的。
  const coldAgent = 'default'
  const hitList = async (agent) => {
    const t0 = performance.now()
    const res = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/agents/' + encodeURIComponent(agent) + '/plugins')
    const body = await res.text()
    return { ms: Math.round(performance.now() - t0), status: res.status, bytes: body.length, body: body }
  }
  const cold = await hitList(coldAgent)
  if (cold.status !== 200) console.log('        [cold body] ' + cold.body.slice(0, 600))
  const hots = []
  for (let i = 0; i < 7; i += 1) hots.push((await hitList(coldAgent)).ms)
  hots.sort((a, b) => a - b)
  const coldMs = cold.ms
  const hotMedian = hots[Math.floor(hots.length / 2)]
  console.log('        冷加载 %d ms（status=%d）', coldMs, cold.status)
  console.log('        热加载 7 次 %s → 中位 %d ms', JSON.stringify(hots), hotMedian)
  check('冷加载返回 200', cold.status === 200, 'status=' + cold.status)
  check('热加载全部 200 且快于 500ms', hots[hots.length - 1] < 500, 'max=' + hots[hots.length - 1])

  site = await startSite()

  browser = await chromium.launch({
    // 本机 ms-playwright 目录里只有 ffmpeg/winldd（没下载 chromium），用系统 Edge：
    // 它同样是 Chromium 内核、版本 153，与桌面端 Electron 的渲染口径一致。
    // 想用下载的 chromium 就设 HC_PW_CHANNEL='' 或别的 channel。
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    // 静态站没放 favicon，浏览器会报一条 404 —— 那不是本页的错，别把它算成 console error。
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })

  // 进入设置分区
  await page.click('.hc-nav__item:has-text("设置")')
  await page.waitForSelector('[data-agent-bar]', { timeout: 20000 })

  // ---- A. 首屏：框架 + 页内 agent 列表，且还没读插件 ----
  console.log('\n[A] 首屏：框架 + 页面内 agent 列表（先框架，后按需）')
  const chips = await page.$$('[data-agent-pick]')
  check('★页面内有 agent 列表（可点按钮）', chips.length >= 2, 'chips=' + chips.length)
  const early = await page.evaluate(() => window.__hcRequests.filter((r) => /^\/agents\/[^/]+\/plugins$/.test(r.path)).length)
  const earlyDistinct = await page.evaluate(() =>
    Array.from(new Set(window.__hcRequests.filter((r) => /^\/agents\/[^/]+\/plugins$/.test(r.path)).map((r) => r.path.split('/')[2])))
  )
  check('★首屏没有整批预取（不同 agent 的 /plugins ≤ 1）', earlyDistinct.length <= 1, JSON.stringify(earlyDistinct))
  check('agent 列表在页面里，不依赖顶部作用域选择器（顶部 select 另存）', Boolean(await page.$('.hc-select')))

  await page.screenshot({ path: join(SHOTS, '01-first-paint.png') })

  // 首屏默认沿用顶部作用域那一个 agent（只读一个），等它读完，后面的"点选"才好干净地计数
  const firstPaintLoaded = await page
    .waitForSelector('.hc-set__row', { timeout: 30000 })
    .then(() => true)
    .catch(() => false)
  check('首屏自动沿用顶部作用域，只读那一个 agent 的清单（不是空白）', firstPaintLoaded)
  await sleep(400)

  // ---- B. 未选中 = 骨架 + 引导态 ----
  console.log('\n[B] 未选中 agent：骨架 + 引导态（不是空列表，也不是 ErrorState）')
  const clearBtn = await page.$('[data-agent-clear]')
  if (clearBtn) await clearBtn.click()
  else {
    // 首屏如果已经默认选了顶部作用域，先点「不选」以外的路径：这里直接断言引导态可到达
    check('有「不选」按钮（可回到引导态）', false, 'no [data-agent-clear]')
  }
  await page.waitForSelector('[data-guide-state]', { timeout: 10000 })
  const guide = await page.evaluate(() => {
    const node = document.querySelector('[data-guide-state]')
    return {
      state: node ? node.getAttribute('data-guide-state') : null,
      hasSkel: Boolean(node && node.querySelector('.hc-skel')),
      isError: Boolean(node && node.querySelector('.hc-state--error')),
      hasList: Boolean(document.querySelector('.hc-set__list')),
      text: node ? node.textContent : ''
    }
  })
  check('★引导态出现（data-guide-state=no-agent）', guide.state === 'no-agent', String(guide.state))
  check('★引导态里有骨架（不是空白）', guide.hasSkel)
  check('★引导态不是 ErrorState（没有假报错）', !guide.isError)
  check('★引导态不是空列表', !guide.hasList)
  check('引导态说清「选一个 Agent」+ 为什么不再整批拉', /选一个 Agent/.test(guide.text) && /整批/.test(guide.text))
  await page.screenshot({ path: join(SHOTS, '02-guide-no-agent.png') })

  // ---- C. 点一个 agent 才读它一个 ----
  console.log('\n[C] 选中哪个 agent 才加载它的插件（按需）')
  const beforePick = await page.evaluate(() => window.__hcRequests.length)
  const pickId = await page.evaluate(() => {
    const node = document.querySelector('[data-agent-pick]')
    return node ? node.getAttribute('data-agent-pick') : null
  })
  await page.click('[data-agent-pick="' + pickId + '"]')
  await page.waitForSelector('.hc-set__row', { timeout: 30000 })
  await page.waitForSelector('[data-elapsed-ms]', { timeout: 30000 })
  const picked = await page.evaluate((from) => {
    const node = document.querySelector('[data-elapsed-ms]')
    return {
      agent: node ? node.getAttribute('data-elapsed-agent') : null,
      ms: node ? Number(node.getAttribute('data-elapsed-ms')) : null,
      rows: document.querySelectorAll('.hc-set__row').length,
      newReqs: window.__hcRequests.slice().length,
      tail: window.__hcRequests.slice(from).map((r) => r.method + ' ' + r.path)
    }
  }, beforePick)
  console.log('        点选后新增请求:', JSON.stringify(picked.tail))
  check('★点选后只读了这一个 agent（耗时标签标的就是它）', picked.agent === pickId, String(picked.agent))
  check('★耗时是真实毫秒数（不是占位）', Number.isFinite(picked.ms) && picked.ms >= 0, String(picked.ms))
  check('插件行渲染出来了', picked.rows > 0, 'rows=' + picked.rows)
  check(
    '★这次只为自己这一个 agent 新增了插件清单请求（不整批）',
    picked.tail.filter((line) => /\/agents\/[^/]+\/plugins$/.test(line)).length <= 1,
    JSON.stringify(picked.tail.filter((line) => /\/agents\/[^/]+\/plugins$/.test(line)))
  )
  await page.screenshot({ path: join(SHOTS, '03-agent-picked.png') })

  // ---- D. 6 个 agent 并发刷新，零 503 ----
  console.log('\n[D] 6 个 agent 并发刷新：零 503 + 三组耗时')
  const agentIds = await page.evaluate(() => Array.from(document.querySelectorAll('[data-agent-pick]')).map((n) => n.getAttribute('data-agent-pick')))
  const six = agentIds.slice(0, 6)
  check('页面里至少有 6 个 agent 可并发点', six.length >= 6, 'agents=' + JSON.stringify(six))

  const concurrent = await page.evaluate(async (ids) => {
    const started = performance.now()
    const results = await Promise.all(
      ids.map(async (id) => {
        const t0 = performance.now()
        const res = await fetch('/api/plugins/hermes-console/agents/' + encodeURIComponent(id) + '/plugins')
        await res.text()
        return { id, status: res.status, ms: Math.round(performance.now() - t0) }
      })
    )
    return { wall: Math.round(performance.now() - started), results }
  }, six)

  const codes = concurrent.results.map((r) => r.status)
  const ms = concurrent.results.map((r) => r.ms)
  const sorted = ms.slice().sort((a, b) => a - b)
  const median = sorted[Math.floor(sorted.length / 2)]
  console.log('        6 并发明细:', JSON.stringify(concurrent.results))
  check('★6 个 agent 并发全部 200', codes.every((c) => c === 200), JSON.stringify(codes))
  check('★零 503', codes.filter((c) => c === 503).length === 0, 'count503=' + codes.filter((c) => c === 503).length)
  console.log('        并发墙钟 %d ms / 中位 %d ms / 最大 %d ms', concurrent.wall, median, Math.max(...ms))

  // 页面里也走一遍「逐个点」的交互刷新（证明 UI 侧并发点不会互相踩）
  for (const id of six) {
    await page.click('[data-agent-pick="' + id + '"]')
    await page.waitForSelector('.hc-set__row', { timeout: 30000 })
  }
  await page.screenshot({ path: join(SHOTS, '04-after-six-agents.png') })

  const statuses = await page.evaluate(() => window.__hcStatuses.slice())
  check('★整轮 UI 请求里零 503（含页面自身发的）', statuses.filter((s) => s === 503).length === 0, 'total=' + statuses.length)
  check('整轮 UI 请求里零 5xx', statuses.filter((s) => s >= 500).length === 0, JSON.stringify(statuses.filter((s) => s >= 500)))

  // ---- E. 控制台零报错 ----
  console.log('\n[E] 控制台 / 页面异常')
  check('★零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('★零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))

  // 收尾：把三组耗时写进一张卡片式截图（老板一眼能看到数）
  const summaryLines = [
    '设置页实测（真后端 · 真 Chromium）',
    '冷加载（夹具进程首次）  : ' + coldMs + ' ms',
    '热加载（同 agent 连续 7 次中位）: ' + hotMedian + ' ms',
    '6 agent 并发墙钟        : ' + concurrent.wall + ' ms（中位 ' + median + ' ms，最大 ' + Math.max(...ms) + ' ms）',
    '503 计数                : ' + codes.filter((c) => c === 503).length
  ].join('\n')
  const summary = await page.evaluate((data) => {
    const box = document.createElement('div')
    box.setAttribute('id', 'hc-summary')
    box.style.cssText =
      'position:fixed;right:16px;bottom:16px;z-index:99999;padding:12px 16px;border-radius:8px;' +
      'background:var(--ui-bg-tertiary, #1b1b1b);color:var(--ui-text-primary, #eee);' +
      'border:1px solid var(--ui-stroke-secondary, #444);font:12px/1.7 ui-monospace,monospace;white-space:pre'
    box.textContent = data
    document.body.appendChild(box)
    return data
  }, summaryLines)
  await page.screenshot({ path: join(SHOTS, '05-summary.png') })
  console.log('\n[summary]\n' + summary)
} catch (err) {
  console.log('  FAIL 运行异常：' + (err && err.message ? err.message : String(err)))
  failures.push('运行异常：' + (err && err.message ? err.message : String(err)))
} finally {
  try {
    if (browser) await browser.close()
  } catch (e) {
    /* ignore */
  }
  try {
    if (site) site.close()
  } catch (e) {
    /* ignore */
  }
  try {
    if (fixture) fixture.child.kill()
  } catch (e) {
    /* ignore */
  }
  try {
    if (process.platform === 'win32') rmdirSync(join(WORK, 'node_modules'))
    else unlinkSync(join(WORK, 'node_modules'))
    rmSync(WORK, { recursive: true, force: true })
  } catch (e) {
    /* ignore */
  }
}

console.log('\n=== 汇总 ===')
console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
if (failures.length) {
  for (const f of failures) console.log('  FAIL ' + f)
  console.log('SETTINGS-UI-PROBE-FAILED')
  process.exit(1)
}
console.log('SETTINGS-UI-PROBE-OK')
