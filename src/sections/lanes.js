/**
 * hermes-console · 「执行 Lane」分区（**只读**）。
 *
 * 一屏看清 codex-worker / claude-worker 两条外部执行 lane 是否活着。数据来自
 * `GET /lanes`（后端 `dashboard/api_lanes.py`），后端读
 * `<HERMES_HOME>/cache/lane/status/<assignee>.json` 的心跳并给出 `liveness`。
 *
 * 四条硬规矩（都写进了验收）：
 *   1. **只读**：本文件不渲染任何按钮（没有启动 / 停止 / 回收）。误点会杀掉正在跑任务的
 *      supervisor —— 所以连「重试」都不放，数据每 10 秒自己刷一次。
 *   2. **状态必须有文字**：在线 / 心跳停滞 / 离线 / 未知；色点只做辅助（色盲 + 黑白打印都
 *      能读）。
 *   3. **时间说人话**：`N 秒前` / `N 分钟前`、当前卡已运行时长、`last_poll_at` 显示
 *      `HH:mm:ss`。解析不了就 `—`，不填 0。
 *   4. 三态复用 `Skeleton` / `Empty` / `ErrorState`，不各写一套。
 *
 * 颜色纪律：本文件不出现任何字面色值，全部走 `style.css` 里的 `var(--ui-*)`。
 */
import { useEffect } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

import { get } from '../lib/rest.js'
import { useQuery, refresh } from '../lib/query.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 自动刷新间隔（毫秒）：心跳窗口最小 90 秒，10 秒足够及时。 */
var POLL_MS = 10000

/** liveness slug → 中文文字（文字是主通道，色点只是辅助）。 */
var LIVENESS_TEXT = {
  online: '在线',
  stalled: '心跳停滞',
  offline: '离线',
  unknown: '未知'
}

/** state slug → 中文。后端原话不认识时显示原值。 */
var STATE_TEXT = {
  idle: '空闲',
  claimed: '已领取',
  running: '运行中'
}

/** last_result.action → 中文。 */
var ACTION_TEXT = {
  complete: '完成',
  block: '阻塞',
  skipped: '跳过'
}

// ---------------------------------------------------------------- 格式化
function pad2(n) {
  return (n < 10 ? '0' : '') + n
}

/** 秒级 epoch → `HH:mm:ss`。拿不到 → '—'（不猜当前时间）。 */
function clock(value) {
  if (typeof value !== 'number' || !isFinite(value)) return '—'
  var d = new Date(value * 1000)
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds())
}

/** 秒差 → `N 秒前` / `N 分钟前` / `N 小时前`。拿不到 → '—'。 */
function agoText(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return Math.round(seconds) + ' 秒前'
  if (seconds < 3600) return Math.round(seconds / 60) + ' 分钟前'
  if (seconds < 86400) return Math.round(seconds / 3600) + ' 小时前'
  return Math.round(seconds / 86400) + ' 天前'
}

/** 已运行时长（秒）→ `N 秒` / `N 分钟` / `N 小时 M 分`。拿不到 → '—'。 */
function spanText(seconds) {
  if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return Math.round(seconds) + ' 秒'
  if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟'
  var h = Math.floor(seconds / 3600)
  var m = Math.floor((seconds % 3600) / 60)
  return h + ' 小时 ' + m + ' 分'
}

function pick(map, value) {
  if (typeof value !== 'string' || !value) return '—'
  return map[value] || value
}

/** 一行「标签：值」。值是空也显示 —（空白会让人以为没有这项）。 */
function Row(props) {
  return jsxs('div', {
    className: 'hc-lane__row',
    children: [
      jsx('span', { className: 'hc-lane__key', children: props.label }),
      jsx('span', { className: 'hc-lane__val', children: props.value })
    ]
  })
}

