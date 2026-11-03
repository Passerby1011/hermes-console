/**
 * hermes-console · 「自动命名」分区真浏览器验收（Chromium + 真产物 + 真后端）
 *
 * 跑法（仓库根目录下）:
 *
 *     node plugins/hermes-console/scripts/probe-autotitler.mjs
 *
 * 三层都是真的（与 probe-models-layout.mjs 同一套路）：
 *
 *   1. **真产物**：`dashboard/dist/index.js`（= `desktop/plugin.js`）原样加载，只套一层 harness 入口。
 *   2. **真后端**：`scripts/autotitler_fixture.py --sandbox` 按 dashboard 的方式加载**真实的**
 *      `dashboard/plugin_api.py`，`HERMES_HOME` 指向沙盒副本。写操作落在副本上，
 *      真 home 一个字节都不动（本探针用 sha256 盯住）。
 *   3. **真浏览器**：Playwright + Chromium(msedge)。
 *
 * 它证明的事（对应卡片验收 A/B/C）：
 *   - 23 项参数全部渲染、枚举下拉选项与 config_schema.choices 一致；
 *   - max_title_length 填 5 被拒且错误可读（前端先拦）；
 *   - **未修改时零写请求**（拦截网络请求计数）；
 *   - 只改一项 → 只发一个 PUT，且落宿主 settings、插件 config.yaml 字节不变；
 *   - 三类计数/失败率/失败分类/重试队列/每会话明细与后端（=日志/state.json）一致；
 *   - 既有「设置」分区（plugin:browserpaw 的配置那条链路）照常渲染（不退化）。
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
const HOST_MODULES = process.env.HERMES_NODE_MODULES || 'D:/hermes-data/hermes/hermes-agent/node_modules'
const DESKTOP_ASSETS = process.env.HERMES_DESKTOP_ASSETS || 'D:/hermes-data/hermes/hermes-agent/apps/desktop/dist/assets'
const PYTHON = process.env.HERMES_PYTHON || 'D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe'
const FIXTURE_PORT = Number(process.env.HC_ATL_FIXTURE_PORT || 9197)
const SITE_PORT = Number(process.env.HC_ATL_SITE_PORT || 9196)
const API = '/api/plugins/hermes-console'

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

/** 容器内有没有横向溢出（900 / 1350 两个宽度都要过）。 */
async function overflowReport(page, width) {
  await page.setViewportSize({ width: width, height: 960 })
  await sleep(450)
  return page.evaluate(() => {
    const pane = document.querySelector('.hc-pane__body') || document.querySelector('.hc-atl')
    const box = pane ? pane.getBoundingClientRect() : { left: 0, right: window.innerWidth }
    const offenders = []
    Array.from(document.querySelectorAll('.hc-atl *')).forEach((el) => {
      const rect = el.getBoundingClientRect()
      if (rect.width > 0 && rect.right > box.right + 2) {
        offenders.push(
          (el.className || el.tagName) +
            ' right=' +
            Math.round(rect.right) +
            ' w=' +
            Math.round(rect.width) +
            ' parent=' +
            (el.parentElement ? el.parentElement.className || el.parentElement.tagName : '?') +
            ' text=' +
            String(el.textContent || '').slice(0, 16)
        )
      }
    })
    return {
      docOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2,
      offenders: offenders.slice(0, 4),
      inner: window.innerWidth
    }
  })
}

