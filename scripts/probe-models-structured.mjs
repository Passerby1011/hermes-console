/**
 * hermes-console · 模型分区「结构化段」真浏览器验收
 *   问题 1：provider 目录排版（三列紧凑 + 列标题 + 行分隔，900 / 1350 两个宽度）
 *   问题 2：备用链动态列表（添加 3 条以上 / 删中间一条 / 调序 / 清空，逐次回读）
 *   问题 3：辅助槽位编辑（8 个槽位可编辑、逐槽位隔离、逐段隔离、未改动零写请求）
 *
 * 跑法（仓库根目录下）::
 *
 *     node plugins/hermes-console/scripts/probe-models-structured.mjs
 *
 * 三层都是真的：
 *   1. **真产物**：`dashboard/dist/index.js`（= `desktop/plugin.js`）原样加载，只套一层 harness 入口；
 *   2. **真后端**：`scripts/models_batch_fixture.py --sandbox` 加载真实的 `dashboard/plugin_api.py`，
 *      `HERMES_HOME` 指向**沙盒副本** —— 写操作只落在副本上，真 profile 用 sha256 盯住前后；
 *   3. **真浏览器**：Playwright + Chromium(msedge)。
 *
 * 断言口径：本探针只看「界面真的做到了吗」——每个写操作之后都**读沙盒磁盘**核对，
 * 请求体从 `window.__hcRequests`（含 body）里取，不看界面自述。
 */
import { spawn, spawnSync } from 'node:child_process'
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
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_MODELS_FIXTURE_PORT || 9197)
const SITE_PORT = Number(process.env.HC_MODELS_SITE_PORT || 9196)
const API = '/api/plugins/hermes-console'
const REAL_HOME = 'D:/hermes-data/hermes'

const SHOTS = resolve(process.env.HC_STRUCT_SHOTS || join(PLUGIN, 'scripts', '_probe_out', 'models-structured'))

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-models-struct-'))
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
    requests.push({ method, path, status: res.status, body: options.body === undefined ? null : options.body })
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
<title>hermes-console · 模型（结构化段验收夹具）</title>
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
  // PYTHONPATH 若指向别的解释器的 site-packages（本机是 3.14 的），会把 venv 自己的
  // pydantic_core 盖掉 ⇒ ImportError: cannot import name 'ArgsKwargs'。夹具只要自己那套包。
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
  for (let i = 0; i < 160; i += 1) {
    // 只认**本次**子进程自己打的 ready（端口上要是坐着上一轮残留的夹具，
    // /health 也会 200 —— 那会把写操作打到别的沙盒里，看上去全对其实全错）
    if (/\[fixture\] ready on/.test(fixture.log())) {
      const res = await fetch(url).catch(() => null)
      if (res && res.ok) return true
    }
    await sleep(250)
  }
  throw new Error('后端夹具没起来（端口 ' + FIXTURE_PORT + ' 可能被上一轮残留进程占着）：\n' + fixture.log())
}

/** 端口上是不是已经有人在听（有 ⇒ 上一轮探针没清干净，必须人工收掉再跑）。 */
async function portBusy() {
  try {
    const res = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/health', { signal: AbortSignal.timeout(1200) })
    return res.ok
  } catch (e) {
    return false
  }
}

// ---- config.yaml 的极简解析（只取本卡关心的三段） --------------------------
/** `model:` 段 → {provider, default, base_url, api_mode, key_env} */
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

/** `fallback_providers:` → [{provider, model}]（顺序即文件顺序） */
function fallbackChain(text) {
  const lines = String(text).split(/\r?\n/)
  const start = lines.findIndex((l) => /^fallback_providers:/.test(l))
  if (start < 0) return []
  const out = []
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i]
    if (!/^\s/.test(line) && line.trim()) break
    const item = /^\s*-\s*provider:\s*(.*)$/.exec(line)
    if (item) {
      out.push({ provider: item[1].trim().replace(/^['"]|['"]$/g, ''), model: '' })
      continue
    }
    const field = /^\s+(model|provider):\s*(.*)$/.exec(line)
    if (field && out.length) out[out.length - 1][field[1]] = field[2].trim().replace(/^['"]|['"]$/g, '')
  }
  return out
}

/** `auxiliary:` → {slot: {provider, model, ...}} */
function auxiliaryBlock(text) {
  const lines = String(text).split(/\r?\n/)
  const start = lines.findIndex((l) => /^auxiliary:\s*$/.test(l))
  const out = {}
  if (start < 0) return out
  let slot = ''
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i]
    if (!/^\s/.test(line) && line.trim()) break
    const head = /^ {2}([A-Za-z0-9._-]+):\s*$/.exec(line)
    if (head) {
      slot = head[1]
      out[slot] = {}
      continue
    }
    const field = /^ {4}([A-Za-z0-9_]+):\s*(.*)$/.exec(line)
    if (field && slot) out[slot][field[1]] = field[2].trim().replace(/^['"]|['"]$/g, '')
  }
  return out
}

/** `agent:` 段的 `reasoning_effort`（别的段里出现同名键不算）。 */
function reasoningOf(text) {
  const lines = String(text).split(/\r?\n/)
  const start = lines.findIndex((l) => /^agent:\s*$/.test(l))
  if (start < 0) return ''
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i]
    if (!/^\s/.test(line) && line.trim()) break
    const m = /^ {2}reasoning_effort:\s*(.*)$/.exec(line)
    if (m) return m[1].trim().replace(/^['"]|['"]$/g, '')
  }
  return ''
}

/** 直接问夹具（真后端）要一份 JSON —— 回读断言只看它，不看界面自述。 */
function apiGet(path) {
  return new Promise((resolve) => {
    const req = httpRequest(
      { host: '127.0.0.1', port: FIXTURE_PORT, path: API + path, method: 'GET' },
      (res) => {
        let buf = ''
        res.on('data', (chunk) => {
          buf += chunk
        })
        res.on('end', () => {
          try {
            resolve(JSON.parse(buf))
          } catch (failure) {
            resolve({ ok: false, error: 'not json: ' + buf.slice(0, 120) })
          }
        })
      }
    )
    req.on('error', (failure) => resolve({ ok: false, error: failure.message }))
    req.end()
  })
}

const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

/**
 * 颜色的 alpha（主题色是 color-mix，浏览器把它算成 rgba() 或 color(srgb r g b / a)）。
 * 同一主题下"更深一档"就是 alpha 更大，用它做断言不依赖具体色值。
 */
function alphaOf(color) {
  const text = String(color)
  const srgb = /\/\s*([0-9.]+)\s*\)/.exec(text)
  if (/^color\(/.test(text) && srgb) return Number(srgb[1])
  const m = /rgba?\(([^)]+)\)/.exec(text)
  if (!m) return 1
  const parts = m[1].split(',').map((s) => s.trim())
  return parts.length >= 4 ? Number(parts[3]) : 1
}

/** provider 目录的几何量测（列宽 / 行高 / 分隔线 / key_env 对齐 / 溢出）。 */
async function measureProvider(page, width) {
  await page.setViewportSize({ width, height: 900 })
  await sleep(600)
  return page.evaluate(() => {
    const root = document.documentElement
    const rows = Array.from(document.querySelectorAll('[data-models-provider]'))
    const head = document.querySelector('[data-models-pv-head]')
    const first = rows[0] || null
    const cells = first ? Array.from(first.querySelectorAll('.hc-models__pv-cell')) : []
    const boxes = cells.map((c) => {
      const b = c.getBoundingClientRect()
      return { left: Math.round(b.left), right: Math.round(b.right), width: Math.round(b.width) }
    })
    const rowBox = first ? first.getBoundingClientRect() : null
    const keyChip = first ? first.querySelector('[data-models-provider-key]') : null
    const keyBox = keyChip ? keyChip.getBoundingClientRect() : null
    const urlCell = first ? first.querySelector('.hc-models__pv-url') : null
    const urlColor = urlCell ? getComputedStyle(urlCell).color : ''
    // URL **文字**的右边界（不是整列右边界）：文字到 key_env 列的距离才是肉眼看到的空白
    let urlTextRight = 0
    if (urlCell && urlCell.firstChild) {
      const range = document.createRange()
      range.selectNodeContents(urlCell)
      const rect = range.getBoundingClientRect()
      urlTextRight = Math.round(rect.right)
    }
    const muted = document.querySelector('.hc-models__pv-key .hc-chip--off') || document.querySelector('.hc-muted')
    const mutedColor = muted ? getComputedStyle(muted).color : ''
    const second = rows[1] || null
    // 逐行量：三列左边界（列对齐）与「URL 文字 → key_env 列」的空白
    const perRow = rows.map((row) => {
      const cellsOf = Array.from(row.querySelectorAll('.hc-models__pv-cell'))
      const url = row.querySelector('.hc-models__pv-url')
      const key = row.querySelector('.hc-models__pv-key')
      let textRight = 0
      if (url && url.firstChild) {
        const r = document.createRange()
        r.selectNodeContents(url)
        textRight = Math.round(r.getBoundingClientRect().right)
      }
      return {
        lefts: cellsOf.map((c) => Math.round(c.getBoundingClientRect().left)),
        blank: key ? Math.round(key.getBoundingClientRect().left - textRight) : 0,
        url: url ? (url.textContent || '') : ''
      }
    })
    return {
      perRow,
      scrollWidth: root.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      innerWidth: window.innerWidth,
      headVisible: Boolean(head) && head.getBoundingClientRect().height > 0,
      headText: head ? (head.textContent || '') : '',
      rowCount: rows.length,
      rowHeight: rowBox ? Math.round(rowBox.height) : 0,
      rowWidth: rowBox ? Math.round(rowBox.width) : 0,
      cells: boxes,
      keyLeft: keyBox ? Math.round(keyBox.left) : 0,
      urlTextRight,
      urlColor,
      mutedColor,
      borderTop: second ? getComputedStyle(second).borderTopWidth : '',
      padLeft: first ? Math.round(parseFloat(getComputedStyle(first).paddingLeft) || 0) : 0,
      padRight: first ? Math.round(parseFloat(getComputedStyle(first).paddingRight) || 0) : 0,
      overflow: root.scrollWidth > window.innerWidth + 1 || document.body.scrollWidth > window.innerWidth + 1
    }
  })
}

/** 一组 provider 目录断言（1350 / 900 各跑一遍）。 */
async function providerChecks(page, width, tag) {
  const info = await measureProvider(page, width)
  check(`★${width}px 无横向溢出`, !info.overflow, `scrollWidth=${info.scrollWidth} body=${info.bodyScrollWidth} innerWidth=${info.innerWidth}`)
  check(`★${width}px 有列标题 Provider / URL / Key env`,
    info.headVisible && /Provider/.test(info.headText) && /URL/.test(info.headText) && /Key\s*env/.test(info.headText),
    info.headText.replace(/\s+/g, '|'))
  check(`★${width}px 三列仍在（不是退化成一块）`, info.cells.length === 3, 'cells=' + info.cells.length + ' widths=' + JSON.stringify(info.cells.map((c) => c.width)))
  const gap1 = info.cells.length === 3 ? info.cells[1].left - info.cells[0].right : -1
  const gap2 = info.cells.length === 3 ? info.cells[2].left - info.cells[1].right : -1
  check(`★${width}px 三列紧邻（列间只有一个 gap，不是大片空白）`, gap1 > 0 && gap1 <= 32 && gap2 > 0 && gap2 <= 32 && Math.abs(gap1 - gap2) <= 1,
    `gap1=${gap1} gap2=${gap2}`)
  const sum = info.cells.length === 3 ? info.cells[0].width + info.cells[1].width + info.cells[2].width + gap1 + gap2 : -1
  const inner = info.rowWidth - info.padLeft - info.padRight
  check(`★${width}px 三列铺满整行（无游离空白列）`, Math.abs(sum - inner) <= 3,
    `列宽合计 ${sum} vs 行内容宽 ${inner}（行宽 ${info.rowWidth} − padding ${info.padLeft}/${info.padRight}）`)
  check(`★${width}px 行高 40–44px`, info.rowHeight >= 40 && info.rowHeight <= 44, 'rowHeight=' + info.rowHeight)
  check(`★${width}px 普通行之间有细分隔线`, info.borderTop === '1px', 'borderTop=' + info.borderTop)
  check(`★${width}px key_env 与所在列左对齐`, Math.abs(info.keyLeft - info.cells[2].left) <= 2, `key=${info.keyLeft} col=${info.cells[2] && info.cells[2].left}`)
  const lefts = info.perRow.map((row) => row.lefts.join(','))
  check(`★${width}px 所有行的三列左边界逐行一致（列真的对齐）`,
    lefts.length >= 2 && lefts.every((one) => one === lefts[0]), lefts.slice(0, 3).join(' | '))
  const blanks = info.perRow.map((row) => row.blank)
  const worst = blanks.length ? Math.max.apply(null, blanks) : 0
  check(`★${width}px 每行 URL 文字到 key_env 列之间都没有大片横向空白（最空的一行 ≤ 行宽 45%）`,
    worst >= 0 && worst <= info.rowWidth * 0.45,
    `最空 ${worst}px / 行宽 ${info.rowWidth}px（逐行 ${blanks.join(',')}）`)
  check(`★${width}px 目录表宽有上限（不再被拉满整屏）`, info.rowWidth <= 900, 'rowWidth=' + info.rowWidth)
  check(`★${width}px 次要文字（URL）比最浅一档更深`, alphaOf(info.urlColor) > alphaOf(info.mutedColor),
    `url=${info.urlColor}(${alphaOf(info.urlColor).toFixed(2)}) muted=${info.mutedColor}(${alphaOf(info.mutedColor).toFixed(2)})`)
  await page.screenshot({ path: join(SHOTS, `${tag}-${width}.png`), fullPage: true })
  console.log('  [shot] ' + join(SHOTS, `${tag}-${width}.png`))
  return info
}

/** 打开某个 agent 的编辑器（单个编辑态）。 */
async function openSingle(page, id) {
  await page.click('[data-models-editor-close]').catch(() => {})
  await sleep(200)
  await page.click('[data-models-select-none]').catch(() => {})
  await sleep(200)
  await page.click(`[data-models-row-edit="${id}"]`)
  await page.waitForSelector('[data-models-editor]', { timeout: 15000 })
  await sleep(400)
}

/** 切换写入段。 */
async function pickScope(page, scope) {
  await page.click(`[data-models-editor-scope="${scope}"]`)
  await sleep(350)
}

/** 只做预览（纯读），返回预览表。 */
async function doPreview(page) {
  await page.click('[data-models-editor-preview-ask]')
  await page.waitForSelector('[data-models-preview]', { timeout: 40000 })
  await sleep(300)
  return page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('[data-models-preview-row]'))
    return rows.map((row) => ({
      agent: row.getAttribute('data-models-preview-row'),
      writable: row.getAttribute('data-writable'),
      slot: row.getAttribute('data-slot'),
      changes: Array.from(row.querySelectorAll('[data-models-preview-change]')).map((c) => ({
        key: c.getAttribute('data-models-preview-change'),
        to: c.getAttribute('data-to')
      }))
    }))
  })
}

