/**
 * hermes-console · 模型下拉接上 live=1 验收探针（t_286c4cc0）—— **真后端 / 真产物 / 真浏览器**。
 *
 * 与 probe-model-picker-ui.mjs 的区别：那份用沙盒夹具（不出网、没有真 provider）。
 * 这份打**真 serve（127.0.0.1:9119）+ 真 home + 真上游**：
 *   1. 用 .env 里的 dashboard basic-auth 向真 serve 走 `/auth/password-login` 拿真会话 cookie；
 *   2. 本脚本起的静态站把 `/api/plugins/hermes-console/*` 原样反代到真 serve（带真 cookie），
 *      所以下拉里的模型清单就是「供应商真实返回的那份」；
 *   3. 加载 `dashboard/dist/index.js`（真产物）原样，真 Chromium。
 *
 * 断言（每条都来自页面真实渲染 / 真实请求 URL）：
 *   A. 两处调用都带 live=1：Picker（辅助槽位）与 candidates（model scope 候选）
 *   B. balance：下拉里出现 live 独有的模型（deepseek-flash / doubao-seed-2.0-mini），
 *      live_count >= 37，且**不是只有 config 的那 10 条**；逐条标了来源（data-model-source）
 *   C. 切 gemini：模型数变（live_count = 44），且**旧 model 被清空**
 *   D. 点「刷新」：请求体带 refresh=1
 *   E. 选 anthropic：下拉里显示 http_401 明确错误（不是空下拉、不是静默只剩配置项）
 *   F. 三处产物 sha256 全等 + 零 console error / pageerror
 *
 * 跑法：
 *   node scripts/probe-models-live-picker.mjs --label after --expect-ok
 *   node scripts/probe-models-live-picker.mjs --label before        （只记事实，不判红）
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { createHash } from 'node:crypto'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const HERMES_HOME = process.env.HERMES_HOME_DIR || 'D:/hermes-data/hermes'
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const LIVE_HOST = '127.0.0.1'
const LIVE_PORT = Number(process.env.HC_LIVE_BACKEND_PORT || 9119)
const SITE_PORT = Number(process.env.HC_LIVE_SITE_PORT || 9297)
const API = '/api/plugins/hermes-console'

function argOf(name, fallback) {
  const i = process.argv.indexOf(name)
  return i > -1 ? process.argv[i + 1] : fallback
}
const LABEL = argOf('--label', 'live')
const EXPECT_OK = process.argv.includes('--expect-ok')
const AGENT = String(argOf('--agent', 'code-worker'))
const SHOTS = resolve(argOf('--shots', join(PLUGIN, 'scripts', '_probe_out', 'models-live-picker-' + LABEL)))
const OUT_JSON = resolve(argOf('--out', join(SHOTS, 'report.json')))

let pass = 0
const failures = []
const facts = {}
function check(name, ok, extra) {
  if (ok) {
    pass += 1
    console.log('  PASS ' + name + (extra ? '  — ' + extra : ''))
  } else {
    failures.push(name + (extra ? '  — ' + extra : ''))
    console.log('  FAIL ' + name + (extra ? '  — ' + extra : ''))
  }
}
function fact(name, value) {
  facts[name] = value
  console.log('  FACT ' + name + ' = ' + JSON.stringify(value))
}
const assertOrFact = (name, ok, extra) => (EXPECT_OK ? check(name, ok, extra) : fact(name, ok))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** 从真实响应体里取「并集 / 实时 / 配置」三个集合，供与 DOM 数对账。 */
function bodyFor(provider) {
  for (let i = apiBodies.length - 1; i >= 0; i -= 1) {
    const item = apiBodies[i]
    if (!item.body) continue
    if (String(item.body.provider || '') !== provider) continue
    return item.body
  }
  return null
}
/** 后端并集里「只在 config.yaml」的那些（供应商这次没返回）。 */
function configOnlyOf(body) {
  if (!body) return []
  const entries = Array.isArray(body.model_entries) ? body.model_entries : []
  return entries.filter((e) => String((e && e.source) || '') === 'config').map((e) => String(e.name))
}

// ------------------------------------------------------------------ .env 凭据
function readDotEnv() {
  const path = join(HERMES_HOME, '.env')
  const out = {}
  if (!existsSync(path)) return out
  for (const raw of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const i = line.indexOf('=')
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, '')
  }
  return out
}

