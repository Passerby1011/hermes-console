/**
 * hermes-console · 升级分区（V2）：真实 UI，替掉 1,374B 的占位。
 *
 * 数据全部来自本插件自己的 6 条端点（本文件不发任何别的请求）：
 *   GET  /upgrade/scan       进程分类 + 版本 + 就绪快照（纯只读）
 *   GET  /upgrade/readiness  完整就绪检查（唯一会做自动恢复兜底的读端点）
 *   POST /upgrade/prepare    清场：暂停全部激活 cron（带快照，写失败会回滚）
 *   POST /upgrade/restore    恢复：只恢复本次快照里暂停的任务
 *   POST /upgrade/wait       轮询等待 worker 自然退出
 *   POST /upgrade/kill       结束指定进程（只放行 worker / CLI）
 *
 * 六条不许退让的规则：
 *   1. **三态齐备**：加载中是骨架屏（占住形状，不是空白）；一个进程都没扫到是空态
 *      （说清为什么空 + 下一步）；请求失败是错误态（后端原话 + 重试）。
 *   2. **错误原样透出**：后端 `error.message` 一个字都不改写（rest.js 已把
 *      `{"ok":false,"error":{code,message}}` 里的 message 抽出来），本文件不编原因、
 *      也不把失败渲染成成功。
 *   3. **危险操作二次确认**：清场（会暂停全部 cron）、恢复（会改动 cron 状态）、
 *      结束进程（不可撤销）三个动作都必须先「点一次 → 出现确认条 → 再点确认」。
 *      一次点击绝不直接执行。
 *   4. **口径不自己编**：拦路进程 / 激活 cron / 待恢复快照全部来自后端，前端只做展示；
 *      版本号、状态文件路径也是后端给的，不在这里拼。
 *   5. **宿主级分区**：升级是整机动作，与顶部作用域选择器无关 —— 界面明说这一点，
 *      而不是悄悄忽略 agent 参数。
 *   6. 颜色只走 `var(--ui-*)`（样式在 style.css 的 `.hc-up*`），本文件零字面色值。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

import { num } from '../lib/format.js'
import { refresh, useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 缓存 key：两个端点各自独立（scan 变化不代表 readiness 变化，反之亦然）。 */
var SCAN_KEY = 'upgrade|scan'
var READY_KEY = 'upgrade|readiness'

/** 10 秒新鲜期：升级过程里手点刷新比自动轮询更可靠，不做定时轮询（省 CPU 也不误判）。 */
var STALE_MS = 10000

/** 等待收尾的默认超时（秒）。后端会钳到 5..600。 */
var WAIT_SECONDS = 120

/** 进程身份 → 界面文案。后端给的 kind 不认识时原样显示（不编一个「未知」）。 */
var KIND_TEXT = {
  worker: 'worker / CLI',
  gateway: '消息网关',
  'desktop-serve': '桌面 serve',
  'hermes-other': '其它 Hermes 进程',
  unrelated: '与本机 Hermes 无关'
}

/** 取 `{ok:true,data:{...}}` 里的 data；形状不对就当空（不抛、不崩）。 */
function payloadOf(query) {
  var body = query && query.data
  if (!body || typeof body !== 'object') return null
  var data = body.data
  return data && typeof data === 'object' ? data : null
}

/** 一行动作结果的一句话摘要（数字全部来自后端返回值，不自己算）。 */
function summarize(key, data) {
  var d = data || {}
  if (key === 'prepare') {
    return (
      '已暂停 ' +
      num(d.paused_count || 0) +
      ' 个定时任务并写入快照；可清场 worker ' +
      num((d.workers || []).length) +
      ' 个'
    )
  }
  if (key === 'restore') {
    var parts = ['已恢复 ' + num(d.resumed_count || 0) + ' 个任务']
    if ((d.skipped || []).length) parts.push('跳过 ' + d.skipped.length + ' 个（暂停前就非激活）')
    if ((d.failed || []).length) parts.push('失败 ' + d.failed.length + ' 个：' + d.failed.join('、'))
    return parts.join('；')
  }
  if (key === 'wait') {
    if (d.timed_out) {
      return (
        '等待超时：仍有 ' +
        num((d.still_alive || []).length) +
        ' 个进程存活（' +
        (d.still_alive || []).join('、') +
        '）'
      )
    }
    return '全部已退出：' + (d.exited || []).join('、')
  }
  if (key === 'kill') {
    var text = '已结束 ' + num((d.killed || []).length) + ' 个进程'
    if ((d.refused || []).length) text += '；被拒绝 ' + d.refused.length + ' 个'
    return text
  }
  return '已完成'
}

