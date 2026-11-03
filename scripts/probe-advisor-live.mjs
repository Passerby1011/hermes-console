/**
 * hermes-console · Advisor 真登录态 / 真后端 / 真产物 验收探针（live）
 *
 * 与 probe-advisor-ui.mjs 的区别：那份用**夹具**后端。这份打**真**后端：
 *   1. 用 .env 里的 dashboard basic-auth 凭据向真 serve（127.0.0.1:9119）走真
 *      `/auth/password-login`，拿到真会话 cookie —— 这就是「真登录态」；
 *   2. 本脚本起的静态站把 `/api/plugins/hermes-console/*` 原样反代到那个真 serve，
 *      并把真 cookie 带上，所以面板读到的是**真 home 的真数据**（不是沙盒）；
 *   3. 加载 `dashboard/dist/index.js`（或 --bundle 指定的产物）原样，真 Chromium。
 *
 * 覆盖：
 *   A. 读接口的网络取证（method/url/status/bytes/If-None-Match/ETag/Cache-Control）
 *   B. 304 行为：--inject-304 让反代对重复 GET 回 304（模拟中间缓存/条件请求），
 *      验证 rest.js 是否把 304 显式当成功（不再进 ErrorState）
 *   C. 「建议详情与策略设置」真实留白几何（Agent 下拉框 bottom → 当前读写行 top）
 *   D. 覆盖矩阵四列可操作性 + 允许注入两段式（--toggle 时才写）
 *   E. console error / pageerror / 横向溢出
 *
 * 跑法：
 *   node scripts/probe-advisor-live.mjs --label before --facts
 *   node scripts/probe-advisor-live.mjs --label after --expect-ok
 *   node scripts/probe-advisor-live.mjs --label after --expect-ok --toggle --inject-304
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
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
const SITE_PORT = Number(process.env.HC_LIVE_SITE_PORT || 9296)
const API = '/api/plugins/hermes-console'

function argOf(name, fallback) {
  const i = process.argv.indexOf(name)
  return i > -1 ? process.argv[i + 1] : fallback
}
const BUNDLE = resolve(argOf('--bundle', join(DIST, 'index.js')))
const LABEL = argOf('--label', 'live')
const EXPECT_OK = process.argv.includes('--expect-ok')
const FACTS = process.argv.includes('--facts') || !EXPECT_OK
const INJECT_304 = process.argv.includes('--inject-304')
const TOGGLE = process.argv.includes('--toggle')
const WIDTHS = String(argOf('--widths', '1350,900')).split(',').map((n) => Number(n.trim())).filter(Boolean)
const SHOTS = resolve(argOf('--shots', join(PLUGIN, 'scripts', '_probe_out', 'advisor-live-' + LABEL)))
const OUT_JSON = resolve(argOf('--out', join(SHOTS, 'live-report.json')))
const GAP_LIMIT = Number(argOf('--gap-limit', '80'))
const REFRESH_ROUNDS = Number(argOf('--refresh-rounds', '3'))

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
function fact(name, value, extra) {
  facts[name] = { value, extra: extra === undefined ? '' : String(extra) }
  console.log('  FACT ' + name + ' = ' + String(value) + (extra ? '  — ' + extra : ''))
}
const assertOrFact = (name, ok, extra) => (EXPECT_OK ? check(name, ok, extra) : fact(name, ok, extra))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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

/** 真 /auth/password-login → 真会话 cookie。 */
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

// ------------------------------------------------------------- 临时工作区 / harness
const WORK = mkdtempSync(join(tmpdir(), 'hc-adv-live-probe-'))
mkdirSync(SHOTS, { recursive: true })
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
let chromium
try {
  const { symlinkSync } = await import('node:fs')
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
} catch (e) {
  throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
}
;({ chromium } = requireFromWork('playwright'))

if (!existsSync(BUNDLE)) throw new Error('找不到产物：' + BUNDLE)
copyFileSync(BUNDLE, join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))

const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

