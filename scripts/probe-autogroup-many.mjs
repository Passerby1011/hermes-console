/**
 * hermes-console · 项目分组「建议数 > 200」真浏览器验收（t_4d192e1b）
 *
 * 跑法（任意目录）::
 *
 *     node D:/hermes-data/hermes/plugins/hermes-console/scripts/probe-autogroup-many.mjs
 *     node .../probe-autogroup-many.mjs --shots D:/tmp/shots
 *
 * 验的是本卡要消灭的那个矛盾：**建议总数 500 时，状态条 / 抽屉徽标 / 明细行数三个数字**。
 *
 *   1. **真产物**：`dashboard/dist/index.js` 原样加载（只套一层 harness 入口）；
 *   2. **真后端**：`scripts/autogroup_ui_fixture.py --suggested-extra 497` —— 沙盒里
 *      3 条常规 suggested + 497 条批量 suggested = **500 条**，后端单次上限仍是 200；
 *   3. **真浏览器**：Playwright + Chromium(msedge)，1350×900。
 *
 * 断言（对应卡面验收 1、2）：
 *   M1. 后端真相：`/autogroup/status` 的 suggested = 500，`/autogroup/suggestions` 的
 *       `data.total` = 500 且 `limit=1000` 被钳到 200（证明「一次取不全」是硬上限，不是前端偷懒）；
 *   M2. 打开抽屉（点状态条「待确认」）后：状态条 = 徽标 = 后端 total = 500，三处同值；
 *   M3. 抽屉明写「显示 200 / 共 500 条」，明细 200 行，剩余 300 条写在「加载更多」上；
 *   M4. 点两次「加载更多」取满 500 行（session_id 去重后正好 500 个），按钮消失；
 *   M5. 页面上不存在互相矛盾的数字：loaded + remaining = total，loaded 不超过 total；
 *   M6. 全程零 console error / 零 pageerror，真 home 的 assignments.db 一个字节没动。
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, rmdirSync, symlinkSync, unlinkSync, writeFileSync } from 'node:fs'
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
const FIXTURE_PORT = Number(process.env.HC_AGG_MANY_FIXTURE_PORT || 9199)
const SITE_PORT = Number(process.env.HC_AGG_MANY_SITE_PORT || 9200)
const API = '/api/plugins/hermes-console'
/** 夹具里 3 条常规 suggested + 497 条批量 = 500。 */
const EXPECT_TOTAL = 500
const EXPECT_EXTRA = EXPECT_TOTAL - 3
/** 后端 SUGGESTIONS_MAX_LIMIT = 200：一页最多 200 条。 */
const PAGE = 200

const shotsArg = process.argv.indexOf('--shots')
const SHOTS = shotsArg > -1 ? resolve(process.argv[shotsArg + 1]) : resolve(PLUGIN, 'scripts', '_probe_out', 'autogroup-many')

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

const WORK = mkdtempSync(join(tmpdir(), 'hc-agg-many-'))
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
    requests.push({ method, path, status: res.status, at: Date.now() })
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
<title>hermes-console · 项目分组（建议 >200 条验收）</title>
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
  const child = spawn(
    PYTHON,
    [
      join(HERE, 'autogroup_ui_fixture.py'),
      '--port', String(FIXTURE_PORT),
      '--sandbox', SANDBOX,
      '--suggested-extra', String(EXPECT_EXTRA)
    ],
    {
      cwd: PLUGIN,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', HC_AI_STUB: '1' })
    }
  )
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

const apiJson = async (path) => {
  const res = await fetch('http://127.0.0.1:' + FIXTURE_PORT + API + path)
  const text = await res.text()
  try {
    return JSON.parse(text)
  } catch (e) {
    return { ok: false, raw: text }
  }
}

const consoleErrors = []
const pageErrors = []
let fixture = null
let site = null
let browser = null