const WORK = mkdtempSync(join(tmpdir(), 'hc-atl-'))
const SANDBOX = join(WORK, 'home')

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
  // harness 里 import 了 react / react-dom：宿主 node_modules 用一个 junction 接过来，
  // 不复制、不安装（与 probe-dom.mjs 同一手法）。
  try {
    symlinkSync(HOST_MODULES, join(WORK, 'node_modules'), 'junction')
  } catch (e) {
    if (!existsSync(join(WORK, 'node_modules'))) throw new Error('无法建立到 ' + HOST_MODULES + ' 的 junction：' + e.message)
  }
  copyFileSync(join(DIST, 'index.js'), join(WORK, 'entry.js'))
  writeFileSync(join(WORK, 'harness.js'), HARNESS, 'utf8')
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
  const themeCss = existsSync(DESKTOP_ASSETS)
    ? readdirSafe(DESKTOP_ASSETS).filter((f) => /^main-.*\.css$/.test(f)).map((f) => join(DESKTOP_ASSETS, f))[0]
    : ''
  const html = `<!doctype html>
<html class="dark" lang="zh-CN">
<head><meta charset="utf-8"><title>自动命名验收</title>
${themeCss ? '<link rel="stylesheet" href="/theme.css">' : ''}
<link rel="stylesheet" href="/style.css">
<style>html,body{margin:0;padding:0;height:100%}#root{height:100%}</style>
</head><body><div id="root"></div>
<script type="module" src="/harness.bundle.js"></script></body></html>`

  const server = createServer((req, res) => {
    const url = (req.url || '/').split('?')[0]
    // /api/** 反代到 fixture：harness 里的 fetch 是**相对 URL**，会打到本静态服务器，
    // 不转发的话每个请求都是 404（实测：/autotitler/* 全 404）。
    if (url.indexOf(API) === 0) {
      const proxyReq = httpRequest(
        {
          host: '127.0.0.1',
          port: FIXTURE_PORT,
          path: req.url,
          method: req.method,
          headers: { ...req.headers, host: '127.0.0.1:' + FIXTURE_PORT }
        },
        (proxyRes) => {
          res.writeHead(proxyRes.statusCode || 502, proxyRes.headers)
          proxyRes.pipe(res)
        }
      )
      proxyReq.on('error', () => {
        if (!res.writableEnded) {
          res.writeHead(502)
          res.end('fixture unreachable')
        }
      })
      req.pipe(proxyReq)
      return
    }
    if (url === '/favicon.ico') {
      // 浏览器总会请求它；不提供就会在 console 里留一条 404（会污染「零 console error」验收）
      res.writeHead(204)
      res.end()
      return
    }
    if (url === '/') {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
      res.end(html)
      return
    }
    if (url === '/harness.bundle.js') {
      res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8' })
      res.end(readFileSync(join(WORK, 'harness.bundle.js')))
      return
    }
    if (url === '/style.css') {
      res.writeHead(200, { 'content-type': 'text/css; charset=utf-8' })
      res.end(readFileSync(join(DIST, 'style.css')))
      return
    }
    if (url === '/theme.css' && themeCss) {
      res.writeHead(200, { 'content-type': 'text/css; charset=utf-8' })
      res.end(readFileSync(themeCss))
      return
    }
    // 主题 CSS 里引用的字体（woff2/woff/ttf）也来自桌面产物目录：照原样提供，
    // 否则 console 里会留一条字体 404（那是夹具缺文件，不是插件问题）。
    if (/\.(woff2?|ttf|otf|svg|png|jpe?g)$/i.test(url) && existsSync(DESKTOP_ASSETS)) {
      const asset = join(DESKTOP_ASSETS, url.split('/').pop())
      if (existsSync(asset)) {
        res.writeHead(200)
        res.end(readFileSync(asset))
        return
      }
    }
    res.writeHead(404)
    res.end('not found')
  })
  return new Promise((res) => server.listen(SITE_PORT, '127.0.0.1', () => res(server)))
}

function readdirSafe(dir) {
  try {
    return readdirSync(dir)
  } catch (e) {
    return []
  }
}

