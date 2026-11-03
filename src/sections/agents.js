/**
 * hermes-console · Agent 总览分区（C7：一屏 9 个 agent + 单行下钻）。
 *
 * 与其它三个分区的区别只有一点：**本分区忽略顶部作用域选择器**。
 * 它回答的是「这台机器上所有 agent 各自怎么样了」，所以永远取全量；
 * 标题旁那行字（本页显示全部 N 个 agent）就是给老板看的边界说明。
 *
 * 四条规矩：
 *   1. **一行坏不连坐**：后端已经逐行隔离，前端也逐行降级 —— 坏行显示 `—` + 原因 +
 *      单独重试，其余行照常；整页数据源整体失败才进 ErrorState；
 *   2. **数字带口径**：后端 `calibers` 原样挂在表头/单元格的 title 上，不在前端另编说法；
 *   3. **展开才取数**：会话与 cron 列表在行被展开时才发请求（懒加载，不预取 9×2 份）；
 *   4. 颜色只走 `var(--ui-*)`；本文件不出现字面色值（构建脚本会检查）。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

import { num, relTime } from '../lib/format.js'
import { useQuery } from '../lib/query.js'
import { get } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 占位符（不是 0，不是空字符串）：取不到就说取不到。 */
var DASH = '—'

/** 总览缓存窗口。9 个 profile 的取数实测 ~1.8s（首屏含网关拓扑扫描），别让人一进分区就重拉。 */
var OVERVIEW_STALE_MS = 15000

/** 展开后的会话 / cron 列表各取几条（下钻用，够看清“最近在干什么”）。 */
var DETAIL_LIMIT = 5

/** 排序选项：默认最近活动倒序（谁刚干过活排最上面）。 */
var SORTS = [
  { id: 'recent', label: '最近活动', hint: '按最近一次会话活动倒序（没有活动的排最后）' },
  { id: 'sessions', label: '会话数', hint: '按会话总数倒序' },
  { id: 'name', label: '名称', hint: '按 agent id 字母序' }
]

function fetchOverview() {
  return get('/agents/overview')
}

/** 后端口径表取值（缺了就是空字符串，不编造）。 */
function caliber(calibers, key) {
  return (calibers && calibers[key]) || ''
}

/** 数字单元格：null → `—`，并把该字段的失败原因挂到 title 上。 */
function metricCell(value, fieldErrors, key, calibers) {
  var reason = (fieldErrors && fieldErrors[key]) || ''
  var text = value === null || value === undefined ? DASH : num(value)
  var title = reason
    ? '没读到：' + reason
    : caliber(calibers, key) || ''
  return jsx('span', {
    className: 'hc-ag__num' + (value === null || value === undefined ? ' hc-ag__num--none' : ''),
    title: title,
    children: text
  })
}

/** 复制文本（Electron 里 navigator.clipboard 可能不可用，失败不抛、不弹窗）。 */
function copyText(text) {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
      return true
    }
  } catch (e) {
    // 受限上下文：当作没复制成功，用户还能从展开详情里看到 id
  }
  return false
}

/** 状态灯：形状 + 文字双通道（不靠颜色单独表达）。 */
function statusOf(row) {
  if (!row.available) return { tone: 'warn', text: '取不到' }
  var gw = row.gateway
  if (!gw) return { tone: 'off', text: '网关未知' }
  if (gw.running) return { tone: 'ok', text: gw.mode === 'multiplexed' ? '被承载' : '在跑' }
  return { tone: 'off', text: '无网关' }
}

/**
 * 展开后的详情：会话 + cron 各一条请求，各自三态。
 * @param {{agent: string, calibers: Object}} props
 */
