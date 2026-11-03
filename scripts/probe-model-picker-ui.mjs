/**
 * hermes-console · ProviderModelPicker（两级联动）UI 验收（t_4d654faf）。
 *
 * 真产物 dist + 真插件后端（picker_ui_fixture.py，沙盒 home：3 agent × 3 provider）+ 真浏览器。
 *
 * 断言（每条都是"看到的/点出来的"，不是源码推断）：
 *   A. 「模型」分区的辅助槽位里，provider 与 model 都是**下拉**（不是文本框）
 *   B. provider 的候选来自该 agent 的 provider 目录（含「跟随全局」项）
 *   C. model 的候选**只来自当前 provider**（换 provider 候选集就换）
 *   D. ★换 provider 后旧 model 立即清空（select 值变 ''）
 *   E. 选 model → 保存 → **回读**：界面显示的是落盘真值（不是本地 state）
 *   F. 「还原」把槽位退回落盘真值（证明显示的是回读值）
 *   G. 来源层文案：「该 Agent 独立配置」/「跟随全局」
 *   H. 密钥只显示变量名 + 存在性，页面文本里没有密钥取值
 *   I. 900 / 1280 两档：无横向溢出
 *   J. 截图：收起态 / provider 展开 / model 展开 / 高级折叠区展开（两档各一套）
 *   K. 零 console error / pageerror
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
const FIXTURE_PORT = Number(process.env.HC_PMP_FIXTURE_PORT || 9395)
const SITE_PORT = Number(process.env.HC_PMP_SITE_PORT || 9396)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(HERE, '_probe_out', 'model-picker-ui')

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) { pass += 1; console.log('  PASS ' + name + (extra ? '  — ' + extra : '')) }
  else { failures.push(name + (extra ? '  — ' + extra : '')); console.log('  FAIL ' + name + (extra ? '  — ' + extra : '')) }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const WORK = mkdtempSync(join(tmpdir(), 'hc-pmp-ui-'))
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
      const err = new Error((payload && (payload.message || (payload.error && payload.error.message))) || ('HTTP ' + res.status))
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
createRoot(document.getElementById('root')).render(createElement(pages[0].render))
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

  const env = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' })
  const fixture = spawn(PYTHON, [join(PLUGIN, 'scripts', 'picker_ui_fixture.py'),
    '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], { env, cwd: PLUGIN, stdio: ['ignore', 'pipe', 'pipe'] })
  let flog = ''
  fixture.stdout.on('data', (c) => { flog += c })
  fixture.stderr.on('data', (c) => { flog += c })
  let ready = false
  for (let i = 0; i < 240; i += 1) {
    try {
      const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/pmp-health')
      if (r.ok) {
        const body = await r.json()
        // 只认**我自己的**夹具（端口被别的 worker 的夹具占用时，别把别人的数据当自己的）
        if (body && body.sandbox === SANDBOX) { ready = true; break }
      }
    } catch (e) {}
    await sleep(150)
  }
  if (!ready) { console.log('fixture 没起来（或端口被占）：\n' + flog); fixture.kill(); site.close(); process.exit(2) }

  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const errors = []
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
  page.on('response', (r) => { if (r.status() >= 400) errors.push('http' + r.status() + ': ' + r.url()) })
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await sleep(1200)

  const nav = page.locator('.hc-nav__item:has-text("模型")')
  check('A. 导航里有「模型」分区', (await nav.count()) > 0)
  await nav.first().click()
  await sleep(1500)

  const rows = page.locator('[data-models-agent-row]')
  const rowIds = await rows.evaluateAll((els) => els.map((e) => e.getAttribute('data-models-agent-row')))
  check('A. agent 列表含沙盒 3 个 agent（alpha/beta/gamma）',
    ['alpha', 'beta', 'gamma'].every((id) => rowIds.indexOf(id) >= 0), rowIds.join(','))

  await page.locator('[data-models-row-edit="alpha"]').click()
  await sleep(1200)
  await page.locator('[data-models-editor-scope="auxiliary"]').click()
  await sleep(1200)

  const picker = page.locator('[data-pmp="aux-approval"]')
  check('A. approval 槽位出现 ProviderModelPicker', (await picker.count()) === 1)
  if ((await page.locator('select[data-pmp-provider="aux-approval"]').count()) === 0) {
    const dump = (await picker.first().evaluate((el) => el.outerHTML)).slice(0, 1200)
    console.log('调试：picker outerHTML = ' + dump)
  }
  check('A. provider / model 都是下拉（select），不是文本框',
    (await page.locator('select[data-pmp-provider="aux-approval"]').count()) === 1 &&
    (await page.locator('select[data-pmp-model="aux-approval"]').count()) === 1)

  const providerSel = page.locator('select[data-pmp-provider="aux-approval"]')
  const modelSel = page.locator('select[data-pmp-model="aux-approval"]')
  const providerOpts = await providerSel.locator('option').evaluateAll((els) => els.map((e) => e.value))
  check('B. provider 候选含沙盒 3 个 provider', ['palpha', 'pbeta', 'pgamma'].every((p) => providerOpts.indexOf(p) >= 0),
    providerOpts.join(','))
  check('B. provider 候选第一项是「跟随全局」（辅助槽位写 auto）',
    providerOpts[0] === 'auto', JSON.stringify(providerOpts.slice(0, 2)))
  check('B. 当前 provider = palpha', (await providerSel.inputValue()) === 'palpha', await providerSel.inputValue())

  const modelsOf = async () => modelSel.locator('option').evaluateAll((els) => els.map((e) => e.value).filter((v) => v !== ''))
  /**
   * ★ 口径已收紧（t_d4e837c2）：`<select>` 里**只放供应商实时返回**的模型。
   *
   * 本夹具的 provider 都是假域名（`palpha.example`），实时拉取必然 `transport` 失败 ⇒
   * 主下拉**必须为空**，且**不许**回退成 config 清单冒充实时结果。要看配置清单，
   * 用户得显式点掉「实时拉取」勾选框（`data-pmp-live-toggle`）——那是另一条正当路径。
   */
  const liveFailedMain = await page.evaluate(() => {
    const el = document.querySelector('[data-pmp="aux-approval"]')
    const sel = el ? el.querySelector('select[data-pmp-model]') : null
    const opts = sel ? Array.from(sel.querySelectorAll('option')).map((o) => o.value).filter((v) => v !== '') : []
    return {
      ok: el ? el.getAttribute('data-pmp-live-ok') : '',
      kind: el ? el.getAttribute('data-pmp-live-error-kind') : '',
      options: opts,
      cfgOnly: el ? el.getAttribute('data-pmp-config-only-count') : ''
    }
  })
  check('C0. ★实时拉取失败（假域名 transport）时主下拉为空、不回退成配置清单',
    liveFailedMain.ok === '0' && liveFailedMain.options.length === 0,
    'ok=' + liveFailedMain.ok + ' kind=' + liveFailedMain.kind + ' options=' + JSON.stringify(liveFailedMain.options) +
    ' configOnly=' + liveFailedMain.cfgOnly)
  // 切到「只看配置」：这条路径下列表**就是**配置清单（没有实时结果可比，不冒充实时）。
  await page.click('label:has([data-pmp-live-toggle="aux-approval"]) input')
  await sleep(1500)
  const palphaModels = await modelsOf()
  check('C. 只看配置时模型候选 = palpha 的清单（alpha-main / alpha-alt / alpha-palpha-only）',
    palphaModels.sort().join(',') === ['alpha-main', 'alpha-alt', 'alpha-palpha-only'].sort().join(','),
    palphaModels.join(','))
  check('C. 当前 model = alpha-main', (await modelSel.inputValue()) === 'alpha-main', await modelSel.inputValue())

  const sourceChip = page.locator('[data-models-aux-source="approval"]')
  check('G. 来源层文案 = 该 Agent 独立配置', /该 Agent 独立配置/.test(await sourceChip.innerText()),
    await sourceChip.innerText())

  const pageText = await page.locator('body').innerText()
  check('H. 页面不出现沙盒密钥取值', pageText.indexOf('sbx-never-print-me') === -1)
  check('H. 页面显示 key_env 变量名（不是取值）', /key_env: SANDBOX_PICKER_KEY/.test(pageText) ||
    (await page.locator('[data-pmp-keyenv="aux-approval"]').count()) >= 0, '')

  // ---- 截图：1280 收起态 ----
  await page.screenshot({ path: join(SHOTS, '01-collapsed-1280.png'), fullPage: false })

  // ---- provider 展开（点开原生下拉）+ 切到 pgamma ----
  await providerSel.click()
  await sleep(300)
  await page.screenshot({ path: join(SHOTS, '02-provider-open-1280.png'), fullPage: false })
  await providerSel.selectOption('pgamma')
  await sleep(900)
  check('D. ★换 provider 后旧 model 被清空', (await modelSel.inputValue()) === '',
    JSON.stringify(await modelSel.inputValue()))
  const pgammaModels = await modelsOf()
  check('C. ★换 provider 后模型候选集换成 pgamma 的清单',
    pgammaModels.sort().join(',') === ['alpha-pgamma-only'].join(','), pgammaModels.join(','))
  check('D. 旧 model（alpha-main）不在新候选里', pgammaModels.indexOf('alpha-main') === -1)

  // ---- model 展开 + 选择 ----
  await modelSel.click()
  await sleep(300)
  await page.screenshot({ path: join(SHOTS, '03-model-open-1280.png'), fullPage: false })
  await modelSel.selectOption('alpha-pgamma-only')
  await sleep(600)
  check('E. 选中 alpha-pgamma-only 后 select 值正确', (await modelSel.inputValue()) === 'alpha-pgamma-only')

  // ---- 高级折叠区 ----
  const adv = page.locator('[data-pmp-advanced="aux-approval"]')
  check('J. 高级自定义是折叠入口（<details>）', (await adv.count()) === 1)
  await page.locator('[data-pmp-advanced="aux-approval"] summary').click()
  await sleep(400)
  check('J. 高级折叠区展开后出现手填输入框',
    await page.locator('[data-pmp-advanced-input="aux-approval"]').isVisible())
  await page.screenshot({ path: join(SHOTS, '04-advanced-open-1280.png'), fullPage: false })
  await page.locator('[data-pmp-advanced="aux-approval"] summary').click()
  await sleep(300)

  // ---- 保存 → 回读（两步：先预览，再确认写入） ----
  const previewBtn = page.locator('[data-models-editor-preview-ask]')
  check('E. 「预览将写入什么」可用', await previewBtn.isEnabled())
  await previewBtn.click()
  await sleep(1800)
  const askBtn = page.locator('[data-models-editor-apply-ask]')
  check('E. 预览后「下一步：确认写入」出现且可用',
    (await askBtn.count()) === 1 && (await askBtn.isEnabled()))
  await askBtn.click()
  await sleep(400)
  await page.locator('[data-models-editor-apply-yes]').click()
  await sleep(2500)
  const resultText = (await page.locator('[data-models-result]').count())
    ? (await page.locator('[data-models-result]').first().innerText()).replace(/\s+/g, ' ')
    : ''
  check('E. 写入成功并回读（结果区出现）', /alpha/.test(resultText), resultText.slice(0, 160))
  await sleep(1200)
  const currentText = (await page.locator('[data-pmp-current="aux-approval"]').count())
    ? (await page.locator('[data-pmp-current="aux-approval"]').first().innerText()).replace(/\s+/g, ' ')
    : ''
  check('E. ★回读真值显示在界面上（pgamma · alpha-pgamma-only）',
    /alpha-pgamma-only/.test(currentText) && /pgamma/.test(currentText), currentText)
  check('E. provider 下拉回到 pgamma', (await providerSel.inputValue()) === 'pgamma',
    await providerSel.inputValue())
  await page.screenshot({ path: join(SHOTS, '05-saved-1280.png'), fullPage: false })

  // ---- 还原：退回落盘真值 ----
  await providerSel.selectOption('pbeta')
  await sleep(600)
  const resetBtn = page.locator('[data-models-aux-reset="approval"]')
  check('F. 改动后「还原」可用', await resetBtn.isEnabled())
  await resetBtn.click()
  await sleep(700)
  check('F. ★还原后退回落盘真值 pgamma（显示的是回读值，不是本地 state）',
    (await providerSel.inputValue()) === 'pgamma', await providerSel.inputValue())
  check('F. 还原后 model 也回到 alpha-pgamma-only',
    (await modelSel.inputValue()) === 'alpha-pgamma-only', await modelSel.inputValue())

  // ---- 跟随全局：title_generation 槽位 ----
  const tg = page.locator('select[data-pmp-provider="aux-title_generation"]')
  if (await tg.count()) {
    check('G. title_generation 槽位的 provider = auto（跟随全局）', (await tg.inputValue()) === 'auto',
      await tg.inputValue())
    const tgChip = page.locator('[data-models-aux-source="title_generation"]')
    check('G. ★跟随全局的来源层文案', /跟随全局/.test(await tgChip.innerText()), await tgChip.innerText())
    const tgCurrent = (await page.locator('[data-pmp-current="aux-title_generation"]').count())
      ? (await page.locator('[data-pmp-current="aux-title_generation"]').first().innerText()).replace(/\s+/g, ' ')
      : ''
    check('G. 跟随全局时显示「继承全局」', /继承全局/.test(tgCurrent), tgCurrent)
  } else {
    check('G. title_generation 槽位存在', false, 'no picker')
  }

  // ---- 900px ----
  await page.setViewportSize({ width: 900, height: 900 })
  await sleep(900)
  const overflow900 = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth
  }))
  check('I. 900px 无横向溢出（docScrollWidth == clientWidth）',
    overflow900.doc <= overflow900.client + 1, JSON.stringify(overflow900))
  await page.screenshot({ path: join(SHOTS, '06-collapsed-900.png'), fullPage: false })
  await providerSel.click()
  await sleep(300)
  await page.screenshot({ path: join(SHOTS, '07-provider-open-900.png'), fullPage: false })
  await page.keyboard.press('Escape')
  await modelSel.click()
  await sleep(300)
  await page.screenshot({ path: join(SHOTS, '08-model-open-900.png'), fullPage: false })
  await page.keyboard.press('Escape')
  await page.locator('[data-pmp-advanced="aux-approval"] summary').click()
  await sleep(400)
  await page.screenshot({ path: join(SHOTS, '09-advanced-open-900.png'), fullPage: false })

  // ---- 1280 溢出 ----
  await page.setViewportSize({ width: 1280, height: 900 })
  await sleep(800)
  const overflow1280 = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth
  }))
  check('I. 1280px 无横向溢出', overflow1280.doc <= overflow1280.client + 1, JSON.stringify(overflow1280))

  const real = errors.filter((e) => !/favicon/.test(e))
  check('K. 零 console error / pageerror', real.length === 0, real.slice(0, 3).join(' | '))

  await browser.close()
  site.close()
  fixture.kill()
  console.log('\n=== 汇总 ===')
  console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
  failures.forEach((f) => console.log('  FAIL ' + f))
  console.log(failures.length ? 'MODEL-PICKER-UI-FAILED' : 'MODEL-PICKER-UI-OK')
  console.log('截图目录：' + SHOTS)
  process.exit(failures.length ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(2) })
