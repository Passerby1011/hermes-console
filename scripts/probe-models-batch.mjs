/**
 * ⚠️ 已过期（V5 DOM）：本脚本断言的是整改前的「批量面板」DOM
 * （`data-models-batch-panel` / `data-models-batch-*` / `data-models-agent-picker`）。
 * 布局整改（t_e691ae8c）把 BatchPanel 合并进统一的 AgentEditor，这些 hook 已不存在，
 * 因此本脚本现在会在等待选择器处超时 —— 那是**脚本过期**，不是产品回归。
 * 现行验收脚本：`scripts/probe-models-layout.mjs`（批量预览/写入/回滚全覆盖）。
 *
 * hermes-console · 模型分区「批量设模型」真浏览器验收 + 截图（t_3d4db435）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-models-batch.mjs
 *     node plugins/hermes-console/scripts/probe-models-batch.mjs --shots D:/tmp/shots
 *
 * 三层都是真的（与 probe-settings-ui.mjs 同一套路）：
 *
 *   1. **真产物**：``dashboard/dist/index.js``（= ``desktop/plugin.js``）原样加载，只套一层 harness 入口。
 *   2. **真后端**：``scripts/models_batch_fixture.py`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``，``HERMES_HOME`` 指向**沙盒副本**（真实 config.yaml 与 4 个
 *      profile 的副本）。所以写操作是真的原子写 + 真快照 + 真回读，只是落在副本上 ——
 *      真 profile 一个字节都不动（本探针结尾用 sha256 盯住）。
 *   3. **真浏览器**：Playwright + Chromium(msedge)，1440×900。
 *
 * 断言（对应卡片验收：批量前 / 预览态 / 批量后回读）：
 *   A. moa 出现在 provider 目录里（虚拟 provider，之前完全看不到）。
 *   B. 批量面板能勾「一个 / 几个 / 全部 agent」，勾完能看到每个 agent 的当前值。
 *   C. **预览**：逐 agent 列出将写哪些字段（model.provider / model.default / base_url / api_mode），
 *      且预览**一个字节都没写**（sha256 全等）。
 *   D. **写入**：逐 agent 回读确认（每个 agent 都 200/ok），磁盘上真的变成 moa + 预设名。
 *   E. **一键回滚**：每个 agent 都还原回自己原来的值，sha256 与批量前逐字节相同。
 *   F. 全程零 console error / 零 pageerror。
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
const FIXTURE_PORT = Number(process.env.HC_MODELS_FIXTURE_PORT || 9193)
const SITE_PORT = Number(process.env.HC_MODELS_SITE_PORT || 9192)
const API = '/api/plugins/hermes-console'

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'models-batch')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-models-batch-'))
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
<title>hermes-console · 模型（批量验收夹具）</title>
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
  const child = spawn(PYTHON, [join(HERE, 'models_batch_fixture.py'), '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX], {
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

try {
  console.log('[env] dist   :', DIST)
  console.log('[env] shots  :', SHOTS)
  console.log('[env] sandbox:', SANDBOX)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)

  // ---- 沙盒配置指纹（批量前后逐字节对比用） --------------------------------
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
  check('沙盒里有真配置副本（root + 每个 profile 各一份）', cfgPaths.length >= 5, String(cfgPaths.length))

  // 真 home 的指纹也先记下来（结尾证明沙盒隔离真的隔离）
  const REAL_HOME = 'D:/hermes-data/hermes'
  const realCfgPaths = [join(REAL_HOME, 'config.yaml')]
    .concat(profileNames.map((name) => join(REAL_HOME, 'profiles', name, 'config.yaml')))
    .filter((p) => existsSync(p))
  const realBefore = {}
  realCfgPaths.forEach((p) => {
    realBefore[p] = sha256(p)
  })

  // ---- 后端事实：moa 在 provider 目录里 -----------------------------------
  const provRes = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/models/providers?agent=xiaomo&all=1')
  const provBody = await provRes.json()
  const provRows = Array.isArray(provBody.providers) ? provBody.providers : []
  const moaRow = provRows.filter((r) => String(r.provider).toLowerCase() === 'moa')[0]
  check('★moa 出现在 provider 目录里（以前完全看不到）', Boolean(moaRow), provRows.map((r) => r.provider).join(','))
  check('★moa 标成虚拟 provider（kind=virtual / 无 key_env）',
    Boolean(moaRow) && moaRow.kind === 'virtual' && !moaRow.key_env && moaRow.base_url === 'moa://local',
    moaRow ? JSON.stringify({ kind: moaRow.kind, key_env: moaRow.key_env, base_url: moaRow.base_url }) : 'no row')
  const moaModels = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/models/models?agent=xiaomo&provider=moa')).json()
  check('★moa 的"模型列表"= 该 agent 的 MoA 预设名（source=moa-presets）',
    moaModels.source === 'moa-presets' && Array.isArray(moaModels.models) && moaModels.models.length >= 1,
    moaModels.source + ' ' + JSON.stringify(moaModels.models))

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
  await page.click('.hc-nav__item:has-text("模型")')
  await page.waitForSelector('[data-models-batch-panel]', { timeout: 30000 })
  await page.waitForSelector('[data-models-batch-agent]', { timeout: 30000 })

  // 截图：**整屏**一张（桌面 1440×900，验收要的三张就是这个口径），
  // 再给选择器就**额外**截那个元素本身（面板比一屏高，整屏会切掉一半预览行，
  // 元素图用来看清逐行字段）—— 两张都留，别互相替代。
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

  // ---- A. 批量前：勾 agent ------------------------------------------------
  console.log('\n[A] 批量前：勾选 agent（一个 / 几个 / 全部）')
  const agentCount = (await page.$$('[data-models-batch-agent]')).length
  check('批量面板列出全部 agent（含当前 provider/模型）', agentCount >= 4, 'agents=' + agentCount)
  await page.click('[data-models-batch-all]')
  await sleep(200)
  const selectedCount = Number(await page.getAttribute('[data-models-batch-agents]', 'data-models-batch-agents'))
  check('★「全选」一键勾上全部 agent', selectedCount === agentCount, selectedCount + '/' + agentCount)

  // provider 下拉里必须能选到 moa（这是本卡第一诉求）
  await page.waitForSelector('[data-models-batch-provider] option[value="moa"]', { timeout: 30000 }).catch(() => null)
  const debugState = await page.evaluate(() => ({
    requests: window.__hcRequests.map((r) => r.method + ' ' + r.path + ' ' + r.status).slice(-12),
    stats: Array.from(document.querySelectorAll('.hc-card')).map((n) => (n.textContent || '').trim()).slice(0, 6),
    options: Array.from(document.querySelectorAll('[data-models-batch-provider] option')).map((n) => n.value)
  }))
  console.log('        [debug] ' + JSON.stringify(debugState))
  const providerOptions = await page.$$eval('[data-models-batch-provider] option', (nodes) => nodes.map((n) => n.value))
  check('★provider 下拉里能选到 moa', providerOptions.indexOf('moa') >= 0, JSON.stringify(providerOptions.slice(0, 12)))

  await page.selectOption('[data-models-batch-provider]', 'moa')
  await sleep(1500)
  const afterSelect = await page.evaluate(() => ({
    requests: window.__hcRequests.map((r) => r.method + ' ' + r.path + ' ' + r.status).slice(-6),
    selectValue: document.querySelector('[data-models-batch-provider]').value,
    chips: document.querySelectorAll('[data-models-batch-model-pick]').length,
    bodyText: (document.querySelector('[data-models-batch-panel]').textContent || '').slice(0, 400)
  }))
  console.log('        [debug-after-select] ' + JSON.stringify(afterSelect))
  await page.waitForSelector('[data-models-batch-model-pick]', { timeout: 20000 })
  const presetChips = await page.$$eval('[data-models-batch-model-pick]', (nodes) => nodes.map((n) => n.textContent))
  check('★选了 moa 之后，模型候选 = MoA 预设名', presetChips.length >= 1, JSON.stringify(presetChips.slice(0, 8)))
  const preset = presetChips[0]
  await page.click('[data-models-batch-model-pick="' + preset + '"]')
  await sleep(200)
  await shot('01-before-batch.png', '[data-models-batch-panel]')
  console.log('        截图 01-before-batch.png（批量前：勾了 ' + selectedCount + ' 个 agent，目标 moa / ' + preset + '）')

  // ---- B. 预览：将写哪些行、哪个字段（且零写入） --------------------------
  console.log('\n[B] 预览态：逐 agent 列出将写的字段（纯读）')
  await page.click('[data-models-batch-preview-ask]')
  await page.waitForSelector('[data-models-batch-preview]', { timeout: 30000 })
  const preview = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('[data-models-batch-preview-row]'))
    return {
      count: rows.length,
      rows: rows.map((row) => ({
        agent: row.getAttribute('data-models-batch-preview-row'),
        writable: row.getAttribute('data-writable'),
        changes: Array.from(row.querySelectorAll('[data-models-batch-change]')).map((n) =>
          n.getAttribute('data-models-batch-change')
        ),
        text: row.textContent || ''
      }))
    }
  })
  check('★预览逐 agent 出表（4 个 agent 各一行）', preview.count === selectedCount, preview.count + ' 行')
  check('★每行都有实打实的字段变更内容（不是空壳行）',
    preview.rows.every((r) => r.text.replace(/\s/g, '').length > 40),
    JSON.stringify(preview.rows.map((r) => r.text.replace(/\s/g, '').length)))
  check('★每行都点名要写的字段（model.provider / model.default）',
    preview.rows.every((r) => r.changes.indexOf('model.provider') >= 0 && r.changes.indexOf('model.default') >= 0),
    JSON.stringify(preview.rows.map((r) => r.changes)))
  const notMoaYet = preview.rows.filter((r) => /provider: (?!moa)/.test(r.text) || r.changes.indexOf('model.base_url') >= 0)
  check('★虚拟 provider 的 base_url / api_mode 也在预览里（不藏）',
    preview.rows.some((r) => r.changes.indexOf('model.base_url') >= 0),
    JSON.stringify(preview.rows.map((r) => r.changes.join('+'))))
  const afterPreview = {}
  cfgPaths.forEach((p) => {
    afterPreview[p] = sha256(p)
  })
  check('★预览**一个字节都没写**（4 份 config.yaml 的 sha256 全等）',
    JSON.stringify(before) === JSON.stringify(afterPreview))
  await shot('02-preview.png', '[data-models-batch-preview]')
  console.log('        截图 02-preview.png（预览态：' + preview.count + ' 行，字段逐条列出）')

  // ---- C. 二次确认 → 写入 -------------------------------------------------
  console.log('\n[C] 确认写入（二次确认条 + 逐 agent 回读）')
  await page.click('[data-models-batch-ask]')
  await page.waitForSelector('[data-models-batch-apply-yes]', { timeout: 10000 })
  const confirmText = await page.evaluate(() => {
    const node = document.querySelector('[data-models-batch-ask]')
    return node ? node.textContent : ''
  })
  check('二次确认条出现（要求再点一次才写）', Boolean(confirmText !== null))
  await page.click('[data-models-batch-apply-yes]')
  const applyOk = await page
    .waitForSelector('[data-models-batch-result]', { timeout: 60000 })
    .then(() => true)
    .catch(() => false)
  if (!applyOk) {
    const dump = await page.evaluate(() => ({
      requests: window.__hcRequests.map((r) => r.method + ' ' + r.path + ' ' + r.status).slice(-8),
      err: (document.querySelector('[data-models-batch-error]') || {}).textContent || '',
      text: (document.querySelector('[data-models-batch-panel]').textContent || '').slice(-700)
    }))
    console.log('        [debug-apply-failed] ' + JSON.stringify(dump))
  }
  check('★批量写入返回结果表', applyOk)
  const applied = await page.evaluate(() => {
    const root = document.querySelector('[data-models-batch-result]')
    const rows = Array.from(document.querySelectorAll('[data-models-batch-result-row]'))
    return {
      batchId: root ? root.getAttribute('data-models-batch-result') : '',
      rows: rows.map((row) => ({
        agent: row.getAttribute('data-models-batch-result-row'),
        ok: row.getAttribute('data-ok'),
        text: row.textContent || ''
      }))
    }
  })

  check('★批量写入逐 agent 出结果（4 行）', applied.rows.length === selectedCount, applied.rows.length + ' 行')
  check('★每个 agent 都写成功（data-ok=1）', applied.rows.every((r) => r.ok === '1'),
    JSON.stringify(applied.rows.map((r) => r.agent + '=' + r.ok)))
  check('★每行都带「回读 N/N 项通过」', applied.rows.every((r) => /回读 \d+\/\d+ 项通过/.test(r.text)),
    applied.rows[0] ? applied.rows[0].text : '')
  check('★结果里给出 batch_id（一键回滚的凭据）', Boolean(applied.batchId), String(applied.batchId))

  // 磁盘上真的变了（回读，不信接口自述）
  const diskAfter = cfgPaths.map((p) => readFileSync(p, 'utf-8'))
  check('★磁盘上真的写成 provider: moa', diskAfter.every((text) => /provider: moa/.test(text)),
    diskAfter.map((t) => (/provider: moa/.test(t) ? 'moa' : 'NOT')).join(','))
  check('★磁盘上真的写成 model.default = 预设名', diskAfter.every((text) => text.indexOf('default: ' + preset) >= 0),
    preset)
  check('★磁盘上 base_url 写成 moa://local', diskAfter.every((text) => /base_url: moa:\/\/local/.test(text)))
  const ledgerPath = join(SANDBOX, 'plugins', 'hermes-console', 'data', 'batches', applied.batchId + '.json')
  check('★整批一份账本（data/batches/<batch_id>.json）', existsSync(ledgerPath), ledgerPath)
  await shot('03-after-batch.png', '[data-models-batch-result]')
  console.log('        截图 03-after-batch.png（批量后：逐 agent 回读确认）')

  // ---- D. 一键回滚：真的还原原值 ------------------------------------------
  console.log('\n[D] 一键回滚整批（逐 agent 还原各自原值）')
  await page.click('[data-models-batch-rollback-ask]')
  await page.waitForSelector('[data-models-batch-rollback-yes]', { timeout: 10000 })
  await page.click('[data-models-batch-rollback-yes]')
  await page.waitForSelector('[data-models-batch-rollback-result]', { timeout: 120000 })
  const rolled = await page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('[data-models-batch-rollback-row]'))
    return rows.map((row) => ({
      agent: row.getAttribute('data-models-batch-rollback-row'),
      ok: row.getAttribute('data-ok'),
      text: row.textContent || ''
    }))
  })
  check('★一键回滚逐 agent 出结果', rolled.length === selectedCount, rolled.length + ' 行')
  check('★每个 agent 都还原成功（data-ok=1）', rolled.every((r) => r.ok === '1'),
    JSON.stringify(rolled.map((r) => r.agent + '=' + r.ok)))
  const afterRollback = {}
  cfgPaths.forEach((p) => {
    afterRollback[p] = sha256(p)
  })
  const changed = Object.keys(before).filter((p) => before[p] !== afterRollback[p])
  check('★回滚后 4 份 config.yaml 与批量前**逐字节相同**（真还原）', changed.length === 0,
    changed.length ? '变了: ' + JSON.stringify(changed) : 'sha256 全等')
  await shot('04-rollback.png', '[data-models-batch-rollback-result]')
  console.log('        截图 04-rollback.png（回滚后：sha256 与批量前全等）')

  // ---- E. 窄窗（<900px）不出现横向滚动条 --------------------------------
  console.log('\n[E] 窄窗 760px：竖排、无横向滚动')
  await page.setViewportSize({ width: 760, height: 900 })
  await sleep(500)
  const narrowInfo = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    panel: Boolean(document.querySelector('[data-models-batch-panel]'))
  }))
  check('★窄窗没有横向滚动条（scrollWidth ≤ innerWidth）',
    narrowInfo.scrollWidth <= narrowInfo.innerWidth + 1,
    narrowInfo.scrollWidth + ' vs ' + narrowInfo.innerWidth)
  check('窄窗下批量面板仍在（不塌成空白）', narrowInfo.panel)
  await shot('05-narrow.png', '[data-models-batch-panel]')
  await page.setViewportSize({ width: 1440, height: 900 })

  // ---- G. 单 agent 面板也能选 moa（"单个"这一档） ------------------------
  console.log('\n[G] 单 agent 切换面板：provider 里能选 moa，模型候选是预设名')
  // 先把这个分区自己的目标 agent 换成 default（它有 moa: 段 ⇒ 有预设名可列）
  await page.selectOption('[data-models-agent-picker]', 'default')
  await page.waitForFunction(
    () => document.querySelector('[data-models-current="default"]') !== null,
    { timeout: 20000 }
  ).catch(() => null)
  await page.waitForSelector('[data-models-provider-picker] option[value="moa"]', { timeout: 30000 }).catch(() => null)
  const singleOptions = await page.$$eval('[data-models-provider-picker] option', (nodes) => nodes.map((n) => n.value))
  check('★单 agent 的 provider 下拉里也能选到 moa', singleOptions.indexOf('moa') >= 0,
    JSON.stringify(singleOptions.slice(0, 12)))
  await page.selectOption('[data-models-provider-picker]', 'moa')
  // 模型候选来自 model-library（按 provider 分组），等它真的出来
  await page.waitForSelector('[data-models-model-pick]', { timeout: 20000 }).catch(() => null)
  const singlePicks = await page.$$eval('[data-models-model-pick]', (nodes) => nodes.map((n) => n.textContent))
  check('★单 agent 面板选 moa 后，模型候选 = MoA 预设名', singlePicks.length >= 1,
    JSON.stringify(singlePicks.slice(0, 8)))
  await shot('06-single-agent-moa.png', '[data-models-confirm]')

  // ---- H. 零 console error ------------------------------------------------
  console.log('\n[H] 浏览器零报错')
  check('零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))

  // ---- F. 真 home 零污染 --------------------------------------------------
  const realNow = {}
  realCfgPaths.forEach((p) => {
    realNow[p] = sha256(p)
  })
  const realChanged = Object.keys(realBefore).filter((p) => realBefore[p] !== realNow[p])
  check('★真 home 的 config.yaml 一个字节都没动（写操作只落在沙盒副本上）',
    realChanged.length === 0 && realCfgPaths.length >= 5,
    realChanged.length ? '变了: ' + JSON.stringify(realChanged) : realCfgPaths.length + ' 份全等')
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
  console.log('MODELS-BATCH-PROBE-FAILED')
  failures.forEach((item) => console.log('  - ' + item))
  process.exitCode = 1
} else {
  console.log('MODELS-BATCH-PROBE-OK')
}
try {
  rmSync(WORK, { recursive: true, force: true })
} catch (e) {
  /* ignore */
}
