/**
 * hermes-console · 天气分区（C4：从零建前端）。
 *
 * 背景：weather-card 那个面板的 `dist/index.js` **从来不存在**（manifest 的 entry 指向空气），
 * 所以这个分区是本插件里第一次有天气前端。数据来自 `/weather/{now,daily,hourly,at}`
 * （后端 `dashboard/api_weather.py`，取数仍走 `scripts/weather_lib.py`：彩云 → 和风 → Open-Meteo）。
 *
 * 五条硬规矩（都写进了验收）：
 *   1. **只照抄工具输出**：温度、天气现象直接用后端给的值，不做「晴转多云」这类推测性合并；
 *      后端没给的字段一律显示 `—`，不填 0、不猜。
 *   2. **来源与取数时间必须可见**：每条数据下面都有 `来源 xx · 更新 HH:mm（N 分钟前）`，
 *      再加一行降级过程（哪个源 ✓ / 哪个源 ×空），回答"这数据哪来的"。
 *   3. **单位标在界面上**：℃ / % / km/h / μg/m³ 一律跟着数字走（单位表由后端 `units` 给）。
 *   4. **三态复用 C2**：加载 `Skeleton`、无数据 `Empty`、失败 `ErrorState`（带后端原话）。
 *   5. **地点不写死**：默认用后端配置的地点（`HERMES_WEATHER_PLACE`，缺省"家"），
 *      输入框可临时查任何城市或 `纬度,经度`，不影响默认值。
 *
 * 颜色纪律：本文件不出现任何字面色值，全部走 `style.css` 里的 `var(--ui-*)`。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

import { get } from '../lib/rest.js'
import { useQuery } from '../lib/query.js'
import { absDate, relTime } from '../lib/format.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 逐小时默认窗口（小时）。 */
var HOURLY_HOURS = 24
/** 多日默认窗口（天）。 */
var DAILY_DAYS = 7
/** 「N 小时后」的可选项。 */
var AT_OPTIONS = [1, 6, 12, 24, 48]
var AT_DEFAULT = 6
/** 后端来源 slug → 中文标签的兜底表（正常情况用后端给的 source_label）。 */
var SOURCE_LABELS = {
  caiyun: '彩云天气',
  qweather: '和风天气',
  'open-meteo': 'Open-Meteo'
}

// ---------------------------------------------------------------- 格式化
// 全部是"拿不到就 —"的纯函数：不把 null 变成 0，也不四舍五入到看不出原值。

function toNumber(value) {
  if (typeof value === 'number') return isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    var n = Number(value)
    return isFinite(n) ? n : null
  }
  return null
}

