/**
 * hermes-console · 项目分组「行内同意归入」与「按钮为什么点不动」（t_0c8ab8c1 A 块）。
 *
 * 从 39KB 的 autogroup.js 里拆出来的两个小组件，解决老板 2026-10-30 亲测的两句话：
 *   「好像并没有看见同意这些建议的地方」→ :func:`AcceptRowButton`：每条建议行一个
 *       「同意并归入」，点了先出**二次确认（写明 from→to）**，确认才写；
 *   「我点击好像没啥反应」→ :func:`assignBlockReason`：按钮 disabled 时**必须把原因写在旁边**
 *       （未勾选 / 未选目标项目 / 写入中），不许点了没反应也没提示。
 *
 * 三条规矩（与 autogroup.js 文件头同一套）：
 *   1. 确认前不写：二次确认里明写「当前 → 归入」，取消就什么都不写；
 *   2. 回读才算数：写完立刻 GET /autogroup/suggestions?status=applied 逐条比
 *      slug/status/batch_id，请求 200 不当成功；
 *   3. 失败要能看出是哪条：错误文案带上会话标题与后端原因，不吞。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

import { get, send } from '../lib/rest.js'

/** 回读一次拉多少条 applied 行（后端把 limit 钳到 1..200）。 */
var READBACK_LIMIT = 200

/**
 * 「预览归入」为什么点不动 —— 空串 = 能点。
 * @param {{busy?: boolean, selectedCount?: number, targetSlug?: string}} props
 * @returns {string}
 */
export function assignBlockReason(props) {
  if (props && props.busy) return '正在写入，等这一批回读完了再点'
  if (!props || !props.selectedCount) return '还没勾选会话：先勾左侧复选框（或用「全选本页」）'
  if (!props.targetSlug) return '还没选目标项目：先在右边下拉里选一个项目'
  return ''
}

/** 回读：把这一条会话在 applied 列表里的现状捞出来（没有就是 null）。 */
function readbackOne(sessionId) {
  return get('/autogroup/suggestions', { params: { status: 'applied', limit: READBACK_LIMIT } }).then(
    function (res) {
      if (!res || res.ok === false) {
        return { ok: false, message: (res && res.message) || '回读请求失败' }
      }
      var items = (res.data && res.data.data && res.data.data.items) || []
      var got = null
      items.forEach(function (row) {
        if (row && row.session_id === sessionId) got = row
      })
      return { ok: true, row: got }
    }
  )
}

/**
 * 单条「同意并归入」：点 → 二次确认（from→to）→ 写 applied → 立刻回读 → 行变「已归入」。
 *
 * @param {{item: object, busy?: boolean, accepted?: object, onAccepted: Function}} props
 */