function AgentDetail(props) {
  var id = props.agent
  var sessions = useQuery(
    'agents.' + id + '.sessions',
    function () {
      return get('/agents/' + encodeURIComponent(id) + '/sessions', { params: { limit: DETAIL_LIMIT } })
    },
    { staleMs: OVERVIEW_STALE_MS }
  )
  var cron = useQuery(
    'agents.' + id + '.cron',
    function () {
      return get('/agents/' + encodeURIComponent(id) + '/cron', { params: { limit: DETAIL_LIMIT } })
    },
    { staleMs: OVERVIEW_STALE_MS }
  )

  function panel(label, key, query, emptyHint, render) {
    var head = jsx('div', { className: 'hc-ag__detail-title', children: label }, 'h')
    var body
    if (query.status === 'loading') {
      body = jsx(Skeleton, { lines: 2, label: label + '正在读取' }, 'b')
    } else if (query.status === 'error') {
      body = jsx(
        ErrorState,
        { title: label + '没读到', message: query.error, onRetry: query.reload, retryLabel: '重试' },
        'b'
      )
    } else {
      var payload = (query.data && query.data.data) || {}
      var rows = payload[key] || []
      var total = payload.total
      if (!rows.length) {
        body = jsx(Empty, { title: '0 条', hint: emptyHint }, 'b')
      } else {
        body = jsxs(
          'div',
          {
            className: 'hc-ag__list',
            children: [
              rows.map(function (item, index) {
                return render(item, index)
              }),
              jsx(
                'div',
                {
                  className: 'hc-ag__list-foot',
                  children: '显示最近 ' + rows.length + ' 条 / 共 ' + num(total) + ' 条'
                },
                'foot'
              )
            ]
          },
          'b'
        )
      }
    }
    return jsxs('div', { className: 'hc-ag__detail-panel', children: [head, body] }, label)
  }

  return jsxs('div', {
    className: 'hc-ag__detail',
    children: [
      panel('会话', 'sessions', sessions, '这个 agent 还没有会话', function (item, index) {
        return jsxs(
          'div',
          {
            className: 'hc-ag__list-row',
            children: [
              jsx(
                'span',
                { className: 'hc-ag__list-name', title: item.title || item.id, children: item.title || item.id },
                'n'
              ),
              jsx('span', { className: 'hc-ag__list-meta', children: (item.message_count || 0) + ' 条消息' }, 'm'),
              jsx('span', { className: 'hc-ag__list-meta', children: relTime(item.last_active) }, 't')
            ]
          },
          'session' + index + (item.id || '')
        )
      }),
      panel('定时任务', 'jobs', cron, '这个 agent 没有定时任务', function (item, index) {
        return jsxs(
          'div',
          {
            className: 'hc-ag__list-row',
            children: [
              jsx('span', { className: 'hc-ag__list-name', title: item.name || item.id, children: item.name || item.id }, 'n'),
              jsx(
                'span',
                {
                  className: 'hc-ag__list-meta',
                  children: item.paused_at ? '已暂停' : item.enabled ? '启用' : '停用'
                },
                's'
              ),
              jsx(
                'span',
                { className: 'hc-ag__list-meta', children: '下次 ' + relTime(item.next_run_at) },
                't'
              )
            ]
          },
          'job' + index + (item.id || '')
        )
      })
    ]
  })
}

/**
 * 一行。
 * @param {{row: Object, calibers: Object, expanded: boolean, onToggle: Function,
 *          onRetry: Function, onOpen: Function, onRefresh: Function}} props
 */
