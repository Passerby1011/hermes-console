/**
 * hermes-console · 上下文压缩「按对话下钻」真浏览器验收 + 截图（t_c4c4d49e）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-vcc-conv.mjs
 *     node plugins/hermes-console/scripts/probe-vcc-conv.mjs --shots D:/tmp/shots
 *     node plugins/hermes-console/scripts/probe-vcc-conv.mjs --bundle <别的产物.js>
 *
 * 三层都是真的（与 probe-models-batch.mjs 同一套路）：
 *
 *   1. **真产物**：默认 ``dashboard/dist/index.js``（= ``desktop/plugin.js``）原样加载；
 *      ``--bundle`` 可以指到别处的产物（本卡并行推进时**不重写 dist**，用临时构建验证）。
 *   2. **真后端**：``scripts/models_batch_fixture.py --home <真 home>`` 按 dashboard 的方式
 *      加载**真实的** ``dashboard/plugin_api.py``；本探针只发 GET，一个字节都不写。
 *   3. **真浏览器**：Playwright + Chromium(msedge)，1440×900。
 *
 * 断言（对应卡片验收）：
 *   A. 对话下钻是默认视图，左侧列表有行、可搜索、可按「有压缩 / 有召回 / 无记录」筛；
 *   B. 点一个有压缩的对话 → 右侧出压缩明细（次数 / 摘要 / 压缩前后长度）；
 *   C. **token 与占比必须显示「无记录」**（本机 token_count 全 NULL），不许出现 0 假数字；
 *   D. 点一个无记录的对话 → 压缩与召回两块都出「无记录」空态（不是空白、不是假数字）；
 *   E. 全程零 console error / 零 pageerror；1440 与 900 宽都不出现横向滚动。
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

  // ---- 后端事实（先确认「有数据」这一侧真的有数据） ---------------------------
  const listUrl = 'http://127.0.0.1:' + FIXTURE_PORT + API +
    '/vcc/conversations?agent=' + encodeURIComponent(SCOPE_AGENT) + '&q=&filter=all&limit=500'
  const listBody = await (await fetch(listUrl)).json()
  const rows = Array.isArray(listBody.conversations) ? listBody.conversations : []
  check('后端：对话列表非空', rows.length > 0, 'n=' + rows.length)
  const compressed = rows.filter((r) => r.has_compression)
  const bare = rows.filter((r) => !r.has_compression && !r.has_recall)
  check('后端：有「有压缩」的对话', compressed.length > 0, 'n=' + compressed.length)
  check('后端：有「无记录」的对话', bare.length > 0, 'n=' + bare.length)
  // 验收用哪个会话：由后端事实挑（不靠列表顺序猜）
  const pickCompressed = compressed.filter((r) => Number(r.summary_distinct) >= 2)[0] || compressed[0]
  const pickBare = bare.filter((r) => Number(r.rows_total) > 0)[0] || bare[0]
  // 最"有料"的一个：压缩次数最多且还召回过 —— 一张图里同时看得到压缩与召回
  const pickRich = compressed
    .filter((r) => r.has_recall && Number(r.summary_distinct) >= 2)
    .sort((a, b) => Number(b.summary_distinct) - Number(a.summary_distinct))[0] || pickCompressed
  check('挑到「压缩 ≥ 2 次」的会话', Boolean(pickCompressed),
    pickCompressed ? pickCompressed.session.slice(0, 20) + '… / 压缩 ' + pickCompressed.summary_distinct + ' 次' : '')
  check('挑到「压缩 + 召回都有」的会话（截图用）', Boolean(pickRich),
    pickRich ? pickRich.session.slice(0, 20) + '… / 压缩 ' + pickRich.summary_distinct + ' 次 / 召回 ' + pickRich.recall_total + ' 次' : '')
  check('挑到「无压缩无召回」的会话', Boolean(pickBare),
    pickBare ? pickBare.session.slice(0, 20) + '… / ' + pickBare.rows_total + ' 条' : '')

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

  // ---- A. 对话下钻是默认视图 -------------------------------------------------
  await page.click('.hc-nav__item:has-text("上下文引擎")')
  await page.waitForSelector('[data-vcc-conv-view]', { timeout: 30000 })
  await page.waitForSelector('[data-vcc-conv]', { timeout: 30000 })
  const view = await page.getAttribute('.hc-block[data-vcc-view]', 'data-vcc-view')
  check('★默认视图 = 对话下钻（data-vcc-view=conv）', view === 'conv', String(view))
  const boot = await page.evaluate(() => ({
    store: window.__hcStore,
    reqs: (window.__hcRequests || []).slice(0, 6).map((r) => r.method + ' ' + r.path + ' → ' + r.status)
  }))
  console.log('[info] 作用域/请求：' + JSON.stringify(boot).slice(0, 500))
  const tabs = await page.$$eval('[data-vcc-tab]', (nodes) => nodes.map((n) => n.getAttribute('data-vcc-tab')))
  check('六个视图都在（含 conv）', tabs.length === 6 && tabs.indexOf('conv') === 0, tabs.join(','))
  const listCount = await page.$$eval('[data-vcc-conv]', (nodes) => nodes.length)
  check('左侧对话列表有行', listCount > 5, 'rows=' + listCount)

  const shot = async (name) => {
    await page.screenshot({ path: join(SHOTS, name) })
    console.log('  SHOT ' + name)
  }
  // 元素特写：面板比一屏高，整屏会切掉下半部分，所以右侧详情再单独截一张
  const shotEl = async (name, selector) => {
    const handle = await page.$(selector)
    if (!handle) {
      console.log('  SKIP ' + name + '（找不到 ' + selector + '）')
      return
    }
    await handle.scrollIntoViewIfNeeded()
    await page.waitForTimeout(200)
    await handle.screenshot({ path: join(SHOTS, name) })
    console.log('  SHOT ' + name + '（元素特写 ' + selector + '）')
  }

  // ---- 搜索真的能筛 ----------------------------------------------------------
  const firstLabel = await page.$eval('[data-vcc-conv]', (n) => (n.textContent || '').slice(0, 40))
  await page.fill('[data-vcc-conv-search]', 'zzz-不存在的关键词')
  await page.waitForTimeout(300)
  const afterSearch = await page.$$eval('[data-vcc-conv]', (nodes) => nodes.length)
  check('搜索无命中 ⇒ 列表清空（不是白屏）', afterSearch === 0, 'rows=' + afterSearch)
  const emptyText = await page.textContent('[data-vcc-conv-view] .hc-state__title')
  check('搜索无命中时给了空态说明', /没有匹配/.test(String(emptyText)), String(emptyText).slice(0, 40))
  await page.fill('[data-vcc-conv-search]', '')
  await page.waitForTimeout(300)
  const restored = await page.$$eval('[data-vcc-conv]', (nodes) => nodes.length)
  check('清空搜索后列表回来', restored === listCount, 'rows=' + restored)

  // ---- B. 一个有压缩的对话 ---------------------------------------------------
  await page.click('[data-vcc-conv-filter="compressed"]')
  await page.waitForTimeout(300)
  const compressedRows = await page.$$eval('[data-vcc-conv]', (nodes) => nodes.length)
  check('筛「有压缩」后列表变短', compressedRows > 0 && compressedRows <= listCount,
    'rows=' + compressedRows + ' / 全部 ' + listCount)
  // 用搜索框定位到「压缩 + 召回都有」的那个会话（顺便证明搜索真的能命中）
  await page.fill('[data-vcc-conv-search]', pickRich.session)
  await page.waitForTimeout(350)
  const hitRows = await page.$$eval('[data-vcc-conv]', (nodes) => nodes.length)
  check('搜索能按会话 id 精确命中（1 行）', hitRows === 1, 'rows=' + hitRows)
  await page.click('[data-vcc-conv="' + pickRich.session + '"]')
  await page.waitForSelector('[data-vcc-comp-event]', { timeout: 30000 })
  const compEvents = await page.$$eval('[data-vcc-comp-event]', (nodes) => nodes.length)
  check('★有压缩的对话：右侧出压缩明细', compEvents >= 1, 'events=' + compEvents)
  const detailSession = await page.getAttribute('[data-vcc-conv-detail]', 'data-vcc-conv-detail')
  check('详情对应选中的会话', Boolean(detailSession), String(detailSession).slice(0, 24) + '…')
  const detailText = await page.textContent('[data-vcc-conv-detail]')
  check('压缩明细里有「压缩前」与「压缩后」', /压缩前/.test(detailText) && /压缩后/.test(detailText))
  check('压缩明细里有摘要预览（看全文按钮）', /看全文/.test(detailText))
  check('上下文卡里有条数与字符数', /当前上下文/.test(detailText) && /字符/.test(detailText))

  // ---- C. token / 占比 必须是「无记录」 --------------------------------------
  const tokenNoRecord = await page.$('[data-vcc-norecord="tokens"]')
  check('★token 数显示「无记录」（不是 0）', Boolean(tokenNoRecord))
  const tokenText = tokenNoRecord ? await tokenNoRecord.textContent() : ''
  check('token「无记录」旁边写明了原因（token_count）', /token_count/.test(String(tokenText)), String(tokenText).slice(0, 60))
  const ratioNoRecord = await page.$('[data-vcc-norecord="ratio"]')
  check('★占模型窗口比例显示「无记录」', Boolean(ratioNoRecord))
  const statCards = await page.$$eval('.hc-card__value', (nodes) => nodes.map((n) => (n.textContent || '').trim()))
  check('统计卡里 token/占比 是「无记录」而不是 0',
    statCards.filter((t) => t === '无记录').length >= 2, JSON.stringify(statCards.slice(0, 6)))
  check('没有把 token 显示成 0', statCards.indexOf('0') === -1, JSON.stringify(statCards))

  await shot('vcc-conv-compressed.png')
  await shotEl('vcc-conv-compressed-detail.png', '[data-vcc-conv-detail]')
  const dumpCompressed = await page.evaluate(() => {
    const el = document.querySelector('[data-vcc-conv-detail]')
    return el ? el.innerText : ''
  })
  writeFileSync(join(SHOTS, 'detail-compressed.txt'), dumpCompressed, 'utf8')

  // 召回（这个会话既有 session_search 也有 memory-context）
  const recallEvents = await page.$$('[data-vcc-recall-event]')
  check('★有召回的对话：右侧出召回明细（每次召回了哪些）', recallEvents.length >= 1, 'events=' + recallEvents.length)
  const recallText = await page.textContent('[data-vcc-recall-event]')
  check('召回事件里有真实内容（查询词/命中标题/记忆条目）', String(recallText).length > 40,
    String(recallText).replace(/\s+/g, ' ').slice(0, 80))

  // ---- D. 一个无记录的对话 ---------------------------------------------------
  await page.fill('[data-vcc-conv-search]', '')
  await page.waitForTimeout(250)
  await page.click('[data-vcc-conv-filter="none"]')
  await page.waitForTimeout(400)
  await page.click('[data-vcc-conv]')
  await page.waitForSelector('[data-vcc-comp-none]', { timeout: 30000 })
  const compNoneText = await page.textContent('[data-vcc-comp-none]')
  check('★无记录对话：压缩块是空态并写「无记录」', /无记录/.test(String(compNoneText)), String(compNoneText).slice(0, 50))
  const recallNone = await page.$('[data-vcc-recall-none]')
  check('★无记录对话：召回块是空态并写「无记录」',
    Boolean(recallNone) && /无记录/.test(String(await recallNone.textContent())),
    recallNone ? String(await recallNone.textContent()).slice(0, 40) : 'missing')
  const noneText = await page.textContent('[data-vcc-conv-detail]')
  check('无记录对话也有上下文数字（不是空白页）', /当前上下文/.test(noneText) && /字符/.test(noneText))
  await shot('vcc-conv-none.png')
  await shotEl('vcc-conv-none-detail.png', '[data-vcc-conv-detail]')
  const dumpNone = await page.evaluate(() => {
    const el = document.querySelector('[data-vcc-conv-detail]')
    return el ? el.innerText : ''
  })
  writeFileSync(join(SHOTS, 'detail-none.txt'), dumpNone, 'utf8')

  // ---- E. 无横向滚动 ---------------------------------------------------------
  const wide = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth
  }))
  check('1440 宽无横向滚动', wide.scroll <= wide.client + 1, JSON.stringify(wide))
  // 左右两栏真的并排（不是叠成一列、也不是空白右栏）
  const layout = await page.evaluate(() => {
    const box = (sel) => {
      const el = document.querySelector(sel)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }
    }
    const overflowing = []
    document.querySelectorAll('[data-vcc-conv-view] *').forEach((el) => {
      const style = window.getComputedStyle(el)
      // overflow != visible 的元素自己会裁（省略号/滚动条），不算横向溢出
      if (style.overflowX !== 'visible') return
      if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
        overflowing.push((el.className || el.tagName) + ' ' + el.scrollWidth + '>' + el.clientWidth)
      }
    })
    return { list: box('[data-vcc-conv-pane="list"]'), detail: box('[data-vcc-conv-pane="detail"]'), overflowing: overflowing.slice(0, 5) }
  })
  check('左栏在左、右栏在右（并排）',
    layout.list && layout.detail && layout.list.x + layout.list.w <= layout.detail.x + 2,
    JSON.stringify({ list: layout.list, detail: layout.detail }))
  check('右栏有足够宽度（不是空白/被挤扁）', layout.detail && layout.detail.w >= 700,
    layout.detail ? 'w=' + layout.detail.w : 'missing')
  check('面板内没有横向溢出的元素', layout.overflowing.length === 0, layout.overflowing.join(' | '))

  await page.setViewportSize({ width: 900, height: 900 })
  await page.waitForTimeout(400)
  const narrow = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth
  }))
  check('900 宽无横向滚动', narrow.scroll <= narrow.client + 1, JSON.stringify(narrow))
  const narrowFlag = await page.getAttribute('.hc-block[data-vcc-view]', 'data-vcc-narrow')
  check('900 宽切到窄窗布局（data-vcc-narrow=true）', narrowFlag === 'true', String(narrowFlag))
  await shot('vcc-conv-narrow.png')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.waitForTimeout(300)

  // ---- F. 全程无报错 ---------------------------------------------------------
  check('零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  console.log('\n[info] 首个对话行文本：' + String(firstLabel).replace(/\s+/g, ' '))
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
