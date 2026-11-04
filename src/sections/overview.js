/**
 * hermes-console · 概览分区（C3：home-dashboard 的 6 个只读路由 + 看板挂件）。
 *
 * 数据来源全部是本插件自己的后端（`/api/plugins/hermes-console/overview/*`），
 * 每个区块各发一条请求、各自进三态 —— 一个数据源坏掉不会连累另外三块，
 * 这正是旧 Home 面板做不到的事（它一次性拉全量，一处失败整页空白）。
 *
 * 四条规矩：
 *   1. **数字必须带口径**。每个统计卡下面那行小字就是"这个数怎么算出来的"，
 *      拿不到就显示 `—` 并说明原因，绝不填 0 冒充真实值；
 *   2. **只读**。cron / 会话 / 看板布局都只展示，不做编辑（拖拽编辑留给后续卡片）；
 *   3. **agent 作用域要透明**。后端的 `scope.kind` 直接渲染成一行说明
 *      （按 agent 取 / 宿主级 / 单一文件），不替后端圆场；
 *   4. 颜色只走 `var(--ui-*)`（本文件不出现任何字面色值，构建脚本会检查）；
 *   5. **每张卡都要自报状态**。13 张挂件卡右上角那枚角标（有数据 / 空态 / 占位 /
 *      取数失败）就是"这一项到底做完了没有"的界面答案 —— 不给悬空卡片，
 *      也不拿 `ok=true` 的后端响应替界面圆场；
 *   6. **能交互的就真交互**。token 卡的 7/30/90 天是真的换 `days=` 重发请求；
 *      每个区块右上角、页面顶部那两个「刷新」是真的重取；仍然只读的部分
 *      （拖拽排版、便签/倒计时编辑、cron 增删）在卡上写明"只读 + 去哪改"。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { cloneElement, useEffect, useState } from 'react'

import { absDate, bytes, clip, compact, duration, num, relTime } from '../lib/format.js'
import { useQuery } from '../lib/query.js'
import { get } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** token 统计窗口（天）。与后端 `days` 参数一致。缺省 30 天。 */
var TOKEN_WINDOW_DAYS = 30

/** token 卡可切的窗口档位（天）。后端 `/overview/analytics` 接受 1..365；
    这里只给三档常用的 —— 切一次真的重发一条 `days=N` 的请求，
    不是前端拿 30 天的数字自己换算（那会变成假数字）。 */
var TOKEN_WINDOWS = [7, 30, 90]

/** analytics 的取数上限。实测 2.4–4.2s，给 12s：足够覆盖慢盘 + 冷缓存，
    又不至于让卡片像挂死。超时后走 ErrorState（原因写「取数超时」）。 */
var ANALYTICS_TIMEOUT_MS = 12000

/** 会话列表每页条数。 */
var SESSION_PAGE_SIZE = 20

/** errors 挂件的日志尾窗口行数（与服务端 `lines` 参数一致）。100 行够看出最近有没有出事，
    又不至于把 30KB 日志塞进一次响应。 */
var LOG_WINDOW_LINES = 100

/** 本地时钟的重算间隔（毫秒）。1 秒：时钟卡要显示到秒，日历/月相顺带每分钟重算。 */
var CLOCK_TICK_MS = 1000

/** 占位符（不是 0，不是空字符串）：取不到就说取不到。 */
var DASH = '—'

/** 统计卡骨架屏：占住「标签 / 大数值 / 构成 / 口径」的形状，不写「读取中」。
    `extra` 是卡片自带的控件（token 卡的窗口切换）：读数据时它必须**还在**，
    否则用户点一下 90 天，按钮先消失再出现，等于没法连续切。 */
function StatSkeleton(props) {
  var parts = [jsx('div', { className: 'hc-skel-card__bar hc-skel-card__bar--label' }, 'label')]
  if (props.extra) parts.push(jsx('div', { children: props.extra }, 'extra'))
  parts.push(jsx('div', { className: 'hc-skel-card__bar hc-skel-card__bar--value' }, 'value'))
  parts.push(jsx('div', { className: 'hc-skel-card__bar hc-skel-card__bar--sub' }, 'sub'))
  parts.push(jsx('div', { className: 'hc-skel-card__bar hc-skel-card__bar--caliber' }, 'caliber'))
  return jsx('div', {
    className: 'hc-skel-card',
    role: 'status',
    'aria-busy': 'true',
    'aria-label': (props.label || '读数') + '正在读取',
    children: parts
  })
}

/**
 * 统计卡错误态：原因原样给出（复用 ErrorState 组件，带重试），外面套统计卡的框，
 * 这样四张卡仍然在同一个栅格里，坏掉的那张不会把布局顶歪。
 */
function StatErrorCard(props) {
  return jsxs('div', {
    className: 'hc-card hc-card--error',
    children: [
      jsx('div', { className: 'hc-card__label', children: props.label }, 'label'),
      props.extra ? jsx('div', { children: props.extra }, 'extra') : null,
      jsx(ErrorState, {
        title: props.title || '没读到',
        message: props.message || '原因未知',
        onRetry: props.onRetry,
        retryLabel: '重试'
      })
    ]
  })
}

/**
 * 把一条 useQuery 结果翻译成"能渲染的东西"。
 *
 * `rest.js` 已经把 HTTP 失败和 `{ok:false}` 都归一成 `{ok:false, message}`，
 * 所以这里只处理三种状态 + 一个双保险分支（老后端可能 resolve 出 `ok:false`）。
 * @param {{status: string, data: any, error: string}} query
 */
function readQuery(query) {
  if (query.status === 'loading') return { state: 'loading' }
  if (query.status === 'error') return { state: 'error', error: query.error, reload: query.reload }
  var body = query.data || {}
  if (body.ok === false) {
    var nested = body.error || {}
    return {
      state: 'error',
      error: nested.message || nested.code || '数据源没读到',
      reload: query.reload
    }
  }
  return {
    state: 'ok',
    data: body.data,
    scope: body.scope || null,
    calibers: body.calibers || {},
    counts: body.counts || null,
    reload: query.reload
  }
}

/** 后端口径表取值（缺了就是空字符串，不编造）。 */
function caliber(calibers, key) {
  return (calibers && calibers[key]) || ''
}

/** 作用域说明行：把后端 `scope.kind` 直说，不替它圆场。 */
function scopeText(scope) {
  if (!scope || !scope.kind) return ''
  var label
  if (scope.kind === 'profile') label = '按 agent 取数'
  else if (scope.kind === 'all') label = '跨全部 profile 聚合'
  else if (scope.kind === 'current_profile') label = '作用域未定，取当前 profile'
  else if (scope.kind === 'host') label = '宿主级指标，与 agent 无关'
  else if (scope.kind === 'shared_file') label = '单一数据文件，与 agent 无关'
  else label = scope.kind
  return label + (scope.agent ? '（' + scope.agent + '）' : '')
}

/** 一个统计卡：标题 + （可选控件）+ 主数值 + 单位 + 构成 + 口径（三档字号的层级）。 */
function StatCard(props) {
  var parts = [jsx('div', { className: 'hc-card__label', children: props.label }, 'label')]
  // extra：卡片自带的小控件（窗口切换 / 开关）。放在标签下面、数值上面，
  // 位置固定 —— 切窗口时数值区换成骨架，控件不会跟着跳。
  if (props.extra) parts.push(jsx('div', { children: props.extra }, 'extra'))
  parts.push(
    jsxs(
      'div',
      {
        className: 'hc-card__value',
        children: [
          jsx('span', { className: 'hc-card__num', children: props.value }, 'num'),
          props.unit ? jsx('span', { className: 'hc-card__unit', children: props.unit }, 'unit') : null
        ]
      },
      'value'
    )
  )
  parts.push(jsx('div', { className: 'hc-card__sub', children: props.sub || '' }, 'sub'))
  // sub2/sub3：补充行（宿主构成 / 进程与版本）。单独成行，不和主口径挤在一起。
  if (props.sub2) parts.push(jsx('div', { className: 'hc-card__sub', children: props.sub2 }, 'sub2'))
  if (props.sub3) parts.push(jsx('div', { className: 'hc-card__sub', children: props.sub3 }, 'sub3'))
  parts.push(
    jsx(
      'div',
      {
        className: 'hc-card__caliber',
        title: props.detail || '',
        children: '口径：' + (props.caliber || DASH)
      },
      'caliber'
    )
  )
  return jsx('div', {
    className: 'hc-card' + (props.tone ? ' hc-card--' + props.tone : ''),
    children: parts
  })
}

