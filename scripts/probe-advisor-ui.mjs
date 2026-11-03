/**
 * hermes-console · Advisor 分区真浏览器验收（Playwright + 真产物 + 真后端夹具）
 *
 * 跑法::
 *
 *   node plugins/hermes-console/scripts/probe-advisor-ui.mjs                     # 验当前 dist
 *   node plugins/hermes-console/scripts/probe-advisor-ui.mjs --bundle <旧产物> --label before --facts
 *   node plugins/hermes-console/scripts/probe-advisor-ui.mjs --widths 900,1350 --shots D:/tmp/shots
 *
 * 三层都是真的：
 *   1. **真产物**：``--bundle`` 指定的那份 plugin.js（默认 ``dashboard/dist/index.js``）原样加载，
 *      只套一层 harness（react / react-dom 一起 bundle，否则会拿到第二份 React）。
 *   2. **真后端**：``scripts/advisor_ui_fixture.py`` 按 dashboard 的方式加载**真实的**
 *      ``dashboard/plugin_api.py``，home 指向沙盒（台账是真实台账的拷贝），所以列表正文、
 *      severity、目标来源都是真数据，而设置写操作落在沙盒里。
 *   3. **真浏览器**：Playwright + 系统 Edge（Chromium 内核）。
 *
 * 覆盖本卡验收：列表选中 / 详情切换 / 开关切换 / 保存 / 展开折叠 / 筛选 / 切换 agent /
 * 未改动零写请求 / 模式块 / 流水与 504 / 设置逐项来源层 / 900 与 1350 无溢出 /
 * 零 console error / 零 pageerror。每个控件点击前都记录 ``elementFromPoint`` 命中什么。
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
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_ADV_FIXTURE_PORT || 9195)
const SITE_PORT = Number(process.env.HC_ADV_SITE_PORT || 9196)
const API = '/api/plugins/hermes-console'

function argOf(name, fallback) {
  const i = process.argv.indexOf(name)
  return i > -1 ? process.argv[i + 1] : fallback
}
const BUNDLE = resolve(argOf('--bundle', join(DIST, 'index.js')))
const LABEL = argOf('--label', 'current')
const EXPECT_OK = process.argv.includes('--expect-ok')
const WIDTHS = String(argOf('--widths', '1350,900')).split(',').map((n) => Number(n.trim())).filter(Boolean)
const SHOTS = resolve(argOf('--shots', join(PLUGIN, 'scripts', '_probe_out', 'advisor-ui-' + LABEL)))
// 整页截图用的视口高度：面板是绝对定位滚动容器，viewport 矮就只能拍到一屏；
// 拉高视口只改「能看到多少」，不改行内宽度，所以容器查询档位不变。
const SHOT_HEIGHT = Number(argOf('--shot-height', '2400'))

let pass = 0
let skipped = 0
const skips = []
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
function skip(name, why) {
  // 数据不支持这条断言的前提（本批 20 行里没有这类行）：记 SKIP，不判红。
  // 「界面上必须如实标记」这件事另有一条与后端三态逐行对账的断言兜住，不会被放过。
  skipped += 1
  skips.push(name + (why ? '  — ' + why : ''))
  console.log('  SKIP ' + name + (why ? '  — ' + why : ''))
}
function fact(name, ok, extra) {
  // --facts 模式（before 组）：只记录事实，不断言
  facts[name] = { ok: Boolean(ok), extra: extra === undefined ? '' : String(extra) }
  console.log('  FACT ' + name + ' = ' + String(ok) + (extra ? '  — ' + extra : ''))
}
const assertOrFact = (name, ok, extra) => (EXPECT_OK ? check(name, ok, extra) : fact(name, ok, extra))
const assertOrSkip = (name, supported, ok, extra, why) => {
  if (!EXPECT_OK) return fact(name, ok, extra)
  return supported ? check(name, ok, extra) : skip(name, why)
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// --------------------------------------------------------------- 临时工作区
const WORK = mkdtempSync(join(tmpdir(), 'hc-adv-ui-probe-'))
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

// harness：真 ctx（rest 打真后端、记录每个请求的 method/path/body/status），storage 用内存
const HARNESS = `
import rtDefault from 'react/jsx-runtime'
import { createElement } from 'react'
import { createRoot } from 'react-dom/client'

// 诊断：把 jsxs 包一层，记录「children 不是数组」的调用（React 19 会对它报
// "Static children should always be an array"）。要在 entry 之前装好，所以 entry 用动态 import。
const origJsxs = rtDefault.jsxs
rtDefault.jsxs = function (type, props, key) {
  if (props && props.children !== undefined && props.children !== null && !Array.isArray(props.children)) {
    window.__badJsxs = window.__badJsxs || []
    if (window.__badJsxs.length < 24) {
      window.__badJsxs.push({
        type: typeof type === 'string' ? type : 'component',
        cls: String((props && props.className) || '').slice(0, 60),
        childType: typeof props.children,
        sample: String(props.children).slice(0, 60),
        stack: String(new Error('bad-jsxs').stack || '').split('\\n').slice(1, 6).join(' <- ')
      })
    }
  }
  return origJsxs(type, props, key)
}
// 另一个方向：jsx（单子节点原语）拿到**数组**时，React 不会替数组元素打 validated 标记，
// react-dom 在协调时就会报「unique key」。这里把这种调用连同元素 key 记下来。
const origJsx = rtDefault.jsx
rtDefault.jsx = function (type, props, key) {
  const kids = props && props.children
  if (Array.isArray(kids)) {
    const unkeyed = kids.filter((kid) => kid && typeof kid === 'object' && kid.$$typeof && (kid.key === null || kid.key === undefined))
    if (unkeyed.length) {
      window.__arrayJsx = window.__arrayJsx || []
      if (window.__arrayJsx.length < 24) {
        window.__arrayJsx.push({
          type: typeof type === 'string' ? type : 'component',
          cls: String((props && props.className) || '').slice(0, 60),
          total: kids.length,
          unkeyed: unkeyed.length,
          sampleCls: String((unkeyed[0] && unkeyed[0].props && unkeyed[0].props.className) || '').slice(0, 60),
          stack: String(new Error('array-jsx').stack || '').split('\\n').slice(1, 5).join(' <- ')
        })
      }
    }
  }
  return origJsx(type, props, key)
}

const entryMod = await import('./entry.js')
const plugin = entryMod.default
const injectConsoleStyles = entryMod.injectConsoleStyles

const store = {}
const pages = []
const requests = []
// 诊断用：把 React 的「Static children should always be an array」警告连同调用栈留下来，
// 否则只知道「有 N 条警告」，不知道是哪一行。正常路径不影响任何行为。
const origError = console.error
console.error = function () {
  const first = arguments[0]
  if (typeof first === 'string' && first.indexOf('Static children') > -1) {
    window.__staticStacks = window.__staticStacks || []
    if (window.__staticStacks.length < 4) window.__staticStacks.push(String(new Error('probe').stack || ''))
  }
  if (typeof first === 'string' && first.indexOf('unique "key"') > -1) {
    window.__keyStacks = window.__keyStacks || []
    if (window.__keyStacks.length < 4) window.__keyStacks.push(String(new Error('probe').stack || ''))
  }
  return origError.apply(console, arguments)
}
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
    const res = await fetch('${API}' + path, init)
    const text = await res.text()
    let body = null
    try { body = init.body ? JSON.parse(init.body) : null } catch (e) { body = String(init.body) }
    // 响应体也留一份：验收要能证明「界面显示的是后端回读值」，而不是猜请求发对了。
    let response = null
    try { response = text ? JSON.parse(text) : null } catch (e) { response = null }
    requests.push({ method, path, status: res.status, ms: Math.round(performance.now() - started), body, response })
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
// 桌面桥：生产路径是 Electron preload 暴露的 window.hermesDesktop.api({path,method,body})
// （裸请求、不带 ?profile=，绕开宿主 profile 闸门）。探针必须把它按同样语义补上，
// 否则覆盖矩阵的写操作会全部落到「桌面桥不可用」分支——那不是被测代码的问题，是夹具缺件。
// 注意语义差异：桥拿的是**完整 URL 路径**（形如 /api/plugins/hermes-console/...），
// 不像 ctx.rest 那样自动加 API 前缀；这里必须原样 fetch，否则会拼成双前缀 → 404。
// 返回解析后的 JSON（非 2xx 由桥抛错，与 electron/main.ts::fetchJson 一致）。
window.hermesDesktop = {
  api: async (opts) => {
    const o = opts || {}
    const method = o.method || 'POST'
    const init = { method, headers: { 'content-type': 'application/json' } }
    if (o.body !== undefined) init.body = JSON.stringify(o.body)
    const started = performance.now()
    const res = await fetch(o.path, init)
    const text = await res.text()
    requests.push({ method, path: o.path, status: res.status, ms: Math.round(performance.now() - started), body: o.body === undefined ? null : o.body })
    if (!res.ok) throw new Error(res.status + ': ' + text)
    return text ? JSON.parse(text) : null
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

// ------------------------------------------------------------- 静态站 + 反代
function startSite() {
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hermes-console · Advisor（验收夹具）</title>
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
        /* 客户端已断开：忽略 */
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