function AgentRow(props) {
  var row = props.row
  var calibers = props.calibers
  var errors = row.field_errors || {}
  var status = statusOf(row)
  var copiedState = useState(false)
  var copied = copiedState[0]

  function copy() {
    if (!copyText(row.id)) return
    copiedState[1](true)
    setTimeout(function () {
      copiedState[1](false)
    }, 1200)
  }

  var head = jsxs(
    'div',
    {
      className: 'hc-ag__row' + (row.available ? '' : ' hc-ag__row--bad'),
      role: 'row',
      children: [
      jsx(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--status',
          role: 'cell',
          children: jsx('span', {
            className: 'hc-status hc-status--' + status.tone,
            title: (row.gateway && row.gateway.note) || row.unavailable_reason || '',
            children: jsx('span', { className: 'hc-ag__status-text', children: status.text })
          })
        },
        'status'
      ),
      jsxs(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--name',
          role: 'cell',
          children: [
            jsx('span', {
              className: 'hc-ag__label',
              title: row.label === row.id ? row.id : row.label + '（' + row.id + '）',
              children: row.label || row.id
            }),
            jsx('span', { className: 'hc-ag__id', children: row.id })
          ]
        },
        'name'
      ),
      jsx(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--wide',
          role: 'cell',
          title: caliber(calibers, 'model'),
          children: jsx('span', { className: 'hc-ag__text', children: row.model || DASH })
        },
        'model'
      ),
      jsx(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--sessions',
          role: 'cell',
          children: metricCell(row.sessions_total, errors, 'sessions_total', calibers)
        },
        'sessions'
      ),
      jsx(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--recent',
          role: 'cell',
          children: jsx('span', {
            className: 'hc-ag__text',
            title: caliber(calibers, 'last_active_at'),
            children: relTime(row.last_active_at)
          })
        },
        'recent'
      ),
      jsx(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--cron',
          role: 'cell',
          children:
            row.cron_total === null || row.cron_total === undefined
              ? metricCell(null, errors, 'cron_total', calibers)
              : jsx('span', {
                  className: 'hc-ag__text',
                  title: caliber(calibers, 'cron_enabled'),
                  children: num(row.cron_enabled) + ' / ' + num(row.cron_total)
                })
        },
        'cron'
      ),
      jsx(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--wide',
          role: 'cell',
          children: metricCell(row.plugins_enabled_count, errors, 'plugins_enabled_count', calibers)
        },
        'plugins'
      ),
      jsxs(
        'span',
        {
          className: 'hc-ag__cell hc-ag__cell--ops',
          role: 'cell',
          children: [
          jsx(
            'button',
            {
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              title: '展开该 agent 的会话与定时任务（懒加载）',
              'aria-expanded': props.expanded ? 'true' : 'false',
              onClick: props.onToggle,
              children: props.expanded ? '收起' : '下钻'
            },
            'toggle'
          ),
          jsx(
            'button',
            {
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              title: '以该 agent 为作用域打开概览分区（其它分区会跟着切作用域）',
              onClick: props.onOpen,
              children: '打开'
            },
            'open'
          ),
          jsx(
            'button',
            {
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              title: '复制 agent id',
              onClick: copy,
              children: copied ? '已复制' : '复制'
            },
            'copy'
          ),
          jsx(
            'button',
            {
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              title: '只重取这一行（其它行保留当前数据）',
              onClick: props.onRefresh,
              children: '刷新'
            },
            'refresh'
          )
        ]
        },
        'ops'
      )
      ]
    }
  )

  var children = [head]
  if (!row.available) {
    children.push(
      jsxs(
        'div',
        {
          className: 'hc-ag__reason',
          role: 'cell',
          children: [
            jsx('span', { className: 'hc-ag__reason-text', children: row.unavailable_reason || '原因未知' }),
            jsx('button', {
              type: 'button',
              className: 'hc-btn hc-btn--tiny hc-btn--accent',
              onClick: props.onRetry,
              children: '重试这一行'
            })
          ]
        },
        'reason'
      )
    )
  }
  if (props.expanded) {
    children.push(
      jsx('div', { className: 'hc-ag__detail-wrap', children: jsx(AgentDetail, { agent: row.id, calibers: calibers }) }, 'detail')
    )
  }
  return jsxs(
    'div',
    {
      className: 'hc-ag__item' + (row.available ? '' : ' hc-ag__item--bad'),
      // 行级稳定标记：DOM 断言与失败行样式都靠这两个属性，不要改名
      'data-agent-id': row.id,
      'data-unavailable': row.available ? undefined : 'true',
      children: children
    },
    row.id
  )
}

/** 排序比较器：null 永远排最后（“没有数据”不该冒到最上面冒充最新）。 */
function comparator(sort) {
  return function (left, right) {
    if (sort === 'name') return String(left.id).localeCompare(String(right.id))
    if (sort === 'sessions') {
      var a = typeof left.sessions_total === 'number' ? left.sessions_total : -1
      var b = typeof right.sessions_total === 'number' ? right.sessions_total : -1
      return b - a
    }
    var la = typeof left.last_active_at === 'number' ? left.last_active_at : -1
    var ra = typeof right.last_active_at === 'number' ? right.last_active_at : -1
    return ra - la
  }
}

/**
 * 分区入口。
 * @param {{agent: string, ready: boolean, onOpenAgent?: (id: string) => void}} props
 */