/** 二次确认 → 写入，返回逐 agent 回执。 */
async function doApply(page) {
  await page.click('[data-models-editor-apply-ask]')
  await page.waitForSelector('[data-models-editor-confirm]', { timeout: 15000 })
  await page.click('[data-models-editor-apply-yes]')
  const ok = await page.waitForSelector('[data-models-result]', { timeout: 180000 }).then(() => true).catch(() => false)
  await sleep(400)
  const rows = await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[data-models-result-row]'))
    return nodes.map((r) => ({
      agent: r.getAttribute('data-models-result-row'),
      ok: r.getAttribute('data-ok'),
      changed: r.getAttribute('data-changed'),
      text: (r.textContent || '').replace(/\s/g, '')
    }))
  })
  return { rows, ok }
}

/** 走完「预览 → 二次确认 → 写入」。 */
async function previewAndApply(page) {
  const preview = await doPreview(page)
  const applied = await doApply(page)
  return { preview, result: applied.rows, ok: applied.ok }
}

/** 当前编辑器里的备用链（界面状态）。 */
async function readChainUi(page) {
  return page.evaluate(() => {
    const rows = Array.from(document.querySelectorAll('[data-models-chain-row]'))
    return rows.map((row) => {
      const p = row.querySelector('[data-models-chain-provider]')
      const m = row.querySelector('[data-models-chain-model]')
      return { provider: p ? p.value : '', model: m ? m.value : '' }
    })
  })
}

/** 把备用链整条设成 items（先清空再加，避免受原值干扰）。 */
async function setChainUi(page, items) {
  await page.click('[data-models-chain-clear]')
  await sleep(250)
  for (let i = 0; i < items.length; i += 1) {
    await page.click('[data-models-chain-add]')
    await sleep(180)
    await page.selectOption(`[data-models-chain-provider="${i}"]`, items[i].provider)
    await page.fill(`[data-models-chain-model="${i}"]`, items[i].model)
    await sleep(120)
  }
  return readChainUi(page)
}

/** 写请求记账（只看写端点）。 */
async function writeRequests(page) {
  return page.evaluate(() =>
    window.__hcRequests.filter(
      (r) => String(r.method || 'GET').toUpperCase() !== 'GET' &&
        /models\/apply|batch\/apply|rollback|models\/moa\/presets/.test(r.path)
    )
  )
}

