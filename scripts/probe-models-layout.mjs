/**
 * hermes-console · 模型分区「布局整改」真浏览器验收 + 改前/改后同尺寸截图
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-models-layout.mjs            # 全量交互验收
 *     node plugins/hermes-console/scripts/probe-models-layout.mjs --shots-only --tag before
 *     node plugins/hermes-console/scripts/probe-models-layout.mjs --shots-only --tag after
 *
 * 三层都是真的（与 probe-models-batch.mjs 同一套路）：
 *
 *   1. **真产物**：``dashboard/dist/index.js``（= ``desktop/plugin.js``）原样加载，只套一层 harness 入口。
 *   2. **真后端**：``scripts/models_batch_fixture.py --sandbox`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``，``HERMES_HOME`` 指向**沙盒副本**。写操作落在副本上，
 *      真 profile 一个字节都不动（本探针用 sha256 盯住前后）。
 *   3. **真浏览器**：Playwright + Chromium(msedge)。
 *
 * 截图口径（验收要的「同尺寸改前/改后」）：1350×900 与 900×900 各一张整屏 + 一张
 * 主体（Agent 配置表）元素图。``--shots-only`` 只出图，不跑交互 —— 改前改后共用同一支脚本，
 * 尺寸与取景完全一致，才谈得上「同尺寸对比」。
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
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
const FIXTURE_PORT = Number(process.env.HC_MODELS_FIXTURE_PORT || 9195)
const SITE_PORT = Number(process.env.HC_MODELS_SITE_PORT || 9194)
const API = '/api/plugins/hermes-console'

const argOf = (name, fallback) => {
  const i = process.argv.indexOf(name)
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}
const SHOTS_ONLY = process.argv.includes('--shots-only')
const TAG = argOf('--tag', 'run')
const SHOTS = resolve(argOf('--shots', join(PLUGIN, 'scripts', '_probe_out', 'models-layout')))

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-models-layout-'))
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
window.__hcStore = store
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
<title>hermes-console · 模型（布局验收夹具）</title>
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
  // PYTHONPATH 指向别的解释器的 site-packages 时，会把 venv 自己的 pydantic_core 盖掉
  // （cp314 的 .pyd 给 3.11 用 ⇒ ImportError: cannot import name 'ArgsKwargs'）。夹具只要自己那套包。
  const childEnv = Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' })
  delete childEnv.PYTHONPATH
  const child = spawn(PYTHON, [join(HERE, 'models_batch_fixture.py'), '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: childEnv
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
  for (let i = 0; i < 120; i += 1) {
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

const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

/** 整屏 + 主体元素两张（同尺寸口径：1350 / 900 各一套）。 */
async function layoutShots(page, prefix) {
  const out = []
  for (const width of [1350, 900]) {
    await page.setViewportSize({ width, height: 900 })
    await sleep(600)
    const info = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      bodyScrollWidth: document.body.scrollWidth
    }))
    const overflow = info.scrollWidth > info.innerWidth + 1 || info.bodyScrollWidth > info.innerWidth + 1
    if (!SHOTS_ONLY) {
      check(`★${width}px 无横向溢出（scrollWidth ≤ innerWidth）`, !overflow,
        `${info.scrollWidth} / ${info.bodyScrollWidth} vs ${info.innerWidth}`)
    } else {
      console.log(`  [shot] ${width}px  overflow=${overflow}  (scrollWidth=${info.scrollWidth} innerWidth=${info.innerWidth})`)
    }
    const file = join(SHOTS, `${prefix}-${width}.png`)
    await page.screenshot({ path: file, fullPage: true })
    out.push(file)
    console.log('  [shot] ' + file)
    const main = await page.$('[data-models-main]')
    if (main) {
      const box = await main.boundingBox()
      if (box && box.height > 60) {
        const detail = join(SHOTS, `${prefix}-${width}-main.png`)
        await main.screenshot({ path: detail })
        out.push(detail)
        console.log('  [shot] ' + detail)
      }
    }
  }
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(400)
  return out
}

// ---------------------------------------------------------------------------
// 交互验收（本卡 C + D）：单/批量共用编辑器、「保持不变」不覆盖、预览=实际、回读、回滚
// ---------------------------------------------------------------------------

