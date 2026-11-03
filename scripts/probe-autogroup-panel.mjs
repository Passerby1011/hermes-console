/**
 * hermes-console · 项目分组面板「写操作」真浏览器验收 + 四张桌面截图（t_15df3606）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-autogroup-panel.mjs
 *     node plugins/hermes-console/scripts/probe-autogroup-panel.mjs --shots D:/tmp/shots
 *
 * 三层都是真的（与 probe-models-batch.mjs 同一套路）：
 *
 *   1. **真产物**：``dashboard/dist/index.js``（= ``desktop/plugin.js``）原样加载，只套一层 harness 入口；
 *   2. **真后端**：``scripts/autogroup_ui_fixture.py`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``（12 个域全挂上），home 指向**沙盒**：真 projects.db 副本 +
 *      合成 state.db / assignments.db + 插件树镜像。真 home 的 assignments.db / projects.db 用
 *      sha256 盯住，结尾逐字节对比；
 *   3. **真浏览器**：Playwright + Chromium(msedge)，1440×900。
 *
 * 断言（对应卡面验收项）：
 *   A. 项目管理：新建 / 重命名 / 删除（两次点击确认）都回读项目表，成功与失败都给话；
 *   B. 自动模式：开关能开能暂停，last_tick 来自后端；关着时 tick 零副作用；
 *   C. 批量归入：多选 → **预览弹层**（此时零写入，用 API 复核）→ 用户点确认 → 逐条回读；
 *   D. 撤销：列出最近批次 → 撤销 → 重新回读归属表核对是否还原（不认 200 当成功）；
 *   E. 内联切换：单条下拉切换 → 回读状态 / 错误；
 *   F. 全程零 console error / 零 pageerror。
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
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'autogroup-panel')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-agg-panel-'))
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
    requests.push({ method, path, status: res.status })
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
<title>hermes-console · 项目分组（写操作验收夹具）</title>
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

function startFixture() {
  const child = spawn(PYTHON, [join(HERE, 'autogroup_ui_fixture.py'), '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' })
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
  const url = 'http://127.0.0.1:' + FIXTURE_PORT + API + '/health'
  for (let i = 0; i < 160; i += 1) {
    try {
      const res = await fetch(url)
      if (res.ok) return true
    } catch (e) {
      /* 还没起来 */
    }
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

  // 真 home 指纹：结尾逐字节对比，证明沙盒隔离真的隔离
  const realFiles = [join(REAL_HOME, 'auto-group', 'assignments.db'), join(REAL_HOME, 'projects.db')]
  const realBefore = {}
  realFiles.forEach((p) => {
    realBefore[p] = sha256(p)
  })
  const realAutoMode = join(REAL_HOME, 'auto-group', 'auto_mode.json')
  const realAutoModeBefore = existsSync(realAutoMode)

  site = await startSite()
  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
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
  await page.waitForSelector('[data-agg-projects]', { timeout: 30000 })

  /** 截图：先把目标块滚进视口再整屏截；给了 selector 就额外留一张元素图（看清每一行）。 */
  const shot = async (name, selector) => {
    if (selector) {
      const handle = await page.$(selector)
      if (handle) {
        await handle.scrollIntoViewIfNeeded()
        await sleep(250)
      }
    }
    await page.screenshot({ path: join(SHOTS, name) })
    if (selector) {
      const handle = await page.$(selector)
      if (handle) {
        const box = await handle.boundingBox()
        if (box && box.height > 80) {
          await handle.screenshot({ path: join(SHOTS, name.replace(/\.png$/, '-detail.png')) })
        }
      }
    }
  }

  /** 等某块结果行出现指定文案（后端回读的提示），超时返回空串。 */
  const waitNote = async (name, needle, timeout) => {
    const started = Date.now()
    const limit = timeout || 20000
    while (Date.now() - started < limit) {
      const text = await page.textContent('[data-agg-note="' + name + '"]').catch(() => '')
      if (text && text.indexOf(needle) >= 0) return text.trim()
      await sleep(150)
    }
    return ''
  }

  const slugOfRow = (text) =>
    page.evaluate((needle) => {
      const rows = Array.from(document.querySelectorAll('[data-agg-project]'))
      const hit = rows.filter((node) => (node.textContent || '').indexOf(needle) >= 0)[0]
      return hit ? hit.getAttribute('data-agg-project') : ''
    }, text)

  // ---- A. 项目管理：新建 / 重命名 / 删除 -----------------------------------
  console.log('\n[A] 项目管理：新建 / 重命名 / 删除（每步都回读项目表）')
  await page.waitForSelector('[data-agg-project]', { timeout: 30000 })
  const projectCount0 = Number(await page.getAttribute('[data-agg-projects]', 'data-agg-projects'))
  check('项目列表渲染出来（真 projects.db 的 17 个项目）', projectCount0 >= 17, 'count=' + projectCount0)

  await page.fill('[data-agg-project-name]', '面板验收项目')
  await page.fill('[data-agg-project-keywords]', '验收,面板')
  await page.click('[data-agg-project-create]')
  const createNote = await waitNote('projects', '已新建')
  check('★新建项目成功并回读（提示写明回读项目表）', !!createNote, createNote.slice(0, 120))
  await sleep(400)
  const createdSlug = await slugOfRow('面板验收项目')
  check('★新项目出现在列表里（GET /autogroup/projects 回读）', !!createdSlug, createdSlug)
  const createdApi = await api('/autogroup/projects')
  check(
    '★后端项目表里真的有这个 slug',
    (createdApi.data.projects || []).some((p) => p.slug === createdSlug),
    createdSlug
  )

  await page.click('[data-agg-project-rename="' + createdSlug + '"]')
  await page.fill('[data-agg-project-rename-input="' + createdSlug + '"]', '面板验收项目-改名')
  await page.click('[data-agg-project-rename-save="' + createdSlug + '"]')
  const renameNote = await waitNote('projects', '已改名')
  check('★重命名成功并回读', !!renameNote, renameNote.slice(0, 120))
  await sleep(400)
  const renamedApi = await api('/autogroup/projects')
  const renamedRow = (renamedApi.data.projects || []).filter((p) => p.slug === createdSlug)[0] || {}
  check('★后端项目名已变（回读为准）', renamedRow.name === '面板验收项目-改名', String(renamedRow.name))

  await page.fill('[data-agg-project-name]', '面板临时删除项')
  await page.click('[data-agg-project-create]')
  await waitNote('projects', '已新建')
  await sleep(400)
  const tempSlug = await slugOfRow('面板临时删除项')
  check('临时项目已建好（删除测试用）', !!tempSlug, tempSlug)
  await page.click('[data-agg-project-delete="' + tempSlug + '"]')
  await sleep(250)
  check('删除要点两次：第一次只进确认态（项目还在）', !!(await page.$('[data-agg-project="' + tempSlug + '"]')))
  await page.click('[data-agg-project-delete="' + tempSlug + '"]')
  await sleep(2500)
  const delNoteText = (await page.textContent('[data-agg-note="projects"]').catch(() => '')) || ''
  const delNote = /已删除/.test(delNoteText) ? delNoteText.trim() : ''
  check('★删除成功并回读（提示写明项目表里已没有它）', !!delNote, delNoteText.trim().slice(0, 200))
  await sleep(400)
  check('★删除后列表里没有这一行', !(await page.$('[data-agg-project="' + tempSlug + '"]')))
  const afterDel = await api('/autogroup/projects')
  check(
    '★后端项目表里也没有它',
    !(afterDel.data.projects || []).some((p) => p.slug === tempSlug),
    tempSlug
  )
  await shot('01-projects.png', '[data-agg-projects]')
  console.log('        截图 01-projects.png（项目列表：新建/改名/删除都回读过）')

  // ---- B. 自动模式：开关 / 暂停 / last_tick ---------------------------------
  console.log('\n[B] 自动模式：能开能暂停，last_tick 来自后端')
  check('默认关闭（后端 enabled=false）', (await page.getAttribute('[data-agg-auto-state]', 'data-agg-auto-state')) === 'off')
  await page.click('[data-agg-auto-tick]')
  const skipNote = await waitNote('auto', '跳过')
  check('关着时 tick 直接跳过（零副作用）', !!skipNote, skipNote.slice(0, 100))

  await page.click('[data-agg-auto-toggle]')
  const onNote = await waitNote('auto', '已开启')
  check('★开启自动模式并回读 enabled', !!onNote, onNote.slice(0, 140))
  await sleep(400)
  check('★界面状态切到 on', (await page.getAttribute('[data-agg-auto-state]', 'data-agg-auto-state')) === 'on')

  await page.click('[data-agg-auto-tick]')
  const tickNote = await waitNote('auto', '扫了')
  check('开着时 tick 按阈值直接归入（不再是「只产建议」）', !!tickNote && /直接归入/.test(tickNote),
    tickNote.slice(0, 140))
  const tickApplied = await api('/autogroup/suggestions?status=applied&limit=200')
  check('★开着时 tick 真的写入了 applied（卡面 B7）', (tickApplied.data.total || 0) > 0,
    'applied=' + tickApplied.data.total)
  const daemonChip = await page.getAttribute('[data-agg-auto-daemon]', 'data-agg-auto-daemon')
  check('★开启后守护进程被拉起来（周期触发不是靠人点）', daemonChip === 'on', String(daemonChip))
  await sleep(700)
  const lastTick = await page.getAttribute('[data-agg-auto-last-tick]', 'data-agg-auto-last-tick')
  check('★显示后端记的上次 tick 时间', !!lastTick && Number(lastTick) > 0, String(lastTick))
  const autoApi = await api('/autogroup/auto')
  check(
    '★后端状态与界面一致（enabled=true 且 last_tick 非空）',
    autoApi.data.enabled === true && !!autoApi.data.last_tick,
    JSON.stringify({ enabled: autoApi.data.enabled, last_tick: autoApi.data.last_tick })
  )

  await page.click('[data-agg-auto-toggle]')
  const offNote = await waitNote('auto', '已暂停')
  check('★能随时暂停（回读 enabled=false）', !!offNote, offNote.slice(0, 140))
  await sleep(400)
  check('★暂停后界面状态切回 off', (await page.getAttribute('[data-agg-auto-state]', 'data-agg-auto-state')) === 'off')
  await shot('05-auto-mode.png', '[data-agg-auto]')
  // 收掉沙盒里的守护进程：后面 C/D 段要在确定的 suggested 列表上跑，别让它后台继续 tick
  try {
    const beat = JSON.parse(readFileSync(join(SANDBOX, 'auto-group', 'auto_daemon.json'), 'utf8'))
    if (beat && beat.pid) process.kill(beat.pid)
    console.log('        已收掉沙盒守护进程 pid=' + beat.pid)
  } catch (e) {
    console.log('        沙盒里没有守护进程心跳（跳过收尾）')
  }


  // ---- C. 批量归入：多选 → 预览（零写入）→ 确认 → 逐条回读 ---------------
  console.log('\n[C] 批量归入：预览弹层 → 用户确认 → 逐条回读')
  await page.click('button:has-text("刷新数据")')
  await sleep(1200)
  const selectable = await page.$$('[data-agg-select]')
  check('待确认列表有可勾选的会话', selectable.length >= 2, 'rows=' + selectable.length)
  await page.click('[data-agg-select-all]')
  await sleep(250)
  const selectedCount = Number(await page.getAttribute('[data-agg-selected]', 'data-agg-selected'))
  check('★全选：已勾选条数 = 列表行数', selectedCount === selectable.length, selectedCount + '/' + selectable.length)

  const optionLabels = await page.$$eval('[data-agg-assign-target] option', (nodes) =>
    nodes.map((node) => ({ value: node.value, label: (node.textContent || '').trim() }))
  )
  const targetOption = optionLabels[optionLabels.length - 1]
  const targetSlug = targetOption.value
  const targetName = targetOption.label
  await page.selectOption('[data-agg-assign-target]', targetSlug)
  await sleep(250)

  const appliedBefore = await api('/autogroup/suggestions?status=applied&limit=200')
  const suggestedBefore = await api('/autogroup/suggestions?status=suggested&limit=200')
  const movedBefore = (suggestedBefore.data.items || []).filter((item) => item.project_slug === targetSlug).length
  await page.click('[data-agg-assign-preview]')
  await page.waitForSelector('[data-agg-preview]', { timeout: 20000 })
  await sleep(350)
  const previewRows = await page.$$('[data-agg-preview-row]')
  check('★预览弹层逐条列出 from→to', previewRows.length === selectedCount, previewRows.length + '/' + selectedCount)
  const previewText = (await page.textContent('[data-agg-preview]')).replace(/\s+/g, ' ')
  check(
    '预览里写明目标项目与「还没写」',
    previewText.indexOf(targetName) >= 0 && previewText.indexOf('还没写') >= 0,
    previewText.slice(0, 140)
  )
  await shot('02-assign-preview.png')
  console.log('        截图 02-assign-preview.png（预览弹层，此时一条都没写）')

  const appliedDuringPreview = await api('/autogroup/suggestions?status=applied&limit=200')
  check(
    '★★预览（未确认）时后端 applied 行数没变：确认前一条都没写',
    (appliedDuringPreview.data.total || 0) === (appliedBefore.data.total || 0),
    appliedBefore.data.total + ' → ' + appliedDuringPreview.data.total
  )
  const suggestedDuringPreview = await api('/autogroup/suggestions?status=suggested&limit=200')
  const movedEarly = (suggestedDuringPreview.data.items || []).filter((item) => item.project_slug === targetSlug).length
  check('★预览态：目标项目下的会话数没变（没有提前归入）', movedEarly === movedBefore, movedBefore + ' → ' + movedEarly)

  await page.click('[data-agg-preview-cancel]')
  await sleep(300)
  check('取消预览：弹层关掉，什么都不写', !(await page.$('[data-agg-preview]')))

  await page.click('[data-agg-assign-preview]')
  await page.waitForSelector('[data-agg-preview-confirm]', { timeout: 20000 })
  await page.click('[data-agg-preview-confirm]')
  await page.waitForSelector('[data-agg-readback]', { timeout: 30000 })
  await sleep(600)
  const readbackRows = await page.$$eval('[data-agg-readback-row]', (nodes) =>
    nodes.map((node) => ({ id: node.getAttribute('data-agg-readback-row'), ok: node.getAttribute('data-agg-readback-ok') }))
  )
  check('★逐条回读：每个勾选的会话都有一条回读结果', readbackRows.length === selectedCount, readbackRows.length + '/' + selectedCount)
  check(
    '★★回读全部一致（slug / status / batch_id 都对）',
    readbackRows.length > 0 && readbackRows.every((row) => row.ok === 'true'),
    JSON.stringify(readbackRows.filter((row) => row.ok !== 'true'))
  )
  const appliedAfter = await api('/autogroup/suggestions?status=applied&limit=200')
  const moved = (appliedAfter.data.items || []).filter((item) => readbackRows.some((row) => row.id === item.session_id))
  check(
    '★后端回读：勾选的会话都变成 applied 且落在目标项目',
    moved.length === readbackRows.length && moved.every((item) => item.project_slug === targetSlug && item.status === 'applied'),
    moved.map((item) => item.session_id + ':' + item.project_slug).join(',')
  )
  const batchIds = Array.from(new Set(moved.map((item) => item.batch_id)))
  check('★同一批次号（撤销按批次/op 走）', batchIds.length === 1 && !!batchIds[0], String(batchIds[0]))
  await shot('03-assign-readback.png', '[data-agg-readback]')
  console.log('        截图 03-assign-readback.png（归入后逐条回读）')

  // ---- D. 撤销：列出最近批次 → 撤销 → 重新回读核对 -------------------------
  console.log('\n[D] 撤销：列出最近写入批次 → 撤销 → 重新回读核对')
  const opsBefore = await api('/autogroup/ops?limit=20')
  const topOp = (opsBefore.data.ops || [])[0] || {}
  check(
    '台账倒序第一条就是刚写入的批次',
    topOp.op === 'assign' && topOp.session_count === selectedCount,
    JSON.stringify({ op: topOp.op, n: topOp.session_count, op_id: topOp.op_id })
  )
  check('撤销前这条批次还没被撤销', topOp.undone === false)
  const beforeSlugs = {}
  ;(topOp.sessions || []).forEach((row) => {
    beforeSlugs[row.session_id] = row.from
  })

  await page.evaluate(() => {
    const el = document.querySelector('[data-agg-ops]')
    if (el) el.scrollIntoView()
  })
  await sleep(300)
  check('撤销入口列出了这条批次', !!(await page.$('[data-agg-op="' + topOp.op_id + '"]')))
  await page.click('[data-agg-op="' + topOp.op_id + '"] [data-agg-op-undo]')
  await page.waitForSelector('[data-agg-undo-readback]', { timeout: 30000 })
  await sleep(700)
  const undoRows = await page.$$eval('[data-agg-undo-row]', (nodes) =>
    nodes.map((node) => ({ id: node.getAttribute('data-agg-undo-row'), ok: node.getAttribute('data-agg-undo-ok') }))
  )
  check('★撤销后逐条回读结果都在', undoRows.length === selectedCount, undoRows.length + '/' + selectedCount)
  check(
    '★★撤销后回读全部一致（逐字段还原到写入前）',
    undoRows.length > 0 && undoRows.every((row) => row.ok === 'true'),
    JSON.stringify(undoRows.filter((row) => row.ok !== 'true'))
  )
  const afterUndo = await api('/autogroup/suggestions?status=applied&limit=200')
  const stillApplied = (afterUndo.data.items || []).filter((item) => undoRows.some((row) => row.id === item.session_id))
  check('★后端回读：这批会话不再是 applied', stillApplied.length === 0, JSON.stringify(stillApplied.map((item) => item.session_id)))
  const suggestedAfterUndo = await api('/autogroup/suggestions?status=suggested&limit=200')
  const restored = (suggestedAfterUndo.data.items || []).filter((item) =>
    Object.prototype.hasOwnProperty.call(beforeSlugs, item.session_id)
  )
  check(
    '★后端回读：行已还原成写入前的项目（from）',
    restored.length === selectedCount && restored.every((item) => item.project_slug === beforeSlugs[item.session_id]),
    restored.map((item) => item.session_id + ':' + item.project_slug).join(',')
  )
  const opsAfter = await api('/autogroup/ops?limit=20')
  const sameOp = (opsAfter.data.ops || []).filter((op) => op.op_id === topOp.op_id)[0] || {}
  check('★台账里这条批次已标 undone', sameOp.undone === true, String(sameOp.undone_at))
  await shot('04-undo-readback.png', '[data-agg-undo-readback]')
  console.log('        截图 04-undo-readback.png（撤销后重新回读归属表）')

  // ---- E. 内联切换：单条下拉切归属 ----------------------------------------
  console.log('\n[E] 内联切换：单条下拉切项目 → 回读状态')
  await page.click('button:has-text("刷新数据")')
  await sleep(1000)
  const firstRow = await page.evaluate(() => {
    const select = document.querySelector('[data-agg-switch]')
    if (!select) return null
    return { id: select.getAttribute('data-agg-switch'), value: select.value }
  })
  check('每行都有内联项目下拉（不实现拖拽）', !!firstRow, JSON.stringify(firstRow))
  const switchOptions = await page.$$eval('[data-agg-switch] option', (nodes) =>
    nodes.map((node) => node.value).filter((value) => !!value)
  )
  const switchTarget = switchOptions.filter((value) => value !== (firstRow && firstRow.value))[0]
  await page.selectOption('[data-agg-switch="' + firstRow.id + '"]', switchTarget)
  let switchText = ''
  for (let i = 0; i < 120; i += 1) {
    // timeout 必须显式给：切换成功后这条会变 applied、离开「待确认建议」列表，
    // 元素消失时 page.textContent 默认要等满 30s 才 reject，120 轮就是 60 分钟（曾把探针挂死）。
    switchText =
      (await page.textContent('[data-agg-switch-note="' + firstRow.id + '"]', { timeout: 1500 }).catch(() => '')) || ''
    if (switchText) break
    await sleep(150)
  }
  const rowStillListed = !!(await page.$('[data-session-id="' + firstRow.id + '"]'))
  check(
    '★切换后给出实际状态（或者行已因归入离开建议列表）',
    /已切到/.test(switchText) || !rowStillListed,
    switchText.slice(0, 140) || (rowStillListed ? '行还在但没有状态文案' : '行已离开建议列表（= 已写入）')
  )

  const afterSwitch = await api('/autogroup/suggestions?status=applied&limit=200')
  const switched = (afterSwitch.data.items || []).filter((item) => item.session_id === firstRow.id)[0] || {}
  check(
    '★后端回读：这条会话真的换到新项目（applied）',
    switched.project_slug === switchTarget && switched.status === 'applied',
    switched.project_slug + ' / ' + switched.status + ' vs ' + switchTarget
  )
  await shot('06-inline-switch.png', '[data-session-id="' + firstRow.id + '"]')

  // ---- F. 收尾：零报错 + 真 home 隔离 -------------------------------------
  check('全程零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('全程零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  realFiles.forEach((file) => {
    const same = sha256(file) === realBefore[file]
    check('真 home 未被写入：' + file.replace(REAL_HOME, ''), same, same ? '' : 'sha256 变了')
  })
  check('真 home 没有冒出 auto_mode.json', existsSync(realAutoMode) === realAutoModeBefore)
} catch (err) {
  failures.push('异常：' + (err && err.message ? err.message : String(err)))
  console.log('  FAIL 异常：' + (err && err.stack ? err.stack : err))
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) site.close()
  if (fixture) {
    fixture.child.kill()
    console.log('\n[fixture] 尾部日志：\n' + fixture.log().split('\n').slice(-6).join('\n'))
  }
  console.log('\n结果：PASS ' + pass + ' / FAIL ' + failures.length)
  if (failures.length) {
    failures.forEach((item) => console.log(' - ' + item))
    process.exitCode = 1
  } else {
    console.log('ALL-PASS  shots=' + SHOTS.replace(/\\/g, '/'))
  }
}