async function startFixture() {
  const child = spawn(
    PYTHON,
    [join(HERE, 'autotitler_fixture.py'), '--port', String(FIXTURE_PORT), '--sandbox', SANDBOX],
    { stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let log = ''
  child.stdout.on('data', (c) => {
    log += c
  })
  child.stderr.on('data', (c) => {
    log += c
  })
  for (let i = 0; i < 240; i += 1) {
    if (log.indexOf('ready on') !== -1) return { child, log }
    if (child.exitCode !== null) break
    await sleep(100)
  }
  throw new Error('fixture 没起来：\n' + log)
}

// ---------------------------------------------------------------------------
const requireFromWork = createRequire(join(HOST_MODULES, 'probe.cjs'))
const { chromium } = requireFromWork('playwright')

let site = null
let fixture = null
let browser = null

try {
  mkdirSync(WORK, { recursive: true })
  await buildHarness()
  fixture = await startFixture()
  console.log('[fixture]', fixture.log.trim().split('\n').slice(-1)[0])
  site = await startSite()

  browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } })
  const consoleErrors = []
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e && e.message ? e.message : e)))
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const where = msg.location && msg.location() ? msg.location().url || '' : ''
    if (/favicon/i.test(msg.text()) || /favicon/i.test(where)) return
    consoleErrors.push(msg.text().slice(0, 200) + (where ? ' @ ' + where : ''))
  })
  await page.goto('http://127.0.0.1:' + SITE_PORT + '/', { waitUntil: 'load' })
  await page.waitForFunction('window.__hcBooted === true', null, { timeout: 20000 })
  await page.waitForSelector('.hc-nav__item', { timeout: 20000 })

  // ---------------------------------------------------------------- 导航
  const navLabels = await page.$$eval('.hc-nav__item', (nodes) => nodes.map((n) => n.textContent.trim()))
  check('导航里有「自动命名」', navLabels.some((t) => t.indexOf('自动命名') !== -1), navLabels.join(' / '))

  const navIndex = navLabels.findIndex((t) => t.indexOf('自动命名') !== -1)
  const navButtons = await page.$$('.hc-nav__item')
  await navButtons[navIndex].click()
  await page.waitForSelector('.hc-atl__banner', { timeout: 20000 })

  // ---------------------------------------------------------------- 监控 · 区间
  const windowButtons = await page.$$('.hc-atl__windowbtn')
  check('区间切换条有 3 个按钮（1 小时 / 24 小时 / 全部历史）', windowButtons.length === 3, 'buttons=' + windowButtons.length)

  const activeWindow = await page.$eval('.hc-atl__windowbtn--active', (n) => n.getAttribute('data-window'))
  check('缺省区间 = 最近 24 小时', activeWindow === '24h', 'active=' + activeWindow)

  const rate24 = await page.textContent('.hc-atl__banner-rate')
  check('24h 口径失败率 = 50.0%（3 失败 / 3 改名）', rate24.trim() === '50.0%', 'banner=' + rate24.trim())

  const statTexts = await page.$$eval('.hc-atl__stat', (nodes) =>
    nodes.map((n) => n.textContent.replace(/\s+/g, ' ').trim())
  )
  const hasStat = (needle) => statTexts.some((t) => t.indexOf(needle) !== -1)
  check('成功改名计数 = 3（24h 内 1 条 9 小时前 + 2 条 10 分钟前）', hasStat('成功改名'), statTexts.join(' | ').slice(0, 200))
  const renamedStat = statTexts.find((t) => t.indexOf('成功改名') !== -1) || ''
  check('成功改名数值是 3', /^3\s*成功改名/.test(renamedStat), renamedStat)
  const failedStat = statTexts.find((t) => /^\d+\s*失败/.test(t)) || ''
  check('失败数值是 3', /^3\s*失败/.test(failedStat), failedStat)

  const bannerWindow = await page.textContent('.hc-atl__banner-window')
  check(
    '失败率旁边写明时间范围与最后更新时间',
    /区间：/.test(bannerWindow) && /最后更新：/.test(bannerWindow),
    bannerWindow.replace(/\s+/g, ' ').trim()
  )
  const nowLine = await page.textContent('.hc-atl__nowline')
  check(
    '「现在（最近 1 小时）」始终可见且零失败',
    /最近 1 小时/.test(nowLine) && /零失败/.test(nowLine),
    nowLine.replace(/\s+/g, ' ').trim()
  )

  // —— 切到 1 小时：数字必须真的变
  await page.click('.hc-atl__windowbtn[data-window="1h"]')
  await sleep(300)
  const rate1h = (await page.textContent('.hc-atl__banner-rate')).trim()
  const activeAfter1h = await page.$eval('.hc-atl__windowbtn--active', (n) => n.getAttribute('data-window'))
  check('切到 1 小时：选中态切换', activeAfter1h === '1h', 'active=' + activeAfter1h)
  check('切到 1 小时：失败率变成 0.0%（当前是好的）', rate1h === '0.0%', 'banner=' + rate1h)
  const bannerTitle1h = (await page.textContent('.hc-atl__banner-title')).replace(/\s+/g, ' ').trim()
  check('标题写明「最近 1 小时」口径', bannerTitle1h.indexOf('最近 1 小时') !== -1, bannerTitle1h)

  // —— 切到全部历史：数字再变一次
  await page.click('.hc-atl__windowbtn[data-window="all"]')
  await sleep(300)
  const rateAll = (await page.textContent('.hc-atl__banner-rate')).trim()
  check('切到全部历史：失败率 = 62.5%（5 失败 / 3 改名）', rateAll === '62.5%', 'banner=' + rateAll)
  const statsAll = await page.$$eval('.hc-atl__stat', (nodes) =>
    nodes.map((n) => n.textContent.replace(/\s+/g, ' ').trim())
  )
  const failedStatAll = statsAll.find((t) => /^\d+\s*失败/.test(t)) || ''
  check('全部历史口径失败计数 = 5（含 30 小时前那 2 次）', /^5\s*失败/.test(failedStatAll), failedStatAll)
  check(
    '三个口径的数字互不相同（0% / 50% / 62.5%）',
    rate1h !== rate24.trim() && rate24.trim() !== rateAll && rate1h !== rateAll,
    rate1h + ' / ' + rate24.trim() + ' / ' + rateAll
  )

  // 回到 24h（后面按 24h 口径断言）
  await page.click('.hc-atl__windowbtn[data-window="24h"]')
  await sleep(250)

  // ---------------------------------------------------------------- 监控 · 基线
  const baselineHeadline = (await page.textContent('.hc-atl__baseline-copy')).replace(/\s+/g, ' ').trim()
  check(
    '基线面板给出「此前 N 次失败、此后 0 次」',
    /基线之前失败 5 次/.test(baselineHeadline) && /基线之后失败 0 次/.test(baselineHeadline),
    baselineHeadline
  )
  const baselineTs = (await page.textContent('.hc-atl__baseline-ts')).trim()
  check('基线时间可见', /\d{4}-\d{2}-\d{2}/.test(baselineTs), baselineTs)
  const sideValues = await page.$$eval('.hc-atl__side-value', (nodes) => nodes.map((n) => n.textContent.trim()))
  check(
    '基线两侧卡片（之前 5 次 / 之后 0 次）',
    sideValues.length === 2 && /5/.test(sideValues[0]) && /0/.test(sideValues[1]),
    sideValues.join(' | ')
  )
  const timelineBars = await page.$$eval('.hc-atl__timeline-bar', (n) => n.length)
  check('时间线画出来了（≥3 个桶）', timelineBars >= 3, 'bars=' + timelineBars)

  const diagnosis = await page.textContent('.hc-atl__diagnosis-head')
  check('失败原因一眼可见（散文而非 JSON）', diagnosis.indexOf('散文') !== -1, diagnosis)

  const classKeys = await page.$$eval('.hc-atl__bar-key', (nodes) => nodes.map((n) => n.textContent.trim()))
  check(
    '失败原因分类聚合正确',
    classKeys.indexOf('invalid_decision_payload') !== -1 && classKeys.indexOf('model_call_error') !== -1,
    classKeys.join(' / ')
  )

  const retryRows = await page.$$eval('.hc-atl__panel', (panels) => {
    const target = panels.find((p) => (p.querySelector('.hc-atl__panel-title') || {}).textContent === '重试队列')
    if (!target) return -1
    return target.querySelectorAll('.hc-atl__table-row').length
  })
  check('重试队列条目数 = 2（state.json 的 finalize + retry）', retryRows === 2, 'rows=' + retryRows)

  const sessionRows = await page.$$eval('.hc-atl__table--wide .hc-atl__table-row', (n) => n.length)
  check('每会话明细 3 行', sessionRows === 3, 'rows=' + sessionRows)

  // 监控页整屏留证（失败率大条 + 分类 + 重试队列 + 每会话明细都在这一屏）
  mkdirSync(join(PLUGIN, 'scripts', '_probe_out'), { recursive: true })
  await page.screenshot({ path: join(PLUGIN, 'scripts', '_probe_out', 'autotitler-monitor.png'), fullPage: true })

  // ---------------------------------------------------------------- 运行日志
  const tabs = await page.$$('.hc-atl__tab')
  check('三个页签（运行监控 / 运行日志 / 参数设置）', tabs.length === 3, 'tabs=' + tabs.length)
  await tabs[1].click()
  await page.waitForSelector('.hc-atl__table--log', { timeout: 20000 })
  await sleep(300)

  const logRows = await page.$$eval('.hc-atl__table--log .hc-atl__table-row', (n) =>
    n.map((r) => Array.from(r.children).map((c) => c.textContent.trim()))
  )
  check('日志页读出 audit 行（≥5 条）', logRows.length >= 5, 'rows=' + logRows.length)
  check(
    '每行都有 时间 / 会话 / action / 原因 / 耗时 五列',
    logRows.every((cells) => cells.length === 5 && /\d{4}-\d{2}-\d{2}/.test(cells[0]) && cells[1] && cells[2]),
    JSON.stringify(logRows[0] || [])
  )
  const stamps = logRows.map((cells) => cells[0])
  check(
    '按时间倒序（新 → 旧）',
    stamps.join('|') === stamps.slice().sort().reverse().join('|'),
    stamps[0] + ' … ' + stamps[stamps.length - 1]
  )

  const chips = await page.$$eval('.hc-atl__chipbtn', (n) => n.map((x) => x.getAttribute('data-act')))
  check('action 过滤 chips 出现（含 failed）', chips.indexOf('failed') !== -1, chips.join(','))

  await page.click('.hc-atl__chipbtn[data-act="failed"]')
  await sleep(700)
  const failedActs = await page.$$eval('.hc-atl__table--log .hc-atl__table-row', (n) =>
    n.map((r) => r.getAttribute('data-act'))
  )
  check(
    '按 action=failed 过滤后只剩失败行（5 条）',
    failedActs.length === 5 && failedActs.every((a) => a === 'failed'),
    failedActs.join(',')
  )

  await page.click('.hc-atl__chipbtn[data-act="all"]')
  await sleep(600)
  await page.fill('.hc-atl__input--narrow', 'local-ee')
  await page.click('[data-role="sid-apply"]')
  await sleep(700)
  const sidRows = await page.$$eval('.hc-atl__table--log .hc-atl__table-row', (n) =>
    n.map((r) => Array.from(r.children).map((c) => c.textContent.trim()))
  )
  check('按会话 id 过滤（local-ee → 1 行）', sidRows.length === 1 && sidRows[0][1] === 'local-ee', JSON.stringify(sidRows))

  const logPanelInfo = await page.evaluate(() => {
    const panel = Array.from(document.querySelectorAll('.hc-atl__panel')).find(
      (p) => (p.querySelector('.hc-atl__panel-title') || {}).textContent === '运行日志（audit 行）'
    )
    if (!panel) return { text: '', buttons: ['<no panel>'] }
    return {
      text: panel.textContent.replace(/\s+/g, ' '),
      buttons: Array.from(panel.querySelectorAll('button'))
        .map((b) => b.textContent.trim())
        .filter((t) => /删除|清空|移除/.test(t))
    }
  })
  check('脱敏说明在页面上', /只读 · 脱敏/.test(logPanelInfo.text), logPanelInfo.text.slice(-80))
  check('日志页没有删除/清空入口', logPanelInfo.buttons.length === 0, logPanelInfo.buttons.join(','))

  await page.screenshot({ path: join(PLUGIN, 'scripts', '_probe_out', 'autotitler-logs.png'), fullPage: true })

  // ---------------------------------------------------------------- 设置
  const tabs2 = await page.$$('.hc-atl__tab')
  await tabs2[2].click()
  await page.waitForSelector('.hc-atl__field', { timeout: 20000 })
  await sleep(300)

  const fieldCount = await page.$$eval('.hc-atl__field', (n) => n.length)
  check('23 项参数全部渲染', fieldCount === 23, 'fields=' + fieldCount)

  const pinnedBlocks = await page.$$eval('.hc-atl__pinned-block', (n) =>
    n.map((x) => ({ id: x.getAttribute('data-pinned'), title: (x.querySelector('.hc-atl__pinned-head') || {}).textContent }))
  )
  check(
    '用户点名的四项置顶（是否开启 / 多久监测 / 模型 / 提示词）',
    pinnedBlocks.length === 4 && pinnedBlocks.map((b) => b.id).join(',') === 'enabled,rhythm,model,prompt',
    pinnedBlocks.map((b) => b.id + ':' + b.title).join(' | ')
  )
  const pinnedFieldCount = await page.$$eval('.hc-atl__pinned .hc-atl__field', (n) => n.length)
  check(
    '置顶区含 6 个具体键（enabled / every_n_turns / min_interval_minutes / title_model / provider / custom_instructions）',
    pinnedFieldCount === 6,
    'pinned fields=' + pinnedFieldCount
  )
  const dupKeys = await page.$$eval('.hc-atl__field', (n) => {
    const keys = n.map((x) => x.getAttribute('data-key'))
    return keys.filter((k, i) => keys.indexOf(k) !== i)
  })
  check('每个参数只渲染一次（置顶与分组不重复）', dupKeys.length === 0, dupKeys.join(','))

  const promptTag = await page.$eval('.hc-atl__field[data-key="custom_instructions"]', (n) =>
    n.querySelector('textarea') ? 'textarea' : 'not-textarea'
  )
  check('提示词是 textarea（不是单行 input）', promptTag === 'textarea', promptTag)
  mkdirSync(join(PLUGIN, 'scripts', '_probe_out'), { recursive: true })
  await page.screenshot({ path: join(PLUGIN, 'scripts', '_probe_out', 'autotitler-settings.png'), fullPage: true })

  const groupTitles = await page.$$eval('.hc-atl__group-title', (n) => n.map((x) => x.textContent.trim()))
  check(
    '按语义分组渲染（置顶的键不再重复分组）',
    groupTitles.length >= 3 && groupTitles.some((t) => t.indexOf('开关与模式') !== -1),
    groupTitles.join(' / ')
  )

  const strategyOptions = await page.$$eval('.hc-atl__field[data-key="strategy"] option', (n) =>
    n.map((x) => x.value)
  )
  check(
    '枚举下拉与 config_schema.choices 一致',
    JSON.stringify(strategyOptions) === JSON.stringify(['conservative', 'aggressive']),
    strategyOptions.join(',')
  )

  const sourceText = await page.textContent('.hc-atl__field[data-key="every_n_turns"] .hc-atl__from')
  check(
    '显示「值来自哪一层」',
    sourceText.indexOf('宿主 settings') !== -1,
    sourceText.replace(/\s+/g, ' ').trim()
  )
  const defaultText = await page.textContent('.hc-atl__field[data-key="every_n_turns"] .hc-atl__default')
  check('显示默认值', defaultText.indexOf('2') !== -1, defaultText.replace(/\s+/g, ' ').trim())

  // 未改动时保存按钮是禁用的（零写请求的硬保证之一）
  const saveDisabled = await page.$eval('.hc-atl__panel-actions button', (n) => n.disabled)
  check('未改动时保存按钮禁用（零写请求）', saveDisabled === true)

  // ------------------------------------------------- 非法值被拒（前端先拦）
  await page.fill('.hc-atl__field[data-key="max_title_length"] input', '5')
  await page.waitForSelector('.hc-atl__field[data-key="max_title_length"] .hc-atl__field-error', {
    timeout: 5000
  })
  const fieldError = await page.textContent('.hc-atl__field[data-key="max_title_length"] .hc-atl__field-error')
  check('max_title_length=5 被拒且错误可读', fieldError.indexOf('10') !== -1, fieldError)

  const putBefore = await page.evaluate(() =>
    (window.__hcRequests || []).filter((r) => r.method === 'PUT').length
  )
  await page.click('.hc-atl__panel-actions button')
  await sleep(400)
  const putAfterInvalid = await page.evaluate(() =>
    (window.__hcRequests || []).filter((r) => r.method === 'PUT').length
  )
  check('非法值不会产生写请求', putAfterInvalid === putBefore, 'PUT ' + putBefore + ' → ' + putAfterInvalid)

  // ------------------------------------------------- 只改一项 → 只发一个 PUT
  await page.fill('.hc-atl__field[data-key="max_title_length"] input', '0')
  await page.fill('.hc-atl__field[data-key="max_display_width"] input', '64')
  await sleep(150)
  const putBeforeSave = await page.evaluate(() =>
    (window.__hcRequests || []).filter((r) => r.method === 'PUT').length
  )
  // 作用域决定写入落点：default → 根 config.yaml；其它 → profiles/<agent>/config.yaml
  const scopeAgent = await page.$eval('.hc-atl__tabs-right .hc-atl__muted', (n) =>
    n.textContent.replace('作用域', '').trim()
  )
  const hostCfg =
    scopeAgent === 'default'
      ? join(SANDBOX, 'config.yaml')
      : join(SANDBOX, 'profiles', scopeAgent, 'config.yaml')
  const pluginCfg = join(SANDBOX, 'plugins', 'hermes-auto-titler', 'config.yaml')
  const pluginCfgBefore = sha256(pluginCfg)
  const hostCfgBefore = sha256(hostCfg)

  await page.click('.hc-atl__panel-actions button')
  await page.waitForSelector('.hc-atl__save-status--good', { timeout: 15000 })
  const status = await page.textContent('.hc-atl__save-status')
  const putAfterSave = await page.evaluate(() =>
    (window.__hcRequests || []).filter((r) => r.method === 'PUT').length
  )
  check('只改一项就只发一个 PUT', putAfterSave - putBeforeSave === 1, 'delta=' + (putAfterSave - putBeforeSave))
  check('保存后回读一致并提示', status.indexOf('回读确认一致') !== -1, status)
  check('插件 config.yaml 字节未变（写的是宿主 settings）', sha256(pluginCfg) === pluginCfgBefore)
  check('宿主 settings 文件真的被改了', sha256(hostCfg) !== hostCfgBefore, scopeAgent)
  const hostText = readFileSync(hostCfg, 'utf8')
  check('宿主 settings 里出现了新值 64', /max_display_width:\s*64/.test(hostText), hostText.match(/max_display_width:\s*\d+/))

  // 回读后「来自哪一层」要变成宿主层（等后台重取完成，最多 8s）
  let afterSource = ''
  for (let i = 0; i < 40; i += 1) {
    afterSource = await page.textContent('.hc-atl__field[data-key="max_display_width"] .hc-atl__from')
    if (afterSource.indexOf('宿主 settings') !== -1) break
    await sleep(200)
  }
  check('写后来源层变成宿主 settings', afterSource.indexOf('宿主 settings') !== -1, afterSource.replace(/\s+/g, ' ').trim())

  // ------------------------------------------------- 提示词真的能改并落库
  const promptText = '只回 JSON。标题不超过 20 字。\n不要照抄会话里的助手回复。'
  const putBeforePrompt = await page.evaluate(() =>
    (window.__hcRequests || []).filter((r) => r.method === 'PUT').length
  )
  await page.fill('.hc-atl__field[data-key="custom_instructions"] textarea', promptText)
  await sleep(150)
  await page.click('.hc-atl__panel-actions button')
  await page.waitForSelector('.hc-atl__save-status--good', { timeout: 15000 })
  const putAfterPrompt = await page.evaluate(() =>
    (window.__hcRequests || []).filter((r) => r.method === 'PUT').length
  )
  const promptStatus = await page.textContent('.hc-atl__save-status')
  const hostTextAfter = readFileSync(hostCfg, 'utf8')
  check('提示词改动只发一个 PUT', putAfterPrompt - putBeforePrompt === 1, 'delta=' + (putAfterPrompt - putBeforePrompt))
  check('提示词保存后回读一致', promptStatus.indexOf('回读确认一致') !== -1, promptStatus)
  check(
    '提示词真的写进宿主 settings',
    /custom_instructions:/.test(hostTextAfter) && hostTextAfter.indexOf('不要照抄会话里的助手回复') !== -1,
    (hostTextAfter.match(/custom_instructions:[\s\S]{0,120}/) || [''])[0].replace(/\s+/g, ' ')
  )
  check('插件 config.yaml 仍然一字未动', sha256(pluginCfg) === pluginCfgBefore)

  // ------------------------------------------------- 900 / 1350 无横向溢出
  for (const width of [900, 1350]) {
    const report = await overflowReport(page, width)
    check(width + 'px 宽度下无横向溢出（参数页）', !report.docOverflow && report.offenders.length === 0, JSON.stringify(report))
  }
  await page.setViewportSize({ width: 900, height: 960 })
  await page.screenshot({ path: join(PLUGIN, 'scripts', '_probe_out', 'autotitler-settings-900.png'), fullPage: true })

  const tabs3 = await page.$$('.hc-atl__tab')
  await tabs3[0].click()
  await page.waitForSelector('.hc-atl__banner', { timeout: 20000 })
  for (const width of [900, 1350]) {
    const report = await overflowReport(page, width)
    check(width + 'px 宽度下无横向溢出（监控页）', !report.docOverflow && report.offenders.length === 0, JSON.stringify(report))
  }
  await page.setViewportSize({ width: 900, height: 960 })
  await page.screenshot({ path: join(PLUGIN, 'scripts', '_probe_out', 'autotitler-monitor-900.png'), fullPage: true })

  // ------------------------------------------------- 不退化：既有设置分区
  const navAfter = await page.$$eval('.hc-nav__item', (nodes) => nodes.map((n) => n.textContent.trim()))
  const settingsIndex = navAfter.findIndex((t) => t.trim() === '设置')
  check('既有「设置」分区仍在导航里', settingsIndex !== -1, navAfter.join(' / '))
  const navButtons2 = await page.$$('.hc-nav__item')
  await navButtons2[settingsIndex].click()
  await page.waitForFunction(
    () => {
      const panes = Array.from(document.querySelectorAll('.hc-pane'))
      return panes.some((p) => p.getAttribute('aria-hidden') !== 'true' && p.textContent.length > 40)
    },
    null,
    { timeout: 20000 }
  )
  await sleep(1200)
  const settingsText = await page.evaluate(() => {
    const panes = Array.from(document.querySelectorAll('.hc-pane'))
    const visible = panes.find((p) => p.offsetParent !== null) || panes[0]
    return visible ? visible.textContent.replace(/\s+/g, ' ').slice(0, 400) : ''
  })
  check('设置分区照常渲染（插件清单/配置入口）', settingsText.length > 20, settingsText.slice(0, 120))
  // 回归留证：既有「设置」分区（plugin:browserpaw 那条链路）照常工作
  await page.screenshot({ path: join(PLUGIN, 'scripts', '_probe_out', 'settings-section.png'), fullPage: true })

  check('没有页面级 JS 异常', consoleErrors.length === 0, consoleErrors.join(' | ').slice(0, 300))

} catch (error) {
  failures.push('探针异常：' + (error && error.stack ? error.stack : String(error)))
  console.log('  FAIL 探针异常：' + String(error && error.message ? error.message : error))
} finally {
  if (browser) await browser.close().catch(() => {})
  if (site) site.close()
  if (fixture && fixture.child) fixture.child.kill()
}

console.log('\n' + pass + ' passed, ' + failures.length + ' failed')
if (failures.length) {
  console.log('失败项：')
  failures.forEach((item) => console.log('  - ' + item))
  process.exitCode = 1
}