try {
  console.log('[env] dist   :', DIST)
  console.log('[env] shots  :', SHOTS)
  console.log('[env] sandbox:', SANDBOX)
  if (await portBusy()) throw new Error('端口 ' + FIXTURE_PORT + ' 上已经有夹具在跑（上一轮残留）：先收掉再重跑')
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture:', '127.0.0.1:' + FIXTURE_PORT + API)

  const profileNames = existsSync(join(SANDBOX, 'profiles'))
    ? readdirSync(join(SANDBOX, 'profiles')).filter((name) => existsSync(join(SANDBOX, 'profiles', name, 'config.yaml')))
    : []
  const cfgPaths = [join(SANDBOX, 'config.yaml')]
    .concat(profileNames.map((name) => join(SANDBOX, 'profiles', name, 'config.yaml')))
    .filter((p) => existsSync(p))
  const pathOf = (agent) => (agent === 'default' ? join(SANDBOX, 'config.yaml') : join(SANDBOX, 'profiles', agent, 'config.yaml'))
  const realCfgPaths = [join(REAL_HOME, 'config.yaml')]
    .concat(profileNames.map((name) => join(REAL_HOME, 'profiles', name, 'config.yaml')))
    .filter((p) => existsSync(p))
  const realBefore = {}
  realCfgPaths.forEach((p) => {
    realBefore[p] = sha256(p)
  })
  if (!cfgPaths.length) {
    console.log('[dbg] sandbox 空：fixture 日志如下')
    console.log(fixture.log().split('\n').slice(0, 12).join('\n'))
    console.log('[dbg] sandbox 实际内容：' + (existsSync(SANDBOX) ? readdirSync(SANDBOX).join(',') : '（目录不存在）'))
  }
  check('沙盒里有真配置副本（root + 每个 profile 各一份）', cfgPaths.length >= 5,
    String(cfgPaths.length) + '（sandbox=' + SANDBOX + '）')

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
  await page.waitForSelector('[data-models-agent-row]', { timeout: 30000 })

  // ---- C. provider 目录排版（1350 / 900） --------------------------------
  console.log('\n[C] provider 目录：三列紧凑 + 列标题 + 行分隔（1350 / 900）')
  await page.click('[data-models-tab="providers"]')
  await sleep(700)
  const prov1350 = await providerChecks(page, 1350, 'providers')
  const prov900 = await providerChecks(page, 900, 'providers')
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(400)
  const provText = await page.evaluate(() => (document.querySelector('[data-models-providers]') || {}).textContent || '')
  check('★provider 目录里没有明文 key（只有变量名）', !/sk-[A-Za-z0-9]{6}/.test(provText) && !/AIza[0-9A-Za-z_-]{12}/.test(provText),
    provText.replace(/\s/g, '').slice(0, 70))
  const keyTitle = await page.evaluate(() => {
    const chip = document.querySelector('[data-models-provider-key]')
    return chip ? { text: (chip.textContent || '').trim(), title: chip.getAttribute('title') || '' } : null
  })
  check('★key_env 标签带 title（长变量名可看全值）', Boolean(keyTitle) && keyTitle.title.length > 0, JSON.stringify(keyTitle))

  // ---- D. 备用链（多条 / 删中间 / 调序 / 清空） ---------------------------
  console.log('\n[D] 备用链：动态列表（添加 3 条以上 / 删中间一条 / 调序 / 清空）')
  await page.click('[data-models-tab="agents"]')
  await sleep(500)
  const ids = await page.$$eval('[data-models-agent-row]', (nodes) => nodes.map((n) => n.getAttribute('data-models-agent-row')))
  const target = ids.indexOf('code-worker') >= 0 ? 'code-worker' : ids[0]
  const targetPath = pathOf(target)
  const beforeAll = {}
  cfgPaths.forEach((p) => {
    beforeAll[p] = readFileSync(p, 'utf-8')
  })
  const beforeChain = fallbackChain(beforeAll[targetPath])
  await openSingle(page, target)
  await pickScope(page, 'fallbacks')
  const chainUi0 = await readChainUi(page)
  check('★备用链编辑器预填该 agent 现在的链（不是空表单）',
    chainUi0.length === beforeChain.length && chainUi0.every((item, i) => item.provider === beforeChain[i].provider && item.model === beforeChain[i].model),
    JSON.stringify(chainUi0) + ' vs ' + JSON.stringify(beforeChain))

  await page.click('[data-models-chain-add]')
  await sleep(250)
  const provOptions = await page.$$eval('[data-models-chain-provider]', (nodes) =>
    nodes.length ? Array.from(nodes[0].options).map((o) => o.value).filter(Boolean) : [])
  check('每条备用模型的 provider 是可选项（真数据）', provOptions.length >= 2, JSON.stringify(provOptions.slice(0, 6)))
  const pA = provOptions[0]
  const pB = provOptions.length > 1 ? provOptions[1] : provOptions[0]
  const four = [
    { provider: pA, model: 'fb-one' },
    { provider: pB, model: 'fb-two' },
    { provider: pA, model: 'fb-three' },
    { provider: pB, model: 'fb-four' }
  ]
  const uiChain4 = await setChainUi(page, four)
  check('★能添加 3 条以上备用模型（界面里 4 条）', uiChain4.length === 4, JSON.stringify(uiChain4))
  const beforePreview = readFileSync(targetPath, 'utf-8')
  const preview4 = await doPreview(page)
  const afterPreview = readFileSync(targetPath, 'utf-8')
  check('★预览一个字节都没写', afterPreview === beforePreview)
  const countChange = preview4[0] ? preview4[0].changes.filter((c) => c.key === 'fallback_providers.count') : []
  check('★预览里报出链长变化（' + beforeChain.length + ' → 4）', countChange.length === 1 && String(countChange[0].to) === '4',
    JSON.stringify(preview4[0] && preview4[0].changes.slice(0, 6)))
  const applied4 = await doApply(page)
  check('★4 条备用链写入成功且回读通过',
    applied4.ok && applied4.rows.length === 1 && applied4.rows[0].ok === '1' && /回读\d+\/\d+项通过/.test(applied4.rows[0].text),
    JSON.stringify(applied4.rows.map((r) => r.agent + ':' + r.ok)))
  const after4 = fallbackChain(readFileSync(targetPath, 'utf-8'))
  check('★回读顺序与写入一致（4 条，逐条）',
    JSON.stringify(after4) === JSON.stringify(four.map((x) => ({ provider: x.provider, model: x.model }))),
    JSON.stringify(after4))
  const previewMatch4 = preview4[0] ? preview4[0].changes.filter((c) => /^fallback_providers\[\d+\]\./.test(c.key)) : []
  check('★预览值 = 写入值（逐条）',
    previewMatch4.every((c) => {
      const m = /^fallback_providers\[(\d+)\]\.(provider|model)$/.exec(c.key)
      if (!m) return true
      const item = after4[Number(m[1])] || {}
      return String(item[m[2]] || '') === String(c.to || '')
    }) && previewMatch4.length > 0,
    JSON.stringify(previewMatch4.slice(0, 8)))

  // 删中间一条（index 1）：其余顺序保持不变
  await page.click('[data-models-chain-remove="1"]')
  await sleep(300)
  const uiAfterRemove = await readChainUi(page)
  check('★删中间一条后界面里剩 3 条、顺序保持', uiAfterRemove.length === 3 &&
    uiAfterRemove[0].model === 'fb-one' && uiAfterRemove[1].model === 'fb-three' && uiAfterRemove[2].model === 'fb-four',
    JSON.stringify(uiAfterRemove))
  const removed = await previewAndApply(page)
  const afterRemove = fallbackChain(readFileSync(targetPath, 'utf-8'))
  check('★删中间一条写盘后回读顺序不变（one/three/four）',
    removed.ok && JSON.stringify(afterRemove.map((x) => x.model)) === JSON.stringify(['fb-one', 'fb-three', 'fb-four']),
    JSON.stringify(afterRemove))

  // 调序：把第 3 条上移 → one/four/three
  await page.click('[data-models-chain-up="2"]')
  await sleep(300)
  const uiMoved = await readChainUi(page)
  check('调序后界面顺序变化（第 3 条上移）', uiMoved.map((x) => x.model).join(',') === 'fb-one,fb-four,fb-three', JSON.stringify(uiMoved))
  const moved = await previewAndApply(page)
  const afterMove = fallbackChain(readFileSync(targetPath, 'utf-8'))
  check('★调序写盘后回读顺序与界面一致（one/four/three）',
    moved.ok && JSON.stringify(afterMove.map((x) => x.model)) === JSON.stringify(['fb-one', 'fb-four', 'fb-three']),
    JSON.stringify(afterMove))

  // 清空整条链
  await page.click('[data-models-chain-clear]')
  await sleep(300)
  const uiCleared = await readChainUi(page)
  check('点「清空整条链」后界面里 0 条', uiCleared.length === 0)
  const cleared = await previewAndApply(page)
  const afterClear = fallbackChain(readFileSync(targetPath, 'utf-8'))
  check('★清空写盘后回读备用链为空', cleared.ok && afterClear.length === 0, JSON.stringify(afterClear))

  // 未选中的 agent 逐字节未变
  const nowAll = {}
  cfgPaths.forEach((p) => {
    nowAll[p] = readFileSync(p, 'utf-8')
  })
  const others = cfgPaths.filter((p) => p !== targetPath)
  const othersChanged = others.filter((p) => nowAll[p] !== beforeAll[p])
  check('★未选中的 agent 逐字节未变（' + others.length + ' 份）', othersChanged.length === 0,
    othersChanged.length ? JSON.stringify(othersChanged) : 'sha256/字节全等')

  // ---- E. 辅助槽位（8 个槽位可编辑 / 逐槽位隔离 / 逐段隔离 / 未改动零写请求） ----
  console.log('\n[E] 辅助槽位：动态生成 + 逐槽位编辑 + 隔离')
  // 优先用 root `default`（用户口径里的那 8 个槽位）；它没有 auxiliary 段就退到槽位最多的 agent
  const slotCount = (id) => Object.keys(auxiliaryBlock(readFileSync(pathOf(id), 'utf-8'))).length
  let auxAgent = ids.indexOf('default') >= 0 ? 'default' : ids[0]
  if (slotCount(auxAgent) < 1) {
    let best = -1
    ids.forEach((id) => {
      const count = slotCount(id)
      if (count > best) {
        best = count
        auxAgent = id
      }
    })
  }
  const allSlotCounts = ids.map((id) => id + '=' + slotCount(id)).join(' ')
  console.log('  [aux] 每个 agent 的槽位数：' + allSlotCounts)
  const auxPath = pathOf(auxAgent)
  const beforeAux = readFileSync(auxPath, 'utf-8')
  const auxDisk0 = auxiliaryBlock(beforeAux)
  const slotNames = Object.keys(auxDisk0).sort()
  await openSingle(page, auxAgent)
  await pickScope(page, 'auxiliary')
  await sleep(500)
  const auxUi = await page.evaluate(() => {
    const slots = Array.from(document.querySelectorAll('[data-models-aux-slot]'))
    return {
      count: slots.length,
      names: slots.map((s) => s.getAttribute('data-models-aux-slot')),
      withProvider: slots.filter((s) => s.querySelector('[data-models-aux-provider]')).length,
      withModel: slots.filter((s) => s.querySelector('[data-models-aux-field$=":model"]')).length,
      keyEnv: slots.map((s) => (s.querySelector('[data-models-aux-keyenv]') || {}).getAttribute
        ? s.querySelector('[data-models-aux-keyenv]').getAttribute('data-models-aux-keyenv')
        : null),
      // key_env 不许有写入控件：所有可写字段的 data 属性里不得出现 key_env
      writableFields: Array.from(document.querySelectorAll('[data-models-aux-field]')).map((n) => n.getAttribute('data-models-aux-field'))
    }
  })
  check('★辅助槽位按配置动态生成（数量 = 配置里的槽位数，且 ≥8）', auxUi.count === slotNames.length && auxUi.count >= 8,
    '界面 ' + auxUi.count + ' / 配置 ' + slotNames.length + '：' + slotNames.join(','))
  check('★槽位名与配置逐字一致（不硬编码）', JSON.stringify(auxUi.names.slice().sort()) === JSON.stringify(slotNames), JSON.stringify(auxUi.names))
  check('每个槽位都有 provider 与 model 控件', auxUi.withProvider === auxUi.count && auxUi.withModel === auxUi.count,
    'provider=' + auxUi.withProvider + ' model=' + auxUi.withModel)
  check('★key_env 只读展示（没有任何写入控件）',
    auxUi.writableFields.every((name) => !/:key_env$/.test(String(name))) && auxUi.keyEnv.some((v) => Boolean(v)),
    'key_env 展示：' + JSON.stringify(auxUi.keyEnv.filter(Boolean)))

  // 8 个槽位全部改一遍
  const wanted = {}
  slotNames.forEach((name) => {
    wanted[name] = 'aux-' + name
  })
  for (const name of slotNames) {
    await page.fill(`[data-models-aux-field="${name}:model"]`, wanted[name])
    await sleep(80)
  }
  const dirtyCount = await page.evaluate(() => document.querySelectorAll('[data-models-aux-slot][data-models-aux-dirty="1"]').length)
  check('★改过的槽位都标脏（' + slotNames.length + ' 个）', dirtyCount === slotNames.length, 'dirty=' + dirtyCount)

  const writesBeforeAux = (await writeRequests(page)).length
  const previewAux = await doPreview(page)
  const previewSlots = previewAux.map((row) => row.slot)
  check('★预览逐槽位出行（一个槽位一行）', previewSlots.length === slotNames.length, JSON.stringify(previewSlots))
  const auxApplied = await doApply(page)
  const auxWrites = (await writeRequests(page)).slice(writesBeforeAux)
  const writeSlots = auxWrites.map((r) => (r.body && r.body.slot) || '')
  check('★每个槽位各一个写请求（改 approval 不会顺带写 compression）',
    writeSlots.length === slotNames.length && writeSlots.slice().sort().join(',') === slotNames.join(','),
    JSON.stringify(writeSlots))
  check('★全部槽位（' + slotNames.length + ' 个）写入都成功并逐条回读通过',
    auxApplied.ok && auxApplied.rows.length === slotNames.length && auxApplied.rows.every((r) => r.ok === '1'),
    JSON.stringify(auxApplied.rows.map((r) => r.agent + ':' + r.ok)))
  const afterAuxText = readFileSync(auxPath, 'utf-8')
  const auxDisk1 = auxiliaryBlock(afterAuxText)
  const mismatch = slotNames.filter((name) => String((auxDisk1[name] || {}).model || '') !== wanted[name])
  check('★回读一致：' + slotNames.length + ' 个槽位的 model 都等于写入值', mismatch.length === 0,
    mismatch.length ? JSON.stringify(mismatch) : '全等')
  const keptFields = slotNames.filter((name) => {
    const before = auxDisk0[name] || {}
    const after = auxDisk1[name] || {}
    return Object.keys(before).every((key) => key === 'model' || String(before[key]) === String(after[key] === undefined ? '' : after[key]))
  })
  check('★没改的槽位字段原样保留（provider / timeout / reasoning_effort / key_env）',
    keptFields.length === slotNames.length, '保留了 ' + keptFields.length + '/' + slotNames.length)

  // 逐槽位隔离：只改 approval，compression 一个字节都不许动
  const compressionBefore = JSON.stringify(auxDisk1['compression'] || {})
  const approvalBefore = JSON.stringify(auxDisk1['approval'] || {})
  await page.fill('[data-models-aux-field="approval:model"]', 'aux-approval-2')
  await sleep(200)
  const writesBeforeIso = (await writeRequests(page)).length
  const beforeIso = readFileSync(auxPath, 'utf-8')
  // 先预览（预览块才带「下一步：确认写入」按钮），再确认写入
  await doPreview(page)
  const isoApplied = await doApply(page)
  const isoWrites = (await writeRequests(page)).slice(writesBeforeIso)
  const auxDisk2 = auxiliaryBlock(readFileSync(auxPath, 'utf-8'))
  check('★只改 approval：写请求只有一个、且只带 approval', isoWrites.length === 1 && isoWrites[0].body && isoWrites[0].body.slot === 'approval',
    JSON.stringify(isoWrites.map((r) => r.body && r.body.slot)))
  check('★改 approval 不影响 compression（逐槽位隔离）',
    JSON.stringify(auxDisk2['compression'] || {}) === compressionBefore,
    compressionBefore + ' vs ' + JSON.stringify(auxDisk2['compression'] || {}))
  check('approval 真的改了', String((auxDisk2['approval'] || {}).model || '') === 'aux-approval-2',
    'before=' + approvalBefore + ' after=' + JSON.stringify(auxDisk2['approval']))
  check('★改辅助槽位不影响 model / fallback_providers（逐段隔离）',
    JSON.stringify(modelFields(readFileSync(auxPath, 'utf-8'))) === JSON.stringify(modelFields(beforeAux)) &&
      JSON.stringify(fallbackChain(readFileSync(auxPath, 'utf-8'))) === JSON.stringify(fallbackChain(beforeAux)),
    'model=' + JSON.stringify(modelFields(readFileSync(auxPath, 'utf-8')).provider) + ' chain=' + fallbackChain(readFileSync(auxPath, 'utf-8')).length + ' 条')
  check('辅助槽位写入成功且回读通过', isoApplied.ok && isoApplied.rows.every((r) => r.ok === '1'), JSON.stringify(isoApplied.rows.map((r) => r.ok)))

  // 未改动的槽位零写请求：把值填成**界面自己报的当前值**（解析器比的正是这份现值）
  const zeroWritesBefore = (await writeRequests(page)).length
  // 写完会 refreshAll；等界面上的「当前」真的变成刚写进去的值，再拿它当输入
  await page.waitForFunction(
    () => {
      const node = document.querySelector('[data-models-aux-slot="approval"]')
      if (!node) return false
      const spans = Array.from(node.querySelectorAll('span'))
      const hit = spans.filter((s) => (s.textContent || '').indexOf('当前 ') === 0)[0]
      return Boolean(hit) && /aux-approval-2/.test(hit.textContent || '')
    },
    null,
    { timeout: 20000 }
  ).catch(() => {})
  await sleep(300)
  const currentShown = await page.evaluate(() => {
    const node = document.querySelector('[data-models-aux-slot="approval"]')
    if (!node) return null
    const spans = Array.from(node.querySelectorAll('span'))
    const hit = spans.filter((s) => (s.textContent || '').indexOf('当前 ') === 0)[0]
    return hit ? hit.textContent : null
  })
  const shownModel = currentShown ? String(currentShown).split('/').pop().trim() : ''
  check('界面把该槽位的当前值显示出来了（用于零请求判定）', Boolean(shownModel), JSON.stringify(currentShown))
  await page.fill('[data-models-aux-field="approval:model"]', shownModel)
  await sleep(300)
  const planDisabled = await page.evaluate(() => Boolean((document.querySelector('[data-models-editor-preview-ask]') || {}).disabled))
  const planError = await page.evaluate(() => ((document.querySelector('[data-models-editor-error]') || {}).textContent || ''))
  const planCount = await page.evaluate(() => (document.querySelector('[data-models-plan]') || {}).getAttribute
    ? document.querySelector('[data-models-plan]').getAttribute('data-models-plan')
    : '')
  check('★值等于现值时发不出去（与现值相同 ⇒ 零请求）', planDisabled === true,
    'disabled=' + planDisabled + ' plan=' + planCount + ' err=' + planError.slice(0, 80))
  const zeroWritesAfter = (await writeRequests(page)).length
  check('★这一路没有产生任何写请求', zeroWritesAfter === zeroWritesBefore, zeroWritesBefore + ' → ' + zeroWritesAfter)

  // ---- 回滚链路：快照回滚把 config.yaml 逐字节还原 ------------------------
  console.log('\n[F] 回滚链路：快照回滚逐字节还原')
  const beforeRollback = readFileSync(auxPath, 'utf-8')
  const isoSnap = (isoApplied.rows[0] ? String(isoApplied.rows[0].text) : '').match(/快照(\d{8}-\d{6}(?:-\d+)?)/)
  check('写入回执里带快照时间戳（可精确回滚到那一次）', Boolean(isoSnap), isoSnap ? isoSnap[1] : '（没有）')
  await page.click('[data-models-editor-close]')
  await sleep(300)
  await page.click('[data-models-tab="history"]')
  await sleep(900)
  const snapPick = await page.evaluate(() => {
    const hit = Array.from(document.querySelectorAll('[data-models-rollback]')).filter((n) => !n.disabled)[0]
    return hit ? hit.getAttribute('data-models-rollback') : ''
  })
  check('历史面板里能看到快照', Boolean(snapPick), 'ts=' + snapPick)
  const wantSnap = isoSnap ? isoSnap[1] : snapPick
  const exact = await page.$(`[data-models-rollback="${wantSnap}"]:not([disabled])`)
  if (exact) await exact.click()
  else await page.locator('[data-models-rollback]:not([disabled])').first().click()
  await page.waitForSelector('[data-models-snapshot-rollback-yes]', { timeout: 15000 })
  await page.click('[data-models-snapshot-rollback-yes]')
  const rolled = await page.waitForSelector('[data-models-batch-rollback-result]', { timeout: 120000 }).then(() => true).catch(() => false)
  check('★快照回滚走通', rolled)
  await sleep(500)
  const afterRollback = readFileSync(auxPath, 'utf-8')
  check('★回滚后 config.yaml 与回滚前不同（真的还原了）', afterRollback !== beforeRollback)
  check('★回滚后 config.yaml 与那次写入前逐字节相同（快照字节还原）', afterRollback === beforeIso,
    'sha256 ' + sha256(auxPath).slice(0, 16) + ' vs 期望 ' + createHash('sha256').update(beforeIso).digest('hex').slice(0, 16))

  // ---- F2. 批量「保持不变」：一个请求都不发（备用链 / 辅助槽位都算） --------
  console.log('\n[F2] 批量「保持不变」：四个段都不发请求')
  await page.click('[data-models-tab="agents"]')
  await sleep(500)
  const pairIds = ids.slice(0, 2)
  await page.click('[data-models-select-none]')
  await sleep(200)
  for (const id of pairIds) {
    await page.click(`[data-models-row-check="${id}"]`)
    await sleep(150)
  }
  await page.click('[data-models-editor-open]')
  await page.waitForSelector('[data-models-editor]', { timeout: 15000 })
  await sleep(400)
  const batchMode = await page.evaluate(() => ({
    mode: document.querySelector('[data-models-editor]').getAttribute('data-models-mode'),
    target: (document.querySelector('[data-models-editor-target]') || {}).textContent || '',
    editors: document.querySelectorAll('[data-models-editor]').length
  }))
  check('★单/批量共用同一个编辑器（页面上只有一个）', batchMode.editors === 1 && batchMode.mode === 'batch',
    batchMode.mode + ' / editors=' + batchMode.editors)
  check('批量态点名「已选 N 个 agent」', /已选/.test(batchMode.target), batchMode.target.slice(0, 50))
  const writesBeforeKeep = (await writeRequests(page)).length
  await pickScope(page, 'fallbacks')
  const chainKeepState = await page.evaluate(() => (document.querySelector('[data-models-chain]') || {}).getAttribute
    ? document.querySelector('[data-models-chain]').getAttribute('data-models-chain-mode')
    : '')
  const chainPreviewDisabled = await page.evaluate(() => Boolean((document.querySelector('[data-models-editor-preview-ask]') || {}).disabled))
  check('★批量备用链默认「保持不变」（不点就不改）', chainKeepState === 'keep', 'mode=' + chainKeepState)
  check('★保持不变时预览按钮禁用（发不出去）', chainPreviewDisabled === true)
  await pickScope(page, 'auxiliary')
  const auxKeep = await page.evaluate(() => ({
    slots: document.querySelectorAll('[data-models-aux-slot]').length,
    dirty: document.querySelectorAll('[data-models-aux-slot][data-models-aux-dirty="1"]').length,
    previewDisabled: Boolean((document.querySelector('[data-models-editor-preview-ask]') || {}).disabled)
  }))
  check('★批量辅助槽位默认全部「保持不变」（脏标记 0）', auxKeep.slots >= 1 && auxKeep.dirty === 0,
    'slots=' + auxKeep.slots + ' dirty=' + auxKeep.dirty)
  check('★批量辅助槽位未改动时发不出去', auxKeep.previewDisabled === true)
  await pickScope(page, 'model')
  const writesAfterKeep = (await writeRequests(page)).length
  check('★这一路零写请求（「保持不变」不产生请求）', writesAfterKeep === writesBeforeKeep,
    writesBeforeKeep + ' → ' + writesAfterKeep)

  // ---- F3. 旧段不退化：scope=fallback（备用首条）仍能出预览 ----------------
  console.log('\n[F3] 旧段 scope=fallback（备用首条）仍可用')
  await page.click('[data-models-editor-close]').catch(() => {})
  await sleep(250)
  await page.click('[data-models-select-none]').catch(() => {})
  await sleep(250)
  // 注意：D 段把 code-worker 的备用链清空了，这里要挑一个**还有链**的 agent
  const fbAgent = ids.filter((id) => fallbackChain(readFileSync(pathOf(id), 'utf-8')).length > 0)[0] || ids[0]
  console.log('  [F3] 用 ' + fbAgent + '（备用链 ' + fallbackChain(readFileSync(pathOf(fbAgent), 'utf-8')).length + ' 条）')
  await openSingle(page, fbAgent)
  await pickScope(page, 'fallback')
  const fbWritesBefore = (await writeRequests(page)).length
  const fbState = await page.evaluate(() => {
    const select = document.querySelector('[data-models-editor-provider]')
    return {
      hasProvider: Boolean(select),
      hasModel: Boolean(document.querySelector('[data-models-editor-model]')),
      current: select ? select.value : '',
      model: (document.querySelector('[data-models-editor-model]') || {}).value || '',
      modes: Array.from(document.querySelectorAll('[data-models-field-mode]')).map((n) => n.getAttribute('data-models-field-mode')),
      options: select ? Array.from(select.options).map((o) => o.value).filter(Boolean) : []
    }
  })
  check('★备用首条段仍有 provider / 模型控件（旧语义不退化）',
    fbState.hasProvider && fbState.hasModel && fbState.modes.length === 2, JSON.stringify(fbState.modes))
  check('★备用首条段预填该 agent 现在的第一条（不是空表单）',
    Boolean(fbState.current) && Boolean(fbState.model), JSON.stringify({ provider: fbState.current, model: fbState.model }))
  const fbPick = fbState.options.filter((name) => name !== fbState.current)[0] || ''
  check('备用首条段能选到另一个 provider（用于构造差异）', Boolean(fbPick), 'current=' + fbState.current + ' pick=' + fbPick)
  await page.selectOption('[data-models-editor-provider]', fbPick)
  await sleep(400)
  const fbPlan = await page.evaluate(() => (document.querySelector('[data-models-plan]') || {}).getAttribute
    ? document.querySelector('[data-models-plan]').getAttribute('data-models-plan')
    : '')
  check('备用首条：选了 provider 之后算得出写请求', Number(fbPlan) >= 1, 'plan=' + fbPlan)
  const fbPreview = await doPreview(page)
  const fbKeys = fbPreview[0] ? fbPreview[0].changes.map((c) => c.key) : []
  check('★备用首条预览只动 fallback_providers[0]',
    fbKeys.length > 0 && fbKeys.every((key) => key.indexOf('fallback_providers[0]') === 0), JSON.stringify(fbKeys))
  await page.click('[data-models-preview-cancel]').catch(() => {})
  await sleep(200)
  const fbWritesAfter = (await writeRequests(page)).length
  check('★备用首条这一路零写请求（只看预览就取消）', fbWritesAfter === fbWritesBefore,
    fbWritesBefore + ' → ' + fbWritesAfter)

  // ---- M. MoA 预设（项 2：moa 可选 + 预设来源三种状态如实显示） ----------
  console.log('\n[M] MoA：provider 可选 moa、候选=该 agent 的预设名、来源如实显示')
  const moaHost = ids.filter((id) => /^moa:\s*$/m.test(readFileSync(pathOf(id), 'utf-8')))[0] || ''
  const noMoaHost = ids.filter((id) => !/^moa:\s*$/m.test(readFileSync(pathOf(id), 'utf-8')))[0] || ''
  console.log('  [M] 有 moa 段：' + (moaHost || '（无）') + ' / 无 moa 段：' + (noMoaHost || '（无）'))

  if (moaHost) {
    const moaBefore = readFileSync(pathOf(moaHost), 'utf-8')
    await openSingle(page, moaHost)
    await pickScope(page, 'model')
    const moaOptions = await page.evaluate(() => {
      const select = document.querySelector('[data-models-editor-provider]')
      return select ? Array.from(select.options).map((o) => o.value) : []
    })
    check('★provider 下拉里能选到 moa（虚拟 provider）', moaOptions.indexOf('moa') >= 0,
      JSON.stringify(moaOptions.slice(0, 8)))
    await page.selectOption('[data-models-editor-provider]', 'moa')
    await sleep(1800)
    const moaCand = await page.evaluate(() => {
      const box = document.querySelector('[data-models-candidates-source]')
      const picks = Array.from(document.querySelectorAll('[data-models-editor-model-pick]')).map((n) => n.getAttribute('data-models-editor-model-pick'))
      const note = document.querySelector('[data-models-candidates-note]')
      return {
        source: box ? box.getAttribute('data-models-candidates-source') : '',
        count: box ? box.getAttribute('data-models-candidates-count') : '',
        picks,
        note: note ? (note.textContent || '') : ''
      }
    })
    check('★选 moa 后候选来自 moa 预设（source=moa-presets）', moaCand.source === 'moa-presets', 'source=' + moaCand.source)
    check('★候选就是该 agent 的预设名（不是 balance 的模型）',
      moaCand.picks.length > 0 && moaCand.picks.indexOf('deepseek-v4.1-flash') === -1 && moaCand.picks.indexOf('glm-5.3-flash') === -1,
      JSON.stringify(moaCand.picks.slice(0, 10)))
    const moaCur = modelFields(moaBefore)
    const moaPick = moaCand.picks.filter((name) => name !== moaCur.default)[0] || moaCand.picks[0] || ''
    check('有可换的预设（与当前不同）', Boolean(moaPick), '当前 ' + moaCur.default + ' → 选 ' + moaPick)
    await page.click(`[data-models-editor-model-pick="${moaPick}"]`)
    await sleep(400)
    const moaWritesBefore = (await writeRequests(page)).length
    const moaPreview = await doPreview(page)
    const moaChanges = moaPreview[0] ? moaPreview[0].changes : []
    check('★预览里 model.provider → moa 且 model.default → 预设名',
      moaChanges.some((c) => c.key === 'model.provider' && c.to === 'moa') &&
        moaChanges.some((c) => c.key === 'model.default' && c.to === moaPick),
      JSON.stringify(moaChanges))
    const moaApplied = await doApply(page)
    check('★MoA 预设写入成功并回读通过', moaApplied.ok && moaApplied.rows.every((r) => r.ok === '1'),
      JSON.stringify(moaApplied.rows.map((r) => r.ok)))
    const moaAfter = modelFields(readFileSync(pathOf(moaHost), 'utf-8'))
    check('★保存后回读：model.provider == moa 且 model.default == 预设名',
      moaAfter.provider === 'moa' && moaAfter.default === moaPick,
      'provider=' + moaAfter.provider + ' default=' + moaAfter.default + ' 期望=' + moaPick)
    check('★moa 的 base_url 是本地标记（不联网）', String(moaAfter.base_url || '') === 'moa://local',
      'base_url=' + moaAfter.base_url)
    check('★写入 MoA 没有产生额外的 key 写入请求（只有这一个写请求）',
      (await writeRequests(page)).length === moaWritesBefore + 1,
      moaWritesBefore + ' → ' + (await writeRequests(page)).length)
  } else {
    check('★有 moa 段的 agent 存在（MoA 预设验收前提）', false, '沙盒里没有 agent 带 moa: 段')
  }

  if (noMoaHost) {
    await openSingle(page, noMoaHost)
    await pickScope(page, 'model')
    await page.selectOption('[data-models-editor-provider]', 'moa')
    await sleep(1800)
    const noMoa = await page.evaluate(() => {
      const box = document.querySelector('[data-models-candidates-source]')
      const note = document.querySelector('[data-models-candidates-note]')
      return {
        source: box ? box.getAttribute('data-models-candidates-source') : '',
        count: box ? box.getAttribute('data-models-candidates-count') : '',
        picks: Array.from(document.querySelectorAll('[data-models-editor-model-pick]')).length,
        note: note ? (note.textContent || '') : ''
      }
    })
    check('★没有 moa 段的 agent：来源如实报 moa-presets-missing（不假装有预设）',
      noMoa.source === 'moa-presets-missing', 'agent=' + noMoaHost + ' source=' + noMoa.source + ' count=' + noMoa.count)
    check('★没有 moa 段时不列出任何预设候选', noMoa.picks === 0, 'picks=' + noMoa.picks)
    check('★界面把「用宿主内置默认配方」说清楚', /内置默认配方|没有 moa/.test(noMoa.note), noMoa.note.slice(0, 80))
  } else {
    check('★无 moa 段的 agent 存在（三种来源状态验收前提）', false, '沙盒里所有 agent 都有 moa: 段')
  }

  // ---- R. 思考强度（scope=reasoning：默认「保持不变」+ 真写入 + 回读一致） ---
  console.log('\n[R] 思考强度：按后端能力开关 + 默认「保持不变」+ 真写入回读 + 逐段隔离')
  await page.click('[data-models-editor-close]').catch(() => {})
  await sleep(250)
  await page.click('[data-models-select-none]').catch(() => {})
  await sleep(250)
  const reAgent = ids.indexOf('default') >= 0 ? 'default' : ids[0]
  const rePath = pathOf(reAgent)
  const reBefore = readFileSync(rePath, 'utf-8')
  const reBeforeEffort = reasoningOf(reBefore)
  await openSingle(page, reAgent)
  await pickScope(page, 'reasoning')
  await sleep(400)
  const reWritesBefore = (await writeRequests(page)).length
  const reUi = await page.evaluate(() => {
    const select = document.querySelector('[data-models-editor-reasoning]')
    const chip = document.querySelector('[data-models-reasoning-mode]')
    const note = document.querySelector('[data-models-reasoning-note]')
    return {
      exists: Boolean(select),
      disabled: select ? select.disabled === true : null,
      writable: select ? select.getAttribute('data-models-reasoning-writable') : '',
      mode: chip ? chip.getAttribute('data-models-reasoning-mode') : '',
      current: select ? select.getAttribute('data-models-reasoning-value') : '',
      choices: select ? Array.from(select.options).map((o) => o.value) : [],
      note: note ? (note.textContent || '') : ''
    }
  })
  check('★思考强度控件在（独立的「思考强度」写入段里）', reUi.exists === true)
  check('★后端已声明 scope=reasoning ⇒ 控件可用（不是禁用占位、不是点了静默失败）',
    reUi.writable === '1' && reUi.disabled === false, 'writable=' + reUi.writable + ' disabled=' + reUi.disabled)
  check('★默认状态是「保持不变」', reUi.mode === 'keep', 'mode=' + reUi.mode)
  check('★选项齐备（空=保持不变 + none + 7 档，共 9 项）',
    reUi.choices.length === 9 &&
      ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].every((v) => reUi.choices.indexOf(v) >= 0),
    JSON.stringify(reUi.choices))
  check('★界面明说「保持不变 ⇒ 零写请求」', /零写请求/.test(reUi.note), reUi.note.slice(0, 90))
  check('★控件显示当前值（如实展示）', Boolean(reUi.current), '当前=' + reUi.current)
  const reAfter = readFileSync(rePath, 'utf-8')
  check('★「保持不变」时零写请求、config.yaml 未变', reAfter === reBefore &&
    (await writeRequests(page)).length === reWritesBefore,
    'writes=' + reWritesBefore + '→' + (await writeRequests(page)).length)

  // 真写入：选一个与现值不同的档位 → 预览（零写入）→ 二次确认 → 写入 → 磁盘回读
  const reTarget = reBeforeEffort === 'low' ? 'high' : 'low'
  await page.selectOption('[data-models-editor-reasoning]', reTarget)
  await sleep(300)
  check('★选了档位但还没确认：不写盘', readFileSync(rePath, 'utf-8') === reBefore)
  const reWritesBeforeApply = (await writeRequests(page)).length
  const rePreview = await doPreview(page)
  check('★预览里出现 agent.reasoning_effort → ' + reTarget,
    rePreview.some((row) => row.agent === reAgent && row.changes.some((c) => /reasoning/.test(c.key) && c.to === reTarget)),
    JSON.stringify(rePreview).slice(0, 220))
  check('★预览零写入（预览值 ≠ 已写入）',
    (await writeRequests(page)).length === reWritesBeforeApply && readFileSync(rePath, 'utf-8') === reBefore)
  const reApplied = await doApply(page)
  const reWrites = (await writeRequests(page)).slice(reWritesBeforeApply)
  check('★写入只发一个请求、scope=reasoning 且只带 reasoning_effort',
    reWrites.length === 1 && Boolean(reWrites[0].body) &&
      reWrites[0].body.scope === 'reasoning' &&
      String(reWrites[0].body.reasoning_effort) === reTarget &&
      !('provider' in reWrites[0].body) && !('model' in reWrites[0].body),
    JSON.stringify(reWrites.map((r) => r.body)))
  check('写入成功并逐条回读通过', reApplied.ok && reApplied.rows.length === 1 && reApplied.rows[0].ok === '1',
    JSON.stringify(reApplied.rows))
  const reAfterWrite = readFileSync(rePath, 'utf-8')
  check('★回读一致：磁盘 agent.reasoning_effort = ' + reTarget, reasoningOf(reAfterWrite) === reTarget,
    'before=' + reBeforeEffort + ' after=' + reasoningOf(reAfterWrite))
  check('★改思考强度不影响 model / fallback_providers / auxiliary（逐段隔离）',
    JSON.stringify(modelFields(reAfterWrite)) === JSON.stringify(modelFields(reBefore)) &&
      JSON.stringify(fallbackChain(reAfterWrite)) === JSON.stringify(fallbackChain(reBefore)) &&
      JSON.stringify(auxiliaryBlock(reAfterWrite)) === JSON.stringify(auxiliaryBlock(reBefore)),
    'model=' + JSON.stringify(modelFields(reAfterWrite).provider) + ' chain=' + fallbackChain(reAfterWrite).length +
      ' 条 aux=' + Object.keys(auxiliaryBlock(reAfterWrite)).length + ' 个')
  // 反向：改主模型（provider → moa）时，reasoning_effort 一个字节都不许动
  await pickScope(page, 'model')
  await page.selectOption('[data-models-editor-provider]', 'moa').catch(() => {})
  await sleep(1200)
  const reReasonMid = readFileSync(rePath, 'utf-8')
  check('★只点 provider 不写盘（仍是预览前状态）', reReasonMid === reAfterWrite)
  check('★改模型不会顺带覆盖 reasoning_effort（agent 段逐字未变）', reasoningOf(reReasonMid) === reTarget,
    'reasoning=' + reasoningOf(reReasonMid))

  // ---- P. MoA 配方：真编辑器（每条参考位独立思考强度 + 引用保护 409） ------
  console.log('\n[P] MoA 配方：内容可编辑、写入回读一致、被引用预设 409 原文透出')
  await page.click('[data-models-editor-close]').catch(() => {})
  await sleep(250)
  await page.click('[data-models-select-none]').catch(() => {})
  await sleep(250)
  await page.click('[data-models-tab="presets"]')
  await sleep(1500)
  const pWritesBefore = (await writeRequests(page)).length
  const moaPanel = await page.evaluate(() => {
    const box = document.querySelector('[data-models-moa-presets]')
    if (!box) return null
    const presets = Array.from(document.querySelectorAll('[data-models-moa-preset]')).map((n) => ({
      name: n.getAttribute('data-models-moa-preset'),
      referenced: n.getAttribute('data-models-moa-preset-referenced'),
      disabled: n.getAttribute('data-models-moa-preset-disabled')
    }))
    return {
      count: box.getAttribute('data-models-moa-presets'),
      source: box.getAttribute('data-models-moa-source'),
      defaultPreset: box.getAttribute('data-models-moa-default'),
      dangling: box.getAttribute('data-models-moa-dangling'),
      backend: box.getAttribute('data-models-moa-backend'),
      presets,
      note: (document.querySelector('[data-models-moa-editor-note]') || {}).textContent || '',
      consoleDraftLabel: (document.querySelector('[data-models-presets]') || {}).textContent || ''
    }
  })
  check('★MoA 配方面板存在（预设页签里）', Boolean(moaPanel))
  const pAgent = ids.indexOf('default') >= 0 ? 'default' : ids[0]
  if (moaPanel) {
    check('★面板列出该 agent 的 moa 预设（来源如实）',
      Number(moaPanel.count) >= 1 && ['moa-presets', 'moa-presets-empty', 'moa-presets-missing'].indexOf(moaPanel.source) >= 0,
      'count=' + moaPanel.count + ' source=' + moaPanel.source)
    check('★默认预设被标出来', Boolean(moaPanel.defaultPreset), 'default=' + moaPanel.defaultPreset)
    const defaultRow = moaPanel.presets.filter((p) => p.name === moaPanel.defaultPreset)[0]
    check('★default_preset 指向的那个预设被标「被引用」', Boolean(defaultRow) && defaultRow.referenced === '1',
      JSON.stringify(defaultRow))
    check('★后端已上线 /models/moa/presets ⇒ 编辑器就绪（不是「后端待支持」占位）',
      moaPanel.backend === 'ready' && !/后端待支持/.test(moaPanel.note),
      'backend=' + moaPanel.backend + ' note=' + moaPanel.note.slice(0, 60))
    check('★console 草稿与 MoA 配方分开摆（不混为一谈）', /不是 MoA 配方/.test(moaPanel.consoleDraftLabel))
  }
  check('★MoA 配方面板零写请求（只读展示不发请求）',
    (await writeRequests(page)).length === pWritesBefore,
    pWritesBefore + ' → ' + (await writeRequests(page)).length)
  await page.screenshot({ path: join(SHOTS, 'moa-presets-1350.png'), fullPage: true })
  console.log('  [shot] ' + join(SHOTS, 'moa-presets-1350.png'))

  // 真编辑：挑一个**没被引用**、且有 reference_models 的预设
  const pPath = pathOf(pAgent)
  const pApi = await apiGet('/models/moa/presets?agent=' + encodeURIComponent(pAgent))
  const pPresets = Array.isArray(pApi.presets) ? pApi.presets : []
  const editable = pPresets.filter((p) => !p.referenced && Array.isArray(p.reference_models) && p.reference_models.length)[0]
  check('验收前提：有一个未被引用、且带 reference_models 的预设', Boolean(editable),
    pPresets.map((p) => p.name + (p.referenced ? '(引用)' : '')).join(','))
  if (editable) {
    const pBeforeText = readFileSync(pPath, 'utf-8')
    const pWritesBeforeEdit = (await writeRequests(page)).length
    await page.click(`[data-models-moa-edit="${editable.name}"]`)
    await sleep(500)
    const refUi = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('[data-models-moa-ref]'))
      return {
        count: rows.length,
        withProvider: rows.filter((r) => r.querySelector('[data-models-moa-ref-provider]')).length,
        withModel: rows.filter((r) => r.querySelector('[data-models-moa-ref-model]')).length,
        withReasoning: rows.filter((r) => r.querySelector('[data-models-moa-ref-reasoning]')).length,
        withEnabled: rows.filter((r) => r.querySelector('[data-models-moa-ref-enabled]')).length,
        hasAgg: Boolean(document.querySelector('[data-models-moa-agg-provider]')),
        hasAdd: Boolean(document.querySelector('[data-models-moa-ref-add]')),
        saveDisabled: (document.querySelector('[data-models-moa-editor-save]') || {}).disabled === true
      }
    })
    check('★参考位按预设内容逐条生成（每条 provider + model + 思考强度 + 启用）',
      refUi.count === editable.reference_models.length && refUi.withProvider === refUi.count &&
        refUi.withModel === refUi.count && refUi.withReasoning === refUi.count && refUi.withEnabled === refUi.count,
      JSON.stringify(refUi))
    check('★参考位可增删（有「加一条参考模型」按钮、每行有删除）',
      refUi.hasAdd && (await page.evaluate(() => document.querySelectorAll('[data-models-moa-ref-del]').length)) === refUi.count,
      'add=' + refUi.hasAdd)
    check('★保存按钮可用（后端已就绪、配置无红灯）', refUi.saveDisabled === false, 'disabled=' + refUi.saveDisabled)
    // 第 2 层 reasoning：只改第一个参考位的思考强度
    const oldEffort = String(editable.reference_models[0].reasoning_effort || '')
    const newEffort = oldEffort === 'xhigh' ? 'max' : 'xhigh'
    await page.selectOption('[data-models-moa-ref-reasoning="0"]', newEffort)
    await sleep(300)
    check('★改了参考位但还没保存：不写盘', readFileSync(pPath, 'utf-8') === pBeforeText)
    await page.click('[data-models-moa-editor-save]')
    const pOk = await page.waitForSelector('[data-models-moa-result]', { timeout: 120000 }).then(() => true).catch(() => false)
    const pResult = await page.evaluate(() => {
      const node = document.querySelector('[data-models-moa-result]')
      const errNode = document.querySelector('[data-models-moa-error]')
      return {
        state: node ? node.getAttribute('data-models-moa-result') : '',
        text: node ? (node.textContent || '') : '',
        error: errNode ? (errNode.textContent || '') : ''
      }
    })
    check('写入返回并报成功（逐键校验）', pOk && pResult.state === 'ok', JSON.stringify(pResult).slice(0, 220))
    const pWrites = (await writeRequests(page)).slice(pWritesBeforeEdit)
    check('★写入只发一个 PUT /models/moa/presets/<name>（confirm 带在体里）',
      pWrites.length === 1 && /\/models\/moa\/presets\//.test(pWrites[0].path || '') &&
        String((pWrites[0].method || '')).toUpperCase() === 'PUT' && pWrites[0].body && pWrites[0].body.confirm === true,
      JSON.stringify(pWrites.map((r) => (r.method || '') + ' ' + (r.path || ''))))
    const pAfterApi = await apiGet('/models/moa/presets?agent=' + encodeURIComponent(pAgent))
    const pAfter = (Array.isArray(pAfterApi.presets) ? pAfterApi.presets : []).filter((p) => p.name === editable.name)[0]
    check('★回读一致：该参考位的 reasoning_effort = ' + newEffort,
      Boolean(pAfter) && String((pAfter.reference_models || [])[0] && pAfter.reference_models[0].reasoning_effort || '') === newEffort,
      'before=' + (oldEffort || '(空)') + ' after=' + JSON.stringify(((pAfter || {}).reference_models || [])[0] || {}))
    check('★参考位条数与顺序不变（整条替换没打乱）',
      Boolean(pAfter) && (pAfter.reference_models || []).length === editable.reference_models.length &&
        (pAfter.reference_models || []).every((item, at) => String(item.model || '') === String(editable.reference_models[at].model || '') &&
          String(item.provider || '') === String(editable.reference_models[at].provider || '')),
      JSON.stringify((pAfter || {}).reference_models))
    check('★该预设的其它键原样保留（enabled / fanout / degraded_reference_policy）',
      Boolean(pAfter) && String(pAfter.fanout === null || pAfter.fanout === undefined ? '' : pAfter.fanout) ===
        String(editable.fanout === null || editable.fanout === undefined ? '' : editable.fanout) &&
        String(pAfter.degraded_reference_policy === null || pAfter.degraded_reference_policy === undefined ? '' : pAfter.degraded_reference_policy) ===
          String(editable.degraded_reference_policy === null || editable.degraded_reference_policy === undefined ? '' : editable.degraded_reference_policy),
      'fanout=' + JSON.stringify((pAfter || {}).fanout) + ' degraded=' + JSON.stringify((pAfter || {}).degraded_reference_policy))
    const othersBefore = pPresets.filter((p) => p.name !== editable.name)
    const othersAfter = (Array.isArray(pAfterApi.presets) ? pAfterApi.presets : []).filter((p) => p.name !== editable.name)
    check('★其它预设逐字段未变（只动了点名的那个）',
      JSON.stringify(othersBefore.map((p) => [p.name, p.reference_models, p.aggregator, p.enabled])) ===
        JSON.stringify(othersAfter.map((p) => [p.name, p.reference_models, p.aggregator, p.enabled])),
      othersAfter.map((p) => p.name).join(','))
  }

  // 被引用的预设：默认拒改 → 后端 409 preset_referenced 原文透出，磁盘一个字节不动
  const referencedPreset = pPresets.filter((p) => p.referenced)[0]
  check('验收前提：存在被引用的预设', Boolean(referencedPreset),
    pPresets.map((p) => p.name + (p.referenced ? '(引用)' : '')).join(','))
  if (referencedPreset) {
    const refBeforeText = readFileSync(pPath, 'utf-8')
    await page.click(`[data-models-moa-edit="${referencedPreset.name}"]`)
    await sleep(400)
    const refRows = await page.evaluate(() => document.querySelectorAll('[data-models-moa-ref]').length)
    if (refRows > 0) {
      const cur = String((referencedPreset.reference_models || [])[0] && referencedPreset.reference_models[0].reasoning_effort || '')
      await page.selectOption('[data-models-moa-ref-reasoning="0"]', cur === 'max' ? 'xhigh' : 'max')
      await sleep(200)
      await page.click('[data-models-moa-editor-save]')
      const refDone = await page.waitForSelector('[data-models-moa-error]', { timeout: 60000 }).then(() => true).catch(() => false)
      const refErr = await page.evaluate(() => {
        const node = document.querySelector('[data-models-moa-error]')
        const warn = document.querySelector('[data-models-moa-ref-warn]')
        return { error: node ? (node.textContent || '') : '', warn: warn ? (warn.textContent || '') : '' }
      })
      check('★被引用预设默认拒改：界面显示 409 preset_referenced 原文（不是静默失败）',
        refDone && /preset_referenced|拒绝修改/.test(refErr.error), refErr.error.slice(0, 160))
      check('★被拒时磁盘一个字节都没动', readFileSync(pPath, 'utf-8') === refBeforeText)
      check('★界面提前说明「被引用」并给 allow_referenced_edit 显式确认开关',
        /正被引用/.test(refErr.warn) && (await page.evaluate(() => Boolean(document.querySelector('[data-models-moa-allow-referenced]')))))
    } else {
      check('被引用预设也有参考位可改（验收前提）', false, referencedPreset.name)
    }
  }

  // 悬空引用红灯：沙盒里把 default_preset 指向一个不存在的预设，界面必须红字报出来
  const dangleAgent = ids.indexOf('default') >= 0 ? 'default' : ids[0]
  const danglePath = pathOf(dangleAgent)
  const dangleBefore = readFileSync(danglePath, 'utf-8')
  const hasMoaBlock = /^moa:\s*$/m.test(dangleBefore)
  check('悬空引用验收前提：该 agent 有 moa: 段', hasMoaBlock, 'agent=' + dangleAgent)
  if (hasMoaBlock) {
    const mangled = dangleBefore.replace(/^(\s*)default_preset:\s*(.*)$/m, '$1default_preset: __no_such_preset__')
    writeFileSync(danglePath, mangled, 'utf-8')
    try {
    // 主动刷新：overview / moa 视图都是缓存查询，不点「刷新」不会重取
    await page.click('[data-models-toolbar] button:has-text("刷新")').catch(() => {})
    await sleep(2000)
    const danglingView = await page.evaluate(() => {
      const box = document.querySelector('[data-models-moa-presets]')
      const warn = document.querySelector('[data-models-moa-dangling-warn]')
      return {
        flag: box ? box.getAttribute('data-models-moa-dangling') : '',
        warn: warn ? (warn.textContent || '') : ''
      }
    })
    check('★悬空引用被红字报出来（default_preset 指向不存在的预设）',
      danglingView.flag === '1' && /悬空引用/.test(danglingView.warn),
      'flag=' + danglingView.flag + ' warn=' + danglingView.warn.slice(0, 80))
    } finally {
      writeFileSync(danglePath, dangleBefore, 'utf-8')
      await sleep(300)
    }
  }

  // ---- G. 截图：provider 目录 / 备用链 / 辅助槽位（两种宽度） -------------
  console.log('\n[G] 截图：provider 目录 / 备用链 / 辅助槽位')
  await page.click('[data-models-editor-close]').catch(() => {})
  await sleep(250)
  await page.click('[data-models-tab="agents"]').catch(() => {})
  await sleep(400)
  await page.click('[data-models-select-none]').catch(() => {})
  await sleep(250)
  const shotAgent = ids.indexOf('default') >= 0 ? 'default' : ids[0]
  await openSingle(page, shotAgent)
  await pickScope(page, 'fallbacks')
  const editorShots = []
  for (const scopeName of ['fallbacks', 'auxiliary']) {
    await pickScope(page, scopeName)
    await sleep(400)
    for (const width of [1350, 900]) {
      await page.setViewportSize({ width, height: 900 })
      await sleep(650)
      const info = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: window.innerWidth
      }))
      check(`★${scopeName} 编辑器 ${width}px 无横向溢出`, info.scrollWidth <= info.innerWidth + 1,
        info.scrollWidth + ' vs ' + info.innerWidth)
      const node = await page.$('[data-models-editor]')
      if (node) {
        await node.scrollIntoViewIfNeeded()
        await sleep(200)
        const file = join(SHOTS, `${scopeName}-editor-${width}.png`)
        await node.screenshot({ path: file })
        editorShots.push(file)
        console.log('  [shot] ' + file)
      }
      const full = join(SHOTS, `${scopeName}-page-${width}.png`)
      await page.screenshot({ path: full })
      console.log('  [shot] ' + full)
    }
  }
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(400)
  await page.click('[data-models-editor-close]').catch(() => {})
  await sleep(250)
  await page.click('[data-models-tab="providers"]')
  await sleep(700)
  for (const width of [1350, 900]) {
    await page.setViewportSize({ width, height: 900 })
    await sleep(650)
    const file = join(SHOTS, `providers-${width}.png`)
    await page.screenshot({ path: file })
    console.log('  [shot] ' + file)
  }
  await page.setViewportSize({ width: 1350, height: 900 })
  await sleep(300)
  check('编辑器截图两种宽度不是同一张（内容真的重排了）',
    editorShots.length === 4 &&
      createHash('sha256').update(readFileSync(editorShots[0])).digest('hex') !==
        createHash('sha256').update(readFileSync(editorShots[1])).digest('hex'))

  // ---- H. 零报错 + 真 home 零污染 ----------------------------------------
  console.log('\n[H] 浏览器零报错 + 真 home 零污染')
  // [P] 段故意让后端回一次 409（被引用预设拒改）：浏览器会在 console 留一条 409，
  // 那是**验收证据**，不是缺陷。把它单独摘出来断言，其余 console error 一律不许有。
  const intended409 = consoleErrors.filter((line) => /status of 409/.test(line) && /moa\/presets/.test(line))
  const unexpected = consoleErrors.filter((line) => intended409.indexOf(line) === -1)
  check('零 console error（除验收故意触发的那 1 条 409）', unexpected.length === 0, unexpected.slice(0, 3).join(' | '))
  check('★故意触发的 409 真的发生了（被引用预设拒改走的是真请求，不是前端假报）',
    intended409.length === 1, intended409.join(' | ').slice(0, 160))
  check('零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
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
    if (fixture && fixture.child && fixture.child.pid) {
      // Windows 上单发 kill() 收不掉在 uvicorn 线程里 sleep 的夹具，会残留占端口；
      // 按 PID 连子进程一起收，下一轮才不会打到别的沙盒里。
      spawnSync('taskkill', ['/PID', String(fixture.child.pid), '/T', '/F'], { stdio: 'ignore' })
      fixture.child.kill()
    }
  } catch (e) {
    /* ignore */
  }
}

console.log('\nPASS=' + pass + ' FAIL=' + failures.length + ' TOTAL=' + (pass + failures.length))
if (failures.length) {
  console.log('MODELS-STRUCTURED-PROBE-FAILED')
  failures.forEach((item) => console.log('  - ' + item))
  process.exitCode = 1
} else {
  console.log('MODELS-STRUCTURED-PROBE-OK')
}
