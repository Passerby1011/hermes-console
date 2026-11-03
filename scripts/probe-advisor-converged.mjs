/**
 * Advisor 面板版面收敛取证（t_22813006），真产物 + 真夹具 + 真浏览器。
 *
 * 只读：不写任何 profile / settings.json。断的是本卡的硬指标：
 *   1. Agent 选择器**唯一**（含 profile 值的 select 计数 = 1）；
 *   2. `enabled / concern_default / min_interval_seconds / mode` 四个键的**可编辑控件各 1 个**；
 *   3. 技术详情折叠区里没有任何可编辑控件（纯文本对账）；
 *   4. 切 agent 后表单值 = 该 agent 的 GET values（default ↔ work 各一次，贴两次 GET 原文）；
 *   5. 覆盖矩阵高亮行 = 选择器当前值；
 *   6. 诊断区默认收起成一行，摘要里是计数；
 *   7. 覆盖矩阵收起态每行 ≤ 56px，且都有名字 / 徽标 / 展开箭头。
 *
 * 跑法：
 *   node scripts/probe-advisor-converged.mjs [--label tail]
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
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_UNIQ_FIXTURE_PORT || 9295)
const SITE_PORT = Number(process.env.HC_UNIQ_SITE_PORT || 9296)
const API = '/api/plugins/hermes-console'
const LABEL = process.argv.indexOf('--label') > -1 ? process.argv[process.argv.indexOf('--label') + 1] : 'uniq'
const SHOTS = resolve(PLUGIN, 'scripts', '_probe_out', 'advisor-converged-' + LABEL)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
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

const WORK = mkdtempSync(join(tmpdir(), 'hc-adv-conv-'))
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
const requests = []
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
    let response = null
    try { response = text ? JSON.parse(text) : null } catch (e) { response = null }
    requests.push({ method, path, status: res.status, response })
    if (!res.ok) throw new Error(res.status + ': ' + text)
    return response
  },
  storage: {
    get: (k, f) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : f),
    set: (k, v) => { store[k] = v },
    remove: (k) => { delete store[k] }
  },
  registerMany: (items) => { for (const it of items || []) if (it.area === 'routes') pages.push(it) }
}
window.hermesDesktop = {
  api: async (o) => {
    const init = { method: o.method || 'POST', headers: { 'content-type': 'application/json' } }
    if (o.body !== undefined) init.body = JSON.stringify(o.body)
    const res = await fetch(o.path, init)
    const text = await res.text()
    requests.push({ method: o.method || 'POST', path: o.path, status: res.status })
    if (!res.ok) throw new Error(res.status + ': ' + text)
    return text ? JSON.parse(text) : null
  }
}
const mod = await import('./entry.js')
mod.default.register(ctx)
const styleText = await import('./style.css')
const style = document.createElement('style')
style.setAttribute('data-hermes-console-style', '')
style.textContent = styleText.default || styleText
document.head.appendChild(style)
window.__hcRequests = requests
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
      const body = readFileSync(file)
      res.writeHead(200, { 'content-type': url.endsWith('.css') ? 'text/css' : 'text/javascript' })
      res.end(body)
      return
    }
    if (url === '/favicon.ico') { res.writeHead(204); res.end(); return }
    res.writeHead(404); res.end('nf')
  })
  return new Promise((r) => server.listen(SITE_PORT, '127.0.0.1', () => r(server)))
}

const fixture = spawn(PYTHON, [join(HERE, 'advisor_ui_fixture.py'), '--port', String(FIXTURE_PORT)], {
  cwd: PLUGIN,
  stdio: ['ignore', 'pipe', 'pipe'],
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
  } catch (e) { /* 还没起来 */ }
  await sleep(250)
}
if (!ready) {
  fixture.kill()
  throw new Error('夹具没起来：\n' + flog)
}
const site = await startSite()
const browser = await chromium.launch({ channel: process.env.HC_PW_CHANNEL || 'msedge' })
const errors = []
try {
  const page = await browser.newPage({ viewport: { width: 1350, height: 900 } })
  page.on('pageerror', (e) => errors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    if (/favicon|net::ERR_/.test(m.text())) return
    errors.push(m.text())
  })
  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await page.click('.hc-nav__item:has-text("Advisor")')
  await page.waitForSelector('.hc-adv', { timeout: 20000 })
  await page.waitForSelector('[data-settings-profile]', { timeout: 30000 })
  await sleep(1200)

  console.log('\n[1] Agent 选择器唯一性')
  const scope = await page.evaluate(() => {
    const adv = document.querySelector('.hc-adv')
    const selects = Array.from(adv.querySelectorAll('select'))
    const PROFILE_NAMES = ['default', 'work', 'code-worker', 'full', 'missing', 'reviewing', 'disabled']
    const profileSelects = selects.filter((el) =>
      Array.from(el.options).some((o) => PROFILE_NAMES.indexOf(o.value) > -1))
    return {
      totalSelects: selects.length,
      profileSelects: profileSelects.length,
      profileSelectFilters: profileSelects.map((el) => el.getAttribute('data-filter') || '(none)'),
      scopeBars: adv.querySelectorAll('[data-adv-scope]').length,
      settingsPicker: adv.querySelectorAll('[data-filter="settings-profile"]').length,
      modeAgentChip: adv.querySelectorAll('[data-modes-agent-picker="no"]').length
    }
  })
  check('Agent 切换入口只有 1 个（含 profile 值的 select 计数 = 1）',
    scope.profileSelects === 1, JSON.stringify(scope))
  check('那一个入口挂在唯一的 scope 条上（data-adv-scope = 1）',
    scope.scopeBars === 1, JSON.stringify(scope))
  check('设置区没有第二个 Agent 选择器（data-filter=settings-profile = 0）',
    scope.settingsPicker === 0, 'settingsPicker=' + scope.settingsPicker)

  console.log('\n[2] 四个设置键的可编辑控件各 1 个')
  const editable = await page.evaluate(() => {
    const adv = document.querySelector('.hc-adv')
    const keys = ['enabled', 'concern_default', 'min_interval_seconds', 'mode']
    return keys.map((key) => {
      const els = Array.from(adv.querySelectorAll('[data-settings-field="' + key + '"]'))
      const el = els[0]
      return {
        key,
        count: els.length,
        kind: el ? (el.tagName === 'INPUT' ? 'input:' + el.type : el.tagName.toLowerCase()) : '(none)'
      }
    })
  })
  for (const row of editable) {
    check('可编辑控件唯一：' + row.key + ' = ' + row.count + ' 个（' + row.kind + '）',
      row.count === 1, JSON.stringify(row))
  }
  const techControls = await page.evaluate(() => {
    const tech = document.querySelector('[data-settings-tech]')
    return tech ? tech.querySelectorAll('input, select, textarea, button').length : -1
  })
  check('技术详情折叠区无任何可编辑控件（纯文本对账）', techControls === 0, 'controls=' + techControls)
  const techClosed = await page.evaluate(() => {
    const tech = document.querySelector('[data-settings-tech]')
    return tech ? tech.hasAttribute('open') : null
  })
  check('技术详情默认收起（source 表的注意力不与表单并列）', techClosed === false, 'open=' + techClosed)

  console.log('\n[3] profile 切换：表单值来自该 agent 的 GET')
  const formOf = () => page.evaluate(() => {
    const box = document.querySelector('[data-settings-profile]')
    const val = (key) => {
      const el = box && box.querySelector('[data-settings-field="' + key + '"]')
      if (!el) return null
      return el.type === 'checkbox' ? el.checked : el.value
    }
    return {
      profile: box ? box.getAttribute('data-settings-profile') : '',
      enabled: val('enabled'),
      concern_default: val('concern_default'),
      min_interval_seconds: val('min_interval_seconds'),
      mode: val('mode'),
      gate: (box.querySelector('[data-gate-enabled]') || {}).getAttribute
        ? box.querySelector('[data-gate-enabled]').getAttribute('data-gate-enabled')
        : ''
    }
  })
  const apiGet = async (agent) => (await (await fetch(
    'http://127.0.0.1:' + FIXTURE_PORT + API + '/advisor/settings?agent=' + agent)).json())

  const getDefault = await apiGet('default')
  const formDefault = await formOf()
  check('default：表单四键 = GET values',
    formDefault.profile === 'default'
    && String(formDefault.enabled) === String(getDefault.data.values.enabled)
    && String(formDefault.concern_default) === String(getDefault.data.values.concern_default)
    && String(formDefault.min_interval_seconds) === String(getDefault.data.values.min_interval_seconds)
    && String(formDefault.mode) === String(getDefault.data.values.mode || 'auto'),
    JSON.stringify({ ui: formDefault, api: getDefault.data.values }))

  await page.selectOption('[data-filter="agent"]', 'work')
  await page.waitForFunction(() => {
    const box = document.querySelector('[data-settings-profile]')
    return box && box.getAttribute('data-settings-profile') === 'work'
  }, undefined, { timeout: 20000 })
  await sleep(700)
  const getWork = await apiGet('work')
  const formWork = await formOf()
  check('work：设置卡片 data-settings-profile=work', formWork.profile === 'work', formWork.profile)
  check('work：表单四键 = work 自己的 GET values（不是 default 的）',
    String(formWork.enabled) === String(getWork.data.values.enabled)
    && String(formWork.concern_default) === String(getWork.data.values.concern_default)
    && String(formWork.min_interval_seconds) === String(getWork.data.values.min_interval_seconds)
    && String(formWork.mode) === String(getWork.data.values.mode || 'auto'),
    JSON.stringify({ ui: formWork, api: getWork.data.values, defaultApi: getDefault.data.values }))

  const settingsCalls = await page.evaluate(() => window.__hcRequests
    .filter((r) => r.path.indexOf('/advisor/settings') === 0)
    .map((r) => r.method + ' ' + r.path))
  check('切 work 真的发了 /advisor/settings?agent=work（带 agent、不带 profile）',
    settingsCalls.some((p) => /agent=work/.test(p)) && !settingsCalls.some((p) => /[?&]profile=/.test(p)),
    JSON.stringify(settingsCalls.slice(-4)))

  await page.selectOption('[data-filter="agent"]', 'default')
  await page.waitForFunction(() => {
    const box = document.querySelector('[data-settings-profile]')
    return box && box.getAttribute('data-settings-profile') === 'default'
  }, undefined, { timeout: 20000 })
  await sleep(700)
  const formBack = await formOf()
  check('切回 default：表单四键回到 default 的 GET values',
    formBack.profile === 'default'
    && String(formBack.enabled) === String(getDefault.data.values.enabled)
    && String(formBack.concern_default) === String(getDefault.data.values.concern_default)
    && String(formBack.min_interval_seconds) === String(getDefault.data.values.min_interval_seconds),
    JSON.stringify({ ui: formBack, api: getDefault.data.values }))

  console.log('\n[4] 同一个选择器驱动矩阵高亮')
  const selectedRow = await page.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('[data-coverage-profile]'))
    const scopeEl = document.querySelector('[data-adv-scope]')
    return {
      selected: cards.filter((c) => /hc-adv__cov-card--selected/.test(c.className))
        .map((c) => c.getAttribute('data-coverage-profile')),
      scope: scopeEl ? scopeEl.getAttribute('data-adv-scope') : ''
    }
  })
  check('矩阵高亮行 = 选择器当前值（同一个 state 驱动）',
    selectedRow.selected.length === 1 && selectedRow.selected[0] === selectedRow.scope,
    JSON.stringify(selectedRow))

  console.log('\n[5] 诊断默认收起成一行')
  const diag = await page.evaluate(() => {
    const box = document.querySelector('[data-adv-diagnostics]')
    if (!box) return null
    const sum = box.querySelector('summary')
    const body = box.querySelector('.hc-adv__diag-body')
    return {
      open: box.hasAttribute('open'),
      summaryText: sum ? String(sum.textContent || '').replace(/\s+/g, ' ').trim() : '',
      chips: Array.from(box.querySelectorAll('[data-diag-chip]')).map((c) =>
        c.getAttribute('data-diag-chip') + '=' + String(c.textContent || '').trim()),
      bodyHeight: body ? Math.round(body.getBoundingClientRect().height) : 0,
      height: Math.round(box.getBoundingClientRect().height)
    }
  })
  check('诊断区默认收起，且摘要高度 ≤ 60px（一行）',
    Boolean(diag) && diag.open === false && diag.bodyHeight === 0 && diag.height <= 60,
    JSON.stringify(diag && { open: diag.open, h: diag.height, body: diag.bodyHeight }))
  check('诊断摘要是计数 chip（无台账 / 超时 / 无终态 / 日志旁证）',
    Boolean(diag) && diag.chips.length >= 3 && /无台账|超时|无终态|日志/.test(diag.summaryText),
    JSON.stringify(diag && diag.chips))
  // 点开要真的能展开（收起不等于打不开）
  await page.click('[data-adv-diagnostics] > summary')
  await sleep(400)
  const diagOpen = await page.evaluate(() => {
    const box = document.querySelector('[data-adv-diagnostics]')
    const body = box.querySelector('.hc-adv__diag-body')
    return {
      open: box.hasAttribute('open'),
      bodyHeight: body ? Math.round(body.getBoundingClientRect().height) : 0,
      hasTrail: Boolean(box.querySelector('[data-trail-total]'))
    }
  })
  check('点开诊断：明细（评审流水）真的出现', diagOpen.open && diagOpen.bodyHeight > 0 && diagOpen.hasTrail,
    JSON.stringify(diagOpen))
  await page.click('[data-adv-diagnostics] > summary')
  await sleep(300)

  console.log('\n[6] 覆盖矩阵压成紧凑行')
  const cov = await page.evaluate(() => {
    const cards = Array.from(document.querySelectorAll('[data-coverage-profile]'))
    return {
      cards: cards.length,
      open: cards.filter((c) => /hc-adv__cov-card--open/.test(c.className)).length,
      heights: cards.map((c) => Math.round(c.getBoundingClientRect().height)),
      bodies: document.querySelectorAll('[data-cov-body]').length,
      heads: cards.map((c) => ({
        p: c.getAttribute('data-coverage-profile'),
        name: Boolean(c.querySelector('.hc-adv__cov-name')),
        badge: Boolean(c.querySelector('.hc-adv__cov-badge')),
        expand: Boolean(c.querySelector('[data-cov-expand]'))
      }))
    }
  })
  check('覆盖矩阵默认全收起（展开区 0 个）', cov.bodies === 0 && cov.open === 0,
    JSON.stringify({ bodies: cov.bodies, open: cov.open }))
  check('每个 agent 一行、收起态高度 ≤ 56px（改造前 ~90px）',
    cov.cards > 0 && cov.heights.every((h) => h > 0 && h <= 56), 'heights=' + JSON.stringify(cov.heights))
  check('收起态每行都有名字 / 状态徽标 / 展开箭头',
    cov.heads.every((h) => h.name && h.badge && h.expand), JSON.stringify(cov.heads))
  // 行内对齐：每行的「操作区」必须贴同一条右缘，否则一行一个按钮位置，读起来像随手摆的。
  const rowAlign = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('[data-coverage-profile]')).map((c) => {
      const rect = (el) => (el ? { l: Math.round(el.getBoundingClientRect().left), r: Math.round(el.getBoundingClientRect().right) } : null)
      const r = c.getBoundingClientRect()
      return {
        p: c.getAttribute('data-coverage-profile'),
        card: { l: Math.round(r.left), r: Math.round(r.right), w: Math.round(r.width) },
        side: rect(c.querySelector('.hc-adv__cov-side')),
        arrow: rect(c.querySelector('[data-cov-expand]')),
        primary: rect(c.querySelector('[data-cov-action]'))
      }
    })
    const uniq = (arr) => Array.from(new Set(arr.filter((v) => v !== null && v !== undefined)))
    return {
      rows: rows.length,
      cardRight: uniq(rows.map((r) => r.card.r)),
      cardWidth: uniq(rows.map((r) => r.card.w)),
      sideRight: uniq(rows.map((r) => r.side && r.side.r)),
      arrowRight: uniq(rows.map((r) => r.arrow && r.arrow.r)),
      primaryRight: uniq(rows.map((r) => r.primary && r.primary.r)),
      detail: rows
    }
  })
  console.log('        rowAlign: ' + JSON.stringify({ cardW: rowAlign.cardWidth, cardR: rowAlign.cardRight, side: rowAlign.sideRight, arrow: rowAlign.arrowRight, primary: rowAlign.primaryRight }))
  check('覆盖矩阵每行操作区贴同一条右缘（按钮不再逐行左右跳）',
    rowAlign.sideRight.length === 1 && rowAlign.arrowRight.length === 1,
    JSON.stringify({ side: rowAlign.sideRight, arrow: rowAlign.arrowRight }))
  check('同一行里「主操作 → 展开箭头」的间距固定（有主操作的几行右缘同一列）',
    rowAlign.primaryRight.length === 1 && rowAlign.rows > 0,
    JSON.stringify({ primary: rowAlign.primaryRight, arrow: rowAlign.arrowRight }))

  console.log('\n[8] 顶栏外壳作用域选择器：ignoresScope 分区里不再像第二个 agent 选择器')
  const shellScope = await page.evaluate(() => {
    const box = document.querySelector('.hc-topbar .hc-scope')
    const sel = document.querySelector('#hc-scope-select')
    const advSel = document.querySelector('[data-filter="agent"]')
    const paneChip = Array.from(document.querySelectorAll('.hc-pane__head .hc-chip'))
      .map((c) => String(c.textContent || '').trim())
    const style = box ? getComputedStyle(box) : null
    return {
      hasBox: Boolean(box),
      hiddenAttr: box ? box.hasAttribute('hidden') : null,
      disabledAttr: sel ? sel.disabled : null,
      hiddenProp: box ? box.hidden : null,
      display: style ? style.display : '',
      boxHeight: box ? Math.round(box.getBoundingClientRect().height) : -1,
      visibleScopeWidgets: Array.from(document.querySelectorAll('.hc-topbar .hc-select, .hc-topbar .hc-scope'))
        .filter((el) => el.getBoundingClientRect().height > 0 && getComputedStyle(el).display !== 'none').length,
      advPickerVisible: advSel ? advSel.getBoundingClientRect().height > 0 : false,
      paneHeadChips: paneChip
    }
  })
  check('Advisor 分区（ignoresScope）：顶栏作用域控件 hidden + disabled',
    shellScope.hasBox && shellScope.hiddenAttr === true && shellScope.hiddenProp === true
    && shellScope.disabledAttr === true && shellScope.display === 'none' && shellScope.boxHeight === 0,
    JSON.stringify(shellScope))
  check('Advisor 分区：顶栏可见的作用域控件 = 0（屏幕上只剩面板自己那个 Agent 选择器）',
    shellScope.visibleScopeWidgets === 0 && shellScope.advPickerVisible === true,
    JSON.stringify({ topbar: shellScope.visibleScopeWidgets, adv: shellScope.advPickerVisible }))
  check('Advisor 分区：边界由分区标题旁「全部 agent」chip 承接说明',
    shellScope.paneHeadChips.indexOf('全部 agent') !== -1,
    JSON.stringify(shellScope.paneHeadChips))

  // 切到受作用域约束的分区（概览）⇒ 顶栏选择器必须原样回来（不能把外壳功能关死）
  await page.click('.hc-nav__item:has-text("概览")')
  await sleep(900)
  const shellScopeBack = await page.evaluate(() => {
    const box = document.querySelector('.hc-topbar .hc-scope')
    const sel = document.querySelector('#hc-scope-select')
    const style = box ? getComputedStyle(box) : null
    return {
      hasBox: Boolean(box),
      hiddenAttr: box ? box.hasAttribute('hidden') : null,
      disabledAttr: sel ? sel.disabled : null,
      display: style ? style.display : '',
      boxHeight: box ? Math.round(box.getBoundingClientRect().height) : -1,
      options: sel ? sel.options.length : 0
    }
  })
  check('切到概览（受作用域约束）：顶栏作用域选择器恢复可见可用',
    shellScopeBack.hasBox && shellScopeBack.hiddenAttr === false && shellScopeBack.disabledAttr === false
    && shellScopeBack.display !== 'none' && shellScopeBack.boxHeight > 0 && shellScopeBack.options > 1,
    JSON.stringify(shellScopeBack))
  await page.click('.hc-nav__item:has-text("Advisor")')
  await page.waitForSelector('.hc-adv', { timeout: 20000 })
  await sleep(900)

  console.log('\n[9] 同一段口径说明不再打印两次（统计条 vs 列表底）')
  const noteDup = await page.evaluate(() => {
    const texts = (scopeSel) => Array.from(document.querySelectorAll(scopeSel))
      .map((n) => String(n.textContent || '').replace(/\s+/g, ' ').trim())
    const stat = texts('.hc-adv__status .hc-adv__note')
    const list = texts('[data-block="list"] .hc-adv__note')
    const dup = list.filter((t) => stat.indexOf(t) !== -1)
    // 台账文件名在这两个区域里的出现次数（同一份文件只该说一次）
    const joined = stat.concat(list).join(' \n ')
    const repeat = (needle) => (joined.match(new RegExp(needle.replace('.', '\\.'), 'g')) || []).length
    return {
      stat: stat.length,
      list: list.length,
      dup: dup.slice(0, 2),
      fileMentions: { 'advice.jsonl': repeat('advice.jsonl'), 'reviews.jsonl': repeat('reviews.jsonl') }
    }
  })
  check('列表底的 notes 与统计条不再重复（去重生效）',
    noteDup.dup.length === 0, JSON.stringify({ stat: noteDup.stat, list: noteDup.list, dup: noteDup.dup }))
  check('同一份台账文件（advice.jsonl / reviews.jsonl）在统计+列表口径里各只被提及一次',
    Object.keys(noteDup.fileMentions).every((k) => noteDup.fileMentions[k] <= 1),
    JSON.stringify(noteDup.fileMentions))

  console.log('\n[10] 折叠项体检（同类口径说明不该散成很多个 ▸）')
  const folds = await page.evaluate(() => {
    const pane = document.querySelector('.hc-adv')
    const label = (d) => {
      const s = d.querySelector('summary')
      return s ? String(s.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 42) : '(无 summary)'
    }
    const all = Array.from(pane.querySelectorAll('details'))
    // 「用户一眼看到的 ▸」= 没有被别的**收起**的 details 藏起来的那批（嵌套证据/明细不算独立碎片）
    const visible = all.filter((d) => {
      let p = d.parentElement
      while (p && p !== pane) {
        if (p.tagName === 'DETAILS' && !p.hasAttribute('open')) return false
        p = p.parentElement
      }
      return true
    })
    return {
      count: all.length,
      visible: visible.map(label),
      visibleCount: visible.length,
      all: all.map(label)
    }
  })
  console.log('        folds: ' + JSON.stringify({ visible: folds.visible, all: folds.count }))
  check('一屏可见的折叠项 ≤ 5 个（同类口径说明已合并，不散成碎片）',
    folds.visibleCount <= 5, 'visible=' + folds.visibleCount + ' / 全部=' + folds.count)

  console.log('\n[7] 版面取证')
  const layout = await page.evaluate(() => {
    const root = document.querySelector('.hc-adv')
    const pane = document.querySelector('.hc-pane')
    return {
      blocks: Array.from(root.children).map((el) => ({
        cls: String(el.className || el.tagName).slice(0, 40),
        h: Math.round(el.getBoundingClientRect().height)
      })),
      pane: pane ? { h: pane.clientHeight, scroll: pane.scrollHeight } : null
    }
  })
  console.log('        layout: ' + JSON.stringify(layout))
  check('覆盖矩阵块高度 ≤ 420px（9 张 90px 卡片 = ~810px 的旧账）',
    (layout.blocks.find((b) => /hc-adv__block/.test(b.cls)) || {}).h <= 420,
    JSON.stringify(layout.blocks))

  await page.setViewportSize({ width: 1350, height: 2400 })
  await sleep(500)
  await page.screenshot({ path: join(SHOTS, 'after-1350.png'), fullPage: true })
  await page.setViewportSize({ width: 900, height: 2400 })
  await sleep(500)
  await page.screenshot({ path: join(SHOTS, 'after-900.png'), fullPage: true })
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement
    const content = document.querySelector('.hc-content') || document.body
    const cr = content.getBoundingClientRect()
    let spill = 0
    for (const el of document.querySelectorAll('.hc-adv, .hc-adv *')) {
      const st = getComputedStyle(el)
      if (st.position === 'fixed' || st.display === 'none') continue
      const r = el.getBoundingClientRect()
      if (r.width === 0) continue
      if (r.right > cr.right + 1 || r.left < cr.left - 1) spill += 1
    }
    return { docScroll: doc.scrollWidth, docClient: doc.clientWidth,
      contentScroll: content.scrollWidth, contentClient: content.clientWidth, spill }
  })
  check('900px 无横向溢出（文档 / 内容区 / 元素级）',
    overflow.docScroll <= overflow.docClient + 1
    && overflow.contentScroll <= overflow.contentClient + 1 && overflow.spill === 0,
    JSON.stringify(overflow))
  check('零 console error / pageerror', errors.length === 0, JSON.stringify(errors.slice(0, 3)))
} finally {
  await browser.close().catch(() => {})
  site.close()
  fixture.kill()
}

console.log('\n' + '='.repeat(64))
if (failures.length) {
  console.log('ADVISOR-CONVERGED-FAILED  (%d passed, %d failed)', pass, failures.length)
  failures.forEach((f) => console.log('  - ' + f))
  process.exit(1)
}
console.log('ADVISOR-CONVERGED-OK  (%d checks)  shots=%s', pass, SHOTS)
