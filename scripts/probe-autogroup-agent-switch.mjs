/**
 * hermes-console · 切换 Agent 的隔离验收（t_52125d5d）
 *
 * 跑法（任意目录）::
 *
 *     node D:/hermes-data/hermes/plugins/hermes-console/scripts/probe-autogroup-agent-switch.mjs
 *
 * 三层都是真的：
 *   1. **真产物** `dashboard/dist/index.js`（= desktop/plugin.js 同字节）原样加载，只套一层 harness；
 *   2. **真后端** `autogroup_ui_fixture.py`（真 plugin_api.py + 沙盒 home + 传输层 AI stub）；
 *   3. **真浏览器** Playwright + Chromium(msedge)。
 *
 * 断言（卡面验收 1~4 逐条）：
 *   A1. default 勾 3 条 → 切到 xiaomo → 勾选显示 0、列表内容全变（DOM 里 default 的 id 一条不剩）；
 *   A2. xiaomo 点扫描 → 请求体 `agent=xiaomo`，且**不含 default 的任何 session_id**；
 *   A3. 切回 default → 数据重载（重新发 agent=default 的会话请求），勾选仍为 0（不恢复）；
 *   A4. 建议列表 / AI 面板按 agent 隔离（数字与行数都随 agent 变，不拿全库数冒充）；
 *   A5. 三处产物 sha256 一致；
 *   A6. 零 console error / pageerror（豁免夹具 /modules 降级 404，A/B 已证既有）。
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
const PLUGIN = resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_AGENT_FIXTURE_PORT || 9281)
const SITE_PORT = Number(process.env.HC_AGENT_SITE_PORT || 9282)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'autogroup-agent-switch')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-agent-switch-'))
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
<title>hermes-console autogroup agent switch</title>
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
    /* 先登记再发：请求「发起时刻」才是作用域证据（响应可能几秒后才回来）。 */
    const entry = { method, path, status: null, body: options.body || null, t: Date.now() }
    requests.push(entry)
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    entry.status = res.status
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

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
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
  const page = await browser.newPage({ viewport: { width: 1360, height: 980 } })
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push('console: ' + msg.text()) })
  page.on('pageerror', (err) => errors.push('pageerror: ' + err.message))
  page.on('response', (res) => {
    if (res.status() === 404) errors.push('http404: ' + res.url())
    if (res.status() >= 400 && res.status() !== 404) errors.push('http' + res.status() + ': ' + res.url())
  })

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await sleep(1500)

  const navItem = page.locator('.hc-nav__item:has-text("项目分组")')
  if (await navItem.count()) {
    await navItem.first().click()
    await sleep(2000)
  }

  async function openDrawer(id) {
    await page.evaluate((drawerId) => {
      const node = document.querySelector('[data-agg2-drawer="' + drawerId + '"]')
      if (node) node.open = true
    }, id)
    await sleep(400)
  }

  async function listIds() {
    return page.evaluate(() =>
      Array.from(document.querySelectorAll('[data-agg2-session]')).map((n) => n.getAttribute('data-agg2-session'))
    )
  }
  async function selectedCount() {
    const node = page.locator('[data-agg2-selected-count]')
    if (!(await node.count())) return -1
    return Number(await node.first().getAttribute('data-agg2-selected-count'))
  }
  async function checkedIds() {
    return page.evaluate(() =>
      Array.from(document.querySelectorAll('[data-agg2-pick]'))
        .filter((n) => n.checked)
        .map((n) => n.getAttribute('data-agg2-pick'))
    )
  }
  async function clearRequests() {
    await page.evaluate(() => { window.__hcRequests.length = 0 })
  }
  async function requestsMatching(re) {
    return page.evaluate((src) => {
      const rx = new RegExp(src)
      return window.__hcRequests.filter((r) => rx.test(r.path))
    }, re.source)
  }

  await openDrawer('suggested')
  await openDrawer('ai')
  await sleep(600)

  // ---------- A1a. 先切到 default ----------
  const defaultChip = page.locator('[data-agg2-agent="default"]')
  check('A1. 界面上有 default 的 Agent 芯片', (await defaultChip.count()) > 0, String(await defaultChip.count()))
  /**
   * 点芯片 → 等「这个芯片变成 active」（.hc-agg2__agent--on）+ 等会话列表非空，
   * 再断言。浏览器点击可能在元素被重渲染时落空，等状态就绪比死等固定毫秒可靠；
   * 状态没变成 active 就直接把「点了没反应」当失败报出来，不掩盖。
   */
  async function switchAgent(name) {
    const chip = page.locator('[data-agg2-agent="' + name + '"]')
    if (!(await chip.count())) return false
    await chip.first().click()
    for (let i = 0; i < 60; i += 1) {
      const on = await page.evaluate((n) => {
        const node = document.querySelector('[data-agg2-agent="' + n + '"]')
        return !!node && node.className.indexOf('--on') >= 0
      }, name)
      if (on) return true
      await sleep(100)
    }
    return false
  }

  async function waitListPopulated(minMs) {
    const deadline = Date.now() + (minMs || 15000)
    while (Date.now() < deadline) {
      if ((await listIds()).length > 0) return true
      await sleep(150)
    }
    return false
  }

  await clearRequests()
  const switchedDefault = await switchAgent('default')
  check('A1. 点 default 芯片后该芯片变为 active（点击生效）', switchedDefault, String(switchedDefault))
  await waitListPopulated(15000)
  await sleep(600)

  const defaultIds = await listIds()
  check('A1. 切到 default 后列表非空', defaultIds.length > 0, defaultIds.join(','))
  check('A1. 切到 default 后发出 /overview/sessions?agent=default',
    (await requestsMatching(/\/overview\/sessions\?agent=default/)).length > 0,
    String((await requestsMatching(/\/overview\/sessions\?agent=default/)).length))

  // 勾 3 条
  const picks = page.locator('[data-agg2-pick]')
  const pickTotal = await picks.count()
  check('A1. default 列表里有 ≥3 条可勾选会话', pickTotal >= 3, pickTotal + ' 个 checkbox')
  const pickedDefault = []
  for (let i = 0; i < Math.min(3, pickTotal); i += 1) {
    const cb = picks.nth(i)
    pickedDefault.push(await cb.getAttribute('data-agg2-pick'))
    await cb.check({ force: true })
    await sleep(120)
  }
  await sleep(600)
  check('A1. default 勾 3 条后「选中」计数 = 3', (await selectedCount()) === 3, String(await selectedCount()))
  const scanLabelSel = (await page.locator('[data-agg2-scan]').first().innerText()).replace(/\s+/g, ' ')
  check('A1. 勾 3 条时扫描按钮文案 = 分析已选 3 条', scanLabelSel === '分析已选 3 条', scanLabelSel)
  const aiLabelSel = (await page.locator('[data-aic-run]').first().innerText()).replace(/\s+/g, ' ')
  check('A1. 勾 3 条时 AI 按钮文案 = 分析已选 3 条', aiLabelSel === '分析已选 3 条', aiLabelSel)

  // 记录 default 侧的建议列表与 AI 面板数字
  const defSuggestedRows = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-suggested-table]')
    return n ? Number(n.getAttribute('data-agg2-suggested-table')) : 0
  })
  const defSuggestedTotal = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-suggested-total]')
    return n ? Number(n.getAttribute('data-agg2-suggested-total')) : -1
  })
  const defAiTotal = await page.evaluate(() => {
    const n = document.querySelector('[data-aic-total]')
    return n ? n.getAttribute('data-aic-total') : ''
  })
  await page.screenshot({ path: join(SHOTS, '01-default-selected-3.png'), fullPage: false })

  // ---------- A1b. 切到 xiaomo ----------
  await clearRequests()
  const switchStartedAt = Date.now()
  const xiaomoChip = page.locator('[data-agg2-agent="xiaomo"]')
  check('A1. 界面上有 xiaomo 的 Agent 芯片', (await xiaomoChip.count()) > 0, String(await xiaomoChip.count()))
  const switchedXiaomo = await switchAgent('xiaomo')
  check('A1. 点 xiaomo 芯片后该芯片变为 active（点击生效）', switchedXiaomo, String(switchedXiaomo))
  for (let i = 0; i < 60; i += 1) {
    const ids = await listIds()
    if (ids.length && ids.every((id) => !defaultIds.includes(id))) break
    await sleep(150)
  }
  await sleep(900)

  const xiaomoIds = await listIds()
  check('A1. 切到 xiaomo 后勾选计数 = 0', (await selectedCount()) === 0, String(await selectedCount()))
  check('A1. 切到 xiaomo 后没有任何勾选框仍是勾上的', (await checkedIds()).length === 0,
    JSON.stringify(await checkedIds()))
  /**
   * 第二道闸没被当成遮羞布：切换时是**主动清空**（selected 变空），而不是「留着旧 id 靠过滤救」。
   * 所以界面上不该出现「另有 N 条不属于当前 Agent，已从勾选里剔除」这条提示。
   */
  check('A1. 切 Agent 是主动清空，没有靠作用域守卫兜底（无 dropped 提示）',
    (await page.locator('[data-agg2-selected-dropped]').count()) === 0, 'OK')
  check('A1. 切到 xiaomo 后列表内容全变（与 default 无交集）',
    xiaomoIds.length > 0 && xiaomoIds.every((id) => !defaultIds.includes(id)),
    'xiaomo=' + xiaomoIds.join(',') + ' / default=' + defaultIds.join(','))
  check('A1. 切到 xiaomo 后 DOM 里没有任何 default 的会话行',
    (await page.locator('[data-agg2-session="sess-agg-01"]').count()) === 0 &&
    (await page.locator('[data-agg2-session="sess-agg-02"]').count()) === 0, 'OK')

  const xiaomoReq = await requestsMatching(/\/overview\/sessions\?agent=xiaomo/)
  check('A2. 切换后重新拉了 /overview/sessions?agent=xiaomo', xiaomoReq.length > 0, String(xiaomoReq.length))
  /**
   * 硬要求 3 的强断言：切到 xiaomo 之后这个窗口里发出的**任何** GET 都不许再带 agent=default。
   * （写请求另在 A2 里按请求体断言。）
   */
  const afterSwitchReqs = await requestsMatching(/./)
  const defaultAgentReqs = afterSwitchReqs.filter(
    (r) => r.t >= switchStartedAt && /[?&]agent=default(&|$)/.test(r.path)
  )
  check('A2. 切到 xiaomo 后（点击之后发出）没有任何请求再带 agent=default', defaultAgentReqs.length === 0,
    defaultAgentReqs.map((r) => r.path).slice(0, 3).join(' | '))
  const scopeReq = await requestsMatching(/\/autogroup\/ai\/scope/)
  const scopeWithAgent = scopeReq.filter((r) => /agent=xiaomo/.test(r.path))
  check('A4. AI 面板重算：发了 /autogroup/ai/scope?agent=xiaomo', scopeWithAgent.length > 0,
    scopeReq.map((r) => r.path).join(' | '))

  const xmSuggestedTotal = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-suggested-total]')
    return n ? Number(n.getAttribute('data-agg2-suggested-total')) : -1
  })
  const xmSuggestedScope = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-suggested-scope]')
    return n ? n.getAttribute('data-agg2-suggested-scope') : ''
  })
  const xmOther = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-suggested-other]')
    return n ? Number(n.getAttribute('data-agg2-suggested-other')) : -1
  })
  check('A4. 建议列表标了当前作用域 = xiaomo', xmSuggestedScope === 'xiaomo', xmSuggestedScope)
  check('A4. 建议总数按 agent 重算（不等于 default 的全库数）', xmSuggestedTotal !== defSuggestedTotal,
    'default=' + defSuggestedTotal + ' / xiaomo=' + xmSuggestedTotal)
  check('A4. 别的 Agent 的建议条数被明确标出（不静默吞掉）', xmOther >= 0, String(xmOther))
  const xmSuggestedRows = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-suggested-table]')
    return n ? Number(n.getAttribute('data-agg2-suggested-table')) : 0
  })
  check('A4. xiaomo 的建议明细行数与 default 不同（隔离生效）', xmSuggestedRows !== defSuggestedRows,
    'default明细=' + defSuggestedRows + ' / xiaomo明细=' + xmSuggestedRows)

  const xmAiTotal = await page.evaluate(() => {
    const n = document.querySelector('[data-aic-total]')
    return n ? n.getAttribute('data-aic-total') : ''
  })
  check('A4. AI 候选总数按 agent 重算', xmAiTotal !== defAiTotal && xmAiTotal !== '',
    'default=' + defAiTotal + ' / xiaomo=' + xmAiTotal)

  await page.screenshot({ path: join(SHOTS, '02-xiaomo-cleared.png'), fullPage: false })

  // ---------- A2. 在 xiaomo 点扫描 ----------
  await clearRequests()
  const scanBtn = page.locator('[data-agg2-scan]')
  const xmScanLabel = (await scanBtn.first().innerText()).replace(/\s+/g, ' ')
  check('A2. xiaomo 扫描按钮回到「分析全部未分组」', /分析全部未分组/.test(xmScanLabel), xmScanLabel)
  await scanBtn.first().click()
  await sleep(1200)
  const scanReqs = await requestsMatching(/\/autogroup\/scan/)
  check('A2. 点了扫描确实发出 /autogroup/scan', scanReqs.length > 0, String(scanReqs.length))
  let scanBody = null
  if (scanReqs.length) {
    scanBody = scanReqs[scanReqs.length - 1].body || {}
    check('A2. 扫描请求体 agent=xiaomo', scanBody.agent === 'xiaomo', JSON.stringify(scanBody))
    const bodyIds = [].concat(scanBody.session_ids || [], scanBody.skip_ids || [])
    const leaked = bodyIds.filter((id) => defaultIds.includes(id) || pickedDefault.includes(id))
    check('A2. 请求体不含 default 的任何 session_id', leaked.length === 0, JSON.stringify(bodyIds))
  }

  // 等扫描 job 结束（异步轮询）
  for (let i = 0; i < 100; i += 1) {
    const done = await page.locator('[data-agg2-scan-phase="done"], [data-agg2-scan-phase="failed"]').count()
    if (done) break
    await sleep(300)
  }
  await sleep(800)
  const scanAgentAttr = await page.evaluate(() => {
    const n = document.querySelector('[data-agg2-scan-agent]')
    return n ? n.getAttribute('data-agg2-scan-agent') : ''
  })
  check('A2. 扫描结果面板标的 agent = xiaomo', scanAgentAttr === 'xiaomo', scanAgentAttr)
  const scanResultIds = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-agg2-scan-id], [data-agg2-scan-row]'))
      .map((n) => n.getAttribute('data-agg2-scan-id') || n.getAttribute('data-agg2-scan-row')))
  const scanLeak = scanResultIds.filter((id) => defaultIds.includes(id))
  check('A2. 扫描结果里没有 default 的 session_id', scanLeak.length === 0, JSON.stringify(scanResultIds))

  await page.screenshot({ path: join(SHOTS, '03-xiaomo-scan.png'), fullPage: false })

  // ---------- A2b. 在 xiaomo 点 AI 跑批（「任何请求都带当前 agent」的第二条写路径） ----------
  await clearRequests()
  const aiRunBtn = page.locator('[data-aic-run]')
  check('A2b. AI 跑批按钮存在且可点（xiaomo 有候选）',
    (await aiRunBtn.count()) > 0 && !(await aiRunBtn.first().isDisabled()), String(await aiRunBtn.count()))
  await aiRunBtn.first().click()
  await sleep(1500)
  const aiScanReqs = (await requestsMatching(/\/autogroup\/scan/)).filter((r) => r.body && r.body.ai === true)
  check('A2b. 点了 AI 跑批确实发出 /autogroup/scan {ai:true}', aiScanReqs.length > 0, String(aiScanReqs.length))
  if (aiScanReqs.length) {
    const aiBody = aiScanReqs[aiScanReqs.length - 1].body || {}
    check('A2b. AI 请求体 agent=xiaomo', aiBody.agent === 'xiaomo', JSON.stringify(aiBody))
    const aiIds = [].concat(aiBody.session_ids || [], aiBody.skip_ids || [])
    const aiLeak = aiIds.filter((id) => defaultIds.includes(id) || pickedDefault.includes(id))
    check('A2b. AI 请求体不含 default 的任何 session_id', aiLeak.length === 0, JSON.stringify(aiIds))
  }
  await page.screenshot({ path: join(SHOTS, '03b-xiaomo-ai-run.png'), fullPage: false })

  // ---------- A3. 切回 default ----------
  await clearRequests()
  const backSwitched = await switchAgent('default')
  check('A3. 点回 default 芯片生效（active）', backSwitched, String(backSwitched))
  for (let i = 0; i < 60; i += 1) {
    const ids = await listIds()
    if (ids.length && ids.every((id) => defaultIds.includes(id))) break
    await sleep(150)
  }
  await sleep(900)
  const backIds = await listIds()
  const backReq = await requestsMatching(/\/overview\/sessions\?agent=default/)
  check('A3. 切回 default 后重新拉了 /overview/sessions?agent=default', backReq.length > 0, String(backReq.length))
  check('A3. 切回 default 后列表恢复成 default 的会话',
    backIds.length > 0 && backIds.every((id) => defaultIds.includes(id)), backIds.join(','))
  check('A3. 切回 default 后勾选仍为 0（不恢复）', (await selectedCount()) === 0, String(await selectedCount()))
  check('A3. 切回 default 后没有任何勾选框是勾上的', (await checkedIds()).length === 0,
    JSON.stringify(await checkedIds()))
  const backScanLabel = (await page.locator('[data-agg2-scan]').first().innerText()).replace(/\s+/g, ' ')
  check('A3. 切回后扫描按钮是「分析全部未分组」（不是上一个 Agent 的残留态）',
    /分析全部未分组/.test(backScanLabel), backScanLabel)

  await page.screenshot({ path: join(SHOTS, '04-back-default-cleared.png'), fullPage: false })

  // ---------- A5. 三处产物 sha256 ----------
  const a = sha256(join(PLUGIN, 'dashboard', 'dist', 'index.js'))
  const b = sha256(join(PLUGIN, 'desktop', 'plugin.js'))
  const c = sha256(join(PLUGIN, '..', '..', 'desktop-plugins', 'hermes-console', 'plugin.js'))
  check('A5. 三处产物 sha256 一致', a === b && b === c, a + ' / ' + b + ' / ' + c)

  // ---------- A6. console ----------
  const isKnownFixture404 = (e) => /404 \(Not Found\)/.test(e) || /http404: .*\/modules(\?|$)/.test(e)
  const realErrors = errors.filter((e) => !isKnownFixture404(e))
  check('A6. 零 console error（豁免夹具 /modules 降级 404）', realErrors.length === 0,
    realErrors.length ? realErrors.slice(0, 3).join(' | ') : 'OK')
  check('A6. 零 pageerror', !errors.some((e) => e.startsWith('pageerror:')), 'OK')

  console.log('\n======== 汇总 ========')
  console.log('PASS: ' + pass + ' / FAIL: ' + failures.length + ' / TOTAL: ' + (pass + failures.length))
  if (failures.length) {
    console.log('\n失败项：')
    for (const f of failures) console.log('  ✗ ' + f)
  }

  await browser.close()
  fixture.kill()
  site.close()
  if (failures.length) process.exit(1)
  console.log('AGENT-SWITCH-PROBE-OK')
}

main().catch((e) => {
  console.error('FATAL: ' + e.message + '\n' + e.stack)
  process.exit(1)
})
