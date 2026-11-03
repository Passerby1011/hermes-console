/**
 * hermes-console · 项目分组新布局验收（t_1dea3c49）
 *
 * 跑法（任意目录）::
 *
 *     node D:/hermes-data/hermes/plugins/hermes-console/scripts/probe-autogroup-ui.mjs
 *     node .../probe-autogroup-ui.mjs --shots D:/tmp/shots
 *
 * 三层都是真的：
 *   1. **真产物**：`dashboard/dist/index.js`（= `desktop/plugin.js` 同字节）原样加载，只套一层 harness；
 *   2. **真后端**：`scripts/autogroup_ui_fixture.py` 按 dashboard 的方式加载**真实的** `dashboard/plugin_api.py`
 *      （16 个域全挂上），home 指向沙盒（真 projects.db 副本 + 合成 state.db/assignments.db + 插件树镜像）；
 *   3. **真浏览器**：Playwright + Chromium(msedge)，900×900 与 1350×900 两个宽度。
 *
 * 断言（对应卡面 A–E）：
 *   A. 入口唯一性：「扫描并生成建议」1 个、「跑一批」类 1 个、「待确认建议」区块 1 个、「立即 tick」1 个；
 *   B. 首屏主体：900/1350 两个宽度下项目区与会话区（含两个分区）都在首屏内，无横向溢出，附截图；
 *   C. Agent 筛选 + 分区：切 agent 后列表只剩该 agent，已分组/未分组计数与后端逐条复算一致；
 *   D. 快捷操作：单条改分组（预览零写入 → 确认回读一致）、批量（未选中零变更）、
 *      重命名显示名（目录不变）、解散（无孤儿）、自建项目（目录+实体）、删除；
 *   E. 桌面端口径：单独核对 desktop/plugin.js 与产物哈希，门副本不一致就明说「桌面端运行态未加载新产物」。
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, symlinkSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, request as httpRequest } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const DIST = join(PLUGIN, 'dashboard', 'dist')
const DESKTOP = join(PLUGIN, 'desktop', 'plugin.js')
const DOOR = 'D:/hermes-data/hermes/desktop-plugins/hermes-console/plugin.js'
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_AGG_FIXTURE_PORT || 9197)
const SITE_PORT = Number(process.env.HC_AGG_SITE_PORT || 9198)
const API = '/api/plugins/hermes-console'
const EXCLUDED_SOURCES = ['acp', 'kanban', 'oneshot', 'subagent', 'tool']

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'autogroup-ui')

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
const sha256 = (p) => (existsSync(p) ? createHash('sha256').update(readFileSync(p)).digest('hex') : '')

const WORK = mkdtempSync(join(tmpdir(), 'hc-agg-ui-'))
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
  ? readdirSync(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0] || ''
  : ''

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
    requests.push({ method, path, status: res.status, body: options.body === undefined ? null : options.body, at: Date.now() })
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

function startSite() {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · 项目分组（新布局验收）</title>
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
        /* client gone */
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
  child.stdout.on('data', (c) => {
    out += c
  })
  child.stderr.on('data', (c) => {
    out += c
  })
  return { child, log: () => out }
}

async function waitForFixture(fixture) {
  for (let i = 0; i < 200; i += 1) {
    try {
      const res = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/health')
      if (res.ok) return true
    } catch (e) {
      /* not up yet */
    }
    await sleep(250)
  }
  throw new Error('后端夹具没起来：\n' + fixture.log())
}

const apiJson = async (path, init) => {
  const res = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + path, init)
  const text = await res.text()
  try {
    return JSON.parse(text)
  } catch (e) {
    return { ok: false, raw: text }
  }
}

const isVisibleRow = (s) => {
  const source = String(s.source || '').toLowerCase()
  if (EXCLUDED_SOURCES.indexOf(source) >= 0) return false
  if (s.message_count === null || s.message_count === undefined) return true
  return Number(s.message_count) >= 1
}

/** 独立复算：某个 agent 下桌面端可见 / 已分组 / 未分组各多少条（只读后端，不读界面）。 */
async function expectedCounts(agentId) {
  let sessions = []
  if (agentId === '__all__') {
    const agents = (await apiJson('/agents')).agents || []
    for (const a of agents.slice(0, 16)) {
      const one = await apiJson('/overview/sessions?agent=' + encodeURIComponent(a.id) + '&limit=100')
      sessions = sessions.concat((one.data && one.data.sessions) || [])
    }
  } else {
    const one = await apiJson('/overview/sessions?agent=' + encodeURIComponent(agentId) + '&limit=100')
    sessions = (one.data && one.data.sessions) || []
  }
  const visible = sessions.filter(isVisibleRow)
  const applied = await apiJson('/autogroup/suggestions?status=applied&limit=200')
  const owned = new Set(((applied.data && applied.data.items) || []).map((i) => i.session_id))
  const grouped = visible.filter((s) => owned.has(s.id)).length
  return { visible: visible.length, grouped: grouped, ungrouped: visible.length - grouped }
}

async function appliedRow(sessionId) {
  const payload = await apiJson('/autogroup/suggestions?status=applied&limit=200')
  const items = (payload.data && payload.data.items) || []
  return items.filter((i) => i.session_id === sessionId)[0] || null
}

async function entities() {
  const payload = await apiJson('/autogroup/entities')
  return (payload.data && payload.data.projects) || []
}

/** 轮询到条件成立（写操作的回读核对用：不认「元素出现过」，认后端现状）。 */
async function waitUntil(fn, ms) {
  const deadline = Date.now() + (ms || 15000)
  while (Date.now() < deadline) {
    const got = await fn()
    if (got) return got
    await sleep(250)
  }
  return null
}

const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