/** 统计卡（复用概览的 .hc-card，不新造一套）。 */
function Stat(props) {
  var children = [
    jsx('div', { className: 'hc-card__label', children: props.label }, 'label'),
    jsx('div', { className: 'hc-card__value', children: props.value }, 'value')
  ]
  if (props.sub) {
    children.push(jsx('div', { className: 'hc-card__sub', children: props.sub }, 'sub'))
  }
  return jsxs('div', {
    className: 'hc-card' + (props.tone ? ' hc-card--' + props.tone : ''),
    children: children
  })
}

/**
 * 二次确认条：危险动作点第一次只**出现这一条**，不执行任何请求。
 * `token` 会写进 `data-confirm`，探针与样式都靠它定位。
 */
function ConfirmStrip(props) {
  return jsxs('div', {
    className: 'hc-up__confirm',
    'data-confirm': props.token,
    role: 'group',
    'aria-label': props.question,
    children: [
      jsx('span', { className: 'hc-up__confirm-q', children: props.question }, 'q'),
      jsx(
        'button',
        {
          type: 'button',
          className: 'hc-btn hc-btn--accent',
          'data-confirm-yes': props.token,
          onClick: props.onConfirm,
          children: props.label || '确认执行'
        },
        'yes'
      ),
      jsx('button', { type: 'button', className: 'hc-btn', onClick: props.onCancel, children: '取消' }, 'no')
    ]
  })
}

/** 动作结果条：成功一句话；失败给后端原话 + 错误码（不吞、不美化）。 */
function Feedback(props) {
  var result = props.result
  if (!result) return null
  var children = [
    jsx('span', { className: 'hc-up__feedback-title', children: result.title }, 'title'),
    jsx('span', { className: 'hc-up__feedback-text', children: result.message }, 'text')
  ]
  if (result.code) {
    children.push(jsx('span', { className: 'hc-chip', children: '错误码 ' + result.code }, 'code'))
  }
  return jsxs('div', {
    className: 'hc-up__feedback hc-up__feedback--' + (result.tone === 'ok' ? 'ok' : 'error'),
    role: result.tone === 'ok' ? 'status' : 'alert',
    'data-upgrade-feedback': result.tone,
    children: children
  })
}

/**
 * 分区入口。
 * @param {{agent?: string, ready?: boolean}} props
 */