/** 区块外壳（标题 + 一句说明 + 内容）。`action` 是标题右侧的小控件（刷新按钮）。 */
function Block(props) {
  var head = [
    jsx('span', { className: 'hc-block__title', children: props.title }, 'title'),
    props.hint ? jsx('span', { className: 'hc-muted', children: props.hint }, 'hint') : null
  ]
  if (props.action) {
    head.push(jsx('div', { style: { marginLeft: 'auto' }, children: props.action }, 'action'))
  }
  return jsxs('section', {
    className: 'hc-block',
    children: [jsx('div', { className: 'hc-block__head', children: head }, 'head'), props.children]
  })
}

/** 系统卡：内存占用 + CPU + 开机时长（全部宿主级，与 agent 无关）。 */
function systemCard(view) {
  if (view.state === 'loading') {
    return jsx(StatSkeleton, { label: '系统状态' })
  }
  if (view.state === 'error') {
    return jsx(StatErrorCard, {
      label: '系统状态',
      title: '宿主读数没拿到',
      message: view.error,
      onRetry: view.reload
    })
  }
  var data = view.data || {}
  var memory = data.memory || null
  var disk = data.disk || null
  var hasMemory = memory && typeof memory.percent === 'number'
  var parts = []
  if (typeof data.cpu_percent === 'number') parts.push('CPU ' + Math.round(data.cpu_percent) + '%')
  if (disk && typeof disk.percent === 'number') parts.push('磁盘 ' + Math.round(disk.percent) + '%')
  if (typeof data.uptime_seconds === 'number') parts.push('开机 ' + duration(data.uptime_seconds))
  if (typeof data.cpu_count === 'number') parts.push(data.cpu_count + ' 核')
  var memoryLine = hasMemory
    ? '已用 ' + bytes(memory.used) + ' / 共 ' + bytes(memory.total)
    : '宿主未提供内存读数（psutil 不可用）'
  // 后端 `get_system_stats` 还给了负载 / 本进程 RSS / 版本号，旧版概览全丢了 ——
  // 口径表（SYSTEM_CALIBERS）里明明写着 load_avg / process.rss / hermes_version，
  // 界面上却一个都没有。这里按真实字段补上；取不到就写"宿主未提供"，不填 0。
  var load = Array.isArray(data.load_avg) ? data.load_avg : null
  var proc = data.process && typeof data.process === 'object' ? data.process : null
  var extra = []
  if (load && load.length) {
    extra.push(
      '负载 ' +
        load
          .map(function (v) {
            return typeof v === 'number' ? (Math.round(v * 100) / 100).toFixed(2) : DASH
          })
          .join(' / ') +
        '（1/5/15 分）'
    )
  }
  if (proc && typeof proc.rss === 'number') extra.push('本进程 RSS ' + bytes(proc.rss))
  if (proc && typeof proc.num_threads === 'number') extra.push(proc.num_threads + ' 线程')
  if (typeof data.hermes_version === 'string' && data.hermes_version) {
    extra.push('Hermes v' + data.hermes_version)
  }
  var sub3 = extra.length
    ? extra.join(' · ')
    : '宿主未提供负载 / 进程 / 版本读数（psutil 与版本接口都不可用）'
  var detail = [
    caliber(view.calibers, 'memory.percent'),
    caliber(view.calibers, 'cpu_percent'),
    caliber(view.calibers, 'load_avg'),
    caliber(view.calibers, 'process.rss'),
    caliber(view.calibers, 'hermes_version')
  ]
    .filter(function (line) {
      return !!line
    })
    .join('\n')
  return jsx(StatCard, {
    label: '系统状态',
    value: hasMemory ? Math.round(memory.percent) + '%' : DASH,
    unit: hasMemory ? '内存占用' : '',
    sub: memoryLine,
    sub2: parts.join(' · '),
    sub3: sub3,
    caliber: '宿主整机内存占用率（不是 Hermes 进程内存）',
    detail: detail,
    tone: hasMemory ? '' : 'warn'
  })
}

/**
 * 窗口切换控件（token 卡的 7 / 30 / 90 天）。
 *
 * 复用天气分区的分段控件样式（`.hc-wx-seg`）—— 本卡约束只允许改 overview.js，
 * 不能往 style.css 里加新类，而这一段本来就是"分段单选"的形状。
 * 点一下是真的换 `days=` 重发请求（后端 `/overview/analytics` 支持 1..365），
 * 不是把 30 天的数按比例折算成 7 天。
 */
function windowControl(windowDays, onWindow) {
  return jsx('div', {
    className: 'hc-wx-seg',
    role: 'group',
    'aria-label': 'token 统计窗口',
    children: TOKEN_WINDOWS.map(function (days) {
      var on = days === windowDays
      return jsx(
        'button',
        {
          type: 'button',
          className: 'hc-wx-seg__item' + (on ? ' hc-wx-seg__item--on' : ''),
          'aria-pressed': on ? 'true' : 'false',
          title: '按近 ' + days + ' 天窗口重新取数（真的重发 days=' + days + '）',
          onClick: function () {
            onWindow(days)
          },
          children: days + ' 天'
        },
        'w' + days
      )
    })
  })
}

/** token / 调用量卡。缓存读入单列，不并入总量（否则会把 1.2 千万说成 46 亿）。 */
function tokenCard(view, windowDays, onWindow) {
  var ctl = windowControl(windowDays, onWindow)
  if (view.state === 'loading') {
    return jsx(StatSkeleton, { label: 'token / 调用量', extra: ctl })
  }
  if (view.state === 'error') {
    return jsx(StatErrorCard, {
      label: 'token / 调用量',
      title: '用量统计没读到',
      message: view.error,
      onRetry: view.reload,
      extra: ctl
    })
  }
  var totals = (view.data || {}).totals || {}
  var input = typeof totals.total_input === 'number' ? totals.total_input : null
  var output = typeof totals.total_output === 'number' ? totals.total_output : null
  var total = input !== null && output !== null ? input + output : null
  var bits = []
  if (input !== null) bits.push('输入 ' + num(input))
  if (output !== null) bits.push('输出 ' + num(output))
  if (typeof totals.total_api_calls === 'number') bits.push('调用 ' + num(totals.total_api_calls) + ' 次')
  if (typeof totals.total_cache_read === 'number') {
    bits.push('缓存读入 ' + num(totals.total_cache_read) + '（单列，未并入）')
  }
  // 成本与会话数：后端 totals 里一直有，旧版概览没展示。成本必须写明"估算"。
  var extraBits = []
  if (typeof totals.total_estimated_cost === 'number') {
    var cost = totals.total_estimated_cost
    // 0 不能读成"免费"：后端是 COALESCE(SUM(estimated_cost_usd), 0)，全为 NULL 也会是 0。
    extraBits.push(
      cost > 0
        ? '估算成本 $' + cost.toFixed(2) + '（估算值，不是账单实付）'
        : '估算成本 $0.00（宿主这一列合计为 0：本地/免费模型，或没有记录成本）'
    )
  }
  if (typeof totals.total_sessions === 'number') {
    extraBits.push('窗口内开始的会话 ' + num(totals.total_sessions) + ' 个')
  }
  return jsx(StatCard, {
    label: 'token / 调用量',
    value: total === null ? DASH : compact(total),
    unit: total === null ? '' : 'token',
    extra: ctl,
    sub: bits.length ? bits.join(' · ') : '该窗口内没有任何用量记录',
    sub2: extraBits.length ? extraBits.join(' · ') : '',
    caliber: '近 ' + windowDays + ' 天窗口内开始的会话，输入+输出 token 之和',
    detail: [
      caliber(view.calibers, 'totals.total_input'),
      caliber(view.calibers, 'totals.total_estimated_cost')
    ]
      .filter(function (line) {
        return !!line
      })
      .join('\n'),
    tone: total === null ? 'warn' : ''
  })
}

