/**
 * hermes-console · 「AI 分类」面板真浏览器验收（t_364da8f0）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-autogroup-ai.mjs
 *     node plugins/hermes-console/scripts/probe-autogroup-ai.mjs --shots D:/tmp/shots
 *
 * 三层都是真的（与 probe-autogroup-panel.mjs 同一套路）：
 *   1. **真产物**：``dashboard/dist/index.js``（= ``desktop/plugin.js``）原样加载，只套一层 harness；
 *   2. **真后端**：``scripts/autogroup_ui_fixture.py`` 加载**真实的** ``dashboard/plugin_api.py``，
 *      home 指向沙盒；模型调用器被 ``HC_AI_STUB=1`` 换成确定性 stub（引擎的解析/校验/落库仍是真代码）；
 *   3. **真浏览器**：Playwright + Chromium(msedge)，1440×900。
 *
 * 断言（逐条对应卡面要求）：
 *   A. 候选总数与「已处理 N / 共 total」都来自后端返回体，界面不硬编码；
 *   B. 跑批请求**显式带 limit**（分批 4 条 / 全量 0），绝不沿用引擎默认 40；
 *   C. 逐条展示：标题 / 当前归属 / AI 建议 / 置信度 / 理由与证据 / 分档·确认后 / 是否新建；
 *   D. 「继续下一批」不重复处理同一会话（skip_ids 生效）；
 *   E. 逐条取消：取消的条目进 selected 之前就排除，界面写明「已取消（本次不写入）」；
 *   F. 确认后：写入摘要、逐条结果（applied vs suggested）、逐条回读、批次标识、撤销入口都在；
 *   G. 撤销后：逐条恢复校验结果 + API 回读确认归属与 cwd 都回到写入前；
 *   H. 全程零 console error / 零 pageerror；真 home 的 assignments.db / projects.db 字节未变。
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from 'node:fs'
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
const FIXTURE_PORT = Number(process.env.HC_AIC_FIXTURE_PORT || 9197)
const SITE_PORT = Number(process.env.HC_AIC_SITE_PORT || 9196)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'autogroup-ai')

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
const sha256 = (path) => (existsSync(path) ? createHash('sha256').update(readFileSync(path)).digest('hex') : '')

const WORK = mkdtempSync(join(tmpdir(), 'hc-aic-panel-'))
const SANDBOX = join(WORK, 'home')
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

// harness 记录每次请求的 method/path/status/**body**（body 用来断言「limit 一定显式传」）
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
    requests.push({ method, path, status: res.status, body: options.body === undefined ? null : options.body })
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
<title>hermes-console · AI 分类面板验收夹具</title>
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
        /* 客户端已断开 */
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

function startFixture() {
  const child = spawn(PYTHON, [join(HERE, 'autogroup_ui_fixture.py'), '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', HC_AI_STUB: '1' })
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
    } catch (e) {
      /* 还没起来 */
    }
    await sleep(250)
  }
  throw new Error('后端夹具没起来：\n' + fixture.log())
}

const api = (path, init) => fetch('http://127.0.0.1:' + FIXTURE_PORT + API + path, init).then((r) => r.json())