// ctx.rest 与宿主 electron/main.ts::fetchJson 同语义：非 2xx reject、3xx reject、
// 空 body → null。**故意不做任何 304 兜底** —— 兜底应该在 lib/rest.js 里。
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
    const started = performance.now()
    let res
    try {
      res = await fetch('${API}' + path, init)
    } catch (e) {
      requests.push({ method, path, status: 0, error: String(e && e.message || e) })
      throw e
    }
    const text = await res.text()
    const hdrs = {}
    res.headers.forEach((v, k) => { hdrs[k] = v })
    requests.push({
      method, path, status: res.status, bytes: text.length,
      ms: Math.round(performance.now() - started),
      etag: hdrs.etag || '', cacheControl: hdrs['cache-control'] || '',
      contentType: hdrs['content-type'] || ''
    })
    // 宿主语义：>=400 reject（httpStatusError），3xx 同样 reject（htmlResponseError），
    // 空 body resolve(null)。304 是 3xx —— 在这里就是一条错误，rest.js 必须自己拦。
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
window.__hcStore = store
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
    child.stderr.on('data', (c) => {
      err += c
    })
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

// ------------------------------------------------------- 静态站 + 真后端反代
const netLog = []
const seenPaths = new Map()

function startSite(cookie) {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · Advisor（真登录态验收）</title>
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
      const path = url.split('?')[0]
      const count = (seenPaths.get(url) || 0) + 1
      seenPaths.set(url, count)
      // --inject-304：模拟「中间缓存 / 代理做了条件请求」。第一次正常放行，
      // 之后对同一 URL 的 GET 直接回 304（无 body）—— 这正是用户报的那个故障态。
      if (INJECT_304 && req.method === 'GET' && count > 1) {
        netLog.push({
          method: req.method, url, status: 304, bytes: 0, round: count,
          reqIfNoneMatch: req.headers['if-none-match'] || '',
          reqIfModifiedSince: req.headers['if-modified-since'] || '',
          respEtag: 'W/"probe-synthetic"', respCacheControl: 'no-cache',
          synthetic: true
        })
        safe(() => {
          res.writeHead(304, { ETag: 'W/"probe-synthetic"', 'Cache-Control': 'no-cache' })
          res.end()
        })
        return
      }
      const headers = Object.assign({}, req.headers, { cookie })
      delete headers.host
      const upstream = httpRequest(
        { host: LIVE_HOST, port: LIVE_PORT, path: url, method: req.method, headers },
        (up) => {
          const chunks = []
          up.on('data', (c) => chunks.push(c))
          up.on('end', () => {
            const body = Buffer.concat(chunks)
            netLog.push({
              method: req.method, url, status: up.statusCode || 502, bytes: body.length, round: count,
              reqIfNoneMatch: req.headers['if-none-match'] || '',
              reqIfModifiedSince: req.headers['if-modified-since'] || '',
              respEtag: up.headers.etag || '',
              respLastModified: up.headers['last-modified'] || '',
              respCacheControl: up.headers['cache-control'] || '',
              synthetic: false
            })
            safe(() => {
              res.writeHead(up.statusCode || 502, up.headers)
              res.end(body)
            })
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

// ---------------------------------------------------------------- 真 config 读取
function readConfigYaml(profile) {
  const home = profile === 'default' ? HERMES_HOME : join(HERMES_HOME, 'profiles', profile)
  const path = join(home, 'config.yaml')
  if (!existsSync(path)) return { path, text: '' }
  return { path, text: readFileSync(path, 'utf8') }
}

/** 从 config.yaml 文本里抓 plugins.entries.advisor.allow_gateway_injection 的字面值。 */
function injectionLiteral(text) {
  const lines = text.split(/\r?\n/)
  let inEntries = false
  let inAdvisor = false
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]
    if (/^\S/.test(line) || /^ {0,2}entries:\s*$/.test(line)) {
      if (/^ {0,2}entries:\s*$/.test(line)) inEntries = true
      else if (/^\S/.test(line)) { inEntries = false; inAdvisor = false }
      continue
    }
    if (!inEntries) continue
    if (/^ {4}advisor:\s*$/.test(line)) { inAdvisor = true; continue }
    if (inAdvisor && /^ {4}\S/.test(line)) { inAdvisor = false }
    if (inAdvisor) {
      const m = /allow_gateway_injection:\s*(.+?)\s*$/.exec(line)
      if (m) return m[1]
    }
  }
  return '（缺省 = 非 true）'
}

// ---------------------------------------------------------------------- 主流程
const consoleErrors = []
const pageErrors = []
let site = null
let browser = null
const report = { label: LABEL, bundle: BUNDLE, bundleSha256: '', widths: {}, net: [], geometry: {}, toggle: null, gapLimit: GAP_LIMIT, inject304: INJECT_304 }

try {
  const { createHash } = await import('node:crypto')
  report.bundleSha256 = createHash('sha256').update(readFileSync(BUNDLE)).digest('hex')
  console.log('[env] bundle :', BUNDLE, '(' + basename(BUNDLE) + ')')
  console.log('[env] sha256 :', report.bundleSha256)
  console.log('[env] 真后端 :', `http://${LIVE_HOST}:${LIVE_PORT}`, '· inject304 =', INJECT_304, '· toggle =', TOGGLE)

  const auth = await realLogin()
  console.log('[env] 真登录态 cookie :', auth.names.join(' / '))
  report.login = { ok: true, cookieNames: auth.names }

  await buildHarness()
  site = await startSite(auth.cookie)
  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })

  for (const width of WIDTHS) {
    console.log('\n===== 宽度 ' + width + ' =====')
    const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 1 })
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return
      const where = msg.location && msg.location() ? msg.location().url || '' : ''
      if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
      consoleErrors.push('[' + width + '] ' + msg.text() + (where ? ' @ ' + where : ''))
    })
    page.on('pageerror', (err) => pageErrors.push('[' + width + '] ' + String(err)))

    await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
    await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
    await page.click('.hc-nav__item:has-text("Advisor")')
    await page.waitForSelector('.hc-adv', { timeout: 20000 })
    await page.waitForSelector('[data-coverage-rows]', { timeout: 30000 })
    await sleep(900)

    // ---- 1. ErrorState 计数 ----
    const errorStates = await page.evaluate(() =>
      document.querySelectorAll('.hc-error, [data-state="error"], .hc-state--error').length)
    assertOrFact('★零 ErrorState（' + width + '）', errorStates === 0, 'errorStates=' + errorStates)

    // ---- 2. 卡片结构齐备（覆盖矩阵已从 8 列表格改成「摘要 + 卡片 + 展开区」） ----
    const cols = await page.evaluate(() => {
      const cards = Array.from(document.querySelectorAll('[data-coverage-profile]'))
      const first = cards[0]
      if (!first) return { found: false }
      return {
        found: true,
        profile: first.getAttribute('data-coverage-profile'),
        name: !!first.querySelector('.hc-adv__cov-name'),
        facts: first.querySelectorAll('.hc-adv__cov-fact').length,
        badge: !!first.querySelector('.hc-adv__cov-badge'),
        expand: !!first.querySelector('[data-cov-expand]'),
        primary: first.getAttribute('data-cov-primary'),
        table: document.querySelectorAll('.hc-adv__cov-table').length,
        rows: cards.length
      }
    })
    report.widths[width] = { cols }
    assertOrFact('★覆盖矩阵卡片结构齐备：名字/一行短描述/状态词/展开箭头（' + width + '）',
      cols.found && cols.name && cols.facts >= 1 && cols.badge && cols.expand, JSON.stringify(cols))
    assertOrFact('★废弃 8 列表格（' + width + '）', cols.table === 0, 'tables=' + cols.table)

    // ---- 2b. 展开后「允许注入」开关按钮完整可见（不被裁、不溢出卡片） ----
    // 卡片是折叠的：先点开第一张，量注入开关那颗按钮的几何。
    await page.click('[data-cov-expand="' + cols.profile + '"]')
    await page.waitForSelector('[data-cov-body="' + cols.profile + '"]', { timeout: 15000 })
    const injGeom = await page.evaluate((profile) => {
      const card = document.querySelector('[data-coverage-profile="' + profile + '"]')
      const sw = card && card.querySelector('[data-cov-switch="injection"]')
      const btn = sw && sw.querySelector('[data-cov-action]')
      if (!btn) return []
      const b = btn.getBoundingClientRect()
      const c = card.getBoundingClientRect()
      return [{
        profile,
        action: btn.getAttribute('data-cov-action'),
        btnH: Math.round(b.height), btnW: Math.round(b.width),
        clippedRight: Math.round(b.right - c.right),
        clippedBottom: Math.round(b.bottom - c.bottom),
        cardScrollX: card.scrollWidth - card.clientWidth
      }]
    }, cols.profile)
    report.widths[width].injectionGeom = injGeom
    const badInj = injGeom.filter((g) => g.btnH < 18 || g.btnW < 24
      || g.clippedRight > 1 || g.clippedBottom > 1 || g.cardScrollX > 1)
    assertOrFact('★★「允许注入」开关按钮在展开区完整可见（未被裁切，' + width + '）',
      injGeom.length > 0 && badInj.length === 0,
      'geom=' + JSON.stringify(injGeom) + ' bad=' + JSON.stringify(badInj.slice(0, 3)))
    await page.click('[data-cov-expand="' + cols.profile + '"]')
    await page.waitForSelector('[data-cov-body="' + cols.profile + '"]', { state: 'detached', timeout: 15000 })

    // ---- 3. 「建议详情与策略设置」真实留白几何 ----
    const geo = await page.evaluate(() => {
      const block = document.querySelector('[data-block="detail-settings"]')
      if (!block) return { found: false }
      const picker = block.querySelector('[data-filter="settings-profile"]')
      const pickerLabel = picker ? picker.closest('.hc-adv__filter') || picker : null
      const hint = block.querySelector('[data-settings-path]')
      if (!picker || !hint) return { found: false, why: 'picker/hint 缺失' }
      // 任务口径：「Agent 下拉框」= 那个 <select> 控件本身（不是包它的 label）。
      // 包壳 label 被 flex-basis 撑高时，控件到底下一条可见行之间的空白就是用户看到的留白。
      const cRect = picker.getBoundingClientRect()
      const pRect = pickerLabel.getBoundingClientRect()
      const hRect = hint.getBoundingClientRect()
      // 中间是否夹着「有高度但没内容」的占位块
      const blockRect = block.getBoundingClientRect()
      const between = []
      for (const el of block.querySelectorAll('*')) {
        const r = el.getBoundingClientRect()
        if (r.height <= 0 || r.width <= 0) continue
        if (r.top >= cRect.bottom - 0.5 && r.bottom <= hRect.top + 0.5) {
          between.push({
            cls: String(el.className || el.tagName).slice(0, 60),
            h: Math.round(r.height),
            text: String(el.textContent || '').trim().slice(0, 40)
          })
        }
      }
      // 整块内「相邻可见子元素之间」的最大垂直间隙：抓任何被撑开的容器
      const visible = Array.from(block.querySelectorAll('*'))
        .map((el) => ({ el, r: el.getBoundingClientRect() }))
        .filter((x) => x.r.height > 0 && x.r.width > 0)
      const gaps = []
      for (let i = 1; i < visible.length; i += 1) {
        const a = visible[i - 1]
        const b = visible[i]
        const gap = b.r.top - a.r.bottom
        if (gap > 40) {
          gaps.push({
            from: String(a.el.className || a.el.tagName).slice(0, 50),
            to: String(b.el.className || b.el.tagName).slice(0, 50),
            gap: Math.round(gap)
          })
        }
      }
      gaps.sort((x, y) => y.gap - x.gap)
      const titleEl = block.querySelector('.hc-adv__block-title')
      const titleRect = titleEl ? titleEl.getBoundingClientRect() : null
      const settings = block.querySelector('.hc-adv__settings')
      const kids = []
      if (settings) {
        for (const el of settings.children) {
          const r = el.getBoundingClientRect()
          kids.push({
            cls: String(el.className || el.tagName).slice(0, 44),
            top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height),
            text: String(el.textContent || '').trim().slice(0, 34)
          })
        }
      }
      const kidGaps = []
      for (let i = 1; i < kids.length; i += 1) {
        kidGaps.push({ from: kids[i - 1].cls, to: kids[i].cls, gap: kids[i].top - kids[i - 1].bottom })
      }
      return {
        found: true,
        pickerBottom: Math.round(pRect.bottom * 100) / 100,
        controlBottom: Math.round(cRect.bottom * 100) / 100,
        controlHeight: Math.round(cRect.height * 100) / 100,
        wrapperHeight: Math.round(pRect.height * 100) / 100,
        wrapperSlack: Math.round((pRect.height - cRect.height) * 100) / 100,
        hintTop: Math.round(hRect.top * 100) / 100,
        gap: Math.round((hRect.top - cRect.bottom) * 100) / 100,
        gapFromWrapper: Math.round((hRect.top - pRect.bottom) * 100) / 100,
        titleToPicker: titleRect ? Math.round((cRect.top - titleRect.bottom) * 100) / 100 : null,
        blockHeight: Math.round(blockRect.height),
        betweenCount: between.length,
        between: between.slice(0, 8),
        emptyBetween: between.filter((b) => !b.text && b.h > 4),
        bigGaps: gaps.slice(0, 6),
        maxBigGap: gaps.length ? gaps[0].gap : 0,
        settingsChildren: kids,
        settingsChildGaps: kidGaps
      }
    })
    report.widths[width].geometry = geo
    report.geometry[width] = geo
    console.log('        几何:', JSON.stringify(geo))
    assertOrFact('★★「建议详情」下拉框→当前读写行 间隙 ≤ ' + GAP_LIMIT + 'px（' + width + '）',
      geo.found && geo.gap <= GAP_LIMIT, 'gap=' + (geo.found ? geo.gap : 'n/a'))
    assertOrFact('★★间隙内无空占位块（' + width + '）',
      geo.found && (geo.emptyBetween || []).length === 0, JSON.stringify((geo.emptyBetween || []).slice(0, 3)))
    assertOrFact('★下拉框包壳没有被撑高（wrapperSlack ≤ 8px，' + width + '）',
      geo.found && (geo.wrapperSlack || 0) <= 8,
      'wrapperSlack=' + (geo.wrapperSlack || 0) + ' wrapperH=' + (geo.wrapperHeight || 0) + ' controlH=' + (geo.controlHeight || 0))
    const worstKidGap = Math.max(0, ...((geo.settingsChildGaps || []).map((g) => g.gap)))
    assertOrFact('★「策略设置」面板相邻子元素间隙无 >40px（' + width + '）',
      geo.found && worstKidGap <= 40,
      'worst=' + worstKidGap + ' ' + JSON.stringify((geo.settingsChildGaps || []).filter((g) => g.gap > 40)))

    // ---- 3b. 详情打开态再量一次（详情面板渲染出来后，设置区被推到哪里） ----
    const openDetail = await page.evaluate(() => {
      const btn = Array.from(document.querySelectorAll('button'))
        .find((b) => /^详情/.test(String(b.textContent || '').trim()))
      if (btn) { btn.click(); return true }
      return false
    })
    let geoOpen = { found: false, why: '没有详情按钮' }
    if (openDetail) {
      await sleep(1200)
      geoOpen = await page.evaluate(() => {
        const block = document.querySelector('[data-block="detail-settings"]')
        const picker = block && block.querySelector('[data-filter="settings-profile"]')
        const hint = block && block.querySelector('[data-settings-path]')
        if (!block || !picker || !hint) return { found: false, why: 'picker/hint 缺失' }
        const pl = picker.closest('.hc-adv__filter') || picker
        const pRect = pl.getBoundingClientRect()
        const hRect = hint.getBoundingClientRect()
        return {
          found: true,
          gap: Math.round((hRect.top - pRect.bottom) * 100) / 100,
          blockHeight: Math.round(block.getBoundingClientRect().height),
          detailOpen: !!block.querySelector('.hc-adv__detail-title')
        }
      })
      // 收起来，避免影响后面的刷新计数
      await page.evaluate(() => {
        const close = Array.from(document.querySelectorAll('button'))
          .find((b) => String(b.textContent || '').trim() === '收起')
        if (close) close.click()
      })
      await sleep(400)
    }
    report.widths[width].geometryOpen = geoOpen
    report.geometryOpen = report.geometryOpen || {}
    report.geometryOpen[width] = geoOpen
    console.log('        几何（详情打开）:', JSON.stringify(geoOpen))
    assertOrFact('★★详情打开时该间隙仍 ≤ ' + GAP_LIMIT + 'px（' + width + '）',
      geoOpen.found && geoOpen.gap <= GAP_LIMIT, 'gap=' + (geoOpen.found ? geoOpen.gap : 'n/a'))

    // ---- 4. 横向溢出 ----
    const overflow = await page.evaluate(() => {
      const root = document.querySelector('.hc-adv')
      const wide = []
      if (root) {
        const rr = root.getBoundingClientRect()
        for (const el of root.querySelectorAll('*')) {
          const r = el.getBoundingClientRect()
          if (r.width <= 0) continue
          // 固定定位的遮罩/浮层本来就覆盖整个视口，不算「内容横向溢出」
          if (getComputedStyle(el).position === 'fixed') continue
          if (r.right > rr.right + 1.5) {
            wide.push({ cls: String(el.className || el.tagName).slice(0, 50), right: Math.round(r.right), limit: Math.round(rr.right) })
          }
        }
      }
      return {
        docScroll: document.documentElement.scrollWidth,
        inner: window.innerWidth,
        paneScroll: root ? root.scrollWidth : 0,
        paneClient: root ? root.clientWidth : 0,
        wide: wide.slice(0, 6)
      }
    })
    assertOrFact('★无横向溢出（' + width + '）',
      overflow.docScroll <= overflow.inner + 1 && overflow.wide.length === 0
      && overflow.paneScroll <= overflow.paneClient + 1, JSON.stringify(overflow))

    // ---- 5. 连续刷新 3 次：每次数据完整 ----
    const rounds = []
    for (let i = 0; i < REFRESH_ROUNDS; i += 1) {
      await page.evaluate(() => {
        const btns = Array.from(document.querySelectorAll('button'))
        const refresh = btns.find((b) => /刷新/.test(String(b.textContent || '')))
        if (refresh) refresh.click()
      })
      await sleep(700)
      const state = await page.evaluate(() => ({
        errorStates: document.querySelectorAll('.hc-error, [data-state="error"], .hc-state--error').length,
        covRows: document.querySelectorAll('[data-coverage-profile]').length,
        hint: !!document.querySelector('[data-settings-path]'),
        picker: !!document.querySelector('[data-filter="settings-profile"]')
      }))
      rounds.push(state)
    }
    report.widths[width].refreshRounds = rounds
    assertOrFact('★★刷新 ' + REFRESH_ROUNDS + ' 次每次都完整可操作（' + width + '）',
      rounds.every((r) => r.errorStates === 0 && r.covRows > 0 && r.hint && r.picker),
      JSON.stringify(rounds))

    // ---- 4c. 逐行证明：已安装的 agent 收起态不出现「安装」按钮（真 home 真数据） ----
    const perCard = await page.evaluate(() => Array.from(document.querySelectorAll('[data-coverage-profile]')).map((c) => ({
      p: c.getAttribute('data-coverage-profile'),
      status: c.getAttribute('data-coverage-status'),
      primary: c.getAttribute('data-cov-primary'),
      install: Boolean(c.querySelector('[data-cov-action="install"]')),
      h: Math.round(c.getBoundingClientRect().height)
    })))
    report.widths[width].perCard = perCard
    const badInstall = perCard.filter((r) => r.status !== 'not_installed' && r.install)
    assertOrFact('★★已安装的 agent 收起态不出现「安装」按钮（逐行，' + width + '）',
      badInstall.length === 0, 'rows=' + JSON.stringify(perCard.map((r) => r.p + ':' + r.status + ':' + (r.primary || 'none') + (r.install ? ':INSTALL' : ''))))
    assertOrFact('★收起态单卡高度落在 88~120px（' + width + '）',
      perCard.every((r) => r.h >= 88 && r.h <= 120), 'heights=' + JSON.stringify(perCard.map((r) => r.h)))

    await page.screenshot({ path: join(SHOTS, 'advisor-live-' + width + '.png'), fullPage: true })
    // 覆盖矩阵专项截图：收起态整块 + 单卡展开态（真后端 / 真 home / 桌面端当前主题）
    try {
      const covBox = await page.$('[data-coverage-rows]')
      if (covBox) {
        await covBox.scrollIntoViewIfNeeded()
        await sleep(300)
        await covBox.screenshot({ path: join(SHOTS, 'coverage-collapsed-' + width + '.png') })
        const target = await page.evaluate(() => {
          const list = Array.from(document.querySelectorAll('[data-coverage-profile]'))
          const installed = list.find((c) => c.getAttribute('data-coverage-status') !== 'not_installed')
          return (installed || list[0]).getAttribute('data-coverage-profile')
        })
        if (target) {
          await page.click('[data-cov-expand="' + target + '"]')
          await page.waitForSelector('[data-cov-body="' + target + '"]', { timeout: 15000 })
          await sleep(400)
          await covBox.screenshot({ path: join(SHOTS, 'coverage-expanded-' + width + '.png') })
          await page.click('[data-cov-expand="' + target + '"]')
          await page.waitForSelector('[data-cov-body="' + target + '"]', { state: 'detached', timeout: 15000 })
        }
        console.log('        [shots] coverage-collapsed-' + width + '.png + coverage-expanded-' + width + '.png')
      }
    } catch (e) {
      console.log('        （覆盖矩阵截图失败：' + e.message + '）')
    }
    // 「建议详情与策略设置」这一块单独截图：整页截图里它常常在滚动容器下方，看不见
    try {
      const panel = await page.$('.hc-adv__settings')
      if (panel) {
        await panel.scrollIntoViewIfNeeded()
        await sleep(250)
        await panel.screenshot({ path: join(SHOTS, 'settings-panel-' + width + '.png') })
      }
    } catch (e) {
      console.log('        （settings 面板截图失败：' + e.message + '）')
    }
    await page.close()
  }

  // ---- 5b. 零 console error / pageerror（真登录态、两个宽度都算） ----
  assertOrFact('★零 console error / pageerror（真登录态，1350 + 900）',
    consoleErrors.length === 0 && pageErrors.length === 0,
    'console=' + JSON.stringify(consoleErrors.slice(0, 3)) + ' page=' + JSON.stringify(pageErrors.slice(0, 3)))

  // ---- 6. 允许注入两段式（真写；--toggle） ----
  if (TOGGLE) {
    console.log('\n===== 允许注入：真写验收（code-worker）=====')
    const otherProfiles = ['default']
    for (const name of readdirSync(join(HERMES_HOME, 'profiles'), { withFileTypes: true })
      .filter((d) => d.isDirectory()).map((d) => d.name)) {
      if (name !== 'code-worker' && existsSync(join(HERMES_HOME, 'profiles', name, 'config.yaml'))) otherProfiles.push(name)
    }
    const { createHash } = await import('node:crypto')
    const hashAll = () => {
      const out = {}
      for (const name of otherProfiles) {
        const { path, text } = readConfigYaml(name)
        out[name] = createHash('sha256').update(text).digest('hex')
      }
      return out
    }
    const beforeHashes = hashAll()
    const beforeCw = readConfigYaml('code-worker')

    const page = await browser.newPage({ viewport: { width: 1350, height: 900 }, deviceScaleFactor: 1 })
    page.on('pageerror', (err) => pageErrors.push('[toggle] ' + String(err)))
    await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
    await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
    await page.click('.hc-nav__item:has-text("Advisor")')
    await page.waitForSelector('[data-coverage-rows]', { timeout: 30000 })
    await sleep(900)

    const toggle = { steps: [] }
    // 卡片式：注入是「展开区里的权限开关」，不是表格里的一列按钮。
    // 先展开 code-worker 的卡片，再点开关；面板就地渲染在同一张卡里。
    async function openCw() {
      const body = '[data-cov-body="code-worker"]'
      if (await page.$(body)) return
      await page.click('[data-cov-expand="code-worker"]')
      await page.waitForSelector(body, { timeout: 15000 })
    }
    async function clickInjection(want) {
      await openCw()
      const sel = `[data-coverage-profile="code-worker"] [data-cov-switch="injection"] [data-cov-action="${want}"]`
      await page.waitForSelector(sel, { timeout: 15000 })
      await page.click(sel)
      await page.waitForSelector('[data-cov-dialog]', { timeout: 15000 })
      // 预览是异步的：等确认按钮出现（= 变更清单已到），不要用固定 sleep 猜
      await page.waitForSelector('[data-cov-confirm="yes"]', { timeout: 30000 })
      const dialogText = await page.evaluate(() => {
        const d = document.querySelector('[data-cov-dialog]')
        return d ? String(d.textContent || '') : ''
      })
      const changesText = await page.evaluate(() => {
        const l = document.querySelector('[data-cov-changes]')
        return l ? String(l.textContent || '') : ''
      })
      await page.click('[data-cov-confirm="yes"]')
      // 执行完成 = 结果区（改前→改后）出现
      await page.waitForSelector('[data-cov-transitions]', { timeout: 30000 })
      await sleep(400)
      const resultText = await page.evaluate(() => {
        const d = document.querySelector('[data-cov-dialog]')
        return d ? String(d.textContent || '') : ''
      })
      await page.click('.hc-adv__cov-panel .hc-adv__modal-actions .hc-btn')
      await page.waitForSelector('[data-cov-dialog]', { state: 'detached', timeout: 15000 }).catch(() => {})
      await sleep(300)
      return { dialogText, changesText, resultText }
    }

    // ① 打开注入
    const on = await clickInjection('enable_injection')
    const cwOn = readConfigYaml('code-worker')
    const litOn = injectionLiteral(cwOn.text)
    toggle.steps.push({ step: 'enable_injection', changes: on.changesText, dialog: on.dialogText.slice(0, 800), literal: litOn })
    assertOrFact('★★① 打开注入后 config.yaml 回读 allow_gateway_injection 为 true',
      litOn === 'true', 'literal=' + litOn)
    assertOrFact('★① 确认框写明了要改的字段与后果',
      /allow_gateway_injection/.test(on.changesText) && /只记账不打扰/.test(on.dialogText),
      'changes=' + on.changesText.slice(0, 120))
    await sleep(700)
    const cellOn = await page.evaluate(() => {
      const sw = document.querySelector('[data-coverage-profile="code-worker"] [data-cov-switch="injection"]')
      return sw ? sw.getAttribute('data-cov-switch-state') : 'missing'
    })
    assertOrFact('★★③ 卡片「允许注入」开关随之变为 on', cellOn === 'on', 'data-cov-switch-state=' + cellOn)
    toggle.steps.push({ step: 'cell_after_on', value: cellOn })

    // ② 再关闭
    const off = await clickInjection('disable_injection')
    const cwOff = readConfigYaml('code-worker')
    const litOff = injectionLiteral(cwOff.text)
    toggle.steps.push({ step: 'disable_injection', dialog: off.dialogText.slice(0, 600), literal: litOff })
    assertOrFact('★★② 关闭注入后 config.yaml 回读为 false', litOff === 'false', 'literal=' + litOff)
    await sleep(700)
    const cellOff = await page.evaluate(() => {
      const sw = document.querySelector('[data-coverage-profile="code-worker"] [data-cov-switch="injection"]')
      return sw ? sw.getAttribute('data-cov-switch-state') : 'missing'
    })
    assertOrFact('★★③ 卡片「允许注入」开关随之变回 off', cellOff === 'off', 'data-cov-switch-state=' + cellOff)
    toggle.steps.push({ step: 'cell_after_off', value: cellOff })

    // ④ 其它 profile 字节不变
    const afterHashes = hashAll()
    const changed = Object.keys(beforeHashes).filter((k) => beforeHashes[k] !== afterHashes[k])
    assertOrFact('★★④ 未勾选 Agent 的 config 逐文件哈希不变',
      changed.length === 0, 'changed=' + JSON.stringify(changed))
    toggle.beforeHashes = beforeHashes
    toggle.afterHashes = afterHashes
    toggle.changed = changed
    toggle.codeWorkerBefore = beforeCw.path
    report.toggle = toggle
    await page.screenshot({ path: join(SHOTS, 'advisor-live-toggle.png'), fullPage: true })
    await page.close()
  }
} catch (e) {
  failures.push('异常：' + (e && e.stack ? e.stack.split('\n')[0] : String(e)))
  console.log('  FAIL 异常：' + (e && e.stack ? e.stack : e))
} finally {
  report.consoleErrors = consoleErrors
  report.pageErrors = pageErrors
  report.net = netLog
  report.pass = pass
  report.failures = failures
  report.facts = facts
  if (browser) await browser.close().catch(() => {})
  if (site) site.close()
  try {
    mkdirSync(dirname(OUT_JSON), { recursive: true })
    writeFileSync(OUT_JSON, JSON.stringify(report, null, 2), 'utf8')
    console.log('\n[out] ' + OUT_JSON)
  } catch (e) {
    console.log('[out] 写报告失败：' + e.message)
  }
  // 网络摘要：状态码序列
  const byStatus = {}
  for (const item of netLog) byStatus[item.status] = (byStatus[item.status] || 0) + 1
  console.log('[net] 状态码分布 :', JSON.stringify(byStatus))
  console.log('[net] advisor 读接口序列 :')
  for (const item of netLog.filter((n) => /advisor/.test(n.url))) {
    console.log(`   ${item.method} ${item.url.split('?')[0]} → ${item.status} (${item.bytes}B)`
      + (item.reqIfNoneMatch ? ' inm=' + item.reqIfNoneMatch : '')
      + (item.respEtag ? ' etag=' + item.respEtag : '')
      + (item.respCacheControl ? ' cc=' + item.respCacheControl : ''))
  }
  console.log('\nPASS ' + pass + ' / FAIL ' + failures.length)
  if (failures.length) {
    console.log('FAILURES:')
    for (const f of failures) console.log('  - ' + f)
  }
  if (EXPECT_OK && failures.length) process.exitCode = 1
  else console.log('LIVE-PROBE-OK')
}