/** cron 任务数卡。 */
function cronCard(view) {
  if (view.state === 'loading') {
    return jsx(StatSkeleton, { label: 'cron 任务数' })
  }
  if (view.state === 'error') {
    return jsx(StatErrorCard, {
      label: 'cron 任务数',
      title: '定时任务没读到',
      message: view.error,
      onRetry: view.reload
    })
  }
  var counts = view.counts || {}
  var total = typeof counts.total === 'number' ? counts.total : null
  return jsx(StatCard, {
    label: 'cron 任务数',
    value: total === null ? DASH : num(counts.enabled),
    unit: total === null ? '' : '/ ' + num(total) + ' 启用',
    sub:
      '启用 ' + num(counts.enabled) + ' · 暂停 ' + num(counts.paused) + ' · 共 ' +
      num(counts.total) + ' 个（含已完成的一次性任务）',
    caliber: '该 profile 全部任务；「启用」= enabled 且未暂停',
    detail: caliber(view.calibers, 'counts.enabled'),
    tone: total === null ? 'warn' : ''
  })
}

/** 会话数卡。`statusView` 是宿主 status 查询态：只用来补一行"宿主此刻活跃会话数"
    （另一个口径，与库里的总数不是一回事，所以单独一行并写明）。 */
function sessionsCard(view, statusView) {
  if (view.state === 'loading') {
    return jsx(StatSkeleton, { label: '会话数' })
  }
  if (view.state === 'error') {
    return jsx(StatErrorCard, {
      label: '会话数',
      title: '会话数没读到',
      message: view.error,
      onRetry: view.reload
    })
  }
  var data = view.data || {}
  var rows = data.sessions || []
  var newest = rows.length ? rows[0].last_active || rows[0].started_at : null
  var storage = data.storage || {}
  var corrupt = Object.keys(storage).length > 0
  var sub
  if (corrupt) sub = '存储报告异常（' + Object.keys(storage).join(',') + '），数字可能不准'
  else if (newest) sub = '最近活动 ' + relTime(newest)
  else sub = '该 profile 还没有会话'
  var hostActive = null
  if (
    statusView &&
    statusView.state === 'ok' &&
    statusView.data &&
    typeof statusView.data.active_sessions === 'number'
  ) {
    hostActive = statusView.data.active_sessions
  }
  var sub2 =
    hostActive === null
      ? '宿主活跃会话数没读到（status 数据源未就绪）'
      : '宿主此刻活跃会话 ' + num(hostActive) + ' 个（另一个口径：宿主实时统计，不是库里总数）'
  return jsx(StatCard, {
    label: '会话数',
    value: typeof data.total === 'number' ? num(data.total) : DASH,
    unit: typeof data.total === 'number' ? '个' : '',
    sub: sub,
    sub2: sub2,
    caliber: '会话总数（不含子会话与压缩链子节点）',
    detail: [
      caliber(view.calibers, 'total'),
      'active_sessions 来自宿主 status（/overview/status），是宿主实时统计的活跃会话，与 state.db 总数不同口径'
    ]
      .filter(function (line) {
        return !!line
      })
      .join('\n'),
    tone: corrupt ? 'warn' : ''
  })
}

/**
 * 挂件清单：13 个挂件位 → 13 张「有真数据 / 有明确空态 / 明确标注占位」的卡。
 *
 * 为什么重写（C3 的老实现在这里是半成品）：
 *   旧版把 widgets 数组按 id 聚合成「挂件名 + 出现次数 + gx,gy w×h」。可这份布局里
 *   13 个挂件**每个都只出现 1 次**，于是 13 张卡全是「1 个」，页面外再看只剩
 *   「clock×1 gateway×1 …」——名字是英文 id、数字是恒等于 1 的计数、位置是给拖拽用的
 *   坐标，等于**没有任何信息**。老板说的「概览好像还有工作没做完」就是这一块。
 *
 * 现在的规矩（每张卡必须落进三档之一，不许悬空）：
 *   1. **有数据**：能从本插件后端真取到就显示数值 + 构成 + 口径（例：gateway 读
 *      /overview/status，errors 读 /overview/logs，host 读 /overview/system）；
 *   2. **空态**：数据源明确为空（便签没写、倒计时没设目标日期、errors.log 不存在）→
 *      说清「为什么空」+「在哪能填」，不填 0 冒充；
 *   3. **占位**：这个挂件本来就产不出指标（康威生命游戏画布、纯装饰的 ECG 波形）→
 *      卡上打「占位」角标 + 一句它是什么、为什么没有数字。
 *
 * 位置（gx,gy,w×h）不删，但降级成卡片脚注 —— 它是拖拽用的坐标，不是给人读的读数。
 */

/** 13 个挂件的中文名与数据来源。表里没有的 id 一律按「未登记挂件」渲染（不猜语义）。 */
var WIDGET_META = {
  clock: { label: '时钟', source: '本机时钟', kind: 'clock' },
  calendar: { label: '日历', source: '本机日期', kind: 'calendar' },
  moon: { label: '月相', source: '朔望月周期推算', kind: 'moon' },
  gateway: { label: '网关', source: '/overview/status', kind: 'gateway' },
  heartbeat: { label: '心跳', source: '/overview/status', kind: 'heartbeat' },
  host: { label: '宿主', source: '/overview/system', kind: 'host' },
  tokens: { label: 'token 用量', source: '/overview/analytics', kind: 'tokens' },
  sessions: { label: '会话', source: '/overview/sessions', kind: 'sessions' },
  cron: { label: '定时任务', source: '/overview/cron', kind: 'cron' },
  errors: { label: '日志错误', source: '/overview/logs', kind: 'errors' },
  notes: { label: '便签', source: 'layout.json 的 props.notes', kind: 'notes' },
  countdown: { label: '倒计时', source: 'layout.json 的 props.target', kind: 'countdown' },
  life: { label: '生命游戏', source: '无（装饰画布）', kind: 'life' }
}

/** 朔望月长度（天）与参考新月时刻 —— 与旧 Home 的 MoonWidget 同一套常数。 */
var SYNODIC_DAYS = 29.53058867
var NEW_MOON_MS = Date.UTC(2000, 0, 6, 18, 14)
var DAY_MS = 86400000
var MOON_PHASES = ['新月', '娥眉月', '上弦月', '盈凸月', '满月', '亏凸月', '下弦月', '残月']
var WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

function pad2(n) {
  return (n < 10 ? '0' : '') + n
}

/** 0..1 的月相位置（0=新月，0.5=满月）。纯数学，不联网。 */
function moonPhase(now) {
  var days = (now - NEW_MOON_MS) / DAY_MS
  return (((days % SYNODIC_DAYS) + SYNODIC_DAYS) % SYNODIC_DAYS) / SYNODIC_DAYS
}

/** 当月天数（按本机日期）。 */
function daysInMonth(year, month) {
  return new Date(year, month + 1, 0).getDate()
}

/** 从布局挂件 props 里取一个时间点（倒计时用），解析不了返回 null。 */
function readTarget(props) {
  if (!props || typeof props !== 'object') return null
  var raw = props.target !== undefined ? props.target : props.targetDate
  if (raw === undefined || raw === null || raw === '') return null
  if (typeof raw === 'number' && isFinite(raw) && raw > 0) {
    return raw < 1e12 ? Math.round(raw * 1000) : Math.round(raw)
  }
  var parsed = Date.parse(String(raw))
  return isNaN(parsed) ? null : parsed
}

/** 便签数组（旧 NotesWidget 存在 layout props 里，形如 [{id,text,done}]）。 */
function readNotes(props) {
  if (!props || !Array.isArray(props.notes)) return []
  return props.notes.filter(function (note) {
    return note && typeof note === 'object' && typeof note.text === 'string'
  })
}

/** 一张卡的形状。state: loading | error | real | empty | placeholder */
function card(id, label, fields) {
  var out = {
    id: id,
    label: label,
    state: fields.state || 'real',
    value: fields.value === undefined ? '' : fields.value,
    unit: fields.unit || '',
    lines: fields.lines || [],
    caliber: fields.caliber || '',
    badge: fields.badge || '',
    hint: fields.hint || ''
  }
  return out
}

