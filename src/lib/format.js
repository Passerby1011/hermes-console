/**
 * hermes-console · 格式化（时间 / 数字 / 字节）。
 *
 * 全部分区共用一套，避免三个分区对「3 分钟前」写出三种说法。
 * 纯函数，无副作用，不依赖 DOM。
 */

var MINUTE = 60 * 1000
var HOUR = 60 * MINUTE
var DAY = 24 * HOUR

/**
 * 任意输入 → 毫秒时间戳；解析不了返回 null（不要猜、不要填 0）。
 * 支持 Date / ISO 字符串 / 秒级或毫秒级 epoch。
 * @param {unknown} value
 * @returns {number|null}
 */
export function toMillis(value) {
  if (value === null || value === undefined || value === '') return null
  if (value instanceof Date) {
    var t = value.getTime()
    return isNaN(t) ? null : t
  }
  if (typeof value === 'number') {
    if (!isFinite(value) || value <= 0) return null
    // 10 位数是秒级（后端常见），13 位是毫秒
    return value < 1e12 ? Math.round(value * 1000) : Math.round(value)
  }
  var text = String(value).trim()
  if (!text) return null
  if (/^\d+$/.test(text)) return toMillis(Number(text))
  var parsed = Date.parse(text)
  return isNaN(parsed) ? null : parsed
}

/**
 * 相对时间：「刚刚 / 3 分钟前 / 2 小时前 / 昨天 / 3 天前 / 具体日期」。
 * 未来时间说「N 后」，不说「-N 前」。
 * @param {unknown} value
 * @param {number} [now]
 * @returns {string} 解析不了返回 '—'
 */
export function relTime(value, now) {
  var ms = toMillis(value)
  if (ms === null) return '—'
  var base = typeof now === 'number' ? now : Date.now()
  var delta = base - ms
  var future = delta < 0
  var abs = Math.abs(delta)
  var text
  if (abs < MINUTE) text = '刚刚'
  else if (abs < HOUR) text = Math.round(abs / MINUTE) + ' 分钟'
  else if (abs < DAY) text = Math.round(abs / HOUR) + ' 小时'
  else if (abs < 2 * DAY) text = '昨天'
  else if (abs < 30 * DAY) text = Math.round(abs / DAY) + ' 天'
  else text = absDate(ms)
  if (future) return text === '刚刚' ? '刚刚' : text + '后'
  return text === '刚刚' || text === '昨天' ? text : text + '前'
}

/**
 * 绝对时间：同年 `09-14 13:32`，跨年 `2024-09-14`。
 * @param {unknown} value
 * @param {number} [now]
 */
export function absDate(value, now) {
  var ms = toMillis(value)
  if (ms === null) return '—'
  var d = new Date(ms)
  var base = new Date(typeof now === 'number' ? now : Date.now())
  var pad = function (n) {
    return (n < 10 ? '0' : '') + n
  }
  var md = pad(d.getMonth() + 1) + '-' + pad(d.getDate())
  if (d.getFullYear() === base.getFullYear()) {
    return md + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes())
  }
  return d.getFullYear() + '-' + md
}

/**
 * 千分位整数。非数字返回 '—'（不显示 0 冒充真实值）。
 * @param {unknown} value
 * @returns {string}
 */
export function num(value) {
  if (value === null || value === undefined || value === '') return '—'
  var n = typeof value === 'number' ? value : Number(value)
  if (!isFinite(n)) return '—'
  var parts = String(Math.round(n)).split('.')
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return parts.join('.')
}

/**
 * 大数字的紧凑写法（统计卡标题用）：亿 / 万 / 千分位。
 *
 * 只做进位，不改数值：`343432426` → `3.4亿`。精确值仍然用 `num()` 显示在副标题里，
 * 免得"紧凑"变成"约等于"。
 * @param {unknown} value
 * @returns {string}
 */
export function compact(value) {
  if (value === null || value === undefined || value === '') return '—'
  var n = typeof value === 'number' ? value : Number(value)
  if (!isFinite(n)) return '—'
  var abs = Math.abs(n)
  if (abs >= 1e8) return (n / 1e8).toFixed(1) + '亿'
  if (abs >= 1e4) return (n / 1e4).toFixed(1) + '万'
  return num(n)
}

/**
 * 秒 → 「3 天 4 小时」这类时长。解析不了返回 '—'。
 * @param {unknown} value 秒
 */
export function duration(value) {
  if (value === null || value === undefined || value === '') return '—'
  var n = typeof value === 'number' ? value : Number(value)
  if (!isFinite(n) || n < 0) return '—'
  var days = Math.floor(n / 86400)
  var hours = Math.floor((n % 86400) / 3600)
  var minutes = Math.floor((n % 3600) / 60)
  if (days > 0) return days + ' 天 ' + hours + ' 小时'
  if (hours > 0) return hours + ' 小时 ' + minutes + ' 分'
  return minutes + ' 分钟'
}

/**
 * 字节 → B / KB / MB / GB（1024 进制，保留 1 位小数，整数不带小数点）。
 * @param {unknown} value
 * @returns {string}
 */
export function bytes(value) {
  if (value === null || value === undefined || value === '') return '—'
  var n = typeof value === 'number' ? value : Number(value)
  if (!isFinite(n) || n < 0) return '—'
  if (n < 1024) return Math.round(n) + ' B'
  var units = ['KB', 'MB', 'GB', 'TB']
  var size = n / 1024
  var unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size = size / 1024
    unit += 1
  }
  var rounded = Math.round(size * 10) / 10
  return (Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)) + ' ' + units[unit]
}

/**
 * 百分比（给定分子分母）。分母为 0 时返回 '—'。
 * @param {number} part
 * @param {number} total
 */
export function pct(part, total) {
  if (!isFinite(part) || !isFinite(total) || total <= 0) return '—'
  return Math.round((part / total) * 100) + '%'
}

/**
 * 截断长文本，保留首尾（日志行用）。
 * @param {unknown} value
 * @param {number} [max]
 */
export function clip(value, max) {
  var text = value === null || value === undefined ? '' : String(value)
  var limit = typeof max === 'number' ? max : 160
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text
}
