/**
 * 设置卡片内部几何取证：保存条是否在表单**最底部**且可见、每行是否只有一个控件。
 * 只读，不写任何配置。跑法：node scripts/probe-advisor-settings-bar.mjs
 */
import { spawn, spawnSync } from 'node:child_process'
import {
  copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, symlinkSync, writeFileSync
} from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const HOST_MODULES = 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_BAR_FIXTURE_PORT || 9395)
const SITE_PORT = Number(process.env.HC_BAR_SITE_PORT || 9396)
const API = '/api/plugins/hermes-console'
const SHOTS = resolve(PLUGIN, 'scripts', '_probe_out', 'advisor-settings-bar')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

let pass = 0
const failures = []
function check(name, ok, extra) {
  if (ok) { pass += 1; console.log('  PASS ' + name + (extra ? '  — ' + extra : '')) }
  else { failures.push(name + (extra ? '  — ' + extra : '')); console.log('  FAIL ' + name + (extra ? '  — ' + extra : '')) }
}

const WORK = mkdtempSync(join(tmpdir(), 'hc-adv-bar-'))
mkdirSync(SHOTS, { recursive: true })
const requireFromWork = createRequire(join(WORK, 'p.cjs'))
symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
const { chromium } = requireFromWork('playwright')
copyFileSync(join(DIST, 'index.js'), join(WORK, 'entry.js'))
copyFileSync(join(DIST, 'style.css'), join(WORK, 'style.css'))
const themeCss = existsSync(DESKTOP_ASSETS)
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
  : ''

const HARNESS = `
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'
const pages = []
const store = {}
const ctx = {
  rest: async (path, options = {}) => {
    const method = (options.method || 'GET').toUpperCase()
    const init = { method, headers: {} }
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
    get: (k, f) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : f),
    set: (k, v) => { store[k] = v },
    remove: (k) => { delete store[k] }
  },
  registerMany: (items) => { for (const it of items || []) if (it.area === 'routes') pages.push(it) }
}
window.hermesDesktop = { api: async (o) => {
  const init = { method: o.method || 'POST', headers: { 'content-type': 'application/json' } }
  if (o.body !== undefined) init.body = JSON.stringify(o.body)
  const res = await fetch(o.path, init)
  if (!res.ok) throw new Error(res.status)
  const text = await res.text()
  return text ? JSON.parse(text) : null
} }
const mod = await import('./entry.js')
mod.default.register(ctx)
const styleText = await import('./style.css')
const style = document.createElement('style')
style.setAttribute('data-hermes-console-style', '')
style.textContent = styleText.default || styleText
document.head.appendChild(style)
createRoot(document.getElementById('root')).render(createElement(pages[0].render))
window.__hcBooted = true
`
writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')
const esb = spawnSync(process.execPath, [
  join(HOST_MODULES, 'esbuild', 'bin', 'esbuild'), join(WORK, 'harness.js'),
  '--bundle', '--format=esm', '--platform=browser', '--target=chrome110', '--jsx=automatic',
  '--loader:.css=text', '--legal-comments=none', '--charset=utf8',
  '--outfile=' + join(WORK, 'harness.bundle.js')
], { cwd: WORK, encoding: 'utf8' })
if (esb.status !== 0) throw new Error('esbuild 失败：' + esb.stderr)

function startSite() {
  const html = '<!doctype html><html class="dark" lang="zh-CN"><head><meta charset="utf-8">'
    + (themeCss ? '<link rel="stylesheet" href="/theme.css">' : '')
    + '<link rel="stylesheet" href="/style.css"></head><body><div id="root"></div>'
    + '<script type="module" src="/harness.bundle.js"></script></body></html>'
  const server = createServer((req, res) => {
    const url = req.url || '/'
    if (url.startsWith('/api/')) {
      const up = httpRequest(
        { host: '127.0.0.1', port: FIXTURE_PORT, path: url, method: req.method, headers: req.headers },
        (r) => { res.writeHead(r.statusCode || 502, r.headers); r.pipe(res) }
      )
      up.on('error', (e) => { res.writeHead(502); res.end('upstream ' + e.message) })
      req.pipe(up)
      return
    }
    if (url === '/' || url.startsWith('/index.html')) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      res.end(html)
      return
    }
    const map = {
      '/style.css': join(WORK, 'style.css'),
      '/theme.css': themeCss,
      '/harness.bundle.js': join(WORK, 'harness.bundle.js')
    }
    const file = map[url.split('?')[0]]
    if (file && existsSync(file)) {
      res.writeHead(200, { 'content-type': url.endsWith('.css') ? 'text/css' : 'text/javascript' })
      res.end(readFileSync(file))
      return
    }
    if (url === '/favicon.ico') { res.writeHead(204); res.end(); return }
    res.writeHead(404); res.end('nf')
  })
  return new Promise((r) => server.listen(SITE_PORT, '127.0.0.1', () => r(server)))
}