/** 查询态 → 卡的三档（loading / error / ok+data）。数据源坏了只坏这一张。 */
function fromQuery(view) {
  if (view.state === 'loading') return { state: 'loading' }
  if (view.state === 'error') return { state: 'error', message: view.error }
  return { state: 'ok', data: view.data || {} }
}

/** 一张挂件的「真数据 / 空态 / 占位」判定。ctx 里是已取到的各域数据与当前时刻。 */
function describeWidget(group, ctx) {
  var meta = WIDGET_META[group.id] || null
  var label = meta ? meta.label : group.id
  var props = group.slots[0] && group.slots[0].props ? group.slots[0].props : {}
  var kind = meta ? meta.kind : 'unknown'
  var now = ctx.now

  if (kind === 'clock') {
    var d = new Date(now)
    return card(group.id, label, {
      value: pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds()),
      lines: [d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + WEEKDAYS[d.getDay()]],
      caliber: '本机时钟（桌面端本地时间），不来自后端'
    })
  }

  if (kind === 'calendar') {
    var today = new Date(now)
    var dim = daysInMonth(today.getFullYear(), today.getMonth())
    var left = dim - today.getDate()
    // ISO 周序：周四所在的那一周算第 1 周
    var start = new Date(today.getFullYear(), 0, 1)
    var week = Math.ceil(((today - start) / DAY_MS + start.getDay() + 1) / 7)
    return card(group.id, label, {
      value: String(today.getDate()),
      unit: '日',
      lines: [
        today.getFullYear() + ' 年 ' + (today.getMonth() + 1) + ' 月 · 第 ' + week + ' 周',
        '本月 ' + dim + ' 天，还剩 ' + left + ' 天'
      ],
      caliber: '按本机日期推算；没有节假日/农历数据（旧 Home 的日历也只有格子，没有日程）'
    })
  }

  if (kind === 'moon') {
    var phase = moonPhase(now)
    var lit = Math.round(((1 - Math.cos(2 * Math.PI * phase)) / 2) * 100)
    var idx = Math.round(phase * 8) % 8
    var toFull = (0.5 - phase + 1) % 1
    return card(group.id, label, {
      value: lit + '%',
      unit: '照亮',
      lines: [
        MOON_PHASES[idx],
        toFull < 0.001 ? '今晚就是满月' : '约 ' + Math.max(1, Math.round(toFull * SYNODIC_DAYS)) + ' 天后满月'
      ],
      caliber: '按朔望月 29.53 天周期推算（天文近似，不是天文台实测；与旧 Home 的 MoonWidget 同一套常数）'
    })
  }

  if (kind === 'gateway' || kind === 'heartbeat') {
    var st = fromQuery(ctx.status)
    if (st.state === 'loading') return card(group.id, label, { state: 'loading' })
    if (st.state === 'error') {
      return card(group.id, label, {
        state: 'error',
        hint: '宿主 status 没读到：' + st.message
      })
    }
    var running = st.data.gateway_running === true
    var platforms = Array.isArray(st.data.platforms) ? st.data.platforms : []
    var bad = platforms.filter(function (p) {
      return p && p.error
    })
    if (kind === 'gateway') {
      return card(group.id, label, {
        value: running ? '在线' : '离线',
        lines: [
          'pid ' + (st.data.gateway_pid === undefined || st.data.gateway_pid === null ? DASH : String(st.data.gateway_pid)) +
            ' · 状态 ' + (st.data.gateway_state || DASH),
          '平台 ' + platforms.length + ' 个' + (bad.length ? ' · ' + bad.length + ' 个报错（' + bad[0].name + '）' : ''),
          '版本 ' + (st.data.version || DASH)
        ],
        caliber: '宿主 status：gateway_running / pid / 状态机；网关是宿主单实例，与当前 agent 无关'
      })
    }
    var beatAge = st.data.gateway_updated_at ? relTime(st.data.gateway_updated_at) : DASH
    return card(group.id, label, {
      value: running ? beatAge : '停跳',
      unit: running ? '前' : '',
      lines: [
        running ? '网关状态文件最后一次写入' : '网关没在跑，所以没有心跳',
        st.data.gateway_exit_reason ? '退出原因 ' + clip(st.data.gateway_exit_reason, 60) : '健康探测 ' + (st.data.gateway_health_url || DASH)
      ],
      caliber: '心跳时效 = 网关状态文件最后写入时间；旧 Home 的 ECG 波形只是把 gateway_running 画成一条线，概览给数字'
    })
  }

  if (kind === 'host') {
    var sys = fromQuery(ctx.system)
    if (sys.state === 'loading') return card(group.id, label, { state: 'loading' })
    if (sys.state === 'error') return card(group.id, label, { state: 'error', hint: '宿主读数没拿到：' + sys.message })
    var memory = sys.data.memory || null
    var disk = sys.data.disk || null
    var bits = []
    if (typeof sys.data.cpu_percent === 'number') bits.push('CPU ' + Math.round(sys.data.cpu_percent) + '%')
    if (disk && typeof disk.percent === 'number') bits.push('磁盘 ' + Math.round(disk.percent) + '%')
    if (typeof sys.data.uptime_seconds === 'number') bits.push('开机 ' + duration(sys.data.uptime_seconds))
    return card(group.id, label, {
      value: memory && typeof memory.percent === 'number' ? Math.round(memory.percent) + '%' : DASH,
      unit: memory && typeof memory.percent === 'number' ? '内存' : '',
      lines: bits.length ? [bits.join(' · ')] : ['宿主没给内存/CPU 读数（psutil 不可用）'],
      caliber: '宿主整机内存占用率 + CPU 瞬时值 + 开机时长（不是 Hermes 进程内存）'
    })
  }

  if (kind === 'tokens') {
    var an = fromQuery(ctx.analytics)
    if (an.state === 'loading') return card(group.id, label, { state: 'loading' })
    if (an.state === 'error') return card(group.id, label, { state: 'error', hint: '用量统计没读到：' + an.message })
    var totals = an.data.totals || {}
    var input = typeof totals.total_input === 'number' ? totals.total_input : null
    var output = typeof totals.total_output === 'number' ? totals.total_output : null
    var total = input !== null && output !== null ? input + output : null
    var tl = []
    if (input !== null) tl.push('输入 ' + num(input))
    if (output !== null) tl.push('输出 ' + num(output))
    if (typeof totals.total_api_calls === 'number') tl.push('调用 ' + num(totals.total_api_calls) + ' 次')
    return card(group.id, label, {
      value: total === null ? DASH : compact(total),
      unit: total === null ? '' : 'token',
      lines: tl.length ? tl : ['该窗口内没有任何用量记录'],
      caliber:
        '近 ' +
        (ctx.tokenWindow || TOKEN_WINDOW_DAYS) +
        ' 天窗口内开始的会话，输入+输出 token 之和（与上面 token 卡同一条请求，窗口跟着上面那个切换走）'
    })
  }

  if (kind === 'sessions') {
    var se = fromQuery(ctx.sessions)
    if (se.state === 'loading') return card(group.id, label, { state: 'loading' })
    if (se.state === 'error') return card(group.id, label, { state: 'error', hint: '会话数没读到：' + se.message })
    var rows = se.data.sessions || []
    var newest = rows.length ? rows[0].last_active || rows[0].started_at : null
    return card(group.id, label, {
      value: typeof se.data.total === 'number' ? num(se.data.total) : DASH,
      unit: typeof se.data.total === 'number' ? '个' : '',
      lines: [newest ? '最近活动 ' + relTime(newest) : '该 profile 还没有会话'],
      caliber: '会话总数（不含子会话与压缩链子节点）；与上面会话卡同一条请求'
    })
  }

  if (kind === 'cron') {
    var cr = fromQuery(ctx.cron)
    if (cr.state === 'loading') return card(group.id, label, { state: 'loading' })
    if (cr.state === 'error') return card(group.id, label, { state: 'error', hint: '定时任务没读到：' + cr.message })
    var counts = ctx.cronCounts || {}
    var jobs = Array.isArray(cr.data) ? cr.data : []
    var nextJob = null
    jobs.forEach(function (job) {
      if (!job || !job.next_run_at) return
      if (!nextJob || String(job.next_run_at) < String(nextJob.next_run_at)) nextJob = job
    })
    return card(group.id, label, {
      value: typeof counts.enabled === 'number' ? num(counts.enabled) : DASH,
      unit: typeof counts.total === 'number' ? '/ ' + num(counts.total) + ' 启用' : '',
      lines: [nextJob ? '下一个 ' + (nextJob.name || nextJob.id) + ' · ' + absDate(nextJob.next_run_at) : '没有排期中的任务'],
      caliber: '该 profile 的 cron/jobs.json：启用 = enabled 且未暂停；与上面 cron 卡同一条请求'
    })
  }

  if (kind === 'errors') {
    var lg = fromQuery(ctx.logs)
    if (lg.state === 'loading') return card(group.id, label, { state: 'loading' })
    if (lg.state === 'error') return card(group.id, label, { state: 'error', hint: '日志没读到：' + lg.message })
    var lc = lg.data.counts || {}
    var scanned = typeof lg.data.scanned === 'number' ? lg.data.scanned : 0
    // exists 只表示「文件在不在」：后端已改成真实文件存在性（旧版拿「窗口有行」冒充
    // 「文件存在」，于是"文件在、但尾窗口 0 行"会被说成"没有文件可读"——那是假话）。
    if (lg.data.exists === false) {
      return card(group.id, label, {
        state: 'empty',
        hint:
          '宿主没有可读的 errors.log 尾窗口（WARNING 及以上才会写进去）。\n路径：' +
          (lg.data.path || DASH) + '\n不是 0 条错误，是没有文件可读。'
      })
    }
    // 文件在、但窗口 0 行：要么空文件，要么这一级别在窗口里一条都没有（两者都不等于"没有文件"）。
    if (!scanned) {
      var lv = lg.data.level || 'ALL'
      return card(group.id, label, {
        state: 'empty',
        hint:
          '日志文件在（' +
          (lg.data.path || DASH) +
          '），但尾窗口读到 0 行。' +
          (lv === 'ALL'
            ? '文件是空的，或这份日志还没写过内容。'
            : '当前按级别 ' + lv + ' 过滤，这段窗口里没有该级别的行（不代表文件是空的）。') +
          '不是 0 条错误，是没有内容可数。'
      })
    }
    // 后端没给按级别计数：这是取数口径缺失，不能当成「0 条 ERROR」。
    if (typeof lc.ERROR !== 'number') {
      return card(group.id, label, {
        state: 'empty',
        hint:
          '窗口内读到 ' +
          num(scanned) +
          ' 行，但后端没给按级别计数（counts 缺 ERROR 键）。这是口径缺失，不是「没有错误」——不填 0 冒充。'
      })
    }
    var lastErr = ''
    var linesAll = Array.isArray(lg.data.lines) ? lg.data.lines : []
    for (var i = linesAll.length - 1; i >= 0; i -= 1) {
      if (/\s(ERROR|CRITICAL)\s/.test(linesAll[i])) {
        lastErr = linesAll[i]
        break
      }
    }
    return card(group.id, label, {
      value: num(lc.ERROR || 0),
      unit: '条 ERROR',
      lines: [
        '窗口 ' +
          num(scanned) +
          ' 行（级别 ' +
          (lg.data.level || 'ALL') +
          '） · 警告 ' +
          num(lc.WARNING || 0) +
          ' · 未标注 ' +
          num(lc.unlabeled || 0),
        lastErr ? '最近：' + clip(lastErr.replace(/^\S+\s+\S+\s+/, ''), 80) : '窗口内没有 ERROR'
      ],
      caliber:
        '按当前 agent 的 profile 读它自己的 logs/errors.log，尾 ' +
        num(scanned) +
        ' 行内按级别计数（不是全量历史、不是错误率）；traceback 续行单独计'
    })
  }

  if (kind === 'notes') {
    var notes = readNotes(props)
    if (!notes.length) {
      return card(group.id, label, {
        state: 'empty',
        hint:
          '便签是空的：正文存在 layout.json 这个挂件的 props.notes 里，当前没有内容。\n写的地方是旧 Home 面板（/home）；概览这边只读。'
      })
    }
    var done = notes.filter(function (n) {
      return n.done === true
    }).length
    var preview = notes
      .filter(function (n) {
        return n.done !== true
      })
      .slice(0, 2)
      .map(function (n) {
        return '· ' + clip(n.text, 40)
      })
    return card(group.id, label, {
      value: String(notes.length),
      unit: '条',
      lines: [done + ' 条已完成'].concat(preview.length ? preview : ['没有未完成的便签']),
      caliber: '读 layout.json 里该挂件的 props.notes（旧 Home 面板写的那份，同一文件）'
    })
  }

  if (kind === 'countdown') {
    var target = readTarget(props)
    if (target === null) {
      return card(group.id, label, {
        state: 'empty',
        hint:
          '没设置目标日期：这个挂件的 props.target 是空的，所以算不出天数（不填 0 冒充）。\n在旧 Home 面板（/home）里点这个挂件设置日期。'
      })
    }
    var leftDays = Math.ceil((target - now) / DAY_MS)
    return card(group.id, label, {
      value: String(Math.abs(leftDays)),
      unit: leftDays >= 0 ? '天后' : '天前',
      lines: [absDate(target) + (leftDays >= 0 ? ' 到' : ' 已过')],
      caliber: '目标日期取自 layout.json 的 props.target；按本机时间算整天差'
    })
  }

  if (kind === 'life') {
    return card(group.id, label, {
      state: 'placeholder',
      badge: '占位',
      hint:
        '康威生命游戏画布：旧 Home 里是可交互的 canvas（暂停/播种/手绘），状态不落盘、不产生任何指标。\n概览里没有可展示的数据 —— 明确标注为占位，不编一个数字。'
    })
  }

  return card(group.id, label, {
    state: 'placeholder',
    badge: '占位',
    hint:
      '布局里有这个挂件 id，但控制台没登记它的语义（可能来自旧 Home 的其它挂件）。\n不猜它是什么，也不编数据；要用请在旧 Home 面板（/home）里看。'
  })
}

