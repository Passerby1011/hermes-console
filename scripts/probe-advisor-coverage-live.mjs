/**
 * hermes-console · 评审面板徽标文案 / 开关真值 验收探针（t_5dbc60b8）
 *
 * 打**真**后端（127.0.0.1:9119）+ 真产物（dashboard/dist/index.js）+ 真 Chromium：
 *   1. 五档徽标文案逐条对表（active / reviewing_only / installed_disabled×2 / not_installed）；
 *   2. 「参与评审」开关状态必须等于该 profile 的 settings.json 里 enabled 的实际值
 *      （探针自己读盘，不信接口）；
 *   3. 后端字段与文件不一致时必须出现「文件与面板不一致」提示（本机当前应无此情况，
 *      但探针会把 data-cov-settings-mismatch 全部打出来）；
 *   4. 每种状态一张单卡截图 + work 展开态截图；
 *   5. 零 console error / pageerror。
 *
 * 跑法：node scripts/probe-advisor-coverage-live.mjs [--shots <dir>]
 */
import { spawn } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
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
const BUNDLE = resolve(argOf('--bundle', join(DIST, 'index.js')))
const SHOTS = resolve(argOf('--shots', join(PLUGIN, 'scripts', '_probe_out', 'advisor-coverage')))

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) { pass += 1; console.log('  PASS ' + name + (extra ? '  — ' + extra : '')) }
  else { failures.push(name + (extra ? '  — ' + extra : '')); console.log('  FAIL ' + name + (extra ? '  — ' + extra : '')) }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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
  if (!res.ok) throw new Error('登录失败 ' + res.status)
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

/** 真后端 /advisor/coverage 的行（与页面同一份数据，用于对表）。 */
async function fetchCoverage(cookie) {
  const res = await fetch(`http://${LIVE_HOST}:${LIVE_PORT}${API}/advisor/coverage`, { headers: { cookie } })
  const payload = await res.json()
  const body = payload.data || payload
  return body.profiles || []
}

/** 直接读盘：settings.json 的 enabled 实际值（文件不存在 / 没有该键 = 缺省 → true）。 */
function diskEnabled(settingsPath) {
  try {
    const data = JSON.parse(readFileSync(settingsPath, 'utf8'))
    if (data && typeof data === 'object' && Object.prototype.hasOwnProperty.call(data, 'enabled')) {
      return { value: data.enabled !== false, raw: data, has_key: true, missing: false }
    }
    return { value: true, raw: data, has_key: false, missing: false }
  } catch (e) {
    // ENOENT：settings.json 还没有 → advisor 用 DEFAULT_SETTINGS 的 enabled=true
    return { value: true, raw: null, has_key: false, missing: true, error: String(e.message || e) }
  }
}

mkdirSync(SHOTS, { recursive: true })
const WORK = mkdtempSync(join(tmpdir(), 'hc-cov-live-'))
const requireFromWork = createRequire(join(WORK, 'probe.cjs'))
let chromium
{
  const { symlinkSync } = await import('node:fs')
  symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
}
;({ chromium } = requireFromWork('playwright'))

if (!existsSync(BUNDLE)) throw new Error('找不到产物：' + BUNDLE)
copyFileSync(BUNDLE, join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))
const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

const HARNESS = `
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
    const res = await fetch('${API}' + path, init)
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
  registerMany: (items) => { for (const item of items || []) if (item.area === 'routes') pages.push(item) }
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
      cwd: WORK, stdio: ['ignore', 'pipe', 'pipe']
    })
    let err = ''
    child.stderr.on('data', (c) => { err += c })
    child.on('exit', (code) => (code === 0 ? res() : rej(new Error('esbuild ' + code + '\n' + err))))
  })
}
await runEsbuild([
  join(WORK, 'harness.js'), '--bundle', '--format=esm', '--platform=browser', '--target=chrome110',
  '--jsx=automatic', '--loader:.css=text', '--legal-comments=none', '--charset=utf8',
  '--outfile=' + join(WORK, 'harness.bundle.js')
])

function startSite(cookie) {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head><meta charset="utf-8"><title>hermes-console · 覆盖矩阵真后端验收</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}body{background:var(--ui-bg-editor, var(--ui-bg-chrome, transparent))}#root{height:100%}</style>
</head><body><div id="root"></div><script type="module" src="/harness.bundle.js"></script></body></html>`
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
            safe(() => { res.writeHead(up.statusCode || 502, up.headers); res.end(body) })
          })
        }
      )
      upstream.on('error', (e) => safe(() => { res.writeHead(502, { 'content-type': 'text/plain' }); res.end('upstream: ' + e.message) }))
      req.pipe(upstream)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      safe(() => { res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }); res.end(html) })
      return
    }
    if (url.startsWith('/favicon')) {
      safe(() => { res.writeHead(204); res.end() })
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
  return new Promise((res) => server.listen(SITE_PORT, '127.0.0.1', () => res(server)))
}

