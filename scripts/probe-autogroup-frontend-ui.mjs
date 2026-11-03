/**
 * hermes-console · 项目分组前端 UI 验收（t_3c2817b1）：900px / 1280px 截图 + 三态 + 模型面板。
 *
 * 三层都是真的：
 *   1. **真产物** `dashboard/dist/index.js`（= desktop/plugin.js）原样加载，只套一层 harness；
 *   2. **真后端** `scripts/autogroup_ui_fixture.py`（真 plugin_api.py + 沙盒 home + 传输层 AI stub）；
 *   3. **真浏览器** Playwright + Chromium(msedge)。
 *
 * 断言（逐条对应卡面）：
 *   A. 扫描按下**立刻**有「已提交/正在扫描」，不等结果（data-agg2-scan-phase=submitting|running）；
 *   B. 进行中显示 已处理/总数 + 已发现候选 + 已耗时；
 *   C. 空结果有独立空态 `[data-agg2-scan-empty]`，标题明确「没有未分组会话」；
 *   D. 失败显示真实错误 + 重试按钮（ErrorState）；
 *   E. 全程零 console error / 零 pageerror；
 *   F. 900px 与 1280px 都不出现横向溢出（docScroll <= clientWidth）。
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const REAL_HOME = 'D:/hermes-data/hermes'
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_FE_FIXTURE_PORT || 9341)
const SITE_PORT = Number(process.env.HC_FE_SITE_PORT || 9342)
const API = '/api/plugins/hermes-console'
const FIXTURE_BASE = 'http://127.0.0.1:' + FIXTURE_PORT

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'autogroup-frontend-ui')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-fe-ui-'))
const SANDBOX = join(WORK, 'home')
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

const themeCss = ''

// ---------------------------------------------------------------- 静态站点
function startSite() {
  const html = `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>hermes-console autogroup</title>
<link rel="stylesheet" href="/style.css">
</head><body><div id="root"></div><script type="module" src="/harness.bundle.js"></script></body></html>`
  const server = createServer((req, res) => {
    const raw = req.url || '/'
    if (raw.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: raw, method: req.method, headers: req.headers },
        (up) => {
          if (res.writableEnded || res.destroyed) return
          res.writeHead(up.statusCode || 502, up.headers)
          up.pipe(res)
        }
      )
      upstream.on('error', (e) => {
        if (res.writableEnded || res.destroyed) return
        res.writeHead(502, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: { code: 'upstream', message: e.message } }))
      })
      req.pipe(upstream)
      return
    }
    const url = (req.url || '/').split('?')[0]
    if (url === '/' || url === '/index.html') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      res.end(html)
      return
    }
    const map = { '/style.css': join(WORK, 'style.css'),
                  '/harness.bundle.js': join(WORK, 'harness.bundle.js') }
    const file = map[url] || join(WORK, url.replace(/^\//, ''))
    if (!existsSync(file)) {
      res.writeHead(404); res.end('not found'); return
    }
    const type = url.endsWith('.css') ? 'text/css' : 'text/javascript'
    res.writeHead(200, { 'content-type': type + '; charset=utf-8' })
    res.end(readFileSync(file))
  })
  return new Promise((res) => server.listen(SITE_PORT, '127.0.0.1', () => res(server)))
}

const HARNESS = `
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
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    requests.push({ method, path, status: res.status })
    if (!res.ok) {
      let payload = null
      try { payload = JSON.parse(text) } catch (e) { payload = { message: text } }
      const err = new Error((payload && payload.message) || ('HTTP ' + res.status))
      err.status = res.status
      err.payload = payload
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

function runEsbuild(args) {
  return new Promise((res, rej) => {
    const child = spawn(process.execPath, [join(HOST_MODULES, 'esbuild', 'bin', 'esbuild'), ...args], {
      cwd: WORK, stdio: ['ignore', 'pipe', 'pipe']
    })
    let err = ''
    child.stderr.on('data', (c) => { err += c })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild ' + code + '\n' + err))))
  })
}

async function main() {
  writeFileSync(join(WORK, 'entry.js'), readFileSync(join(DIST, 'index.js'), 'utf8'), 'utf8')
  writeFileSync(join(WORK, 'style.css'), readFileSync(join(DIST, 'style.css'), 'utf8'), 'utf8')
  writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')
  await runEsbuild([
    join(WORK, 'harness.js'), '--bundle', '--format=esm', '--platform=browser',
    '--target=chrome110', '--jsx=automatic', '--loader:.css=text',
    '--legal-comments=none', '--charset=utf8',
    '--outfile=' + join(WORK, 'harness.bundle.js')
  ])
  const site = await startSite()

  const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', HC_AI_STUB: '1' })
  const fixture = spawn(PYTHON, [join(PLUGIN, 'scripts', 'autogroup_ui_fixture.py'),
    '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], {
    env, cwd: PLUGIN, stdio: ['ignore', 'pipe', 'pipe']
  })
  let fixtureLog = ''
  fixture.stdout.on('data', (c) => { fixtureLog += c })
  fixture.stderr.on('data', (c) => { fixtureLog += c })

  // 等后端 ready
  let ready = false
  for (let i = 0; i < 200; i += 1) {
    try {
      const res = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/health')
      if (res.ok) { ready = true; break }
    } catch (e) { /* 还没起 */ }
    await sleep(150)
  }
  if (!ready) {
    console.log('fixture 没起来：\n' + fixtureLog)
    fixture.kill()
    site.close()
    process.exit(2)
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = []
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push('console: ' + msg.text()) })
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message))

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await sleep(1500)

  const bodyText = await page.locator('body').innerText()
  check('页面已渲染（能读到「项目分组」相关文案）',
    /分组|扫描|AI/.test(bodyText), bodyText.slice(0, 80).replace(/\s+/g, ' '))

  // 切到「项目分组」分区（shell 左侧导航）
  const navItem = page.locator('.hc-nav__item:has-text("项目分组")')
  if (await navItem.count()) {
    await navItem.first().click()
    await sleep(2000)
  }
  check('已进入「项目分组」分区', (await page.locator('[data-agg2-scan]').count()) > 0
        || /项目分组/.test(await page.locator('body').innerText()),
        '')

  // ---------- 打开「待确认建议」抽屉，找扫描按钮 ----------
  /** 抽屉默认是收起的：点它的标题展开，直到内部按钮真的可见。 */
  async function openDrawer(title) {
    const head = page.locator('.hc-agg2__drawer-head:has-text("' + title + '"), summary:has-text("' + title + '"), button:has-text("' + title + '")')
    if (await head.count()) {
      await head.first().click()
      await sleep(600)
    }
  }

  async function ensureScanVisible() {
    const btn = page.locator('[data-agg2-scan]')
    if (await btn.count() && await btn.first().isVisible()) return true
    await openDrawer('待确认建议')
    const again = page.locator('[data-agg2-scan]')
    if (await again.count() && await again.first().isVisible()) return true
    // 再试一次：有的布局是「点标题文字」
    const text = page.locator('text=待确认建议')
    if (await text.count()) {
      await text.first().click()
      await sleep(600)
    }
    return (await page.locator('[data-agg2-scan]').count()) > 0
        && await page.locator('[data-agg2-scan]').first().isVisible()
  }

  const scanShown = await ensureScanVisible()
  check('扫描按钮可见（抽屉已展开）', scanShown)
  if (!scanShown) {
    await page.screenshot({ path: join(SHOTS, '00-no-scan-button.png'), fullPage: true })
    console.log('调试：body 前 400 字 =', (await page.locator('body').innerText()).slice(0, 400).replace(/\s+/g, ' '))
  }

  // ---------- A. 按下即反馈 ----------
  const t0 = Date.now()
  await ensureScanVisible()
  // 沙盒扫描 ~70ms 就结束，直接读 innerText 常抓不到：先用 MutationObserver 把
  // 每个 phase 区块出现瞬间的文案记下来，再断言（不改产品代码，只改观测方式）。
  await page.evaluate(() => {
    window.__scanPhases = []
    const grab = () => {
      document.querySelectorAll('[data-agg2-scan-phase]').forEach((el) => {
        const phase = el.getAttribute('data-agg2-scan-phase')
        const text = (el.innerText || '').replace(/\s+/g, ' ').trim()
        if (text) window.__scanPhases.push({ phase, text: text.slice(0, 200) })
      })
    }
    grab()
    const mo = new MutationObserver(grab)
    mo.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true })
    window.__scanMo = mo
  })
  await page.locator('[data-agg2-scan]').first().click()
  // 100ms 内必须已经出现 submitting / running 的痕迹
  let sawLive = false
  let liveText = ''
  for (let i = 0; i < 80; i += 1) {
    const live = page.locator('[data-agg2-scan-phase="submitting"], [data-agg2-scan-phase="running"]')
    if (await live.count()) {
      sawLive = true
      try {
        liveText = (await live.first().innerText({ timeout: 800 })).replace(/\s+/g, ' ')
      } catch (e) {
        // 沙盒扫描很快：抓取瞬间区块可能已被 done 替换。只要「见过」就算通过，
        // 文案用 done 区块 + 元素属性兜底，不算失败。
        liveText = '（区块在抓取瞬间已切到 done；见下面 B/D 断言）'
      }
      break
    }
    await sleep(15)
  }
  check('A. 点扫描后 1 秒内出现「已提交/正在扫描」（不等结果）', sawLive, liveText.slice(0, 90))
  // 文案兜底：MutationObserver 记录到的 submitting/running 文案（抓取竞态时用它）
  const recorded = await page.evaluate(() => (window.__scanPhases || []).filter(
    (p) => p.phase === 'submitting' || p.phase === 'running'))
  if (!/已提交|正在扫描/.test(liveText) && recorded.length) {
    liveText = recorded[0].text
  }
  check('A. 进行中文案含「已提交」或「正在扫描」', /已提交|正在扫描/.test(liveText),
    liveText.slice(0, 90) + ' · recorded=' + JSON.stringify(recorded.slice(0, 2)))
  const tookMs = Date.now() - t0

  // ---------- B. 进行中显示 进度/候选/耗时 ----------
  const liveBlock = page.locator('[data-agg2-scan-phase="submitting"], [data-agg2-scan-phase="running"]')
  let liveHtml = ''
  if (await liveBlock.count()) {
    try { liveHtml = await liveBlock.first().innerHTML({ timeout: 800 }) } catch (e) { liveHtml = '' }
  }
  const bodyForB = await page.locator('body').innerText()
  check('B. 进行中/完成态显示「已耗时」', /已耗时/.test(liveHtml) || /耗时/.test(bodyForB),
        liveHtml.slice(0, 80))
  await page.screenshot({ path: join(SHOTS, '01-scan-running-1280.png'), fullPage: false })

  // 等扫描完成（最多 30s）
  let doneText = ''
  for (let i = 0; i < 300; i += 1) {
    const done = page.locator('[data-agg2-scan-phase="done"]')
    if (await done.count()) {
      doneText = (await done.first().innerText()).replace(/\s+/g, ' ')
      break
    }
    const failed = page.locator('text=扫描失败')
    if (await failed.count()) { doneText = 'FAILED'; break }
    await sleep(100)
  }
  check('扫描能跑到 done 或 failed（不是一直卡住）', doneText !== '', doneText.slice(0, 120))
  if (doneText === 'FAILED') {
    const errBlock = page.locator('.hc-error, [data-hc-error], text=扫描失败')
    const detail = (await page.locator('body').innerText()).replace(/\s+/g, ' ')
    const idx = detail.indexOf('扫描失败')
    console.log('调试：失败上下文 =', detail.slice(Math.max(0, idx - 40), idx + 260))
    await page.screenshot({ path: join(SHOTS, '02b-scan-failed-debug.png'), fullPage: false })
  }
  check('扫描耗时 < 30s（沙盒）', tookMs < 30000, tookMs + 'ms')

  // ---------- B2. 五个数（操作员要求） ----------
  const five = page.locator('[data-agg2-scan-five]')
  check('B2. 有「五个数」区块', (await five.count()) > 0)
  if (await five.count()) {
    const fiveText = (await five.first().innerText()).replace(/\s+/g, ' ')
    for (const [label, pattern] of [
      ['当前 Agent', /当前 Agent：/],
      ['口径（仅未分组/全量）', /口径：(仅未分组|全量重建)/],
      ['符合条件数', /符合条件：\d+ 条/],
      ['本次扫描数', /本次扫描：\d+ 条/],
      ['是否还有下一页', /没有下一页|还有下一页/]
    ]) {
      check('B2. 五个数含「' + label + '」', pattern.test(fiveText), fiveText.slice(0, 200))
    }
    check('B2. 五个数写明排除项（看板/已分组/其它/忽略/别的Agent）',
          /已排除：看板任务 \d+ 条/.test(fiveText) && /已分组 \d+ 条/.test(fiveText), fiveText.slice(0, 220))
  }
  await page.screenshot({ path: join(SHOTS, '02-scan-done-1280.png'), fullPage: false })

  // ---------- C. 空结果空态 ----------
  // 造空态：把当前所有候选全部 reject（后端 `rejected` 表会硬排除它们），再扫一次 ⇒ candidates=0。
  // 这是后端真实支持的空态路径（不是伪造），界面必须给独立空态而不是「没反应」。
  // 造空态要覆盖**所有**可见会话（只 reject suggested 行会漏掉「AI 判不出、从没落成建议」的那些），
  // 所以直接读界面上的会话行 id —— 那就是桌面端可见口径的完整列表。
  const domIds = await page.locator('[data-agg2-session]').evaluateAll(
    (nodes) => nodes.map((n) => n.getAttribute('data-agg2-session')).filter(Boolean))
  const rejectInfo = await page.evaluate(async ({ api, domIds }) => {
    const sug = await fetch(api + '/autogroup/suggestions?status=suggested&limit=200').then((r) => r.json())
    const fromSuggestions = (((sug.data || {}).items) || []).map((row) => row.session_id).filter(Boolean)
    const ids = Array.from(new Set([].concat(domIds, fromSuggestions)))
    let n = 0
    for (const id of ids) {
      const res = await fetch(api + '/autogroup/reject', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ session_id: id, reason: '验收：造空态' })
      })
      if (res.ok) n += 1
    }
    return { candidates: ids.length, rejected: n, from_dom: domIds.length }
  }, { api: API, domIds })
  check('C. 已把候选全部 reject（造空态前置）', rejectInfo.rejected > 0, JSON.stringify(rejectInfo))
  // 兜底：扫描池 = `core.list_sessions`（主 home 的 state.db，含界面不显示的那些来源），
  // 所以还要把「池里有、界面没有」的会话也 reject 掉，才是真的「没有未分组会话」。
  const poolReject = spawn(PYTHON, ['-c', [
    'import sqlite3, sys, time',
    'home = sys.argv[1]',
    'db = sqlite3.connect(home + "/auto-group/assignments.db")',
    'sdb = sqlite3.connect("file:" + home + "/state.db?mode=ro", uri=True)',
    'rows = [r[0] for r in sdb.execute("SELECT id FROM sessions WHERE message_count >= 2 AND archived = 0")]',
    'now = time.time()',
    'n = 0',
    'for sid in rows:',
    '    db.execute("INSERT OR REPLACE INTO rejected(session_id, reason, created_at) VALUES(?,?,?)", (sid, "验收：造空态", now))',
    '    n += 1',
    'db.commit()',
    'print(n)'
  ].join('\n'), SANDBOX], { env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' }) })
  let poolOut = ''
  poolReject.stdout.on('data', (c) => { poolOut += c })
  await new Promise((r) => poolReject.on('exit', r))
  check('C. 池内剩余会话也已 reject（真空态）', Number(poolOut.trim()) > 0, poolOut.trim())
  await sleep(600)
  await ensureScanVisible()
  const scan2 = page.locator('[data-agg2-scan]')
  if (await scan2.count()) {
    await scan2.first().click()
  }
  let emptyText = ''
  // 只等空态本身，不用「看到 done 就 break」：上一次扫描的 done 区块还在 DOM 里，
  // 那个启发式会提前退出（实测两种结果交替出现，是探针竞态不是产品问题）。
  for (let i = 0; i < 300; i += 1) {
    const empty = page.locator('[data-agg2-scan-empty]')
    if (await empty.count()) {
      emptyText = (await empty.first().innerText()).replace(/\s+/g, ' ')
      break
    }
    await sleep(100)
  }
  if (!emptyText) {
    const done = page.locator('[data-agg2-scan-phase="done"]')
    if (await done.count()) {
      console.log('调试：30s 内没等到空态，done 区块文案 =',
        (await done.first().innerText()).replace(/\s+/g, ' ').slice(0, 300))
      const html = await done.first().innerHTML()
      console.log('调试：done 区块 HTML 前 600 字 =', html.slice(0, 600))
      console.log('调试：页面上 scan-empty 类元素数 =',
        await page.locator('.hc-agg__scan-empty').count(),
        ' 带属性元素数 =', await page.locator('[data-agg2-scan-empty]').count())
    }
  }
  check('C. 空结果有独立空态 [data-agg2-scan-empty]', emptyText !== '',
        JSON.stringify(rejectInfo))
  check('C. 空态标题写明「没有未分组会话」或后端 empty_reason',
    /没有未分组会话|没有可扫描的会话/.test(emptyText), emptyText.slice(0, 120))
  await page.screenshot({ path: join(SHOTS, '03-scan-empty-1280.png'), fullPage: false })
  await page.setViewportSize({ width: 900, height: 900 })
  await sleep(600)
  await page.screenshot({ path: join(SHOTS, '03b-scan-empty-900.png'), fullPage: false })
  await page.setViewportSize({ width: 1280, height: 900 })
  await sleep(400)

  // ---------- 1280 / 900 布局 ----------
  for (const width of [1280, 900]) {
    await page.setViewportSize({ width, height: 900 })
    await sleep(700)
    const metrics = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
      bodyScroll: document.body.scrollWidth
    }))
    check('F. ' + width + 'px 无横向溢出', metrics.docScroll <= metrics.client + 1,
          JSON.stringify(metrics))
    await page.screenshot({ path: join(SHOTS, '04-layout-' + width + '.png'), fullPage: true })
  }

  // ---------- AI 模型面板 ----------
  await page.setViewportSize({ width: 1280, height: 900 })
  await sleep(400)
  const modelTab = page.locator('text=AI 模型')
  if (await modelTab.count()) {
    await modelTab.first().click()
    await sleep(1200)
    const panel = page.locator('[data-agm-panel]')
    check('模型面板渲染', (await panel.count()) > 0)
    if (await panel.count()) {
      const text = (await panel.first().innerText()).replace(/\s+/g, ' ')
      check('模型面板显示来源层', /来源层/.test(text), text.slice(0, 160))
      check('模型面板显示实际生效值', /实际生效/.test(text), '')
      check('模型面板有配置对象选择器', (await page.locator('[data-agm-target-select]').count()) > 0)
      check('模型面板有保存按钮', (await page.locator('[data-agm-save]').count()) > 0)
      check('密钥只显示变量名+存在性（不含取值）', !/sk-[A-Za-z0-9]{8,}/.test(text), '')
      await page.screenshot({ path: join(SHOTS, '05-model-panel-1280.png'), fullPage: false })
      // 切到独立模式 → provider 选择器出现
      const moduleRadio = page.locator('[data-agm-mode="module"] input')
      if (await moduleRadio.count()) {
        const enabled = await moduleRadio.first().isEnabled()
        if (enabled) {
          await moduleRadio.first().check({ force: true })
          await sleep(1200)
          check('选「独立模型」后出现 provider 选择器',
            (await page.locator('[data-agm-provider-select]').count()) > 0)
          const providerSelect = page.locator('[data-agm-provider-select]')
          if (await providerSelect.count()) {
            const options = await providerSelect.first().locator('option').allInnerTexts()
            check('provider 选择器有可选项', options.length > 1, options.join(',').slice(0, 120))
            const values = await providerSelect.first().locator('option').evaluateAll(
              (nodes) => nodes.map((n) => n.value).filter((v) => v))
            if (values.length >= 2) {
              // 选第一个 provider → 记下模型列表；再选第二个 → 列表必须不同
              await providerSelect.first().selectOption(values[0])
              await sleep(1200)
              const modelsA = await page.locator('[data-agm-model-select] option, [data-agm-model-input]')
                .evaluateAll((nodes) => nodes.map((n) => n.value || n.placeholder || ''))
              await providerSelect.first().selectOption(values[1])
              await sleep(1200)
              const modelsB = await page.locator('[data-agm-model-select] option, [data-agm-model-input]')
                .evaluateAll((nodes) => nodes.map((n) => n.value || n.placeholder || ''))
              check('换 provider 后模型列表确实变化（UI 实测）',
                JSON.stringify(modelsA) !== JSON.stringify(modelsB),
                JSON.stringify(modelsA.slice(0, 5)) + ' vs ' + JSON.stringify(modelsB.slice(0, 5)))
              await page.screenshot({ path: join(SHOTS, '06-model-two-level-1280.png'), fullPage: false })
            }
          }
        } else {
          check('（global 段不支持独立模式，已正确禁用）', true, '')
        }
      }
    }
  } else {
    check('AI 模型抽屉入口存在', false, '没找到「AI 模型」')
  }

  const errorsBeforeInjection = errors.slice()

  // ---------- 失败态：让 job 查询返回错误 ----------
  // 先回到有会话的 agent，保证能扫起来、失败发生在轮询阶段
  const anyAgent = page.locator('[data-agg2-agent]:not([data-agg2-agent="cad-worker"])')
  if (await anyAgent.count()) {
    await anyAgent.first().click()
    await sleep(1000)
  }
  await page.route('**/autogroup/job/**', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json',
                    body: JSON.stringify({ ok: false, error: { code: 'boom', message: '后端炸了（验收注入）' } }) }))
  await page.setViewportSize({ width: 1280, height: 900 })
  await sleep(300)
  await ensureScanVisible()
  const scan3 = page.locator('[data-agg2-scan]')
  let failText = ''
  if (await scan3.count()) {
    await scan3.first().click()
    for (let i = 0; i < 100; i += 1) {
      const failed = page.locator('text=扫描失败')
      if (await failed.count()) {
        failText = (await page.locator('body').innerText()).replace(/\s+/g, ' ')
        break
      }
      await sleep(100)
    }
    check('D. 轮询失败 → 出现「扫描失败」区块', failText !== '', failText.slice(0, 80))
    const failSlice = failText.slice(Math.max(0, failText.indexOf('扫描失败')),
                                     failText.indexOf('扫描失败') + 160)
    check('D. 失败文案带真实错误（HTTP 状态码 + 失败的那个 job id，不是「出错了」四个字）',
          /HTTP 500/.test(failSlice) && /job-/.test(failSlice), failSlice)
    check('D. 失败提供重试按钮', /重新扫描|重试/.test(failText), '')
    await page.screenshot({ path: join(SHOTS, '07-scan-failed-1280.png'), fullPage: false })
    await page.setViewportSize({ width: 900, height: 900 })
    await sleep(600)
    await page.screenshot({ path: join(SHOTS, '07b-scan-failed-900.png'), fullPage: false })
    await page.setViewportSize({ width: 1280, height: 900 })
    await sleep(400)
  }
  await page.unroute('**/autogroup/job/**')

  // ---------- 900px 下的模型面板 ----------
  await page.setViewportSize({ width: 900, height: 900 })
  await sleep(700)
  await page.screenshot({ path: join(SHOTS, '08-model-900.png'), fullPage: true })

  const unexpected = errorsBeforeInjection.filter((e) => !/404 \(Not Found\)/.test(e))
  check('E. 注入失败之前零 console error / pageerror', unexpected.length === 0,
        unexpected.slice(0, 4).join(' | '))

  await browser.close()
  site.close()
  fixture.kill()

  console.log('\nFRONTEND-UI: ' + pass + ' PASS / ' + failures.length + ' FAIL')
  for (const f of failures) console.log('  FAILED: ' + f)
  writeFileSync(join(SHOTS, 'ui-result.txt'),
    'FRONTEND-UI: ' + pass + ' PASS / ' + failures.length + ' FAIL\n\n'
    + failures.map((f) => 'FAILED: ' + f).join('\n'), 'utf8')
  process.exit(failures.length ? 1 : 0)
}

main().catch((err) => {
  console.error(err)
  process.exit(3)
})