/** 13 个挂件位 → 卡片数组。按 id 聚合出现次数（同一挂件摆多格时只出一张卡）。 */
function buildWidgetCards(widgets, ctx) {
  var groups = []
  var index = {}
  widgets.forEach(function (w) {
    var id = w && w.id ? w.id : '(未命名挂件)'
    if (!index[id]) {
      index[id] = { id: id, slots: [] }
      groups.push(index[id])
    }
    index[id].slots.push(w || {})
  })
  return groups.map(function (group) {
    var described = describeWidget(group, ctx)
    described.count = group.slots.length
    described.place = group.slots
      .map(function (w) {
        return (w.gx || 0) + ',' + (w.gy || 0) + ' ' + (w.gw || 0) + '×' + (w.gh || 0)
      })
      .join(' / ')
    return described
  })
}

/** 挂件卡右上角的「逐项状态」角标：每张卡必须说清自己是哪一档，不许悬空。
    这四档就是验收表里的四类（有数据 / 空态 / 占位 / 取数失败）。 */
var WIDGET_STATE_BADGE = {
  real: '有数据',
  empty: '空态',
  placeholder: '占位',
  error: '取数失败',
  loading: '读取中'
}

/** 一张挂件卡。有数据 → 数值 + 构成 + 口径；空/占位 → 说清原因，不放裸数字。 */
function WidgetCard(props) {
  var w = props.widget
  var headChildren = [
    jsx('span', { className: 'hc-widget__label', children: w.label }, 'label'),
    jsx('span', { className: 'hc-widget__id', children: w.id }, 'id')
  ]
  var badge = w.badge || WIDGET_STATE_BADGE[w.state] || ''
  if (badge) {
    headChildren.push(jsx('span', { className: 'hc-widget__badge', children: badge }, 'badge'))
  }
  var body
  if (w.state === 'loading') {
    body = jsx('div', { className: 'hc-widget__loading', children: '读取中…' }, 'loading')
  } else if (w.state === 'real') {
    body = jsxs(
      'div',
      {
        className: 'hc-widget__body',
        children: [
          jsxs(
            'div',
            {
              className: 'hc-widget__value',
              children: [
                jsx('span', { className: 'hc-widget__num', children: w.value }, 'num'),
                w.unit ? jsx('span', { className: 'hc-widget__unit', children: w.unit }, 'unit') : null
              ]
            },
            'value'
          )
        ].concat(
          w.lines.map(function (line, i) {
            return jsx('div', { className: 'hc-widget__meta', children: line }, 'line' + i)
          })
        )
      },
      'body'
    )
  } else {
    body = jsx('div', {
      className: 'hc-widget__hint',
      children: w.hint || (w.state === 'error' ? '没读到' : '没有内容')
    }, 'hint')
  }
  var foot = []
  if (w.count > 1) foot.push(w.count + ' 个位置')
  foot.push('位置 ' + (w.place || DASH))
  return jsxs(
    'div',
    {
      className: 'hc-widget hc-widget--' + w.state,
      title: w.id + ' · ' + w.label + (w.caliber ? '\n口径：' + w.caliber : '') + '\n位置 ' + (w.place || DASH),
      children: [
        jsx('div', { className: 'hc-widget__head', children: headChildren }, 'head'),
        body,
        w.caliber
          ? jsx('div', { className: 'hc-widget__caliber', children: w.caliber }, 'caliber')
          : null,
        jsx('div', { className: 'hc-widget__foot', children: foot.join(' · ') }, 'foot')
      ]
    },
    w.id
  )
}