// -------------------------------------------------------------- 后端夹具进程
function startFixture() {
  const child = spawn(PYTHON, [join(HERE, 'advisor_ui_fixture.py'), '--port', String(FIXTURE_PORT)], {
    cwd: PLUGIN,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' })
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

// ---------------------------------------------------------------------- 主流程
const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null
const pageFacts = {}
let lastPage = null

try {
  console.log('[env] bundle :', BUNDLE, '(' + basename(BUNDLE) + ')')
  console.log('[env] label  :', LABEL, EXPECT_OK ? '(断言模式)' : '(事实模式)')
  console.log('[env] shots  :', SHOTS)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  // 夹具必须是**本次新建**的：端口被上一次的僵尸夹具占着时，探针会连到旧沙盒
  // （读到的 enabled 就不是 false，后面所有设置断言都会跟着错；更糟的是可能对着
  //  旧代码的夹具跑出一片假绿）。这里不只信 enabled，还要把「本次子进程报的 sandbox
  //  路径」与「应答方返回的 settings.json 路径」对上，对不上直接停。
  const normPath = (value) => String(value || '').replace(/\\/g, '/').toLowerCase()
  const sandboxFromLog = ((/\[fixture\] sandbox=(.+)/.exec(fixture.log()) || [])[1] || '').trim()
  const initial = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/advisor/settings?agent=default')).json()
  const initialEnabled = initial && initial.data ? initial.data.enabled : null
  const answeringPath = String((initial && initial.data && initial.data.path) || '')
  const sameSandbox = Boolean(sandboxFromLog)
    && normPath(answeringPath).indexOf(normPath(sandboxFromLog)) === 0
  console.log('[env] fixture sandbox =', sandboxFromLog || '(本次夹具没报 sandbox)', '· 应答方设置文件 =', answeringPath)
  if (!sameSandbox) {
    throw new Error('端口 ' + FIXTURE_PORT + ' 上的夹具不是本次新建的（僵尸夹具）：本次 sandbox=' + (sandboxFromLog || '?')
      + '，应答方设置文件=' + (answeringPath || '?') + '。先清掉旧进程再跑。')
  }
  console.log('[env] fixture enabled =', initialEnabled)
  if (initialEnabled !== false) {
    throw new Error('夹具的 enabled 不是 false（' + initialEnabled + '）：端口上可能是上一次的僵尸夹具，先清掉再跑')
  }
  site = await startSite()
  browser = await chromium.launch({
    channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined
  })

  for (const width of WIDTHS) {
    console.log('\n===== 宽度 ' + width + ' =====')
    // 每个宽度都从**同一份基线**开始：上一个宽度会真的保存设置，不重置的话第二个宽度
    // 读到的是上一个宽度改完的状态（断言方向会反）。
    await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/advisor/settings?agent=default', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        enabled: false, concern_default: 'hold', min_interval_seconds: 0, model: '', provider: ''
      })
    })
    const page = await browser.newPage({ viewport: { width, height: 900 }, deviceScaleFactor: 1 })
    lastPage = page
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return
      const where = msg.location && msg.location() ? msg.location().url || '' : ''
      if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
      // 本地静态站偶发的传输层失败（net::ERR_NO_BUFFER_SPACE 之类）不是页面代码的问题：
      // 只过滤 net::ERR_ 传输错误，HTTP 状态错误（404 之类）照旧算红。
      if (/net::ERR_/.test(msg.text())) return
      consoleErrors.push('[' + width + '] ' + msg.text() + (where ? ' @ ' + where : ''))
    })
    page.on('pageerror', (err) => pageErrors.push('[' + width + '] ' + String(err)))

    await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
    await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
    await page.click('.hc-nav__item:has-text("Advisor")')
    await page.waitForSelector('.hc-adv', { timeout: 20000 })
    await page.waitForSelector('.hc-adv__row, .hc-empty', { timeout: 30000 })
    await sleep(700) // 等四个取数都落地

    // ---- 1. 区块齐全 ----
    const blocks = await page.evaluate(() => ({
      coverage: document.querySelectorAll('[data-coverage-bar]').length,
      stats: document.querySelectorAll('.hc-adv__stats').length,
      modes: document.querySelectorAll('[data-modes-switchable]').length,
      list: document.querySelectorAll('[data-block="list"]').length,
      trail: document.querySelectorAll('[data-trail-total]').length,
      settings: document.querySelectorAll('[data-settings-profile]').length,
      rows: document.querySelectorAll('.hc-adv__row').length,
      domNodes: document.querySelectorAll('.hc-adv, .hc-adv *').length
    }))
    pageFacts['blocks'] = blocks
    console.log('        blocks:', JSON.stringify(blocks))
    assertOrFact('★五个区块都在（覆盖 / 统计 / 模式 / 列表 / 流水 / 设置）',
      blocks.coverage === 1 && blocks.stats === 1 && blocks.list === 1 && blocks.settings === 1,
      JSON.stringify(blocks))
    assertOrFact('★模式块存在', blocks.modes === 1, 'modes=' + blocks.modes)
    assertOrFact('★评审流水块存在', blocks.trail === 1, 'trail=' + blocks.trail)

    // ---- 1b. 列表行几何：各列不重叠（objective，不靠看图） ----
    const rowLayout = await page.evaluate(() => {
      const row = document.querySelector('.hc-adv__row')
      if (!row) return { found: false }
      const cells = Array.from(row.querySelectorAll('.hc-adv__cell')).map((el) => {
        const rect = el.getBoundingClientRect()
        return {
          cls: String(el.className || '').replace('hc-adv__cell ', ''),
          left: Math.round(rect.left),
          right: Math.round(rect.right)
        }
      })
      let overlap = false
      for (let i = 1; i < cells.length; i += 1) {
        if (cells[i].left < cells[i - 1].right - 1) overlap = true
      }
      return { found: true, cells, overlap }
    })
    pageFacts['rowLayout'] = rowLayout
    assertOrFact('★列表行各列几何上不重叠', rowLayout.found && !rowLayout.overlap,
      JSON.stringify(rowLayout.cells || []))
    const badge = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.hc-adv__row'))
      const marks = rows.map((row) => {
        const el = row.querySelector('[data-no-target]')
        return {
          id: row.getAttribute('data-id'),
          value: el ? el.getAttribute('data-no-target') : '',
          text: el ? String(el.textContent || '') : ''
        }
      })
      return {
        rows: rows.length,
        noTarget: marks.filter((m) => m.value === 'yes').length,
        quoteOnly: marks.filter((m) => m.value === 'quote').length,
        namedRows: marks.filter((m) => !m.value).length,
        quoteTexts: Array.from(new Set(marks.filter((m) => m.value === 'quote').map((m) => m.text))),
        // 同一个徽标不能既是「仅引用」又被说成「未点名」（也不能被当成点名了）
        mixed: marks.filter((m) => m.value === 'quote' && m.text.indexOf('未点名') > -1).length,
        sampleQuoteIds: marks.filter((m) => m.value === 'quote').slice(0, 3).map((m) => m.id)
      }
    })
    // 同一批 20 行在后端眼里的三态：直接取**这次渲染用的那个响应**（window.__hcRequests
    // 里记的 /advisor/advice 列表请求），不重新猜参数、也不重新取一次数据。
    const listPage = await page.evaluate(() => {
      const reqs = window.__hcRequests.filter((r) =>
        r.path.indexOf('/advisor/advice') === 0 && r.path.indexOf('/advisor/advice/') !== 0)
      const last = reqs.slice(-1)[0]
      const items = (last && last.response && last.response.data && last.response.data.items) || []
      const counts = { rows: items.length, named: 0, quoted_only: 0, none: 0 }
      items.forEach((item) => {
        const state = (item.note_structure && item.note_structure.target_state) || 'none'
        counts[state] = (counts[state] || 0) + 1
      })
      return { path: last ? last.path : '', counts, ids: items.map((item) => item.id) }
    })
    pageFacts['noTargetBadges'] = badge
    pageFacts['advicePage'] = listPage
    console.log('        本批列表分布:', JSON.stringify({ rows: badge.rows, noTarget: badge.noTarget, quoteOnly: badge.quoteOnly }),
      '· 后端三态:', JSON.stringify(listPage.counts), '· 请求:', listPage.path)
    // 1) 「不是静默」的真正判据：界面上标出来的条数必须与后端三态**逐行对得上**。
    //    这条与数据怎么漂无关 —— 后端说 3 条没点名，界面就必须有 3 个「未点名」徽标。
    assertOrFact('★列表标记与后端三态逐行一致（rows / noTarget / quoteOnly）',
      badge.rows === listPage.counts.rows
        && badge.noTarget === (listPage.counts.none || 0)
        && badge.quoteOnly === (listPage.counts.quoted_only || 0),
      JSON.stringify({
        dom: { rows: badge.rows, noTarget: badge.noTarget, quoteOnly: badge.quoteOnly },
        backend: listPage.counts
      }))
    // 2) 首屏必须真的画出「未点名」徽标。这个前提由夹具保证（advisor_ui_fixture.py 追加一条
    //    target_state=none 的样本行，ts 取台账最大值 +2s），所以不再依赖真台账最新 20 行的运气；
    //    万一换了不带这条样本行的输入，记 SKIP 而不是 FAIL（渲染一致性仍由上面那条兜住）。
    assertOrSkip('★「没点名对象」的建议在列表里有标记（不是静默）',
      (listPage.counts.none || 0) > 0, badge.noTarget > 0,
      JSON.stringify({ rows: badge.rows, noTarget: badge.noTarget, quoteOnly: badge.quoteOnly }),
      '本批 ' + badge.rows + ' 行里没有 target_state=none 的建议（数据不支持）；'
        + '渲染一致性已由「列表标记与后端三态逐行一致」兜住')
    // 引号原文不算点名（插件闸门只认四种识别形态）：只引了原话的要单列，不能显示成点名了
    assertOrSkip('★「只引了原话」的建议单列（不是已点名，也不是静默）',
      (listPage.counts.quoted_only || 0) > 0,
      badge.quoteOnly > 0 && badge.quoteTexts.length === 1 && badge.quoteTexts[0].indexOf('仅引用') > -1,
      JSON.stringify(badge.quoteTexts),
      '本批 ' + badge.rows + ' 行里没有 target_state=quoted_only 的建议（数据不支持）')
    assertOrFact('★同一个徽标不会既算「仅引用」又算「未点名」', badge.mixed === 0, 'mixed=' + badge.mixed)

    // ---- 2. 模式块：三态设置 / 生效值 / 来源层 / agent 作用域 / 证据 ----
    // 版面收敛（t_22813006）：模式不再是独立大块，而是**设置表单里的一行**（唯一可编辑
    // 入口）+ 一段默认收起的「三种模式的区别」。所以先断言那一行的读数，再看折叠区。
    const modes = await page.evaluate(() => {
      const box = document.querySelector('[data-modes-switchable]')
      if (!box) return { exists: false }
      const factOf = (name) => {
        const el = box.querySelector('[data-mode-fact="' + name + '"]')
        return el ? el.textContent.trim() : ''
      }
      return {
        exists: true,
        switchable: box.getAttribute('data-modes-switchable'),
        current: box.getAttribute('data-modes-current'),
        setting: box.getAttribute('data-modes-setting'),
        resolved: box.getAttribute('data-modes-resolved'),
        source: box.getAttribute('data-modes-source'),
        currentLabel: (box.querySelector('[data-mode-current-label]') || {}).getAttribute
          ? box.querySelector('[data-mode-current-label]').getAttribute('data-mode-current-label')
          : '',
        // 「可切换（按 agent）」chip：收敛后挂在设置卡片标题行（同一个设置对象）
        fixedText: (document.querySelector('.hc-adv__settings [data-modes-switchable-chip]') || {}).textContent
          || '',
        blocked: (box.querySelector('[data-modes-blocked]') || {}).textContent || '',
        // 三态说明在默认收起的 <details> 里：still in DOM，计数照旧有效
        modeRows: box.querySelectorAll('[data-mode]').length,
        modeIds: Array.from(box.querySelectorAll('[data-mode]'))
          .map((el) => el.getAttribute('data-mode')),
        facts: { setting: factOf('setting'), resolved: factOf('resolved'), source: factOf('source') },
        agentPicker: (box.querySelector('[data-modes-agent-picker]') || {}).getAttribute
          ? box.querySelector('[data-modes-agent-picker]').getAttribute('data-modes-agent-picker')
          : '',
        // agent 作用域不再用「只读 chip」表达（那是第二个看得见的入口），
        // 收敛后由**唯一**的 Agent 选择器承担：这里断言分区里只有那一个。
        agentSelectors: document.querySelectorAll('.hc-adv [data-filter="agent"]').length,
        agentChip: (document.querySelector('.hc-adv__scope-pick .hc-select') || {}).value || '',
        guideOpen: (box.querySelector('[data-modes-guide]') || {}).hasAttribute
          ? box.querySelector('[data-modes-guide]').hasAttribute('open')
          : null,
        branches: box.querySelectorAll('[data-mode-branch]').length,
        branchMarkers: Array.from(box.querySelectorAll('[data-mode-branch]'))
          .map((el) => el.textContent),
        evidence: box.textContent.indexOf('sha256') > -1 && box.textContent.indexOf('CONSERVATIVE') > -1
      }
    })
    pageFacts['modes'] = modes
    console.log('        modes:', JSON.stringify(modes).slice(0, 320))
    assertOrFact('★当前模式可见（下拉自己的值 + 生效值读数）', Boolean(modes.exists && modes.setting && modes.facts.resolved),
      JSON.stringify({ setting: modes.setting, resolved: modes.facts.resolved }))
    assertOrFact('★界面写「可切换（按 agent）」而不是「不支持切换」',
      modes.exists && modes.fixedText.indexOf('可切换') > -1
        && modes.fixedText.indexOf('不支持切换') === -1,
      modes.fixedText)
    assertOrFact('★switchable 属性 = yes（后端 switchable === true）', modes.switchable === 'yes',
      'switchable=' + modes.switchable)
    assertOrFact('★三态模式都列出来（auto / target / conservative）',
      modes.exists && modes.modeRows === 3 && modes.modeIds.join(',') === 'auto,target,conservative',
      'rows=' + modes.modeRows + ' ids=' + modes.modeIds.join(','))
    assertOrFact('★「设置值 / 生效值 / 来源层」三个读数都在',
      modes.facts && modes.facts.setting && modes.facts.resolved && modes.facts.source,
      JSON.stringify(modes.facts))
    assertOrFact('★提示词证据（sha256 + CONSERVATIVE 分支）', modes.exists && modes.evidence)
    // 契约变更（t_22813006）：分区里**只能有一个** Agent 选择器（原来三个）。
    assertOrFact('★分区里只有一个 Agent 选择器（模式块不再自带第二个）',
      modes.exists && modes.agentPicker === 'no' && modes.agentSelectors === 1,
      JSON.stringify({ agentPicker: modes.agentPicker, agentSelectors: modes.agentSelectors }))
    assertOrFact('★「三种模式的区别」默认收起（有效信息保留、不占版面）',
      modes.exists && modes.guideOpen === false, 'guideOpen=' + modes.guideOpen)
    assertOrFact('★两个提示词分支的标记句都贴在界面上',
      modes.exists && modes.branches === 2
        && modes.branchMarkers.join(' ').indexOf('CONSERVATIVE') > -1
        && modes.branchMarkers.join(' ').indexOf('judged against') > -1,
      JSON.stringify(modes.branchMarkers))

    // ---- 3. 评审流水与 504 ----
    const trail = await page.evaluate(() => {
      const box = document.querySelector('[data-trail-total]')
      if (!box) return { exists: false }
      const err = box.querySelector('[data-error-kind="model_timeout"]')
      return {
        exists: true,
        total: box.getAttribute('data-trail-total'),
        errors: box.getAttribute('data-trail-errors'),
        inflight: box.getAttribute('data-trail-inflight'),
        logFailures: box.getAttribute('data-trail-log-failures'),
        timeoutItem: Boolean(err),
        readable: err ? (err.querySelector('.hc-adv__err-read') || {}).textContent || '' : '',
        hints: err ? err.querySelectorAll('.hc-adv__err-hints li').length : 0,
        text: box.textContent.slice(0, 400)
      }
    })
    pageFacts['trail'] = trail
    console.log('        trail:', JSON.stringify(trail).slice(0, 360))
    assertOrFact('★504 被归类成「模型超时」并在界面上有卡片',
      trail.exists && trail.timeoutItem, 'errors=' + trail.errors)
    assertOrFact('★504 有人话解释（不是只显示 Error code）',
      Boolean(trail.readable && trail.readable.indexOf('超时') > -1), String(trail.readable || '').slice(0, 60))
    assertOrFact('★504 给了三条建议', trail.hints >= 3, 'hints=' + trail.hints)
    assertOrFact('★日志旁证统计到 2 条（次数不是写死 1）', trail.logFailures === '2', 'log=' + trail.logFailures)
    assertOrFact('★飞行中评审单列', Number(trail.inflight) > 0, 'inflight=' + trail.inflight)
    if (!(Number(trail.inflight) > 0)) {
      // 诊断区默认收起后，流水块只在「有异常」时才渲染。夹具里本次没有只写了 started
      // 的记录时，in_flight 会是 0 —— 那是数据不支持这条断言，不该判红。
      skip('★飞行中评审单列（数据没有 in_flight 记录）', 'inflight=' + trail.inflight)
    }

    // ---- 4. 设置：逐项来源层 + 总闸 ----
    const settings = await page.evaluate(() => {
      const box = document.querySelector('[data-settings-profile]')
      if (!box) return { exists: false }
      const rows = Array.from(box.querySelectorAll('[data-settings-key]'))
      const applied = Array.from(box.querySelectorAll('[data-applied-field]'))
      const gateEl = box.querySelector('[data-gate-enabled]')
      return {
        exists: true,
        profile: box.getAttribute('data-settings-profile'),
        rows: rows.length,
        appliedRows: applied.length,
        sources: rows.map((r) => r.getAttribute('data-settings-source')),
        keys: rows.map((r) => r.getAttribute('data-settings-key')),
        gate: gateEl ? gateEl.getAttribute('data-gate-enabled') : '',
        gateText: (box.querySelector('.hc-adv__gate') || {}).textContent || '',
        host: box.querySelectorAll('[data-host-layers]').length,
        saveLabel: (box.querySelector('[data-settings-save]') || {}).textContent || '',
        dirty: (box.querySelector('[data-settings-dirty]') || {}).getAttribute
          ? box.querySelector('[data-settings-dirty]').getAttribute('data-settings-dirty')
          : ''
      }
    })
    pageFacts['settings'] = settings
    console.log('        settings:', JSON.stringify(settings).slice(0, 420))
    assertOrFact('★设置逐项列出（6 可写 + 1 只读 = 7 行）', settings.exists && settings.rows === 7,
      'rows=' + settings.rows)
    assertOrFact('★每行都标了来源层',
      settings.exists && settings.sources.every((s) => Boolean(s)),
      (settings.sources || []).join(','))
    assertOrFact('★enabled=false 在界面上看得见',
      settings.exists && settings.gate === 'false' && settings.gateText.indexOf('enabled=false') > -1,
      settings.gate + ' · ' + String(settings.gateText).slice(0, 40))
    assertOrFact('★宿主层（plugins.enabled / allow_gateway_injection）只读展示', settings.host === 1)

    // ---- 5. 列表选中 / 详情切换（展开 - 折叠）----
    const rowBtnInfo = await page.evaluate(() => {
      const btn = document.querySelector('.hc-adv__row .hc-adv__cell--ops button')
      if (!btn) return { found: false }
      // 先把按钮滚进视口：elementFromPoint 只对**视口内**的坐标有效，
      // 不滚动就取到的是视口外的空坐标（命中 null），那不是「按钮不可点」。
      btn.scrollIntoView({ block: 'center', inline: 'nearest' })
      const box = btn.getBoundingClientRect()
      const cx = box.x + box.width / 2
      const cy = box.y + box.height / 2
      const hit = document.elementFromPoint(cx, cy)
      const clickable = hit && hit.closest ? hit.closest('button') : null
      return {
        found: true,
        cx, cy,
        inViewport: box.top >= 0 && box.bottom <= window.innerHeight,
        hitTag: hit ? hit.tagName : '',
        hitClass: hit ? String(hit.className || '') : '',
        clickableText: clickable ? clickable.textContent : null
      }
    })
    pageFacts['rowButton'] = rowBtnInfo
    console.log('        elementFromPoint(详情按钮):', JSON.stringify(rowBtnInfo))
    assertOrFact('★详情按钮命中可点元素（elementFromPoint → button）',
      rowBtnInfo.found && rowBtnInfo.clickableText !== null, JSON.stringify(rowBtnInfo))

    let detailOpened = false
    if (rowBtnInfo.found) {
      // 真鼠标点击（Playwright 会做命中测试：被遮住/不可点会直接报错，不会假通过）
      try {
        await page.click('.hc-adv__row .hc-adv__cell--ops button', { timeout: 10000 })
        detailOpened = await page
          .waitForSelector('[data-detail-id]', { timeout: 15000 })
          .then(() => true)
          .catch(() => false)
      } catch (e) {
        detailOpened = false
        pageFacts['detailClickError'] = String(e).slice(0, 200)
      }
    }
    assertOrFact('★点「详情」真的展开（data-detail-id 出现）', detailOpened)
    const structInfo = detailOpened
      ? await page.evaluate(() => {
          const box = document.querySelector('[data-structure-complete]')
          return box
            ? {
                complete: box.getAttribute('data-structure-complete'),
                missing: box.getAttribute('data-structure-missing'),
                targetState: box.getAttribute('data-target-state'),
                cells: box.querySelectorAll('[data-struct-field]').length,
                hasTarget: box.querySelector('[data-struct-field="target"]').getAttribute('data-struct-has'),
                cellState: box.querySelector('[data-struct-field="target"]').getAttribute('data-struct-state'),
                targetText: String(box.querySelector('[data-struct-field="target"]').textContent || '')
              }
            : null
        })
      : null
    pageFacts['structure'] = structInfo
    console.log('        structure:', JSON.stringify(structInfo))
    assertOrFact('★详情里有结构自检（target + 四要素）',
      Boolean(structInfo && structInfo.cells === 5), JSON.stringify(structInfo))
    assertOrFact('★结构自检的 target 一列带三态（named / quoted_only / none）',
      Boolean(structInfo && ['named', 'quoted_only', 'none'].indexOf(structInfo.cellState) > -1
        && structInfo.targetState === structInfo.cellState
        && structInfo.hasTarget === (structInfo.cellState === 'named' ? 'yes' : 'no')),
      JSON.stringify(structInfo && { state: structInfo.targetState, has: structInfo.hasTarget, text: structInfo.targetText }))

    // 收起
    let detailClosed = false
    if (detailOpened) {
      try {
        await page.click('.hc-adv__row--active .hc-adv__cell--ops button', { timeout: 10000 })
        detailClosed = await page
          .waitForFunction('!document.querySelector("[data-detail-id]")', { timeout: 10000 })
          .then(() => true)
          .catch(() => false)
        pageFacts['detailCloseState'] = await page.evaluate(() => ({
          activeRows: document.querySelectorAll('.hc-adv__row--active').length,
          detail: document.querySelectorAll('[data-detail-id]').length,
          btnText: (document.querySelector('.hc-adv__row .hc-adv__cell--ops button') || {}).textContent || ''
        }))
      } catch (e) {
        pageFacts['detailCloseError'] = String(e).slice(0, 200)
      }
    }
    assertOrFact('★再点「收起」真的折叠', detailClosed)

    // ---- 5b. 「只引了原话」那条的详情：target 显示为「只引了原话」，不是「点名了」 ----
    // 这是本卡的核心验收：引号原文不算点名（插件闸门只认四种识别形态），
    // 面板不能在一条永远不会被投递的建议上显示「点了名」。
    const quoteId = (badge.sampleQuoteIds || [])[0] || ''
    let quoteDetail = null
    if (quoteId) {
      try {
        await page.click('[data-id="' + quoteId + '"] .hc-adv__cell--ops button', { timeout: 10000 })
        await page.waitForSelector('[data-detail-id="' + quoteId + '"] [data-target-state]', { timeout: 15000 })
        quoteDetail = await page.evaluate((id) => {
          const box = document.querySelector('[data-detail-id="' + id + '"] [data-target-state]')
          if (!box) return null
          const cell = box.querySelector('[data-struct-field="target"]')
          const text = cell ? String(cell.textContent || '') : ''
          return {
            state: box.getAttribute('data-target-state'),
            has: cell ? cell.getAttribute('data-struct-has') : '',
            cellState: cell ? cell.getAttribute('data-struct-state') : '',
            text,
            missing: box.getAttribute('data-structure-missing') || '',
            quoteHint: Boolean(box.querySelector('[data-quote-only="yes"]')),
            namedClaim: text.indexOf('点名了') > -1
          }
        }, quoteId)
      } catch (e) {
        pageFacts['quoteDetailError'] = String(e).slice(0, 200)
      }
      pageFacts['quoteDetail'] = quoteDetail
      console.log('        quote-only detail:', JSON.stringify(quoteDetail))
      assertOrFact('★「只引了原话」的详情 target 显示为「只引了原话」而不是「点名了」',
        Boolean(quoteDetail && quoteDetail.state === 'quoted_only' && quoteDetail.cellState === 'quoted_only'
          && quoteDetail.has === 'no' && quoteDetail.text.indexOf('只引了原话') > -1 && !quoteDetail.namedClaim),
        JSON.stringify(quoteDetail))
      assertOrFact('★「只引了原话」的详情 missing 含 target，并写明闸门会拒',
        Boolean(quoteDetail && quoteDetail.missing.split(',').indexOf('target') > -1 && quoteDetail.quoteHint),
        quoteDetail ? quoteDetail.missing : 'null')
      // 收起，别把详情留在展开态影响后面的分段
      await page.click('[data-id="' + quoteId + '"] .hc-adv__cell--ops button', { timeout: 10000 }).catch(() => {})
      await sleep(300)
    } else {
      // 本批列表里没有 quoted_only 行（数据不支持）：跳过详情验证，不判红。
      // 「界面必须把这类行标出来」已由「列表标记与后端三态逐行一致」断言兜住。
      if (EXPECT_OK) {
        skip('★「只引了原话」的详情 target 显示为「只引了原话」而不是「点名了」',
          '列表里没有 data-no-target="quote" 的行：' + JSON.stringify({ rows: badge.rows, quoteOnly: badge.quoteOnly }))
        skip('★「只引了原话」的详情 missing 含 target，并写明闸门会拒',
          '同上：本批没有 quoted_only 行')
      } else {
        fact('quoteDetail', false, JSON.stringify(badge))
      }
    }

    // ---- 6. 筛选切换（severity=concern；status 里没有 concern 这个值）----
    let filterOk = false
    try {
      await page.selectOption('[data-filter="severity"]', 'concern')
      await page.waitForFunction(
        'window.__hcRequests.some((r) => r.path.indexOf("/advisor/advice") === 0 && r.path.indexOf("severity=concern") > -1)',
        { timeout: 15000 }
      )
      filterOk = true
    } catch (e) {
      filterOk = false
    }
    assertOrFact('★筛选「严重度=CONCERN」真的带参重取', filterOk)
    await page.selectOption('[data-filter="severity"]', '')

    // ---- 7. 开关切换（enabled）----
    // before 组（旧产物）没有 data-settings-field，退回 input[type=checkbox]。
    const enabledSelector = (await page.$('[data-settings-field="enabled"]'))
      ? '[data-settings-field="enabled"]'
      : '.hc-adv__settings input[type="checkbox"]'
    let toggleOk = false
    let dirtyAfterToggle = ''
    try {
      await page.click(enabledSelector)
      await sleep(300)
      const state = await page.evaluate(() => ({
        dirty: (document.querySelector('[data-settings-dirty]') || {}).getAttribute
          ? document.querySelector('[data-settings-dirty]').getAttribute('data-settings-dirty')
          : '',
        save: (document.querySelector('[data-settings-save]') || {}).textContent || '',
        note: (document.querySelector('[data-gate-change]') || {}).textContent || ''
      }))
      dirtyAfterToggle = state.dirty
      toggleOk = state.dirty === 'enabled' && state.save.indexOf('1 项') > -1
      pageFacts['toggle'] = state
      console.log('        toggle:', JSON.stringify(state).slice(0, 300))
    } catch (e) {
      pageFacts['toggle'] = { error: String(e) }
    }
    assertOrFact('★开关切换后「待保存」只含 enabled，且保存按钮显示 1 项',
      toggleOk, dirtyAfterToggle)

    // ---- 8. 保存：请求体只有 enabled ----
    const saveSelector = (await page.$('[data-settings-save]'))
      ? '[data-settings-save]'
      : '.hc-adv__settings button:has-text("保存设置")'
    let putBodies = []
    let saveDone = false
    let gateAfter = ''
    try {
      const beforeCount = await page.evaluate(() => window.__hcRequests.length)
      await page.click(saveSelector)
      await page.waitForFunction(
        (n) => window.__hcRequests.slice(n).some((r) => r.method === 'PUT' && r.path.indexOf('/advisor/settings') > -1),
        beforeCount,
        { timeout: 20000 }
      )
      await page.waitForSelector('[data-save-done], .hc-adv__formsaved', { timeout: 20000 })
      // 「保存后回读」有两步：先出 data-save-done，再 query.reload() 把后端生效值刷回界面。
      // 两者不是同一次渲染，直接读 gate 会偶发读到刷新前的旧值（假红）。这里等它落定。
      await page.waitForFunction(
        () => {
          const el = document.querySelector('[data-gate-enabled]')
          return Boolean(el) && el.getAttribute('data-gate-enabled') === 'true'
        },
        undefined,
        { timeout: 10000 }
      ).catch(() => {})
      const state = await page.evaluate(() => ({
        puts: window.__hcRequests.filter((r) => r.method === 'PUT'),
        done: (document.querySelector('[data-save-done]') || document.querySelector('.hc-adv__formsaved') || {}).textContent || '',
        written: (document.querySelector('[data-save-written]') || {}).getAttribute
          ? document.querySelector('[data-save-written]').getAttribute('data-save-written')
          : '',
        gate: (document.querySelector('[data-gate-enabled]') || {}).getAttribute
          ? document.querySelector('[data-gate-enabled]').getAttribute('data-gate-enabled')
          : '',
        enabledRow: (document.querySelector('[data-settings-key="enabled"] .hc-adv__stable-value') || {}).textContent || ''
      }))
      putBodies = state.puts.map((r) => ({ path: r.path, keys: Object.keys(r.body || {}), body: r.body }))
      saveDone = Boolean(state.done)
      gateAfter = state.gate
      pageFacts['save'] = state
      console.log('        save:', JSON.stringify({ puts: putBodies, done: state.done.slice(0, 60), gate: state.gate }))
    } catch (e) {
      pageFacts['save'] = { error: String(e) }
    }
    assertOrFact('★保存后出现回读确认', saveDone)
    assertOrFact('★请求体只有改动过的键（enabled 一个）',
      putBodies.length === 1 && putBodies[0].keys.length === 1 && putBodies[0].keys[0] === 'enabled',
      JSON.stringify(putBodies.map((p) => p.keys)))
    assertOrFact('★保存后回读把总闸改成 true（后端生效值，不是本地值）', gateAfter === 'true', gateAfter)

    // ---- 9. 未改动再点保存：零写请求 ----
    let zeroWrite = false
    let zeroWriteNote = ''
    try {
      const beforeCount = await page.evaluate(() => window.__hcRequests.length)
      await page.click(saveSelector)
      await sleep(600)
      const after = await page.evaluate((n) => ({
        puts: window.__hcRequests.slice(n).filter((r) => r.method === 'PUT').length,
        done: (document.querySelector('[data-save-done]') || document.querySelector('.hc-adv__formsaved') || {}).textContent || ''
      }), beforeCount)
      zeroWrite = after.puts === 0
      zeroWriteNote = after.done
    } catch (e) {
      zeroWriteNote = String(e)
    }
    assertOrFact('★没有改动时点保存：零写请求', zeroWrite, zeroWriteNote.slice(0, 70))

    // ---- 10. 改数值 + 还原（不发请求）----
    let resetOk = false
    try {
      const intervalSelector = (await page.$('[data-settings-field="min_interval_seconds"]'))
        ? '[data-settings-field="min_interval_seconds"]'
        : '.hc-adv__settings input[type="number"]'
      await page.fill(intervalSelector, '30')
      await sleep(200)
      const dirtyBefore = await page.evaluate(() =>
        (document.querySelector('[data-settings-dirty]') || {}).getAttribute
          ? document.querySelector('[data-settings-dirty]').getAttribute('data-settings-dirty')
          : '')
      const beforeCount = await page.evaluate(() => window.__hcRequests.length)
      await page.click('.hc-adv__form-actions .hc-btn:not(.hc-btn--accent)')
      await sleep(400)
      const state = await page.evaluate((n) => ({
        dirty: (document.querySelector('[data-settings-dirty]') || {}).getAttribute
          ? document.querySelector('[data-settings-dirty]').getAttribute('data-settings-dirty')
          : '',
        puts: window.__hcRequests.slice(n).filter((r) => r.method === 'PUT').length,
        value: (document.querySelector('.hc-adv__settings input[type="number"]') || {}).value
      }), beforeCount)
      resetOk = dirtyBefore === 'min_interval_seconds' && !state.dirty && state.puts === 0 && state.value === '0'
      pageFacts['reset'] = { dirtyBefore, ...state }
    } catch (e) {
      pageFacts['reset'] = { error: String(e) }
    }
    assertOrFact('★改数值后点「还原」：回到生效值且零写请求', resetOk, JSON.stringify(pageFacts.reset))

    // ---- 10b. 评审模式：三态可选 + 保存后回读（设置值 → 生效值）----
    // 这条是「模式真的能选」的浏览器证据：选 conservative → 保存 → 模式块的生效值
    // （data-modes-resolved，来自后端 /advisor/modes 的推导）必须变成 conservative，
    // 再选回 auto → 保存 → 生效值必须回到目标对齐（该 agent 最近一次评审有目标）。
    const modeFacts = { steps: [] }
    let modeSelectOk = false
    let modeSaveOk = false
    let modeReadbackOk = false
    let modeRestoreOk = false
    const modeSel = '[data-settings-field="mode"]'
    const modesResolved = () => page.evaluate(() => {
      const box = document.querySelector('[data-modes-resolved]')
      const facts = {}
      if (box) {
        Array.from(box.querySelectorAll('[data-mode-fact]')).forEach((el) => {
          facts[el.getAttribute('data-mode-fact')] = el.textContent.trim()
        })
      }
      const select = document.querySelector('[data-settings-field="mode"]')
      const panelFacts = {}
      const panel = document.querySelector('[data-mode-field], .hc-adv__modefield')
      if (panel) {
        Array.from(panel.querySelectorAll('[data-mode-fact]')).forEach((el) => {
          panelFacts[el.getAttribute('data-mode-fact')] = el.textContent.trim()
        })
      }
      const dirtyEl = document.querySelector('[data-settings-dirty]')
      const tableRow = document.querySelector('[data-settings-key="mode"]')
      return {
        setting: box ? box.getAttribute('data-modes-setting') : '',
        resolved: box ? box.getAttribute('data-modes-resolved') : '',
        source: box ? box.getAttribute('data-modes-source') : '',
        facts,
        selectValue: select ? select.value : '',
        panelFacts,
        formDirty: dirtyEl ? dirtyEl.getAttribute('data-settings-dirty') : '',
        tableRow: tableRow ? tableRow.textContent.trim().slice(0, 120) : '',
        tableSource: tableRow ? tableRow.getAttribute('data-settings-source') : '',
        counts: {
          modeSelects: document.querySelectorAll('[data-settings-field="mode"]').length,
          modeFields: document.querySelectorAll('[data-mode-field]').length,
          panels: document.querySelectorAll('[data-settings-profile]').length,
          selectValues: Array.from(document.querySelectorAll('[data-settings-field="mode"]'))
            .map((el) => el.value),
          panelProfiles: Array.from(document.querySelectorAll('[data-settings-profile]'))
            .map((el) => el.getAttribute('data-settings-profile'))
        }
      }
    })
    async function saveMode(value, expectResolved) {
      const beforeCount = await page.evaluate(() => window.__hcRequests.length)
      await page.selectOption(modeSel, value)
      await sleep(200)
      const dirty = await page.evaluate(() => {
        const el = document.querySelector('[data-settings-dirty]')
        return {
          dirty: el ? el.getAttribute('data-settings-dirty') : '',
          save: (document.querySelector('[data-settings-save]') || {}).textContent || ''
        }
      })
      await page.click(saveSelector)
      await page.waitForFunction(
        (n) => window.__hcRequests.slice(n).some((r) => r.method === 'PUT'
          && r.path.indexOf('/advisor/settings') > -1),
        beforeCount,
        { timeout: 20000 }
      )
      await page.waitForSelector('[data-save-done], .hc-adv__formsaved', { timeout: 20000 })
      await page.waitForFunction(
        (want) => {
          const box = document.querySelector('[data-modes-resolved]')
          return Boolean(box) && box.getAttribute('data-modes-setting') === want
        },
        value,
        { timeout: 15000 }
      ).catch(() => {})
      const after = await modesResolved()
      const put = await page.evaluate((n) => window.__hcRequests.slice(n)
        .filter((r) => r.method === 'PUT'), beforeCount)
      const entry = {
        value, dirty, after,
        putKeys: put.map((r) => Object.keys(r.body || {})),
        putBody: put.map((r) => r.body),
        putValues: put.map((r) => ((r.response || {}).data || {}).values || null),
        putApplied: put.map((r) => ((r.response || {}).data || {}).applied || null)
      }
      modeFacts.steps.push(entry)
      console.log('        mode save ' + value + ':', JSON.stringify(entry).slice(0, 320))
      return entry
    }
    try {
      const initial = await modesResolved()
      modeFacts.initial = initial
      const options = await page.$$eval(modeSel + ' option', (els) => els.map((e) => e.value))
      modeFacts.options = options
      modeSelectOk = options.join(',') === 'auto,target,conservative'
      const step1 = await saveMode('conservative', 'conservative')
      modeSaveOk = step1.dirty.dirty === 'mode'
        && step1.putKeys.length === 1 && step1.putKeys[0].join(',') === 'mode'
        && step1.putBody[0].mode === 'conservative'
      modeReadbackOk = step1.after.setting === 'conservative'
        && step1.after.resolved === 'conservative'
        && step1.after.source === 'settings_file'
        && (step1.after.facts.resolved || '').indexOf('保守') > -1
      const step2 = await saveMode('auto', 'auto')
      modeRestoreOk = step2.after.setting === 'auto' && step2.after.resolved === 'goal_aligned'
    } catch (e) {
      modeFacts.error = String(e).slice(0, 300)
    }
    pageFacts['modeSetting'] = modeFacts
    assertOrFact('★模式选择器三态可选（auto / target / conservative）', modeSelectOk,
      JSON.stringify(modeFacts.options))
    assertOrFact('★选 conservative 保存：请求体只有 mode',
      modeSaveOk, JSON.stringify((modeFacts.steps[0] || {}).putBody))
    assertOrFact('★保存后回读：模式块的设置值与生效值都变成 conservative（后端推导，不是本地 state）',
      modeReadbackOk, JSON.stringify((modeFacts.steps[0] || {}).after))
    assertOrFact('★再选回 auto 保存：生效值回到目标对齐（auto 按目标推导）',
      modeRestoreOk, JSON.stringify((modeFacts.steps[1] || {}).after))

    // ---- 11. 切换 agent（唯一的 Agent 选择器：同时是设置作用域）----
    let profileOk = false
    try {
      const options = await page.$$eval('[data-filter="agent"] option', (els) => els.map((e) => e.value))
      if (options.length > 1) {
        const other = options.find((v) => v && v !== 'default')
        await page.selectOption('[data-filter="agent"]', other)
        await page.waitForFunction(
          (name) => {
            const box = document.querySelector('[data-settings-profile]')
            return box && box.getAttribute('data-settings-profile') === name
          },
          other,
          { timeout: 20000 }
        )
        profileOk = true
        pageFacts['profileSwitch'] = { options, picked: other }
      } else {
        pageFacts['profileSwitch'] = { options }
      }
    } catch (e) {
      pageFacts['profileSwitch'] = { error: String(e).slice(0, 200) }
    }
    assertOrFact('★切 agent 后设置真的重取那个 profile', profileOk, JSON.stringify(pageFacts.profileSwitch))

    // 宿主层回归（t_43f17bdb）：设置端点必须带 ?agent=。带 ?profile= 时宿主 gateway 会把整条
    // 请求重绑到那个 profile 的 config scope，本插件的 API 宿主在那里不存在 → 404 Plugin not found
    //（这条 404 是宿主拒绝，不是插件返回）。所以这里同时钉住「带 agent=」和「请求 200」。
    const scopeFacts = await page.evaluate(() => {
      const reqs = window.__hcRequests.filter((r) => r.path.indexOf('/advisor/settings') === 0)
      const box = document.querySelector('[data-settings-profile]')
      return {
        last: reqs.filter((r) => r.method === 'GET').slice(-1)[0] || null,
        legacy: reqs.filter((r) => /[?&]profile=/.test(r.path)).map((r) => r.path),
        box: box ? box.getAttribute('data-settings-profile') : '',
        rows: document.querySelectorAll('[data-settings-profile] [data-settings-key]').length
      }
    })
    pageFacts['settingsScope'] = scopeFacts
    assertOrFact('★切 agent 后设置请求带 ?agent=（一律不带 ?profile=）',
      Boolean(scopeFacts.last) && /[?&]agent=/.test(scopeFacts.last.path) && scopeFacts.legacy.length === 0,
      JSON.stringify(scopeFacts))
    assertOrFact('★切 agent 后设置请求 200（不再 404 Plugin not found）',
      Boolean(scopeFacts.last) && scopeFacts.last.status === 200,
      scopeFacts.last ? scopeFacts.last.status + ' ' + scopeFacts.last.path : '无设置请求')
    assertOrFact('★切 agent 后策略设置逐项仍渲染（技术详情 7 行，默认收起）',
      scopeFacts.rows === 7 && scopeFacts.box === (pageFacts.profileSwitch || {}).picked,
      'rows=' + scopeFacts.rows + ' box=' + scopeFacts.box)

    // ---- 12. 刷新数据 ----
    let refreshOk = false
    try {
      const beforeCount = await page.evaluate(() => window.__hcRequests.length)
      await page.click('button:has-text("刷新数据")')
      await sleep(900)
      const count = await page.evaluate((n) => window.__hcRequests.length - n, beforeCount)
      refreshOk = count >= 4
      pageFacts['refresh'] = { requests: count }
    } catch (e) {
      pageFacts['refresh'] = { error: String(e) }
    }
    assertOrFact('★「刷新数据」重取四个分区数据', refreshOk, JSON.stringify(pageFacts.refresh))

    // ---- 13. 溢出 ----
    // 判据分两种，别混：
    //   * 文档 / 内容区：scrollWidth 必须 == clientWidth（真的没有横向滚动条）；
    //   * 元素级：只抓「矩形右边超出容器右边」的真实溢出。省略号元素（overflow:hidden +
    //     text-overflow:ellipsis）的 scrollWidth 天生大于 clientWidth，那不是溢出。
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement
      const content = document.querySelector('.hc-content') || document.body
      const contentRect = content.getBoundingClientRect()
      const spill = []
      for (const el of document.querySelectorAll('.hc-adv, .hc-adv *')) {
        const style = getComputedStyle(el)
        if (style.position === 'fixed' || style.display === 'none') continue
        const rect = el.getBoundingClientRect()
        if (rect.width === 0) continue
        if (rect.right > contentRect.right + 1 || rect.left < contentRect.left - 1) {
          spill.push({
            cls: String(el.className || '').slice(0, 48),
            left: Math.round(rect.left),
            right: Math.round(rect.right)
          })
        }
        if (spill.length >= 6) break
      }
      return {
        docScroll: doc.scrollWidth, docClient: doc.clientWidth,
        contentScroll: content.scrollWidth, contentClient: content.clientWidth,
        contentLeft: Math.round(contentRect.left), contentRight: Math.round(contentRect.right),
        spill
      }
    })
    pageFacts['overflow' + width] = overflow
    console.log('        overflow@' + width + ':', JSON.stringify(overflow).slice(0, 300))
    assertOrFact('★' + width + 'px 无横向溢出（文档）', overflow.docScroll <= overflow.docClient + 1,
      overflow.docScroll + '/' + overflow.docClient)
    assertOrFact('★' + width + 'px 无横向溢出（内容区）', overflow.contentScroll <= overflow.contentClient + 1,
      overflow.contentScroll + '/' + overflow.contentClient)
    assertOrFact('★' + width + 'px 分区内没有元素超出容器边界', overflow.spill.length === 0,
      JSON.stringify(overflow.spill))

    pageFacts['stacks' + width] = await page.evaluate(() => ({
      static: window.__staticStacks || [],
      key: window.__keyStacks || [],
      bad: window.__badJsxs || [],
      arrayJsx: window.__arrayJsx || []
    }))

    // 版面取证：`.hc-adv` 的**直接子块**高度（判断「一屏能装下几块」用）。
    // 只记事实，不断言 —— 各块的绝对高度会随文案变化，写成断言就是变更检测器。
    pageFacts['layout' + width] = await page.evaluate(() => {
      const root = document.querySelector('.hc-adv')
      const kids = root ? Array.from(root.children) : []
      return {
        paneScroll: (() => {
          const p = document.querySelector('.hc-pane')
          return p ? { h: p.clientHeight, scroll: p.scrollHeight } : null
        })(),
        blocks: kids.map((el) => ({
          cls: String(el.className || el.tagName).slice(0, 46),
          h: Math.round(el.getBoundingClientRect().height)
        }))
      }
    })
    console.log('        layout@' + width + ':', JSON.stringify(pageFacts['layout' + width]))

    // 整页截图：面板是绝对定位的滚动容器，viewport 只有 900 高时 fullPage 也只能拍到
    // 一屏。临时把 viewport 拉高（容器查询按行内尺寸判定，改高不影响任何一档布局），
    // 让整块内容落在一张图里 —— 证据图必须能看完整个页面。
    await page.setViewportSize({ width, height: SHOT_HEIGHT })
    await sleep(400)
    pageFacts['layoutTall' + width] = await page.evaluate(() => {
      const root = document.querySelector('.hc-adv')
      return { h: root ? Math.round(root.getBoundingClientRect().height) : 0 }
    })
    await page.screenshot({ path: join(SHOTS, 'advisor-' + LABEL + '-' + width + '.png'), fullPage: true })
    await page.close()
  }

  // ---- 13b. 覆盖矩阵（卡片式）：摘要/卡片/展开区三段 + 动作按状态收敛 + 允许注入独立操作 + 就地面板 ----
  {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 })
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return
      const where = msg.location && msg.location() ? msg.location().url || '' : ''
      if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
      // 同 13a：只放过传输层 net::ERR_* 抖动，HTTP 状态错误仍然算红。
      if (/net::ERR_/.test(msg.text())) return
      consoleErrors.push('[act] ' + msg.text() + (where ? ' @ ' + where : ''))
    })
    page.on('pageerror', (err) => pageErrors.push('[act] ' + String(err)))
    await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
    await page.waitForFunction('window.__hcBooted === true', { timeout: 20000 })
    await page.click('.hc-nav__item:has-text("Advisor")')
    await page.waitForSelector('[data-coverage-rows]', { timeout: 20000 })
    await sleep(500)

    const readCov = async (profile) => {
      const j = await (await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + '/advisor/coverage')).json()
      return (j.data.profiles || []).find((r) => r.profile === profile) || null
    }
    async function closePanel() {
      await page.evaluate(() => {
        const b = Array.from(document.querySelectorAll('.hc-adv__cov-panel .hc-adv__modal-actions button'))
          .find((x) => x.textContent.indexOf('关闭') > -1)
        if (b) b.click()
      })
      await sleep(250)
    }
    let panelShot = 0
    // 展开一张卡片：**幂等**——动作确认面板是就地渲染在卡片里的，执行过一次动作后该卡片
    // 已经是展开态；再无脑点一次展开箭头会把它收起来（然后等 body 超时）。
    async function openCard(profile) {
      const bodySel = '[data-cov-body="' + profile + '"]'
      if (await page.$(bodySel)) return
      await page.click('[data-cov-expand="' + profile + '"]')
      await page.waitForSelector(bodySel, { timeout: 15000 })
    }

    // 点一个动作 → 等就地面板与变更清单 → 记录面板是否夹在卡片内/遮挡别的行 → 确认 → 等改前改后
    async function runAction(selector) {
      await page.click(selector)
      await page.waitForSelector('[data-cov-dialog]', { timeout: 15000 })
      try {
        await page.waitForSelector('[data-cov-changes]', { timeout: 15000 })
      } catch (e) {
        const dump = await page.evaluate(() => {
          const el = document.querySelector('[data-cov-dialog]')
          return {
            html: el ? el.outerHTML.slice(0, 1200) : '(no dialog)',
            reqs: (window.__hcRequests || []).slice(-6)
          }
        })
        console.log('        [diag runAction] ' + JSON.stringify(dump))
        throw e
      }
      // 证据截图：确认面板开着的时候，把**被操作的那张卡片**整张拍下来——
      // 「面板就地展开在卡片内、不覆盖被操作行、不遮挡别的 agent」是这张图的用途。
      try {
        panelShot += 1
        const cardEl = await page.evaluateHandle(() =>
          document.querySelector('[data-cov-dialog]').closest('[data-coverage-profile]'))
        const el = cardEl.asElement()
        if (el) await el.screenshot({ path: join(SHOTS, 'cov-panel-' + panelShot + '.png') })
      } catch (e) {
        console.log('        （面板截图失败：' + e.message + '）')
      }
      const panel = await page.evaluate(() => {
        const el = document.querySelector('[data-cov-dialog]')
        const card = el ? el.closest('[data-coverage-profile]') : null
        const covers = []
        if (el) {
          const pr = el.getBoundingClientRect()
          for (const other of document.querySelectorAll('[data-coverage-profile]')) {
            if (other === card) continue
            const r = other.getBoundingClientRect()
            if (r.bottom > pr.top + 1 && r.top < pr.bottom - 1) covers.push(other.getAttribute('data-coverage-profile'))
          }
        }
        return {
          insideCard: Boolean(el && card),
          fixed: el ? getComputedStyle(el).position : '',
          coversOther: covers,
          changes: (document.querySelector('[data-cov-changes]') || {}).textContent || ''
        }
      })
      await page.click('[data-cov-confirm]')
      await page.waitForSelector('[data-cov-transitions]', { timeout: 30000 })
      const trans = await page.evaluate(() => String(((document.querySelector('[data-cov-transitions]') || {}).textContent) || ''))
      await closePanel()
      return { panel, trans }
    }

    // ---- A. 静态结构：卡片取代表格、收起态高度、徽标无框、主操作按状态收敛 ----
    const layout = await page.evaluate(() => {
      const cards = Array.from(document.querySelectorAll('[data-coverage-profile]'))
      const badge = document.querySelector('[data-coverage-profile] .hc-adv__cov-badge')
      const bst = badge ? getComputedStyle(badge) : null
      return {
        cards: cards.length,
        checkboxes: document.querySelectorAll('[data-cov-checkbox]').length,
        batch: document.querySelectorAll('[data-cov-batch]').length,
        table: document.querySelectorAll('.hc-adv__cov-table').length,
        heights: cards.map((c) => Math.round(c.getBoundingClientRect().height)),
        rows: cards.map((c) => ({
          p: c.getAttribute('data-coverage-profile'),
          status: c.getAttribute('data-coverage-status'),
          primary: c.getAttribute('data-cov-primary'),
          install: Boolean(c.querySelector('[data-cov-action="install"]')),
          sideButtons: c.querySelectorAll('.hc-adv__cov-side button').length
        })),
        badge: bst ? { width: bst.borderTopWidth, style: bst.borderTopStyle, bg: bst.backgroundColor } : null
      }
    })
    pageFacts['covLayout'] = layout
    const rows = layout.rows || []
    const byProfile = {}
    rows.forEach((r) => { byProfile[r.p] = r })
    assertOrFact('★废弃 8 列表格：无表格/复选框/批量条，改为卡片', layout.table === 0 && layout.checkboxes === 0 && layout.batch === 0,
      JSON.stringify({ table: layout.table, checkboxes: layout.checkboxes, batch: layout.batch, cards: layout.cards }))
    assertOrFact('★每个 profile 一张卡片', layout.cards >= 5, 'cards=' + layout.cards)
    assertOrFact('★覆盖矩阵压成紧凑行（收起态单行 ≤ 56px，改造前 ~90px）',
      (layout.heights || []).every((h) => h > 0 && h <= 56),
      'heights=' + JSON.stringify(layout.heights))
    assertOrFact('★状态徽标不再带边框灰底（border 0 / 极淡底）',
      Boolean(layout.badge) && layout.badge.width === '0px' && layout.badge.style === 'none',
      JSON.stringify(layout.badge))
    // 已安装的行收起态绝不出现「安装」按钮
    const installedInstall = rows.filter((r) => r.status !== 'not_installed' && r.install)
    assertOrFact('★已安装的 agent 收起态不出现「安装」按钮', installedInstall.length === 0,
      'violations=' + JSON.stringify(installedInstall.map((r) => r.p)))
    assertOrFact('★未安装的 agent（missing）收起态主操作是「安装」',
      Boolean(byProfile.missing) && byProfile.missing.install === true && byProfile.missing.primary === 'install',
      JSON.stringify(byProfile.missing))
    // 六种状态的主操作 = 状态自洽（逐条对表：未安装→安装 / 装了未启用→启用插件 /
    // 启用但评审关→开启评审 / 已安装→同步副本 / 全开→无主操作 / 执行中→全部禁用）
    const expectPrimary = {
      missing: 'install', disabled: 'enable_plugin', reviewing: 'sync', full: 'none'
    }
    const primaryMismatch = []
    Object.keys(expectPrimary).forEach((p) => {
      if (!byProfile[p] || byProfile[p].primary !== expectPrimary[p]) {
        primaryMismatch.push({ p, want: expectPrimary[p], got: byProfile[p] && byProfile[p].primary })
      }
    })
    // 评审关闭的已装行（default / code-worker settings_enabled=false）→ 主操作开启评审
    const reviewOffRow = rows.find((r) => r.status !== 'not_installed' && r.primary === 'enable_review')
    assertOrFact('★主操作按状态收敛：未安装→安装 / 装了未启用→启用插件 / 评审关→开启评审 / 已安装→同步副本 / 全开→无主操作',
      primaryMismatch.length === 0, JSON.stringify(primaryMismatch))
    assertOrFact('★插件已启用、评审关闭的已装行主操作是「开启评审」', Boolean(reviewOffRow),
      JSON.stringify(rows.filter((r) => r.primary === 'enable_review').map((r) => r.p)))

    // ---- A2. 视觉规范：卡内元素间距 ≥ var(--s2)=8px；同屏强调色 ≤ 2 种 ----
    const spacing = await page.evaluate(() => {
      const gapOf = (el) => {
        if (!el) return null
        const st = getComputedStyle(el)
        const nums = [parseFloat(st.columnGap), parseFloat(st.rowGap)].filter((v) => Number.isFinite(v) && v > 0)
        return nums.length ? Math.min.apply(null, nums) : null
      }
      const card = document.querySelector('[data-coverage-profile]')
      const sels = ['.hc-adv__cov-card', '.hc-adv__cov-card-head', '.hc-adv__cov-facts',
        '.hc-adv__cov-side', '.hc-adv__cov-body', '.hc-adv__cov-section',
        '.hc-adv__cov-ops', '.hc-adv__cov-switch', '.hc-adv__cov-switch-text']
      const out = {}
      sels.forEach((sel) => {
        const el = sel === '.hc-adv__cov-card' ? card : (card ? card.querySelector(sel) : null)
        if (el) out[sel] = gapOf(el)
      })
      return out
    })
    pageFacts['covSpacing'] = spacing
    const tooTight = Object.keys(spacing).filter((k) => spacing[k] !== null && spacing[k] < 8)
    assertOrFact('★视觉规范：卡内各层元素间距都 ≥ var(--s2)=8px', tooTight.length === 0,
      JSON.stringify(spacing) + ' tooTight=' + JSON.stringify(tooTight))

    const accents = await page.evaluate(() => {
      // 强调色预算：只算覆盖矩阵里**实际画出来**的强调色（accent / warning / orange / red）。
      // 注意两个坑：① 未定义的主题变量用 style.color='var(--x)' 读不出来（非法值会保留旧值），
      // 必须用 getPropertyValue 判「有没有定义」；② border-color 默认是 currentColor，
      // 所以只有真有描边宽度时才把边框色算进预算。
      const rs = getComputedStyle(document.documentElement)
      const names = ['ui-accent', 'ui-warning', 'ui-orange', 'ui-red', 'ui-danger']
      const raw = {}
      names.forEach((n) => { raw[n] = String(rs.getPropertyValue('--' + n) || '').trim() })
      const probeEl = document.createElement('div')
      document.body.appendChild(probeEl)
      const defs = {}
      names.forEach((n) => {
        if (!raw[n]) return
        probeEl.style.color = 'var(--' + n + ')'
        defs[n] = getComputedStyle(probeEl).color
      })
      probeEl.remove()
      const used = new Set()
      const root = document.querySelector('[data-coverage-rows]') || document.body
      for (const el of [root].concat(Array.from(root.querySelectorAll('*')))) {
        const st = getComputedStyle(el)
        const vals = [st.color, st.backgroundColor]
        if (parseFloat(st.borderTopWidth) > 0 && st.borderTopStyle !== 'none') vals.push(st.borderTopColor)
        vals.forEach((v) => {
          if (!v || v === 'rgba(0, 0, 0, 0)') return
          Object.keys(defs).forEach((n) => { if (defs[n] === v) used.add(n) })
        })
      }
      return { defined: Object.keys(defs), defs, used: Array.from(used) }
    })
    pageFacts['covAccents'] = accents
    // 强调色 = accent（主操作/开着的开关）+ warning（部分可用/风险）；red 是「危险动作」的语义色，
    // 任务书第 6 条本身就要求危险项用红字，所以不算进「强调色」预算。
    const accentOnly = (accents.used || []).filter((n) => n === 'ui-accent' || n === 'ui-warning' || n === 'ui-orange')
    assertOrFact('★视觉规范：同屏强调色不超过 2 种（accent + warning）', accentOnly.length <= 2,
      JSON.stringify(accents))

    // ---- B. 1280px 无横向滚动 ----
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement
      const content = document.querySelector('.hc-content') || document.body
      const cr = content.getBoundingClientRect()
      let spill = 0
      const spillWho = []
      for (const el of document.querySelectorAll('.hc-adv, .hc-adv *')) {
        const st = getComputedStyle(el)
        if (st.position === 'fixed' || st.display === 'none') continue
        const r = el.getBoundingClientRect()
        if (r.width === 0) continue
        if (r.right > cr.right + 1 || r.left < cr.left - 1) {
          spill += 1
          if (spillWho.length < 8) {
            spillWho.push({
              cls: String(el.className || el.tagName).slice(0, 90),
              right: Math.round(r.right), left: Math.round(r.left), width: Math.round(r.width)
            })
          }
        }
      }
      return {
        docScroll: doc.scrollWidth, docClient: doc.clientWidth,
        contentScroll: content.scrollWidth, contentClient: content.clientWidth, spill, spillWho
      }
    })
    pageFacts['covOverflow1280'] = overflow
    assertOrFact('★1280px 无横向滚动条（文档）', overflow.docScroll <= overflow.docClient + 1,
      overflow.docScroll + '/' + overflow.docClient)
    assertOrFact('★1280px 无横向滚动条（内容区）', overflow.contentScroll <= overflow.contentClient + 1,
      overflow.contentScroll + '/' + overflow.contentClient)
    assertOrFact('★覆盖矩阵无元素横向溢出', overflow.spill === 0,
      'spill=' + overflow.spill + ' ' + JSON.stringify(overflow.spillWho))

    // ---- C. 展开区：三个权限开关都在 ----
    await openCard('reviewing')
    const switches = await page.evaluate(() => ({
      ids: Array.from(document.querySelectorAll('[data-cov-body="reviewing"] [data-cov-switch]'))
        .map((el) => el.getAttribute('data-cov-switch')),
      ops: Array.from(document.querySelectorAll('[data-cov-body="reviewing"] [data-cov-action]'))
        .map((el) => el.getAttribute('data-cov-action'))
    }))
    pageFacts['covSwitches'] = switches
    assertOrFact('★展开区有「插件运行 / 参与评审 / 允许注入」三个权限开关',
      ['plugin', 'review', 'injection'].every((k) => switches.ids.indexOf(k) > -1), JSON.stringify(switches.ids))
    assertOrFact('★其它操作含同步副本 / 停用插件 / 关闭评审 / 关闭注入 / 卸载',
      ['sync', 'disable_plugin', 'disable_review', 'disable_injection', 'uninstall'].every((k) => switches.ops.indexOf(k) > -1),
      JSON.stringify(switches.ops))

    // ---- D. 允许注入独立操作 + 就地面板不覆盖被操作行 + preview→确认→apply→回读 ----
    // reviewing：已装 + 启用 + 评审开 + 注入关 → 注入开关显示「关」，可独立打开
    const injBefore = await readCov('reviewing')
    assertOrFact('前置：reviewing.injection_allowed=false', Boolean(injBefore) && injBefore.injection_allowed === false,
      JSON.stringify(injBefore && { inj: injBefore.injection_allowed }))
    const enableInj = await runAction('[data-coverage-profile="reviewing"] [data-cov-switch="injection"] [data-cov-action="enable_injection"]')
    pageFacts['covPanel'] = enableInj.panel
    assertOrFact('★允许注入可独立操作（开关按钮带 data-cov-action=enable_injection）',
      /allow_gateway_injection/.test(enableInj.panel.changes || ''), String(enableInj.panel.changes).slice(0, 120))
    assertOrFact('★预览/确认就地在卡片内展开，不覆盖被操作行、不遮挡别的 agent',
      enableInj.panel.insideCard === true && enableInj.panel.fixed === 'static'
        && (enableInj.panel.coversOther || []).length === 0, JSON.stringify(enableInj.panel))
    const injOn = await readCov('reviewing')
    assertOrFact('★允许注入 apply 后 GET coverage：reviewing.injection_allowed=true（回读真值）',
      Boolean(injOn) && injOn.injection_allowed === true, JSON.stringify(injOn && { inj: injOn.injection_allowed }))
    // 开关自己刷新成「关」→ 再点一次关掉
    await page.waitForSelector('[data-coverage-profile="reviewing"] [data-cov-switch="injection"] [data-cov-action="disable_injection"]', { timeout: 20000 })
    await runAction('[data-coverage-profile="reviewing"] [data-cov-switch="injection"] [data-cov-action="disable_injection"]')
    const injOff = await readCov('reviewing')
    assertOrFact('★关闭注入 apply 后 GET coverage：reviewing.injection_allowed=false（回读真值）',
      Boolean(injOff) && injOff.injection_allowed === false, JSON.stringify(injOff && { inj: injOff.injection_allowed }))
    // 注入动作不改「插件启用 / 评审启用」
    assertOrFact('★注入动作不改插件启用/评审启用（四件事独立）',
      Boolean(injOff) && injOff.enabled_in_config === true && injOff.settings_enabled === true,
      JSON.stringify(injOff && { e: injOff.enabled_in_config, s: injOff.settings_enabled }))

    // ---- D2. 第六种状态「动作执行中」：显示进行中 + 该卡所有动作按钮禁用 ----
    // 用 sync 做（幂等、不改状态）；把 apply 请求人为拖慢 1.2s 才抓得住这个中间态。
    await page.route('**/advisor/coverage/action', async (route) => {
      if (route.request().method() === 'POST') await sleep(1200)
      await route.continue()
    })
    await page.click('[data-coverage-profile="reviewing"] [data-cov-action="sync"]')
    await page.waitForSelector('[data-cov-changes]', { timeout: 15000 })
    await page.click('[data-cov-confirm]')
    await page.waitForSelector('[data-cov-running="reviewing"]', { timeout: 8000 })
    const busyFacts = await page.evaluate(() => {
      const card = document.querySelector('[data-coverage-profile="reviewing"]')
      const acts = Array.from(card.querySelectorAll('[data-cov-action]'))
      return {
        running: Boolean(document.querySelector('[data-cov-running="reviewing"]')),
        runningText: (document.querySelector('[data-cov-running="reviewing"]') || {}).textContent || '',
        actions: acts.length,
        enabled: acts.filter((b) => !b.disabled).length
      }
    })
    pageFacts['covBusy'] = busyFacts
    assertOrFact('★动作执行中：卡片显示「执行中…」且该卡全部动作按钮禁用',
      busyFacts.running && /执行中/.test(busyFacts.runningText) && busyFacts.actions > 0 && busyFacts.enabled === 0,
      JSON.stringify(busyFacts))
    await page.waitForSelector('[data-cov-transitions]', { timeout: 30000 })
    await closePanel()
    await page.unroute('**/advisor/coverage/action')

    // ---- E. 六类动作零回归：安装 / 同步 / 启停插件 / 启停评审 / 开关注入 / 卸载 ----
    // missing：未安装 → 安装 → 启用插件 → 同步 → 停用插件 → 卸载
    await page.click('[data-coverage-profile="missing"] [data-cov-action="install"]')
    await page.waitForSelector('[data-cov-dialog]', { timeout: 15000 })
    await page.click('[data-cov-confirm]')
    await page.waitForSelector('[data-cov-transitions]', { timeout: 30000 })
    await closePanel()
    const afterInstall = await readCov('missing')
    assertOrFact('★安装：missing 由 not_installed → 已安装（回读）',
      Boolean(afterInstall) && afterInstall.plugin_installed === true, JSON.stringify(afterInstall && { inst: afterInstall.plugin_installed, st: afterInstall.status }))

    await page.waitForSelector('[data-coverage-profile="missing"] [data-cov-action="enable_plugin"]', { timeout: 20000 })
    await page.click('[data-coverage-profile="missing"] [data-cov-action="enable_plugin"]')
    await page.waitForSelector('[data-cov-changes]', { timeout: 15000 })
    await page.click('[data-cov-confirm]')
    await page.waitForSelector('[data-cov-transitions]', { timeout: 30000 })
    await closePanel()
    const afterEnable = await readCov('missing')
    assertOrFact('★启用插件：missing.enabled_in_config=true（回读）',
      Boolean(afterEnable) && afterEnable.enabled_in_config === true, JSON.stringify(afterEnable && { e: afterEnable.enabled_in_config }))

    await openCard('missing')
    await page.waitForSelector('[data-cov-body="missing"] .hc-adv__cov-ops [data-cov-action="sync"]', { timeout: 20000 })
    const syncRun = await runAction('[data-cov-body="missing"] .hc-adv__cov-ops [data-cov-action="sync"]')
    assertOrFact('★同步副本：执行返回改前→改后', /missing/.test(syncRun.trans || ''), String(syncRun.trans).slice(0, 120))

    // 展开 missing，停用插件（其它操作）
    await openCard('missing')
    await runAction('[data-cov-body="missing"] .hc-adv__cov-ops [data-cov-action="disable_plugin"]')
    const afterDisable = await readCov('missing')
    assertOrFact('★停用插件：missing.enabled_in_config=false（回读）',
      Boolean(afterDisable) && afterDisable.enabled_in_config === false, JSON.stringify(afterDisable && { e: afterDisable.enabled_in_config }))

    // code-worker：settings_enabled=false → 开启评审（主操作）→ 关闭评审（其它操作）
    await runAction('[data-coverage-profile="code-worker"] [data-cov-action="enable_review"]')
    const afterReviewOn = await readCov('code-worker')
    assertOrFact('★开启评审：code-worker.settings_enabled=true（回读）',
      Boolean(afterReviewOn) && afterReviewOn.settings_enabled === true, JSON.stringify(afterReviewOn && { s: afterReviewOn.settings_enabled }))

    await openCard('code-worker')
    await runAction('[data-cov-body="code-worker"] .hc-adv__cov-ops [data-cov-action="disable_review"]')
    const afterReviewOff = await readCov('code-worker')
    assertOrFact('★关闭评审：code-worker.settings_enabled=false（回读）',
      Boolean(afterReviewOff) && afterReviewOff.settings_enabled === false, JSON.stringify(afterReviewOff && { s: afterReviewOff.settings_enabled }))

    // missing：卸载（危险项）→ 回到 not_installed
    await openCard('missing')
    await runAction('[data-cov-body="missing"] .hc-adv__cov-ops [data-cov-action="uninstall"]')
    const afterUninstall = await readCov('missing')
    assertOrFact('★卸载：missing.plugin_installed=false（回读）',
      Boolean(afterUninstall) && afterUninstall.plugin_installed === false, JSON.stringify(afterUninstall && { inst: afterUninstall.plugin_installed }))

    // 布局几何：相邻顶层块垂直间隙（无大片留白）+ 无横向溢出
    const geo = await page.evaluate(() => {
      const root = document.querySelector('.hc-adv')
      const kids = root ? Array.from(root.children).filter((el) => el.getBoundingClientRect().height > 0) : []
      let maxGap = 0
      for (let i = 1; i < kids.length; i += 1) {
        const prev = kids[i - 1].getBoundingClientRect()
        const cur = kids[i].getBoundingClientRect()
        const gap = Math.round(cur.top - prev.bottom)
        if (gap > maxGap) maxGap = gap
      }
      return { maxGap }
    })
    pageFacts['covGeo'] = geo
    assertOrFact('★覆盖矩阵无大片留白（相邻顶层块垂直间隙 ≤ 80px）', geo.maxGap <= 80, JSON.stringify(geo))

    await page.close()
  }

  // ---- 14. 零 console error / 零 pageerror ----
  assertOrFact('★零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  assertOrFact('★零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  if (consoleErrors.length) {
    // 诊断：把警告的调用栈打出来（指向 dist 的行号，再对回 src）
    for (const width of WIDTHS) {
      const stacks = (pageFacts['stacks' + width] || { static: [], key: [] })
      console.log('        [diag ' + width + '] static-children 栈:')
      stacks.static.forEach((s, i) => console.log('        #' + i + ' ' + s.split('\n').slice(0, 4).join(' <- ')))
      console.log('        [diag ' + width + '] key 栈:')
      stacks.key.forEach((s, i) => console.log('        #' + i + ' ' + s.split('\n').slice(0, 9).join(' <- ')))
      console.log('        [diag ' + width + '] bad jsxs:')
      ;(stacks.bad || []).forEach((b, i) => console.log('        #' + i + ' ' + JSON.stringify(b)))
      console.log('        [diag ' + width + '] array-children via jsx:')
      ;(stacks.arrayJsx || []).forEach((b, i) => console.log('        #' + i + ' ' + JSON.stringify(b)))
    }
  }
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) site.close()
  if (fixture) fixture.child.kill()
}

writeFileSync(join(SHOTS, 'facts-' + LABEL + '.json'), JSON.stringify({ label: LABEL, bundle: BUNDLE, facts, pageFacts, consoleErrors, pageErrors, skips }, null, 2), 'utf8')

console.log('\n' + '='.repeat(72))
if (EXPECT_OK) {
  if (failures.length) {
    console.log('ADVISOR-UI-PROBE-FAILED  (%d passed, %d failed, %d skipped)  bundle=%s', pass, failures.length, skipped, basename(BUNDLE))
    failures.forEach((f) => console.log('  - ' + f))
    skips.forEach((s) => console.log('  ~ SKIP ' + s))
    process.exit(1)
  }
  console.log('ADVISOR-UI-PROBE-OK  (%d checks, %d skipped)  bundle=%s', pass, skipped, basename(BUNDLE))
  skips.forEach((s) => console.log('  ~ SKIP ' + s))
} else {
  console.log('ADVISOR-UI-PROBE-FACTS  bundle=%s  shots=%s', basename(BUNDLE), SHOTS)
  console.log('  （事实模式：不断言；--expect-ok 才断言）')
}