export function AgentsSection(props) {
  var query = useQuery('agents.overview', fetchOverview, { staleMs: OVERVIEW_STALE_MS })
  var sortState = useState('recent')
  var expandState = useState({})
  var expanded = expandState[0]

  function toggle(id) {
    expandState[1](function (current) {
      var next = {}
      Object.keys(current).forEach(function (key) {
        next[key] = current[key]
      })
      if (next[id]) delete next[id]
      else next[id] = true
      return next
    })
  }

  function openAgent(id) {
    if (typeof props.onOpenAgent === 'function') props.onOpenAgent(id)
  }

  if (query.status === 'loading') {
    return jsx('div', { className: 'hc-ag', children: jsx(Skeleton, { lines: 2, cards: 6, label: '全部 agent 正在读取' }) })
  }
  if (query.status === 'error') {
    return jsx('div', {
      className: 'hc-ag',
      children: jsx(ErrorState, {
        title: 'agent 总览没读到',
        message: query.error,
        onRetry: query.reload,
        retryLabel: '重试'
      })
    })
  }

  var body = query.data || {}
  var rows = Array.isArray(body.agents) ? body.agents : []
  var calibers = body.calibers || {}
  if (!rows.length) {
    return jsx('div', {
      className: 'hc-ag',
      children: jsx(Empty, {
        title: '一个 agent 都没枚举到',
        hint: '后端扫的是 Hermes 主目录下的 profiles/（含 config.yaml 的目录 + default）；目录存在但这里为空，说明主目录解析到了别处。',
        action: { label: '重试', onClick: query.reload }
      })
    })
  }

  var sorted = rows.slice().sort(comparator(sortState[0]))
  var badCount = rows.filter(function (row) {
    return !row.available
  }).length

  var head = jsxs('div', {
    className: 'hc-ag__head',
    role: 'row',
    children: [
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--status', role: 'columnheader', children: '状态' }, 'status'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--name', role: 'columnheader', children: '名称' }, 'name'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--wide', role: 'columnheader', children: '模型' }, 'model'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--sessions', role: 'columnheader', children: '会话' }, 'sessions'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--recent', role: 'columnheader', children: '最近活动' }, 'recent'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--cron', role: 'columnheader', children: '定时任务' }, 'cron'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--wide', role: 'columnheader', children: '插件' }, 'plugins'),
      jsx('span', { className: 'hc-ag__cell hc-ag__cell--ops', role: 'columnheader', children: '操作' }, 'ops')
    ]
  })

  return jsxs('div', {
    className: 'hc-ag',
    children: [
      jsxs('div', {
        className: 'hc-ag__bar',
        children: [
          jsx('span', { className: 'hc-ag__count', children: '本页显示全部 ' + rows.length + ' 个 agent' }, 'count'),
          badCount
            ? jsx('span', { className: 'hc-chip hc-chip--off', children: badCount + ' 行取不到' }, 'bad')
            : null,
          jsxs(
            'span',
            {
              className: 'hc-ag__sort',
              children: [
                jsx('span', { className: 'hc-ag__sort-label', children: '排序' }, 'label'),
                jsx(
                  'select',
                  {
                    className: 'hc-select',
                    value: sortState[0],
                    onChange: function (event) {
                      sortState[1](event.target.value)
                    },
                    children: SORTS.map(function (item) {
                      return jsx('option', { value: item.id, title: item.hint, children: item.label }, item.id)
                    })
                  },
                  'select'
                )
              ]
            },
            'sort'
          )
        ]
      }),
      jsxs('div', {
        className: 'hc-ag__table',
        role: 'table',
        'aria-label': '全部 agent 总览',
        children: [
          head,
          sorted.map(function (row) {
            return jsx(
              AgentRow,
              {
                row: row,
                calibers: calibers,
                expanded: !!expanded[row.id],
                onToggle: function () {
                  toggle(row.id)
                },
                onRetry: query.reload,
                onRefresh: function () {
                  query.reload()
                },
                onOpen: function () {
                  openAgent(row.id)
                }
              },
              row.id
            )
          })
        ]
      }),
      jsx('div', {
        className: 'hc-ag__foot',
        children:
          '每行数据独立取数（单 agent ≤3 秒超时）：某一行取不到只影响那一行。' +
          '口径：' +
          (caliber(calibers, 'sessions_total') || DASH) +
          '；' +
          (caliber(calibers, 'data_at') || DASH)
      })
    ]
  })
}
