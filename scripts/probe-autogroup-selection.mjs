/**
 * hermes-console · 扫描与 AI 共用勾选状态验收（t_8754de3f）
 *
 * 跑法（任意目录）::
 *
 *     node D:/hermes-data/hermes/plugins/hermes-console/scripts/probe-autogroup-selection.mjs
 *
 * 三层都是真的：
 *   1. **真产物** `dashboard/dist/index.js`（= desktop/plugin.js 同字节）原样加载，只套一层 harness；
 *   2. **真后端** `autogroup_ui_fixture.py`（真 plugin_api.py + 沙盒 home + 传输层 AI stub）；
 *   3. **真浏览器** Playwright + Chromium(msedge)。
 *
 * 断言：
 *   S1. 初始未勾选 → 扫描/AI 按钮文案含「分析全部未分组 N 条」，data-*-mode=all；
 *   S2. 勾 3 条 → 两个按钮文案都变「分析已选 3 条」，data-*-mode=selected、count=3；
 *   S3. 不勾选点扫描 → 请求体**无** session_ids；勾 3 条点扫描 → 请求体**有**这 3 个 id；
 *   S4. AI 按钮行为与扫描一致（同文案、勾选时请求体也带 session_ids）；
 *   S5. 勾选时两个面板都显示「改为分析全部未分组」显式入口；
 *   S6. 全程零 console error / 零 pageerror；
 *   S7. 截图。
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
const FIXTURE_PORT = Number(process.env.HC_SEL_FIXTURE_PORT || 9268)
const SITE_PORT = Number(process.env.HC_SEL_SITE_PORT || 9269)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'autogroup-selection')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-sel-ui-'))
const SANDBOX = join(WORK, 'home')
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

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
    if (url === '/favicon.ico') {
      res.writeHead(204); res.end(); return
    }
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
    requests.push({ method, path, status: res.status, body: options.body || null })
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
  page.on('response', (res) => {
    if (res.status() === 404) errors.push('http404: ' + res.url())
    if (res.status() >= 400 && res.status() !== 404) errors.push('http' + res.status() + ': ' + res.url())
  })

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await sleep(1500)

  const bodyText = await page.locator('body').innerText()
  check('页面已渲染', /分组|扫描|AI/.test(bodyText), bodyText.slice(0, 80).replace(/\s+/g, ' '))

  const navItem = page.locator('.hc-nav__item:has-text("项目分组")')
  if (await navItem.count()) {
    await navItem.first().click()
    await sleep(2000)
  }

  // 抽屉是原生 <details>：直接 open = true 最稳
  async function openDrawer(id) {
    await page.evaluate((drawerId) => {
      const node = document.querySelector('[data-agg2-drawer="' + drawerId + '"]')
      if (node) node.open = true
    }, id)
    await sleep(400)
  }

  const scanBtn = page.locator('[data-agg2-scan]')
  const aiBtn = page.locator('[data-aic-run]')

  async function scanLabel() {
    if (!(await scanBtn.count())) return ''
    return (await scanBtn.first().innerText()).replace(/\s+/g, ' ')
  }
  async function aiLabel() {
    if (!(await aiBtn.count())) return ''
    return (await aiBtn.first().innerText()).replace(/\s+/g, ' ')
  }
  async function scanAttr(name) {
    if (!(await scanBtn.count())) return ''
    return (await scanBtn.first().getAttribute(name)) || ''
  }
  async function aiAttr(name) {
    if (!(await aiBtn.count())) return ''
    return (await aiBtn.first().getAttribute(name)) || ''
  }

  // 打开「待确认建议」（扫描按钮）与「AI 分类」（AI 按钮）两个抽屉
  await openDrawer('suggested')
  await openDrawer('ai')
  await sleep(500)

  check('扫描按钮存在', (await scanBtn.count()) > 0, String(await scanBtn.count()))
  check('AI 按钮存在', (await aiBtn.count()) > 0, String(await aiBtn.count()))

  // ---------- S1. 未勾选 ----------
  const label0 = await scanLabel()
  const aiLabel0 = await aiLabel()
  check('S1. 未勾选时扫描按钮文案含「分析全部未分组」', /分析全部未分组/.test(label0), label0)
  check('S1. 未勾选时扫描 data-agg2-scan-mode=all', (await scanAttr('data-agg2-scan-mode')) === 'all',
        await scanAttr('data-agg2-scan-mode'))
  check('S1. 未勾选时扫描 data-agg2-scan-count 是数字',
        /^\d+$/.test(await scanAttr('data-agg2-scan-count')), await scanAttr('data-agg2-scan-count'))
  check('S1. 未勾选时 AI 按钮文案含「分析全部未分组」', /分析全部未分组/.test(aiLabel0), aiLabel0)
  check('S1. 未勾选时 AI data-aic-run-mode=all', (await aiAttr('data-aic-run-mode')) === 'all',
        await aiAttr('data-aic-run-mode'))
  check('S1. 未勾选时 AI data-aic-run-count 是数字',
        /^\d+$/.test(await aiAttr('data-aic-run-count')), await aiAttr('data-aic-run-count'))
  check('S1. 未勾选时无「改为分析全部未分组」按钮',
        (await page.locator('[data-agg2-scan-all]').count()) === 0 &&
        (await page.locator('[data-aic-run-all]').count()) === 0, '')

  await page.screenshot({ path: join(SHOTS, '01-unselected.png'), fullPage: false })
  // 按钮本体截图（文案证据）
  if (await scanBtn.count()) {
    await scanBtn.first().scrollIntoViewIfNeeded()
    await scanBtn.first().screenshot({ path: join(SHOTS, '01a-scan-btn-unselected.png') })
  }
  if (await aiBtn.count()) {
    await aiBtn.first().scrollIntoViewIfNeeded()
    await aiBtn.first().screenshot({ path: join(SHOTS, '01b-ai-btn-unselected.png') })
  }

  // ---------- S2/S4/S5. 勾 3 条 ----------
  // 只勾「本 home」的会话（sess-agg-*）：夹具把 profiles/xiaomo、profiles/work 的会话也
  // 聚合进列表，但 /autogroup/scan 打的是当前 home（root），跨 profile 的 id 会被后端按设计
  // 400 unknown_session_ids。真实用法里作用域=单个 agent，勾选的 id 都在同一 home 下，
  // 所以这里挑 sess-agg-* 才是与线上一致的口径。
  const homePicks = page.locator('[data-agg2-pick^="sess-agg-"]')
  const homePickCount = await homePicks.count()
  const picks = homePickCount >= 3 ? homePicks : page.locator('[data-agg2-pick]')
  const pickCount = await picks.count()
  check('界面有可勾选的会话行', pickCount >= 3,
        pickCount + ' 个 checkbox（本 home ' + homePickCount + ' 个）')

  const pickedIds = []
  if (pickCount >= 3) {
    for (let i = 0; i < 3; i += 1) {
      const cb = picks.nth(i)
      const id = await cb.getAttribute('data-agg2-pick')
      await cb.check({ force: true })
      pickedIds.push(id)
      await sleep(150)
    }
    await sleep(700)

    const label1 = await scanLabel()
    const aiLabel1 = await aiLabel()
    check('S2. 勾 3 条后扫描按钮文案 = 分析已选 3 条', label1 === '分析已选 3 条', label1)
    check('S2. 勾 3 条后扫描 data-agg2-scan-mode=selected',
          (await scanAttr('data-agg2-scan-mode')) === 'selected', await scanAttr('data-agg2-scan-mode'))
    check('S2. 勾 3 条后扫描 data-agg2-scan-count=3',
          (await scanAttr('data-agg2-scan-count')) === '3', await scanAttr('data-agg2-scan-count'))
    check('S4. 勾 3 条后 AI 按钮文案 = 分析已选 3 条', aiLabel1 === '分析已选 3 条', aiLabel1)
    check('S4. 勾 3 条后 AI data-aic-run-mode=selected',
          (await aiAttr('data-aic-run-mode')) === 'selected', await aiAttr('data-aic-run-mode'))
    check('S4. 勾 3 条后 AI data-aic-run-count=3',
          (await aiAttr('data-aic-run-count')) === '3', await aiAttr('data-aic-run-count'))

    const scanAll = page.locator('[data-agg2-scan-all]')
    const aiAll = page.locator('[data-aic-run-all]')
    check('S5. 勾选时扫描有「改为分析全部未分组」入口', (await scanAll.count()) > 0, String(await scanAll.count()))
    if (await scanAll.count()) {
      const t = (await scanAll.first().innerText()).replace(/\s+/g, ' ')
      check('S5. 扫描显式入口文案含「改为分析全部未分组」', /改为分析全部未分组/.test(t), t)
    }
    check('S5. 勾选时 AI 有「改为分析全部未分组」入口', (await aiAll.count()) > 0, String(await aiAll.count()))
    if (await aiAll.count()) {
      const t = (await aiAll.first().innerText()).replace(/\s+/g, ' ')
      check('S5. AI 显式入口文案含「改为分析全部未分组」', /改为分析全部未分组/.test(t), t)
    }

    await page.screenshot({ path: join(SHOTS, '02-selected-3.png'), fullPage: false })
    // 按钮本体截图（文案证据）
    if (await scanBtn.count()) {
      await scanBtn.first().scrollIntoViewIfNeeded()
      await scanBtn.first().screenshot({ path: join(SHOTS, '02a-scan-btn-selected.png') })
    }
    if (await aiBtn.count()) {
      await aiBtn.first().scrollIntoViewIfNeeded()
      await aiBtn.first().screenshot({ path: join(SHOTS, '02b-ai-btn-selected.png') })
    }
  }

  async function waitScanDone(maxMs) {
    const deadline = Date.now() + (maxMs || 30000)
    while (Date.now() < deadline) {
      if (await page.locator('[data-agg2-scan-phase="done"], [data-agg2-scan-phase="failed"]').count()) return true
      await sleep(150)
    }
    return false
  }

  // ---------- S3b. 勾 3 条点扫描 → 请求体带 session_ids ----------
  if (pickedIds.length === 3) {
    await page.evaluate(() => { window.__hcRequests.length = 0 })
    await scanBtn.first().click()
    await sleep(800)
    const reqs = await page.evaluate(() => window.__hcRequests.filter((r) => r.path.startsWith('/autogroup/scan')))
    check('S3b. 勾选后点扫描发出 /autogroup/scan', reqs.length > 0, reqs.length + ' 次')
    if (reqs.length) {
      const body = reqs[reqs.length - 1].body || {}
      const ids = body.session_ids
      check('S3b. 请求体带 session_ids 且等于勾选的 3 个 id',
            Array.isArray(ids) && ids.length === 3 && pickedIds.every((id) => ids.includes(id)),
            'body.session_ids=' + JSON.stringify(ids))
    }
    await waitScanDone(30000)
    await sleep(800)
  }

  // ---------- S3a. 取消勾选点扫描 → 请求体无 session_ids ----------
  if (pickCount >= 3) {
    const picks2 = page.locator('[data-agg2-pick]')
    const n = await picks2.count()
    for (let i = 0; i < n; i += 1) {
      const cb = picks2.nth(i)
      if (await cb.isChecked()) await cb.uncheck({ force: true })
      await sleep(60)
    }
    await sleep(700)
    const labelBack = await scanLabel()
    check('S1b. 取消勾选后扫描文案回到「分析全部未分组」', /分析全部未分组/.test(labelBack), labelBack)

    await page.evaluate(() => { window.__hcRequests.length = 0 })
    if (await scanBtn.count() && await scanBtn.first().isVisible()) {
      await scanBtn.first().click()
      await sleep(800)
      const reqs = await page.evaluate(() => window.__hcRequests.filter((r) => r.path.startsWith('/autogroup/scan')))
      check('S3a. 不勾选点扫描发出 /autogroup/scan', reqs.length > 0, reqs.length + ' 次')
      if (reqs.length) {
        const body = reqs[reqs.length - 1].body || {}
        check('S3a. 不勾选时请求体**无** session_ids',
              body.session_ids === undefined || body.session_ids === null,
              'body=' + JSON.stringify(body))
      }
      await waitScanDone(30000)
      await sleep(800)
    }
  }

  // ---------- S4b. 勾 3 条点 AI → 请求体带 session_ids ----------
  if (pickCount >= 3) {
    const picks3 = page.locator('[data-agg2-pick^="sess-agg-"]')
    const n3 = Math.min(3, await picks3.count())
    const ids3 = []
    for (let i = 0; i < n3; i += 1) {
      const cb = picks3.nth(i)
      const id = await cb.getAttribute('data-agg2-pick')
      if (!(await cb.isChecked())) await cb.check({ force: true })
      ids3.push(id)
      await sleep(150)
    }
    await sleep(700)

    if (await aiBtn.count() && await aiBtn.first().isVisible()) {
      await page.evaluate(() => { window.__hcRequests.length = 0 })
      await aiBtn.first().click()
      await sleep(1200)
      const reqs = await page.evaluate(() => window.__hcRequests.filter((r) => r.path.startsWith('/autogroup/scan')))
      check('S4b. 勾选后点 AI 发出 /autogroup/scan', reqs.length > 0, reqs.length + ' 次')
      if (reqs.length) {
        const body = reqs[reqs.length - 1].body || {}
        check('S4b. AI 请求体 ai=true 且带 session_ids',
              body.ai === true && Array.isArray(body.session_ids) && body.session_ids.length === ids3.length,
              'body=' + JSON.stringify(body))
      }
    }
  }

  // ---------- S3c. 扫描产出（result.items）口径：只含**本次扫描产出**，不掺全库建议 ----------
  // t_9cd859ba 回归点：`items` 曾经是「全库 suggested 回读」（`load_suggestions(home,"suggested")`），
  // 库里历史遗留、属于别的 agent 的 suggested 行会混进来 —— 于是「扫描 agent=xiaomo，结果不含
  // default 会话」这类断言假红（功能本身是对的）。这里直接打真后端，按 job 结果逐条核对：
  //   1. items 里**不允许**出现任何不属于本 agent 的 id（含别的 agent 的历史行）；
  //   2. items 必须 ⊆ 本次 processed_ids（是「本次产出」，不是全库快照）；
  //   3. 同时证明「库里确实还有别人的 suggested 行」—— 否则第 1 条是被空库蒙过去的。
  // 放在所有 UI 交互之后：它直接打夹具后端并会写建议表，不掺进上面的界面断言。
  {
    const scanResp = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/scan', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ agent: 'xiaomo', scope: 'ungrouped' })
    })
    const scanBody = await scanResp.json()
    const jobId = ((scanBody.data || {}).job_id) || scanBody.job_id || ''
    check('S3c. 直连后端 agent=xiaomo 扫描拿到 job_id', !!jobId, 'status=' + scanResp.status)
    let job = null
    for (let i = 0; i < 200 && jobId; i += 1) {
      await sleep(150)
      const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/job/' + jobId)
      const jb = await r.json()
      const state = jb.state || (jb.data || {}).state
      if (state === 'done' || state === 'failed') { job = jb; break }
    }
    const result = (job && job.data && job.data.result) || {}
    const itemIds = (result.items || []).map((r) => r.session_id)
    const processed = result.processed_ids || []
    check('S3c. job 跑完且 items 非空（不是空库蒙过去的）',
          !!job && itemIds.length > 0, 'items=' + JSON.stringify(itemIds) + ' processed=' + JSON.stringify(processed))
    // 本夹具里 root home 的 default 会话是 sess-agg-07..10（见 autogroup_ui_fixture.SESSIONS）；
    // xiaomo 的是 sess-agg-02/04/06；sess-agg-01 属 kanban-worker、sess-agg-03 属 work、
    // sess-agg-05 属 media-worker —— 后三个正是改前会混进 items 的历史行。
    const defaultIds = ['sess-agg-07', 'sess-agg-08', 'sess-agg-09', 'sess-agg-10']
    const xiaomoIds = ['sess-agg-02', 'sess-agg-04', 'sess-agg-06']
    check('S3c. 扫描 agent=xiaomo 时 items **不含** default 的任何 session_id',
          !itemIds.some((id) => defaultIds.includes(id)), 'items=' + JSON.stringify(itemIds))
    check('S3c. items 里没有任何不属于 xiaomo 的会话（含 kanban-worker/work/media-worker 的历史行）',
          itemIds.every((id) => xiaomoIds.includes(id)),
          'items=' + JSON.stringify(itemIds) + ' 越界=' +
          JSON.stringify(itemIds.filter((id) => !xiaomoIds.includes(id))))
    check('S3c. items 是「本次扫描产出」：逐条都在本次 processed_ids 里',
          itemIds.every((id) => processed.includes(id)),
          'items=' + JSON.stringify(itemIds) + ' processed=' + JSON.stringify(processed))
    const libResp = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API +
                                '/autogroup/suggestions?status=suggested&limit=200')
    const lib = await libResp.json()
    const libIds = (((lib.data || {}).items) || []).map((i) => i.session_id)
    const foreignLib = ['sess-agg-01', 'sess-agg-03', 'sess-agg-05']
    check('S3c. 反证：库里确有别的 agent 的历史 suggested 行，但它们不在 items 里',
          libIds.some((id) => foreignLib.includes(id)) &&
          !itemIds.some((id) => foreignLib.includes(id)),
          'library=' + JSON.stringify(libIds) + ' items=' + JSON.stringify(itemIds))
  }

  // 负控开关（**只用于验收自身**，默认关闭）：注入一个非 /modules 的 404，
  // 证明「豁免只针对已知夹具路径」——这条注入必须让下面的 S6 判 FAIL。
  if (process.env.HC_SEL_INJECT_404) {
    await page.evaluate(
      (p) => fetch(p).then(() => {}).catch(() => {}),
      'http://127.0.0.1:' + SITE_PORT + process.env.HC_SEL_INJECT_404,
    )
    await sleep(400)
  }
  // 第二个负控（同样默认关闭）：直接把一条**合成**的 404 塞进 errors，走与真响应事件
  // 完全相同的分类代码，用来分别验证「已存在 profile 的 /modules 404 ⇒ FAIL」与
  // 「沙盒里不存在 profile 的 /modules 404 ⇒ 只 skip 不计 FAIL」。
  if (process.env.HC_SEL_FAKE_404) {
    errors.push('http404: ' + process.env.HC_SEL_FAKE_404)
    console.log('  [negative-control] 注入合成 404：' + process.env.HC_SEL_FAKE_404)
  }

  // ---------- S5b. /modules?agent=<profile> 逐 profile 状态码 ----------
  // 前端 shell 启动时按 ``/agents`` 的 ``current`` 拉 ``GET /modules?agent=<当前作用域>``。
  // 沙盒里没有这个 profile ⇒ 后端按设计返回 404 ``agent_not_found``（前端降级到
  // FALLBACK_SECTIONS 是正确路径）。夹具补上 code-worker 之后这里必须全是 200 ——
  // 这是「那条 404 不再出现」的直接证据。
  const agentsBody = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/agents')
    .then((r) => r.json())
  const agentIds = ((agentsBody || {}).agents || []).map((a) => a.id)
  const moduleStatus = {}
  for (const id of agentIds.concat(['code-worker'])) {
    const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/modules?agent=' + encodeURIComponent(id))
    moduleStatus[id] = r.status
  }
  console.log('  [evidence] /modules 状态码 = ' + JSON.stringify(moduleStatus) +
              ' / agents = ' + JSON.stringify(agentIds))
  check('S5b. /modules?agent=<各 profile> 全部 200（含 code-worker，无 agent_not_found）',
        Object.keys(moduleStatus).every((k) => moduleStatus[k] === 200), JSON.stringify(moduleStatus))

  // ---------- S6. 零 console error / 404 口径 ----------
  // 只有「``/modules?agent=<沙盒里不存在的 profile>``」这一条 404 是**已知夹具限制**
  // （后端按设计回 ``agent_not_found``，前端正确降级），写成 skip 不计 FAIL。
  // 任何非 /modules 的 404、任何 4xx/5xx、任何 console error / pageerror 仍必须 FAIL。
  // 判定不许宽化：``/404 \(Not Found\)/`` 这种纯文本匹配会把**任何** 404 一起豁免掉，
  // 所以这里逐条按 URL 分类，且 /modules 的 404 还要核对 agent 真的不在 /agents 里
  // （存在的 agent 拿不到模块 = 产品错，属于 FAIL，不是夹具限制）。
  const nonModule404 = errors.filter((e) => e.startsWith('http404: ') && !/\/modules(\?|$)/.test(e))
  const module404 = errors.filter((e) => /^http404: .*\/modules(\?|$)/.test(e))
  const knownFixture404 = module404.filter((e) => {
    const m = /[?&]agent=([^&\s]+)/.exec(e)
    const agent = m ? decodeURIComponent(m[1]) : ''
    return !!agent && agentIds.indexOf(agent) === -1
  })
  const productModule404 = module404.filter((e) => knownFixture404.indexOf(e) === -1)
  const otherHttpErr = errors.filter((e) => /^http[45]\d\d: /.test(e) && !e.startsWith('http404: '))
  const consoleErrors = errors.filter((e) => e.startsWith('console: '))
  const pageErrors = errors.filter((e) => e.startsWith('pageerror: '))
  // 浏览器给失败 fetch 打的「Failed to load resource … 404 (Not Found)」控制台噪声：
  // 只在**确实有已知夹具 404**、且噪声条数 ≤ 已知 404 条数（一一对应）时才豁免；
  // 有噪声但对不上号 = FAIL。这条噪声不带 URL，所以按条数配平，不按 URL 认。
  const consoleNoise = consoleErrors.filter((e) => /404 \(Not Found\)/.test(e))
  const consoleReal = consoleErrors.filter((e) => consoleNoise.indexOf(e) === -1)
  console.log('  [evidence] 网络/控制台异常 = ' + JSON.stringify(errors))
  check('S6. 没有非 /modules 的 404（任何产品路径的 404 都算失败）', nonModule404.length === 0,
        nonModule404.length ? nonModule404[0] : '无')
  check('S6. /modules 的 404 只能是「沙盒里不存在的 profile」的降级（存在却 404 = 产品错）',
        productModule404.length === 0, productModule404.length ? productModule404[0] : '无')
  check('S6. 没有 4xx/5xx 响应（404 走上面的分档）', otherHttpErr.length === 0,
        otherHttpErr.length ? otherHttpErr[0] : '无')
  check('S6. 零 console error（仅豁免与已知夹具 404 一一对应的加载噪声）',
        consoleReal.length === 0 && consoleNoise.length <= knownFixture404.length,
        consoleReal[0] || (consoleNoise.length + ' 条噪声 / 已知夹具 404 ' + knownFixture404.length + ' 条'))
  check('S6. 零 pageerror', pageErrors.length === 0, pageErrors[0] || 'OK')
  if (knownFixture404.length) {
    console.log('  SKIP S6. 已知夹具 404（沙盒缺 profile，不计 FAIL）：' + knownFixture404.join(' | ') +
                ' —— 本卡已补 code-worker，正常应为 0 条')
  }

  await page.screenshot({ path: join(SHOTS, '03-final.png'), fullPage: false })

  // 夹具日志原文落盘（验收要求「附 fixture.log 原文」）：含 home= 启动自检行
  // 与后端可能打的任何 profile 解析报错（如旧版的 "Profile 'code-worker' does not exist"）。
  writeFileSync(join(SHOTS, 'fixture.log'), fixtureLog, 'utf8')

  await browser.close()
  fixture.kill()
  site.close()

  console.log('\n======== 汇总 ========')
  console.log('PASS: ' + pass + ' / FAIL: ' + failures.length + ' / TOTAL: ' + (pass + failures.length))
  if (failures.length) {
    console.log('\n失败项：')
    for (const f of failures) console.log('  ✗ ' + f)
    process.exit(1)
  }
  console.log('SELECTION-PROBE-OK')
}

main().catch((e) => {
  console.error('FATAL: ' + e.message + '\n' + e.stack)
  process.exit(1)
})