/** 区块右上角的「刷新」小按钮：真的重取这一个区块（query.reload），不是装饰。 */
function RefreshButton(props) {
  return jsx('button', {
    type: 'button',
    className: 'hc-btn hc-btn--tiny',
    disabled: props.busy === true,
    title: props.title || '重新取数（只重取这一个区块）',
    onClick: props.onClick,
    children: props.busy ? '刷新中…' : '刷新'
  })
}

/** 看板挂件：13 张卡（真数据 / 空态 / 占位），只读。 */
function LayoutBlock(props) {
  var view = readQuery(props.query)
  var body
  var hint = scopeText(view.scope)
  if (view.state === 'loading') {
    body = jsx(Skeleton, { cards: 4, lines: 2, label: '看板挂件正在读取' })
  } else if (view.state === 'error') {
    body = jsx(ErrorState, {
      title: '看板布局没读到',
      message: view.error,
      onRetry: props.query.reload
    })
  } else {
    var data = view.data || {}
    var layout = data.layout || null
    var widgets = (layout && layout.widgets) || []
    if (!layout || !widgets.length) {
      body = jsx(Empty, {
        title: '还没有保存过看板布局',
        hint:
          (data.reason || '布局文件里没有挂件') +
          '\n路径：' + (data.path || DASH) +
          '\n这一块只做只读展示，排版在旧 Home 面板里改。',
        action: { label: '重新读取', onClick: props.query.reload }
      })
    } else {
      var cards = buildWidgetCards(widgets, props.ctx || {})
      var real = 0
      var empty = 0
      var placeholder = 0
      var pending = 0
      cards.forEach(function (c) {
        if (c.state === 'real') real += 1
        else if (c.state === 'empty') empty += 1
        else if (c.state === 'placeholder') placeholder += 1
        else pending += 1
      })
      var parts = [widgets.length + ' 个挂件位', cards.length + ' 种']
      if (real) parts.push('有数据 ' + real)
      if (empty) parts.push('空态 ' + empty)
      if (placeholder) parts.push('占位 ' + placeholder)
      if (pending) parts.push('读取中 ' + pending)
      hint = parts.join(' · ')
      body = jsxs('div', {
        className: 'hc-layout',
        children: [
          jsx('div', {
            className: 'hc-widgets',
            children: cards.map(function (c) {
              return jsx(WidgetCard, { widget: c }, c.id)
            })
          }, 'grid'),
          jsx('div', {
            className: 'hc-muted',
            children:
              '文件 ' + (data.path || DASH) +
              (data.mtime ? ' · 保存于 ' + absDate(data.mtime) : '') +
              ' · 角标四档：有数据 / 空态 / 占位 / 取数失败' +
              ' · 每张卡都带口径（鼠标悬停看完整说明）；拖拽排版仍在旧 Home 面板（/home），概览只读'
          }, 'foot')
        ]
      })
    }
  }
  return jsx(Block, {
    title: '看板挂件',
    hint: hint,
    action: jsx(RefreshButton, {
      busy: props.query.busy,
      onClick: props.query.reload,
      title: '重新读 layout.json 与它依赖的取数（只重取本区块）'
    }),
    children: body
  })
}

/** cron 列表：列对齐（任务名 / 下次执行 / 状态 / 作用域），只读。 */
function CronBlock(props) {
  var view = readQuery(props.query)
  var body
  if (view.state === 'loading') {
    body = jsx(Skeleton, { lines: 4, label: '定时任务正在读取' })
  } else if (view.state === 'error') {
    body = jsx(ErrorState, {
      title: '定时任务没读到',
      message: view.error,
      onRetry: props.query.reload
    })
  } else {
    var jobs = view.data || []
    if (!jobs.length) {
      body = jsx(Empty, {
        title: '这个作用域没有定时任务',
        hint:
          '来源：' +
          (view.scope && view.scope.kind === 'all' ? '全部 profile 聚合' : '该 profile 的 cron/jobs.json') +
          '。\n任务用 `hermes cron` 创建；这里只读。',
        action: { label: '重新读取', onClick: props.query.reload }
      })
    } else {
      // 表头与数据行都是 .hc-rows 的直接子节点 —— 这样 `--row-cols` 与 `行+行` 的
      // 分隔线规则都能命中，不会因为夹了一层 wrapper 而失效。
      var headRow = jsx(
        'div',
        {
          className: 'hc-rows__head',
          role: 'row',
          children: [
            jsx('span', { children: '任务' }, 'h-name'),
            jsx('span', { children: '下次执行' }, 'h-next'),
            jsx('span', { children: '上次执行' }, 'h-last'),
            jsx('span', { children: '状态' }, 'h-status'),
            jsx('span', { className: 'hc-row__scope', children: '作用域' }, 'h-scope')
          ]
        },
        'head'
      )
      var dataRows = jobs.map(function (job, index) {
        var paused = !!job.paused_at
        var enabled = job.enabled === true
        var lastError = job.last_error || ''
        // 三档状态：异常（有 last_error）> 暂停/停用 > 启用
        var tone = lastError ? 'warn' : paused || !enabled ? 'off' : 'ok'
        var status = lastError ? '异常' : paused ? '已暂停' : enabled ? '启用' : '停用'
        return jsxs(
          'div',
          {
            className: 'hc-row',
            title: lastError ? '最近错误：' + lastError : job.name || '',
            children: [
              jsx(
                'span',
                { className: 'hc-row__name', children: job.name || job.id || '(未命名任务)' },
                'name'
              ),
              jsx(
                'span',
                {
                  className: 'hc-row__next',
                  children: job.next_run_at ? absDate(job.next_run_at) : '无下次执行'
                },
                'next'
              ),
              jsx(
                'span',
                {
                  className: 'hc-row__next',
                  title:
                    (job.last_status ? '上次终态 ' + job.last_status : '从未执行过') +
                    (job.last_run_at ? ' · ' + absDate(job.last_run_at) : '') +
                    (lastError ? ' · 错误：' + lastError : ''),
                  children: job.last_run_at
                    ? relTime(job.last_run_at) + (job.last_status ? ' · ' + job.last_status : '')
                    : '从未执行'
                },
                'last'
              ),
              jsx(
                'span',
                {
                  className: 'hc-status hc-status--' + tone,
                  title: lastError || (job.next_run_at ? '下次 ' + absDate(job.next_run_at) : ''),
                  children: status
                },
                'status'
              ),
              jsx('span', { className: 'hc-row__scope', children: job.profile || '' }, 'profile')
            ]
          },
          (job.id || 'job') + ':' + index
        )
      })
      body = jsx('div', {
        className: 'hc-rows hc-rows--cron',
        // 多加一列「上次执行」：列定义只能在行容器上用 CSS 变量覆盖，
        // 而本卡约束不许改 style.css，所以在这里就地覆盖（五列）。
        style: { '--row-cols': 'minmax(0, 1fr) minmax(0, 132px) minmax(0, 112px) 76px 100px' },
        children: [headRow].concat(dataRows)
      })
    }
  }
  var hint = view.state === 'ok' ? (view.data || []).length + ' 个 · 只读' : '只读'
  return jsx(Block, {
    title: '定时任务',
    hint: scopeText(view.scope) ? hint + ' · ' + scopeText(view.scope) : hint,
    action: jsx(RefreshButton, {
      busy: props.query.busy,
      onClick: props.query.reload,
      title: '重新读该 profile 的 cron/jobs.json（只重取本区块）'
    }),
    children: body
  })
}