try {
  console.log('[env] dist    :', DIST)
  console.log('[env] shots   :', SHOTS)
  console.log('[env] sandbox :', SANDBOX)
  await buildHarness()
  fixture = startFixture()
  await waitForFixture(fixture)
  console.log('[env] fixture :', '127.0.0.1:' + FIXTURE_PORT + API)
  // 真 home 指纹：夹具只写沙盒，结尾逐字节比对
  const REAL_ASSIGN = 'D:/hermes-data/hermes/auto-group/assignments.db'
  const realBefore = sha256(REAL_ASSIGN)

  // ------------------------------------------------------- M1 后端真相（先钉死口径）
  const status = await apiJson('/autogroup/status')
  const statusSuggested = Number(status.data && status.data.suggested)
  const page1 = await apiJson('/autogroup/suggestions?status=suggested&limit=' + PAGE + '&offset=0')
  const detailTotal = Number(page1.data && page1.data.total)
  const page1Items = ((page1.data && page1.data.items) || []).length
  const oversized = await apiJson('/autogroup/suggestions?status=suggested&limit=1000&offset=0')
  const oversizedItems = ((oversized.data && oversized.data.items) || []).length
  const oversizedLimit = Number(oversized.data && oversized.data.limit)
  console.log('[M1] status.suggested=' + statusSuggested + ' detail.total=' + detailTotal +
    ' page1.items=' + page1Items + ' limit=1000→' + oversizedLimit + '/' + oversizedItems)
  check('M1a 夹具真的造出 500 条 status=suggested', statusSuggested === EXPECT_TOTAL && detailTotal === EXPECT_TOTAL,
    'status=' + statusSuggested + ' total=' + detailTotal)
  check('M1b 后端一次只给 200 条（limit=1000 被钳到 200）——「一次取不全」是硬上限',
    oversizedLimit === PAGE && oversizedItems === PAGE, 'limit=' + oversizedLimit + ' items=' + oversizedItems)
  check('M1c 第一页 200 条，total 仍是 500（分页信息由后端给，前端不用猜）',
    page1Items === PAGE && detailTotal === EXPECT_TOTAL, 'items=' + page1Items + ' total=' + detailTotal)

  site = await startSite()
  browser = await chromium.launch({ channel: process.env.HC_PW_CHANNEL === undefined ? 'msedge' : process.env.HC_PW_CHANNEL || undefined })
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
  await page.click('.hc-nav__item:has-text("项目分组")')
  await page.waitForSelector('[data-agg2-root]', { timeout: 30000 })
  // 计数行在默认收起的抽屉里 → 只等 attached（DOM 到位），可见性等下面打开抽屉后再验
  try {
    await page.waitForFunction(
      (want) => {
        const el = document.querySelector('[data-agg2-suggested-counts]')
        return !!el && el.getAttribute('data-agg2-suggested-counts') === want
      },
      PAGE + '/' + EXPECT_TOTAL,
      { timeout: 30000 }
    )
  } catch (e) {
    const dbg = await page.evaluate(() => {
      const el = document.querySelector('[data-agg2-suggested-counts]')
      return {
        exists: !!el,
        attr: el ? el.getAttribute('data-agg2-suggested-counts') : null,
        drawers: document.querySelectorAll('[data-agg2-drawer]').length,
        text: document.body.textContent.slice(0, 300)
      }
    })
    throw new Error('计数行没到位：' + JSON.stringify(dbg))
  }

  // 读一次「界面上现在写着的数字」（抽屉未打开时也能读到：details 里的内容仍在 DOM 里）
  const readNumbers = () =>
    page.evaluate(() => {
      const q = (sel) => document.querySelector(sel)
      const text = (el) => (el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : '')
      const attr = (sel, name) => {
        const el = q(sel)
        return el ? el.getAttribute(name) : null
      }
      const metric = q('[data-agg2-metric="suggested"]')
      const head = q('[data-agg2-drawer-head="suggested"]')
      const chip = head ? head.querySelector('.hc-chip') : null
      const more = q('[data-agg2-suggested-more]')
      const rows = Array.from(document.querySelectorAll('[data-agg2-drawer="suggested"] .hc-agg__row'))
      const ids = rows.map((r) => r.getAttribute('data-session-id')).filter(Boolean)
      return {
        statusValue: metric ? metric.getAttribute('data-agg2-metric-value') : null,
        badgeText: text(chip),
        countsText: text(q('[data-agg2-suggested-counts]')),
        loaded: attr('[data-agg2-suggested-loaded]', 'data-agg2-suggested-loaded'),
        total: attr('[data-agg2-suggested-total]', 'data-agg2-suggested-total'),
        moreText: text(more),
        moreRemaining: more ? more.getAttribute('data-agg2-suggested-remaining') : null,
        moreExists: Boolean(more),
        mismatch: Boolean(q('[data-agg2-count-mismatch]')),
        rows: rows.length,
        uniqueIds: new Set(ids).size,
        tableAttr: attr('[data-agg2-suggested-table]', 'data-agg2-suggested-table')
      }
    })

  // ------------------------------------------------------- M2/M3 抽屉：三个数字口径一致
  // 走真实入口：点状态条「待确认」应把抽屉打开（不是靠 evaluate 改 DOM）
  await page.click('[data-agg2-metric="suggested"]')
  await page.waitForSelector('[data-agg2-drawer="suggested"][open]', { timeout: 10000 })
  const drawerOpen = await page.evaluate(() => {
    const d = document.querySelector('[data-agg2-drawer="suggested"]')
    return Boolean(d && d.open)
  })
  check('M2a 点状态条「待确认」真的把抽屉打开了（入口可用）', drawerOpen)
  check('M2a2 打开后「显示 X / 共 N 条」这一行是真的可见（不是藏在收起态里）',
    await page.isVisible('[data-agg2-suggested-counts]'))
  const before = await readNumbers()
  console.log('[M2] before-load-more:', JSON.stringify(before))
  check('M2b 状态条「待确认」= 500（后端真实总数，不是明细行数）', before.statusValue === String(EXPECT_TOTAL),
    'status=' + before.statusValue)
  check('M2c 抽屉徽标 = 500 条（与状态条同一个数）',
    before.badgeText.replace(/[^0-9]/g, '') === String(EXPECT_TOTAL), before.badgeText)
  check('M2d 明细行数 = 200（后端单次上限），表属性同步',
    before.rows === PAGE && before.tableAttr === String(PAGE),
    'rows=' + before.rows + ' table=' + before.tableAttr)
  check('M2e 抽屉明写「显示 200 / 共 500 条」（差额写在明面上，不是让人自己对）',
    before.countsText.indexOf('显示 200 / 共 500 条') >= 0, before.countsText)
  check('M2f 「加载更多」写明还剩 300 条',
    before.moreExists && before.moreRemaining === String(EXPECT_TOTAL - PAGE) &&
      before.moreText.indexOf('还有 300 条') >= 0,
    before.moreText + ' (remaining=' + before.moreRemaining + ')')
  check('M2g 页面上没有「口径不一致」告警（status 与明细接口同源）', before.mismatch === false)
  // 数字自洽：loaded + remaining = total，且 loaded ≤ total（矛盾数字的机器判据）
  const loaded0 = Number(before.loaded)
  const total0 = Number(before.total)
  const remaining0 = Number(before.moreRemaining)
  check('M2h 数字自洽：显示条数 + 剩余 = 总数，且不超过总数',
    loaded0 + remaining0 === total0 && loaded0 <= total0 && total0 === EXPECT_TOTAL,
    loaded0 + ' + ' + remaining0 + ' = ' + total0)
  // 抽屉在页面底部：先把它滚进视口，截图才是「人打开抽屉看到的东西」（不然截的是首屏三列）
  await page.locator('[data-agg2-suggested-counts]').scrollIntoViewIfNeeded()
  await sleep(200)
  await page.screenshot({ path: join(SHOTS, 'many-500-before-load.png'), fullPage: false })

  // ------------------------------------------------------- M4 点两次「加载更多」取满 500
  for (const expect of [400, EXPECT_TOTAL]) {
    await page.click('[data-agg2-suggested-more]')
    await page.waitForFunction(
      (n) => document.querySelectorAll('[data-agg2-drawer="suggested"] .hc-agg__row').length === n,
      expect,
      { timeout: 20000 }
    )
    const step = await readNumbers()
    console.log('[M4] rows=' + step.rows + ' loaded=' + step.loaded + ' counts=' + step.countsText)
    check('M4 点「加载更多」后明细到 ' + expect + ' 行（loaded 字段同步）',
      step.rows === expect && step.loaded === String(expect),
      'rows=' + step.rows + ' loaded=' + step.loaded)
  }
  const full = await readNumbers()
  console.log('[M4] full:', JSON.stringify({ rows: full.rows, uniqueIds: full.uniqueIds, counts: full.countsText, more: full.moreExists }))
  check('M4a 取满 500 行，且 session_id 去重后正好 500 个（没有重复渲染）',
    full.rows === EXPECT_TOTAL && full.uniqueIds === EXPECT_TOTAL,
    'rows=' + full.rows + ' unique=' + full.uniqueIds)
  check('M4b 取满后抽屉写「显示 500 / 共 500 条」', full.countsText.indexOf('显示 500 / 共 500 条') >= 0, full.countsText)
  check('M4c 取满后「加载更多」消失（不留点了没反应的按钮）', full.moreExists === false)
  check('M4d 取满后状态条与徽标仍是 500（没有跟着行数漂移）',
    full.statusValue === String(EXPECT_TOTAL) && full.badgeText.replace(/[^0-9]/g, '') === String(EXPECT_TOTAL),
    'status=' + full.statusValue + ' badge=' + full.badgeText)
  await page.locator('[data-agg2-suggested-counts]').scrollIntoViewIfNeeded()
  await sleep(200)
  await page.screenshot({ path: join(SHOTS, 'many-500-loaded.png'), fullPage: false })

  // ------------------------------------------------------- M5 窄屏也不打架（900 宽）
  await page.setViewportSize({ width: 900, height: 900 })
  await sleep(300)
  const narrow = await page.evaluate(() => {
    const el = document.querySelector('[data-agg2-suggested-counts]')
    if (!el) return null
    const r = el.getBoundingClientRect()
    const root = document.querySelector('[data-agg2-root]')
    const limit = root ? root.getBoundingClientRect().right : window.innerWidth
    return { text: (el.textContent || '').replace(/\s+/g, ' ').trim(), overflow: r.right > limit + 2 }
  })
  check('M5a 900 宽下计数行不横向溢出', Boolean(narrow) && narrow.overflow === false,
    narrow ? narrow.text + ' overflow=' + narrow.overflow : '(missing)')
  await page.locator('[data-agg2-suggested-counts]').scrollIntoViewIfNeeded()
  await sleep(200)
  await page.screenshot({ path: join(SHOTS, 'many-500-900w.png'), fullPage: false })

  // ------------------------------------------------------- M6 零报错 + 真 home 没动
  check('M6a 全程零 console error', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '))
  check('M6b 全程零 pageerror', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '))
  const realAfter = sha256(REAL_ASSIGN)
  check('M6c 真 home 的 assignments.db 一个字节没动（夹具只写沙盒）',
    realBefore === realAfter, 'before=' + realBefore.slice(0, 12) + ' after=' + realAfter.slice(0, 12))
  console.log('[shots] ' + SHOTS)
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) await new Promise((r) => site.close(r))
  if (fixture && fixture.child) fixture.child.kill()
  // junction 必须用 rmdir/unlink 摘掉链接本身 —— rmSync(recursive) 会走进去删宿主的 node_modules。
  try {
    if (process.platform === 'win32') rmdirSync(join(WORK, 'node_modules'))
    else unlinkSync(join(WORK, 'node_modules'))
    rmSync(WORK, { recursive: true, force: true })
  } catch (e) {
    console.log('  (清理临时目录失败，手动删：' + WORK + ')')
  }
}

console.log('\n=== 汇总 ===')
console.log('PASS: ' + pass + '   FAIL: ' + failures.length)
if (failures.length) {
  failures.forEach((f) => console.log('  ! ' + f))
  process.exitCode = 1
} else {
  console.log('MANY-PROBE-OK')
}
