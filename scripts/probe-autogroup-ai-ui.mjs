/**
 * hermes-console · 项目分组「AI 分类」面板 UI 验收（t_3c2817b1 验收项）：
 *   G. AI 建议行必须同时有「建议项目 / 理由 / 置信度」
 *   H. 跑批只产生预览批次（applied 条数不变 = 预览零写入）
 *   I. 全选可写 → 确认写入 → 有 write-summary（回读真值），且 applied 真的增加
 *   J. 1280 / 900 两档截图，无横向溢出
 *
 * 三层都是真的：真产物 dashboard/dist/index.js + 真后端 autogroup_ui_fixture.py（隔离 home + 传输层 AI stub）+ 真浏览器。
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
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_AI_FIXTURE_PORT || 9351)
const SITE_PORT = Number(process.env.HC_AI_SITE_PORT || 9352)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'autogroup-ai-ui')

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) { pass += 1; console.log('  PASS ' + name + (extra ? '  — ' + extra : '')) }
  else { failures.push(name + (extra ? '  — ' + extra : '')); console.log('  FAIL ' + name + (extra ? '  — ' + extra : '')) }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const WORK = mkdtempSync(join(tmpdir(), 'hc-ai-ui-'))
const SANDBOX = join(WORK, 'home')
mkdirSync(SHOTS, { recursive: true })
try { symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction') }
catch (e) { throw new Error('无法建立 junction：' + e.message) }
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

function startSite() {
  const html = `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<link rel="stylesheet" href="/style.css"></head><body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script></body></html>`
  const server = createServer((req, res) => {
    const raw = req.url || '/'
    if (raw.startsWith('/api/')) {
      const up = httpRequest({ host: '127.0.0.1', port: FIXTURE_PORT, path: raw, method: req.method, headers: req.headers },
        (u) => { if (res.writableEnded) return; res.writeHead(u.statusCode || 502, u.headers); u.pipe(res) })
      up.on('error', (e) => {
        if (res.writableEnded) return
        res.writeHead(502, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: { code: 'upstream', message: e.message } }))
      })
      req.pipe(up); return
    }
    const url = (req.url || '/').split('?')[0]
    if (url === '/favicon.ico') { res.writeHead(204); res.end(); return }
    if (url === '/' || url === '/index.html') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(html); return
    }
    const file = join(WORK, url.replace(/^\//, ''))
    if (!existsSync(file)) { res.writeHead(404); res.end('not found'); return }
    res.writeHead(200, { 'content-type': url.endsWith('.css') ? 'text/css' : 'text/javascript' })
    res.end(readFileSync(file))
  })
  return new Promise((r) => server.listen(SITE_PORT, '127.0.0.1', () => r(server)))
}

const HARNESS = `
import plugin, { injectConsoleStyles } from './entry.js'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
const store = {}
const pages = []
const ctx = {
  rest: async (path, opts) => {
    const options = opts || {}
    const method = options.method || 'GET'
    const init = { method, headers: {} }
    if (options.body !== undefined) { init.headers['content-type'] = 'application/json'; init.body = JSON.stringify(options.body) }
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    if (!res.ok) {
      let payload = null
      try { payload = JSON.parse(text) } catch (e) { payload = { message: text } }
      const err = new Error((payload && payload.message) || ('HTTP ' + res.status))
      err.status = res.status; err.payload = payload; throw err
    }
    return text ? JSON.parse(text) : null
  },
  storage: {
    get: (k, f) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : f),
    set: (k, v) => { store[k] = v },
    remove: (k) => { delete store[k] }
  },
  registerMany: (items) => { for (const i of items || []) if (i.area === 'routes') pages.push(i) }
}
plugin.register(ctx)
injectConsoleStyles()
const host = document.getElementById('root')
createRoot(host).render(createElement(pages[0].render))
window.__hcBooted = true
`

function runEsbuild(args) {
  return new Promise((res, rej) => {
    const child = spawn(process.execPath, [join(HOST_MODULES, 'esbuild', 'bin', 'esbuild'), ...args], { cwd: WORK })
    let err = ''
    child.stderr.on('data', (c) => { err += c })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild ' + code + '\n' + err))))
  })
}

async function main() {
  writeFileSync(join(WORK, 'entry.js'), readFileSync(join(DIST, 'index.js'), 'utf8'), 'utf8')
  writeFileSync(join(WORK, 'style.css'), readFileSync(join(DIST, 'style.css'), 'utf8'), 'utf8')
  writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')
  await runEsbuild([join(WORK, 'harness.js'), '--bundle', '--format=esm', '--platform=browser',
    '--target=chrome110', '--jsx=automatic', '--loader:.css=text', '--legal-comments=none', '--charset=utf8',
    '--outfile=' + join(WORK, 'harness.bundle.js')])
  const site = await startSite()

  const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', HC_AI_STUB: '1' })
  const fixture = spawn(PYTHON, [join(PLUGIN, 'scripts', 'autogroup_ui_fixture.py'),
    '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], { env, cwd: PLUGIN, stdio: ['ignore', 'pipe', 'pipe'] })
  let flog = ''
  fixture.stdout.on('data', (c) => { flog += c })
  fixture.stderr.on('data', (c) => { flog += c })
  let ready = false
  for (let i = 0; i < 200; i += 1) {
    try { const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/health'); if (r.ok) { ready = true; break } } catch (e) {}
    await sleep(150)
  }
  if (!ready) { console.log('fixture 没起来：\n' + flog); fixture.kill(); site.close(); process.exit(2) }

  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = []
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
  page.on('requestfailed', (r) => errors.push('reqfail: ' + r.url()))
  page.on('response', (r) => { if (r.status() >= 400) errors.push('http' + r.status() + ': ' + r.url()) })
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await sleep(1500)

  const nav = page.locator('.hc-nav__item:has-text("项目分组")')
  if (await nav.count()) { await nav.first().click(); await sleep(2000) }

  async function appliedCount() {
    return page.evaluate(async () => {
      const r = await fetch('/api/plugins/hermes-console/autogroup/suggestions?status=applied')
      const j = await r.json()
      const d = (j && (j.data || j)) || {}
      const items = d.items || d.suggestions || []
      return Array.isArray(items) ? items.length : -1
    })
  }

  // 打开「AI 分类」抽屉
  async function openAiDrawer() {
    const vis = async () => {
      const p = page.locator('[data-aic-panel]')
      if (!(await p.count())) return false
      return p.first().isVisible()
    }
    if (await vis()) return true
    const cands = [
      '[data-agg2-drawer-head="ai"]',
      '.hc-agg2__drawer-head:has-text("AI 分类")',
      'summary:has-text("AI 分类")'
    ]
    for (const sel of cands) {
      const loc = page.locator(sel)
      if (await loc.count()) {
        try { await loc.first().click({ timeout: 2000 }) } catch (e) { continue }
        for (let i = 0; i < 30; i += 1) { if (await vis()) return true; await sleep(100) }
      }
    }
    return vis()
  }
  const opened = await openAiDrawer()
  check('打开「AI 分类」抽屉，面板渲染 [data-aic-panel]', opened)
  if (!opened) {
    await page.screenshot({ path: join(SHOTS, 'ai-00-no-panel.png'), fullPage: true })
    console.log('调试 body =', (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 500))
  }

  const totalChip = page.locator('[data-aic-total-chip]')
  check('面板显示候选总数（后端 /autogroup/ai/scope）',
    (await totalChip.count()) > 0 && /共\s*\d+\s*条/.test(await totalChip.first().innerText()))

  const appliedBefore = await appliedCount()
  check('读到底层 applied 条数（零写入基线）', appliedBefore >= 0, 'applied=' + appliedBefore)

  // ---- G/H：跑一批（只产生预览批次，不写库）
  const run = page.locator('[data-aic-run]')
  check('跑批按钮存在且可用', (await run.count()) > 0 && !(await run.first().isDisabled()))
  await run.first().click()
  let batch = null
  for (let i = 0; i < 600; i += 1) {
    const b = page.locator('[data-aic-batch]')
    if (await b.count()) { batch = b.first(); break }
    await sleep(100)
  }
  check('跑批后出现预览批次 [data-aic-batch]', !!batch)
  let batchText = ''
  if (batch) {
    await sleep(800)
    batchText = (await batch.innerText()).replace(/\s+/g, ' ')
    await page.screenshot({ path: join(SHOTS, 'ai-01-preview-1280.png'), fullPage: false })
    // 表格行在抽屉内部滚动区，滚到第一行再截一张「项目 + 理由 + 置信度」的证据图
    const firstRow = page.locator('[data-aic-row]').first()
    if (await firstRow.count()) {
      await firstRow.scrollIntoViewIfNeeded()
      await sleep(400)
      await page.screenshot({ path: join(SHOTS, 'ai-01b-rows-1280.png'), fullPage: false })
      await page.setViewportSize({ width: 900, height: 900 })
      await sleep(500)
      await firstRow.scrollIntoViewIfNeeded()
      await sleep(300)
      await page.screenshot({ path: join(SHOTS, 'ai-01c-rows-900.png'), fullPage: false })
      await page.setViewportSize({ width: 1280, height: 900 })
      await sleep(400)
    }
  }
  const rowLoc = page.locator('[data-aic-row]')
  const rowCount = await rowLoc.count()
  check('预览批次有建议行 [data-aic-row]', rowCount > 0, 'rows=' + rowCount)
  const confLoc = page.locator('[data-aic-conf]')
  const confTexts = []
  for (let i = 0; i < Math.min(await confLoc.count(), 6); i += 1) {
    confTexts.push((await confLoc.nth(i).innerText()).trim())
  }
  check('每行都有置信度 [data-aic-conf]（数字或「无置信度」，不许空白）',
    confTexts.length > 0 && confTexts.every((t) => t.length > 0), confTexts.join(' | '))
  check('置信度是百分比或明确「无置信度」', confTexts.some((t) => /%|无置信度/.test(t)), confTexts.join(' | '))
  // 分档文案必须与置信度自洽：**逐行配对**（≥65% 的行不许显示「<0.5 · 无法判定（禁止写入）」）
  const rowPairs = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('[data-aic-row]')).map((el) => {
      const confEl = el.querySelector('[data-aic-conf]')
      const tierEl = el.querySelector('[class*="cell--agent"]')
      return {
        conf: confEl ? confEl.textContent.trim() : '',
        tier: tierEl ? tierEl.textContent.replace(/\s+/g, ' ').trim() : ''
      }
    })
  })
  const badPairs = rowPairs.filter((p) => {
    const m = /^(\d+)%$/.exec(p.conf)
    return m && Number(m[1]) >= 65 && /禁止写入|无法判定/.test(p.tier)
  })
  check('分档文案与置信度自洽（逐行配对：≥65% 的行不显示「禁止写入」）',
    badPairs.length === 0, JSON.stringify(rowPairs).slice(0, 220))
  check('行里有「理由与证据」（后端 basis.reason）',
    /理由|basis|因为|置信/.test(batchText) && rowCount > 0, '')
  const projCells = await page.locator('[data-aic-row] [class*="cell--project"]').allInnerTexts()
  const nonEmptyProj = projCells.map((t) => t.trim()).filter((t) => t.length > 0)
  check('行里有 AI 建议项目（非空项目单元格）', nonEmptyProj.length > 0,
    nonEmptyProj.slice(0, 4).join(' | '))

  const appliedAfterPreview = await appliedCount()
  check('预览零写入：跑批后 applied 条数不变', appliedAfterPreview === appliedBefore,
    appliedBefore + ' → ' + appliedAfterPreview)

  // ---- I：全选可写 → 确认写入
  const pickAll = page.locator('[data-aic-pick-all]')
  let pickedLabel = ''
  if (await pickAll.count()) {
    await pickAll.first().click()
    await sleep(600)
  }
  const confirmBtn = page.locator('[data-aic-confirm]')
  pickedLabel = (await confirmBtn.count()) ? (await confirmBtn.first().innerText()).replace(/\s+/g, ' ') : ''
  check('「全选可写」后确认按钮显示将写入条数', /确认写入\s*[1-9]/.test(pickedLabel), pickedLabel)
  await page.screenshot({ path: join(SHOTS, 'ai-02-picked-1280.png'), fullPage: false })
  if (await confirmBtn.count()) { await confirmBtn.first().click() }
  let summary = ''
  for (let i = 0; i < 600; i += 1) {
    const s = page.locator('[data-aic-write-summary]')
    if (await s.count()) { summary = (await s.first().innerText()).replace(/\s+/g, ' '); break }
    await sleep(100)
  }
  check('确认写入后出现写后回读摘要 [data-aic-write-summary]', summary !== '', summary.slice(0, 140))
  const appliedAfterWrite = await appliedCount()
  check('确认后 applied 条数真的增加（写库是真写，不是本地 state）',
    appliedAfterWrite > appliedBefore, appliedBefore + ' → ' + appliedAfterWrite)
  await page.screenshot({ path: join(SHOTS, 'ai-03-written-1280.png'), fullPage: false })

  // ---- J：两档布局
  for (const width of [1280, 900]) {
    await page.setViewportSize({ width, height: 900 })
    await sleep(700)
    const m = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth
    }))
    check('J. ' + width + 'px 无横向溢出（AI 面板）', m.docScroll <= m.client + 1, JSON.stringify(m))
    await page.screenshot({ path: join(SHOTS, 'ai-04-layout-' + width + '.png'), fullPage: true })
  }

  const bad = errors.filter((e) => !/favicon/.test(e))
  check('E. 全程零 console error / pageerror（favicon 除外）', bad.length === 0, bad.slice(0, 6).join(' | '))

  await browser.close()
  site.close()
  fixture.kill()
  console.log('\nAI-UI: ' + pass + ' PASS / ' + failures.length + ' FAIL')
  for (const f of failures) console.log('  FAILED: ' + f)
  writeFileSync(join(SHOTS, 'ai-result.txt'),
    'AI-UI: ' + pass + ' PASS / ' + failures.length + ' FAIL\n\n' + failures.map((f) => 'FAILED: ' + f).join('\n'), 'utf8')
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(3) })
