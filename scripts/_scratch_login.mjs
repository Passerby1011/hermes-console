/**
 * 临时探针（本卡自用）：真登录态 cookie 落盘，供 curl 打真 serve(9119)。
 * 用法： node scripts/_scratch_login.mjs
 * 产物： scripts/_probe_out/_cookie.txt （一行 `name=value; name2=value2`）
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = resolve(HERE, '..')
const HOME = process.env.HERMES_HOME_DIR || 'D:/hermes-data/hermes'
const HOST = '127.0.0.1'
const PORT = Number(process.env.HC_LIVE_BACKEND_PORT || 9119)

function readDotEnv() {
  const p = join(HOME, '.env')
  const out = {}
  if (!existsSync(p)) return out
  for (const line of readFileSync(p, 'utf8').split(/\r?\n/)) {
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const i = line.indexOf('=')
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, '')
  }
  return out
}

const env = readDotEnv()
const username = env.HERMES_DASHBOARD_BASIC_AUTH_USERNAME || ''
const password = env.HERMES_DASHBOARD_BASIC_AUTH_PASSWORD || ''
if (!username || !password) throw new Error('读不到 .env 里的 dashboard basic-auth 凭据')
const res = await fetch(`http://${HOST}:${PORT}/auth/password-login`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ provider: 'basic', username, password, next: '/' })
})
const text = await res.text()
if (!res.ok) throw new Error('登录失败 ' + res.status + ': ' + text.slice(0, 200))
const raw = res.headers.getSetCookie ? res.headers.getSetCookie() : []
const jar = {}
for (const item of raw) {
  const pair = String(item).split(';', 1)[0]
  const i = pair.indexOf('=')
  if (i > 0) jar[pair.slice(0, i).trim()] = pair.slice(i + 1).trim()
}
if (!Object.keys(jar).length) throw new Error('登录成功但没有拿到会话 cookie')
const cookie = Object.keys(jar).map((k) => k + '=' + jar[k]).join('; ')
const outDir = join(PLUGIN, 'scripts', '_probe_out')
mkdirSync(outDir, { recursive: true })
writeFileSync(join(outDir, '_cookie.txt'), cookie, 'utf8')
console.log('LOGIN-OK cookie names: ' + Object.keys(jar).join(' / '))