const consoleErrors = []
const pageErrors = []
let site = null
let browser = null
const report = { bundle: BUNDLE, shots: SHOTS, rows: [], labels: {}, checks: [] }

try {
  const auth = await realLogin()
  console.log('[env] 真登录态 cookie :', auth.names.join(' / '))
  const apiRows = await fetchCoverage(auth.cookie)
  console.log('[env] /advisor/coverage rows =', apiRows.length)

  site = await startSite(auth.cookie)
  browser = await chromium.launch({ channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined })
  const page = await browser.newPage({ viewport: { width: 1350, height: 950 }, deviceScaleFactor: 1 })
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await page.click('.hc-nav__item:has-text("Advisor")')
  await page.waitForSelector('.hc-adv', { timeout: 20000 })
  await page.waitForSelector('[data-coverage-rows]', { timeout: 30000 })
  await sleep(1200)

  // ---- 1. 逐卡读徽标 / 第二行 / 真值属性 ----
  const cards = await page.evaluate(() => Array.from(document.querySelectorAll('[data-coverage-profile]')).map((c) => {
    const badge = c.querySelector('.hc-adv__cov-badge')
    return {
      profile: c.getAttribute('data-coverage-profile'),
      status: c.getAttribute('data-coverage-status'),
      label: c.getAttribute('data-coverage-label'),
      badgeText: badge ? String(badge.textContent || '').trim() : '',
      badgeTitle: badge ? badge.getAttribute('title') : '',
      facts: Array.from(c.querySelectorAll('.hc-adv__cov-fact')).map((f) => String(f.textContent || '').trim()),
      mismatch: c.getAttribute('data-cov-settings-mismatch')
    }
  }))
  report.rows = cards

  const EXPECT_FIXED = {
    active: '可用',
    reviewing_only: '已启用 · 待首次产出',
    not_installed: '未安装'
  }
  for (const row of apiRows) {
    const seen = cards.find((c) => c.profile === row.profile)
    if (!seen) { check('卡片存在：' + row.profile, false); continue }
    let expected
    if (row.status === 'installed_disabled') expected = row.enabled_in_config ? '已启用 · 无记录' : '未启用'
    else expected = EXPECT_FIXED[row.status] || row.status
    check('徽标文案 ' + row.profile + ' [' + row.status + (row.status === 'installed_disabled' ? ' cfg=' + row.enabled_in_config : '') + '] → ' + expected,
      seen.badgeText === expected, 'actual=' + JSON.stringify(seen.badgeText))
    check('徽标不含「部分可用」（' + row.profile + '）', !/部分可用/.test(seen.badgeText), 'actual=' + seen.badgeText)
  }

  // ---- 2. 逐卡展开：评审开关 vs settings.json 实际值 ----
  for (const row of apiRows) {
    if (!row.plugin_installed) continue
    const sel = '[data-coverage-profile="' + row.profile + '"]'
    await page.click(sel + ' [data-cov-expand="' + row.profile + '"]')
    await page.waitForSelector('[data-cov-body="' + row.profile + '"]', { timeout: 15000 })
    await sleep(250)
    const info = await page.evaluate((s) => {
      const card = document.querySelector(s)
      const sw = card.querySelector('[data-cov-switch="review"]')
      const note = card.querySelector('[data-cov-settings-note]')
      return {
        reviewSwitch: sw ? sw.getAttribute('data-cov-switch-state') : 'missing',
        file: note ? note.getAttribute('data-cov-settings-file') : '',
        panel: note ? note.getAttribute('data-cov-settings-panel') : '',
        note: note ? String(note.textContent || '').trim() : '',
        noteClass: note ? note.className : '',
        cardMismatch: card.getAttribute('data-cov-settings-mismatch')
      }
    }, sel)
    const disk = diskEnabled(row.settings_path)
    const expectedState = disk.value === false ? 'off' : 'on'
    const detail = 'switch=' + info.reviewSwitch + ' disk=' + JSON.stringify(disk.value)
      + ' has_key=' + disk.has_key + ' note=' + JSON.stringify(info.note)
    check('评审开关 = settings.json 实际值（' + row.profile + '）', info.reviewSwitch === expectedState, detail)
    check('settings 提示行写明文件真值（' + row.profile + '）',
      info.file === String(disk.value) && /settings\.json/.test(info.note)
        && (disk.missing ? /还不存在/.test(info.note) : /enabled=/.test(info.note)),
      'file=' + info.file + ' disk=' + JSON.stringify(disk) + ' note=' + info.note)
    if (info.cardMismatch === 'yes') {
      check('不一致时必须标警告条（' + row.profile + '）', /文件与面板不一致/.test(info.note) && /hc-adv__cov-alert/.test(info.noteClass), info.note)
    } else {
      check('后端字段与文件一致时不误报（' + row.profile + '）', info.file === info.panel, 'file=' + info.file + ' panel=' + info.panel)
    }
    report.rows.find((c) => c.profile === row.profile).expanded = info

    // work 的展开态单独留证（任务书点名要）
    if (row.profile === 'work') {
      const card = await page.$(sel)
      await card.screenshot({ path: join(SHOTS, 'work-review-switch.png') })
      writeFileSync(join(SHOTS, 'work-settings.json.txt'),
        row.settings_path + '\n' + readFileSync(row.settings_path, 'utf8'), 'utf8')
    }
    await page.click(sel + ' [data-cov-expand="' + row.profile + '"]')
    await page.waitForSelector('[data-cov-body="' + row.profile + '"]', { state: 'detached', timeout: 15000 })
  }

  // ---- 3. 五种文案各一张单卡截图 ----
  const WANT = [
    ['可用', 'active'],
    ['已启用 · 待首次产出', 'reviewing-only'],
    ['已启用 · 无记录', 'installed-disabled-enabled'],
    ['未启用', 'installed-disabled-disabled'],
    ['未安装', 'not-installed']
  ]
  for (const [label, slug] of WANT) {
    const hit = cards.find((c) => c.badgeText === label)
    if (!hit) { check('找到「' + label + '」的卡片', false); continue }
    const el = await page.$('[data-coverage-profile="' + hit.profile + '"]')
    await el.scrollIntoViewIfNeeded()
    await el.screenshot({ path: join(SHOTS, slug + '.png') })
    report.labels[label] = { profile: hit.profile, file: slug + '.png' }
    check('截图「' + label + '」= ' + hit.profile + ' → ' + slug + '.png', true)
  }
  const box = await page.$('[data-coverage-rows]')
  if (box) {
    await box.scrollIntoViewIfNeeded()
    await sleep(200)
    await box.screenshot({ path: join(SHOTS, 'coverage-matrix.png') })
  }
  await page.screenshot({ path: join(SHOTS, 'advisor-full.png'), fullPage: true })

  // ---- 4. 零 console error / pageerror ----
  check('零 console error', consoleErrors.length === 0, JSON.stringify(consoleErrors.slice(0, 5)))
  check('零 pageerror', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 5)))
  report.consoleErrors = consoleErrors
  report.pageErrors = pageErrors
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) await new Promise((r) => site.close(r))
  writeFileSync(join(SHOTS, 'report.json'), JSON.stringify(report, null, 2), 'utf8')
}

console.log('\n=== 汇总 ===')
console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
failures.forEach((f) => console.log('  FAILED: ' + f))
console.log(failures.length ? 'COVERAGE-LIVE-FAIL' : 'COVERAGE-LIVE-OK')
console.log('截图目录：' + SHOTS)
process.exit(failures.length ? 1 : 0)
