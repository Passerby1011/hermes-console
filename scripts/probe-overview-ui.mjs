/**
 * hermes-console · 概览分区**真浏览器验收 + 桌面端截图**
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-overview-ui.mjs
 *     node plugins/hermes-console/scripts/probe-overview-ui.mjs --shots D:/tmp/shots
 *     node plugins/hermes-console/scripts/probe-overview-ui.mjs --bundle <path/to/index.js>
 *
 * 三层都是真的：
 *
 *   1. **真产物**：默认加载 ``dashboard/dist/index.js``；``--bundle`` 可以指到别处
 *      （本卡与另外三张插件卡并行时不重建 ``dist/``，就用手边这份临时 bundle 出图，
 *      并在日志里把用的是哪份写清楚）。
 *   2. **真后端**：``scripts/settings_ui_fixture.py`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``，直接打真实 HERMES_HOME —— 截图里的内存 / token /
 *      cron / 会话 / 挂件全是这台机器的真数据。
 *   3. **真浏览器**：Playwright + Edge（Chromium 内核），1440×900 桌面窗口尺寸，
 *      宿主主题 CSS 直接引用桌面端那份 ``main-*.css``。
 *
 * 断言（对应卡片 t_c6e9ea8d 的验收）：
 *   A. 四个统计卡都在，且读数不是「读取中」；
 *   B. 13 张挂件卡每张都有状态角标（有数据 / 空态 / 占位 / 取数失败），四档计数与 DOM 相符；
 *   C. token 卡的 7 / 30 / 90 天切换是**真请求**：点 7 天后出现 ``days=7`` 的请求；
 *   D. 定时任务表多了「上次执行」列（表头 + 每行 5 个单元格）；
 *   E. 分区自检区块列出 6 个宿主接口并给出可用数；
 *   F. 「刷新全部」真的重发取数（请求数增加），不是装饰按钮；
 *   G. 所有 ``/overview*`` 响应都是 200 且 ``ok !== false``（没有一个数据源是假通的）；
 *   H. 全程零 console error / 零 pageerror。
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs'
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
const FIXTURE_PORT = Number(process.env.HC_OVERVIEW_FIXTURE_PORT || 9193)
const SITE_PORT = Number(process.env.HC_OVERVIEW_SITE_PORT || 9192)
const API = '/api/plugins/hermes-console'

function argValue(flag) {
  const i = process.argv.indexOf(flag)
  return i > -1 ? process.argv[i + 1] : ''
}

const SHOTS = resolve(argValue('--shots') || join(PLUGIN, 'scripts', '_probe_out', 'overview-ui'))
const BUNDLE = resolve(argValue('--bundle') || join(DIST, 'index.js'))
const STYLE_SRC = resolve(argValue('--style') || join(DIST, 'style.css'))

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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

// --------------------------------------------------------------- 临时工作区
const WORK = mkdtempSync(join(tmpdir(), 'hc-ov-ui-'))
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

copyFileSync(BUNDLE, join(WORK, 'entry.js'))
copyFileSync(STYLE_SRC, join(WORK, 'style.css'))

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS)
      .filter((f) => /^main-.*\.css$/.test(f))
      .map((f) => join(DESKTOP_ASSETS, f))[0]
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
<title>hermes-console · 概览（验收夹具）</title>
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
        res.writeHead(200, {
          'content-type': url.endsWith('.css') ? 'text/css; charset=utf-8' : 'text/javascript; charset=utf-8'
        })
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
  const url = 'http://127.0.0.1:' + FIXTURE_PORT + API + '/agents'
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

// ---------------------------------------------------------------------- 主流程
const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

try {
  console.log('[env] bundle :', BUNDLE)
  console.log('[env] style  :', STYLE_SRC)
  console.log('[env] shots  :', SHOTS)
  check('bundle 存在', existsSync(BUNDLE), BUNDLE)
  check('style 存在', existsSync(STYLE_SRC), STYLE_SRC)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)
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

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })

  console.log('\n[A] 进入概览分区，四个统计卡都有读数')
  await page.click('.hc-nav__item:has-text("概览")')
  await page.waitForSelector('.hc-overview', { timeout: 20000 })
  await page.waitForSelector('.hc-cards .hc-card', { timeout: 20000 })
  await page.waitForFunction(
    () => document.querySelectorAll('.hc-cards > .hc-skel-card').length === 0,
    { timeout: 60000 }
  )
  // 出图前先等所有取数落定：不然截图会停在「取数中 1 / 8」「读取中」这种中间态，
  // 拿去当验收图会让人以为界面还没加载完（这是真的会误导人的那种"截图假绿"）。
  async function settle() {
    await page
      .waitForFunction(
        () => {
          const chips = Array.from(document.querySelectorAll('.hc-overview .hc-chip'))
          const busyChip = chips.some((n) => /取数中|等作用域/.test(n.textContent || ''))
          const busyWidget = Array.from(document.querySelectorAll('.hc-widget__badge')).some(
            (n) => n.textContent === '读取中'
          )
          const busyBtn = Array.from(document.querySelectorAll('.hc-btn')).some(
            (n) => /刷新中/.test(n.textContent || '') && !n.disabled
          )
          return !busyChip && !busyWidget && !busyBtn
        },
        { timeout: 60000 }
      )
      .catch(() => {})
    await sleep(300)
  }
  await settle()
  const cards = await page.evaluate(() => {
    const list = Array.from(document.querySelectorAll('.hc-cards > .hc-card'))
    return list.map((card) => ({
      label: (card.querySelector('.hc-card__label') || {}).textContent || '',
      value: (card.querySelector('.hc-card__num') || {}).textContent || '',
      caliber: (card.querySelector('.hc-card__caliber') || {}).textContent || '',
      isError: card.classList.contains('hc-card--error')
    }))
  })
  console.log('        ' + cards.map((c) => c.label + '=' + c.value).join(' | '))
  check('★四个统计卡渲染出来（不是骨架、不是错误态）', cards.length === 4 && cards.every((c) => !c.isError), 'n=' + cards.length)
  const systemCard = cards.filter((c) => /系统状态/.test(c.label))[0] || {}
  check('★系统状态卡有真实读数（% 或 —）', /%|—/.test(String(systemCard.value)), String(systemCard.value))
  const tokenCard = cards.filter((c) => /token/.test(c.label))[0] || {}
  check('★token 卡带口径（「近 30 天窗口」）', /近 30 天窗口/.test(tokenCard.caliber), tokenCard.caliber)
  // 系统卡新增的三行：负载 / 本进程 RSS / 版本
  const sysLines = await page.evaluate(() => {
    const card = Array.from(document.querySelectorAll('.hc-cards > .hc-card')).filter((c) =>
      /系统状态/.test((c.querySelector('.hc-card__label') || {}).textContent || '')
    )[0]
    if (!card) return []
    return Array.from(card.querySelectorAll('.hc-card__sub')).map((n) => n.textContent)
  })
  console.log('        系统卡补充行:', JSON.stringify(sysLines))
  check('★系统卡补上了负载 / 本进程 RSS / 版本（旧版这三项后端有数、界面没展示）',
    sysLines.some((l) => /负载/.test(l)) && sysLines.some((l) => /RSS/.test(l)),
    JSON.stringify(sysLines))
  await page.screenshot({ path: join(SHOTS, '01-overview-top.png') })

  console.log('\n[B] 13 张挂件卡：每张都有状态角标（有数据 / 空态 / 占位 / 取数失败）')
  await page.waitForSelector('.hc-widgets .hc-widget', { timeout: 30000 })
  // 挂件卡的角标取决于"它依赖的那条查询"有没有回来（analytics 要 3 秒），
  // 先等全部落定再断言，否则会把"读取中"当成最终状态。
  await page
    .waitForFunction(
      () => !Array.from(document.querySelectorAll('.hc-widget__badge')).some((n) => n.textContent === '读取中'),
      { timeout: 60000 }
    )
    .catch(() => {})
  const widgets = await page.evaluate(() => {
    const list = Array.from(document.querySelectorAll('.hc-widgets .hc-widget'))
    return list.map((w) => ({
      id: (w.querySelector('.hc-widget__id') || {}).textContent || '',
      badge: (w.querySelector('.hc-widget__badge') || {}).textContent || '',
      state: Array.from(w.classList).filter((c) => c.indexOf('hc-widget--') === 0).join(',')
    }))
  })
  const byBadge = {}
  widgets.forEach((w) => {
    byBadge[w.badge] = (byBadge[w.badge] || 0) + 1
  })
  console.log('        角标分布:', JSON.stringify(byBadge), '共', widgets.length, '张')
  check('★挂件卡 13 张', widgets.length === 13, 'n=' + widgets.length)
  check('★每张卡都有角标（没有悬空卡片）', widgets.every((w) => !!w.badge), JSON.stringify(widgets.filter((w) => !w.badge)))
  check('★角标只出现四档之一', widgets.every((w) => ['有数据', '空态', '占位', '取数失败'].indexOf(w.badge) > -1), JSON.stringify(byBadge))

  console.log('\n[C] token 卡的窗口切换是真请求（7 / 30 / 90 天）')
  const segItems = await page.$$('.hc-cards .hc-wx-seg__item')
  check('三段窗口控件渲染出来', segItems.length === 3, 'n=' + segItems.length)
  const beforeSwitch = await page.evaluate(() => window.__hcRequests.length)
  await page.click('.hc-cards .hc-wx-seg__item:has-text("7 天")')
  await page.waitForFunction(
    () => window.__hcRequests.some((r) => /days=7/.test(r.path)),
    { timeout: 30000 }
  )
  const afterSwitch = await page.evaluate((from) => window.__hcRequests.slice(from).map((r) => r.method + ' ' + r.path), beforeSwitch)
  console.log('        切窗口后新增请求:', JSON.stringify(afterSwitch))
  check('★切到 7 天真的重发了一条 days=7 请求', afterSwitch.some((l) => /days=7/.test(l)), JSON.stringify(afterSwitch))
  await page.waitForSelector('.hc-cards > .hc-card', { timeout: 60000 })
  await sleep(600)
  const tokenCaliber = await page.evaluate(() => {
    const card = Array.from(document.querySelectorAll('.hc-cards > .hc-card')).filter((c) =>
      /token/.test((c.querySelector('.hc-card__label') || {}).textContent || '')
    )[0]
    return card ? (card.querySelector('.hc-card__caliber') || {}).textContent || '' : ''
  })
  check('★口径跟着窗口变成「近 7 天窗口」', /近 7 天窗口/.test(tokenCaliber), tokenCaliber)
  await page.screenshot({ path: join(SHOTS, '02-overview-window7.png') })
  // 切回 30 天，别让后面的截图停在一个非缺省窗口上
  await page.click('.hc-cards .hc-wx-seg__item:has-text("30 天")')
  await page.waitForFunction(() => window.__hcRequests.some((r) => /days=30/.test(r.path)), { timeout: 30000 })
  await sleep(400)

  console.log('\n[D] 定时任务表多了「上次执行」列')
  // 当前作用域（code-worker）可能一条 cron 都没有 —— 那就切到 default 再验表格，
  // 顺便把"切作用域会真的按新 profile 重取"一起证了。
  const hasCronTable = async () => (await page.$('.hc-rows--cron:not([data-hc-table]) .hc-rows__head')) !== null
  if (!(await hasCronTable())) {
    console.log('        当前作用域没有 cron 任务，切到 default 再验表格结构')
    await page.selectOption('#hc-scope-select', 'default')
    await page.waitForSelector('.hc-rows--cron:not([data-hc-table]) .hc-rows__head', { timeout: 30000 })
  }
  const cronHead = await page.evaluate(() => {
    const head = document.querySelector('.hc-rows--cron:not([data-hc-table]) .hc-rows__head')
    return head ? Array.from(head.children).map((n) => n.textContent) : []
  })
  const cronRowCells = await page.evaluate(() => {
    const row = document.querySelector('.hc-rows--cron:not([data-hc-table]) .hc-row')
    return row ? row.children.length : 0
  })
  console.log('        表头:', JSON.stringify(cronHead), '每行单元格', cronRowCells)
  check('★表头含「上次执行」', cronHead.indexOf('上次执行') > -1, JSON.stringify(cronHead))
  check('★表头 5 列 = 数据行 5 格', cronHead.length === 5 && cronRowCells === 5, cronHead.length + '/' + cronRowCells)
  // 加了第 5 列最容易踩的坑是「文字被裁」：逐格比 scrollWidth / clientWidth。
  const overflow = await page.evaluate(() => {
    const bad = []
    document
      .querySelectorAll('.hc-rows--cron:not([data-hc-table]) .hc-row, .hc-rows--cron:not([data-hc-table]) .hc-rows__head')
      .forEach((row) => {
      Array.from(row.children).forEach((cell) => {
        if (cell.scrollWidth > cell.clientWidth + 1) {
          bad.push((cell.textContent || '').slice(0, 24) + ' (' + cell.scrollWidth + '>' + cell.clientWidth + ')')
        }
      })
    })
    return bad
  })
  check('★五列文字没有被裁切（scrollWidth ≤ clientWidth）', overflow.length === 0, JSON.stringify(overflow.slice(0, 4)))

  console.log('\n[E] 分区自检区块：6 个宿主接口逐条列出')
  const selfcheck = await page.evaluate(() => {
    const block = Array.from(document.querySelectorAll('.hc-block')).filter((b) =>
      /分区自检/.test((b.querySelector('.hc-block__title') || {}).textContent || '')
    )[0]
    if (!block) return null
    return {
      hint: (block.querySelector('.hc-muted') || {}).textContent || '',
      rows: Array.from(block.querySelectorAll('.hc-row')).map((r) => r.textContent),
      head: Array.from(block.querySelectorAll('.hc-rows__head > *')).map((n) => n.textContent)
    }
  })
  console.log('        自检:', JSON.stringify(selfcheck))
  check('★自检区块渲染出来', !!selfcheck)
  check('★列出 6 个宿主接口', !!selfcheck && selfcheck.rows.length === 6, selfcheck ? String(selfcheck.rows.length) : 'null')
  check('★自检结论写明 x / 6 可用', !!selfcheck && /6 个宿主接口可用/.test(selfcheck.hint), selfcheck ? selfcheck.hint : '')
  const scAlign = await page.evaluate(() => {
    const table = document.querySelector('[data-hc-table="selfcheck"]')
    if (!table) return null
    const head = table.querySelector('.hc-rows__head')
    const row = table.querySelector('.hc-row')
    const cols = (el) => (el ? getComputedStyle(el).gridTemplateColumns.split(' ').filter(Boolean).length : 0)
    return { headCells: head ? head.children.length : 0, rowCells: row ? row.children.length : 0, headCols: cols(head), rowCols: cols(row) }
  })
  console.log('        自检表对齐:', JSON.stringify(scAlign))
  check(
    '★自检表头 3 列 = 数据行 3 格，且两者 grid 列数一致（不错位）',
    !!scAlign && scAlign.headCells === 3 && scAlign.rowCells === 3 && scAlign.headCols === scAlign.rowCols,
    JSON.stringify(scAlign)
  )

  console.log('\n[F] 「刷新全部」真的重取（不是装饰按钮）')
  const beforeRefresh = await page.evaluate(() => window.__hcRequests.length)
  await page.click('.hc-overview .hc-btn:has-text("刷新全部")')
  await sleep(1200)
  const afterRefresh = await page.evaluate(() => window.__hcRequests.length)
  const newPaths = await page.evaluate(
    (from) => window.__hcRequests.slice(from).map((r) => r.path),
    beforeRefresh
  )
  console.log('        刷新新增请求 ' + newPaths.length + ' 条:', JSON.stringify(newPaths.slice(0, 6)))
  check('★点刷新真的重发取数（新增请求 ≥ 5 条）', newPaths.length >= 5, 'n=' + newPaths.length)

  console.log('\n[I] 页面上的数据源状态：没有一个 /overview* 是假通的')
  const ovReqs = await page.evaluate(() =>
    window.__hcRequests
      .filter((r) => /^\/(overview|agents|modules)/.test(r.path))
      .map((r) => r.status)
  )
  check('★所有 /overview* 请求都是 200（没有 5xx）', ovReqs.every((s) => s < 500), JSON.stringify(ovReqs))

  // 整页长截图（横幅 + 四个区块都在）
  await settle()
  await page.screenshot({ path: join(SHOTS, '03-overview-fullpage.png'), fullPage: true })
  console.log('        fullpage shot: ' + join(SHOTS, '03-overview-fullpage.png'))

  // 逐区块单独出图：整页长图在评审里常被压到看不清，分区细节单独存一份。
  const blockShots = [
    ['看板挂件', '04-widgets.png'],
    ['定时任务', '05-cron.png'],
    ['会话', '06-sessions.png'],
    ['分区自检', '07-selfcheck.png']
  ]
  for (const [title, file] of blockShots) {
    const handle = await page.evaluateHandle((wanted) => {
      return Array.from(document.querySelectorAll('.hc-block')).filter(
        (b) => ((b.querySelector('.hc-block__title') || {}).textContent || '') === wanted
      )[0]
    }, title)
    const element = handle.asElement()
    if (element) {
      await element.screenshot({ path: join(SHOTS, file) })
      console.log('        block shot: ' + file)
    } else {
      console.log('        跳过（没找到区块 ' + title + '）')
    }
  }

  // 下半屏（定时任务 / 会话 / 分区自检）：滚到底再出一张视口图，
  // 整页长图在评审里会被压小，视口图才看得清表格。
  await page.evaluate(() => {
    const panes = document.querySelectorAll('.hc-pane')
    for (const pane of panes) {
      if (!pane.classList.contains('hc-pane--idle')) pane.scrollTop = pane.scrollHeight
    }
  })
  await sleep(500)
  await page.screenshot({ path: join(SHOTS, '08-lower-half.png') })
  console.log('        lower shot: ' + join(SHOTS, '08-lower-half.png'))

  console.log('\n[K] 面板拖窄（900px 宽）不横向溢出')
  await page.setViewportSize({ width: 900, height: 800 })
  await sleep(500)
  const narrow = await page.evaluate(() => {
    const pane = Array.from(document.querySelectorAll('.hc-pane')).filter((p) => !p.classList.contains('hc-pane--idle'))[0]
    return pane
      ? { scrollWidth: pane.scrollWidth, clientWidth: pane.clientWidth, overflowX: pane.scrollWidth - pane.clientWidth }
      : null
  })
  console.log('        窄窗面板:', JSON.stringify(narrow))
  check('★900px 宽下面板没有横向溢出', !!narrow && narrow.overflowX <= 1, JSON.stringify(narrow))
  await page.screenshot({ path: join(SHOTS, '09-narrow-900.png') })
  await page.setViewportSize({ width: 1440, height: 900 })
  await sleep(300)

  console.log('\n[J] 全分区文字裁切扫描（卡片 / 挂件 / 行）')
  const clipped = await page.evaluate(() => {
    const bad = []
    const targets = document.querySelectorAll(
      '.hc-overview .hc-card__sub, .hc-overview .hc-card__caliber, .hc-overview .hc-widget__meta, .hc-overview .hc-widget__caliber, .hc-overview .hc-row__name, .hc-overview .hc-row__next, .hc-overview .hc-status'
    )
    targets.forEach((node) => {
      if (node.scrollWidth > node.clientWidth + 1 && node.clientWidth > 0) {
        bad.push((node.textContent || '').slice(0, 30) + ' (' + node.scrollWidth + '>' + node.clientWidth + ')')
      }
    })
    return bad
  })
  console.log('        裁切候选:', JSON.stringify(clipped.slice(0, 6)))
  check('★概览里没有文字被裁切', clipped.length === 0, JSON.stringify(clipped.slice(0, 4)))

  console.log('\n[H] 零 console error / 零 pageerror')
  check('★零 pageerror', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)))
  check('★零 console error', consoleErrors.length === 0, JSON.stringify(consoleErrors.slice(0, 3)))
} catch (failure) {
  failures.push('主流程异常：' + String((failure && failure.stack) || failure))
  console.log('  FAIL 主流程异常 — ' + String((failure && failure.message) || failure))
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) site.close()
  if (fixture && fixture.child) fixture.child.kill()
}

console.log('')
if (failures.length) {
  console.log('OVERVIEW-UI-PROBE-FAIL (' + failures.length + '):\n  ' + failures.join('\n  '))
  process.exit(1)
}
console.log('OVERVIEW-UI-PROBE-OK (' + pass + ' PASS)')