export function AcceptRowButton(props) {
  var item = props.item || {}
  var slug = item.project_slug || ''
  var toName = item.project_name || slug
  var fromName = item.project_name || slug || '未归入'
  var fromStatus = item.status || 'suggested'
  var phaseState = useState('idle')
  var phase = phaseState[0]
  var noteState = useState(null)
  var note = noteState[0]
  var accepted = props.accepted || null

  function write() {
    phaseState[1]('writing')
    noteState[1](null)
    send('/autogroup/assign', {
      slug: slug,
      session_ids: [item.session_id],
      confirm: true,
      sync_cwd: false,
      reason: '面板单条同意并归入'
    }).then(function (res) {
      if (!res || res.ok === false) {
        phaseState[1]('failed')
        noteState[1]({ ok: false, text: '写入失败：' + ((res && res.message) || '后端没有给原因') })
        return
      }
      var payload = res.data || {}
      var data = payload.data || {}
      var batch = data.batch_id || ''
      var opId = data.op_id || ''
      var skipped = (data.skipped || []).length > 0
      readbackOne(item.session_id).then(function (again) {
        if (!again.ok) {
          phaseState[1]('failed')
          noteState[1]({ ok: false, text: '写完了但回读失败：' + again.message })
          return
        }
        var got = again.row
        var ok = !!got && got.project_slug === slug && got.status === 'applied' &&
          (!batch || got.batch_id === batch)
        if (!ok) {
          phaseState[1]('failed')
          noteState[1]({
            ok: false,
            text: '回读不一致：期望 ' + slug + ' / applied，实际 ' +
              (got ? (got.project_slug || '—') + ' / ' + got.status : '回读没有这一行')
          })
          return
        }
        phaseState[1]('done')
        noteState[1]({
          ok: true,
          text: '回读一致：' + fromName + ' → ' + (got.project_name || toName) + '（status=' +
            got.status + (skipped ? '，原本就在这个项目' : '') + '）· 批次 ' + batch +
            (opId ? ' · op ' + opId : '')
        })
        if (typeof props.onAccepted === 'function') {
          props.onAccepted(item.session_id, {
            slug: slug,
            to_name: got.project_name || toName,
            batch_id: batch,
            op_id: opId,
            text: '已归入 ' + (got.project_name || toName)
          })
        }
      })
    })
  }

  if (accepted) {
    var full = (note && note.text) || '（回读一致）'
    var shortBatch = (accepted.batch_id || '').slice(-6)
    return jsxs('div', {
      className: 'hc-agg__cell hc-agg__cell--ops hc-agg__cell--ops-stack',
      'data-agg-accepted': item.session_id || '',
      title: full,
      children: [
        jsx('span', { className: 'hc-agg__project', children: accepted.text || '已归入' }),
        jsx('span', {
          className: 'hc-agg__msg',
          'data-agg-accept-note': item.session_id || '',
          children: '回读一致' + (shortBatch ? ' · 批次 …' + shortBatch : '')
        })
      ]
    })
  }


  var blocked = !slug ? '这条建议没有目标项目（project_slug 为空）：先用右侧下拉手动选一个项目' : ''
  var writing = phase === 'writing'

  if (phase === 'confirm') {
    return jsxs('div', {
      className: 'hc-agg__cell hc-agg__cell--ops hc-agg__cell--ops-stack',
      'data-agg-accept-confirm': item.session_id || '',
      children: [
        jsx('div', {
          className: 'hc-agg__hint',
          'data-agg-accept-from-to': item.session_id || '',
          children: fromName + '（' + fromStatus + '）→ ' + toName + '（applied）'
        }),
        jsxs('div', {
          className: 'hc-agg__toolbar',
          children: [
            jsx('button', {
              type: 'button',
              className: 'hc-btn hc-btn--accent hc-btn--tiny',
              'data-agg-accept-go': item.session_id || '',
              onClick: write,
              children: '确认归入'
            }),
            jsx('button', {
              type: 'button',
              className: 'hc-btn hc-btn--tiny',
              'data-agg-accept-cancel': item.session_id || '',
              onClick: function () {
                phaseState[1]('idle')
                noteState[1](null)
              },
              children: '取消（什么都不写）'
            })
          ]
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-agg__cell hc-agg__cell--ops hc-agg__cell--ops-stack',
    'data-agg-accept-state': phase,
    children: [
      jsx('button', {
        type: 'button',
        className: 'hc-btn hc-btn--accent hc-btn--tiny',
        'data-agg-accept': item.session_id || '',
        disabled: !!blocked || writing,
        title: blocked || ('把这条会话按建议归入「' + toName + '」：先出二次确认，确认后才写'),
        onClick: function () {
          phaseState[1]('confirm')
        },
        children: writing ? '写入中…' : '同意并归入'
      }),
      blocked
        ? jsx('span', { className: 'hc-agg__warn', 'data-agg-accept-blocked': item.session_id || '', children: blocked })
        : null,
      note
        ? jsx('span', {
            className: note.ok ? 'hc-agg__msg' : 'hc-agg__warn',
            'data-agg-accept-note': item.session_id || '',
            children: note.text
          })
        : null
    ]
  })
}