async function realLogin() {
  const env = readDotEnv()
  const username = env.HERMES_DASHBOARD_BASIC_AUTH_USERNAME || ''
  const password = env.HERMES_DASHBOARD_BASIC_AUTH_PASSWORD || ''
  if (!username || !password) throw new Error('读不到 .env 里的 dashboard basic-auth 凭据')
  const res = await fetch(`http://${LIVE_HOST}:${LIVE_PORT}/auth/password-login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'basic', username, password, next: '/' })
  })
  const text = await res.text()
  if (!res.ok) throw new Error('登录失败 ' + res.status + ': ' + text.slice(0, 200))
  const raw = res.headers.getSetCookie ? res.headers.getSetCookie() : []
  const jar = {}
  for (const item of raw) {
    const pair = String(item).split(';', 1)[0]
    const i = pair.indexOf('=')
    if (i > 0) jar[pair.slice(0, i).trim()] = pair.slice(i + 1).trim()
  }
  if (!Object.keys(jar).length) throw new Error('登录成功但没有拿到会话 cookie')
  return { cookie: Object.keys(jar).map((k) => k + '=' + jar[k]).join('; '), names: Object.keys(jar) }
}

// --------------------------------------------------------------- 临时工作区
const WORK = mkdtempSync(join(tmpdir(), 'hc-mlp-live-'))
mkdirSync(SHOTS, { recursive: true })
try {
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立 junction：' + e.message)
}
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

if (!existsSync(join(DIST, 'index.js'))) throw new Error('找不到产物：' + DIST)
copyFileSync(join(DIST, 'index.js'), join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0] || ''
  : ''

// ctx.rest 与宿主 electron/main.ts::fetchJson 同语义：非 2xx reject、3xx reject、空 body → null。
const HARNESS = `
import rtDefault from 'react/jsx-runtime'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

const entryMod = await import('./entry.js')
const plugin = entryMod.default
const injectConsoleStyles = entryMod.injectConsoleStyles

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
    let res
    try {
      res = await fetch('${API}' + path, init)
    } catch (e) {
      requests.push({ method, path, status: 0, error: String((e && e.message) || e) })
      throw e
    }
    const text = await res.text()
    requests.push({ method, path, status: res.status, bytes: text.length })
    if (res.status >= 300) throw new Error(res.status + ': ' + text)
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
createRoot(document.getElementById('root')).render(createElement(pages[0].render))
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

// ------------------------------------------------ 静态站 + 真后端反代（带真 cookie）
const netLog = []
/** `/models/models` 的真实响应体（探针要区分 live_models / 并集 / DOM 三个数）。 */
const apiBodies = []
function startSite(cookie) {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · 模型下拉 live 验收</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head>
<body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script>
</body></html>`

  const server = createServer((req, res) => {
    const safe = (fn) => { try { if (!res.writableEnded && !res.destroyed) fn() } catch (e) {} }
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const headers = Object.assign({}, req.headers, { cookie })
      delete headers.host
      const upstream = httpRequest(
        { host: LIVE_HOST, port: LIVE_PORT, path: url, method: req.method, headers },
        (up) => {
          const chunks = []
          up.on('data', (c) => chunks.push(c))
          up.on('end', () => {
            const body = Buffer.concat(chunks)
            netLog.push({ method: req.method, url, status: up.statusCode || 502, bytes: body.length })
            if (url.indexOf('/models/models') > -1 && (up.statusCode || 502) === 200) {
              try {
                apiBodies.push({ url, body: JSON.parse(body.toString('utf8')) })
              } catch (e) {
                apiBodies.push({ url, parseError: String((e && e.message) || e) })
              }
            }
            safe(() => { res.writeHead(up.statusCode || 502, up.headers); res.end(body) })
          })
        }
      )
      upstream.on('error', (e) => {
        netLog.push({ method: req.method, url, status: 502, error: String(e.message) })
        safe(() => { res.writeHead(502, { 'content-type': 'text/plain' }); res.end('upstream error: ' + e.message) })
      })
      req.pipe(upstream)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      safe(() => { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(html) })
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
    safe(() => { res.writeHead(404, { 'content-type': 'text/plain' }); res.end('not found: ' + url) })
  })
  return new Promise((r) => server.listen(SITE_PORT, '127.0.0.1', () => r(server)))
}

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex')
}

// ---------------------------------------------------------------------- 主流程
const consoleErrors = []
const pageErrors = []
const report = { label: LABEL, agent: AGENT, backend: `http://${LIVE_HOST}:${LIVE_PORT}`, artifacts: {}, requests: [], modelScope: {}, picker: {}, consoleErrors, pageErrors }
let site = null
let browser = null

try {
  await runEsbuild([join(WORK, 'harness.js'), '--bundle', '--format=esm', '--platform=browser',
    '--target=chrome110', '--jsx=automatic', '--loader:.css=text', '--legal-comments=none', '--charset=utf8',
    '--outfile=' + join(WORK, 'harness.bundle.js')])

  const auth = await realLogin()
  console.log('[env] 真后端 ' + report.backend + ' · cookie=' + auth.names.join('/') + ' · agent=' + AGENT)
  site = await startSite(auth.cookie)
  browser = await chromium.launch({ channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined })

  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 1 })
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))
  page.on('request', (req) => {
    const url = req.url()
    if (url.indexOf('/models/models') === -1) return
    report.requests.push(url.split('/api/plugins/hermes-console')[1] || url)
  })

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 30000 })
  await page.click('.hc-nav__item:has-text("模型")')
  await page.waitForSelector('[data-models-agent-row]', { timeout: 30000 })
  await sleep(1500)

  // ---- A/B. 辅助槽位的 Picker：balance 的实时清单 --------------------------
  await page.click(`[data-models-row-edit="${AGENT}"]`)
  await page.waitForSelector('[data-models-editor-scope="auxiliary"]', { timeout: 30000 })
  await page.click('[data-models-editor-scope="auxiliary"]')
  await page.waitForSelector('[data-pmp="aux-approval"]', { timeout: 30000 })
  await sleep(2500)

  const providerSel = page.locator('select[data-pmp-provider="aux-approval"]')
  const modelSel = page.locator('select[data-pmp-model="aux-approval"]')
  const pickerBox = page.locator('[data-pmp="aux-approval"]')

  const snap = async () => {
    const root = await pickerBox.evaluate((el) => {
      const sel = el.querySelector('select[data-pmp-model]')
      const opts = sel ? Array.from(sel.querySelectorAll('option')) : []
      const cfgBox = el.querySelector('[data-pmp-config-only]')
      const cfgList = cfgBox ? Array.from(cfgBox.querySelectorAll('[data-pmp-config-only-item]')) : []
      return {
        live: el.getAttribute('data-pmp-live'),
        requested: el.getAttribute('data-pmp-live-requested'),
        ok: el.getAttribute('data-pmp-live-ok'),
        errorKind: el.getAttribute('data-pmp-live-error-kind'),
        liveCount: el.getAttribute('data-pmp-live-count'),
        configCount: el.getAttribute('data-pmp-config-count'),
        modelCount: el.getAttribute('data-pmp-model-count'),
        mainCount: el.getAttribute('data-pmp-main-count'),
        configOnlyCount: el.getAttribute('data-pmp-config-only-count'),
        modelSource: el.getAttribute('data-pmp-model-source'),
        provider: el.getAttribute('data-pmp-provider-value'),
        model: el.getAttribute('data-pmp-model-value'),
        hint: (el.querySelector('[data-pmp-hint]') || {}).textContent || '',
        err: (el.querySelector('[data-pmp-live-error]') || {}).textContent || '',
        // ★ DOM 实渲染数（去占位）：这才是验收口径，不是后端的 count。
        domOptions: opts.map((o) => ({
          value: o.value,
          text: o.textContent,
          source: o.getAttribute('data-model-source'),
          group: o.getAttribute('data-model-group')
        })),
        domOptionCount: opts.filter((o) => o.value !== '').length,
        domOptionAllCount: opts.length,
        // 配置独有项：必须在 <select> 之外
        cfgInsideSelect: cfgBox ? cfgBox.closest('select') !== null : false,
        cfgBlockPresent: Boolean(cfgBox),
        cfgCountAttr: cfgBox ? cfgBox.getAttribute('data-pmp-config-only-count') : '',
        cfgSummaryText: ((el.querySelector('[data-pmp-config-only-summary]') || {}).textContent || ''),
        cfgItems: cfgList.map((n) => ({
          name: n.getAttribute('data-pmp-config-only-item'),
          current: n.getAttribute('data-pmp-config-only-current'),
          text: (n.textContent || '').trim()
        })),
        cfgItemCount: cfgList.length,
        cfgCurrentItems: cfgList.filter((n) => n.getAttribute('data-pmp-config-only-current') === '1')
          .map((n) => n.getAttribute('data-pmp-config-only-item')),
        // 主下拉里出现 config-only 组/项的痕迹（必须是 0）
        optgroupsInside: sel ? sel.querySelectorAll('optgroup').length : 0
      }
    })
    const providerValue = await providerSel.inputValue().catch(() => '')
    const modelValue = await modelSel.inputValue().catch(() => '')
    return Object.assign(root, { providerValue, modelValue, options: root.domOptions })
  }

  const bal = await snap()
  const balBody = bodyFor('balance')
  const balCfgOnly = configOnlyOf(balBody)
  console.log('\n[balance] ' + JSON.stringify({
    provider: bal.provider, model: bal.model, live: bal.live, requested: bal.requested, ok: bal.ok,
    liveCount: bal.liveCount, configCount: bal.configCount, modelCount: bal.modelCount,
    mainCount: bal.mainCount, configOnlyCount: bal.configOnlyCount, modelSource: bal.modelSource,
    domOptionCount: bal.domOptionCount, domOptionAllCount: bal.domOptionAllCount
  }))
  fact('balance.threeNumbers', {
    live_models_length: balBody && Array.isArray(balBody.live_models) ? balBody.live_models.length : null,
    merged_union_length: balBody && Array.isArray(balBody.models) ? balBody.models.length : null,
    dom_rendered_options: bal.domOptionCount,
    dom_rendered_with_placeholder: bal.domOptionAllCount
  })
  fact('balance.snapshot', {
    liveCount: bal.liveCount, configCount: bal.configCount, modelCount: bal.modelCount,
    mainCount: bal.mainCount, configOnlyCount: bal.configOnlyCount, modelSource: bal.modelSource,
    hint: bal.hint, cfgSummary: bal.cfgSummaryText, cfgItems: bal.cfgItems.map((i) => i.name)
  })

  assertOrFact('A. Picker 走 live=1（data-pmp-live=1 且后端 live.requested=1）',
    bal.live === '1' && bal.requested === '1', 'live=' + bal.live + ' requested=' + bal.requested)
  assertOrFact('A. Picker 的实时拉取成功（live.ok=1）', bal.ok === '1', 'ok=' + bal.ok + ' kind=' + bal.errorKind)

  // ---- ★ 卡面收紧后的精确断言：DOM 实渲染数严格 37 --------------------
  check('B1. ★balance 主下拉 DOM option 数（去占位）严格 === 37', bal.domOptionCount === 37,
    'domOptionCount=' + bal.domOptionCount + ' 全部option=' + bal.domOptionAllCount +
    ' mainCount=' + bal.mainCount + ' modelCount=' + bal.modelCount +
    ' liveCount=' + bal.liveCount)
  check('B2. ★主下拉里没有任何 data-model-source="config" 的 option',
    bal.options.filter((o) => o.source === 'config').length === 0,
    JSON.stringify(bal.options.filter((o) => o.source === 'config').map((o) => o.value)))
  check('B2b. ★主下拉里没有 <optgroup>（配置独有项不许塞进 select）', bal.optgroupsInside === 0,
    'optgroups=' + bal.optgroupsInside)
  check('B3. ★主下拉 option 全是实时来源（source ∈ {live, config+live}）',
    bal.options.filter((o) => o.value !== '').every((o) => o.source === 'live' || o.source === 'config+live'),
    JSON.stringify(bal.options.filter((o) => o.value !== '').map((o) => o.source)
      .reduce((a, s) => { a[s] = (a[s] || 0) + 1; return a }, {})))
  check('B4. ★配置独有项计数 data-pmp-config-only-count === 2', bal.cfgCountAttr === '2',
    'cfgCountAttr=' + bal.cfgCountAttr + ' items=' + bal.cfgItemCount)
  check('B5. ★配置独有项不在 <select> 内（cfgInsideSelect=false）', bal.cfgInsideSelect === false,
    'closest(select)=' + bal.cfgInsideSelect)
  check('B6. ★下拉外提示文案（「另有 2 项已配置但供应商未返回」）',
    /另有\s*2\s*项已配置但供应商未返回/.test(bal.cfgSummaryText),
    String(bal.cfgSummaryText).slice(0, 120))
  check('B7. ★下拉外提示列出的就是后端 config-only 那 2 条',
    bal.cfgItems.map((i) => i.name).sort().join(',') === balCfgOnly.slice().sort().join(','),
    'dom=' + JSON.stringify(bal.cfgItems.map((i) => i.name)) + ' api=' + JSON.stringify(balCfgOnly))
  check('B8. ★三个数可区分且自洽（union > live，DOM == live 且 = 37）',
    Boolean(balBody) &&
      Array.isArray(balBody.live_models) && balBody.live_models.length === 37 &&
      Array.isArray(balBody.models) && balBody.models.length === 39 &&
      bal.domOptionCount === 37,
    'live=' + (balBody && balBody.live_models && balBody.live_models.length) +
    ' union=' + (balBody && balBody.models && balBody.models.length) +
    ' dom=' + bal.domOptionCount)
  const balNames = bal.options.map((o) => o.value)
  check('B. 下拉含只在 live_models 里的 deepseek-flash', balNames.indexOf('deepseek-flash') >= 0, 'n=' + balNames.length)
  check('B. 下拉含只在 live_models 里的 doubao-seed-2.0-mini', balNames.indexOf('doubao-seed-2.0-mini') >= 0, '')
  check('B. ★下拉不含配置独有项 doubao-seed-2.0-lite / doubao-seed-2.1-turbo',
    balNames.indexOf('doubao-seed-2.0-lite') === -1 && balNames.indexOf('doubao-seed-2.1-turbo') === -1,
    JSON.stringify(balCfgOnly))
  const liveOnly = bal.options.filter((o) => o.source === 'live').map((o) => o.value)
  const cfgOnlyInSelect = bal.options.filter((o) => o.source === 'config').map((o) => o.value)
  const both = bal.options.filter((o) => o.source === 'config+live').map((o) => o.value)
  fact('balance.sourceSplit', { live: liveOnly.length, config_in_select: cfgOnlyInSelect.length, config_live: both.length, configOnlyNames: cfgOnlyInSelect })
  check('B. ★逐条标了来源（live 与 config+live 两种都出现）',
    liveOnly.length > 0 && both.length > 0, 'live=' + liveOnly.length + ' config+live=' + both.length)
  check('B. 下拉项文本带来源徽标（如「（实时）」）',
    bal.options.some((o) => /（实时）/.test(o.text || '')), JSON.stringify(bal.options.slice(0, 3).map((o) => o.text)))
  // 逐条明细：37 条的 value + source 全部落盘（验收项 6 的回读报告）
  fact('balance.optionDetail', bal.options.map((o) => ({ value: o.value, source: o.source })))
  await page.screenshot({ path: join(SHOTS, '01-balance-live.png'), fullPage: false })

  // ---- A. 抓请求体 -------------------------------------------------------
  const pickerReq = report.requests.filter((u) => /provider=balance/.test(u))
  fact('balance.requests', pickerReq)
  check('A. 真实请求 URL 带 live=1（平衡器）',
    pickerReq.some((u) => /[?&]live=1(&|$)/.test(u)), pickerReq[pickerReq.length - 1] || '（无）')

  // ---- C. 切 gemini：模型数变 + 旧 model 清空 -----------------------------
  const modelBefore = bal.modelValue
  await providerSel.selectOption('gemini')
  await sleep(4000)
  const gem = await snap()
  const gemBody = bodyFor('gemini')
  const gemCfgOnly = configOnlyOf(gemBody)
  console.log('\n[gemini] ' + JSON.stringify({
    modelBefore, modelAfter: gem.modelValue, liveCount: gem.liveCount, configCount: gem.configCount,
    modelCount: gem.modelCount, mainCount: gem.mainCount, configOnlyCount: gem.configOnlyCount,
    ok: gem.ok, domOptionCount: gem.domOptionCount
  }))
  fact('gemini.threeNumbers', {
    live_models_length: gemBody && Array.isArray(gemBody.live_models) ? gemBody.live_models.length : null,
    merged_union_length: gemBody && Array.isArray(gemBody.models) ? gemBody.models.length : null,
    dom_rendered_options: gem.domOptionCount,
    dom_rendered_with_placeholder: gem.domOptionAllCount
  })
  fact('gemini.snapshot', {
    modelBefore, modelAfter: gem.modelValue, liveCount: gem.liveCount, modelCount: gem.modelCount,
    mainCount: gem.mainCount, configOnlyCount: gem.configOnlyCount, ok: gem.ok,
    hint: gem.hint, cfgSummary: gem.cfgSummaryText
  })
  check('C. 切到 gemini 后 live_count = 44', Number(gem.liveCount) === 44,
    'liveCount=' + gem.liveCount + ' union=' + gem.modelCount)
  check('C. ★gemini 主下拉 DOM option 数（去占位）严格 === 44', gem.domOptionCount === 44,
    'domOptionCount=' + gem.domOptionCount + ' 全部option=' + gem.domOptionAllCount +
    ' mainCount=' + gem.mainCount + ' modelCount=' + gem.modelCount + ' liveCount=' + gem.liveCount)
  check('C. ★gemini 主下拉无 config 来源项、无 optgroup',
    gem.options.filter((o) => o.source === 'config').length === 0 && gem.optgroupsInside === 0,
    'config=' + gem.options.filter((o) => o.source === 'config').length + ' optgroups=' + gem.optgroupsInside)
  check('C. ★gemini 配置独有项计数 === 19', gem.cfgCountAttr === '19',
    'cfgCountAttr=' + gem.cfgCountAttr + ' items=' + gem.cfgItemCount)
  check('C. ★gemini 配置独有项不在 <select> 内', gem.cfgInsideSelect === false, 'closest(select)=' + gem.cfgInsideSelect)
  check('C. ★gemini 下拉外提示文案（另有 19 项）',
    /另有\s*19\s*项已配置但供应商未返回/.test(gem.cfgSummaryText),
    String(gem.cfgSummaryText).slice(0, 120))
  check('C. ★gemini 提示列出的就是后端那 19 条 config-only',
    gem.cfgItems.map((i) => i.name).sort().join(',') === gemCfgOnly.slice().sort().join(','),
    'dom_n=' + gem.cfgItems.length + ' api_n=' + gemCfgOnly.length)
  check('C. ★切换 provider 后旧 model 被清空',
    (modelBefore || '') !== '' && gem.modelValue === '', 'before=' + JSON.stringify(modelBefore) + ' after=' + JSON.stringify(gem.modelValue))
  check('C. gemini 的请求带 live=1',
    report.requests.some((u) => /provider=gemini/.test(u) && /[?&]live=1(&|$)/.test(u)),
    report.requests.filter((u) => /provider=gemini/.test(u)).slice(-1)[0] || '（无）')
  await page.screenshot({ path: join(SHOTS, '02-gemini-switched.png'), fullPage: false })

  // ---- D. 点刷新 → refresh=1 --------------------------------------------
  const beforeRefresh = report.requests.length
  await page.click('[data-pmp-refresh="aux-approval"]')
  await sleep(3500)
  const refreshReqs = report.requests.slice(beforeRefresh)
  fact('refresh.requests', refreshReqs)
  check('D. ★点刷新后请求体带 refresh=1',
    refreshReqs.some((u) => /[?&]refresh=1(&|$)/.test(u)), refreshReqs[refreshReqs.length - 1] || '（无新请求）')
  await page.screenshot({ path: join(SHOTS, '03-refreshed.png'), fullPage: false })

  // ---- E. 选 anthropic → http_401 明确错误 ------------------------------
  await providerSel.selectOption('anthropic')
  await sleep(4500)
  const ant = await snap()
  console.log('\n[anthropic] ' + JSON.stringify({
    ok: ant.ok, errorKind: ant.errorKind, modelCount: ant.modelCount, configCount: ant.configCount,
    hint: ant.hint, err: String(ant.err).replace(/\s+/g, ' ').slice(0, 220)
  }))
  fact('anthropic.snapshot', { ok: ant.ok, errorKind: ant.errorKind, modelCount: ant.modelCount, hint: ant.hint, err: String(ant.err).replace(/\s+/g, ' ').slice(0, 300) })
  check('E. ★anthropic 实时拉取失败被明确标注（data-pmp-live-ok=0）', ant.ok === '0', 'ok=' + ant.ok)
  check('E. ★错误类型是 http_401', ant.errorKind === 'http_401', 'kind=' + ant.errorKind)
  check('E. ★下拉里显示明确错误块（不是空白提示）',
    /http_401/.test(ant.err) && /实时拉取失败/.test(ant.err), String(ant.err).replace(/\s+/g, ' ').slice(0, 160))
  check('E. 错误块带上游原文（provider 名 + 状态码）',
    /provider anthropic/.test(ant.err) && /HTTP 401/.test(ant.err), '')
  check('E. 状态行也点明失败原因', /实时拉取失败/.test(ant.hint), String(ant.hint).slice(0, 120))
  check('E. ★实时失败时主下拉为空（去占位 0 条，不回退成配置列表）', ant.domOptionCount === 0,
    'domOptionCount=' + ant.domOptionCount + ' 全部option=' + ant.domOptionAllCount +
    ' 选项=' + JSON.stringify(ant.options.map((o) => o.value)))
  check('E. ★实时失败时下拉外也没有「已配置但未返回」可选项（配置也是空）',
    ant.cfgItemCount === 0, 'cfgItems=' + JSON.stringify(ant.cfgItems.map((i) => i.name)))
  await page.screenshot({ path: join(SHOTS, '04-anthropic-401.png'), fullPage: false })

  // ---- A. 第二处调用：model scope 的 candidatesQuery --------------------
  await page.click('[data-models-editor-scope="model"]')
  await sleep(800)
  await page.selectOption('[data-models-editor-provider]', 'balance')
  await sleep(4000)
  const cand = await page.evaluate(() => {
    const box = document.querySelector('[data-models-candidates-source]')
    const main = document.querySelector('[data-models-candidates-main]')
    const cfg = document.querySelector('[data-models-candidates-config-only]')
    const picks = Array.from(document.querySelectorAll('[data-models-candidates-main] [data-models-editor-model-pick]'))
    // 注意：主列表容器**内**的 chip 才是主候选；配置独有那批已经换成只读 span，不会落在 picks 里。
    return {
      source: box ? box.getAttribute('data-models-candidates-source') : '',
      count: box ? box.getAttribute('data-models-candidates-count') : '',
      live: box ? box.getAttribute('data-models-candidates-live') : '',
      liveCount: box ? box.getAttribute('data-models-candidates-live-count') : '',
      configCount: box ? box.getAttribute('data-models-candidates-config-count') : '',
      errKind: box ? box.getAttribute('data-models-candidates-live-error-kind') : '',
      mainCount: box ? box.getAttribute('data-models-candidates-main-count') : '',
      cfgCount: box ? box.getAttribute('data-models-candidates-config-only-count') : '',
      // ★ DOM 实渲染的主候选 chip 数（= 验收口径）
      domMainChips: picks.length,
      domMainChipsInsideMainBox: main
        ? main.querySelectorAll('[data-models-editor-model-pick]').length
        : 0,
      // 主候选里有没有 config 来源的 chip（必须 0）
      domMainConfigChips: picks.filter((n) => n.getAttribute('data-models-editor-model-pick-source') === 'config').length,
      chips: picks.slice(0, 6).map((n) => ({
        name: n.getAttribute('data-models-editor-model-pick'),
        src: n.getAttribute('data-models-editor-model-pick-source'),
        group: n.getAttribute('data-models-editor-model-pick-group'),
        text: (n.textContent || '').trim()
      })),
      sourceStats: picks.reduce((acc, n) => {
        const s = n.getAttribute('data-models-editor-model-pick-source') || '（无）'
        acc[s] = (acc[s] || 0) + 1
        return acc
      }, {}),
      // 配置独有项：必须是只读 span、且不在主候选容器里
      cfgReadonlyItems: cfg ? Array.from(cfg.querySelectorAll('[data-models-candidates-config-only-item]'))
        .map((n) => n.getAttribute('data-models-candidates-config-only-item')) : [],
      cfgItemCount: cfg ? cfg.querySelectorAll('[data-models-candidates-config-only-item]').length : 0,
      cfgInsideMainBox: cfg ? (cfg.closest('[data-models-candidates-main]') !== null) : false,
      cfgHasChipButtons: cfg ? cfg.querySelectorAll('[data-models-editor-model-pick]').length : 0,
      cfgText: cfg ? (cfg.textContent || '').replace(/\s+/g, ' ').slice(0, 160) : '',
      mainBoxPresent: Boolean(main),
      totalPicksAnywhere: Array.from(document.querySelectorAll('[data-models-editor-model-pick]')).length
    }
  })
  console.log('\n[candidates] ' + JSON.stringify(cand))
  fact('candidates.snapshot', cand)
  const candBody = bodyFor('balance')
  fact('candidates.threeNumbers', {
    live_models_length: candBody && Array.isArray(candBody.live_models) ? candBody.live_models.length : null,
    merged_union_length: candBody && Array.isArray(candBody.models) ? candBody.models.length : null,
    dom_main_chips: cand.domMainChips
  })
  const candReq = report.requests.filter((u) => /models\/models\?provider=balance/.test(u))
  fact('candidates.requests', candReq)
  assertOrFact('A. candidatesQuery 也带 live=1', cand.live === '1', 'live=' + cand.live)
  assertOrFact('A. candidates 真实请求 URL 带 live=1',
    candReq.some((u) => /[?&]live=1(&|$)/.test(u)), candReq[candReq.length - 1] || '（无）')
  check('A. ★candidates 主候选 DOM chip 数严格 === 37（与 Picker 同口径）',
    cand.domMainChips === 37 && cand.domMainChipsInsideMainBox === 37,
    'domMainChips=' + cand.domMainChips + ' insideMain=' + cand.domMainChipsInsideMainBox +
    ' mainCount=' + cand.mainCount + ' count=' + cand.count + ' liveCount=' + cand.liveCount)
  check('A. ★candidates 主候选里没有任何 config 来源 chip', cand.domMainConfigChips === 0,
    JSON.stringify(cand.sourceStats))
  check('A. ★candidates 配置独有项计数 === 2（下拉外/主列表外）', cand.cfgCount === '2' && cand.cfgItemCount === 2,
    'cfgCount=' + cand.cfgCount + ' items=' + cand.cfgItemCount)
  check('A. ★candidates 配置独有项不在主候选容器里、且不是可点 chip',
    cand.cfgInsideMainBox === false && cand.cfgHasChipButtons === 0,
    'insideMain=' + cand.cfgInsideMainBox + ' chipButtons=' + cand.cfgHasChipButtons)
  check('A. candidates 主候选逐条带来源（live + config+live 两种都出现）',
    cand.sourceStats.live > 0 && cand.sourceStats['config+live'] > 0,
    JSON.stringify(cand.sourceStats))
  await page.screenshot({ path: join(SHOTS, '05-candidates-live.png'), fullPage: false })

  // ---- G. 当前值落在「配置独有」批次里：下拉外可见、主下拉条数不变 ------
  await page.click('[data-models-editor-scope="auxiliary"]')
  await page.waitForSelector('[data-pmp="aux-approval"]', { timeout: 30000 })
  await sleep(1200)
  await providerSel.selectOption('balance')
  await sleep(3500)
  const beforeCur = await snap()
  // 用「高级：手填模型名」把当前值构造成一条**配置独有**项（只改本地草稿，不写盘）。
  await page.click('[data-pmp-advanced="aux-approval"] summary')
  await sleep(300)
  await page.fill('[data-pmp-advanced-input="aux-approval"]', 'doubao-seed-2.0-lite')
  await sleep(200)
  await page.click('[data-pmp-advanced-apply="aux-approval"]')
  await sleep(1500)
  const cur = await snap()
  console.log('\n[config-only-current] ' + JSON.stringify({
    beforeModel: beforeCur.modelValue, model: cur.modelValue,
    domBefore: beforeCur.domOptionCount, domAfter: cur.domOptionCount,
    cfgCurrentItems: cur.cfgCurrentItems, cfgSummary: cur.cfgSummaryText
  }))
  fact('configOnlyCurrent.snapshot', {
    beforeModel: beforeCur.modelValue,
    modelValue: cur.modelValue,
    domBefore: beforeCur.domOptionCount,
    domAfter: cur.domOptionCount,
    cfgCurrentItems: cur.cfgCurrentItems,
    cfgSummary: cur.cfgSummaryText,
    cfgItemCount: cur.cfgItemCount
  })
  check('G. 构造成功：当前 model 落在配置独有项里',
    cur.model === 'doubao-seed-2.0-lite' && cur.cfgCurrentItems.indexOf('doubao-seed-2.0-lite') > -1,
    'data-pmp-model-value=' + JSON.stringify(cur.model) + ' selectValue=' + JSON.stringify(cur.modelValue) +
    ' cfgCurrent=' + JSON.stringify(cur.cfgCurrentItems))
  check('G. ★主下拉条数不因当前值变化（仍是 37，无新增 option）',
    cur.domOptionCount === 37 && beforeCur.domOptionCount === 37,
    'before=' + beforeCur.domOptionCount + ' after=' + cur.domOptionCount)
  check('G. ★当前值在下拉外提示里明确标出「当前值」',
    /当前值/.test(cur.cfgSummaryText) && /doubao-seed-2\.0-lite/.test(cur.cfgSummaryText),
    String(cur.cfgSummaryText).slice(0, 160))
  check('G. ★占位项也把当前值说出来（原生 select 无对应 option 时不显示空白）',
    bal !== null && cur.options.some((o) => o.value === '' && /doubao-seed-2\.0-lite/.test(o.text || '')),
    JSON.stringify(cur.options.filter((o) => o.value === '').map((o) => o.text)))
  check('G. ★当前值那条在下拉外清单里带 data-pmp-config-only-current=1 且不是 option',
    cur.cfgItems.filter((i) => i.current === '1').length === 1 &&
      cur.options.every((o) => o.value !== 'doubao-seed-2.0-lite'),
    JSON.stringify(cur.cfgItems.filter((i) => i.current === '1')) +
    ' inOptions=' + JSON.stringify(cur.options.filter((o) => o.value === 'doubao-seed-2.0-lite')))
  await page.screenshot({ path: join(SHOTS, '06-config-only-current.png'), fullPage: false })

  // ---- F. 三处产物 sha256 + 零 error ----------------------------------
  const distJs = join(DIST, 'index.js')
  const desktopJs = join(PLUGIN, 'desktop', 'plugin.js')
  const gateJs = 'D:/hermes-data/hermes/desktop-plugins/hermes-console/plugin.js'
  report.artifacts = {
    dist: { path: distJs, sha256: sha256(distJs), bytes: readFileSync(distJs).length },
    desktop: { path: desktopJs, sha256: sha256(desktopJs), bytes: readFileSync(desktopJs).length },
    gate: existsSync(gateJs) ? { path: gateJs, sha256: sha256(gateJs), bytes: readFileSync(gateJs).length } : null
  }
  fact('artifacts', report.artifacts)
  check('F. 三处产物 sha256 全等',
    Boolean(report.artifacts.gate) &&
      report.artifacts.dist.sha256 === report.artifacts.desktop.sha256 &&
      report.artifacts.desktop.sha256 === report.artifacts.gate.sha256,
    (report.artifacts.gate ? report.artifacts.gate.sha256.slice(0, 16) : 'missing'))
  check('F. 零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('F. 零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))

  report.picker = { balance: bal, gemini: gem, anthropic: ant, configOnlyCurrent: cur }
  report.modelScope = cand
  report.pass = pass
  report.failures = failures
  writeFileSync(OUT_JSON, JSON.stringify(report, null, 2), 'utf8')

  console.log('\n=== 汇总 ===')
  console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
  failures.forEach((f) => console.log('  FAIL ' + f))
  console.log(failures.length ? 'MODELS-LIVE-PICKER-FAILED' : 'MODELS-LIVE-PICKER-OK')
  console.log('截图：' + SHOTS)
  console.log('报告：' + OUT_JSON)
  await browser.close()
  site.close()
  process.exit(failures.length ? 1 : 0)
} catch (e) {
  console.error('探针异常：' + (e && e.stack ? e.stack : e))
  try { writeFileSync(OUT_JSON, JSON.stringify(report, null, 2), 'utf8') } catch (x) {}
  try { if (browser) await browser.close() } catch (x) {}
  try { if (site) site.close() } catch (x) {}
  process.exit(2)
}