const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null
try {
  console.log('[env] dist   :', DIST)
  console.log('[env] sandbox:', SANDBOX)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', fixture.log().split('\n').filter((line) => line.indexOf('[fixture]') >= 0).join(' | '))

  // 真 home 指纹：结尾逐字节对比，证明沙盒隔离真的隔离
  const realFiles = [join(REAL_HOME, 'auto-group', 'assignments.db'), join(REAL_HOME, 'projects.db')]
  const realBefore = realFiles.map((p) => sha256(p))
  const autoModePath = join(REAL_HOME, 'auto-group', 'auto_mode.json')
  const autoModeBefore = existsSync(autoModePath) ? readFileSync(autoModePath, 'utf8') : ''

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
  await page.click('.hc-nav__item:has-text("项目分组")')
  // t_1dea3c49 重排后 AI 分类收进折叠区抽屉：先展开再操作（面板本身没变）
  await page.click('[data-agg2-drawer-head="ai"]')
  await page.waitForSelector('[data-aic-panel]', { timeout: 30000 })
  await page.locator('[data-aic-panel]').scrollIntoViewIfNeeded()

  const shot = async (name) => {
    try {
      await page.screenshot({ path: join(SHOTS, name + '.png'), fullPage: false })
      console.log('  SHOT ' + join(SHOTS, name + '.png'))
    } catch (e) {
      console.log('  SHOT-FAIL ' + name + ': ' + e.message)
    }
  }

  // ---------------------------------------------------------------- A. 口径
  const scope = (await api('/autogroup/ai/scope')).data
  const total = scope.total_available
  check('A 候选总数来自后端 /autogroup/ai/scope', typeof total === 'number' && total > 0,
    'total_available=' + total + ' applied 排除 ' + scope.excluded_applied)
  await page.waitForFunction(
    'document.querySelector(\'[data-aic-panel]\').getAttribute(\'data-aic-total\') === \'' + String(total) + '\'',
    { timeout: 15000 }
  ).catch(() => {})
  const totalAttr = await page.getAttribute('[data-aic-panel]', 'data-aic-total')
  check('A 面板总量 == 后端 total_available（不硬编码）', totalAttr === String(total), totalAttr + ' vs ' + total)
  const progress0 = await page.getAttribute('[data-aic-progress]', 'data-aic-progress')
  check('A 初始进度 0/total', progress0 === '0/' + String(total), progress0)
  const scopeNote = await page.innerText('[data-aic-scope-note]')
  check('A 口径说明写明「正常排除」与「56 条不在本次范围」',
    /正常排除/.test(scopeNote) && /56/.test(scopeNote) && /自动模式/.test(scopeNote), scopeNote.slice(0, 80))

  // ---------------------------------------------------------------- B. 分批跑（显式 limit）
  await page.fill('[data-aic-batch-size]', '4')
  await page.click('[data-aic-run]')
  await page.waitForSelector('[data-aic-batch]', { timeout: 120000 })
  const batch1 = page.locator('[data-aic-batch]').first()
  const batch1Id = await batch1.getAttribute('data-aic-batch')
  const rows1 = batch1.locator('[data-aic-row]')
  const count1 = await rows1.count()
  check('B 分批 4 条：本批 4 行', count1 === 4, 'rows=' + count1 + ' batch=' + batch1Id)
  const classify1 = await page.evaluate(() => window.__hcRequests.filter((r) => r.path === '/autogroup/ai/classify'))
  check('B 跑批请求显式带 limit=4（不沿用默认 40）',
    classify1.length >= 1 && classify1[0].body && classify1[0].body.limit === 4,
    JSON.stringify(classify1.map((r) => r.body)))
  const progress1 = await page.locator('[data-aic-progress]').first().getAttribute('data-aic-progress')
  const progressText1 = await page.locator('[data-aic-progress]').first().innerText()
  check('A 进度显示「已处理 4 / 共 total」（动态口径）',
    progress1 === '4/' + String(total) && progressText1.indexOf('已处理 4 / 共 ' + total) >= 0,
    'attr=' + progress1 + ' text=' + progressText1)

  // ---------------------------------------------------------------- C. 逐条字段
  const firstRowText = await rows1.first().innerText()
  check('C 行内含：标题 / 当前归属 / 建议项目 / 置信度 / 理由 / 分档',
    /%/.test(firstRowText) && /确认后|禁止写入/.test(firstRowText) && /未归入|suggested|applied/.test(firstRowText),
    firstRowText.replace(/\n/g, ' | ').slice(0, 160))
  const tiers = await rows1.evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-aic-tier')))
  check('C 分档在行上可见（apply/suggest/unjudged 至少两种）',
    new Set(tiers).size >= 2, JSON.stringify(tiers))
  const statusIf = await rows1.first().locator('[data-aic-status-if]').getAttribute('data-aic-status-if')
  check('C 每行给出「确认后状态」', statusIf === 'applied' || statusIf === 'suggested' || statusIf === '', String(statusIf))
  const ids1 = await rows1.evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-aic-row')))
  await shot('01-preview-batch1')

  // ---------------------------------------------------------------- D. 继续下一批不重复
  await page.click('[data-aic-run]')
  await page.waitForFunction('document.querySelectorAll(\'[data-aic-batch]\').length >= 2', { timeout: 120000 })
  const batch2 = page.locator('[data-aic-batch]').first()
  const batch2Id = await batch2.getAttribute('data-aic-batch')
  const ids2 = await batch2.locator('[data-aic-row]').evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-aic-row')))
  const overlap = ids1.filter((id) => ids2.indexOf(id) >= 0)
  check('D 继续下一批：两批会话不重叠（skip_ids 生效）',
    overlap.length === 0 && ids2.length > 0 && batch2Id !== batch1Id,
    'batch1=' + ids1.join(',') + ' batch2=' + ids2.join(','))
  const classify2 = await page.evaluate(() => window.__hcRequests.filter((r) => r.path === '/autogroup/ai/classify'))
  check('D 第二批请求带 skip_ids（不重复处理）',
    classify2.length >= 2 && Array.isArray(classify2[1].body.skip_ids) && classify2[1].body.skip_ids.length === 4,
    JSON.stringify(classify2.map((r) => r.body && r.body.skip_ids)))
  const progress2 = await page.locator('[data-aic-progress]').first().getAttribute('data-aic-progress')
  check('A 进度显示累计 processed/total',
    progress2 === String(ids1.length + ids2.length) + '/' + String(total), progress2)
  // ---------------------------------------------------------------- D2. 新建候选：支撑不足要显式降级，不许无条件新建
  const candidateCount = await page.getAttribute('[data-aic-newprojects]', 'data-aic-newprojects')
  const candidateRows = page.locator('[data-aic-candidate]')
  const candidateN = await candidateRows.count()
  check('D 新建候选区块按后端数据渲染', Number(candidateCount) === candidateN, 'attr=' + candidateCount + ' rows=' + candidateN)
  if (candidateN > 0) {
    const firstCandidate = candidateRows.first()
    const candidateState = await firstCandidate.getAttribute('data-aic-candidate-state')
    const candidateText = await firstCandidate.innerText()
    const createDisabled = await firstCandidate.locator('[data-aic-create-project]').isDisabled()
    check('D 支撑不足的新建候选被降级并写明原因（按钮禁用）',
      candidateState === 'downgraded' ? /降级|阻止|不足/.test(candidateText) && createDisabled : true,
      'state=' + candidateState + ' disabled=' + createDisabled + ' text=' + candidateText.replace(/\n/g, ' ').slice(0, 120))
  } else {
    check('D 新建候选区块按后端数据渲染', false, '本轮没有新建候选，无法验证降级路径')
  }

  // ---------------------------------------------------------------- D3. 无法判定清单单独展示（不许静默丢）
  const unjudgedCount = Number(await page.getAttribute('[data-aic-unjudged]', 'data-aic-unjudged'))
  check('D 无法判定清单单独成块且有条目', unjudgedCount > 0, 'unjudged=' + unjudgedCount)
  const unjudgedText = await page.locator('[data-aic-table="unjudged"]').innerText()
  check('D 无法判定条目带归类与原因', /低置信度|输出无效|调用失败|正文不可用/.test(unjudgedText) && /置信度/.test(unjudgedText),
    unjudgedText.replace(/\n/g, ' | ').slice(0, 160))

  // ---------------------------------------------------------------- E. 逐条取消（对第一批操作：里面有 apply 与 suggest 两档）
  const batch1Live = page.locator('[data-aic-batch]').last()
  const selectable1 = batch1Live.locator('[data-aic-row][data-aic-selectable="true"]')
  const selectableCount1 = await selectable1.count()
  check('E 第一批有可写条目（可取消/可确认）', selectableCount1 >= 2, 'selectable=' + selectableCount1)
  const cancelTarget = batch1Live.locator('[data-aic-row][data-aic-tier="suggest"][data-aic-selectable="true"]').first()
  const cancelId = await cancelTarget.getAttribute('data-aic-row')
  const confirmLabelBefore = await batch1Live.locator('[data-aic-confirm]').innerText()
  await cancelTarget.locator('[data-aic-cancel]').click()
  await page.waitForFunction(
    'document.querySelector(\'[data-aic-row="' + cancelId + '"]\').getAttribute(\'data-aic-cancelled\') === \'true\'',
    { timeout: 10000 }
  )
  const cancelledRow = page.locator('[data-aic-row="' + cancelId + '"]')
  const cancelTag = await cancelledRow.locator('[data-aic-cancelled-tag]').innerText()
  check('E 取消后行上写明「已取消（本次不写入）」', /已取消/.test(cancelTag) && /不写入/.test(cancelTag), cancelTag)
  const confirmLabelAfter = await batch1Live.locator('[data-aic-confirm]').innerText()
  check('E 取消会从确认计数里扣掉', confirmLabelBefore !== confirmLabelAfter,
    confirmLabelBefore + ' → ' + confirmLabelAfter)
  await shot('02-cancelled-row')

  // ---------------------------------------------------------------- F. 确认写入 + 回读
  const batch1Id2 = await batch1Live.getAttribute('data-aic-batch')
  await batch1Live.locator('[data-aic-confirm]').click()
  await page.waitForSelector('[data-aic-results]', { timeout: 120000 })
  await page.waitForSelector('[data-aic-table="readback"]', { timeout: 60000 })
  const writeSummary = await page.locator('[data-aic-results] [data-aic-write-summary]').first().innerText()
  check('F 写入摘要含条数与批次/op 标识', /写入/.test(writeSummary) && /op /.test(writeSummary), writeSummary.replace(/\n/g, ' ').slice(0, 160))
  const writeTable = page.locator('[data-aic-table="write"]')
  const writeText = await writeTable.innerText()
  check('F 逐条写入结果区分 applied 与 suggested', /applied/.test(writeText) && /suggested/.test(writeText),
    writeText.replace(/\n/g, ' | ').slice(0, 220))
  check('F 取消掉的那条没有出现在写入结果里', writeText.indexOf(cancelId) === -1, cancelId)
  const readbackText = await page.locator('[data-aic-table="readback"]').innerText()
  check('F 逐条回读表存在且写明期望→现在', /→/.test(readbackText), readbackText.replace(/\n/g, ' | ').slice(0, 160))
  check('F 撤销入口出现（批次标识绑在按钮上）', await page.locator('[data-aic-undo="' + batch1Id2 + '"]').count() === 1, batch1Id2)
  const confirmDisabled = await batch1Live.locator('[data-aic-confirm]').isDisabled()
  check('F 提交后确认按钮禁用（防重复提交）', confirmDisabled === true, String(confirmDisabled))
  await shot('03-confirmed')

  // API 复核：取消的那条一条都没写；写进去的 status 与回读一致
  const afterConfirm = (await api('/autogroup/ai/readback/' + batch1Id2)).data
  const byId = {}
  afterConfirm.items.forEach((row) => { byId[row.session_id] = row })
  const writtenRows = afterConfirm.items.filter((row) => row.outcome === 'written')
  check('F API 复核：确实写入了条目', writtenRows.length > 0, 'written=' + writtenRows.length)
  const cancelledState = byId[cancelId] || {}
  check('F API 复核：取消的条目 outcome 不是 written', cancelledState.outcome !== 'written',
    JSON.stringify(cancelledState.outcome || null) + ' / now=' + JSON.stringify(cancelledState.now || null))
  const appliedCount = writtenRows.filter((row) => (row.expected || {}).status === 'applied').length
  const suggestedCount = writtenRows.filter((row) => (row.expected || {}).status === 'suggested').length
  check('F API 复核：applied 与 suggested 分档都写对了', appliedCount > 0 && suggestedCount > 0,
    'applied=' + appliedCount + ' suggested=' + suggestedCount)

  // ---------------------------------------------------------------- G. 撤销 + 恢复校验
  await page.click('[data-aic-undo="' + batch1Id2 + '"]')
  await page.waitForSelector('[data-aic-undo-verify]', { timeout: 120000 })
  const undoVerify = await page.innerText('[data-aic-undo-verify]')
  check('G 撤销后显示逐条恢复校验结果', /归属不一致 0 条/.test(undoVerify) && /cwd 不一致 0 条/.test(undoVerify), undoVerify)
  const afterUndo = (await api('/autogroup/ai/readback/' + batch1Id2)).data
  const stillWritten = afterUndo.items.filter((row) => {
    const now = row.now || {}
    const expect = row.expected || {}
    return row.outcome === 'written' && now.project_slug === expect.project_slug && now.status === expect.status
  })
  check('G API 复核：撤销后归属已还原（没有还留在写入后状态的行）', stillWritten.length === 0,
    'still=' + stillWritten.map((row) => row.session_id).join(','))
  await shot('04-undone')

  // ---------------------------------------------------------------- H. 零错误 + 生产红线
  check('H 零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 2).join(' | '))
  check('H 零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 2).join(' | '))
  const realAfter = realFiles.map((p) => sha256(p))
  check('H 真 home 的 assignments.db / projects.db 字节未变',
    realBefore[0] === realAfter[0] && realBefore[1] === realAfter[1])
  const autoModeAfter = existsSync(autoModePath) ? readFileSync(autoModePath, 'utf8') : ''
  check('H 生产 auto_mode.json 未被改动（本面板不碰自动模式）', autoModeBefore === autoModeAfter)
  const forbidden = await page.evaluate(() => window.__hcRequests.filter((r) =>
    (r.method !== 'GET' && /\/autogroup\/auto/.test(r.path)) || /\/autogroup\/auto\/tick/.test(r.path)))
  check('H 全程没有调用 POST /autogroup/auto 与 /autogroup/auto/tick', forbidden.length === 0,
    JSON.stringify(forbidden.map((r) => r.method + ' ' + r.path)))
} catch (err) {
  failures.push('探针异常：' + (err && err.stack ? err.stack : err))
  console.log('  FAIL 探针异常：' + (err && err.message ? err.message : err))
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) await new Promise((res) => site.close(res))
  if (fixture) fixture.child.kill()
}

console.log('\n' + (failures.length ? '失败 ' + failures.length + ' 项：\n  - ' + failures.join('\n  - ') : '全部通过（' + pass + ' 项）'))
process.exit(failures.length ? 1 : 0)