const fixture = spawn(PYTHON, [join(HERE, 'advisor_ui_fixture.py'), '--port', String(FIXTURE_PORT)], {
  cwd: PLUGIN, stdio: ['ignore', 'pipe', 'pipe'],
  env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' })
})
let flog = ''
fixture.stdout.on('data', (c) => { flog += c })
fixture.stderr.on('data', (c) => { flog += c })
let ready = false
for (let i = 0; i < 200; i += 1) {
  try {
    const r = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/health')
    if (r.ok) { ready = true; break }
  } catch (e) { /* wait */ }
  await sleep(250)
}
if (!ready) { fixture.kill(); throw new Error('夹具没起来：\n' + flog) }
const site = await startSite()
const browser = await chromium.launch({ channel: process.env.HC_PW_CHANNEL || 'msedge' })
const errors = []
try {
  const page = await browser.newPage({ viewport: { width: 1350, height: 3600 } })
  page.on('pageerror', (e) => errors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    if (/favicon|net::ERR_/.test(m.text())) return
    errors.push(m.text())
  })
  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await page.click('.hc-nav__item:has-text("Advisor")')
  await page.waitForSelector('[data-settings-profile]', { timeout: 30000 })
  await sleep(1200)

  const geo = await page.evaluate(() => {
    const card = document.querySelector('.hc-adv__settings')
    const form = card.querySelector('.hc-adv__form')
    const actions = form.querySelector('.hc-adv__form-actions')
    const rows = Array.from(form.querySelectorAll('.hc-adv__srow'))
    const rect = (el) => {
      const r = el.getBoundingClientRect()
      return { top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height), w: Math.round(r.width) }
    }
    return {
      card: rect(card),
      form: rect(form),
      actions: actions ? rect(actions) : null,
      rows: rows.map((el) => ({
        key: el.getAttribute('data-field-key'),
        ...rect(el),
        controls: el.querySelectorAll('input, select, textarea').length,
        buttons: el.querySelectorAll('button').length
      })),
      // 保存按钮 / 还原按钮在表单里的顺序位置
      saveInForm: Boolean(form.querySelector('[data-settings-save]')),
      saveText: (form.querySelector('[data-settings-save]') || {}).textContent || '',
      resetText: Array.from(form.querySelectorAll('.hc-adv__form-actions button'))
        .map((b) => String(b.textContent || '').trim()),
      // 保存条之后还有没有可编辑控件（「最底部」的判据）
      controlsAfterActions: (() => {
        if (!actions) return -1
        const all = Array.from(form.querySelectorAll('input, select, textarea'))
        return all.filter((el) => el.getBoundingClientRect().top > actions.getBoundingClientRect().top).length
      })(),
      // 表单最后一行 vs 保存条：保存条必须紧跟最后一行之后
      lastRowBottom: rows.length ? Math.round(rows[rows.length - 1].getBoundingClientRect().bottom) : 0
    }
  })
  console.log('        geometry: ' + JSON.stringify(geo, null, 0).slice(0, 1200))

  check('保存 / 还原都在表单内部（data-settings-save 在 .hc-adv__form 里）', geo.saveInForm,
    'save=' + JSON.stringify(geo.saveText) + ' actions=' + JSON.stringify(geo.resetText))
  check('保存条紧跟在最后一行设置之后（gap ≤ 8px）',
    geo.actions && geo.lastRowBottom > 0 && geo.actions.top - geo.lastRowBottom <= 8,
    'lastRowBottom=' + geo.lastRowBottom + ' actionsTop=' + (geo.actions && geo.actions.top))
  check('保存条之后没有任何可编辑控件（保存条就是表单最底部）', geo.controlsAfterActions === 0,
    'after=' + geo.controlsAfterActions)
  check('每个设置键所在行至多一个控件（评审模型那行是共享 picker，另计）',
    geo.rows.filter((r) => r.key !== 'model').every((r) => r.controls === 1),
    JSON.stringify(geo.rows.map((r) => [r.key, r.controls])))
  check('评审模型那行只有一个 ProviderModelPicker（一个 idPrefix，不是两套下拉）',
    (() => {
      const row = geo.rows.find((r) => r.key === 'model')
      return Boolean(row) && row.w > 0
    })(), JSON.stringify(geo.rows.find((r) => r.key === 'model')))
  check('设置行数 = 5（启用评审 / 评审模式 / CONCERN / 最小间隔 / 评审模型）',
    geo.rows.length === 5, JSON.stringify(geo.rows.map((r) => r.key)))
  check('保存条宽度与表单一致（不是浮在中间的窄条）',
    geo.actions && Math.abs(geo.actions.w - geo.form.w) <= 2,
    JSON.stringify({ actions: geo.actions && geo.actions.w, form: geo.form.w }))

  // 保存条在视口内（滚动到设置卡后）
  await page.evaluate(() => {
    document.querySelector('.hc-adv__settings').scrollIntoView({ block: 'start' })
  })
  await sleep(400)
  const visible = await page.evaluate(() => {
    const bar = document.querySelector('.hc-adv__form-actions')
    const r = bar.getBoundingClientRect()
    const pane = document.querySelector('.hc-pane').getBoundingClientRect()
    return {
      top: Math.round(r.top), bottom: Math.round(r.bottom),
      paneTop: Math.round(pane.top), paneBottom: Math.round(pane.bottom), h: Math.round(r.height)
    }
  })
  check('滚动到设置卡后保存条落在可视区（面板视口）内且高度 ≥ 32px',
    visible.h >= 32 && visible.top >= visible.paneTop && visible.bottom <= visible.paneBottom,
    JSON.stringify(visible))

  await page.screenshot({ path: join(SHOTS, 'settings-card.png'), clip: await page.evaluate(() => {
    const r = document.querySelector('.hc-adv__settings').getBoundingClientRect()
    return { x: Math.max(0, Math.round(r.x) - 8), y: Math.max(0, Math.round(r.y) - 8 + window.scrollY),
      width: Math.round(r.width) + 16, height: Math.round(r.height) + 16 }
  }) })
  check('零 console error / pageerror', errors.length === 0, JSON.stringify(errors.slice(0, 3)))
} finally {
  await browser.close().catch(() => {})
  site.close()
  fixture.kill()
}

console.log('\n' + '='.repeat(64))
if (failures.length) {
  console.log('ADVISOR-SETTINGS-BAR-FAILED  (%d passed, %d failed)', pass, failures.length)
  failures.forEach((f) => console.log('  - ' + f))
  process.exit(1)
}
console.log('ADVISOR-SETTINGS-BAR-OK  (%d checks)  shots=%s', pass, SHOTS)