// ---------------------------------------------------------------- 单条 lane
function LaneCard(props) {
  var lane = props.lane || {}
  var liveness = lane.liveness || 'unknown'
  var pidAliveText =
    lane.pid_alive === true ? '存活' : lane.pid_alive === false ? '已不存在' : '无法判断'
  var task = lane.task || null
  var result = lane.last_result || null

  var taskValue = '—'
  if (task) {
    var bits = []
    if (task.title) bits.push(String(task.title))
    else bits.push('任务 #' + (task.id === null || task.id === undefined ? '—' : task.id))
    if (typeof task.running_seconds === 'number') {
      bits.push('已运行 ' + spanText(task.running_seconds))
    }
    taskValue = bits.join(' · ')
  }

  var resultValue = '—'
  if (result) {
    var rbits = [pick(ACTION_TEXT, result.action)]
    if (typeof result.at === 'number') rbits.push(clock(result.at))
    if (result.reason) rbits.push(String(result.reason))
    resultValue = rbits.join(' · ')
  }

  return jsxs('section', {
    className: 'hc-lane hc-lane--' + liveness,
    'aria-label': '执行 lane ' + (lane.assignee || '—') + '：' + pick(LIVENESS_TEXT, liveness),
    children: [
      jsxs('div', {
        className: 'hc-lane__head',
        children: [
          jsx('span', {
            className: 'hc-lane__dot',
            'aria-hidden': 'true',
            title: '颜色只是辅助，状态以文字为准'
          }),
          jsx('h3', { className: 'hc-lane__name', children: lane.assignee || '—' }),
          // 状态文字（硬要求）：不是只看色点
          jsx('span', {
            className: 'hc-lane__status',
            children: pick(LIVENESS_TEXT, liveness)
          }),
          jsx('span', { className: 'hc-chip', children: '适配器 ' + (lane.adapter || 'unknown') })
        ]
      }),
      jsxs('div', {
        className: 'hc-lane__rows',
        children: [
          jsx(Row, {
            label: '心跳',
            value:
              '最后 ' + clock(lane.last_poll_at) +
              '（' + agoText(lane.age_seconds) + '）'
          }),
          jsx(Row, {
            label: '进程',
            value:
              (lane.pid === null || lane.pid === undefined ? 'pid —' : 'pid ' + lane.pid) +
              ' · ' + pidAliveText
          }),
          jsx(Row, {
            label: '轮询',
            value:
              typeof lane.poll_interval === 'number'
                ? '每 ' + lane.poll_interval + ' 秒'
                : '—'
          }),
          jsx(Row, {
            label: '状态',
            value:
              lane.state === null || lane.state === undefined
                ? '—'
                : pick(STATE_TEXT, lane.state) + '（' + lane.state + '）'
          }),
          jsx(Row, { label: '当前卡', value: taskValue }),
          jsx(Row, { label: '上次结果', value: resultValue }),
          jsx(Row, { label: '脚本', value: lane.lane_script || '—' })
        ]
      }),
      lane.error
        ? jsx('div', { className: 'hc-lane__error', children: String(lane.error) })
        : null
    ]
  })
}

// ---------------------------------------------------------------- 分区
export function LanesSection(props) {
  var ready = props.ready !== false
  var query = useQuery(
    'lanes',
    function () {
      return get('/lanes')
    },
    { enabled: ready, staleMs: POLL_MS }
  )

  // 只读面板没有刷新按钮：定时把缓存标过期重取，心跳才跟得上。
  useEffect(
    function () {
      if (!ready) return
      var timer = setInterval(function () {
        refresh('lanes')
      }, POLL_MS)
      return function () {
        clearInterval(timer)
      }
    },
    [ready]
  )

  var payload = query.data || null
  var body = payload && payload.data ? payload.data : null
  var lanes = body && Array.isArray(body.lanes) ? body.lanes : []
  var children

  if (query.status === 'loading') {
    children = jsx(Skeleton, { cards: 2, lines: 3, label: '正在读取执行 lane 心跳' })
  } else if (query.status === 'error') {
    children = jsx(ErrorState, {
      title: '执行 lane 心跳没读到',
      message: query.error,
      detail: '后端读的是 <HERMES_HOME>/cache/lane/status/<assignee>.json；面板每 10 秒自动重取'
    })
  } else if (!body || !lanes.length) {
    children = jsx(Empty, {
      title: '还没有执行 lane 的心跳',
      hint:
        '后端返回了响应，但 lanes 为空。可能是 supervisor 还没起过、或心跳目录里还没有文件。\n' +
        '心跳文件出现后面板会在 10 秒内自动显示。'
    })
  } else {
    children = jsx('div', {
      className: 'hc-lanes',
      role: 'list',
      children: lanes.map(function (lane, index) {
        return jsx(
          'div',
          { role: 'listitem', children: jsx(LaneCard, { lane: lane }) },
          'lane-' + (lane && lane.assignee ? lane.assignee : index)
        )
      })
    })
  }

  return jsxs('div', {
    className: 'hc-lane-root',
    children: [
      jsxs('div', {
        className: 'hc-pane__head',
        children: [
          jsx('h2', { className: 'hc-pane__title', children: '执行 Lane' }),
          jsx('span', {
            className: 'hc-chip',
            title: '本面板只读：不提供启动 / 停止 / 回收，避免误杀正在跑任务的 supervisor',
            children: '只读'
          }),
          body && body.status_dir
            ? jsx('span', {
                className: 'hc-muted',
                title: '心跳目录（后端只读）',
                children: body.status_dir
              })
            : null
        ]
      }),
      children
    ]
  })
}