/** 数值 → 最多一位小数（工具给 18.83 就显示 18.8，整数不带小数点）。拿不到 → '—'。 */
function dec(value) {
  var n = toNumber(value)
  if (n === null) return '—'
  var rounded = Math.round(n * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

/** 数值 + 单位（单位由后端 `units` 给）。拿不到 → '—'（不带单位，免得看起来像有数据）。 */
function unit(value, suffix) {
  var text = dec(value)
  return text === '—' ? '—' : text + (suffix || '')
}

/** 百分比（降水概率等）。拿不到 → '—'。 */
function popText(value) {
  var n = toNumber(value)
  return n === null ? '—' : Math.round(n) + '%'
}

/** 天气现象：后端原话（含 emoji），不做任何合并/翻译。空 → '—'。 */
function descText(value) {
  var text = value === null || value === undefined ? '' : String(value).trim()
  return text || '—'
}

/** 后端 `units` 表（缺字段时退回常用单位）。 */
function unitsOf(body) {
  return (body && body.units) || {}
}

/** 时:分（逐小时列头用）。解析不了 → '—'。 */
function hourLabel(value) {
  var ms = value === null || value === undefined ? NaN : Date.parse(String(value))
  if (isNaN(ms)) return '—'
  var d = new Date(ms)
  var pad = function (n) {
    return (n < 10 ? '0' : '') + n
  }
  return pad(d.getHours()) + ':' + pad(d.getMinutes())
}

/** 日标签：MM-DD + 周几。解析不了 → '—'。 */
function dayLabel(value) {
  var ms = value === null || value === undefined ? NaN : Date.parse(String(value))
  if (isNaN(ms)) return { md: '—', wd: '' }
  var d = new Date(ms)
  var pad = function (n) {
    return (n < 10 ? '0' : '') + n
  }
  var names = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  return { md: pad(d.getMonth() + 1) + '-' + pad(d.getDate()), wd: names[d.getDay()] }
}

/** 降级过程一行：`彩云天气 ✓ · 和风天气 ×空 · Open-Meteo ✓`。 */
function attemptsText(body) {
  var rows = (body && body.sources) || []
  var parts = []
  rows.forEach(function (row) {
    if (!row || typeof row !== 'object') return
    var label = row.label || SOURCE_LABELS[row.source] || row.source || '?'
    parts.push(label + (row.ok ? ' ✓' : ' ×空'))
  })
  return parts.join(' · ')
}

// ---------------------------------------------------------------- 来源 + 更新时间
/**
 * 「来源 + 更新时间」渲染（验收第 4 项贴的就是这段）。
 *
 * 三块信息缺一不可：
 *   - 来源：后端显式返回的 `source_label`/`source`，不是前端猜的；
 *   - 更新：`fetched_at` 的绝对时刻（MM-DD HH:mm）+ 相对时间（N 分钟前）；
 *   - 降级过程：三个源各自 ✓ / ×空，一眼看出"是不是降级了、降到哪了"。
 * 任何一块拿不到就显示 `—`，不省略成空白（空白会让人以为没这回事）。
 */
function SourceStamp(props) {
  var body = props.body || {}
  var label = body.source_label || SOURCE_LABELS[body.source] || ''
  var tried = attemptsText(body)
  var stamp = absDate(body.fetched_at)
  var ago = relTime(body.fetched_at)
  return jsxs('div', {
    className: 'hc-wx-stamp',
    children: [
      jsx('span', {
        className: 'hc-wx-stamp__src',
        title: '数据来源：后端显式返回（source=' + (body.source || '—') + '）',
        children: '来源 ' + (label || '—')
      }),
      jsx('span', {
        className: 'hc-wx-stamp__time',
        title: '取数时间：' + (body.fetched_at || '—'),
        children: '更新 ' + (stamp === '—' ? '—' : stamp + '（' + ago + '）')
      }),
      jsx('span', {
        className: 'hc-wx-stamp__tried',
        title: '降级顺序：彩云 → 和风 → Open-Meteo（✓ = 命中，×空 = 该源返回空）',
        children: tried || '降级过程 —'
      })
    ]
  })
}

// ---------------------------------------------------------------- 小部件
function Metric(props) {
  return jsxs('div', {
    className: 'hc-wx-metric' + (props.wide ? ' hc-wx-metric--wide' : ''),
    children: [
      jsx('div', { className: 'hc-wx-metric__label', children: props.label }),
      jsx('div', { className: 'hc-wx-metric__value', children: props.value }),
      props.hint ? jsx('div', { className: 'hc-wx-metric__hint', children: props.hint }) : null
    ]
  })
}

function Block(props) {
  return jsxs('section', {
    className: 'hc-wx-block' + (props.className ? ' ' + props.className : ''),
    children: [
      jsxs('div', {
        className: 'hc-wx-block__head',
        children: [
          jsx('h3', { className: 'hc-wx-block__title', children: props.title }),
          props.note ? jsx('span', { className: 'hc-chip', children: props.note }) : null
        ]
      }),
      props.children
    ]
  })
}

// ---------------------------------------------------------------- 当前天气卡
function CurrentCard(props) {
  var body = props.body || {}
  var rt = body.realtime || {}
  var today = body.today || null
  var tomorrow = body.tomorrow || null
  var air = body.air || null
  var u = unitsOf(body)
  var tempUnit = u.temp || '℃'
  var windUnit = u.wind || 'km/h'
  var pmUnit = u.pm25 || 'μg/m³'

  var dayRange = function (row) {
    if (!row) return '—'
    var lo = dec(row.min)
    var hi = dec(row.max)
    if (lo === '—' && hi === '—') return '—'
    return lo + ' ~ ' + hi + tempUnit
  }

  var metrics = [
    jsx(Metric, { label: '体感', value: unit(rt.feels, tempUnit) }, 'feels'),
    jsx(Metric, { label: '湿度', value: unit(rt.humidity, '%') }, 'humidity'),
    jsx(Metric, { label: '风速', value: unit(rt.wind_kmh, windUnit) }, 'wind'),
    jsx(Metric, {
      label: 'PM2.5',
      value: air ? unit(air.pm25, pmUnit) : '—',
      hint: air ? 'AQI ' + dec(air.aqi_chn) + (air.aqi_level ? '（' + air.aqi_level + '）' : '') : '该数据源不提供颗粒物'
    }, 'pm25'),
    jsx(Metric, { label: '今日', value: dayRange(today), hint: today ? descText(today.text) : '—' }, 'today'),
    jsx(Metric, { label: '明日', value: dayRange(tomorrow), hint: tomorrow ? descText(tomorrow.text) : '—' }, 'tomorrow')
  ]

  var extras = []
  if (body.minutely) {
    extras.push(
      jsxs('div', { className: 'hc-wx-note', children: [
        jsx('span', { className: 'hc-wx-note__label', children: '未来 2 小时' }),
        jsx('span', { className: 'hc-wx-note__text', children: String(body.minutely) })
      ] }, 'minutely')
    )
  }
  var warnings = body.warnings || []
  warnings.forEach(function (text, index) {
    extras.push(
      jsxs('div', { className: 'hc-wx-note hc-wx-note--warn', children: [
        jsx('span', { className: 'hc-wx-note__label', children: '预警' }),
        jsx('span', { className: 'hc-wx-note__text', children: String(text) })
      ] }, 'warn' + index)
    )
  })
  var partial = body.partial_errors || []
  if (partial.length) {
    extras.push(
      jsxs('div', { className: 'hc-wx-note hc-wx-note--muted', children: [
        jsx('span', { className: 'hc-wx-note__label', children: '未取到' }),
        jsx('span', { className: 'hc-wx-note__text', children: partial.join('；') })
      ] }, 'partial')
    )
  }

  return jsxs('div', {
    className: 'hc-wx-now',
    children: [
      jsxs('div', {
        className: 'hc-wx-now__main',
        children: [
          jsx('div', { className: 'hc-wx-now__temp', children: unit(rt.temp, tempUnit) }),
          jsxs('div', {
            className: 'hc-wx-now__side',
            children: [
              jsx('div', { className: 'hc-wx-now__desc', title: '天气现象：照抄数据源输出', children: descText(rt.desc) }),
              jsx('div', { className: 'hc-wx-now__place', children: (body.place || '—') + '（' + ((body.place_resolved || {}).name || '—') + '）' })
            ]
          })
        ]
      }),
      jsx('div', { className: 'hc-wx-metrics', children: metrics }),
      extras.length ? jsx('div', { className: 'hc-wx-notes', children: extras }) : null,
      jsx(SourceStamp, { body: body })
    ]
  })
}

/** 区块副标题用的一句话：来源 + 多久之前取的（每个区块都要能自证数据来源）。 */
function sourceNote(body) {
  if (!body) return ''
  var label = body.source_label || SOURCE_LABELS[body.source] || '—'
  var ago = relTime(body.fetched_at)
  return '来源 ' + label + (ago === '—' ? '' : ' · ' + ago)
}

// ---------------------------------------------------------------- 逐小时
function HourlyBlock(props) {
  var query = props.query
  var body = query.data || {}
  var u = unitsOf(body)
  var tempUnit = u.temp || '℃'
  var rows = body.hourly || []
  var children

  if (query.status === 'loading') {
    children = jsx(Skeleton, { lines: 2, label: '正在读取逐小时预报' })
  } else if (query.status === 'error') {
    children = jsx(ErrorState, {
      title: '逐小时预报没读到',
      message: query.error,
      onRetry: query.reload
    })
  } else if (!rows.length) {
    children = jsx(Empty, {
      title: '逐小时没有数据',
      hint: '后端返回了响应但没有逐小时数组。可能这个地点只有日级预报，或数据源这次没给逐时。',
      action: { label: '重试', onClick: query.reload }
    })
  } else {
    children = jsx('div', {
      className: 'hc-wx-hours',
      role: 'list',
      children: rows.map(function (row, index) {
        var pop = popText(row.pop)
        return jsxs(
          'div',
          {
            className: 'hc-wx-hour',
            role: 'listitem',
            title: hourLabel(row.dt) + ' · ' + descText(row.desc) + ' · 降水概率 ' + pop,
            children: [
              jsx('div', { className: 'hc-wx-hour__time', children: hourLabel(row.dt) }),
              jsx('div', { className: 'hc-wx-hour__temp', children: unit(row.temp, tempUnit) }),
              jsx('div', { className: 'hc-wx-hour__desc', children: descText(row.desc) }),
              jsx('div', {
                className: 'hc-wx-hour__pop' + (pop === '—' ? ' hc-wx-hour__pop--none' : ''),
                title: '降水概率',
                children: pop === '—' ? '降水 —' : '降水 ' + pop
              })
            ]
          },
          'h' + index
        )
      })
    })
  }

  return jsx(Block, {
    title: '逐小时',
    note: (rows.length ? '未来 ' + rows.length + ' 小时' : '') +
      (rows.length ? ' · ' : '') + sourceNote(body),
    children: children
  })
}

// ---------------------------------------------------------------- 多日
function DailyBlock(props) {
  var query = props.query
  var body = query.data || {}
  var u = unitsOf(body)
  var tempUnit = u.temp || '℃'
  var rows = body.daily || []
  var children

  if (query.status === 'loading') {
    children = jsx(Skeleton, { lines: 3, label: '正在读取多日预报' })
  } else if (query.status === 'error') {
    children = jsx(ErrorState, {
      title: '多日预报没读到',
      message: query.error,
      onRetry: query.reload
    })
  } else if (!rows.length) {
    children = jsx(Empty, {
      title: '多日预报没有数据',
      hint: '后端返回了响应但没有逐日数组。这个地点的日级预报可能不可用。',
      action: { label: '重试', onClick: query.reload }
    })
  } else {
    children = jsx('div', {
      className: 'hc-wx-days',
      role: 'list',
      children: rows.map(function (row, index) {
        var label = dayLabel(row.date)
        var lo = dec(row.min)
        var hi = dec(row.max)
        var range = lo === '—' && hi === '—' ? '—' : lo + ' ~ ' + hi + tempUnit
        var pop = popText(row.pop)
        return jsxs(
          'div',
          {
            className: 'hc-wx-day',
            role: 'listitem',
            title: row.date + ' · ' + descText(row.text) + ' · 降水概率 ' + pop,
            children: [
              jsxs('span', {
                className: 'hc-wx-day__date',
                children: [
                  jsx('span', { children: label.md }),
                  jsx('span', { className: 'hc-wx-day__wd', children: label.wd })
                ]
              }),
              jsx('span', { className: 'hc-wx-day__desc', children: descText(row.text) }),
              jsx('span', { className: 'hc-wx-day__range', children: range }),
              jsx('span', { className: 'hc-wx-day__pop', children: pop === '—' ? '降水 —' : '降水 ' + pop })
            ]
          },
          'd' + index
        )
      })
    })
  }

  return jsx(Block, {
    title: '多日',
    note: (rows.length ? '共 ' + rows.length + ' 天' : '') +
      (rows.length ? ' · ' : '') + sourceNote(body),
    children: children
  })
}

// ---------------------------------------------------------------- 地点输入
/**
 * 地点工具栏：默认用后端配置的地点，输入框可临时查别的城市/坐标。
 *
 * 为什么要有"复位"：临时查完北京之后，面板不能就这么停在别人家的天气上 —— 一键回到配置地点。
 * 为什么按按钮才生效：边打字边打上游会把请求打爆，而且半截城市名必然查不到。
 */
function PlaceToolbar(props) {
  return jsxs('div', {
    className: 'hc-wx-toolbar',
    children: [
      jsx('span', { className: 'hc-wx-toolbar__label', children: '地点' }),
      jsx('input', {
        className: 'hc-wx-input',
        type: 'text',
        value: props.value,
        placeholder: props.placeholder,
        title: '城市名（如 合肥 / 北京）或「纬度,经度」；留空 = 后端配置的默认地点',
        onChange: function (event) {
          props.onChange(event.target.value)
        },
        onKeyDown: function (event) {
          if (event.key === 'Enter') props.onApply()
        }
      }),
      jsx('button', {
        type: 'button',
        className: 'hc-btn',
        title: '用这个地点重新取一次（不影响默认地点）',
        onClick: props.onApply,
        children: '查询'
      }),
      jsx('button', {
        type: 'button',
        className: 'hc-btn',
        disabled: !props.canReset,
        title: '回到后端配置的默认地点',
        onClick: props.onReset,
        children: '复位'
      }),
      jsx('span', {
        className: 'hc-wx-toolbar__label',
        title: '当前实际查询的地点（后端解析结果）',
        children: '当前：' + (props.current || '—')
      })
    ]
  })
}

// ---------------------------------------------------------------- N 小时后
/**
 * 「N 小时后」：一个 1/6/12/24/48h 的分段选择，结果内联显示。
 *
 * 文案**照抄后端**（后端照抄 weather_lib 的原话），前端不拼句子、不换算温度 ——
 * 一旦前端开始"加工"，就会出现「晴转多云」这种谁也没说过的预报。
 */
function AtBlock(props) {
  var hours = props.hours
  var query = props.query
  var body = query.data || {}
  var children

  if (query.status === 'loading') {
    children = jsx(Skeleton, { lines: 2, label: '正在读取「N 小时后」' })
  } else if (query.status === 'error') {
    children = jsx(ErrorState, {
      title: hours + ' 小时后没查到',
      message: query.error,
      onRetry: query.reload
    })
  } else if (!body.text) {
    children = jsx(Empty, {
      title: hours + ' 小时后没有数据',
      hint: '后端返回了响应但没有 text 字段。换个时段（1/6/12/24/48 小时）再试。',
      action: { label: '重试', onClick: query.reload }
    })
  } else {
    children = jsxs('div', {
      className: 'hc-wx-at',
      children: [
        jsx('div', { className: 'hc-wx-at__text', children: String(body.text) }),
        jsx('div', {
          className: 'hc-wx-at__stamp',
          children: '来源 ' + (body.source_label || SOURCE_LABELS[body.source] || '—') +
            ' · 更新 ' + absDate(body.fetched_at) + '（' + relTime(body.fetched_at) + '）'
        })
      ]
    })
  }

  return jsxs('section', {
    className: 'hc-wx-block',
    children: [
      jsxs('div', {
        className: 'hc-wx-block__head',
        children: [
          jsx('h3', { className: 'hc-wx-block__title', children: 'N 小时后' }),
          jsx('div', {
            className: 'hc-wx-seg',
            role: 'group',
            'aria-label': '选择多少小时之后',
            children: AT_OPTIONS.map(function (option) {
              var on = option === hours
              return jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-wx-seg__item' + (on ? ' hc-wx-seg__item--on' : ''),
                  'aria-pressed': on ? 'true' : 'false',
                  title: option + ' 小时后的天气',
                  onClick: function () {
                    props.onPick(option)
                  },
                  children: option + 'h'
                },
                'at' + option
              )
            })
          })
        ]
      }),
      children
    ]
  })
}