/** 解析 config.yaml 的 model: 段（只取本分区关心的 5 个键）。 */
function modelFields(text) {
  const lines = String(text).split(/\r?\n/)
  const start = lines.findIndex((l) => /^model:\s*$/.test(l))
  const out = {}
  if (start < 0) return out
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i]
    if (!/^\s/.test(line)) break
    const m = /^\s+([A-Za-z0-9_]+):\s*(.*)$/.exec(line)
    if (m) out[m[1]] = m[2].trim().replace(/^['"]|['"]$/g, '')
  }
  return out
}

/** fallback_providers 段的原文（逐字段断言用：scope=model 的写入不许动它）。 */
function fallbackRaw(text) {
  const lines = String(text).split(/\r?\n/)
  const start = lines.findIndex((l) => /^fallback_providers:/.test(l))
  if (start < 0) return ''
  let end = lines.length
  for (let i = start + 1; i < lines.length; i += 1) {
    if (!/^\s/.test(lines[i]) && lines[i].trim()) {
      end = i
      break
    }
  }
  return lines.slice(start, end).join('\n')
}

const sleep2 = (ms) => new Promise((r) => setTimeout(r, ms))

try {
  console.log('[env] dist   :', DIST)
  console.log('[env] shots  :', SHOTS)
  console.log('[env] sandbox:', SANDBOX)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)

  // ---- 沙盒配置指纹（9 份：root + 每个 profile） -------------------------
  const profileNames = existsSync(join(SANDBOX, 'profiles'))
    ? readdirSync(join(SANDBOX, 'profiles')).filter((name) => existsSync(join(SANDBOX, 'profiles', name, 'config.yaml')))
    : []
  const cfgPaths = [join(SANDBOX, 'config.yaml')]
    .concat(profileNames.map((name) => join(SANDBOX, 'profiles', name, 'config.yaml')))
    .filter((p) => existsSync(p))
  const before = {}
  cfgPaths.forEach((p) => {
    before[p] = sha256(p)
  })
  const readAll = () => {
    const out = {}
    cfgPaths.forEach((p) => {
      out[p] = readFileSync(p, 'utf-8')
    })
    return out
  }
  const beforeText = readAll()
  /** agent id → 它的 config.yaml（root agent 叫 default，落在沙盒根上）。 */
  const pathOf = (agent) => (agent === 'default' ? join(SANDBOX, 'config.yaml') : join(SANDBOX, 'profiles', agent, 'config.yaml'))
  if (!SHOTS_ONLY) check('沙盒里有真配置副本（root + 每个 profile 各一份）', cfgPaths.length >= 5, String(cfgPaths.length))

  const REAL_HOME = 'D:/hermes-data/hermes'
  const realCfgPaths = [join(REAL_HOME, 'config.yaml')]
    .concat(profileNames.map((name) => join(REAL_HOME, 'profiles', name, 'config.yaml')))
    .filter((p) => existsSync(p))
  const realBefore = {}
  realCfgPaths.forEach((p) => {
    realBefore[p] = sha256(p)
  })

  site = await startSite()
  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })
  const page = await browser.newPage({ viewport: { width: 1350, height: 900 }, deviceScaleFactor: 1 })
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text() + (where ? ' @ ' + where : ''))
  })
  page.on('pageerror', (err) => pageErrors.push(String(err)))

  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
  await page.click('.hc-nav__item:has-text("模型")')
  await page.waitForSelector('.hc-models', { timeout: 30000 })

  if (SHOTS_ONLY) {
    await sleep2(1500)
    console.log('\n[shots] 只出图（' + TAG + '）')
    await layoutShots(page, TAG)
    check('零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
    check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  } else {
    await page.waitForSelector('[data-models-agent-row]', { timeout: 30000 })
    const sel = {
      rows: '[data-models-agent-row]',
      checkAll: '[data-models-select-all]',
      clear: '[data-models-select-none]',
      openEditor: '[data-models-editor-open]',
      editor: '[data-models-editor]',
      provider: '[data-models-editor-provider]',
      model: '[data-models-editor-model]',
      pick: '[data-models-editor-model-pick]',
      previewAsk: '[data-models-editor-preview-ask]',
      preview: '[data-models-preview]',
      previewRow: '[data-models-preview-row]',
      cancelPreview: '[data-models-preview-cancel]',
      applyAsk: '[data-models-editor-apply-ask]',
      applyYes: '[data-models-editor-apply-yes]',
      confirm: '[data-models-editor-confirm]',
      result: '[data-models-result]',
      resultRow: '[data-models-result-row]',
      close: '[data-models-editor-close]'
    }

    // ---- 0. 首屏结构：Agent 表是主体，没有重复表单 ------------------------
    console.log('\n[0] 首屏：工具栏 + Agent 表（无重复作用域/provider 表单）')
    const first = await page.evaluate(() => {
      const table = document.querySelector('[data-models-table]')
      const box = table ? table.getBoundingClientRect() : null
      const pane = document.querySelector('.hc-pane__inner')
      const paneBox = pane ? pane.getBoundingClientRect() : null
      const section = document.querySelector('.hc-models')
      return {
        rows: document.querySelectorAll('[data-models-agent-row]').length,
        toolbar: Boolean(document.querySelector('[data-models-toolbar]')),
        search: Boolean(document.querySelector('[data-models-search]')),
        editor: Boolean(document.querySelector('[data-models-editor]')),
        // 本分区自己的表单（shell 的全局作用域选择器不属于本卡，单独计数）
        sectionSelects: section ? section.querySelectorAll('select').length : -1,
        sectionInputs: section ? section.querySelectorAll('input').length : -1,
        shellSelects: section ? document.querySelectorAll('select').length - section.querySelectorAll('select').length : -1,
        tableWidth: box ? Math.round(box.width) : 0,
        tableHeight: box ? Math.round(box.height) : 0,
        paneWidth: paneBox ? Math.round(paneBox.width) : 0,
        viewportW: window.innerWidth,
        viewportH: window.innerHeight,
        longBlocks: Array.from(document.querySelectorAll('.hc-models .hc-muted')).map((n) => (n.textContent || '').replace(/\s/g, '').length),
        text: (document.querySelector('.hc-models') || {}).textContent || ''
      }
    })
    check('Agent 表列出全部 agent', first.rows >= 5, 'rows=' + first.rows)
    check('顶部工具栏在（标题 + 搜索 + 刷新）', first.toolbar && first.search)
    check('★默认不展开编辑器（首屏只有一张表，没有第二套表单）', !first.editor, 'editor=' + first.editor)
    check('★本分区首屏零 select（作用域/provider 重复表单已消除）', first.sectionSelects === 0,
      'sectionSelects=' + first.sectionSelects + '（外壳自己的作用域选择器 ' + first.shellSelects + ' 个，不属于本卡）')
    check('★Agent 表用满可用宽度（≥ 内容区 60%，不再只剩左侧 1/4）',
      first.tableWidth >= Math.round(first.paneWidth * 0.6),
      first.tableWidth + 'px / 内容区 ' + first.paneWidth + 'px（视口 ' + first.viewportW + 'px）')
    check('★Agent 表在首屏内可见（高度 > 200px）', first.tableHeight > 200,
      'tableHeight=' + first.tableHeight + ' viewportH=' + first.viewportH)
    check('★没有超过 300 字的大段常驻说明（说明收进 title / 次级入口）',
      first.longBlocks.every((n) => n < 300), '最长说明块 ' + Math.max.apply(null, first.longBlocks.concat([0])) + ' 字')

    // ---- 1. 单选 → 单编辑态 ---------------------------------------------
    console.log('\n[1] 单选：勾一个 → 编辑器是单 Agent 编辑态')
    const ids = await page.$$eval(sel.rows, (nodes) => nodes.map((n) => n.getAttribute('data-models-agent-row')))
    const target = ids.filter((id) => id === 'code-worker')[0] || ids[0]
    const keeper = ids.filter((id) => id !== target)[0]
    await page.click(`[data-models-row-edit="${target}"]`)
    await page.waitForSelector(sel.editor, { timeout: 10000 })
    await sleep2(400)
    const single = await page.evaluate(() => {
      const node = document.querySelector('[data-models-editor]')
      return {
        editors: document.querySelectorAll('[data-models-editor]').length,
        mode: node ? node.getAttribute('data-models-mode') : '',
        target: (document.querySelector('[data-models-editor-target]') || {}).textContent || '',
        forms: document.querySelectorAll('[data-models-editor-provider]').length,
        prefilled: (document.querySelector('[data-models-editor-provider]') || {}).value || '',
        selected: document.querySelectorAll('[data-models-agent-row][data-selected="1"]').length
      }
    })
    check('★全页只有一个编辑器（不存在上下两套重复表单）', single.editors === 1, 'editors=' + single.editors)
    check('★单选进入 single 模式且点名目标 agent',
      single.mode === 'single' && single.target.indexOf(target) >= 0, single.mode + ' / ' + single.target.slice(0, 50))
    check('单选时只勾中 1 个 agent', single.selected === 1, 'selected=' + single.selected)
    check('★单编辑态预填该 agent 的现值（不是空表单）', single.prefilled.length > 0, 'provider=' + single.prefilled)
    await page.screenshot({ path: join(SHOTS, 'editor-single.png'), fullPage: true })
    console.log('  [shot] ' + join(SHOTS, 'editor-single.png'))

    // ---- 2. 多选 → 批量态 + 默认「保持不变」 ------------------------------
    console.log('\n[2] 多选：勾两个 → 同一个编辑器切到批量态，字段默认「保持不变」')
    await page.click(sel.close)
    await sleep2(200)
    await page.click(sel.clear)
    await sleep2(200)
    await page.click(`[data-models-row-check="${target}"]`)
    await page.click(`[data-models-row-check="${keeper}"]`)
    await sleep2(250)
    const barText = await page.evaluate(() => {
      const node = document.querySelector('[data-models-selection-bar]')
      return node ? { text: node.textContent || '', count: node.getAttribute('data-models-selected') } : null
    })
    check('★多选后出现选择栏并报「已选 2 个」', Boolean(barText) && barText.count === '2', JSON.stringify(barText && barText.count))
    await page.click(sel.openEditor)
    await page.waitForSelector(sel.editor, { timeout: 10000 })
    await sleep2(400)
    const batchMode = await page.evaluate(() => ({
      mode: document.querySelector('[data-models-editor]').getAttribute('data-models-mode'),
      target: (document.querySelector('[data-models-editor-target]') || {}).textContent || '',
      fields: Array.from(document.querySelectorAll('[data-models-field-mode]')).map((n) => n.getAttribute('data-models-field-mode')),
      previewDisabled: Boolean((document.querySelector('[data-models-editor-preview-ask]') || {}).disabled)
    }))
    check('★多选进入 batch 模式（同一个编辑器，换的是目标集合）', batchMode.mode === 'batch', batchMode.mode)
    check('批量时点名「已选 N 个 agent」', /已选/.test(batchMode.target), batchMode.target.slice(0, 60))
    check('★批量默认「保持不变」（不点就不改，绝不静默覆盖）',
      batchMode.fields.length >= 2 && batchMode.fields.every((v) => v === 'keep'), JSON.stringify(batchMode.fields))
    check('★两个字段都「保持不变」时预览按钮禁用（发不出去）', batchMode.previewDisabled === true)

    // ---- 3. 全选 / 取消选择 ---------------------------------------------
    console.log('\n[3] 全选 / 取消选择')
    await page.click(sel.clear)
    await sleep2(250)
    const cleared = await page.evaluate(() => ({
      selected: document.querySelectorAll('[data-models-agent-row][data-selected="1"]').length,
      bar: Boolean(document.querySelector('[data-models-selection-bar]')),
      editor: Boolean(document.querySelector('[data-models-editor]'))
    }))
    check('★取消选择：全部取消，选择栏与编辑器一起收起',
      cleared.selected === 0 && !cleared.bar && !cleared.editor, JSON.stringify(cleared))
    await page.click(sel.checkAll)
    await sleep2(300)
    const all = await page.evaluate(() => ({
      selected: document.querySelectorAll('[data-models-agent-row][data-selected="1"]').length,
      total: document.querySelectorAll('[data-models-agent-row]').length
    }))
    check('★全选：全部 agent 都勾上', all.selected === all.total, all.selected + '/' + all.total)
    await page.click(sel.clear)
    await sleep2(200)

    // ---- 4. 「保持不变」的硬语义：provider 写入 / 模型保持不变 -----------
    console.log('\n[4] 保持不变：provider=moa 写入 + 模型保持不变 → 只改 provider')
    const selectedPair = [target, keeper]
    for (const id of selectedPair) {
      await page.click(`[data-models-row-check="${id}"]`)
      await sleep2(120)
    }
    await page.click(sel.openEditor)
    await page.waitForSelector(sel.editor, { timeout: 10000 })
    await sleep2(300)
    await page.waitForFunction(
      (s) => Boolean(document.querySelector(s + ' option[value="moa"]')),
      sel.provider,
      { timeout: 20000 }
    )
    await page.selectOption(sel.provider, 'moa')
    await sleep2(1200)
    const keepModel = await page.evaluate(() => {
      const box = document.querySelector('[data-models-editor-model-keep]')
      return { checked: box ? box.checked : null, fields: Array.from(document.querySelectorAll('[data-models-field-mode]')).map((n) => n.getAttribute('data-models-field-mode')) }
    })
    check('★只把 provider 设成写入，模型仍是「保持不变」',
      keepModel.fields.indexOf('set') >= 0 && keepModel.fields.indexOf('keep') >= 0, JSON.stringify(keepModel.fields))
    const beforeText2 = readAll()
    await page.click(sel.previewAsk)
    await page.waitForSelector(sel.preview, { timeout: 30000 })
    await sleep2(300)
    const preview = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('[data-models-preview-row]'))
      return rows.map((row) => ({
        agent: row.getAttribute('data-models-preview-row'),
        writable: row.getAttribute('data-writable'),
        changes: Array.from(row.querySelectorAll('[data-models-preview-change]')).map((c) => ({
          key: c.getAttribute('data-models-preview-change'),
          to: c.getAttribute('data-to')
        })),
        text: (row.textContent || '').replace(/\s/g, '')
      }))
    })
    check('★预览逐 agent 出表（被写到的 agent 每个一行）', preview.length >= 1, 'rows=' + preview.length)
    const previewDefault = preview[0] ? preview[0].changes.filter((c) => c.key === 'model.default') : []
    check('★「保持不变」的模型字段：预览里即使列出 model.default，其 to 也等于现值（不覆盖）',
      previewDefault.length === 0 || previewDefault.every((c) => String(c.to) === String(modelFields(beforeText2[pathOf(preview[0].agent)] || '').default)),
      JSON.stringify(preview[0] && preview[0].changes))
    check('★预览里出现 model.provider → moa', preview.every((row) => row.changes.some((c) => c.key === 'model.provider' && c.to === 'moa')),
      JSON.stringify(preview[0] && preview[0].changes))
    const afterPreview = readAll()
    check('★预览一个字节都没写（9 份 config.yaml 逐字节相同）',
      cfgPaths.every((p) => afterPreview[p] === beforeText2[p]))
    await page.screenshot({ path: join(SHOTS, 'editor-preview.png'), fullPage: true })
    console.log('  [shot] ' + join(SHOTS, 'editor-preview.png'))

    await page.click(sel.cancelPreview)
    await sleep2(300)
    const cancelled = await page.evaluate(() => ({
      preview: Boolean(document.querySelector('[data-models-preview]')),
      writes: window.__hcRequests.filter((r) => /models\/apply|batch\/apply/.test(r.path)).length
    }))
    check('★取消预览：预览表收起，且一个写请求都没发', !cancelled.preview && cancelled.writes === 0,
      'writes=' + cancelled.writes)

    // ---- 5. 真正写入（沙盒）→ 逐 agent 回读 + 逐字段断言 ------------------
    console.log('\n[5] 确认写入（沙盒）：逐 agent 回读 + 「保持不变」字段逐字段断言')
    await page.click(sel.previewAsk)
    await page.waitForSelector(sel.preview, { timeout: 30000 })
    await page.click(sel.applyAsk)
    await page.waitForSelector(sel.confirm, { timeout: 10000 })
    const confirmText = await page.evaluate(() => (document.querySelector('[data-models-editor-confirm]') || {}).textContent || '')
    check('二次确认条出现（再点一次才写）', confirmText.length > 10, confirmText.slice(0, 60))
    await page.click(sel.applyYes)
    const applied = await page
      .waitForSelector(sel.result, { timeout: 180000 })
      .then(() => true)
      .catch(() => false)
    check('★写入返回逐 agent 结果表', applied)
    const resultInfo = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('[data-models-result-row]'))
      return {
        rows: rows.map((r) => ({ agent: r.getAttribute('data-models-result-row'), ok: r.getAttribute('data-ok'), text: (r.textContent || '').replace(/\s/g, '') })),
        batchId: (document.querySelector('[data-models-result]') || {}).getAttribute
          ? document.querySelector('[data-models-result]').getAttribute('data-models-result')
          : ''
      }
    })
    check('★逐 agent 出结果且都 ok', resultInfo.rows.length >= 1 && resultInfo.rows.every((r) => r.ok === '1'),
      JSON.stringify(resultInfo.rows.map((r) => r.agent + ':' + r.ok)))
    check('★结果里有「回读 N/N 项通过」（既有回读路径无回归）',
      resultInfo.rows.every((r) => /回读\d+\/\d+项通过/.test(r.text)), resultInfo.rows[0] ? resultInfo.rows[0].text.slice(0, 70) : '')

    const afterWrite = readAll()
    // 5a. 预览值 = 实际应用值（逐项）
    const fieldOf = { 'model.provider': 'provider', 'model.default': 'default', 'model.base_url': 'base_url', 'model.api_mode': 'api_mode', 'model.key_env': 'key_env' }
    let previewMatched = 0
    let previewMismatch = []
    preview.forEach((row) => {
      const path = pathOf(row.agent)
      const fields = modelFields(afterWrite[path] || '')
      row.changes.forEach((change) => {
        const key = fieldOf[change.key]
        if (!key) return
        const actual = fields[key] === undefined ? '' : String(fields[key])
        if (actual === String(change.to)) previewMatched += 1
        else previewMismatch.push(row.agent + ' ' + change.key + ': 预览=' + change.to + ' 实际=' + actual)
      })
    })
    check('★预览值与实际写入值逐项一致', previewMismatch.length === 0 && previewMatched > 0,
      'matched=' + previewMatched + (previewMismatch.length ? ' 不符: ' + previewMismatch.join('; ') : ''))

    // 5b. 写到的 agent：provider 变了、模型（保持不变）没变
    const written = preview.map((row) => row.agent)
    const keptModelOk = written.every((agent) => {
      const path = pathOf(agent)
      const beforeFields = modelFields(beforeText[path] || '')
      const afterFields = modelFields(afterWrite[path] || '')
      return afterFields.provider === 'moa' && afterFields.default === beforeFields.default
    })
    check('★写到的 agent：provider → moa，且「保持不变」的模型与改前完全相同', keptModelOk,
      written.join(','))

    // 5c. 未被选中的 agent：一个字节都没动 + 逐字段相等
    const untouched = cfgPaths.filter((p) => !written.some((agent) => p === pathOf(agent)))
    const byteChanged = untouched.filter((p) => afterWrite[p] !== beforeText[p])
    const fieldChanged = untouched.filter((p) => {
      const a = modelFields(beforeText[p])
      const b = modelFields(afterWrite[p])
      const keys = ['provider', 'default', 'base_url', 'api_mode', 'key_env']
      return keys.some((k) => String(a[k] || '') !== String(b[k] || '')) || fallbackRaw(beforeText[p]) !== fallbackRaw(afterWrite[p])
    })
    check('★未选中的 agent 逐字节未变（' + untouched.length + ' 份）', byteChanged.length === 0,
      byteChanged.length ? JSON.stringify(byteChanged.map((p) => p.split(/[\\/]/).slice(-2)[0])) : 'sha256/字节全等')
    check('★未选中的 agent 逐字段未变（provider/default/base_url/api_mode/key_env/fallback_providers）',
      fieldChanged.length === 0, fieldChanged.length ? JSON.stringify(fieldChanged) : '全等')
    await page.screenshot({ path: join(SHOTS, 'editor-applied.png'), fullPage: true })
    console.log('  [shot] ' + join(SHOTS, 'editor-applied.png'))

    // ---- 6. 单个 agent 编辑（同一个编辑器，single 路径） -------------------
    // 选 root agent `default`（provider=moa，改模型不需要动 key_env —— 从 moa 切回
    // 需要 key_env 的 provider 会被官方写入器 fail-closed 拒掉，那是后端纪律，不是缺陷）
    console.log('\n[6] 单个 agent 编辑：只动它自己')
    const solo = ids.filter((id) => id === 'default')[0] || ids[0]
    await page.click(sel.close)
    await sleep2(200)
    await page.click(sel.clear)
    await sleep2(200)
    await page.click(`[data-models-row-edit="${solo}"]`)
    await page.waitForSelector(sel.editor, { timeout: 10000 })
    await sleep2(600)
    await page.waitForSelector(sel.pick, { timeout: 20000 })
    const soloPath = pathOf(solo)
    const soloCur = modelFields(beforeText[soloPath] || '')
    const picks = await page.$$eval(sel.pick, (nodes) => nodes.map((n) => n.getAttribute('data-models-editor-model-pick')))
    check('★单编辑态里能选到当前 provider 的模型候选（真数据）', picks.length >= 1, JSON.stringify(picks.slice(0, 5)))
    const alt = picks.filter((name) => name !== soloCur.default)[0] || picks[0]
    await page.click(`[data-models-editor-model-pick="${alt}"]`)
    await sleep2(250)
    const beforeSolo = readAll()
    await page.click(sel.previewAsk)
    await page.waitForSelector(sel.preview, { timeout: 30000 })
    await page.click(sel.applyAsk)
    await page.waitForSelector(sel.applyYes, { timeout: 10000 })
    await page.click(sel.applyYes)
    const soloDone = await page.waitForSelector(sel.result, { timeout: 120000 }).then(() => true).catch(() => false)
    check('★单 agent 写入返回结果', soloDone)
    const soloResult = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('[data-models-result-row]'))
      return rows.map((r) => ({ agent: r.getAttribute('data-models-result-row'), ok: r.getAttribute('data-ok'), text: (r.textContent || '').replace(/\s/g, '') }))
    })
    check('★单 agent 写入：1 行结果、ok、回读通过',
      soloResult.length === 1 && soloResult[0].ok === '1' && /回读\d+\/\d+项通过/.test(soloResult[0].text),
      JSON.stringify(soloResult.map((r) => r.agent + ':' + r.ok + ' ' + r.text.slice(0, 60))))
    const afterSolo = readAll()
    const othersChanged = cfgPaths.filter((p) => p !== soloPath && afterSolo[p] !== beforeSolo[p])
    check('★单个编辑只改它自己的 config.yaml（其它 agent 逐字节未变）', othersChanged.length === 0,
      othersChanged.length ? JSON.stringify(othersChanged) : '其余全等')
    check('★单个编辑真的写进了磁盘（provider 不变、模型=点选的那个）',
      modelFields(afterSolo[soloPath]).default === alt &&
        modelFields(afterSolo[soloPath]).provider === soloCur.provider,
      'default=' + modelFields(afterSolo[soloPath]).default + ' 期望=' + alt +
        ' provider=' + modelFields(afterSolo[soloPath]).provider)

    // 用「快照回滚」把它还原（顺带验证快照回滚这条路也是活的）
    await page.click(sel.close)
    await sleep2(200)
    await page.click('[data-models-tab="history"]')
    await sleep2(800)
    const snapTs = await page.$$eval('[data-models-snapshot]', (nodes) => nodes.map((n) => n.getAttribute('data-models-snapshot')))
    check('快照列表里有刚写前的那一份', snapTs.length >= 1, JSON.stringify(snapTs.slice(0, 3)))
    // 快照是全局的：别的 agent 的快照按钮是禁用的（「这一份快照里没有 X」），挑第一个可点的
    const snapTarget = await page.evaluate(() => {
      const hit = Array.from(document.querySelectorAll('[data-models-rollback]')).filter((n) => !n.disabled)[0]
      return hit ? hit.getAttribute('data-models-rollback') : ''
    })
    check('有可回滚到该 agent 的快照（其余 agent 的快照按钮禁用）', Boolean(snapTarget), 'ts=' + snapTarget)
    await page.locator('[data-models-rollback]:not([disabled])').first().click()
    await page.waitForSelector('[data-models-snapshot-rollback-yes]', { timeout: 10000 })
    await page.click('[data-models-snapshot-rollback-yes]')
    const snapDone = await page.waitForSelector('[data-models-batch-rollback-result]', { timeout: 120000 }).then(() => true).catch(() => false)
    check('★快照回滚走通（单 agent 写操作也能撤）', snapDone)
    await sleep2(500)
    const afterSnapRollback = readAll()
    check('★快照回滚后该 agent 的 config.yaml 与单编辑前逐字节相同',
      afterSnapRollback[soloPath] === beforeSolo[soloPath], 'sha256=' + sha256(soloPath))

    // ---- 7. 历史与回滚：账本可见 + 一键回滚整批 --------------------------
    console.log('\n[7] 历史与回滚：账本可见 + 一键回滚整批')
    if (await page.$(sel.close)) {
      await page.click(sel.close)
      await sleep2(200)
    }
    const tabReach = await page.$$eval('[data-models-tab]', (nodes) => nodes.map((n) => n.getAttribute('data-models-tab')))
    check('★次级入口里有「历史与回滚」', tabReach.indexOf('history') >= 0, tabReach.join(','))
    await page.click('[data-models-tab="history"]')
    await sleep2(700)
    const history = await page.evaluate(() => ({
      ledgers: Array.from(document.querySelectorAll('[data-models-batch-history]')).map((n) => n.getAttribute('data-models-batch-history')),
      snapshots: document.querySelectorAll('[data-models-snapshot]').length
    }))
    check('★账本可见（刚写的那批在里面）', history.ledgers.length >= 1, JSON.stringify(history.ledgers.slice(0, 3)))
    check('历史里也能看到快照', history.snapshots >= 1, 'snapshots=' + history.snapshots)
    await page.screenshot({ path: join(SHOTS, 'history.png'), fullPage: true })
    console.log('  [shot] ' + join(SHOTS, 'history.png'))

    // 逐批回滚（每一批只回滚一次；回滚完 9 份配置必须与改前逐字节相同）
    const visited = new Set()
    let guard = 0
    while (guard < 10) {
      guard += 1
      const ids2 = (await page.$$eval('[data-models-batch-history-rollback]', (nodes) => nodes.map((n) => n.getAttribute('data-models-batch-history-rollback'))))
        .filter((id) => !visited.has(id))
      if (!ids2.length) break
      visited.add(ids2[0])
      await page.click(`[data-models-batch-history-rollback="${ids2[0]}"]`)
      await page.waitForSelector('[data-models-batch-rollback-yes]', { timeout: 10000 })
      await page.click('[data-models-batch-rollback-yes]')
      await page.waitForSelector('[data-models-batch-rollback-result]', { timeout: 180000 })
      await sleep2(600)
      const rolled = await page.$$eval('[data-models-batch-rollback-row]', (nodes) => nodes.map((n) => n.getAttribute('data-ok')))
      check('★一键回滚整批逐 agent 成功（' + ids2[0] + '）', rolled.length >= 1 && rolled.every((v) => v === '1'), JSON.stringify(rolled))
    }
    const afterRollback = readAll()
    const stillChanged = cfgPaths.filter((p) => afterRollback[p] !== beforeText[p])
    check('★全部回滚后 9 份 config.yaml 与改前逐字节相同', stillChanged.length === 0,
      stillChanged.length ? JSON.stringify(stillChanged) : 'sha256/字节全等')

    // ---- 8. 次级入口不丢：provider 目录 / 模型库 / 预设 -------------------
    console.log('\n[8] 次级入口：provider 目录 / 模型库 / 预设 仍在')
    await page.click('[data-models-tab="providers"]')
    await sleep2(500)
    const provView = await page.evaluate(() => ({
      rows: document.querySelectorAll('[data-models-provider]').length,
      text: (document.querySelector('[data-models-providers]') || {}).textContent || ''
    }))
    check('provider 目录可达且列出 provider', provView.rows >= 3, 'rows=' + provView.rows)
    check('★凭据仍然脱敏（没有明文 key）', !/sk-[A-Za-z0-9]{6}/.test(provView.text), provView.text.replace(/\s/g, '').slice(0, 60))
    await page.click('[data-models-tab="library"]')
    await sleep2(500)
    const libView = await page.evaluate(() => document.querySelectorAll('[data-models-lib]').length)
    check('模型库可达且分组列出', libView >= 1, 'groups=' + libView)
    await page.click('[data-models-tab="presets"]')
    await sleep2(500)
    const presetView = await page.evaluate(() => document.querySelectorAll('[data-models-preset]').length)
    check('预设入口可达', presetView >= 0, 'rows=' + presetView)
    await page.click('[data-models-tab="agents"]')
    await sleep2(400)

    // ---- 9. 布局截图（1350 / 900 同尺寸） -------------------------------
    console.log('\n[9] 布局截图：1350px 与 900px')
    await layoutShots(page, TAG)

    // ---- 10. 零报错 -----------------------------------------------------
    console.log('\n[10] 浏览器零报错')
    check('零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
    check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))

    // ---- 11. 真 home 零污染 ---------------------------------------------
    const realNow = {}
    realCfgPaths.forEach((p) => {
      realNow[p] = sha256(p)
    })
    const realChanged = Object.keys(realBefore).filter((p) => realBefore[p] !== realNow[p])
    check('★真 home 的 config.yaml 一个字节都没动（写操作只落在沙盒副本上）',
      realChanged.length === 0 && realCfgPaths.length >= 5,
      realChanged.length ? '变了: ' + JSON.stringify(realChanged) : realCfgPaths.length + ' 份全等')
  }
} catch (err) {
  failures.push('异常：' + (err && err.stack ? err.stack : String(err)))
  console.log('  FAIL 异常：' + String(err))
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
    if (fixture && fixture.child) fixture.child.kill()
  } catch (e) {
    /* ignore */
  }
}

console.log('\nPASS=' + pass + ' FAIL=' + failures.length + ' TOTAL=' + (pass + failures.length))
if (failures.length) {
  console.log('MODELS-LAYOUT-PROBE-FAILED')
  failures.forEach((item) => console.log('  - ' + item))
  process.exitCode = 1
} else {
  console.log('MODELS-LAYOUT-PROBE-OK')
}
try {
  rmSync(WORK, { recursive: true, force: true })
} catch (e) {
  /* ignore */
}
