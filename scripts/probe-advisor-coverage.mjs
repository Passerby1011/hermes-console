/**
 * 快速对账：真后端 /advisor/coverage 每一行的 status / settings_enabled / settings_path。
 * 用途：核对面板徽标文案与 settings.json 真值是否一致（本卡 t_5dbc60b8）。
 *
 * 跑法：node scripts/probe-advisor-coverage.mjs [--json]
 */
import { existsSync, readFileSync } from 'node:fs'

const HERMES_HOME = process.env.HERMES_HOME_DIR || 'D:/hermes-data/hermes'
const HOST = '127.0.0.1'
const PORT = Number(process.env.HC_LIVE_BACKEND_PORT || 9119)
const API = '/api/plugins/hermes-console'

function readDotEnv() {
  const path = HERMES_HOME + '/.env'
  const out = {}
  if (!existsSync(path)) return out
  for (const raw of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const i = line.indexOf('=')
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, '')
  }
  return out
}

async function realLogin() {
  const env = readDotEnv()
  const username = env.HERMES_DASHBOARD_BASIC_AUTH_USERNAME || ''
  const password = env.HERMES_DASHBOARD_BASIC_AUTH_PASSWORD || ''
  if (!username || !password) throw new Error('读不到 .env 里的 dashboard basic-auth 凭据')
  const res = await fetch(`http://${HOST}:${PORT}/auth/password-login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ provider: 'basic', username, password, next: '/' })
  })
  if (!res.ok) throw new Error('登录失败 ' + res.status)
  const raw = res.headers.getSetCookie ? res.headers.getSetCookie() : []
  const jar = {}
  for (const item of raw) {
    const pair = String(item).split(';', 1)[0]
    const i = pair.indexOf('=')
    if (i > 0) jar[pair.slice(0, i).trim()] = pair.slice(i + 1).trim()
  }
  return Object.keys(jar).map((k) => k + '=' + jar[k]).join('; ')
}

const cookie = await realLogin()
const res = await fetch(`http://${HOST}:${PORT}${API}/advisor/coverage`, { headers: { cookie } })
const payload = await res.json()
const body = payload.data || payload
const rows = body.rows || body.profiles || []
console.log('HTTP ' + res.status + '  rows=' + rows.length)
const table = rows.map((r) => ({
  profile: r.profile,
  status: r.status,
  enabled_in_config: r.enabled_in_config,
  settings_enabled: r.settings_enabled,
  settings_exists: r.settings_exists,
  ledger_events: r.ledger_events,
  review_lines: r.review_lines,
  session_files: r.session_files,
  injection_allowed: r.injection_allowed,
  settings_path: r.settings_path,
  // 真值：直接读盘，不信接口
  disk_enabled: (() => {
    try {
      const j = JSON.parse(readFileSync(r.settings_path, 'utf8'))
      return j && typeof j === 'object' && 'enabled' in j ? j.enabled : '(no key→默认true)'
    } catch (e) {
      return '(missing)'
    }
  })()
}))
if (process.argv.includes('--json')) {
  console.log(JSON.stringify(table, null, 2))
} else {
  for (const t of table) {
    console.log([t.profile, t.status, 'cfg=' + t.enabled_in_config, 'settings=' + t.settings_enabled,
      'disk=' + t.disk_enabled, 'ledger=' + t.ledger_events, 'reviews=' + t.review_lines,
      'sessions=' + t.session_files].join('  '))
  }
}

// ---- 交叉核对：/advisor/settings?agent=work 与 /advisor/modes 里的 enabled ----
if (process.argv.includes('--cross')) {
  for (const agent of ['work', 'cad-worker', 'default']) {
    const s = await (await fetch(`http://${HOST}:${PORT}${API}/advisor/settings?agent=${agent}`, { headers: { cookie } })).json()
    const sb = s.data || s
    const m = await (await fetch(`http://${HOST}:${PORT}${API}/advisor/modes?agent=${agent}`, { headers: { cookie } })).json()
    const mb = m.data || m
    const mrow = (mb.settings || []).find((r) => r.profile === agent) || {}
    console.log('CROSS ' + agent
      + '  settings.enabled=' + JSON.stringify(sb.enabled)
      + '  settings.stored=' + JSON.stringify(sb.stored)
      + '  settings.path=' + sb.path
      + '  modes.enabled=' + JSON.stringify(mrow.enabled)
      + '  modes.path=' + mb.setting_path)
  }
}
