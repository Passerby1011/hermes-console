/**
 * hermes-console · 项目分组「AI 模型（provider/model）」面板 UI 验收（t_3c2817b1 验收项③）。
 *
 * 断言：
 *   K. 面板有配置对象选择器（global 段 + 各 Agent），有来源层四态文案与实际生效值
 *   L. 两级联动：选 provider → 模型清单出现；**换 provider 后旧 model 被清空**
 *   M. 两个 Agent 各设不同 provider/model，保存后回读互不覆盖（盘上按 Agent 映射）
 *   O. 保存提示里写明「已回读盘上真值」，且界面显示的生效值 = 后端 resolved 值
 *   P. 密钥只显示 key_env + 存在性，页面上不出现任何密钥取值
 *   Q. 900 / 1280 两档无横向溢出
 *
 * 真产物 dist + 真后端 fixture（隔离 home）+ 真浏览器。
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
const FIXTURE_PORT = Number(process.env.HC_MD_FIXTURE_PORT || 9381)
const SITE_PORT = Number(process.env.HC_MD_SITE_PORT || 9382)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'autogroup-model-ui')

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) { pass += 1; console.log('  PASS ' + name + (extra ? '  — ' + extra : '')) }
  else { failures.push(name + (extra ? '  — ' + extra : '')); console.log('  FAIL ' + name + (extra ? '  — ' + extra : '')) }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const WORK = mkdtempSync(join(tmpdir(), 'hc-md-ui-'))
const SANDBOX = join(WORK, 'home')
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
  check('K. 打开「AI 模型」抽屉，面板渲染 [data-agm-panel]', opened)
  if (!opened) {
    await page.screenshot({ path: join(SHOTS, 'md-00-no-panel.png'), fullPage: true })
    console.log('调试 body =', (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 500))
    await browser.close(); site.close(); fixture.kill(); process.exit(2)
  }

  const targetSel = page.locator('[data-agm-target-select]')
  // t_11b7edbb：面板默认 target 从「global 段」改成**具体 Agent**，所以打开时要多打一次
  // 「按 Agent 读配置」（global 段那次不读 profile config.yaml）。首帧请求叠加页面其它请求后
  // 会超过原来写死的 1.5s ⇒ 「来源层」还停在「读取中…」。这里改成等 loading 真消失，
  // 别再用固定 sleep 赌时序。
  for (let i = 0; i < 120; i += 1) {
    if (!(await page.locator('[data-agm-loading]').count())) break
    await sleep(100)
  }
  await sleep(300)
  const targetOpts = await targetSel.locator('option').evaluateAll((els) => els.map((e) => e.value))
  check('K. 配置对象选择器含 global 段 + 多个 Agent', targetOpts.length >= 2 && targetOpts.indexOf('__global__') >= 0,
    targetOpts.join(','))
  const agentOpts = targetOpts.filter((v) => v !== '__global__')
  check('K. 至少两个真实 Agent 可选（按 Agent 独立的前提）', agentOpts.length >= 2, agentOpts.join(','))

  const panelText = (await page.locator('[data-agm-panel]').innerText()).replace(/\s+/g, ' ')
  check('K. 面板写明配置目录 auto-group/ai_model.json', /auto-group\/ai_model\.json/.test(panelText), '')
  check('K. 面板有「来源层」与「实际生效」两行', /来源层/.test(panelText) && /实际生效/.test(panelText), '')
  const layerCount = await page.locator('[data-agm-layer]').count()
  const layerText = layerCount ? (await page.locator('[data-agm-layer]').first().innerText()).replace(/\s+/g, ' ') : ''
  check('K. 来源层有具体取值（四态之一，不是空白/未知）',
    /该 Agent 独立|跟随全局|跟随 global 段|模块默认/.test(layerText), layerText.slice(0, 120))
  const effectiveKv = await page.locator('[data-agm-kv="effective"]').count()
    ? (await page.locator('[data-agm-kv="effective"]').first().innerText()).replace(/\s+/g, ' ')
    : ''
  check('K. 「实际生效」显示 provider / model', /\/|—/.test(effectiveKv) && effectiveKv.length > 4, effectiveKv)
  // 抽屉正文是内部滚动容器：截图前把面板滚到顶，否则「配置对象 / 来源层 / 实际生效」会被裁掉
  await page.evaluate(() => {
    const p = document.querySelector('[data-agm-panel]')
    if (p) p.scrollIntoView({ block: 'start' })
    document.querySelectorAll('.hc-agg2__drawer-body, .hc-agg2__drawer').forEach((el) => { el.scrollTop = 0 })
  })
  await sleep(500)
  await page.screenshot({ path: join(SHOTS, 'md-00-panel-top-1280.png'), fullPage: false })

  // ---- P. 密钥脱敏（先等 provider 目录加载完：面板刚挂载时它还在请求中）
  const provRows = page.locator('[data-agm-provider]')
  for (let i = 0; i < 100; i += 1) {
    if (await provRows.count()) break
    await sleep(100)
  }
  const provNames = await provRows.evaluateAll((els) => els.map((e) => e.getAttribute('data-agm-provider')))
  check('P. provider 行渲染出来了', provNames.length > 0, provNames.join(','))
  const keyChips = await page.locator('[data-agm-provider] .hc-chip').allInnerTexts()
  const keyChipText = keyChips.map((t) => t.trim()).filter((t) => /已设置|未设置|key_env/.test(t))
  check('P. 每个 provider 的密钥只显示「变量名 · 已设置/未设置」', keyChipText.length > 0,
    keyChipText.slice(0, 5).join(' | '))
  const pageHtml = await page.content()
  // 真 home config.yaml 里的密钥取值长这样：sk-… / AIza… / 长十六进制
  const leakPatterns = [/sk-[A-Za-z0-9]{16,}/, /AIza[0-9A-Za-z\-_]{20,}/, /nvapi-[A-Za-z0-9]{20,}/]
  const leaks = leakPatterns.filter((re) => re.test(pageHtml)).map(String)
  check('P. 页面上没有出现任何密钥取值（只允许变量名 + 存在性）', leaks.length === 0, leaks.join(' | '))

  // ---- L. 两级联动
  async function pickAgent(agentId) {
    await targetSel.selectOption(agentId)
    await sleep(1500)
  }
  const A = agentOpts[0]
  const B = agentOpts[1]
  await pickAgent(A)
  const modeRadio = page.locator('[data-agm-mode="module"] input')
  if (await modeRadio.count()) { await modeRadio.first().check(); await sleep(500) }
  const provSel = page.locator('[data-agm-provider-select]')
  check('L. 独立模式下出现 provider 选择器 [data-agm-provider-select]', (await provSel.count()) > 0)
  const provOptions = await provSel.locator('option').evaluateAll((els) => els.map((e) => e.value).filter((v) => v))
  check('L. provider 选择器有多个可选项', provOptions.length >= 2, provOptions.join(','))
  const hintBefore = await page.locator('[data-agm-models-hint="no-provider"]').count()
  check('L. 未选 provider 时提示「先选 provider」（两级联动第一级）', hintBefore > 0)

  // 选 provider1 → 拉模型清单
  const p1 = provOptions[0]
  await provSel.selectOption(p1)
  await sleep(1800)
  const modelsInfo1 = await page.evaluate(() => {
    const w = document.querySelector('[data-agm-models]')
    const sel = document.querySelector('[data-agm-model-select]')
    const inp = document.querySelector('[data-agm-model-input]')
    return {
      count: w ? Number(w.getAttribute('data-agm-models')) : -1,
      source: w ? w.getAttribute('data-agm-models-source') : '',
      options: sel ? Array.from(sel.options).map((o) => o.value).filter((v) => v) : [],
      freeInput: !!inp
    }
  })
  check('L. 选 provider=' + p1 + ' 后模型清单出现（或给出可手填的输入框）',
    modelsInfo1.options.length > 0 || modelsInfo1.freeInput, JSON.stringify(modelsInfo1).slice(0, 160))
  if (modelsInfo1.options.length) {
    await page.locator('[data-agm-model-select]').selectOption(modelsInfo1.options[0])
    await sleep(400)
  } else {
    await page.locator('[data-agm-model-input]').fill('probe-model-a')
    await sleep(400)
  }
  const chosenModelA = modelsInfo1.options.length ? modelsInfo1.options[0] : 'probe-model-a'
  const modelValueAfterPick = modelsInfo1.options.length
    ? await page.locator('[data-agm-model-select]').inputValue()
    : await page.locator('[data-agm-model-input]').inputValue()
  check('L. 选中模型后表单里有值（第二级）', modelValueAfterPick === chosenModelA, modelValueAfterPick)

  // 换 provider → 旧 model 必须清空
  const p2 = provOptions.filter((v) => v !== p1)[0]
  await provSel.selectOption(p2)
  await sleep(1800)
  const afterSwitch = await page.evaluate(() => {
    const sel = document.querySelector('[data-agm-model-select]')
    const inp = document.querySelector('[data-agm-model-input]')
    const w = document.querySelector('[data-agm-models]')
    return {
      value: sel ? sel.value : (inp ? inp.value : '<<none>>'),
      options: sel ? Array.from(sel.options).map((o) => o.value).filter((v) => v) : [],
      count: w ? Number(w.getAttribute('data-agm-models')) : -1
    }
  })
  check('L. 换 provider（' + p1 + '→' + p2 + '）后旧 model 被清空', afterSwitch.value === '',
    JSON.stringify(afterSwitch).slice(0, 160))
  check('L. 换 provider 后模型清单内容确实变化（不是同一份缓存）',
    JSON.stringify(afterSwitch.options) !== JSON.stringify(modelsInfo1.options) ||
      afterSwitch.count !== modelsInfo1.options.length,
    p1 + '=' + JSON.stringify(modelsInfo1.options.slice(0, 3)) + ' → ' + p2 + '=' +
      JSON.stringify(afterSwitch.options.slice(0, 3)))
  const saveDisabled = await page.locator('[data-agm-save]').first().isDisabled()
  check('L. 清空 model 后「保存」按钮禁用（不许半写 provider-only）', saveDisabled === true)

  // ---- M. 给 A、B 各存一组不同 provider/model
  function pickModelFor(options) {
    return options[0]
  }
  async function saveAgent(agentId, provider, model) {
    await pickAgent(agentId)
    const mr = page.locator('[data-agm-mode="module"] input')
    if (await mr.count()) { await mr.first().check(); await sleep(500) }
    const ps = page.locator('[data-agm-provider-select]')
    await ps.selectOption(provider)
    await sleep(1800)
    const has = await page.locator('[data-agm-model-select]').count()
    if (has) { await page.locator('[data-agm-model-select]').selectOption(model) }
    else { await page.locator('[data-agm-model-input]').fill(model) }
    await sleep(400)
    await page.locator('[data-agm-save]').first().click()
    for (let i = 0; i < 100; i += 1) {
      if (await page.locator('[data-agm-save-ok]').count()) break
      if (await page.locator('text=保存失败').count()) break
      await sleep(100)
    }
    const ok = await page.locator('[data-agm-save-ok]').count() > 0
    const txt = ok ? (await page.locator('[data-agm-save-ok]').first().innerText()).replace(/\s+/g, ' ') : ''
    return { ok, txt }
  }

  // A 用 balance 系；B 用另一个 provider（夹具里各 profile 都有 providers 段）
  const optsA = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/ai/model/options?agent=' + A)).json()
  const provListA = (optsA.data && optsA.data.providers || []).map((r) => r.provider)
  const optsB = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/ai/model/options?agent=' + B)).json()
  const provListB = (optsB.data && optsB.data.providers || []).map((r) => r.provider)
  const pickA = provListA[0]
  const pickB = provListB.filter((p) => p !== pickA)[0] || provListB[0]

  async function modelsOf(agent, provider) {
    const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/ai/model/options?agent=' +
      encodeURIComponent(agent) + '&provider=' + encodeURIComponent(provider))
    const j = await r.json()
    return (j.data && j.data.models) || []
  }
  const mA = (await modelsOf(A, pickA))[0]
  const mB = (await modelsOf(B, pickB))[0]
  check('M. 两个 Agent 都能拿到 provider + 模型清单', !!pickA && !!pickB && !!mA && !!mB,
    A + '=' + pickA + '/' + mA + ' · ' + B + '=' + pickB + '/' + mB)

  const saveA = await saveAgent(A, pickA, mA)
  check('M. Agent ' + A + ' 保存成功并显示回读', saveA.ok && /已保存并回读/.test(saveA.txt), saveA.txt.slice(0, 150))
  await page.screenshot({ path: join(SHOTS, 'md-01-saved-' + A + '.png'), fullPage: false })

  const saveB = await saveAgent(B, pickB, mB)
  check('M. Agent ' + B + ' 保存成功并显示回读', saveB.ok && /已保存并回读/.test(saveB.txt), saveB.txt.slice(0, 150))
  await page.screenshot({ path: join(SHOTS, 'md-02-saved-' + B + '.png'), fullPage: false })

  // 从后端读，而不是信界面 state
  const readA = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/ai/model?agent=' + A)).json()
  const readB = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/autogroup/ai/model?agent=' + B)).json()
  const declA = ((readA.data || {}).agent_declaration || {}).group || {}
  const declB = ((readB.data || {}).agent_declaration || {}).group || {}
  check('M. 回读 A 的 provider/model 正确', declA.provider === pickA && declA.model === mA,
    declA.provider + '/' + declA.model)
  check('M. 回读 B 的 provider/model 正确', declB.provider === pickB && declB.model === mB,
    declB.provider + '/' + declB.model)
  check('M. 存 B 没有覆盖 A（两个 Agent 各自独立）',
    declA.provider === pickA && declB.provider === pickB && pickA !== pickB,
    'A=' + declA.provider + '/' + declA.model + ' · B=' + declB.provider + '/' + declB.model)
  const agentsOnDisk = Object.keys(((readA.data || {}).config || {}).agents || {})
  check('M. 盘上是按 Agent 映射（schema 2，agents 段有两条）',
    agentsOnDisk.length >= 2 && agentsOnDisk.indexOf(A) >= 0 && agentsOnDisk.indexOf(B) >= 0,
    agentsOnDisk.join(','))

  // ---- O. 界面显示的生效值 == 后端 resolved
  await pickAgent(A)
  const layerA = (await page.locator('[data-agm-layer]').first().innerText()).replace(/\s+/g, ' ')
  check('O. A 的来源层显示「该 Agent 独立」', /该 Agent 独立/.test(layerA), layerA.slice(0, 80))
  const effA = (await page.locator('[data-agm-kv="effective"]').first().innerText()).replace(/\s+/g, ' ')
  const resolvedA = (readA.data || {}).resolved || {}
  check('O. 界面生效值 == 后端 resolved（' + resolvedA.provider + '/' + resolvedA.model + '）',
    effA.indexOf(resolvedA.provider) >= 0 && effA.indexOf(resolvedA.model) >= 0, effA)

  const callsBlock = page.locator('[data-agm-calls]')
  const callsN = (await callsBlock.count()) ? await callsBlock.first().getAttribute('data-agm-calls') : null
  check('O. 面板显示「最近真实调用」（审计 ai_calls.jsonl）区块', callsN !== null, 'calls=' + callsN)

  // ---- Q. 布局
  for (const width of [1280, 900]) {
    await page.setViewportSize({ width, height: 900 })
    await sleep(700)
    const m = await page.evaluate(() => ({
      docScroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth
    }))
    check('Q. ' + width + 'px 无横向溢出（模型面板）', m.docScroll <= m.client + 1, JSON.stringify(m))
    await page.screenshot({ path: join(SHOTS, 'md-03-layout-' + width + '.png'), fullPage: true })
  }

  const bad = errors.filter((e) => !/favicon/.test(e))
  check('R. 全程零 console error / 4xx-5xx 响应', bad.length === 0, bad.slice(0, 6).join(' | '))

  await browser.close()
  site.close()
  fixture.kill()
  console.log('\nMODEL-UI: ' + pass + ' PASS / ' + failures.length + ' FAIL')
  for (const f of failures) console.log('  FAILED: ' + f)
  writeFileSync(join(SHOTS, 'md-result.txt'),
    'MODEL-UI: ' + pass + ' PASS / ' + failures.length + ' FAIL\n\n' +
    failures.map((f) => 'FAILED: ' + f).join('\n'), 'utf8')
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(3) })