/** 会话列表：标题 / 更新时间 / 条数，只读 + 服务端分页。 */
function SessionsBlock(props) {
  var view = readQuery(props.query)
  var body
  if (view.state === 'loading') {
    body = jsx(Skeleton, { lines: 6 })
  } else if (view.state === 'error') {
    body = jsx(ErrorState, {
      title: '会话列表没读到',
      message: view.error,
      onRetry: props.query.reload
    })
  } else {
    var data = view.data || {}
    var rows = data.sessions || []
    if (!rows.length) {
      var pageStart = data.offset || 0
      body = jsx(Empty, {
        title: pageStart > 0 ? '这一页没有内容了' : '这个作用域还没有会话',
        hint:
          pageStart > 0
            ? '从第 ' + (pageStart + 1) + ' 条开始没有数据了（会话可能被归档或清理）。\n回到第一页看最新的。'
            : '来源：该 profile 的 state.db（默认排除归档会话与子会话）。\n换个 agent 再看，或点重试。',
        action:
          pageStart > 0
            ? {
                label: '回到第一页',
                onClick: function () {
                  props.onOffset(0)
                }
              }
            : { label: '重新读取', onClick: props.query.reload }
      })
    } else {
      var total = typeof data.total === 'number' ? data.total : 0
      var offset = data.offset || 0
      var limit = data.limit || rows.length
      var from = total ? offset + 1 : 0
      var to = offset + rows.length
      var canPrev = offset > 0
      var canNext = to < total
      body = jsxs('div', {
        className: 'hc-sessions',
        children: [
          jsx('div', {
            className: 'hc-rows',
            children: rows.map(function (row, index) {
              var title = row.title || row.display_name || row.id || '(无标题)'
              return jsxs(
                'div',
                {
                  className: 'hc-row',
                  title: (row.id || '') + (row.model ? ' · ' + row.model : ''),
                  children: [
                    jsx('span', { className: 'hc-row__name', children: title }, 'title'),
                    jsx(
                      'span',
                      {
                        className: 'hc-row__meta',
                        children: '更新 ' + relTime(row.last_active || row.started_at)
                      },
                      'when'
                    ),
                    jsx(
                      'span',
                      {
                        className: 'hc-row__meta hc-row__meta--tail',
                        children:
                          (typeof row.message_count === 'number' ? num(row.message_count) : DASH) + ' 条'
                      },
                      'count'
                    ),
                    jsx(
                      'span',
                      { className: 'hc-row__meta hc-row__meta--tail', children: row.profile || '' },
                      'profile'
                    )
                  ]
                },
                (row.id || 's') + ':' + index
              )
            })
          }, 'rows'),
          jsxs('div', {
            className: 'hc-pager',
            children: [
              jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-btn',
                  disabled: !canPrev,
                  onClick: function () {
                    props.onOffset(Math.max(0, offset - SESSION_PAGE_SIZE))
                  },
                  children: '上一页'
                },
                'prev'
              ),
              jsx(
                'span',
                {
                  className: 'hc-muted',
                  'aria-live': 'polite',
                  children: '第 ' + from + '–' + to + ' 条 / 共 ' + num(total) + ' 条'
                },
                'info'
              ),
              jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-btn',
                  disabled: !canNext,
                  onClick: function () {
                    props.onOffset(offset + limit)
                  },
                  children: '下一页'
                },
                'next'
              )
            ]
          }, 'pager')
        ]
      })
    }
  }
  return jsx(Block, {
    title: '会话',
    hint: scopeText(view.scope) ? '只读 · ' + scopeText(view.scope) : '只读',
    action: jsx(RefreshButton, {
      busy: props.query.busy,
      onClick: props.query.reload,
      title: '重新读该 profile 的 state.db（只重取本区块）'
    }),
    children: body
  })
}

/**
 * 分区自检：后端 `/overview` 自检路由早就实现了（6 个宿主取数接口的 import 级探测 +
 * 布局文件位置），但概览一直没接 —— 这一块补上。
 *
 * 为什么值得放进界面：它把「数据源到底接没接上」直接摆出来，坏掉的那一项写清
 * 代码与原因，而不是靠一张 `ok=true` 的探针报告替界面圆场（老板要的正是这个）。
 * 只读、只做 import 级探测，不真的取数，所以很便宜。
 */
function SelfCheckBlock(props) {
  var view = readQuery(props.query)
  var body
  var hint = ''
  if (view.state === 'loading') {
    body = jsx(Skeleton, { lines: 4, label: '分区自检正在读取' })
  } else if (view.state === 'error') {
    body = jsx(ErrorState, {
      title: '分区自检没读到',
      message: view.error,
      onRetry: props.query.reload
    })
  } else {
    var data = view.data || {}
    var deps = data.deps || {}
    var names = Object.keys(deps)
    var okCount = 0
    var rows = names.map(function (name) {
      var dep = deps[name] || {}
      var ok = dep.ok === true
      if (ok) okCount += 1
      var reason = ''
      if (!ok) {
        var err = dep.error || {}
        reason = (err.code || 'unknown') + '：' + (err.message || '没给原因')
      }
      return jsxs(
        'div',
        {
          className: 'hc-row',
          title: (dep.target || name) + (reason ? ' — ' + reason : ''),
          children: [
            jsx('span', { className: 'hc-row__name', children: name }, 'name'),
            jsx(
              'span',
              { className: 'hc-status hc-status--' + (ok ? 'ok' : 'warn'), children: ok ? '接上了' : '不可用' },
              'state'
            ),
            jsx(
              'span',
              {
                className: 'hc-row__next',
                children: ok ? dep.target || '' : (dep.target || '') + ' — ' + reason
              },
              'target'
            )
          ]
        },
        'dep-' + name
      )
    })
    var layoutInfo = data.layout || {}
    hint = okCount + ' / ' + names.length + ' 个宿主接口可用'
    body = jsxs('div', {
      children: [
        jsx('div', {
          // 自检行是**三列**（子路由 / 状态 / 宿主接口或失败原因），不是表格的 4 列。
          // 列定义在 style.css 的 `.hc-rows--selfcheck`（基类 `.hc-row` 已不带列定义）。
          className: 'hc-rows hc-rows--selfcheck',
          'data-hc-table': 'selfcheck',
          children: [
            jsx(
              'div',
              {
                className: 'hc-rows__head',
                role: 'row',
                children: [
                  jsx('span', { children: '子路由' }, 'h-name'),
                  jsx('span', { children: '状态' }, 'h-state'),
                  jsx('span', { children: '宿主接口 / 失败原因' }, 'h-target')
                ]
              },
              'head'
            )
          ].concat(rows)
        }, 'rows'),
        jsx('div', {
          className: 'hc-muted',
          children:
            '布局文件 ' + (layoutInfo.path || DASH) + (layoutInfo.exists ? '（存在）' : '（不存在）') +
            ' · 路由 ' + ((data.routes || []).length || 0) + ' 条 · 探测时间 ' +
            (typeof data.ts === 'number' ? absDate(data.ts) : DASH) +
            ' · 这里只做 import 级探测，不取数'
        }, 'foot')
      ]
    })
  }
  return jsx(Block, {
    title: '分区自检',
    hint: hint,
    action: jsx(RefreshButton, {
      busy: props.query.busy,
      onClick: props.query.reload,
      title: '重新探测 6 个宿主取数接口（只重取本区块）'
    }),
    children: body
  })
}

/**
 * 概览分区。
 * @param {{agent: string, ready?: boolean}} props
 */