export function UpgradeSection(props) {
  var enabled = props.ready !== false
  var scanQuery = useQuery(
    SCAN_KEY,
    function () {
      // 升级是宿主级动作：不传 agent（后端也没有这个参数，传了只会让缓存按作用域碎片化）
      return get('/upgrade/scan')
    },
    { staleMs: STALE_MS, enabled: enabled }
  )
  var readyQuery = useQuery(
    READY_KEY,
    function () {
      return get('/upgrade/readiness')
    },
    { staleMs: STALE_MS, enabled: enabled }
  )

  var pendingState = useState('')
  var resultState = useState(null)
  var confirmState = useState('')
  var pending = pendingState[0]
  var result = resultState[0]
  var confirm = confirmState[0]

  function reloadAll() {
    scanQuery.reload()
    readyQuery.reload()
  }

  /** 发一个写动作。`key` 同时用于 pending 标记与摘要口径。 */
  function run(key, path, body, label) {
    pendingState[1](key)
    confirmState[1]('')
    send(path, body || {}).then(function (outcome) {
      pendingState[1]('')
      if (outcome.ok) {
        var data = outcome.data && outcome.data.data ? outcome.data.data : {}
        resultState[1]({ tone: 'ok', title: label + '完成', message: summarize(key, data) })
        refresh(SCAN_KEY)
        refresh(READY_KEY)
        return
      }
      // 失败：message 里就是后端 error.message 原文（rest.js 抽的），不二次改写。
      resultState[1]({
        tone: 'error',
        title: label + '失败',
        message: outcome.message,
        code: outcome.errorCode || outcome.code || ''
      })
    })
  }

  function arm(token) {
    confirmState[1](token)
  }

  function disarm() {
    confirmState[1]('')
  }

  // ------------------------------------------------------------------ 三态
  // 注意：`jsx(type, props, key)` 的第三个参数是 **key**，不是 children。
  // 三态都是单子节点，必须写成 props.children（写成第三个参数会渲染出一个空壳）。
  if (scanQuery.status === 'loading' || readyQuery.status === 'loading') {
    return jsx('div', {
      className: 'hc-up',
      'data-upgrade-state': 'loading',
      children: jsx(Skeleton, { cards: 4, lines: 4, label: '正在扫描进程与定时任务' })
    })
  }

  if (scanQuery.status === 'error') {
    return jsx('div', {
      className: 'hc-up',
      'data-upgrade-state': 'error',
      children: jsx(ErrorState, {
        title: '进程扫描没读到',
        message: scanQuery.error,
        onRetry: reloadAll,
        retryLabel: '重试'
      })
    })
  }

  var scan = payloadOf(scanQuery) || {}
  var procs = Array.isArray(scan.procs) ? scan.procs : []
  var workers = Array.isArray(scan.workers)
    ? scan.workers
    : procs.filter(function (p) {
        return p && p.can_kill
      })
  var version = scan.version || {}
  var statePath = scan.state_file || ''
  var readiness = payloadOf(readyQuery) || scan.readiness || {}

  if (!procs.length) {
    return jsx('div', {
      className: 'hc-up',
      'data-upgrade-state': 'empty',
      children: jsx(Empty, {
        title: '一个 Python 进程都没扫到',
        hint:
          '清场只针对 Python 解释器进程（看板 worker / CLI 会话）。这里为空说明本机此刻没有这类进程，' +
          '升级不会被它们卡住；定时任务那边仍然要单独看待就绪检查。',
        action: { label: '重新扫描', onClick: reloadAll }
      })
    })
  }

  var readyText = readiness.ready ? '可以升级' : '还有拦路项'
  var blockers = Array.isArray(readiness.blockers) ? readiness.blockers : []
  var pendingRestore = readiness.pending_restore === true
  var busyPrepare = pending === 'prepare'
  var busyRestore = pending === 'restore'
  var busyWait = pending === 'wait'

  // ------------------------------------------------------------- 顶部一行
  var bar = jsxs('div', {
    className: 'hc-up__bar',
    children: [
      jsx(
        'span',
        {
          className: 'hc-up__version',
          children:
            'Hermes ' + (version.version || 'unknown') + (version.sha ? ' · ' + version.sha : '')
        },
        'ver'
      ),
      jsx(
        'span',
        {
          className: 'hc-up__path',
          title: statePath || '后端没给状态文件路径',
          children: '快照文件：' + (statePath || '—')
        },
        'path'
      ),
      jsx('span', { className: 'hc-chip', children: '宿主级 · 与作用域无关' }, 'scope'),
      jsx(
        'button',
        {
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-upgrade-action': 'reload',
          onClick: reloadAll,
          children: '重新扫描'
        },
        'reload'
      )
    ]
  })

  // --------------------------------------------------------------- 统计卡
  var cards = jsxs('div', {
    className: 'hc-cards',
    children: [
      jsx(
        Stat,
        {
          label: '就绪状态',
          value: readyText,
          sub: readiness.auto_restore_allowed === false ? '（自动恢复只在就绪检查里执行）' : '',
          tone: readiness.ready ? '' : 'warn'
        },
        'ready'
      ),
      jsx(
        Stat,
        {
          label: '拦路进程',
          value: num(blockers.length),
          sub: blockers.length ? '点下面的「结束」逐个清掉' : '没有需要清场的 worker'
        },
        'blockers'
      ),
      jsx(
        Stat,
        {
          label: '激活的定时任务',
          value: num(readiness.cron_active),
          sub: '共 ' + num(readiness.cron_total) + ' 个任务（含暂停）；清场会暂停其中激活的'
        },
        'cron'
      ),
      jsx(
        Stat,
        {
          label: '待恢复快照',
          value: pendingRestore ? '有' : '无',
          sub: pendingRestore
            ? '快照里有 ' + num(readiness.snapshot_jobs) + ' 个任务未恢复'
            : '没有待恢复的快照',
          tone: pendingRestore ? 'warn' : ''
        },
        'pending'
      )
    ]
  })

  // ------------------------------------------------------------- 操作区块
  var prepareButton = jsx(
    'button',
    {
      type: 'button',
      className: 'hc-btn',
      'data-upgrade-action': 'prepare',
      disabled: busyPrepare || pendingRestore,
      title: pendingRestore
        ? '存在未恢复快照，先恢复再清场（后端会拒绝重复暂停）'
        : '暂停全部激活的定时任务并写快照，然后列出可清场的 worker',
      onClick: function () {
        arm('prepare')
      },
      children: busyPrepare ? '清场中…' : '清场（暂停全部 cron）'
    },
    'prepare'
  )

  var restoreButton = jsx(
    'button',
    {
      type: 'button',
      className: 'hc-btn hc-up__danger',
      'data-upgrade-action': 'restore',
      disabled: busyRestore || !pendingRestore,
      title: pendingRestore ? '按快照恢复本次暂停的任务' : '没有待恢复的快照（后端会返回业务错误）',
      onClick: function () {
        arm('restore')
      },
      children: busyRestore ? '恢复中…' : '恢复定时任务'
    },
    'restore'
  )

  var waitButton = jsx(
    'button',
    {
      type: 'button',
      className: 'hc-btn',
      'data-upgrade-action': 'wait',
      disabled: busyWait || !workers.length,
      title: workers.length
        ? '等待这些 worker 自己退出（最多 ' + WAIT_SECONDS + ' 秒）'
        : '当前没有可清场的 worker',
      onClick: function () {
        run(
          'wait',
          '/upgrade/wait',
          {
            pids: workers.map(function (p) {
              return p.pid
            }),
            timeout: WAIT_SECONDS
          },
          '等待收尾'
        )
      },
      children: busyWait ? '等待中…' : '等待收尾（' + WAIT_SECONDS + ' 秒）'
    },
    'wait'
  )

  var opsChildren = []
  if (confirm === 'prepare') {
    opsChildren.push(
      jsx(
        ConfirmStrip,
        {
          token: 'prepare',
          question:
            '确认清场？会暂停全部 ' + num(readiness.cron_active) + ' 个激活的定时任务（写失败会自动回滚）。',
          label: '确认清场',
          onConfirm: function () {
            run('prepare', '/upgrade/prepare', {}, '清场')
          },
          onCancel: disarm
        },
        'c-prepare'
      )
    )
  } else {
    opsChildren.push(prepareButton)
  }
  if (confirm === 'restore') {
    opsChildren.push(
      jsx(
        ConfirmStrip,
        {
          token: 'restore',
          question:
            '确认恢复？会按快照把 ' +
            num(readiness.snapshot_jobs) +
            ' 个任务里的激活项恢复（快照外的不碰）。',
          label: '确认恢复',
          onConfirm: function () {
            run('restore', '/upgrade/restore', {}, '恢复')
          },
          onCancel: disarm
        },
        'c-restore'
      )
    )
  } else {
    opsChildren.push(restoreButton)
  }
  opsChildren.push(waitButton)

  var ops = jsxs('div', {
    className: 'hc-block',
    children: [
      jsxs(
        'div',
        {
          className: 'hc-block__head',
          children: [
            jsx('span', { className: 'hc-block__title', children: '升级前清场 / 升级后恢复' }, 'title'),
            jsx(
              'span',
              {
                className: 'hc-muted',
                children:
                  '清场只暂停 cron 并写快照；恢复只恢复本次暂停的任务，不复活你本来就暂停的任务。'
              },
              'note'
            )
          ]
        },
        'head'
      ),
      jsx('div', { className: 'hc-up__ops', children: opsChildren }, 'ops'),
      jsx(Feedback, { result: result }, 'feedback')
    ]
  })

  // ------------------------------------------------------------- 进程表
  var rows = procs.map(function (p) {
    var pid = p.pid
    var killToken = 'kill:' + pid
    var kindText = KIND_TEXT[p.kind] || p.kind || '未知'
    var opsCell
    if (confirm === killToken) {
      opsCell = jsx(
        ConfirmStrip,
        {
          token: killToken,
          question: '确认结束 PID ' + pid + '？',
          label: '确认结束',
          onConfirm: function () {
            run('kill', '/upgrade/kill', { pids: [pid] }, '结束进程')
          },
          onCancel: disarm
        },
        'ops'
      )
    } else if (p.can_kill) {
      opsCell = jsx(
        'button',
        {
          type: 'button',
          className: 'hc-btn hc-btn--tiny hc-up__danger',
          'data-upgrade-action': 'kill',
          'data-upgrade-pid': pid,
          disabled: pending === 'kill',
          onClick: function () {
            arm(killToken)
          },
          children: '结束'
        },
        'ops'
      )
    } else {
      opsCell = jsx('span', { className: 'hc-muted', title: p.note || '', children: '不可清场' }, 'ops')
    }
    return jsxs(
      'div',
      {
        className: 'hc-row hc-up__row',
        'data-upgrade-pid': pid,
        'data-can-kill': p.can_kill ? 'true' : 'false',
        children: [
          jsx('span', { className: 'hc-up__cell hc-up__cell--pid', children: String(pid) }, 'pid'),
          jsx(
            'span',
            {
              className: 'hc-up__cell hc-up__cell--kind',
              children: jsx('span', {
                className: 'hc-status hc-status--' + (p.can_kill ? 'warn' : 'off'),
                title: p.note || '',
                children: kindText
              })
            },
            'kind'
          ),
          jsx(
            'span',
            {
              className: 'hc-up__cell hc-up__cell--cmd',
              title: p.cmdline || '',
              children: p.cmdline || '—'
            },
            'cmd'
          ),
          jsx('span', { className: 'hc-up__cell hc-up__cell--ops', children: opsCell }, 'ops')
        ]
      },
      String(pid)
    )
  })

  var table = jsxs('div', {
    className: 'hc-block',
    children: [
      jsxs(
        'div',
        {
          className: 'hc-block__head',
          children: [
            jsx(
              'span',
              { className: 'hc-block__title', children: '扫描到的进程（' + num(procs.length) + '）' },
              'title'
            ),
            jsx(
              'span',
              {
                className: 'hc-muted',
                children: workers.length
                  ? '可清场 worker ' +
                    workers.length +
                    ' 个：只结束这一类；网关与桌面 serve 一律拒绝。'
                  : '没有可清场的 worker：网关与桌面 serve 不在清场范围内（升级流程自己会处理）。'
              },
              'note'
            )
          ]
        },
        'head'
      ),
      jsxs(
        'div',
        {
          className: 'hc-rows hc-up__rows',
          role: 'table',
          'aria-label': '扫描到的 Python 进程',
          children: [
            jsxs(
              'div',
              {
                className: 'hc-rows__head',
                role: 'row',
                children: [
                  jsx(
                    'span',
                    { className: 'hc-up__cell hc-up__cell--pid', role: 'columnheader', children: 'PID' },
                    'pid'
                  ),
                  jsx(
                    'span',
                    { className: 'hc-up__cell hc-up__cell--kind', role: 'columnheader', children: '身份' },
                    'kind'
                  ),
                  jsx(
                    'span',
                    { className: 'hc-up__cell hc-up__cell--cmd', role: 'columnheader', children: '命令行' },
                    'cmd'
                  ),
                  jsx(
                    'span',
                    { className: 'hc-up__cell hc-up__cell--ops', role: 'columnheader', children: '操作' },
                    'ops'
                  )
                ]
              },
              'head'
            ),
            rows
          ]
        },
        'rows'
      ),
      jsx(
        'div',
        {
          className: 'hc-up__foot',
          children:
            '判定顺序：先看是不是 Hermes venv / runtime 解释器 → 网关 → 桌面 serve → worker/CLI。' +
            '前两类永远不可清场；「不可清场」的进程连按钮都不给，避免误点。'
        },
        'foot'
      )
    ]
  })

  // 就绪检查单独失败时：不遮进程表，只在上面挂一条错误 + 重试
  var readyError = null
  if (readyQuery.status === 'error') {
    readyError = jsx(
      ErrorState,
      {
        title: '就绪检查没读到（进程表仍然可用）',
        message: readyQuery.error,
        onRetry: readyQuery.reload,
        retryLabel: '重试就绪检查'
      },
      'ready-error'
    )
  }

  return jsxs('div', {
    className: 'hc-up',
    'data-upgrade-state': 'ok',
    children: [bar, readyError, cards, ops, table]
  })
}
