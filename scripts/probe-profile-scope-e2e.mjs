/**
 * hermes-console · t_bba9216d 端到端验收：**按钮 N == 符合条件 N == 真正处理 N**
 *
 * 场景（老板实际遇到的布局）：work/xiaomo 的会话**只存在** `profiles/<p>/state.db`，
 * 根 `state.db` 一条都没有。旧版引擎只读根库 ⇒ 按钮说 13 条、点下去 0 条。
 *
 * 三层都是真的：真产物 `dashboard/dist/index.js`、真后端 `dashboard/plugin_api.py`
 * （隔离沙盒 HOME）、真浏览器 Playwright + Chromium/Edge。
 *
 * 跑法：node scripts/probe-profile-scope-e2e.mjs [--shots <dir>]
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = process.env.HC_PLUGIN || resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_PSCOPE_FIXTURE_PORT || 9317)
const SITE_PORT = Number(process.env.HC_PSCOPE_SITE_PORT || 9318)
const API = '/api/plugins/hermes-console'
const PROD_DB = 'D:/hermes-data/hermes/auto-group/assignments.db'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'profile-scope', 'e2e')
mkdirSync(SHOTS, { recursive: true })

let pass = 0
const failures = []
const evidence = {}
function check(name, ok, extra) {
  ;(ok ? pass++ : failures.push(name + (extra ? '  — ' + extra : '')))
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + ' ' + name + (extra ? '  — ' + extra : ''))
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const sha256 = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')

const WORK = mkdtempSync(join(tmpdir(), 'hc-pscope-'))
const SANDBOX = join(WORK, 'home')
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

function startSite() {
  const html = `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>hermes-console profile-scope e2e</title><link rel="stylesheet" href="/style.css"></head>
<body><div id="root"></div><script type="module" src="/harness.bundle.js"></script></body></html>`
  const server = createServer((req, res) => {
    const raw = req.url || '/'
    if (raw.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: raw, method: req.method, headers: req.headers },
        (up) => {
          if (res.writableEnded || res.destroyed) return
          res.writeHead(up.statusCode || 502, up.headers)
          up.pipe(res)
        })
      upstream.on('error', (e) => {
        if (res.writableEnded || res.destroyed) return
        res.writeHead(502, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: { code: 'upstream', message: e.message } }))
      })
      req.pipe(upstream)
      return
    }
    const url = (req.url || '/').split('?')[0]
    if (url === '/favicon.ico') { res.writeHead(204); res.end(); return }
    if (url === '/' || url === '/index.html') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      res.end(html)
      return
    }
    const map = { '/style.css': join(WORK, 'style.css'), '/harness.bundle.js': join(WORK, 'harness.bundle.js') }
    const file = map[url] || join(WORK, url.replace(/^\//, ''))
    if (!existsSync(file)) { res.writeHead(404); res.end('not found'); return }
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
    const entry = { method, path, status: null, body: options.body || null, resp: null, t: Date.now() }
    requests.push(entry)
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    entry.status = res.status
    try { entry.resp = text ? JSON.parse(text) : null } catch (e) { entry.resp = text.slice(0, 2000) }
    if (!res.ok) {
      let payload = entry.resp
      const err = new Error((payload && payload.message) || (payload && payload.error && payload.error.message) || ('HTTP ' + res.status))
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
    const child = spawn(process.execPath, [join(HOST_MODULES, 'esbuild', 'bin', 'esbuild'), ...args],
      { cwd: WORK, stdio: ['ignore', 'pipe', 'pipe'] })
    let err = ''
    child.stderr.on('data', (c) => { err += c })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild ' + code + '\n' + err))))
  })
}

async function main() {
  const distShaBefore = sha256(join(DIST, 'index.js'))
  const prodShaBefore = existsSync(PROD_DB) ? sha256(PROD_DB) : '(不存在)'

  writeFileSync(join(WORK, 'entry.js'), readFileSync(join(DIST, 'index.js'), 'utf8'), 'utf8')
  writeFileSync(join(WORK, 'style.css'), readFileSync(join(DIST, 'style.css'), 'utf8'), 'utf8')
  writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')
  await runEsbuild([join(WORK, 'harness.js'), '--bundle', '--format=esm', '--platform=browser',
    '--target=chrome110', '--jsx=automatic', '--loader:.css=text', '--legal-comments=none',
    '--charset=utf8', '--outfile=' + join(WORK, 'harness.bundle.js')])

  const site = await startSite()
  const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' })
  const fixture = spawn(PYTHON, [join(HERE, '_probe_profile_fixture.py'),
    '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX],
    { env, cwd: HERE, stdio: ['ignore', 'pipe', 'pipe'] })
  let fixtureLog = ''
  fixture.stdout.on('data', (c) => { fixtureLog += c })
  fixture.stderr.on('data', (c) => { fixtureLog += c })

  let ready = false
  for (let i = 0; i < 400; i += 1) {
    try { const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/e2e/health'); if (r.ok) { ready = true; break } } catch (e) {}
    await sleep(200)
  }
  if (!ready) {
    console.log('fixture 没起来：\n' + fixtureLog)
    fixture.kill(); site.close(); process.exit(2)
  }

  const api = async (path) => (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + path)).json()
  const dbFacts = (await api('/e2e/dbs')).data
  const idsOf = (name) => new Set(((dbFacts[name] || {}).rows || []).map((r) => r.id))
  const workIds = idsOf('work')
  const xmIds = idsOf('xiaomo')
  const rootIds = idsOf('default')
  console.log('[probe] db ids: work=' + JSON.stringify([...workIds]) +
    ' xiaomo=' + JSON.stringify([...xmIds]) + ' root=' + JSON.stringify([...rootIds]))
  evidence.db_ids = { work: [...workIds], xiaomo: [...xmIds], default: [...rootIds] }

  /* ---------------- 0) 后端口径 ---------------- */
  console.log('\n== 0) 后端 candidate_scope（同一个引擎函数，含 id 列表） ==')
  const scopes = (await api('/e2e/scope')).data
  for (const agent of ['work', 'xiaomo', 'default', '__all__']) {
    const sc = scopes[agent]
    console.log('  ' + agent + ' total_available=' + sc.total_available + ' pool=' + sc.pool +
      ' applied_in_pool=' + sc.applied_in_pool + ' excluded_applied=' + sc.excluded_applied +
      ' excluded_child=' + sc.excluded_child)
  }
  const uiScopes = {}
  for (const agent of ['work', 'xiaomo', 'default']) {
    uiScopes[agent] = (await api('/autogroup/ai/scope?agent=' + agent)).data
  }
  evidence.scopes = scopes
  evidence.ui_scopes = uiScopes
  check('0. UI 路由 total_available 与引擎一致（work/xiaomo/default）',
    ['work', 'xiaomo', 'default'].every((a) => uiScopes[a].total_available === scopes[a].total_available),
    ['work', 'xiaomo', 'default'].map((a) => a + ':' + uiScopes[a].total_available + '/' +
      scopes[a].total_available).join(' '))
  check('0. work 候选 = 3 且全部来自 profiles/work/state.db',
    JSON.stringify(scopes.work.candidate_session_ids.slice().sort()) === JSON.stringify(['work-only-01', 'work-only-02', 'work-only-03']) &&
    scopes.work.candidate_session_ids.every((i) => workIds.has(i)),
    JSON.stringify(scopes.work.candidate_session_ids))
  check('0b. xiaomo 候选 = 1（xm-only-02）且来自 profiles/xiaomo/state.db',
    JSON.stringify(scopes.xiaomo.candidate_session_ids) === JSON.stringify(['xm-only-02']),
    JSON.stringify(scopes.xiaomo.candidate_session_ids))
  check('0c. default 候选 = 2 且来自根库',
    JSON.stringify(scopes.default.candidate_session_ids.slice().sort()) === JSON.stringify(['root-01', 'root-02']),
    JSON.stringify(scopes.default.candidate_session_ids))
  check('0d. work 候选不含任何 default / xiaomo id（agent 隔离）',
    !scopes.work.candidate_session_ids.some((i) => rootIds.has(i) || xmIds.has(i)))
  check('0e. 空态数字同源：applied_in_pool <= pool（三个 agent 各自）',
    ['work', 'xiaomo', 'default'].every((a) => Number(scopes[a].applied_in_pool) <= Number(scopes[a].pool)),
    ['work', 'xiaomo', 'default'].map((a) => a + '=' + scopes[a].pool + '/' + scopes[a].applied_in_pool).join(' '))
  check('0f. work 的 excluded_child=1（归档未置顶那条进 archived 桶，kanban 进看板桶）',
    Number(scopes.work.excluded_child) === 0 && Number(scopes.work.excluded_kanban) === 1 &&
    Number(scopes.work.excluded_archived) === 1 && Number(scopes.work.excluded_hidden) === 1 &&
    Number(scopes.work.excluded_no_message) === 1,
    JSON.stringify({ child: scopes.work.excluded_child, kanban: scopes.work.excluded_kanban,
      archived: scopes.work.excluded_archived, hidden: scopes.work.excluded_hidden,
      no_message: scopes.work.excluded_no_message }))

  /* ---------------- 1) 直接打后端：跑批 == 候选 ---------------- */
  console.log('\n== 1) POST /autogroup/scan {agent=work} ==')
  const started = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/scan', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ agent: 'work', limit: 0, scope: 'ungrouped' })
  })).json()
  const jobId = (started.data || {}).job_id || started.job_id
  let job = {}
  for (let i = 0; i < 600; i += 1) {
    job = (await api('/autogroup/job/' + jobId)).data || {}
    if (job.state === 'done' || job.state === 'failed') break
    await sleep(200)
  }
  const res = job.result || {}
  evidence.scan_work = res
  console.log('  scanned=' + res.scanned + ' available=' + res.available + ' items=' +
    (res.items || []).length + ' pool=' + res.pool + ' already_grouped=' + res.already_grouped +
    ' already_grouped_all=' + res.already_grouped_all)
  check('1. job done 且 scanned == available == 3', job.state === 'done' && res.scanned === 3 && res.available === 3,
    job.state + ' / ' + res.scanned + ' / ' + res.available)
  check('1b. result.items 非空，且每条都来自 profiles/work/state.db（验收 3）',
    (res.items || []).length > 0 && (res.items || []).every((r) => workIds.has(r.session_id)),
    JSON.stringify((res.items || []).map((r) => r.session_id)))
  check('1b2. processed_ids == 3 条候选（真正被处理的是这 3 条）',
    (res.processed_ids || []).length === 3 && (res.processed_ids || []).every((i) => workIds.has(i)),
    JSON.stringify(res.processed_ids))
  check('1c. 每条 items.session_id 都能在 profiles/work/state.db 查到',
    (res.items || []).length > 0 && (res.items || []).every((r) => workIds.has(r.session_id)))
  check('1d. items 里没有 default / xiaomo 的 id',
    !(res.items || []).some((r) => rootIds.has(r.session_id) || xmIds.has(r.session_id)))
  check('1e. 本池已分组 <= pool，且全库数单独给',
    Number(res.already_grouped) <= Number(res.pool) && Number(res.already_grouped_all) >= 1,
    'pool=' + res.pool + ' pool_applied=' + res.already_grouped + ' all_applied=' + res.already_grouped_all)
  check('1f. 每条 items 的 title 非空（_session_meta 也扫 profile 库）',
    (res.items || []).every((r) => !!r.title), JSON.stringify((res.items || []).map((r) => r.title)))

  /* ---------------- 2) 真浏览器：按钮 N == 符合条件 N ---------------- */
  console.log('\n== 2) 浏览器（真 dist + 真后端 + 沙盒） ==')
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = []
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  page.on('response', (r) => { if (r.status() >= 400) errors.push('http' + r.status() + ': ' + r.url()) })

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 30000 })
  await sleep(1500)
  const nav = page.locator('.hc-nav__item:has-text("项目分组")')
  if (await nav.count()) { await nav.first().click(); await sleep(2000) }

  const switchAgent = async (name) => {
    const chip = page.locator('[data-agg2-agent="' + name + '"]')
    if (!(await chip.count())) return false
    await chip.first().click()
    for (let i = 0; i < 80; i += 1) {
      const on = await page.evaluate((n) => {
        const node = document.querySelector('[data-agg2-agent="' + n + '"]')
        return !!node && node.className.indexOf('--on') >= 0
      }, name)
      if (on) { await sleep(1200); return true }
      await sleep(100)
    }
    return false
  }
  const scanBtn = () => page.locator('[data-agg2-scan]').first()
  const scanText = async () => (await scanBtn().innerText()).replace(/\s+/g, ' ')
  const scanCount = async () => Number(await scanBtn().getAttribute('data-agg2-scan-count'))
  const scanSrc = async () => scanBtn().getAttribute('data-agg2-scan-count-source')
  const fiveBlock = async () => {
    const el = page.locator('[data-agg2-scan-five]')
    if (!(await el.count())) return null
    return el.first().evaluate((n) => ({
      agent: n.getAttribute('data-agg2-scan-agent'),
      available: Number(n.getAttribute('data-agg2-scan-available')),
      scanned: Number(n.getAttribute('data-agg2-scan-scanned')),
      text: n.innerText.replace(/\s+/g, ' ')
    }))
  }
  const runScanAndWait = async () => {
    await page.evaluate(() => { window.__hcRequests.length = 0 })
    await scanBtn().click()
    for (let i = 0; i < 400; i += 1) {
      const done = await page.locator('[data-agg2-scan-phase="done"], [data-agg2-scan-phase="failed"]').count()
      if (done) { await sleep(700); return true }
      await sleep(200)
    }
    return false
  }
  const lastJob = async () => {
    const all = await page.evaluate(() => window.__hcRequests)
    const jobs = all.filter((r) => /\/autogroup\/job\//.test(r.path) && r.resp && r.resp.data)
    for (let i = jobs.length - 1; i >= 0; i -= 1) {
      const d = jobs[i].resp.data
      if (d && d.state === 'done' && d.result) return d.result
    }
    return null
  }

  // ---- work ----
  check('2.0 切到 work 芯片生效', await switchAgent('work'))
  // 扫描按钮在「待确认建议」抽屉里（<details>，默认收起）：先展开再读/点。
  await page.evaluate(() => {
    const d = document.querySelector('[data-agg2-drawer="suggested"]')
    if (d) d.open = true
  })
  await sleep(800)
  // 等后端口径真正到达（按钮 N 的后端同源标记变 backend），最多 20s
  const scopeReqs = []
  for (let i = 0; i < 100; i += 1) {
    const src = await scanSrc()
    if (src === 'backend') break
    await sleep(200)
  }
  const reqLog = await page.evaluate(() => window.__hcRequests.map((r) => r.method + ' ' + r.path + ' -> ' + r.status))
  const scopeOnly = reqLog.filter((l) => /\/autogroup\/ai\/scope/.test(l))
  console.log('  [debug] scope 请求：' + JSON.stringify(scopeOnly))
  console.log('  [debug] 请求总览（前 12）：' + JSON.stringify(reqLog.slice(0, 12)))
  evidence.request_log = reqLog
  const workLabel = await scanText()
  const workN = await scanCount()
  const workSrc = await scanSrc()
  evidence.button = { work: { label: workLabel, count: workN, source: workSrc } }
  check('2.1 work 按钮文案 = 分析全部未分组 3 条', /分析全部未分组 3 条/.test(workLabel), workLabel)
  check('2.2 按钮 N 标为后端同源口径（data-agg2-scan-count-source=backend）', workSrc === 'backend', workSrc)
  await page.screenshot({ path: join(SHOTS, '01-work-button.png') })

  check('2.3 work 扫描跑完', await runScanAndWait())
  const workFive = await fiveBlock()
  const workJob = await lastJob()
  evidence.browser_work = { five: workFive, job: workJob }
  console.log('  five = ' + JSON.stringify(workFive))
  check('2.4 work 「符合条件 N 条」== 按钮 N', !!workFive && workFive.available === workN,
    'button=' + workN + ' available=' + (workFive && workFive.available))
  check('2.5 work 本次扫描条数 == 按钮 N', !!workFive && workFive.scanned === workN,
    workFive && workFive.scanned)
  check('2.6 work 面板标注 agent=work', !!workFive && workFive.agent === 'work', workFive && workFive.agent)
  check('2.7 work 的 available_ids == 3 条且都在 profiles/work/state.db',
    !!workJob && (workJob.available_ids || []).length === 3 &&
    (workJob.available_ids || []).every((i) => workIds.has(i)),
    JSON.stringify(workJob && workJob.available_ids))
  check('2.8 work 的 items 非空且 session_id 都来自 work 库',
    !!workJob && (workJob.items || []).length > 0 &&
    (workJob.items || []).every((r) => workIds.has(r.session_id)),
    JSON.stringify(((workJob && workJob.items) || []).map((r) => r.session_id)))
  await page.screenshot({ path: join(SHOTS, '02-work-result.png') })

  // AI 面板按钮同 N
  await page.evaluate(() => { const d = document.querySelector('[data-agg2-drawer="ai"]'); if (d) d.open = true })
  await sleep(1200)
  const aiLabel = (await page.locator('[data-aic-run]').first().innerText()).replace(/\s+/g, ' ')
  const aiCount = await page.locator('[data-aic-run]').first().getAttribute('data-aic-run-count')
  const aiTotal = await page.locator('[data-aic-total]').first().getAttribute('data-aic-total')
  evidence.ai_button = { label: aiLabel, count: aiCount, total: aiTotal }
  check('2.9 AI 面板按钮同 N（分析全部未分组 3 条）且 data-aic-total=3',
    /分析全部未分组 3 条/.test(aiLabel) && aiCount === '3' && aiTotal === '3',
    aiLabel + ' / ' + aiCount + ' / ' + aiTotal)

  // ---- default（根库）----
  check('2.10 切回 default 芯片生效', await switchAgent('default'))
  await page.evaluate(() => {
    const d = document.querySelector('[data-agg2-drawer="suggested"]')
    if (d) d.open = true
  })
  await sleep(800)
  const dLabel = await scanText()
  const dN = await scanCount()
  evidence.button.default = { label: dLabel, count: dN }
  check('2.11 default 按钮 N=2（根库 2 条未分组）', dN === 2 && /分析全部未分组 2 条/.test(dLabel),
    dLabel + ' / ' + dN)
  check('2.12 default 扫描跑完', await runScanAndWait())
  const dFive = await fiveBlock()
  evidence.browser_default = { five: dFive }
  check('2.13 default 「符合条件」== 按钮 N', !!dFive && dFive.available === dN,
    'button=' + dN + ' available=' + (dFive && dFive.available))
  await page.screenshot({ path: join(SHOTS, '03-default-result.png') })

  // 空态：把 work 的池全部标成 applied 后再扫 → 空态文案必须同源
  console.log('\n== 3) 空态文案同源（work 池全部 applied） ==')
  /**
   * 这里**刻意不用** `/autogroup/apply`：规则命中的置信度是 0.59，真实 apply 会按阈值 0.65
   * 正确拒掉（`all_below_threshold`）。那是对的行为，不该为造场景去改阈值 —— 所以用夹具的
   * `/e2e/mark-applied` 直接写**沙盒**库。真实写入路径由覆盖度 E2E（54 项 S4）负责。
   *
   * 标的范围是**该 agent 的全部候选**（不是「有建议的那些行」）：只有把 work 池标满，
   * 重扫才会真的 0 候选、才出空态。早先只标了有 suggested 的行，池里还剩 1 条候选，
   * 于是 3.3「出现空态」根本不可能通过。
   */
  const before = (await api('/e2e/assignments')).data.assignments
  const workScope = (await api('/e2e/scope')).data.work
  const poolIds = (workScope.candidate_session_ids || []).filter(Boolean)
  evidence.pool_ids = { work: poolIds }
  console.log('  work 池候选 = ' + JSON.stringify(poolIds))
  let appliedNow = false
  if (poolIds.length) {
    const applied = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/e2e/mark-applied', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session_ids: poolIds })
    })).json()
    appliedNow = applied.ok === true
    evidence.apply = applied
    console.log('  mark-applied = ' + JSON.stringify(applied).slice(0, 300))
  }
  check('3.0 把 work 池全部候选标成 applied（夹具直写沙盒库，绕过置信度阈值）', appliedNow,
    JSON.stringify(evidence.apply).slice(0, 300))
  const after = (await api('/e2e/assignments')).data.assignments
  check('3.1 写入后回读：applied 行数增加了', after.filter((r) => r.status === 'applied').length >
    before.filter((r) => r.status === 'applied').length,
    before.filter((r) => r.status === 'applied').length + ' -> ' +
    after.filter((r) => r.status === 'applied').length)

  check('3.2 切到 work 并重扫（此时应 0 候选）', await switchAgent('work') && await runScanAndWait())
  const emptyEl = page.locator('[data-agg2-scan-empty]')
  const emptyDetail = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-scan-empty-detail]')
    return n ? n.innerText.replace(/\s+/g, ' ') : ''
  })
  const emptyAttrs = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-scan-empty]')
    return n ? { pool: n.getAttribute('data-agg2-scan-empty-pool'),
                 applied: n.getAttribute('data-agg2-scan-empty-applied'),
                 appliedAll: n.getAttribute('data-agg2-scan-empty-applied-all') } : null
  })
  const emptyJob = await lastJob()
  evidence.empty_state = { detail: emptyDetail, attrs: emptyAttrs, job: emptyJob }
  console.log('  empty detail = ' + emptyDetail)
  console.log('  empty attrs  = ' + JSON.stringify(emptyAttrs))
  check('3.3 出现空态', (await emptyEl.count()) > 0)
  // 空态第一行 = 后端 empty_reason 原话；第二行只补项目数与结论（不重复拼接数字）。
  const emptyTitle = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-scan-empty-title]')
    return n ? n.innerText.replace(/\s+/g, ' ') : ''
  })
  check('3.4 空态文案不是「池里共 0 条 … 已分组 91 条」那种矛盾',
    emptyTitle.indexOf('池里共 0 条') === -1, emptyTitle)
  // 3.5：本池分解必须是**能相加的显式等式**（池 = 候选 + 已分组 + 忽略 + 其它），
  // 光断言「applied <= pool」看不出还有别的桶没算进去。
  const decomposition = emptyTitle.match(/本池 (\d+) 条会话 = 候选 (\d+) 条 \+ 已分组 (\d+) 条 \+ 手动忽略 (\d+) 条 \+ 其它状态 (\d+) 条/)
  const addsUp = !!decomposition &&
    Number(decomposition[1]) === Number(decomposition[2]) + Number(decomposition[3]) +
      Number(decomposition[4]) + Number(decomposition[5])
  check('3.5 空态本池分解是能相加的显式等式（池 = 候选 + 已分组 + 忽略 + 其它）',
    addsUp, emptyTitle)
  check('3.6 空态明说「全机器归属表里已分组 N 条（跨所有 agent，不是本池口径）」',
    /全机器归属表里已分组 \d+ 条/.test(emptyTitle) && /不是本池口径/.test(emptyTitle), emptyTitle)
  check('3.6b 取池阶段挡掉的桶（看板等）不出现在「本池 N 条」那一句里',
    !/本池 \d+ 条会话[^。]*看板/.test(emptyTitle), emptyTitle)
  check('3.7 job 里 pool 与 already_grouped 同源（already_grouped <= pool）且全库数单独给',
    !!emptyJob && Number(emptyJob.already_grouped) <= Number(emptyJob.pool) &&
    Number(emptyJob.already_grouped_all) >= Number(emptyJob.already_grouped),
    emptyJob ? JSON.stringify({ pool: emptyJob.pool, pool_applied: emptyJob.already_grouped,
      all_applied: emptyJob.already_grouped_all }) : '')
  await page.screenshot({ path: join(SHOTS, '04-work-empty.png') })

  // ---- 零 console error ----
  const realErrors = errors.filter((e) => !/404 \(Not Found\)/.test(e))
  evidence.console = { all: errors, real: realErrors }
  check('4. 零 console error / pageerror', realErrors.length === 0, realErrors.slice(0, 3).join(' | '))

  await browser.close()
  fixture.kill()
  site.close()

  const prodShaAfter = existsSync(PROD_DB) ? sha256(PROD_DB) : '(不存在)'
  check('5. 生产 auto-group/assignments.db 未被改动', prodShaAfter === prodShaBefore,
    prodShaBefore.slice(0, 16) + ' -> ' + prodShaAfter.slice(0, 16))
  const distShaAfter = sha256(join(DIST, 'index.js'))
  check('6. 产物在本次运行期间没被别的 worker 重建（sha256 开工/收工一致）',
    distShaAfter === distShaBefore, distShaBefore.slice(0, 16) + ' -> ' + distShaAfter.slice(0, 16))

  evidence.sha = { dist_before: distShaBefore, dist_after: distShaAfter,
    prod_db_before: prodShaBefore, prod_db_after: prodShaAfter }
  writeFileSync(join(SHOTS, 'evidence.json'), JSON.stringify(evidence, null, 2), 'utf8')
  writeFileSync(join(SHOTS, 'fixture.log'), fixtureLog, 'utf8')

  console.log('\n======== 汇总 ========')
  console.log('PASS: ' + pass + ' / FAIL: ' + failures.length + ' / TOTAL: ' + (pass + failures.length))
  if (failures.length) {
    console.log('\n失败项：')
    failures.forEach((f) => console.log('  ✗ ' + f))
  }
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => {
  console.error('PROBE-ERROR: ' + (e && e.stack ? e.stack : e))
  process.exit(3)
})
