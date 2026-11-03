/**
 * hermes-console · t_11b7edbb 验收探针：项目分组「AI 模型」面板**能真的配出模型**。
 *
 * 真产物 dist + 真后端 fixture（隔离沙盒 home，providers 段照抄真 home）+ 真浏览器。
 * 逐条对应卡面验收：
 *   F1 打开面板默认落在**具体 Agent**（不是 global 段），并显式说明是面板自己挑的
 *   F2 Agent=work → 「该 Agent 用独立模型」→ provider 下拉 → 选 balance →
 *      模型下拉含 deepseek-v4-flash → 选中 → 保存并回读
 *   F3 回读盘上 auto-group/ai_model.json：agents.work 真写入；其它 agent 与 global 段**逐字节未变**
 *   F4 global 段：「保存并回读」是**禁用态**，且有「此处不能指定模型」说明；点不到假成功
 *   F5 provider 行是**可点入口**（onClick 生效、选中态、与 form.provider 联动）
 *   F6 生效：受控 AI 分析（transport 打桩，provider/model 解析与 ai_calls.jsonl 审计是真代码）
 *      新增行 agent=work 且 provider/model 与所选一致
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
const FIXTURE_PORT = Number(process.env.HC_MD_FIXTURE_PORT || (9400 + (process.pid % 400)))
const SITE_PORT = FIXTURE_PORT + 1
const API = '/api/plugins/hermes-console'
const FIX = 'http://127.0.0.1:' + FIXTURE_PORT

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'autogroup-model-fix')

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) { pass += 1; console.log('  PASS ' + name + (extra ? '  — ' + extra : '')) }
  else { failures.push(name + (extra ? '  — ' + extra : '')); console.log('  FAIL ' + name + (extra ? '  — ' + extra : '')) }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim()

const WORK = mkdtempSync(join(tmpdir(), 'hc-md-fix-'))
// 沙盒用**固定**路径（不用 mkdtemp）：Node 的 tmpdir() 指向 Hermes scratch，随机名目录里起
// fixture 时 `api_agents.hermes_home()` 的上溯探测会落到别处，options 返回 400；
// 固定路径在同类探针里已被证明可用，也便于失败后人工复查。
const SANDBOX = process.env.HC_MD_SANDBOX ||
  'D:/hermes-data/hermes/profiles/code-worker/cache/scratch/probe-fix-home'
mkdirSync(SHOTS, { recursive: true })
try { symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction') }
catch (e) { throw new Error('junction 失败：' + e.message) }
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

const AI_MODEL_JSON = () => join(SANDBOX, 'auto-group', 'ai_model.json')
const AI_CALLS_JSONL = () => join(SANDBOX, 'auto-group', 'ai_calls.jsonl')
function readJson(p) { try { return JSON.parse(readFileSync(p, 'utf8')) } catch (e) { return null } }

async function apiGet(path) {
  const r = await fetch(FIX + API + path)
  return { status: r.status, body: await r.json() }
}
async function apiPut(path, body) {
  const r = await fetch(FIX + API + path, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  return { status: r.status, body: await r.json() }
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
    try { const r = await fetch(FIX + API + '/health'); if (r.ok) { ready = true; break } } catch (e) {}
    await sleep(150)
  }
  if (!ready) { console.log('fixture 没起来：\n' + flog); fixture.kill(); site.close(); process.exit(2) }

  // 先播一份「已有别的 Agent 声明 + global 段声明」的盘上初值：
  // 只有初值非空，F3 的「其它 agent 一个字节未变 / global 段未变」才是真有牙的断言
  // （空文件时后端首次保存会顺带写出默认 global 段，那不是「改了 global」）。
  const seed = {
    schema: 2,
    agents: { xiaomo: { mode: 'module', provider: 'gemini', model: 'gemini-2.0-flash' } },
    global: { mode: 'global', provider: null, model: null },
    updated_at: Math.floor(Date.now() / 1000) - 3600,
    updated_by: 'probe-seed'
  }
  mkdirSync(join(SANDBOX, 'auto-group'), { recursive: true })
  writeFileSync(AI_MODEL_JSON(), JSON.stringify(seed, null, 2) + '\n', 'utf8')

  const before = readJson(AI_MODEL_JSON())
  const beforeRaw = existsSync(AI_MODEL_JSON()) ? readFileSync(AI_MODEL_JSON(), 'utf8') : ''
  console.log('盘上初值 = ' + norm(beforeRaw).slice(0, 300))
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = []
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
  page.on('response', (r) => { if (r.status() >= 400) errors.push('http' + r.status() + ': ' + r.url()) })
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await sleep(1500)

  const nav = page.locator('.hc-nav__item:has-text("项目分组")')
  if (await nav.count()) { await nav.first().click(); await sleep(2000) }

  async function openModelDrawer() {
    const vis = async () => {
      const p = page.locator('[data-agm-panel]')
      if (!(await p.count())) return false
      return p.first().isVisible()
    }
    if (await vis()) return true
    for (const sel of ['[data-agg2-drawer-head="model"]', '.hc-agg2__drawer-head:has-text("AI 模型")',
                       'summary:has-text("AI 模型")']) {
      const loc = page.locator(sel)
      if (await loc.count()) {
        try { await loc.first().click({ timeout: 2000 }) } catch (e) { continue }
        for (let i = 0; i < 40; i += 1) { if (await vis()) return true; await sleep(100) }
      }
    }
    return vis()
  }
  const opened = await openModelDrawer()
  check('F1. 打开「AI 模型」抽屉，面板渲染 [data-agm-panel]', opened)
  if (!opened) {
    await page.screenshot({ path: join(SHOTS, 'fix-00-no-panel.png'), fullPage: true })
    console.log('调试 body =', norm(await page.locator('body').innerText()).slice(0, 600))
    await browser.close(); site.close(); fixture.kill(); process.exit(2)
  }

  // ---------- F1. 默认 target ----------
  const targetSel = page.locator('[data-agm-target-select]')
  const defaultTarget = await targetSel.inputValue()
  const targetOpts = await targetSel.locator('option').evaluateAll((els) => els.map((e) => e.value))
  check('F1. 默认配置对象**不是** global 段', defaultTarget && defaultTarget !== '__global__', 'target=' + defaultTarget)
  check('F1. 默认配置对象是一个真实 Agent（在选择器里）',
    targetOpts.indexOf(defaultTarget) >= 0 && defaultTarget !== '', targetOpts.join(','))
  const autoNote = await page.locator('[data-agm-auto-target]').count()
    ? norm(await page.locator('[data-agm-auto-target]').first().innerText()) : ''
  check('F1. 面板自己挑的 Agent 有显式说明（不是静默替用户选）', autoNote.length > 0, autoNote.slice(0, 140))
  await page.screenshot({ path: join(SHOTS, 'fix-01-default-target.png'), fullPage: false })

  // ---------- F4. global 段是只读页，不是假成功 ----------
  await targetSel.selectOption('__global__')
  await sleep(1200)
  const globalNote = await page.locator('[data-agm-global-note]').count()
    ? norm(await page.locator('[data-agm-global-note]').first().innerText()) : ''
  check('F4. 选 global 段时写明「跟随各 Agent config.yaml / 此处不能指定模型 / 请选一个 Agent」',
    /config\.yaml/.test(globalNote) && /不能指定/.test(globalNote) && /选一个 Agent/.test(globalNote),
    globalNote.slice(0, 200))
  const globalSaveDisabled = await page.locator('[data-agm-save]').first().isDisabled()
  check('F4. global 段的「保存并回读」是**禁用态**', globalSaveDisabled === true)
  const globalProvRows = await page.locator('[data-agm-provider]').count()
  check('F4. global 段不再列 provider 行（不再给「像能点」的假入口）', globalProvRows === 0, 'rows=' + globalProvRows)
  const globalSaveTitle = await page.locator('[data-agm-save]').first().getAttribute('title')
  check('F4. 禁用理由写在 title 上（不是死按钮）', /global 段不写入|不写入/.test(globalSaveTitle || ''), globalSaveTitle || '')
  // 程序化点击也不能伪造「保存成功」
  await page.evaluate(() => { const b = document.querySelector('[data-agm-save]'); if (b) b.click() })
  await sleep(800)
  const fakeOk = await page.locator('[data-agm-save-ok]').count()
  const blocked = await page.locator('[data-agm-save-blocked]').count()
  const afterGlobalClick = readJson(AI_MODEL_JSON())
  check('F4. 程序化点击 global 保存**不产生** data-agm-save-ok（不伪造成功）', fakeOk === 0, 'ok=' + fakeOk + ' blocked=' + blocked)
  check('F4. global 段点击后盘上 ai_model.json 逐字节未变',
    (existsSync(AI_MODEL_JSON()) ? readFileSync(AI_MODEL_JSON(), 'utf8') : '') === beforeRaw)
  await page.screenshot({ path: join(SHOTS, 'fix-02-global-readonly.png'), fullPage: false })

  // ---------- F2. 完整链路：Agent=work → 独立模型 → balance → deepseek-v4-flash ----------
  await targetSel.selectOption('work')
  // 等「读配置」真的落定：loadConfig 是异步的，它在 resolve 时会按盘上声明重设 form.mode
  // （沙盒里 work 没有声明 ⇒ 会设回 inherit）。在它落地前点单选，会被它覆盖 —— 这是**探针的**竞态，
  // 不是面板的 bug，所以这里显式等 loading 消失再操作。
  for (let i = 0; i < 100; i += 1) {
    if (!(await page.locator('[data-agm-loading]').count())) break
    await sleep(100)
  }
  await sleep(600)
  async function ensureModuleMode() {
    const mr = page.locator('[data-agm-mode="module"] input')
    if (!(await mr.count())) return false
    if (await mr.first().isChecked()) return true
    await mr.first().check()
    await sleep(400)
    return await mr.first().isChecked()
  }
  const modeModule = page.locator('[data-agm-mode="module"] input')
  check('F2. 具体 Agent 下「这个 Agent 用独立模型」可选（不被硬禁用）',
    (await modeModule.count()) > 0 && !(await modeModule.first().isDisabled()))
  await ensureModuleMode()
  // 等 provider 下拉真的带上选项（options 是第二次请求，别只等元素出现）
  let provOptions = []
  for (let i = 0; i < 60; i += 1) {
    if (!(await page.locator('[data-agm-provider-select]').count())) { await ensureModuleMode(); await sleep(300); continue }
    provOptions = await page.locator('[data-agm-provider-select]')
      .locator('option').evaluateAll((els) => els.map((e) => e.value).filter((v) => v))
    if (provOptions.length > 0) break
    await sleep(300)
  }
  check('F2. 选「独立模型」后 provider 下拉出现', (await page.locator('[data-agm-provider-select]').count()) > 0)
  const provSel = page.locator('[data-agm-provider-select]')
  check('F2. provider 下拉有可选项（work 的目录）', provOptions.length > 0, provOptions.join(','))
  check('F2. provider 下拉含 balance', provOptions.indexOf('balance') >= 0, provOptions.join(','))
  if (provOptions.length === 0) {
    const diag = await page.evaluate(async () => {
      const out = {}
      const r = await fetch('/api/plugins/hermes-console/autogroup/ai/model/options?agent=work')
      out.status = r.status
      try { const j = await r.json(); out.providers = ((j.data && j.data.providers) || []).map((p) => p.provider); out.err = j.message || '' } catch (e) { out.body = await r.text() }
      const sel = document.querySelector('[data-agm-provider-select]')
      out.optionCount = sel ? sel.options.length : -1
      const blk = document.querySelector('[data-agm-providers]')
      out.block = blk ? blk.getAttribute('data-agm-providers') : null
      const errEl = document.querySelector('[data-agm-providers-error]')
      out.blockErr = errEl ? errEl.textContent : null
      const emptyEl = document.querySelector('[data-agm-providers-empty]')
      out.blockEmpty = emptyEl ? emptyEl.textContent.slice(0, 120) : null
      return out
    })
    console.log('DIAG = ' + JSON.stringify(diag))
    console.log('DIAG errors = ' + JSON.stringify(errors.slice(0, 5)))
    console.log('DIAG fixture = ' + flog.split('\n').slice(-8).join(' | '))
  }

  await provSel.selectOption('balance')
  let modelOptions = []
  let hasSel = 0
  for (let i = 0; i < 60; i += 1) {
    await sleep(300)
    hasSel = await page.locator('[data-agm-model-select]').count()
    modelOptions = hasSel ? await page.locator('[data-agm-model-select]')
      .locator('option').evaluateAll((els) => els.map((e) => e.value).filter((v) => v)) : []
    if (modelOptions.length > 0) break
  }
  const modelSel = page.locator('[data-agm-model-select]')
  const modelInput = page.locator('[data-agm-model-input]')
  const modelsAttr = await page.locator('[data-agm-models]').count()
    ? await page.locator('[data-agm-models]').first().getAttribute('data-agm-models') : ''
  const modelsSource = await page.locator('[data-agm-models]').count()
    ? await page.locator('[data-agm-models]').first().getAttribute('data-agm-models-source') : ''
  check('F2. 选 balance 后模型下拉出现（不是空下拉）', hasSel > 0 && modelOptions.length > 0,
    'n=' + modelsAttr + ' source=' + modelsSource + ' ' + modelOptions.slice(0, 4).join(','))
  check('F2. 模型清单含 deepseek-v4-flash', modelOptions.indexOf('deepseek-v4-flash') >= 0,
    modelOptions.slice(0, 8).join(','))
  check('F2. 模型清单来源写明（config=离线清单）', modelsSource === 'config', modelsSource)

  await modelSel.selectOption('deepseek-v4-flash')
  await sleep(500)
  check('F2. 选中后表单里有值', (await modelSel.inputValue()) === 'deepseek-v4-flash')
  const saveBtn = page.locator('[data-agm-save]').first()
  check('F2. provider+model 齐了「保存并回读」可点', !(await saveBtn.isDisabled()))
  await page.screenshot({ path: join(SHOTS, 'fix-03-work-two-level.png'), fullPage: false })

  await saveBtn.click()
  let saved = false
  for (let i = 0; i < 120; i += 1) {
    if (await page.locator('[data-agm-save-ok]').count()) { saved = true; break }
    if (await page.locator('text=保存失败').count()) break
    await sleep(100)
  }
  const okText = saved ? norm(await page.locator('[data-agm-save-ok]').first().innerText()) : ''
  check('F2. 保存并回读返回成功提示', saved, okText.slice(0, 200))
  check('F2. 成功提示里带「已回读盘上真值」且 provider/model 是所选的',
    /回读/.test(okText) && /balance/.test(okText) && /deepseek-v4-flash/.test(okText), okText.slice(0, 200))
  await page.screenshot({ path: join(SHOTS, 'fix-04-saved.png'), fullPage: false })

  // ---------- F3. 盘上回读：只动 work，其它 agent 与 global 逐字节不变 ----------
  const after = readJson(AI_MODEL_JSON())
  const wDecl = after && after.agents && after.agents.work
  check('F3. auto-group/ai_model.json 的 agents.work 真写入了',
    !!wDecl && wDecl.provider === 'balance' && wDecl.model === 'deepseek-v4-flash',
    JSON.stringify(wDecl))
  const beforeAgents = (before && before.agents) || {}
  const afterAgents = (after && after.agents) || {}
  const otherKeys = Object.keys(afterAgents).filter((k) => k !== 'work')
  const otherSame = otherKeys.every((k) => JSON.stringify(afterAgents[k]) === JSON.stringify(beforeAgents[k]))
  const otherMissing = Object.keys(beforeAgents).filter((k) => k !== 'work' && !(k in afterAgents))
  check('F3. 其它 agent 的声明一个字节未变', otherSame && otherMissing.length === 0,
    'keys=' + otherKeys.join(',') + ' missing=' + otherMissing.join(','))
  check('F3. global 段未变',
    JSON.stringify((before && before.global) || null) === JSON.stringify((after && after.global) || null),
    JSON.stringify(after && after.global))
  check('F3. 盘上文件确实变了（不是「报成功但盘上无变化」）', beforeRaw !== readFileSync(AI_MODEL_JSON(), 'utf8'))

  // 后端直读再核一次（绕过界面，证明是盘上真值）
  const readBack = await apiGet('/autogroup/ai/model?agent=work')
  const rbDecl = (readBack.body && readBack.body.data && readBack.body.data.agent_declaration) || {}
  const rbGroup = rbDecl.group || {}
  check('F3. 后端 GET /autogroup/ai/model?agent=work 回读到同一组值',
    rbGroup.provider === 'balance' && rbGroup.model === 'deepseek-v4-flash',
    JSON.stringify(rbGroup))

  // ---------- F5. provider 行是**可点入口** ----------
  const provRow = page.locator('[data-agm-provider="gemini"]')
  const rowCount = await provRow.count()
  check('F5. provider 行渲染出来', rowCount > 0)
  if (rowCount > 0) {
    check('F5. provider 行是 <button>（真可点，不是 div 装饰）',
      (await provRow.first().evaluate((el) => el.tagName)) === 'BUTTON')
    const hasOnClick = await provRow.first().evaluate((el) => {
      // React 把 onClick 挂成 root 上的委托：点一下看有没有反应才是硬证据
      return el.hasAttribute('data-agm-provider-pick')
    })
    check('F5. provider 行带可点标记 data-agm-provider-pick', hasOnClick === true)
    const beforeClickProvider = await provSel.inputValue()
    await provRow.first().click()
    await sleep(2000)
    const afterClickProvider = await provSel.inputValue()
    check('F5. 点 provider 行后与 form.provider 联动（下拉值变成被点的那行）',
      afterClickProvider === 'gemini' && afterClickProvider !== beforeClickProvider,
      beforeClickProvider + ' → ' + afterClickProvider)
    const pressed = await page.locator('[data-agm-provider="gemini"]').first().getAttribute('aria-pressed')
    const onClass = await page.locator('[data-agm-provider="gemini"]').first().getAttribute('class')
    check('F5. 被点的行有选中态（aria-pressed=true + --on 样式）',
      pressed === 'true' && /--on/.test(onClass || ''), 'aria-pressed=' + pressed)
    check('F5. 点 provider 行会切到「独立模型」并清空旧 model',
      (await page.locator('[data-agm-mode="module"] input').first().isChecked()) &&
        (await page.locator('[data-agm-model-select]').count() > 0
          ? (await page.locator('[data-agm-model-select]').inputValue()) === ''
          : true))
  }

  // ---------- F6. 生效：受控 AI 分析 → ai_calls.jsonl ----------
  const callsBefore = existsSync(AI_CALLS_JSONL())
    ? readFileSync(AI_CALLS_JSONL(), 'utf8').split('\n').filter(Boolean) : []
  const scan = await fetch(FIX + API + '/autogroup/scan', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ agent: 'work', ai: true, limit: 1 })
  }).then((r) => r.json()).catch((e) => ({ ok: false, message: String(e) }))
  console.log('scan = ' + JSON.stringify(scan).slice(0, 300))
  let job = null
  const jobId = scan && scan.data && (scan.data.job_id || scan.data.jobId)
  for (let i = 0; i < 200 && jobId; i += 1) {
    await sleep(300)
    const r = await fetch(FIX + API + '/autogroup/jobs/' + jobId).then((x) => x.json()).catch(() => null)
    const j = r && r.data
    if (j && (j.status === 'done' || j.status === 'error' || j.status === 'failed')) { job = j; break }
  }
  const callsAfter = existsSync(AI_CALLS_JSONL())
    ? readFileSync(AI_CALLS_JSONL(), 'utf8').split('\n').filter(Boolean) : []
  const fresh = callsAfter.slice(callsBefore.length).map((l) => { try { return JSON.parse(l) } catch (e) { return null } }).filter(Boolean)
  console.log('新增审计行 = ' + JSON.stringify(fresh).slice(0, 600))
  const mine = fresh.filter((r) => r.agent === 'work')
  check('F6. 受控分析新增了审计行（ai_calls.jsonl 增长）', fresh.length > 0,
    'before=' + callsBefore.length + ' after=' + callsAfter.length + ' job=' + JSON.stringify(job).slice(0, 120))
  check('F6. 新增行 agent=work', mine.length > 0, fresh.map((r) => r.agent).join(','))
  check('F6. 新增行 provider/model 与所选一致（balance/deepseek-v4-flash）',
    mine.length > 0 && mine.every((r) => r.provider === 'balance' && r.model === 'deepseek-v4-flash'),
    JSON.stringify(mine.map((r) => r.provider + '/' + r.model)))

  // ---------- 页面零 console error ----------
  const realErrors = errors.filter((e) => !/favicon/.test(e))
  check('F7. 全程零 console error / 零 4xx-5xx', realErrors.length === 0, realErrors.slice(0, 3).join(' | '))

  console.log('\n' + (failures.length ? 'MODEL-FIX-PROBE-FAIL' : 'MODEL-FIX-PROBE-OK') +
    '  PASS=' + pass + ' FAIL=' + failures.length)
  if (failures.length) failures.forEach((f) => console.log('  FAILED: ' + f))
  await browser.close(); site.close(); fixture.kill()
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(3) })
