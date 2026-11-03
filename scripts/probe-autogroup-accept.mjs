/**
 * hermes-console · 项目分组「同意并归入」交互闭环验收 + 四张桌面截图（t_0c8ab8c1 A 块）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-autogroup-accept.mjs --shots D:/tmp/shots
 *
 * 三层都是真的（与 probe-autogroup-panel.mjs 同一套路）：
 *   1. 真产物：dashboard/dist/index.js（= desktop/plugin.js）原样加载，只套一层 harness 入口；
 *   2. 真后端：scripts/autogroup_ui_fixture.py 起真实 dashboard/plugin_api.py，home 指沙盒，
 *      真 home 的 assignments.db / projects.db / auto_mode.json 用 sha256 盯住；
 *   3. 真浏览器：Playwright + Chromium(msedge)，1440×900。
 *
 * 四张截图（卡面验收项，缺一不算）：
 *   01-select-count.png      勾选态：已勾选 N 条的计数真的跟着变
 *   02-preview-from-to.png   预览弹层：逐条 from→to，且此时一条都没写
 *   03-batch-readback.png    确认后逐条回读：每条一行结论
 *   04-single-accept.png     单条「同意并归入」后的回读（行状态变「已归入」）
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
const FIXTURE_PORT = Number(process.env.HC_AGG_FIXTURE_PORT || 9195)
const SITE_PORT = Number(process.env.HC_AGG_SITE_PORT || 9194)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'autogroup-accept')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-agg-accept-'))
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

const HARNESS = `
import plugin, { injectConsoleStyles } from './entry.js'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

const store = {}
const pages = []
const ctx = {
  rest: async (path, opts) => {
    const options = opts || {}
    const init = { method: options.method || 'GET', headers: {} }
    if (options.body !== undefined) {
      init.headers['content-type'] = 'application/json'
      init.body = JSON.stringify(options.body)
    }
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
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
    join(WORK, 'harness.js'), '--bundle', '--format=esm', '--platform=browser', '--target=chrome110',
    '--jsx=automatic', '--loader:.css=text', '--legal-comments=none', '--charset=utf8',
    '--outfile=' + join(WORK, 'harness.bundle.js')
  ])
}

function startSite() {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head><meta charset="utf-8"><title>项目分组 · 同意并归入验收</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head><body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script></body></html>`
  const server = createServer((req, res) => {
    const safe = (fn) => {
      try { if (!res.writableEnded && !res.destroyed) fn() } catch (e) { /* 客户端已断开 */ }
    }
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const upstream = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: url, method: req.method, headers: req.headers },
        (up) => { safe(() => { res.writeHead(up.statusCode || 502, up.headers); up.pipe(res) }) }
      )
      upstream.on('error', (e) => {
        safe(() => { res.writeHead(502, { 'content-type': 'text/plain' }); res.end('upstream error: ' + e.message) })
      })
      req.pipe(upstream)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      safe(() => { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(html) })
      return
    }
    const map = { '/style.css': join(WORK, 'style.css'), '/theme.css': themeCss, '/harness.bundle.js': join(WORK, 'harness.bundle.js') }
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
  const child = spawn(PYTHON, [join(HERE, 'autogroup_ui_fixture.py'), '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], {
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
  for (let i = 0; i < 160; i += 1) {
    try {
      const res = await fetch(url)
      if (res.ok) return true
    } catch (e) { /* 还没起来 */ }
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
  console.log('[env] shots  :', SHOTS)
  console.log('[env] sandbox:', SANDBOX)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)

  const realFiles = [join(REAL_HOME, 'auto-group', 'assignments.db')]
  const realBefore = {}
  realFiles.forEach((p) => {
    realBefore[p] = sha256(p)
  })
  // projects.db 只比大小：project_meta.active_id 是桌面端维护的「当前选中项目」指针，
  // 桌面端一开就会自己翻，字节变了不等于我们写了项目表（项目行数=17、大小不变即可）。
  const realProjects = join(REAL_HOME, 'projects.db')
  const realProjectsSize = existsSync(realProjects) ? readFileSync(realProjects).length : -1
  const realAutoMode = join(REAL_HOME, 'auto-group', 'auto_mode.json')
  const realAutoModeBefore = sha256(realAutoMode)

  site = await startSite()
  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  page.setDefaultTimeout(20000)
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  const shot = async (name, selector) => {
    if (selector) {
      const handle = await page.$(selector)
      if (handle) {
        await handle.scrollIntoViewIfNeeded().catch(() => {})
        await sleep(250)
      }
    }
    await page.screenshot({ path: join(SHOTS, name) })
    console.log('        截图 ' + name)
  }

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 30000 })
  await page.click('.hc-nav__item:has-text("项目分组")')
  await page.waitForSelector('[data-agg-assign]', { timeout: 30000 })
  // 列表是异步读的：必须等行真的渲染出来再数，否则 rowCount=0 会把断言全带偏
  await page.waitForSelector('[data-agg-select]', { timeout: 40000 })
  await sleep(900)

  // ---- 1. 老板主诉①：每条建议行都得有「同意并归入」 -----------------------
  console.log('\n[1] 每条建议行都要有「同意并归入」')
  const rowCount = await page.$$eval('[data-agg-select]', (nodes) => nodes.length)
  const acceptCount = await page.$$eval('[data-agg-accept]', (nodes) => nodes.length)
  check('待确认列表有行', rowCount >= 2, 'rows=' + rowCount)
  check('★ 每条建议行都有「同意并归入」按钮（不再只有「忽略」）', acceptCount === rowCount,
    acceptCount + '/' + rowCount)

  // ---- 2. 目标项目默认「未选择」 ----------------------------------------
  console.log('\n[2] 目标项目默认「未选择」+ 按钮灰的时候写明原因')
  const targetAttr = await page.getAttribute('[data-agg-assign-target]', 'data-agg-assign-target')
  const targetText = await page.$eval('[data-agg-assign-target]',
    (node) => (node.selectedIndex >= 0 && node.options[node.selectedIndex] ? node.options[node.selectedIndex].textContent.trim() : ''))
  check('★ 目标项目默认显式「未选择」（不悄悄停在某个项目）',
    targetAttr === '' && /未选择/.test(targetText), JSON.stringify(targetAttr) + ' / ' + targetText)
  const state0 = await page.$eval('[data-agg-assign-preview]', (node) => ({ disabled: node.disabled, text: node.textContent.trim() }))
  const reason0 = ((await page.textContent('[data-agg-assign-reason]').catch(() => '')) || '').trim()
  check('未勾选时「预览归入」是灰的', state0.disabled === true, state0.text)
  check('★ 灰的时候按钮旁写明具体原因（不是点了没反应也没提示）', /还没勾选/.test(reason0), reason0)

  // ---- 3. 全选 / 反选 / 计数实时变 --------------------------------------
  console.log('\n[3] 全选 / 反选 / 已勾选计数')
  const count0 = Number(await page.getAttribute('[data-agg-selected]', 'data-agg-selected'))
  await page.click('[data-agg-select-all-btn]')
  await sleep(350)
  const countAll = Number(await page.getAttribute('[data-agg-selected]', 'data-agg-selected'))
  const checkedBoxes = await page.$$eval('[data-agg-select]', (nodes) => nodes.filter((n) => n.checked).length)
  check('★ 点「全选本页」后已勾选计数真的跟着变', count0 === 0 && countAll === rowCount,
    count0 + ' → ' + countAll + '（rows=' + rowCount + '）')
  check('复选框真的被勾上（不是只有数字变）', checkedBoxes === rowCount, checkedBoxes + '/' + rowCount)
  await shot('01-select-count.png', '[data-agg-assign]')
  await page.click('[data-agg-select-invert]')
  await sleep(350)
  const countInverted = Number(await page.getAttribute('[data-agg-selected]', 'data-agg-selected'))
  check('★ 反选：全选后点反选 → 0 条', countInverted === 0, String(countInverted))
  await page.click('[data-agg-select-invert]')
  await sleep(350)
  const ids = await page.$$eval('[data-agg-select]', (nodes) => nodes.map((n) => n.getAttribute('data-agg-select')))
  const spareId = ids[0]
  const batchIds = ids.slice(1)
  await page.click('[data-agg-select="' + spareId + '"]')
  await sleep(350)
  let countAfterOne = Number(await page.getAttribute('[data-agg-selected]', 'data-agg-selected'))
  check('★ 单条取消勾选后计数立刻 -1', countAfterOne === rowCount - 1, countAfterOne + '/' + rowCount)

  // ---- 3b. 内联切换也要有反应（老板说「点了没反应」，这条得能自证） --------
  // 注意：切换成功后这条会话就变 applied、会离开「待确认建议」列表 —— 所以先断言，
  // 再用 /autogroup/undo 把它还原成 suggested，后面的批量用例才有行可勾。
  console.log('\n[3b] 内联切换：下拉换项目 → 回读状态（带 8s 硬上限，卡住就算失败）')
  try {
    const swInfo = await page.evaluate(() => {
      const node = document.querySelector('[data-agg-switch]')
      return node ? { id: node.getAttribute('data-agg-switch'), value: node.value } : null
    })
    if (!swInfo) throw new Error('列表里没有 [data-agg-switch]')
    const swOptions = await page.$$eval('[data-agg-switch="' + swInfo.id + '"] option',
      (nodes) => nodes.map((n) => n.value).filter((v) => !!v))
    const swTarget = swOptions.filter((v) => v !== swInfo.value)[0]
    await Promise.race([
      page.selectOption('[data-agg-switch="' + swInfo.id + '"]', swTarget),
      sleep(8000).then(() => { throw new Error('selectOption 8 秒没返回（下拉卡住）') })
    ])
    let swNote = ''
    for (let i = 0; i < 60; i += 1) {
      swNote = ((await page.textContent('[data-agg-switch-note="' + swInfo.id + '"]', { timeout: 2000 }).catch(() => '')) || '').trim()
      if (swNote) break
      await sleep(150)
    }
    check('★ 内联切换给出实际状态（不是点了没反应）', /已切到|回读/.test(swNote), swNote.slice(0, 160))
    const swRow = ((await api('/autogroup/suggestions?status=applied&limit=200')).data.items || [])
      .filter((item) => item.session_id === swInfo.id)[0] || {}
    check('★ 后端回读：这条会话真的换到新项目', swRow.project_slug === swTarget,
      swInfo.id + ' → ' + (swRow.project_slug || '—') + '（期望 ' + swTarget + '）')
    const opsAfterSwitch = ((await api('/autogroup/ops?limit=20')).data.ops || [])
      .filter((op) => op.op === 'switch' && !op.undone)
    const switchOp = opsAfterSwitch[0] || {}
    const undoSwitch = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/undo', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ op_id: switchOp.op_id })
    }).then((r) => r.json())
    check('切回来（撤销这次 switch，把行还原成 suggested）',
      !!undoSwitch && undoSwitch.ok !== false, String(switchOp.op_id))
    await page.click('button:has-text("刷新数据")')
    await page.waitForSelector('[data-agg-select]', { timeout: 30000 })
    await sleep(800)
    // 刷新后重新勾：全选 → 把 spareId 那条留出来（后面单条「同意并归入」用它）
    await page.click('[data-agg-select-all-btn]')
    await sleep(300)
    await page.click('[data-agg-select="' + spareId + '"]')
    await sleep(300)
    countAfterOne = Number(await page.getAttribute('[data-agg-selected]', 'data-agg-selected'))
    check('刷新后重新勾选：批量 ' + countAfterOne + ' 条 + 单条 1 条',
      countAfterOne === rowCount - 1, countAfterOne + '/' + rowCount)
  } catch (err) {
    check('★ 内联切换没卡住', false, String(err && err.message ? err.message : err).slice(0, 200))
  }

  // ---- 4. 选目标项目 → 预览弹层（from→to） ------------------------------
  console.log('\n[4] 选目标项目 → 预览弹层（逐条 from→to，一条都不写）')
  const options = await page.$$eval('[data-agg-assign-target] option',
    (nodes) => nodes.map((n) => ({ value: n.value, label: n.textContent.trim() })).filter((o) => !!o.value))
  const target = options[options.length - 1]
  await page.selectOption('[data-agg-assign-target]', target.value)
  await sleep(350)
  const stateSel = await page.$eval('[data-agg-assign-preview]', (node) => node.disabled)
  check('选了目标项目后「预览归入」可点', stateSel === false, target.label)
  const reasonSel = ((await page.textContent('[data-agg-assign-reason]').catch(() => '')) || '').trim()
  check('原因提示随之消失', !/还没选|还没勾选/.test(reasonSel), reasonSel)

  const appliedBefore = (await api('/autogroup/suggestions?status=applied&limit=200')).data.total
  await page.click('[data-agg-assign-preview]')
  await page.waitForSelector('[data-agg-preview]', { timeout: 20000 })
  await sleep(450)
  const previewRows = await page.$$('[data-agg-preview-row]')
  const previewText = (((await page.textContent('[data-agg-preview]')) || '')).replace(/\s+/g, ' ')
  check('★ 预览弹层逐条列出（行数 = 勾选数）', previewRows.length === countAfterOne,
    previewRows.length + '/' + countAfterOne)
  check('★ 预览写明 from→to', previewText.indexOf('→') >= 0 && previewText.indexOf(target.label) >= 0)
  check('预览写明「还没写」', previewText.indexOf('还没写') >= 0)
  await shot('02-preview-from-to.png')
  const appliedDuring = (await api('/autogroup/suggestions?status=applied&limit=200')).data.total
  check('★★ 预览时后端 applied 一条没变（确认前不写）', appliedDuring === appliedBefore,
    appliedBefore + ' → ' + appliedDuring)

  // ---- 5. 确认归入 → 逐条回读 -------------------------------------------
  console.log('\n[5] 确认归入 → 逐条回读')
  await page.click('[data-agg-preview-confirm]')
  await page.waitForSelector('[data-agg-readback]', { timeout: 40000 })
  await sleep(900)
  const readbackRows = await page.$$('[data-agg-readback-row]')
  const badCount = Number(await page.getAttribute('[data-agg-readback]', 'data-agg-readback-bad'))
  check('★ 确认后逐条回读：每条一行', readbackRows.length === countAfterOne,
    readbackRows.length + '/' + countAfterOne)
  check('★★ 回读零不一致（slug / status / batch_id 都对）', badCount === 0, 'bad=' + badCount)
  const appliedNow = (await api('/autogroup/suggestions?status=applied&limit=200')).data
  const writtenRows = (appliedNow.items || []).filter((item) => batchIds.indexOf(item.session_id) >= 0)
  check('★ 后端回读：勾选的都变成 applied 且落在目标项目',
    writtenRows.length === batchIds.length && writtenRows.every((r) => r.project_slug === target.value && r.status === 'applied'),
    JSON.stringify(writtenRows.map((r) => r.session_id + ':' + r.project_slug)))
  await shot('03-batch-readback.png', '[data-agg-readback]')

  // ---- 6. 单条「同意并归入」：二次确认 → 写 → 立刻回读 → 行变「已归入」 ----
  console.log('\n[6] 单条「同意并归入」：二次确认（from→to）→ 回读')
  await page.waitForSelector('[data-agg-accept="' + spareId + '"]', { timeout: 40000 })
  const spareFrom = await page.$eval('[data-agg-switch="' + spareId + '"]', (node) => node.value)
  const spareTitle = ((await page.textContent('[data-session-id="' + spareId + '"] .hc-agg__title').catch(() => '')) || '').trim()
  await page.click('[data-agg-accept="' + spareId + '"]')
  await page.waitForSelector('[data-agg-accept-confirm="' + spareId + '"]', { timeout: 15000 })
  const confirmText = ((await page.textContent('[data-agg-accept-from-to="' + spareId + '"]')) || '').trim()
  check('★ 点「同意并归入」先出二次确认，写明 from→to', /→/.test(confirmText) && /applied/.test(confirmText),
    confirmText)
  const appliedAtConfirm = (await api('/autogroup/suggestions?status=applied&limit=200')).data.total
  check('二次确认阶段还没写（后端 applied 没变）', appliedAtConfirm === appliedNow.total,
    appliedNow.total + ' → ' + appliedAtConfirm)

  await page.click('[data-agg-accept-go="' + spareId + '"]')
  await page.waitForSelector('[data-agg-accepted="' + spareId + '"]', { timeout: 40000 })
  await sleep(400)
  const acceptedText = ((await page.textContent('[data-agg-accepted="' + spareId + '"]')) || '').replace(/\s+/g, ' ').trim()
  const acceptNote = ((await page.textContent('[data-agg-accept-note="' + spareId + '"]').catch(() => '')) || '').trim()
  check('★ 行状态从「建议」变「已归入」', /已归入/.test(acceptedText), acceptedText.slice(0, 120))
  check('★ 写完立刻回读并把结果贴在行上', /回读一致/.test(acceptNote), acceptNote.slice(0, 180))
  const one = ((await api('/autogroup/suggestions?status=applied&limit=200')).data.items || [])
    .filter((item) => item.session_id === spareId)[0] || {}
  check('★ 后端回读：这条真的变 applied 且落在它建议的项目',
    one.status === 'applied' && one.project_slug === spareFrom,
    spareId + ' → ' + (one.project_slug || '—') + ' / ' + (one.status || '—') + '（建议 ' + spareFrom + '）')
  const chips = await page.$$('[data-agg-accepted-count]')
  check('列表头出现「本次已归入 N 条」chip', chips.length === 1,
    chips.length ? await page.getAttribute('[data-agg-accepted-count]', 'data-agg-accepted-count') : '')
  await shot('04-single-accept.png', '[data-session-id="' + spareId + '"]')
  console.log('        （单条验收对象：' + (spareTitle || spareId) + '）')

  // ---- 7. 收尾：零报错 + 真 home 隔离 -----------------------------------
  console.log('\n[7] 收尾：零 console error / 零 pageerror / 真 home 未被写')
  check('全程零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('全程零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  realFiles.forEach((file) => {
    const same = sha256(file) === realBefore[file]
    check('真 home 未被写入：' + file.replace(REAL_HOME, ''), same, same ? '' : 'sha256 变了')
  })
  const projectsSizeNow = existsSync(realProjects) ? readFileSync(realProjects).length : -1
  check('真 home 的 projects.db 大小未变（项目行没被增删）', projectsSizeNow === realProjectsSize,
    realProjectsSize + ' → ' + projectsSizeNow)
  check('真 home 的 auto_mode.json 没被动过', sha256(realAutoMode) === realAutoModeBefore)
} catch (err) {
  failures.push('异常：' + (err && err.message ? err.message : String(err)))
  console.log('  FAIL 异常：' + (err && err.stack ? err.stack : err))
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) site.close()
  if (fixture) {
    fixture.child.kill()
  }
  console.log('\n[result] ' + pass + ' PASS / ' + failures.length + ' FAIL')
  if (failures.length) {
    failures.forEach((item) => console.log('  FAIL ' + item))
    process.exitCode = 1
  } else {
    console.log('[result] ALL PASS · 截图目录 ' + SHOTS)
  }
}