// ---------------------------------------------------------------- 分区
export function WeatherSection(props) {
  // 输入框草稿 vs 已生效的地点：只有按了"查询"才换 key，避免边打字边打上游。
  var draftState = useState('')
  var placeState = useState('')
  var place = placeState[0]
  var ready = props.ready !== false
  var agent = props.agent || ''

  var nowQuery = useQuery(
    'weather|now|' + agent + '|' + place,
    function () {
      return get('/weather/now', { agent: agent, params: { place: place } })
    },
    { enabled: ready, staleMs: 60000 }
  )
  var hourlyQuery = useQuery(
    'weather|hourly|' + place,
    function () {
      return get('/weather/hourly', { agent: agent, params: { place: place, hours: HOURLY_HOURS } })
    },
    { enabled: ready, staleMs: 300000 }
  )
  var dailyQuery = useQuery(
    'weather|daily|' + place,
    function () {
      return get('/weather/daily', { agent: agent, params: { place: place, days: DAILY_DAYS } })
    },
    { enabled: ready, staleMs: 600000 }
  )
  var atState = useState(AT_DEFAULT)
  var atHours = atState[0]
  var atQuery = useQuery(
    'weather|at|' + place + '|' + atHours,
    function () {
      return get('/weather/at', { agent: agent, params: { place: place, hours: atHours } })
    },
    { enabled: ready, staleMs: 300000 }
  )

  function applyPlace() {
    placeState[1](String(draftState[0] || '').trim())
  }

  function resetPlace() {
    draftState[1]('')
    placeState[1]('')
  }

  var body = nowQuery.data || null
  var head

  if (nowQuery.status === 'loading') {
    head = jsx(Skeleton, { cards: 2, lines: 3, label: '正在读取天气' })
  } else if (nowQuery.status === 'error') {
    head = jsx(ErrorState, {
      title: '天气数据没读到',
      message: nowQuery.error,
      detail: '后端把三个源各自的结果都写在原因里（✓ 命中 / ×空 返回空），据此能看出降级到了哪一源',
      onRetry: nowQuery.reload
    })
  } else if (!body || !body.realtime) {
    head = jsx(Empty, {
      title: '这个地点没有实况数据',
      hint:
        '后端返回了响应，但没有实况字段（realtime）。可能这个城市解析不到、或所有源都只给了预报。\n' +
        '换个城市名或直接给「纬度,经度」再试一次。',
      action: { label: '重新取一次', onClick: nowQuery.reload }
    })
  } else {
    head = jsx(CurrentCard, { body: body })
  }

  // 四块各自独立三态：实况坏了不影响逐时/逐日/「N 小时后」，反之亦然（拿不到就是 — / 空态，不猜）。
  return jsxs('div', {
    className: 'hc-wx',
    children: [
      jsx(PlaceToolbar, {
        value: draftState[0],
        placeholder: '城市名或「纬度,经度」（留空 = 配置的默认地点）',
        current: body ? body.place : '',
        canReset: place !== '' || draftState[0] !== '',
        onChange: function (value) {
          draftState[1](value)
        },
        onApply: applyPlace,
        onReset: resetPlace
      }, 'toolbar'),
      head,
      jsx(HourlyBlock, { query: hourlyQuery }, 'hourly'),
      jsx(DailyBlock, { query: dailyQuery }, 'daily'),
      jsx(AtBlock, {
        hours: atHours,
        query: atQuery,
        onPick: function (option) {
          atState[1](option)
        }
      }, 'at')
    ]
  })
}