export function OverviewSection(props) {
  var agent = props.agent || ''
  var ready = props.ready !== false
  var offsetState = useState(0)
  var offset = offsetState[0]
  var setOffset = offsetState[1]

  // token 卡的统计窗口：真的进 query key，切一次就重发一条 days=N 请求。
  var windowState = useState(TOKEN_WINDOW_DAYS)
  var tokenWindow = windowState[0]
  var setTokenWindow = windowState[1]

  // 「刷新全部」按下的时刻（只用于显示，不参与取数逻辑）：不按就不显示时间，
  // 免得写出一个"上次刷新 12:00"的假时间。
  var stampState = useState(0)
  var refreshedAt = stampState[0]
  var setRefreshedAt = stampState[1]

  // 换 agent 时回到第 1 页：否则在 default 的第 3 页切到只有 20 个会话的 profile，
  // 会拿到空页 + 看不到分页器（列表为空时它不渲染）—— 那是会让人以为"这个 agent 没会话"的死角。
  useEffect(
    function () {
      setOffset(0)
    },
    [agent]
  )

  var systemQuery = useQuery(
    'overview|system|' + agent,
    function () {
      return get('/overview/system', { agent: agent })
    },
    { enabled: ready }
  )
  var analyticsQuery = useQuery(
    'overview|analytics|' + agent + '|' + tokenWindow,
    function () {
      // analytics 实测 2.4–4.2s（sessions 表聚合）。给它一个明确上限：超时就进 ErrorState
      // 说「取数超时」，而不是让卡片一直停在骨架屏上等到宿主的 30s 默认超时。
      return get('/overview/analytics', {
        agent: agent,
        params: { days: tokenWindow },
        timeoutMs: ANALYTICS_TIMEOUT_MS
      })
    },
    { enabled: ready }
  )
  var cronQuery = useQuery(
    'overview|cron|' + agent,
    function () {
      return get('/overview/cron', { agent: agent })
    },
    { enabled: ready }
  )
  var layoutQuery = useQuery(
    'overview|layout|' + agent,
    function () {
      return get('/overview/layout', { agent: agent })
    },
    { enabled: ready }
  )
  var sessionsQuery = useQuery(
    'overview|sessions|' + agent + '|' + offset,
    function () {
      return get('/overview/sessions', {
        agent: agent,
        params: { limit: SESSION_PAGE_SIZE, offset: offset }
      })
    },
    { enabled: ready }
  )
  // 网关 / 心跳两个挂件的真数据源（宿主 status）。与 system 同一条「宿主级」取数口径，
  // 但字段不同，所以单独一条请求：status 挂了不该把系统卡也拖进错误态。
  var statusQuery = useQuery(
    'overview|status|' + agent,
    function () {
      return get('/overview/status', { agent: agent })
    },
    { enabled: ready }
  )
  // 日志错误挂件：errors.log 尾窗口 + 按级别计数。请求 ALL 级别让前端自己数
  // （只要 ERROR 就看不到警告分布，而概览要的是「最近有没有出事」）。
  var logsQuery = useQuery(
    'overview|logs|' + agent,
    function () {
      return get('/overview/logs', {
        agent: agent,
        params: { file: 'errors', lines: LOG_WINDOW_LINES, level: 'ALL' }
      })
    },
    { enabled: ready }
  )
  // 分区自检（后端 `/overview`）：6 个宿主取数接口是否可解析 + 布局文件位置。
  // 后端一直有这个路由，前端以前没用过 —— 概览底部补一块，把"数据源接没接上"摆出来。
  var selfCheckQuery = useQuery(
    'overview|selfcheck|' + agent,
    function () {
      return get('/overview', { agent: agent })
    },
    { enabled: ready }
  )

  // 时钟 / 日历 / 月相是纯本地推算，靠这个 tick 每分钟重算一次（秒级只在时钟卡里用）。
  // unref：Node（探针/SSR）里不把定时器算成"还有活干"，否则探针进程会挂住不退出；
  // 浏览器里 setInterval 返回数字，没有 unref，这行就是空操作。
  var tickState = useState(Date.now())
  var now = tickState[0]
  var setNow = tickState[1]
  useEffect(function () {
    var timer = setInterval(function () {
      setNow(Date.now())
    }, CLOCK_TICK_MS)
    if (timer && typeof timer.unref === 'function') timer.unref()
    return function () {
      clearInterval(timer)
    }
  }, [])

  // 挂件卡要用到「别的区块已经取到的数据」：不再各拉一遍，直接把查询态递下去。
  var widgetCtx = {
    now: now,
    tokenWindow: tokenWindow,
    system: readQuery(systemQuery),
    analytics: readQuery(analyticsQuery),
    cron: readQuery(cronQuery),
    sessions: readQuery(sessionsQuery),
    status: readQuery(statusQuery),
    logs: readQuery(logsQuery),
    cronCounts: readQuery(cronQuery).counts || null
  }

  // 工具条上的「数据源汇总」：数的是真实查询态，不是一句"全部正常"。
  var allQueries = [
    systemQuery,
    analyticsQuery,
    cronQuery,
    layoutQuery,
    sessionsQuery,
    statusQuery,
    logsQuery,
    selfCheckQuery
  ]
  var loadingCount = 0
  var errorCount = 0
  allQueries.forEach(function (query) {
    if (query.status === 'loading') loadingCount += 1
    else if (query.status === 'error') errorCount += 1
  })
  var sourceText
  if (!ready) sourceText = '等作用域就绪'
  else if (loadingCount) sourceText = '取数中 ' + loadingCount + ' / ' + allQueries.length
  else if (errorCount) sourceText = '失败 ' + errorCount + ' / ' + allQueries.length + ' 个数据源'
  else sourceText = allQueries.length + ' 个数据源全部取到'

  var anyBusy = allQueries.some(function (query) {
    return query.busy === true
  })

  function refreshEverything() {
    setRefreshedAt(Date.now())
    allQueries.forEach(function (query) {
      query.reload()
    })
  }

  var toolbar = jsxs('div', {
    className: 'hc-block__head',
    children: [
      // 作用域那块 chip 由外壳的分区标题给（`hc-pane__head`），这里不重复一遍。
      jsx('span', { className: 'hc-chip', children: sourceText }, 'sources'),
      refreshedAt
        ? jsx(
            'span',
            {
              className: 'hc-chip',
              title: '这是「刷新全部」按下的时刻；首屏加载时间不在这里假装',
              children: '上次手动刷新 ' + absDate(refreshedAt)
            },
            'stamp'
          )
        : null,
      jsx(
        'div',
        {
          style: { marginLeft: 'auto' },
          children: jsx('button', {
            type: 'button',
            // 主操作档（实心 accent）：整页唯一一个 —— 「重发本页全部取数」是这一屏最重的那件事，
            // 顶栏「刷新」与各挂件自己的「刷新」都留在次操作档（描边），三档不再混着用同一种样式。
            className: 'hc-btn hc-btn--tiny hc-btn--primary',
            disabled: anyBusy || !ready,
            title: '把概览用到的 ' + allQueries.length + ' 条取数全部重发一遍（不清空当前内容）',
            onClick: refreshEverything,
            children: anyBusy ? '刷新中…' : '刷新全部'
          })
        },
        'refresh'
      )
    ]
  })

  return jsxs('div', {
    className: 'hc-overview',
    children: [
      jsx('div', { children: toolbar }, 'toolbar'),
      jsx(
        'div',
        {
          className: 'hc-cards',
          children: [
            // 四个卡片函数各自返回一个元素；这里补 key（否则 React 会在控制台告警
            // 「Each child in a list should have a unique key prop」）。
            cloneElement(systemCard(readQuery(systemQuery)), { key: 'card-system' }),
            cloneElement(tokenCard(readQuery(analyticsQuery), tokenWindow, setTokenWindow), {
              key: 'card-token'
            }),
            cloneElement(cronCard(readQuery(cronQuery)), { key: 'card-cron' }),
            cloneElement(sessionsCard(readQuery(sessionsQuery), readQuery(statusQuery)), {
              key: 'card-sessions'
            })
          ]
        },
        'cards'
      ),
      jsx(LayoutBlock, { query: layoutQuery, ctx: widgetCtx }, 'layout'),
      jsx(CronBlock, { query: cronQuery }, 'cron'),
      jsx(SessionsBlock, { query: sessionsQuery, onOffset: setOffset }, 'sessions'),
      jsx(SelfCheckBlock, { query: selfCheckQuery }, 'selfcheck')
    ]
  })
}