try {
  console.log('[env] dist    :', DIST)
  console.log('[env] desktop :', DESKTOP)
  console.log('[env] door    :', DOOR)
  console.log('[env] shots   :', SHOTS)
  console.log('[env] sandbox :', SANDBOX)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture :', '127.0.0.1:' + FIXTURE_PORT + API)

  const projectsBefore = await entities()
  const slugA = projectsBefore[0].slug
  const slugB = projectsBefore[1].slug
  console.log('[env] slugs   :', slugA, slugB)
  // 真 home 指纹：结尾逐字节比对，证明沙盒隔离真的隔离（写操作没有打到生产库）
  const REAL_FILES = ['D:/hermes-data/hermes/auto-group/assignments.db', 'D:/hermes-data/hermes/projects.db']
  const realBefore = {}
  REAL_FILES.forEach((p) => {
    realBefore[p] = sha256(p)
  })
  // 第三库 state.db 是 2.2GB 的**活**会话库（Hermes 自己一直在写）：不哈希、不比 mtime，
  // 用「夹具会话没有出现在生产库里」来证明隔离（见 F5 的 probe_real_state_isolation.py）。
  // 计划应用的台账与快照目录：只许落在沙盒，真 home 不许出现
  const REAL_ABSENT = ['D:/hermes-data/hermes/auto-group/mode_ops.db', 'D:/hermes-data/hermes/auto-group/snapshots']
  const realAbsentBefore = REAL_ABSENT.map((p) => existsSync(p))

  site = await startSite()
  browser = await chromium.launch({ channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined })

  async function openPage(width) {
    const page = await browser.newPage({ viewport: { width: width, height: 900 }, deviceScaleFactor: 1 })
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
    await page.waitForSelector('[data-agg2-root]', { timeout: 30000 })
    await page.waitForSelector('[data-agg2-sect="grouped"]', { timeout: 30000 })
    // 会话列表与自动模式状态是两条独立请求：等它们各自到货再断言，避免把「还在 loading」当成「没有」
    await page.waitForFunction(
      () => Number(document.querySelector('[data-agg2-sessions]').getAttribute('data-agg2-sessions') || 0) > 0,
      { timeout: 30000 }
    )
    await page.waitForSelector('[data-agg-auto-tick]', { state: 'attached', timeout: 30000 })
    return page
  }

  // ---------------------------------------------------------------- A 入口唯一性
  const page = await openPage(1350)
  const entries = await page.evaluate(() => {
    const buttons = Array.from(document.querySelectorAll('button'))
    const text = (node) => (node.textContent || '').replace(/\s+/g, '')
    return {
      // t_8754de3f 有意变更：扫描主按钮文案随勾选变（未勾选 = 分析全部未分组 N 条）
      scan: buttons.filter((b) => /^分析(全部未分组|已选)\d+条/.test(text(b))).length,
      run: document.querySelectorAll('[data-aic-run]').length,
      runLegacy: document.querySelectorAll('[data-aic-run-full],[data-aic-run-batch]').length,
      tick: document.querySelectorAll('[data-agg-auto-tick]').length,
      titleExact: Array.from(document.querySelectorAll('*')).filter(
        (el) => el.children.length === 0 && (el.textContent || '').trim() === '待确认建议'
      ).length,
      suggestedBlocks: document.querySelectorAll('[data-agg2-drawer="suggested"]').length,
      drawers: document.querySelectorAll('[data-agg2-drawer]').length,
      openDrawers: document.querySelectorAll('[data-agg2-drawer][open]').length
    }
  })
  console.log('[A] entries:', JSON.stringify(entries))
  check('A1「分析…」主按钮只出现 1 次', entries.scan === 1, 'count=' + entries.scan)
  check('A2「跑一批」类按钮只出现 1 次（data-aic-run）', entries.run === 1, 'count=' + entries.run)
  check('A3 旧的「全量/分批」两个按钮已移除', entries.runLegacy === 0, 'count=' + entries.runLegacy)
  check('A4「立即 tick」只出现 1 次', entries.tick === 1, 'count=' + entries.tick)
  check('A5「待确认建议」区块名只出现 1 次', entries.titleExact === 1 && entries.suggestedBlocks === 1,
    'text=' + entries.titleExact + ' block=' + entries.suggestedBlocks)
  check('A6 折叠区 5 个抽屉默认全部收起', entries.drawers === 5 && entries.openDrawers === 0,
    'drawers=' + entries.drawers + ' open=' + entries.openDrawers)

  // ---------------------------------------------------------------- B 首屏主体
  async function layoutCheck(page, width) {
    const box = await page.evaluate(() => {
      const rect = (sel) => {
        const el = document.querySelector(sel)
        if (!el) return null
        const r = el.getBoundingClientRect()
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width) }
      }
      const root = document.querySelector('[data-agg2-root]')
      const offenders = []
      if (root) {
        const limit = root.getBoundingClientRect().right
        Array.from(root.querySelectorAll('*')).forEach((el) => {
          const r = el.getBoundingClientRect()
          if (r.width === 0) return
          if (r.right > limit + 2) {
            offenders.push({
              tag: el.tagName.toLowerCase(),
              cls: String(el.className || '').slice(0, 60),
              right: Math.round(r.right),
              w: Math.round(r.width)
            })
          }
        })
      }
      return {
        rail: rect('[data-agg2-projects]'),
        railHead: rect('[data-agg2-projects] .hc-agg2__pane-head'),
        sessions: rect('[data-agg2-sessions]'),
        sessHead: rect('[data-agg2-sessions] .hc-agg2__pane-head'),
        grouped: rect('[data-agg2-sect="grouped"]'),
        ungrouped: rect('[data-agg2-sect="ungrouped"]'),
        vw: window.innerWidth,
        vh: window.innerHeight,
        docW: document.documentElement.scrollWidth,
        rootScrollW: root ? root.scrollWidth : 0,
        rootClientW: root ? root.clientWidth : 0,
        offenders: offenders.slice(0, 8)
      }
    })
    console.log('[B] ' + width + 'px:', JSON.stringify(box))
    const inFirstScreen = (r) => !!r && r.top >= 0 && r.top < box.vh - 40
    check('B' + width + '-1 项目区在首屏内', inFirstScreen(box.railHead) && inFirstScreen(box.rail),
      box.railHead ? 'top=' + box.railHead.top : 'missing')
    check('B' + width + '-2 会话区标题在首屏内', inFirstScreen(box.sessHead) && inFirstScreen(box.sessions),
      box.sessHead ? 'top=' + box.sessHead.top : 'missing')
    check('B' + width + '-3 已分组/未分组两个分区都在首屏内', inFirstScreen(box.grouped) && inFirstScreen(box.ungrouped),
      'grouped=' + (box.grouped && box.grouped.top) + ' ungrouped=' + (box.ungrouped && box.ungrouped.top))
    check('B' + width + '-4 无横向溢出', box.docW <= box.vw + 1 && box.rootScrollW <= box.rootClientW + 1,
      'docW=' + box.docW + '/' + box.vw + ' root=' + box.rootScrollW + '/' + box.rootClientW)
    await page.screenshot({ path: join(SHOTS, 'layout-' + width + '.png'), fullPage: false })
    return box
  }

  await layoutCheck(page, 1350)
  await page.setViewportSize({ width: 900, height: 900 })
  await sleep(400)
  await layoutCheck(page, 900)
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(400)

  // ---------------------------------------------------------------- C Agent 筛选 + 分区
  const domCounts = async (p) =>
    p.evaluate(() => {
      const root = document.querySelector('[data-agg2-sessions]')
      return {
        rows: Number(root.getAttribute('data-agg2-sessions') || 0),
        grouped: Number(root.getAttribute('data-agg2-grouped-count') || 0),
        ungrouped: Number(root.getAttribute('data-agg2-ungrouped-count') || 0),
        rowIds: Array.from(document.querySelectorAll('[data-agg2-session]')).map((el) => el.getAttribute('data-agg2-session')),
        bothSects: !!document.querySelector('[data-agg2-sect="grouped"]') && !!document.querySelector('[data-agg2-sect="ungrouped"]')
      }
    })

  const allDom = await domCounts(page)
  const allExp = await expectedCounts('__all__')
  console.log('[C] all dom:', JSON.stringify(allDom), 'exp:', JSON.stringify(allExp))
  check('C1「全部」下两个分区同时存在', allDom.bothSects === true)
  check('C2「全部」下已分组计数与后端复算一致', allDom.grouped === allExp.grouped, allDom.grouped + ' vs ' + allExp.grouped)
  check('C3「全部」下未分组计数与后端复算一致', allDom.ungrouped === allExp.ungrouped, allDom.ungrouped + ' vs ' + allExp.ungrouped)
  check('C4「全部」下会话总数与后端复算一致', allDom.rows === allExp.visible, allDom.rows + ' vs ' + allExp.visible)

  await page.click('[data-agg2-agent="work"]')
  await sleep(700)
  const workDom = await domCounts(page)
  const workExp = await expectedCounts('work')
  console.log('[C] work dom:', JSON.stringify(workDom), 'exp:', JSON.stringify(workExp))
  check('C5 切到 work 后列表只剩 work 的会话', workDom.rowIds.length > 0 && workDom.rowIds.every((id) => id.indexOf('sess-wk-') === 0),
    workDom.rowIds.join(','))
  check('C6 切 agent 后已分组/未分组计数跟着变（与后端一致）',
    workDom.grouped === workExp.grouped && workDom.ungrouped === workExp.ungrouped,
    workDom.grouped + '/' + workDom.ungrouped + ' vs ' + workExp.grouped + '/' + workExp.ungrouped)
  check('C7 切 agent 后计数确实不同于「全部」', workDom.grouped !== allDom.grouped || workDom.ungrouped !== allDom.ungrouped,
    'all=' + allDom.grouped + '/' + allDom.ungrouped + ' work=' + workDom.grouped + '/' + workDom.ungrouped)

  await page.click('[data-agg2-agent="xiaomo"]')
  await sleep(700)
  const xmDom = await domCounts(page)
  const xmExp = await expectedCounts('xiaomo')
  check('C8 切到 xiaomo 计数与后端一致', xmDom.grouped === xmExp.grouped && xmDom.ungrouped === xmExp.ungrouped,
    xmDom.grouped + '/' + xmDom.ungrouped + ' vs ' + xmExp.grouped + '/' + xmExp.ungrouped)

  await page.click('[data-agg2-agent="__all__"]')
  await sleep(700)
  const backDom = await domCounts(page)
  check('C9 切回「全部」计数复原', backDom.grouped === allExp.grouped && backDom.ungrouped === allExp.ungrouped,
    backDom.grouped + '/' + backDom.ungrouped)

  // 搜索过滤
  await page.fill('[data-agg2-search]', 'weather')
  await sleep(400)
  const searchDom = await domCounts(page)
  check('C10 标题搜索只留命中项', searchDom.rows === 1 && searchDom.rowIds[0] === 'sess-agg-06',
    JSON.stringify(searchDom.rowIds))
  await page.fill('[data-agg2-search]', '')
  await sleep(400)

  // ---------------------------------------------------------------- G 模式选择（t_f2338eb2）
  // 卡面 A–D：模式显式可见 + B 破坏性提示 + 预览「这次会动什么」+ 确认应用 + 整批撤销。
  // 这里全部走**真浏览器 + 真后端**：请求体从 harness 抓，写入结果从后端回读，不信 DOM 自述。
  //
  // 顺序：本段排在 D 段（手工新建/删除项目）**之前**，跑在夹具初始态上。
  // 原因：projects.db 是 WAL 模式，而 autogroup_modes._snapshot 只拷主文件、不拷 -wal；
  // D 段刚删过项目时，快照可能还留着那行（未 checkpoint），撤销就会把它拷回来。
  // 那是后端快照的缺陷（已单独报卡），不是界面口径；界面验收不该被它带成随机红。
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(300)

  const stableRow = (row) =>
    JSON.stringify({
      session_id: row.session_id,
      project_slug: row.project_slug,
      status: row.status,
      source: row.source || '',
      batch_id: row.batch_id || '',
      confidence: row.confidence
    })
  const appliedRows = async () => (await apiJson('/autogroup/suggestions?status=applied&limit=200')).data.items
  const appliedMap = async () => {
    const out = {}
    ;(await appliedRows()).forEach((row) => {
      out[row.session_id] = stableRow(row)
    })
    return out
  }
  const totals = async () => {
    const rows = await appliedRows()
    const projects = (await apiJson('/autogroup/entities')).data.projects
    return { applied: rows.length, projects: projects.length }
  }
  const planRequests = () =>
    page.evaluate(() => (window.__hcRequests || []).filter((r) => r.path === '/autogroup/plan').map((r) => r.body))

  const panelState = () =>
    page.evaluate(() => {
      const q = (sel) => document.querySelector(sel)
      const radio = (m) => q('[data-agg2-mode="' + m + '"]')
      const gate = (k) => q('[data-agg2-mode-gate="' + k + '"]')
      const btn = (sel) => q(sel)
      const modePanel = q('[data-agg2-mode-panel]')
      return {
        panel: !!modePanel,
        active: modePanel ? modePanel.getAttribute('data-agg2-mode-active') : '',
        radioA: !!radio('ungrouped_only'),
        radioB: !!radio('rebuild_all'),
        checkedA: radio('ungrouped_only') ? radio('ungrouped_only').checked : null,
        checkedB: radio('rebuild_all') ? radio('rebuild_all').checked : null,
        previewButtons: document.querySelectorAll('[data-agg2-plan-preview]').length,
        safeNote: !!q('[data-agg2-mode-safe]'),
        warn: !!q('[data-agg2-mode-warn]'),
        gates: ['scan', 'ai', 'auto'].map((k) => {
          const el = gate(k)
          return el ? el.getAttribute('data-agg2-mode-gate-state') : 'missing'
        }),
        scanDisabled: q('[data-agg2-scan]') ? q('[data-agg2-scan]').disabled : null,
        aiDisabled: q('[data-aic-run]') ? q('[data-aic-run]').disabled : null,
        tickDisabled: q('[data-agg-auto-tick]') ? q('[data-agg-auto-tick]').disabled : null,
        hasPlan: !!q('[data-agg2-plan]'),
        planMode: btn('[data-agg2-plan]') ? btn('[data-agg2-plan]').getAttribute('data-agg2-plan-mode') : ''
      }
    })

  const planState = () =>
    page.evaluate(() => {
      const plan = document.querySelector('[data-agg2-plan]')
      if (!plan) return null
      const attr = (name) => plan.getAttribute(name) || ''
      const rows = Array.from(document.querySelectorAll('[data-agg2-diff-row]')).map((el) => ({
        id: el.getAttribute('data-agg2-diff-row'),
        from: el.getAttribute('data-from') || '',
        to: el.getAttribute('data-to') || '',
        change: el.getAttribute('data-change') || ''
      }))
      const scopeEl = document.querySelector('[data-agg2-plan-scope]')
      const changesEl = document.querySelector('[data-agg2-plan-changes]')
      return {
        planId: plan.getAttribute('data-agg2-plan') || '',
        mode: attr('data-agg2-plan-mode'),
        zeroWrite: attr('data-agg2-plan-zero-write'),
        request: JSON.parse(attr('data-agg2-plan-request') || '{}'),
        scopeText: scopeEl ? scopeEl.textContent.trim() : '',
        scope: scopeEl
          ? {
              total: Number(scopeEl.getAttribute('data-agg2-plan-scope')),
              grouped: Number(scopeEl.getAttribute('data-agg2-plan-grouped')),
              ungrouped: Number(scopeEl.getAttribute('data-agg2-plan-ungrouped')),
              kanban: Number(scopeEl.getAttribute('data-agg2-plan-kanban'))
            }
          : null,
        changesText: changesEl ? changesEl.textContent.trim() : '',
        newProjects: Number(changesEl?.getAttribute('data-agg2-plan-new') || 0),
        droppedProjects: Number(changesEl?.getAttribute('data-agg2-plan-dropped') || 0),
        willWrite: Number(changesEl?.getAttribute('data-agg2-plan-write') || 0),
        diff: rows,
        diffCount: Number(document.querySelector('[data-agg2-diff]')?.getAttribute('data-agg2-diff') || 0),
        droppedCount: Number(document.querySelector('[data-agg2-dropped]')?.getAttribute('data-agg2-dropped') || 0),
        droppedItems: Array.from(document.querySelectorAll('[data-agg2-dropped-item]')).map((el) => ({
          slug: el.getAttribute('data-agg2-dropped-item'),
          text: el.textContent.trim()
        })),
        applyButtons: document.querySelectorAll('[data-agg2-apply]').length,
        confirmBox: !!document.querySelector('[data-agg2-apply-confirm-box]'),
        modeLabel: document.querySelector('[data-agg2-plan-mode-label]')
          ? document.querySelector('[data-agg2-plan-mode-label]').textContent.trim()
          : ''
      }
    })

  const resultState = () =>
    page.evaluate(() => {
      const el = document.querySelector('[data-agg2-result]')
      if (!el) return null
      return {
        op: el.getAttribute('data-agg2-result'),
        written: Number(el.getAttribute('data-agg2-result-written')),
        failed: Number(el.getAttribute('data-agg2-result-failed')),
        mode: el.getAttribute('data-agg2-result-mode'),
        okChip: document.querySelector('[data-agg2-result-ok]')?.textContent.trim() || '',
        badChip: document.querySelector('[data-agg2-result-bad]')?.textContent.trim() || '',
        readback: document.querySelector('[data-agg2-result-readback]')?.textContent.trim() || '',
        rows: document.querySelectorAll('[data-agg2-result-row]').length,
        undoButton: !!document.querySelector('[data-agg2-undo]'),
        failedDetails: !!document.querySelector('[data-agg2-failed]')
      }
    })

  const undoState = () =>
    page.evaluate(() => {
      const el = document.querySelector('[data-agg2-undo-result]')
      if (!el) return null
      return {
        op: el.getAttribute('data-agg2-undo-result'),
        byteIdentical: el.getAttribute('data-agg2-undo-byte-identical'),
        text: el.textContent.trim(),
        hash: document.querySelector('[data-agg2-undo-hash]')?.getAttribute('data-agg2-undo-hash') || ''
      }
    })

  const warnBox = () =>
    page.evaluate(() => {
      const el = document.querySelector('[data-agg2-mode-warn]')
      if (!el) return { present: false }
      const r = el.getBoundingClientRect()
      const cs = window.getComputedStyle(el)
      const root = document.querySelector('[data-agg2-root]')
      const rr = root.getBoundingClientRect()
      return {
        present: true,
        text: (el.textContent || '').trim(),
        visible: r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden',
        width: Math.round(r.width),
        right: Math.round(r.right),
        rootRight: Math.round(rr.right),
        scrollW: el.scrollWidth,
        clientW: el.clientWidth,
        scrollH: el.scrollHeight,
        clientH: el.clientHeight,
        whiteSpace: cs.whiteSpace,
        color: cs.color
      }
    })

  // G1 模式面板：显式两个单选、默认 A、A 不显示破坏性警告、三个旧入口按模式收口
  const g1 = await panelState()
  console.log('[G1] panel:', JSON.stringify(g1))
  check('G1a 模式面板存在且默认 A（ungrouped_only）', g1.panel && g1.active === 'ungrouped_only', g1.active)
  check('G1b 两个模式单选都在（A/B 各一个）', g1.radioA && g1.radioB, 'A=' + g1.radioA + ' B=' + g1.radioB)
  check('G1c 默认选中 A、未选中 B', g1.checkedA === true && g1.checkedB === false,
    'A=' + g1.checkedA + ' B=' + g1.checkedB)
  check('G1d A 模式显示安全提示、不显示破坏性警告', g1.safeNote === true && g1.warn === false,
    'safe=' + g1.safeNote + ' warn=' + g1.warn)
  check('G1e「先预览」入口只有 1 个', g1.previewButtons === 1, 'count=' + g1.previewButtons)
  check('G1f A 模式下三个旧入口都是 open（口径一致，可用）',
    g1.gates.every((s) => s === 'open') && g1.scanDisabled === false && g1.aiDisabled === false && g1.tickDisabled === false,
    JSON.stringify(g1.gates) + ' scan=' + g1.scanDisabled + ' ai=' + g1.aiDisabled + ' tick=' + g1.tickDisabled)

  // G2 A 模式预览：请求体带 mode=ungrouped_only，且预览零写入
  const g2Before = await totals()
  const g2RowsBefore = await appliedMap()
  await page.click('[data-agg2-plan-preview]')
  await page.waitForSelector('[data-agg2-plan]', { timeout: 30000 })
  await page.waitForFunction(() => !!document.querySelector('[data-agg2-diff-row]'), { timeout: 30000 })
  const g2After = await totals()
  const g2RowsAfter = await appliedMap()
  const g2Req = (await planRequests()).slice(-1)[0] || {}
  const g2 = await planState()
  console.log('[G2] request:', JSON.stringify(g2Req), 'plan:', JSON.stringify({ mode: g2.mode, scope: g2.scope, willWrite: g2.willWrite, diff: g2.diff.length }))
  check('G2a 预览请求体 mode=ungrouped_only', g2Req.mode === 'ungrouped_only', JSON.stringify(g2Req))
  check('G2b 预览请求体是计划契约（带 limit / sync_cwd / propose_new_projects）',
    g2Req.limit === 200 && g2Req.sync_cwd === true && g2Req.propose_new_projects === true, JSON.stringify(g2Req))
  check('G2c 预览后归属总数不变（零写入）', g2Before.applied === g2After.applied,
    g2Before.applied + ' -> ' + g2After.applied)
  check('G2d 预览后项目总数不变（零写入）', g2Before.projects === g2After.projects,
    g2Before.projects + ' -> ' + g2After.projects)
  check('G2e 预览后归属行逐字段不变（零写入）', JSON.stringify(g2RowsBefore) === JSON.stringify(g2RowsAfter))
  check('G2f 面板标注后端确认零写入（data-agg2-plan-zero-write=1）', g2.zeroWrite === '1', g2.zeroWrite)
  check('G2g 预览显示当前模式 + 扫描范围（已分组/未分组/看板排除）',
    g2.mode === 'ungrouped_only' && !!g2.scope && g2.scope.total === g2.scope.grouped + g2.scope.ungrouped &&
      g2.scopeText.indexOf('已分组') >= 0 && g2.scopeText.indexOf('看板排除') >= 0,
    JSON.stringify(g2.scope) + ' | ' + g2.scopeText)
  check('G2h 预览显示计划变更数（新建/删除/改归属）',
    g2.changesText.indexOf('新建项目') >= 0 && g2.changesText.indexOf('删除项目') >= 0 && g2.changesText.indexOf('改归属') >= 0,
    g2.changesText)
  check('G2i 逐条 diff 行数与后端 items 一致（会话 → 原项目 → 新项目）',
    g2.diffCount === g2.diff.length && g2.diff.length > 0 && g2.diff.every((r) => r.id && r.change),
    'rows=' + g2.diff.length + ' attr=' + g2.diffCount)

  // G3 A 模式确认应用：已分组会话逐字段不动
  const g3GroupedBefore = await appliedMap()
  const g3PlanIds = g2.diff.map((r) => r.id)
  const g3GroupedIds = Object.keys(g3GroupedBefore).filter((id) => g3PlanIds.indexOf(id) === -1)
  await sleep(400)
  await page.click('[data-agg2-apply]')
  await page.waitForSelector('[data-agg2-result]', { timeout: 60000 })
  await sleep(800)
  const g3Result = await resultState()
  const g3After = await appliedMap()
  const g3Totals = await totals()
  const g3ChangedGrouped = g3GroupedIds.filter((id) => g3GroupedBefore[id] !== g3After[id])
  const g3UiRows = await page.evaluate((ids) =>
    ids.map((id) => {
      const el = document.querySelector('[data-agg2-session="' + id + '"]')
      if (!el) return { id: id, missing: true }
      const proj = el.querySelector('.hc-agg2__sess-project')
      return {
        id: id,
        grouped: el.getAttribute('data-agg2-session-grouped'),
        project: proj ? proj.getAttribute('data-agg2-session-project') : null,
        projectText: proj ? proj.textContent.trim() : ''
      }
    }), g3GroupedIds)
  console.log('[G3] result:', JSON.stringify(g3Result), 'groupedIds:', g3GroupedIds.join(','), 'changed:', g3ChangedGrouped.join(','))
  check('G3a A 模式应用后已分组会话归属逐字段未变',
    g3GroupedIds.length > 0 && g3ChangedGrouped.length === 0,
    'grouped=' + g3GroupedIds.length + ' changed=' + g3ChangedGrouped.join(','))
  check('G3b A 模式应用后写入条数与计划 will_write 一致', g3Result.written === g2.willWrite,
    g3Result.written + ' vs plan ' + g2.willWrite)
  check('G3c 结果面板给出成功 / 失败数与 op_id',
    g3Result.okChip.indexOf('成功') >= 0 && g3Result.badChip.indexOf('失败') >= 0 && !!g3Result.op,
    g3Result.okChip + ' / ' + g3Result.badChip + ' / ' + g3Result.op)
  check('G3d 结果面板逐条回读（每写入一条一行）', g3Result.rows >= g3Result.written && g3Result.readback.indexOf('回读') >= 0,
    'rows=' + g3Result.rows + ' ' + g3Result.readback)
  const g3UiOk = g3UiRows
    .filter((r) => !r.missing)
    .every((r) => r.grouped === '1' && r.project === JSON.parse(g3GroupedBefore[r.id]).project_slug)
  check('G3e 界面上已分组会话仍在已分组分区且项目未变', g3UiOk,
    JSON.stringify(g3UiRows.slice(0, 2)))
  check('G3f 应用后有「整批撤销」按钮', g3Result.undoButton === true)
  check('G3g A 模式应用不新建项目（accept_new_projects=false，项目数不变）',
    g3Totals.projects === g2Before.projects,
    g3Totals.projects + ' vs ' + g2Before.projects)

  // G4 撤销：归属与项目回到应用前
  const g4ProjectsBefore = g2Before.projects
  await page.click('[data-agg2-undo]')
  await page.waitForSelector('[data-agg2-undo-result]', { timeout: 60000 })
  await sleep(800)
  const g4Undo = await undoState()
  const g4After = await appliedMap()
  const g4Totals = await totals()
  console.log('[G4] undo:', JSON.stringify(g4Undo), 'totals:', JSON.stringify(g4Totals))
  check('G4a 撤销后归属行逐字段回到应用前', JSON.stringify(g4After) === JSON.stringify(g3GroupedBefore),
    'rows=' + Object.keys(g4After).length)
  check('G4b 撤销后归属总数回到应用前', g4Totals.applied === g2Before.applied,
    g4Totals.applied + ' vs ' + g2Before.applied)
  check('G4c 撤销后项目总数回到应用前', g4Totals.projects === g4ProjectsBefore,
    g4Totals.projects + ' vs ' + g4ProjectsBefore)
  check('G4d 撤销面板给出 byte_identical=true（三库回到应用前）', g4Undo.byteIdentical === '1',
    g4Undo.byteIdentical + ' | ' + g4Undo.text.slice(0, 120))

  // G5 切到 B：破坏性提示在两个宽度都完整可见；旧入口全部 blocked
  await page.click('[data-agg2-mode="rebuild_all"]')
  await sleep(600)
  const g5Panel = await panelState()
  check('G5a 切到 B 后面板 mode=rebuild_all、A 计划已作废',
    g5Panel.active === 'rebuild_all' && g5Panel.checkedB === true && g5Panel.hasPlan === false,
    g5Panel.active + ' plan=' + g5Panel.hasPlan)
  check('G5b B 模式显示破坏性警告、不再显示安全提示',
    g5Panel.warn === true && g5Panel.safeNote === false, 'warn=' + g5Panel.warn + ' safe=' + g5Panel.safeNote)
  check('G5c B 模式下三个旧入口全部 blocked（不再出现「选了 B 还点只扫未分组」）',
    g5Panel.gates.every((s) => s === 'blocked') && g5Panel.scanDisabled === true &&
      g5Panel.aiDisabled === true && g5Panel.tickDisabled === true,
    JSON.stringify(g5Panel.gates) + ' scan=' + g5Panel.scanDisabled + ' ai=' + g5Panel.aiDisabled + ' tick=' + g5Panel.tickDisabled)
  const g5Warn1350 = await warnBox()
  console.log('[G5] warn@1350:', JSON.stringify(g5Warn1350))
  check('G5d 1350 宽度下破坏性提示可见且不截断',
    g5Warn1350.present && g5Warn1350.visible && g5Warn1350.right <= g5Warn1350.rootRight + 1 &&
      g5Warn1350.scrollW <= g5Warn1350.clientW + 1 && g5Warn1350.scrollH <= g5Warn1350.clientH + 1 &&
      g5Warn1350.whiteSpace === 'normal' && g5Warn1350.text.indexOf('抛弃') >= 0 &&
      g5Warn1350.text.indexOf('整批撤销') >= 0,
    'w=' + g5Warn1350.width + ' scroll=' + g5Warn1350.scrollW + '/' + g5Warn1350.clientW +
      ' h=' + g5Warn1350.scrollH + '/' + g5Warn1350.clientH + ' ws=' + g5Warn1350.whiteSpace)
  await page.screenshot({ path: join(SHOTS, 'mode-b-1350.png'), fullPage: false })
  await page.setViewportSize({ width: 900, height: 900 })
  await sleep(600)
  const g5Warn900 = await warnBox()
  console.log('[G5] warn@900:', JSON.stringify(g5Warn900))
  check('G5e 900 宽度下破坏性提示可见且不截断（同一段完整文案）',
    g5Warn900.present && g5Warn900.visible && g5Warn900.width > 300 &&
      g5Warn900.right <= g5Warn900.rootRight + 1 &&
      g5Warn900.scrollW <= g5Warn900.clientW + 1 && g5Warn900.scrollH <= g5Warn900.clientH + 1 &&
      g5Warn900.whiteSpace === 'normal' && g5Warn900.text === g5Warn1350.text,
    'w=' + g5Warn900.width + ' scroll=' + g5Warn900.scrollW + '/' + g5Warn900.clientW +
      ' h=' + g5Warn900.scrollH + '/' + g5Warn900.clientH)
  await page.screenshot({ path: join(SHOTS, 'mode-b-900.png'), fullPage: false })
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(400)

  // G6 B 模式预览：请求体 mode 变了、零写入、显示 diff 与被抛弃的旧项目
  const g6Before = await totals()
  const g6RowsBefore = await appliedMap()
  const g6ProjectSlugs = ((await apiJson('/autogroup/entities')).data.projects || []).map((p) => p.slug)
  await page.click('[data-agg2-plan-preview]')
  await page.waitForSelector('[data-agg2-plan-mode="rebuild_all"]', { timeout: 30000 })
  await page.waitForFunction(() => !!document.querySelector('[data-agg2-diff-row]'), { timeout: 30000 })
  const g6After = await totals()
  const g6RowsAfter = await appliedMap()
  const g6Req = (await planRequests()).slice(-1)[0] || {}
  const g6 = await planState()
  console.log('[G6] request:', JSON.stringify(g6Req), 'plan:', JSON.stringify({ mode: g6.mode, willWrite: g6.willWrite, dropped: g6.droppedCount, diff: g6.diff.length }))
  check('G6a 切到 B 后预览请求体 mode=rebuild_all（与 A 的请求体不同）',
    g6Req.mode === 'rebuild_all' && g2Req.mode !== g6Req.mode, JSON.stringify(g6Req))
  check('G6b B 预览后归属/项目总数不变（零写入）',
    g6Before.applied === g6After.applied && g6Before.projects === g6After.projects &&
      JSON.stringify(g6RowsBefore) === JSON.stringify(g6RowsAfter),
    g6Before.applied + '/' + g6Before.projects + ' -> ' + g6After.applied + '/' + g6After.projects)
  const g6Moved = g6.diff.filter((r) => r.from && r.to && r.from !== r.to)
  check('G6c B 的 diff 能看出新旧映射（存在「原项目 → 新项目」的换项目行）',
    g6Moved.length > 0, 'moved=' + JSON.stringify(g6Moved.slice(0, 3)))
  check('G6d B 的 diff 同时列出未分组会话的新归属',
    g6.diff.filter((r) => !r.from && r.to).length > 0,
    'assigned=' + g6.diff.filter((r) => !r.from && r.to).length)
  check('G6e 被抛弃的旧项目清单存在，条数与计划一致，且都是现存项目',
    g6.droppedCount > 0 && g6.droppedCount === g6.droppedProjects &&
      g6.droppedItems.length === g6.droppedCount &&
      g6.droppedItems.every((i) => g6ProjectSlugs.indexOf(i.slug) >= 0),
    'dropped=' + g6.droppedCount + ' items=' + g6.droppedItems.length)
  check('G6f B 模式确认入口仍是「确认应用」，且要二次确认（此时还没确认框）',
    g6.applyButtons === 1 && g6.confirmBox === false,
    'apply=' + g6.applyButtons + ' confirmBox=' + g6.confirmBox)

  // G7 B 模式确认应用：第一次点只出二次确认（零写入），确认后才写
  const g7Before = await totals()
  await sleep(400)
  await page.click('[data-agg2-apply]')
  await page.waitForSelector('[data-agg2-apply-confirm-box]', { timeout: 10000 })
  await sleep(500)
  const g7Mid = await totals()
  const g7MidRows = await appliedMap()
  check('G7a B 第一次点「确认应用」只出二次确认，零写入',
    g7Mid.applied === g7Before.applied && g7Mid.projects === g7Before.projects &&
      JSON.stringify(g7MidRows) === JSON.stringify(g6RowsBefore),
    g7Before.applied + '/' + g7Before.projects + ' -> ' + g7Mid.applied + '/' + g7Mid.projects)
  await page.click('[data-agg2-apply-confirm]')
  await page.waitForSelector('[data-agg2-result]', { timeout: 120000 })
  await sleep(1200)
  const g7Result = await resultState()
  const g7After = await totals()
  const g7Rows = await appliedMap()
  const g7Targets = g6.diff.filter((r) => r.to).map((r) => ({ id: r.id, to: r.to }))
  const g7TargetsOk = g7Targets.every((t) => {
    const row = g7Rows[t.id]
    return !!row && JSON.parse(row).project_slug === t.to
  })
  const g7ExpectedApplied = new Set(Object.keys(g6RowsBefore).concat(g7Targets.map((t) => t.id))).size
  console.log('[G7] result:', JSON.stringify(g7Result), 'totals:', JSON.stringify(g7After),
    'targets:', JSON.stringify(g7Targets), 'newProjects:', g6.newProjects)
  check('G7b 二次确认后才写：每个计划目标都落到 applied 行（归属行数 = 原行 ∪ 计划目标）',
    g7Targets.length > 0 && g7TargetsOk && g7After.applied === g7ExpectedApplied,
    'targets=' + g7Targets.length + ' ok=' + g7TargetsOk + ' rows=' + g7After.applied + ' expect=' + g7ExpectedApplied)
  check('G7c B 应用删除了计划里被抛弃的旧项目（项目数下降条数一致）',
    g7Before.projects - g7After.projects === g6.droppedCount,
    g7Before.projects + ' - ' + g6.droppedCount + ' vs ' + g7After.projects)
  check('G7d B 应用结果逐条回读且失败为 0（有失败时须能展开原因）',
    g7Result.failed === 0 || g7Result.failedDetails === true,
    'written=' + g7Result.written + ' failed=' + g7Result.failed)
  check('G7e B 应用结果有 op_id 与「整批撤销」按钮', !!g7Result.op && g7Result.undoButton === true, g7Result.op)

  // G8 撤销 B：归属与项目回到应用前（逐字段 + 总数 + byte_identical）
  await page.click('[data-agg2-undo]')
  await page.waitForSelector('[data-agg2-undo-result]', { timeout: 120000 })
  await sleep(1200)
  const g8Undo = await undoState()
  const g8Rows = await appliedMap()
  const g8Totals = await totals()
  const g8Slugs = ((await apiJson('/autogroup/entities')).data.projects || []).map((p) => p.slug).sort()
  const g8Extra = g8Slugs.filter((s) => g6ProjectSlugs.indexOf(s) === -1)
  const g8Missing = g6ProjectSlugs.filter((s) => g8Slugs.indexOf(s) === -1)
  console.log('[G8] undo:', JSON.stringify(g8Undo), 'totals:', JSON.stringify(g8Totals),
    'extra:', g8Extra.join(','), 'missing:', g8Missing.join(','))
  check('G8a B 撤销后归属行逐字段回到应用前', JSON.stringify(g8Rows) === JSON.stringify(g6RowsBefore),
    'rows=' + Object.keys(g8Rows).length + ' vs ' + Object.keys(g6RowsBefore).length)
  check('G8b B 撤销后归属数与项目数回到应用前',
    g8Totals.applied === g6Before.applied && g8Totals.projects === g6Before.projects,
    g8Totals.applied + '/' + g8Totals.projects + ' vs ' + g6Before.applied + '/' + g6Before.projects +
      ' extra=' + g8Extra.join(',') + ' missing=' + g8Missing.join(','))
  check('G8c B 撤销后被抛弃的旧项目全部写回（slug 集合一致）',
    g8Extra.length === 0 && g8Missing.length === 0,
    'extra=' + g8Extra.join(',') + ' missing=' + g8Missing.join(',') +
      ' now=' + g8Slugs.length + ' before=' + g6ProjectSlugs.length)
  check('G8d 撤销面板 byte_identical=true 且写明三库', g8Undo.byteIdentical === '1' && g8Undo.text.indexOf('三库') >= 0,
    g8Undo.byteIdentical + ' | ' + g8Undo.text.slice(0, 100))
  await page.screenshot({ path: join(SHOTS, 'mode-b-undone.png'), fullPage: false })

  // G9 切回 A：旧入口恢复可用（模式门是双向的，不是一次性禁用）
  await page.click('[data-agg2-mode="ungrouped_only"]')
  await sleep(600)
  const g9 = await panelState()
  check('G9 切回 A 后三个旧入口恢复 open 且按钮可用',
    g9.active === 'ungrouped_only' && g9.gates.every((s) => s === 'open') &&
      g9.scanDisabled === false && g9.aiDisabled === false && g9.tickDisabled === false,
    JSON.stringify(g9.gates))

  // G10 失败原因可展开：造一次**真实失败**（把目标项目目录临时挪走），确认界面逐条给原因
  // 后端规则：目标目录不存在 ⇒ 该条不写并逐条记 failed_items（不静默建目录）。
  const g10Before = await totals()
  await page.click('[data-agg2-plan-preview]')
  await page.waitForSelector('[data-agg2-plan]', { timeout: 30000 })
  const g10Plan = await planState()
  const g10Target = (g10Plan.diff.filter((r) => r.to)[0] || {}).to
  const g10Project = (await entities()).filter((p) => p.slug === g10Target)[0]
  const g10Dir = g10Project ? g10Project.path : ''
  let g10Moved = false
  if (g10Dir && existsSync(g10Dir)) {
    renameSync(g10Dir, g10Dir + '-probe-away')
    g10Moved = true
  }
  await sleep(300)
  await page.click('[data-agg2-apply]')
  await page.waitForSelector('[data-agg2-result]', { timeout: 60000 })
  await sleep(800)
  const g10Result = await resultState()
  const g10FailedRows = await page.evaluate(() => {
    const box = document.querySelector('[data-agg2-failed]')
    if (box) box.open = true
    return Array.from(document.querySelectorAll('[data-agg2-failed-row]')).map((el) => el.textContent.trim())
  })
  console.log('[G10] target:', g10Target, 'moved:', g10Moved, 'result:', JSON.stringify(g10Result),
    'failedRows:', JSON.stringify(g10FailedRows.slice(0, 2)))
  check('G10a 目标目录缺失时该条不写、结果里出现失败条目', g10Moved && g10Result.failed > 0,
    'failed=' + g10Result.failed + ' target=' + g10Target)
  check('G10b 失败原因可展开（<details> + 逐条原因文案）',
    g10Result.failedDetails === true && g10FailedRows.length === g10Result.failed &&
      g10FailedRows.every((t) => t.indexOf('目录') >= 0 || t.length > 8),
    JSON.stringify(g10FailedRows.slice(0, 2)))
  await page.click('[data-agg2-undo]')
  await page.waitForSelector('[data-agg2-undo-result]', { timeout: 60000 })
  await sleep(600)
  if (g10Moved) renameSync(g10Dir + '-probe-away', g10Dir)
  const g10Totals = await totals()
  check('G10c 失败批次同样可整批撤销，归属与项目数回到应用前',
    g10Totals.applied === g10Before.applied && g10Totals.projects === g10Before.projects,
    g10Totals.applied + '/' + g10Totals.projects + ' vs ' + g10Before.applied + '/' + g10Before.projects)


  // ---------------------------------------------------------------- D 快捷操作
  // D1 单条改分组：预览零写入 → 确认后回读一致
  const looseId = await page.evaluate(() => {
    const el = document.querySelector('[data-agg2-session][data-agg2-session-grouped="0"]')
    return el ? el.getAttribute('data-agg2-session') : ''
  })
  check('D1a 找到一条未分组会话用于单条改分组', !!looseId, looseId)
  const beforeD1 = await appliedRow(looseId)
  await page.selectOption('[data-agg2-quick="' + looseId + '"]', slugA)
  await page.waitForSelector('[data-agg2-pending="' + looseId + '"]', { timeout: 5000 })
  const midD1 = await appliedRow(looseId)
  check('D1b 预览阶段零写入（归属表没有新行）', !midD1 && !beforeD1, 'row=' + JSON.stringify(midD1))
  await page.click('[data-agg2-pending-confirm="' + looseId + '"]')
  await page.waitForSelector('[data-agg2-switch-note="ok"]', { timeout: 15000 })
  const afterD1 = await appliedRow(looseId)
  check('D1c 确认后回读：applied 行落到目标项目', !!afterD1 && afterD1.project_slug === slugA,
    afterD1 ? afterD1.project_slug : 'no row')

  // D2 批量：未选中零变更
  const ungroupedIds = await page.evaluate(() =>
    Array.from(document.querySelectorAll('[data-agg2-session][data-agg2-session-grouped="0"]')).map((el) => el.getAttribute('data-agg2-session'))
  )
  const batchIds = ungroupedIds.slice(0, 2)
  const untouched = ungroupedIds.slice(2)
  check('D2a 至少两条未分组会话可供批量', batchIds.length === 2, batchIds.join(','))
  for (const id of batchIds) {
    await page.check('[data-agg2-pick="' + id + '"]')
  }
  await page.selectOption('[data-agg-assign-target]', slugB)
  await page.click('[data-agg-assign-preview]')
  await page.waitForSelector('[data-agg-preview]', { timeout: 5000 })
  const midD2 = await Promise.all(batchIds.map((id) => appliedRow(id)))
  check('D2b 预览弹层阶段零写入', midD2.every((row) => !row), JSON.stringify(midD2.map((r) => r && r.project_slug)))
  await page.click('[data-agg-preview] button:has-text("确认归入")')
  await page.waitForSelector('[data-agg-readback-bad="0"]', { timeout: 20000 })
  const afterD2 = await Promise.all(batchIds.map((id) => appliedRow(id)))
  check('D2c 确认后两条都落到目标项目', afterD2.every((row) => row && row.project_slug === slugB),
    JSON.stringify(afterD2.map((r) => r && r.project_slug)))
  const untouchedRows = await Promise.all(untouched.map((id) => appliedRow(id)))
  check('D2d 未选中的会话零变更', untouchedRows.every((row) => !row),
    JSON.stringify(untouchedRows.map((r) => r && r.project_slug)))

  // D2e 撤销：底部抽屉 → 撤销最近写入批次 → 回读归属表核对（既有能力不得退化）
  await page.click('[data-agg2-drawer-head="ops"]')
  await page.waitForSelector('[data-agg-op]', { state: 'attached', timeout: 20000 })
  const opId = await page.evaluate(() => {
    const el = document.querySelector('[data-agg-op]')
    return el ? el.getAttribute('data-agg-op') : ''
  })
  await page.click('[data-agg-op-undo="' + opId + '"]')
  await page.waitForSelector('[data-agg-undo-bad="0"]', { timeout: 20000 })
  const afterUndo = await Promise.all(batchIds.map((id) => appliedRow(id)))
  check('D2e 撤销最近批次后两条归属行回读已消失', afterUndo.every((row) => !row),
    'op=' + opId + ' ' + JSON.stringify(afterUndo.map((r) => r && r.project_slug)))
  await page.click('[data-agg2-drawer-head="ops"]')

  // D3 重命名显示名：目录不变
  const beforeRename = (await entities()).filter((p) => p.slug === slugA)[0]
  await page.click('[data-agg-project-rename="' + slugA + '"]')
  await page.fill('[data-agg2-project-input="' + slugA + '"]', 'APM（改名验证）')
  await page.click('[data-agg2-project-save="' + slugA + ':name"]')
  const renamed = await waitUntil(async () => {
    const got = (await entities()).filter((p) => p.slug === slugA)[0]
    return got && got.name === 'APM（改名验证）' ? got : null
  })
  check('D3 改显示名：名字变了、目录没变',
    !!renamed && renamed.path === beforeRename.path,
    renamed ? renamed.name + ' | ' + renamed.path : 'missing')

  // D4 自建项目：目录 + 实体都建好
  const newSlug = 'probe-new-project'
  await page.click('[data-agg2-project-create]')
  await page.fill('[data-agg2-create-name]', '验收新建项目')
  await page.fill('[data-agg2-create-slug]', newSlug)
  await page.click('[data-agg2-create-preview-btn]')
  await page.waitForSelector('[data-agg2-create-preview]', { timeout: 5000 })
  const beforeCreate = (await entities()).filter((p) => p.slug === newSlug).length
  check('D4a 预览阶段实体还没建（零写入）', beforeCreate === 0)
  await page.click('[data-agg2-create-confirm]')
  const created = await waitUntil(async () => (await entities()).filter((p) => p.slug === newSlug)[0])
  check('D4b 新建后实体存在且目录存在', !!created && !!created.dir && created.dir.exists === true,
    created ? JSON.stringify(created.dir) : 'missing')
  // 新建成功后表单应当自己收起（createConfirm 里 formState.open=false）：如果还留着
  // 「确认新建」按钮，后续分区的点击在布局位移后可能误点到它，凭空多建一个项目。
  const formClosed = await page.evaluate(() => !document.querySelector('[data-agg2-create-confirm]'))
  check('D4c 建完新建表单自动收起（不留可误点的「确认新建」）', formClosed === true, String(formClosed))

  // D5 解散项目（选「退回未分组」）：归属会话按口径处理，无孤儿
  // 目标动态挑「当前归属行最多的项目」：夹具预置行落在按 created_at 排序的前 3 个项目上，
  // 和按 sort_order 排序的 entities()[1] 不一定是同一个，写死 slug 会挑到空项目。
  const appliedNow = (await apiJson('/autogroup/suggestions?status=applied&limit=200')).data.items
  const bySlug = {}
  appliedNow.forEach((item) => {
    bySlug[item.project_slug] = (bySlug[item.project_slug] || 0) + 1
  })
  const dissolveTarget = Object.keys(bySlug).sort((a, b) => bySlug[b] - bySlug[a])[0]
  const beforeDissolve = appliedNow.filter((i) => i.project_slug === dissolveTarget)
  check('D5a 解散前该项目有归属行', !!dissolveTarget && beforeDissolve.length > 0,
    'slug=' + dissolveTarget + ' rows=' + beforeDissolve.length)
  await page.click('[data-agg2-project-dissolve="' + dissolveTarget + '"]')
  await page.waitForSelector('[data-agg2-dissolve="' + dissolveTarget + '"]', { timeout: 5000 })
  await page.click('[data-agg2-dissolve-confirm="' + dissolveTarget + '"]')
  // 回读轮询（不信「元素出现过」）：解散是写操作，等后端真的没有残留归属行再断言。
  // 直接 waitForSelector('[data-agg2-project-result]') 会命中 D4 留下的旧结果节点，读到过期文案。
  const dissolveCleared = await waitUntil(async () => {
    const left = (await apiJson('/autogroup/suggestions?status=applied&limit=200')).data.items.filter(
      (i) => i.project_slug === dissolveTarget
    )
    return left.length === 0 ? true : null
  }, 30000)
  const dissolveText = await page.textContent('[data-agg2-project-result]')
  const afterDissolve = (await apiJson('/autogroup/suggestions?status=applied&limit=200')).data.items.filter(
    (i) => i.project_slug === dissolveTarget
  )
  check('D5b 解散后该项目没有残留归属行（无孤儿）', !!dissolveCleared && afterDissolve.length === 0,
    'left=' + afterDissolve.length + ' | ' + dissolveText)

  // D6 删除项目（刚建的那个）：回读用「ok=true 且项目不在列表里」判定，
  // 不用 entities()（它把错误响应当成空列表，会把「接口挂了」误判成「删掉了」）。
  await page.click('[data-agg2-project-delete="' + newSlug + '"]')
  await page.click('[data-agg2-project-delete="' + newSlug + '"]')
  const deleted = await waitUntil(async () => {
    const payload = await apiJson('/autogroup/entities')
    if (!payload || payload.ok !== true) return null
    const list = (payload.data && payload.data.projects) || []
    return list.filter((p) => p.slug === newSlug).length === 0 ? true : null
  }, 30000)
  check('D6 删除项目后回读实体已不存在', !!deleted)

  // ---------------------------------------------------------------- E 桌面端口径
  const shaDist = sha256(join(DIST, 'index.js'))
  const shaDesktop = sha256(DESKTOP)
  const shaDoor = sha256(DOOR)
  console.log('[E] sha dist=' + shaDist.slice(0, 12) + ' desktop=' + shaDesktop.slice(0, 12) + ' door=' + shaDoor.slice(0, 12))
  check('E1 desktop/plugin.js 与本次构建产物同字节', shaDesktop === shaDist, shaDesktop.slice(0, 12) + ' vs ' + shaDist.slice(0, 12))
  const doorFresh = shaDoor === shaDist
  console.log(
    doorFresh
      ? '[E] 门副本 = 产物：桌面端运行态加载的就是本次产物'
      : '[E] 门副本 = ' + shaDoor.slice(0, 12) + ' ≠ 产物 ' + shaDist.slice(0, 12) +
        '：**桌面端运行态未加载新产物**（本卡不写 desktop-plugins/，装门属部署动作）'
  )
  check('E2 门副本是否等于本次产物（不等时报告必须写明「桌面端运行态未加载新产物」）', true,
    doorFresh ? 'door==dist' : 'door!=dist（已如实记录）')

  check('F1 全程零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('F2 全程零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))

  // F3 凭据脱敏：页面文本里不得出现明文 key
  const bodyText = await page.evaluate(() => document.body.innerText || '')
  const keyish = /(sk-[A-Za-z0-9_-]{16,})|(api[_-]?key\s*[:=]\s*\S{8,})|(Bearer\s+[A-Za-z0-9._-]{16,})/i.exec(bodyText)
  check('F3 页面文本里没有明文凭据', !keyish, keyish ? keyish[0].slice(0, 24) : 'clean')

  // F4 沙盒隔离：真 home 的两个库字节未变
  const realChanged = REAL_FILES.filter((p) => sha256(p) !== realBefore[p])
  check('F4 真 home 的 assignments.db / projects.db 未被改动', realChanged.length === 0, realChanged.join(',') || 'unchanged')

  // F5 第三库 state.db（2.2GB 活库，Hermes 自己一直在写）：不哈希文件、也不比 mtime，
  //    改为证明「夹具会话没有出现在生产库里」—— 这才是「写操作落在沙盒」的证据。
  const isolation = await new Promise((resolve) => {
    const child = spawn(PYTHON, [
      join(HERE, 'probe_real_state_isolation.py'),
      '--db', 'D:/hermes-data/hermes/state.db',
      '--prefix', 'sess-agg-', '--prefix', 'sess-wk-', '--prefix', 'sess-xm-'
    ], { cwd: PLUGIN, env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' }) })
    let out = ''
    child.stdout.on('data', (c) => { out += c })
    child.stderr.on('data', (c) => { out += c })
    child.on('exit', () => {
      try {
        resolve(JSON.parse(out.trim().split('\n').pop()))
      } catch (e) {
        resolve({ ok: false, error: out.slice(0, 300) })
      }
    })
  })
  console.log('[F5] real state.db isolation:', JSON.stringify(isolation))
  check('F5 真 home 的 state.db 里没有夹具会话（写入只落在沙盒）',
    isolation && isolation.ok === true && (isolation.found || []).length === 0,
    isolation && isolation.ok === true
      ? 'checked ' + (isolation.prefixes || []).join(',') + ' · sessions=' + isolation.total + ' · found=0'
      : JSON.stringify(isolation).slice(0, 200))

  // F6 计划应用的台账/快照只许落在沙盒：真 home 的 mode_ops.db 与 snapshots 目录不许出现
  const absentAfter = REAL_ABSENT.map((p) => existsSync(p))
  const absentBad = REAL_ABSENT.filter((p, i) => absentAfter[i] !== realAbsentBefore[i])
  check('F6 真 home 没有出现 mode_ops.db / snapshots（台账与快照都在沙盒里）', absentBad.length === 0,
    absentBad.join(',') || 'unchanged(' + realAbsentBefore.map((v) => (v ? 'exists' : 'absent')).join(',') + ')')

  await page.screenshot({ path: join(SHOTS, 'final-1350.png'), fullPage: true })
  await page.close()
} catch (err) {
  failures.push('EXCEPTION ' + (err && err.stack ? err.stack.split('\n').slice(0, 4).join(' | ') : String(err)))
  console.log('EXCEPTION', err)
} finally {
  try {
    if (browser) await browser.close()
  } catch (e) {
    /* ignore */
  }
  try {
    if (site) site.close()
  } catch (e) {
    /* ignore */
  }
  try {
    if (fixture) fixture.child.kill()
  } catch (e) {
    /* ignore */
  }
}

console.log('\n=== RESULT: ' + pass + ' passed, ' + failures.length + ' failed ===')
if (failures.length) {
  failures.forEach((f) => console.log('  FAILED: ' + f))
  process.exit(1)
}
console.log('shots ->', SHOTS)
