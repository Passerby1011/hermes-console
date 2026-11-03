/**
 * hermes-console · 「AI 分类」面板：**扫描+AI 分析 → 预览 → 逐条取消 → 确认写入 → 回读 → 撤销**。
 *
 * ## 路由归属（t_3c2817b1 重接线；旧的三条 `/autogroup/ai/*` 写路由已在后端删除）
 *
 * | 动作 | 现在调用 | 为什么 |
 * |---|---|---|
 * | AI 分类 | `POST /autogroup/scan {ai:true, scope, agent, limit}` + 轮询 `GET /autogroup/job/{id}` | 后端 `_ai_classify()` 走同一个引擎（`autogroup_ai.build_preview` + `autogroup_model.make_caller`），且 scan 与 `/autogroup/ai/scope` **共用 `_candidate_sessions`** ⇒ AI 分析的候选集与扫描候选集必然是同一批，不会各扫一套 |
 * | 预览 | `GET /autogroup/suggestions?status=suggested`（AI 结果已由 scan 落成 suggested 行） | 建议表就是预览真相源；scan 的 job result 里也有逐条 items |
 * | 确认写库 | `POST /autogroup/preview {session_ids}`（200、零写入）→ `POST /autogroup/apply {confirm:true, session_ids}` | apply 是后端唯一写路径，自带看板硬排除 + 置信度阈值 + 写后回读 |
 * | 回读 | `GET /autogroup/suggestions?status=applied` + `GET /autogroup/status` | 不信 apply 的返回值 |
 * | 撤销 | `POST /autogroup/undo {batch_id}` | apply 回 `batch_id`；undo 内部先查面板台账（逐字段还原），没有台账则 `core.undo_batch` |
 * | 新建项目 | `POST /autogroup/projects {name, slug}` | 建项目能力在 panel 路由 |
 *
 * 六条硬规矩（都是卡面要求，别在改动时丢掉）：
 *   1. **确认前零写入**：跑批只产 suggested 行；归属 applied / cwd / 目录一个字节都不动；
 *   2. **limit 必须显式传**：`0` = 全量，`N` = 分批。引擎默认 40 是历史兼容值，界面不许沿用；
 *      「已处理 N / 共 total_available」两个数**都来自后端返回体**，前端不估算、不硬编码；
 *   3. **分批不重复**：下一批把上一批的 session_id 作为 `skip_ids` 带回去；
 *   4. **逐条可取消**：取消的条目只是不进 `session_ids`，界面上明确写「本次不写入」；
 *   5. **无法判定不许静默丢 / 没有模型要说原因**：`no_model_configured` 原样显示后端 reason；
 *   6. 颜色只走 var(--ui-*)，本文件不写字面色值（构建脚本会检查）。
 */
import { jsx as jsxRuntime, jsxs as jsxsRuntime } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { num } from '../lib/format.js'
import { useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState } from '../lib/states.js'

/**
 * React 19 的 jsx runtime **不接受 props 里的 key**（会打 "key prop is being spread" 警告），
 * 而本文件里大量静态兄弟节点写成 `{ key: 'x', ... }`。这两个薄包装把 key 挪到第三参数，
 * 两种写法（props.key / 第三参数）都支持 —— 只是为了避免满屏 `}, 'x')` 的噪音。
 */
function splitKey(props) {
  if (!props || props.key === undefined) return [props, undefined]
  var next = {}
  Object.keys(props).forEach(function (name) {
    if (name !== 'key') next[name] = props[name]
  })
  return [next, props.key]
}

function jsx(type, props, key) {
  var pair = splitKey(props)
  return jsxRuntime(type, pair[0], key !== undefined ? key : pair[1])
}

function jsxs(type, props, key) {
  var pair = splitKey(props)
  return jsxsRuntime(type, pair[0], key !== undefined ? key : pair[1])
}

/** 轮询间隔：与扫描 job 同口径（≥2s）。 */
var POLL_MS = 2000

/** 轮询总上限：全量 67 条 ≈ 6 分钟，给足 30 分钟（超时只报「还在后台跑」，不谎称失败）。 */
var POLL_TIMEOUT_MS = 1800000

/** 分批的默认批大小（界面可改，1..200）。 */
var DEFAULT_BATCH = 10

/** 一行 6 列：会话 / 当前归属 / AI 建议 / 理由与证据 / 分档·确认后 / 操作。 */
var AI_COLS = 'minmax(0, 1.3fr) minmax(0, 0.7fr) minmax(0, 1fr) minmax(0, 1.5fr) 132px minmax(120px, 0.6fr)'

/**
 * AI 候选口径：`GET /autogroup/ai/scope?agent=X`（t_52125d5d）。
 *
 * 传了 agent 就是**只算这个 profile 的候选**（后端 `candidate_scope(agents={agent})`，
 * 见 t_bffc584b）；不传 = 全机器。界面上的「共 N 条可处理」必须跟着当前 Agent 变，
 * 否则切了 Agent 数字不动，用户会拿 A 的候选数去理解 B 的跑批。
 */
function fetchScope(agent) {
  var scoped = !!agent && agent !== '__all__'
  return get('/autogroup/ai/scope', scoped ? { params: { agent: agent } } : undefined)
}

/** 置信度 → 百分比文案（没有数字就写「无」，不猜）。 */
function confText(value) {
  if (value === null || value === undefined || typeof value !== 'number') return '无置信度'
  return Math.round(value * 100) + '%'
}

/**
 * 分档 → 一句话（确认后会写成什么）。
 *
 * 两套分档词汇必须都认：
 *   * 预览行（`POST /autogroup/preview`）：`apply` / `suggest` / `unjudged`
 *   * 扫描行（`POST /autogroup/scan {ai:true}`）：`high`(≥0.8) / `mid`(≥0.65) / `unjudged`
 * 只认第一套时，扫描来的行会全被写成「<0.5 · 无法判定（禁止写入）」——那是假话：
 * 它们其实是可写的（实测确认按钮直接写入了 2 条）。
 */
function tierText(row) {
  if (row.tier === 'apply' || row.tier === 'high') {
    return row.sync_cwd_if_confirmed ? '≥0.8 · 确认后 applied（并同步 cwd）' : '≥0.8 · 确认后 applied'
  }
  if (row.tier === 'mid') return '≥0.65 · 确认后 applied'
  if (row.tier === 'suggest') return '0.5–<0.8 · 确认后 suggested（不动 cwd）'
  return '<0.5 · 无法判定（禁止写入）'
}

/** 无法判定原因的中文归类（引擎给的是 code，这里只翻译，不编原因）。 */
var UNDECIDED_LABELS = {
  low_confidence: '低置信度',
  invalid_json: '模型输出无效（不是 JSON）',
  invalid_schema: '模型输出无效（字段不合规）',
  confidence_out_of_range: '模型输出无效（置信度越界）',
  unknown_project: '模型输出无效（项目不存在）',
  call_failed: '模型调用失败',
  brief_unavailable: '会话正文不可用',
  new_project_insufficient: '新项目候选支撑不足（降级为建议）'
}

function undecidedLabel(code) {
  return UNDECIDED_LABELS[code] || (code ? code : '无法判定')
}

function chip(text, key, title) {
  return jsx('span', { className: 'hc-chip', title: title || text, children: text }, key)
}

function notesBlock(notes, prefix) {
  if (!notes || !notes.length) return null
  return jsx('div', {
    key: 'notes-' + (prefix || 'n'),
    className: 'hc-agg__notes',
    children: notes.map(function (note, index) {
      return jsx('div', { className: 'hc-agg__note', children: note }, (prefix || 'n') + index)
    })
  })
}

function resultLine(name, result, key) {
  if (!result) return null
  return jsx('div', {
    key: key || name,
    className: 'hc-agg__scan-result',
    'data-aic-result': name,
    'data-aic-ok': result.ok ? 'true' : 'false',
    children: jsx('span', { children: (result.ok ? '✓ ' : '✗ ') + result.text })
  })
}

/** 一条会话的证据摘要：正文片段 + （新建候选时）支撑会话 id。 */
function evidenceText(row) {
  var evidence = row.evidence || {}
  var messages = (evidence.messages || []).map(function (m) { return m.content }).filter(Boolean)
  var parts = []
  if (messages.length) parts.push('正文：' + messages.join(' / '))
  else if (evidence.chars) parts.push('正文 ' + num(evidence.chars) + ' 字')
  var support = (row.suggested && row.suggested.support_session_ids) || []
  if (row.new_project) {
    parts.push('同主题支撑 ' + num((row.new_project.evidence_count || support.length || 0)) + ' 条' +
      (support.length ? '（' + support.slice(0, 3).join('、') + (support.length > 3 ? '…' : '') + '）' : ''))
  }
  return parts.join(' · ') || '（没有可用证据）'
}

/** 单条行：标题 / 当前归属 / AI 建议 / 置信度 / 理由与证据 / 分档 / 取消。 */
function AiRow(props) {
  var row = props.row
  var cancelled = !!props.cancelled
  var selectable = !!row.selectable && !cancelled
  var current = row.current || {}
  var suggested = row.suggested || {}
  return jsxs('div', {
    className: 'hc-agg__row',
    'data-aic-row': row.session_id,
    'data-aic-tier': row.tier || '',
    'data-aic-selectable': row.selectable ? 'true' : 'false',
    'data-aic-cancelled': cancelled ? 'true' : 'false',
    children: [
      jsxs('div', {
        key: 'title',
        className: 'hc-agg__cell hc-agg__cell--title',
        children: [
          jsx('input', {
            key: 'pick',
            type: 'checkbox',
            'data-aic-pick': row.session_id,
            checked: selectable && !props.cancelled,
            disabled: !row.selectable || props.locked,
            title: row.selectable ? '勾选=本次确认要写入；取消勾选=这次不写这一条' : (row.selectable_why || '这一条不可写'),
            onChange: function () { props.onToggle(row.session_id) }
          }),
          jsx('span', {
            key: 'title',
            className: 'hc-agg__title',
            title: row.session_id,
            children: row.title || row.session_id
          }),
          cancelled
            ? jsx('span', { key: 'tag', className: 'hc-agg__warn', 'data-aic-cancelled-tag': '1', children: '已取消（本次不写入）' })
            : null
        ]
      }),
      jsx('div', {
        key: 'current',
        className: 'hc-agg__cell hc-agg__cell--project',
        children: current.exists
          ? (current.slug || '—') + ' · ' + (current.status || '—')
          : '未归入（没有归属行）'
      }),
      jsxs('div', {
        key: 'suggested',
        className: 'hc-agg__cell hc-agg__cell--project',
        title: (suggested.slug || '') + ' ' + (suggested.reason || ''),
        children: [
          jsx('span', {
            key: 'name',
            className: 'hc-agg__project',
            children: (suggested.name || suggested.slug || '—') + (row.new_project ? '（新建）' : '')
          }),
          jsx('span', { key: 'conf', className: 'hc-chip', 'data-aic-conf': String(suggested.confidence), children: confText(suggested.confidence) })
        ]
      }),
      jsx('div', {
        key: 'reason',
        className: 'hc-agg__cell hc-agg__cell--basis',
        children: (suggested.reason || '（模型没给理由）') + ' · ' + evidenceText(row)
      }),
      jsxs('div', {
        key: 'tier',
        className: 'hc-agg__cell hc-agg__cell--agent',
        children: [
          jsx('div', { key: 'text', children: tierText(row) }),
          jsx('div', {
            key: 'if',
            className: 'hc-agg__cell--time',
            'data-aic-status-if': row.status_if_confirmed || '',
            children: row.status_if_confirmed ? '→ ' + row.status_if_confirmed : '不写入'
          })
        ]
      }),
      jsx('div', {
        key: 'ops',
        className: 'hc-agg__cell hc-agg__cell--ops-stack',
        children: jsx('button', {
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-aic-cancel': row.session_id,
          disabled: props.locked || !row.selectable,
          title: row.selectable
            ? (cancelled ? '恢复这一条（重新计入本次写入）' : '取消这一条：本次确认不写它')
            : (row.selectable_why || '这一条不可写'),
          onClick: function () { props.onToggle(row.session_id) },
          children: cancelled ? '恢复' : '取消这条'
        })
      })
    ]
  })
}
/** 逐条结果/回读的紧凑表（4 列：会话 / 结果 / 期望→现在 / 说明）。 */
function compactTable(spec) {
  var rows = spec.rows || []
  if (!rows.length) return null
  return jsxs('div', {
    className: 'hc-agg__table',
    style: { '--agg-cols': 'minmax(0, 1.1fr) minmax(0, 0.8fr) minmax(0, 1.2fr) minmax(0, 1.4fr)' },
    key: spec.key,
    'data-aic-table': spec.name,
    children: [
      jsxs('div', {
        className: 'hc-agg__head',
        children: spec.head.map(function (text, index) {
          return jsx('div', { className: 'hc-agg__cell hc-agg__cell--basis', children: text }, 'h' + index)
        })
      }),
      rows.map(function (row, index) {
        return jsxs('div', {
          className: 'hc-agg__row',
          'data-aic-table-row': row.key || 'r' + index,
          'data-aic-table-ok': row.ok === false ? 'false' : 'true',
          children: row.cells.map(function (cell, cellIndex) {
            return jsx('div', {
              className: 'hc-agg__cell hc-agg__cell--basis',
              title: typeof cell === 'string' ? cell : '',
              children: cell
            }, 'c' + cellIndex)
          })
        }, row.key || 'r' + index)
      })
    ]
  })
}

/** 一个预览批次：逐条清单 + 取消 + 确认 + 回读 + 撤销。 */
function AiBatchBlock(props) {
  var batch = props.batch
  var rows = batch.rows || []
  var locked = batch.phase === 'confirming' || batch.submitted
  var picked = rows.filter(function (row) {
    return row.selectable && batch.selected[row.session_id] && !batch.cancelled[row.session_id]
  })
  var cancelledCount = Object.keys(batch.cancelled).length
  var applyCount = picked.filter(function (row) { return row.status_if_confirmed === 'applied' }).length
  var suggestCount = picked.length - applyCount
  var statusChip = batch.status === 'done' ? '已写入（回读完成）'
    : batch.status === 'partial' ? '部分写入（见回读）'
      : batch.status === 'undone' ? '已撤销' : '待确认（未写入）'
  var llm = batch.llm || {}
  /**
   * 模型真实调用情况。后端 `llm.code` 四种取值各有各的话术（旧代码只认 `llm.used===false`
   * 一句「本次没用上」，把 `no_model_configured` 和 `ai_not_requested` 混成同一个笼统错误）。
   */
  var llmText = null
  var llmTone = 'off'
  if (llm.code === 'llm_used') {
    llmText = '模型真调用 ' + num(llm.calls) + ' 次 · ' + (llm.provider || '—') + '/' + (llm.model || '—') +
      (typeof llm.avg_ms === 'number' ? ' · 平均 ' + num(Math.round(llm.avg_ms)) + 'ms' : '')
    llmTone = 'on'
  } else if (llm.code === 'no_model_configured') {
    llmText = '没有可用模型：' + (llm.message || '既没有独立配置，该 Agent 的 config.yaml 里也没有模型')
    llmTone = 'warn'
  } else if (llm.code === 'llm_not_called') {
    llmText = '配了模型但一次都没调通：' + (llm.message || '见 degraded / error')
    llmTone = 'warn'
  } else if (llm.code === 'ai_not_requested') {
    llmText = '本次只跑关键词规则（ai_not_requested）'
  } else if (llm.code) {
    llmText = '模型状态 ' + llm.code + '：' + (llm.message || '')
    llmTone = 'warn'
  }

  function toggle(sessionId) {
    if (locked) return
    props.onToggle(batch.batch_id, sessionId)
  }

  return jsxs('div', {
    className: 'hc-agg__block',
    'data-aic-batch': batch.batch_id,
    'data-aic-batch-status': batch.status || 'pending',
    children: [
      jsxs('div', {
        key: 'head',
        className: 'hc-agg__block-head',
        children: [
          jsx('span', { key: 'title', className: 'hc-agg__block-title', children: '预览批次 ' + batch.batch_id }),
          jsx('span', {
            key: 'state',
            className: 'hc-chip ' + (batch.status === 'pending' ? 'hc-chip--off' : 'hc-chip--on'),
            'data-aic-batch-state': batch.status || 'pending',
            children: statusChip
          }),
          chip('本次 ' + num(rows.length) + ' 条 · 可写 ' + num(picked.length) + ' 条', 'cnt'),
          cancelledCount ? chip('已取消 ' + num(cancelledCount) + ' 条（本次不写入）', 'cancel') : null,
          llmText
            ? jsx('span', {
                key: 'llm',
                className: 'hc-chip' + (llmTone === 'on' ? ' hc-chip--on' : ' hc-chip--off'),
                'data-aic-batch-llm': llm.code || '',
                title: llm.message || '',
                children: llmText
              })
            : null,
          chip('批次标识即写入幂等键：重复确认会被后端 409 拒绝', 'idem')
        ]
      }),
      jsx('div', {
        key: 'hint',
        className: 'hc-agg__hint',
        children: batch.status === 'pending'
          ? '确认前没有写任何东西：归属表、cwd、目录都没动。勾选/取消只是本地选择，点「确认写入」才会落库。'
          : '本批次已提交过，界面不再允许重复提交（后端也会拒）。要重来请重新跑一批分类。'
      }),
      jsxs('div', {
        key: 'table',
        className: 'hc-agg__table',
        style: { '--agg-cols': AI_COLS },
        'data-aic-rows': String(rows.length),
        children: [
          jsxs('div', {
            key: 'head',
            className: 'hc-agg__head',
            children: [
              jsx('div', { key: 'c1', className: 'hc-agg__cell hc-agg__cell--title', children: '会话（勾选=要写，取消=不写）' }),
              jsx('div', { key: 'c2', className: 'hc-agg__cell hc-agg__cell--project', children: '当前归属' }),
              jsx('div', { key: 'c3', className: 'hc-agg__cell hc-agg__cell--project', children: 'AI 建议项目' }),
              jsx('div', { key: 'c4', className: 'hc-agg__cell hc-agg__cell--basis', children: '理由与证据' }),
              jsx('div', { key: 'c5', className: 'hc-agg__cell hc-agg__cell--agent', children: '分档 · 确认后' }),
              jsx('div', { key: 'c6', className: 'hc-agg__cell hc-agg__cell--ops', children: '操作' })
            ]
          }),
          rows.map(function (row) {
            return jsx(AiRow, {
              row: row,
              cancelled: !!batch.cancelled[row.session_id],
              locked: locked,
              onToggle: toggle
            }, batch.batch_id + ':' + row.session_id)
          })
        ]
      }),
      jsxs('div', {
        key: 'toolbar',
        className: 'hc-agg__toolbar',
        children: [
          jsx('button', {
            key: 'confirm',
            type: 'button',
            className: 'hc-btn hc-btn--accent',
            'data-aic-confirm': batch.batch_id,
            disabled: locked || !picked.length,
            title: '带 confirm=true 提交本批次选中的条目；后端逐条写前校验，任何一条不符就跳过并暂停整批',
            onClick: function () { props.onConfirm(batch) },
            children: batch.phase === 'confirming'
              ? '写入中…'
              : '确认写入 ' + num(picked.length) + ' 条（applied ' + num(applyCount) + ' / suggested ' + num(suggestCount) + '）'
          }),
          jsx('button', {
            key: 'pick-all',
            type: 'button',
            className: 'hc-btn',
            'data-aic-pick-all': batch.batch_id,
            disabled: locked,
            title: '只勾选当前可写的条目（不可写的永远不勾）',
            onClick: function () { props.onPickAll(batch.batch_id) },
            children: '全选可写'
          }),
          jsx('button', {
            key: 'clear',
            type: 'button',
            className: 'hc-btn',
            'data-aic-clear': batch.batch_id,
            disabled: locked,
            title: '全部取消：本批次一条都不写',
            onClick: function () { props.onClear(batch.batch_id) },
            children: '全部取消'
          }),
          jsx('button', {
            key: 'reload',
            type: 'button',
            className: 'hc-btn',
            'data-aic-reload': batch.batch_id,
            disabled: batch.phase === 'confirming',
            title: '重新读这个批次（可写性 / 过期提示会按现状刷新）',
            onClick: function () { props.onReload(batch.batch_id) },
            children: '刷新本批次'
          })
        ]
      }),
      resultLine('confirm', batch.feedback)
    ]
  })
}
/**
 * 写入结果 / 逐条回读 / 撤销恢复校验：确认之后才有内容。
 *
 * 数据形态来自**现存路由**（旧 `/autogroup/ai/confirm` 的三件套已不存在）：
 *   * `batch.result`   = `POST /autogroup/apply` 的响应体
 *     （`batch_id` / `write_result{applied,skipped_low_confidence,cwd_synced,missing_project_path,kanban_excluded}` /
 *      `readback{batch_sessions,batch_count,suggested,applied,rejected,by_project}` / `preview{items}`）；
 *   * `batch.readback` = 前端在写后自己重读 `suggestions` + `status` 得到的结果；
 *   * `batch.undo`     = `POST /autogroup/undo` 的响应体（`readback{undone_sessions,expected,back_to_suggested,still_applied,cwd_restored}`）。
 */
function AiBatchResults(props) {
  var batch = props.batch
  var result = batch.result
  var readback = batch.readback
  var undo = batch.undo
  if (!result && !readback && !undo) return null
  var children = []

  if (result) {
    var writeResult = result.write_result || {}
    var rb = result.readback || {}
    var applied = typeof writeResult.applied === 'number' ? writeResult.applied : 0
    var previewItems = (result.preview && result.preview.items) || []
    var outcomes = previewItems.map(function (item) {
      var willWrite = item.will_apply === true
      var inBatch = (rb.batch_sessions || []).indexOf(item.session_id) !== -1
      return {
        key: item.session_id,
        ok: willWrite ? inBatch : true,
        cells: [
          (item.title || item.session_id) + '（' + item.session_id + '）',
          willWrite ? (inBatch ? '已写入 applied' : '应写但回读里没有') : '未写入（跳过）',
          (item.project_name || item.project_slug || '—') + ' → ' +
            (item.will_sync_cwd ? 'cwd 同步' : 'cwd 未动'),
          item.skip_reason || (willWrite
            ? (inBatch ? '回读一致' : '回读未命中，以归属表为准，请刷新')
            : '置信度低于阈值或按硬约束跳过')
        ]
      }
    })
    children.push(jsxs('div', {
      key: 'write-summary',
      className: 'hc-agg__scan-result',
      'data-aic-write-summary': String(applied),
      children: [
        jsx('span', {
          key: 'text',
          children: '批次 ' + (result.batch_id || '—') +
            ' · 写入 applied ' + num(applied) + ' 条' +
            ' · 低于阈值跳过 ' + num(writeResult.skipped_low_confidence) + ' 条' +
            ' · 回读本批 ' + num(rb.batch_count) + ' 条 / 全库 applied ' + num(rb.applied) + ' 条'
        }),
        (writeResult.missing_project_path || []).length
          ? jsx('span', {
              key: 'missing',
              className: 'hc-agg__note',
              'data-aic-missing-path': String((writeResult.missing_project_path || []).length),
              children: '这些项目在 projects.db 里没有目录，同步 cwd 已跳过：' +
                (writeResult.missing_project_path || []).join('、')
            })
          : null,
        (writeResult.kanban_excluded || []).length
          ? jsx('span', {
              key: 'kanban',
              className: 'hc-agg__note',
              'data-aic-kanban-excluded': String((writeResult.kanban_excluded || []).length),
              children: '看板任务会话按硬约束未写入：' + num((writeResult.kanban_excluded || []).length) + ' 条'
            })
          : null,
        (result.notes || []).length
          ? jsx('span', {
              key: 'notes',
              className: 'hc-agg__note',
              children: (result.notes || []).join('；')
            })
          : null
      ]
    }))
    children.push(compactTable({
      key: 'write-table',
      name: 'write',
      head: ['会话', '结果', '写入目标', '说明'],
      rows: outcomes
    }))
  }

  if (readback) {
    var rbRows = (readback.batch_session_rows || []).map(function (row) {
      return {
        key: row.session_id,
        ok: row.status === 'applied',
        cells: [
          row.session_id,
          row.status || '—',
          (row.project_slug || '无归属') + (row.batch_id ? ' · 批次 ' + row.batch_id : ''),
          row.applied_at ? new Date(row.applied_at * 1000).toLocaleString() : '—'
        ]
      }
    })
    children.push(jsx('div', {
      key: 'readback-head',
      className: 'hc-agg__block-head',
      children: jsx('span', {
        className: 'hc-agg__block-title',
        children: '写后回读（重读 /autogroup/suggestions + /autogroup/status，不信 apply 的返回值）'
      })
    }))
    children.push(compactTable({
      key: 'readback-table',
      name: 'readback',
      head: ['会话', '现在状态', '项目 · 批次', 'applied_at'],
      rows: rbRows
    }))
    var summaryNow = readback.summary || {}
    children.push(jsx('div', {
      key: 'readback-verify',
      className: 'hc-agg__hint',
      'data-aic-readback-verify':
        String((readback.applied_now || []).length) + '/' + String(readback.expected || 0),
      children: '回读核对：期望写 ' + num(readback.expected) + ' 条 · 现在 applied ' +
        num((readback.applied_now || []).length) + ' 条 · 仍在待确认 ' +
        num((readback.still_suggested || []).length) + ' 条（全库 applied ' +
        num(summaryNow.applied) + ' / suggested ' + num(summaryNow.suggested) + '）' +
        ((readback.still_suggested || []).length ? ' · 有未落库的条目，见上表' : '')
    }))
  }

  if (undo) {
    var undoRb = undo.readback || {}
    var undone = undoRb.back_to_suggested || []
    var restored = undone.map(function (sid) {
      var stillApplied = (undoRb.still_applied || []).indexOf(sid) !== -1
      return {
        key: sid,
        ok: !stillApplied,
        cells: [
          sid,
          stillApplied ? '仍是 applied' : '已回到 suggested',
          'cwd 还原 ' + num(undoRb.cwd_restored) + ' 条（整批）',
          stillApplied ? '撤销未完全生效' : '逐字段还原到写入前整行'
        ]
      }
    })
    children.push(jsx('div', {
      key: 'undo-head',
      className: 'hc-agg__block-head',
      children: jsx('span', {
        className: 'hc-agg__block-title',
        children: '撤销恢复校验（' + ((undoRb.still_applied || []).length ? '部分未生效' : '已撤销') + '）'
      })
    }))
    children.push(compactTable({
      key: 'undo-table',
      name: 'undo',
      head: ['会话', '状态', 'cwd', '说明'],
      rows: restored
    }))
    children.push(jsx('div', {
      key: 'undo-verify',
      className: 'hc-agg__hint',
      'data-aic-undo-verify': String((undoRb.still_applied || []).length) + '/' +
        String(undoRb.expected || 0),
      children: '撤销核对：回到待确认 ' + num(undoRb.undone_sessions) + ' / ' + num(undoRb.expected) +
        ' 条 · 仍 applied ' + num((undoRb.still_applied || []).length) + ' 条 · cwd 还原 ' +
        num(undoRb.cwd_restored) + ' 条' +
        ((undo.rb_drift || []).length ? ' · 库内容哈希与撤销前不一致（见 notes）' : '')
    }))
    var undoNotes = undo.notes || []
    if (undoNotes.length) {
      children.push(jsx('div', { key: 'undo-notes', children: notesBlock(undoNotes, 'un') }))
    }
  }

  return jsxs('div', {
    className: 'hc-agg__block',
    'data-aic-results': batch.batch_id,
    children: children
  })
}
/** 统一取 `{ok, data:{...}}` 里的那一层（三种路由的包装深度不一样，不做假设）。 */
function unwrap(payload) {
  if (!payload) return {}
  if (payload.data && typeof payload.data === 'object' && !Array.isArray(payload.data)) {
    var inner = payload.data
    if (inner.data && typeof inner.data === 'object') return inner.data
    return inner
  }
  return payload
}

/** 后端错误 → 一句可读文本（优先 `error.message`，其次顶层 `message`）。 */
function errText(res, fallback) {
  if (!res) return fallback
  if (res.error && res.error.message) return res.error.message
  if (res.message) return res.message
  return fallback
}

/**
 * `POST /autogroup/preview` 的 items（**新形态**：`project_slug/project_name/confidence/basis/
 * will_apply/skip_reason`）→ 界面行。
 *
 * 这是 t_3c2817b1 重接线的关键映射：旧代码读的是已删路由 `/autogroup/ai/preview/{batch}` 的
 * `item.suggested / item.state / item.write_policy` 三件套；现存的 `/autogroup/preview` 给的
 * 是**同一份判定结果平铺**的字段。映射规则：
 *   * `suggested.slug  ← project_slug`，`suggested.name ← project_name`
 *   * `current.slug    ← 该会话此刻在归属表里的项目（后端没直接给，只能用 title/profile 之外的
 *      `basis.kind` 推断不可靠 ⇒ 明确留空并写「见归属表」，不编造）`
 *   * `selectable       ← will_apply`（后端已经按置信度阈值 + 看板硬排除算好）
 *   * `selectable_why   ← skip_reason`（后端原话）
 */
function rowsFromPreview(data, previous) {
  var old = {}
  ;(previous || []).forEach(function (row) { old[row.session_id] = row })
  return ((data && data.items) || []).map(function (item) {
    var kept = old[item.session_id] || {}
    var confidence = item.confidence
    var willApply = item.will_apply === true
    var basis = item.basis || {}
    return {
      session_id: item.session_id,
      title: item.title || kept.title || '',
      current: kept.current || { slug: '', status: '', exists: false, unknown: true },
      suggested: {
        slug: item.project_slug,
        name: item.project_name || item.project_slug,
        confidence: confidence,
        reason: basis.reason || (basis.label || ''),
        support_session_ids: [],
        support_count: null
      },
      new_project: null,
      target_slug: item.project_slug,
      target_exists: !!item.target_path,
      tier: willApply
        ? (typeof confidence === 'number' && confidence >= 0.8 ? 'high' : 'mid')
        : (item.kanban ? 'blocked' : 'unjudged'),
      status_if_confirmed: willApply ? 'applied' : null,
      sync_cwd_if_confirmed: !!item.will_sync_cwd,
      can_write: willApply,
      selectable: willApply,
      selectable_why: item.skip_reason || (item.kanban
        ? '看板任务会话（source=kanban）：按硬约束不得进入普通项目'
        : '后端判定本条不写入'),
      stale_warning: '',
      unjudged_reason: willApply ? null : (item.skip_reason || '不写入'),
      undecided_code: kept.undecided_code || null,
      evidence: kept.evidence || null
    }
  })
}

/**
 * `POST /autogroup/scan {ai:true}` 的 job result items → 界面行。
 *
 * scan 的 items 是**从建议表回读**的行（`session_id / project_slug / project_name / confidence /
 * reason / source / basis / status`），也就是「已经落成 suggested」的那一批。这里把它翻成预览行：
 *   * `selectable` 由置信度阈值（后端 `core.MIN_CONFIDENCE`=0.65）+ 有项目 决定 ——
 *     **只是界面预判**，真正的阈值判定在 `POST /autogroup/apply` 里再算一遍（不靠前端说了算）；
 *   * 没有 project_slug 的行（AI 判不出）一律不可写，进「无法判定清单」。
 */
function rowsFromScanResult(result, processed) {
  var already = processed || {}
  var threshold = 0.65
  return ((result && result.items) || []).map(function (item) {
    var confidence = item.confidence
    var hasProject = !!item.project_slug
    var low = typeof confidence !== 'number' || confidence < threshold
    var judged = hasProject && !low
    var basis = item.basis || {}
    return {
      session_id: item.session_id,
      title: item.title || '',
      current: { slug: '', status: '', exists: false, unknown: true },
      suggested: {
        slug: item.project_slug,
        name: item.project_name || item.project_slug,
        confidence: confidence,
        reason: item.reason || basis.reason || basis.label || '',
        support_session_ids: [],
        support_count: null
      },
      new_project: null,
      target_slug: item.project_slug,
      target_exists: hasProject,
      tier: judged ? (confidence >= 0.8 ? 'high' : 'mid') : 'unjudged',
      status_if_confirmed: judged ? 'applied' : null,
      sync_cwd_if_confirmed: false,
      can_write: judged,
      selectable: judged,
      selectable_why: judged
        ? ''
        : (!hasProject
            ? 'AI 没有给出目标项目（判不出），不会写入'
            : '置信度 ' + (typeof confidence === 'number' ? confidence : '—') +
              ' 低于阈值 ' + threshold + '，不会写入'),
      stale_warning: '',
      unjudged_reason: judged ? null : (!hasProject ? 'AI 判不出目标项目' : '置信度低于阈值 ' + threshold),
      undecided_code: judged ? null : (hasProject ? 'low_confidence' : 'undetermined'),
      evidence: { kind: item.source, already_processed: !!already[item.session_id] },
      source: item.source
    }
  })
}

/** 读 applied 建议（回读用）。 */
function fetchAppliedRows() {
  return get('/autogroup/suggestions', { params: { status: 'applied', limit: 200 } })
}

/** 读 suggested 建议（回读用）。 */
function fetchSuggestedRows() {
  return get('/autogroup/suggestions', { params: { status: 'suggested', limit: 200 } })
}

/** 读汇总（回读用）。 */
function fetchStatus() {
  return get('/autogroup/status')
}

/** 「AI 分类」面板：跑批 → 预览 → 逐条取消 → 确认 → 回读 → 撤销。 */
export function AutogroupAiPanel(props) {
  /**
   * 当前 Agent（t_52125d5d）：换 agent 就换 query key ⇒ 候选口径必然重算，
   * 不会拿上一个 Agent 的 `/autogroup/ai/scope` 缓存继续显示。
   */
  var agentKey = props.agent || ''
  var scopeQuery = useQuery(
    'autogroup.ai.scope|' + agentKey,
    function () {
      return fetchScope(props.agent)
    },
    { staleMs: 15000 }
  )
  var batchSizeState = useState(DEFAULT_BATCH)
  var jobState = useState({ phase: 'idle', jobId: '', progress: null, error: '' })
  var batchesState = useState([])
  var processedState = useState({})
  var feedbackState = useState(null)
  var job = jobState[0]
  var setJob = jobState[1]
  var batches = batchesState[0]
  var processed = processedState[0]
  /**
   * 当前 Agent 作用域里的已处理 id（t_52125d5d）：`processed` 是按 session_id 记的，
   * 切过 Agent 之后里面会混着别的 profile 的 id。计数与 skip_ids 都只认本作用域，
   * 否则「已处理 N / 共 total」里的 N 会比 total 大（两个数一个跨 Agent 一个不跨）。
   */
  var scopeIds = props.scopeIds || null
  var scopedProcessedIds = Object.keys(processed).filter(function (id) {
    return !scopeIds || !!scopeIds[id]
  })
  var scope = (scopeQuery.data && scopeQuery.data.data) || {}
  var total = typeof scope.total_available === 'number' ? scope.total_available : null
  var processedCount = scopedProcessedIds.length
  var remaining = total === null ? null : Math.max(0, total - processedCount)
  var running = job.phase === 'starting' || job.phase === 'running'
  /**
   * 模式门（t_f2338eb2）：AI 分类引擎的候选集**硬排除已 applied 会话**（见 scope.excluded_applied），
   * 也就是它天然只补未分组/待确认会话。全量重建模式下继续跑批会与「重建全部项目结构」口径矛盾，
   * 所以这里停用跑批并说明原因 —— 全量重建走上方「先预览」。
   */
  var modeBlocked = props.modeBlocked === true
  /**
   * 共用勾选（t_8754de3f）：勾选状态的唯一真相源在父层 `AutogroupSection`，这里只读不改。
   *   · 勾了 N 条 → 按钮「分析已选 N 条」，请求体带 `session_ids`（只处理这批）；
   *   · 没勾 → 按钮「分析全部未分组 N 条」，请求体不带 `session_ids`（与改前逐字段一致）。
   * 文案与扫描面板同源同口径，两处不会再各说一套。
   */
  var selectedIds = Array.isArray(props.selectedIds) ? props.selectedIds : []
  var selectedCount = selectedIds.length
  var ungroupedCount = typeof props.ungroupedCount === 'number' ? props.ungroupedCount : null
  /**
   * 按钮上的 N（t_bba9216d）：**优先本面板自己读到的 `/autogroup/ai/scope` 总数**，
   * 其次父层传下来的同一个后端数字，最后才是 null（不冒充 0）。
   * 两者同源（同一个后端函数、同一批库），所以「按钮 N」与「点下去符合条件 N」必然相等；
   * 以前这里是父层数会话列表（只取最近 100 条）而跑批吃后端候选集，两个数对不上。
   */
  var scopeTotal = typeof total === 'number' ? total : null
  var shownCount = scopeTotal
  if (shownCount == null && ungroupedCount != null) shownCount = ungroupedCount
  var analyzeLabel = selectedCount
    ? '分析已选 ' + num(selectedCount) + ' 条'
    : '分析全部未分组 ' + num(shownCount == null ? 0 : shownCount) + ' 条'

  function notify(ok, text) {
    // 清空提示时（notify(null)）必须把整条提示置空：只传 ok=null 会渲染成「✗ undefined」
    feedbackState[1](text ? { ok: ok, text: text } : null)
  }

  function patchBatch(batchId, patch) {
    batchesState[1](function (current) {
      return current.map(function (batch) {
        if (batch.batch_id !== batchId) return batch
        var next = {}
        Object.keys(batch).forEach(function (key) { next[key] = batch[key] })
        Object.keys(patch || {}).forEach(function (key) { next[key] = patch[key] })
        return next
      })
    })
  }

  /**
   * 跑一批 AI 分类：**POST /autogroup/scan {ai:true}** + 轮询 job。
   *
   * 为什么不复用旧 `/autogroup/ai/classify`：那条路由已随后端 `api_autogroup_ai_run.py` 一起删除
   * （重启后必 404）。scan 的 AI 分支 `_ai_classify()` 调的是**同一个引擎**
   * （`autogroup_ai.build_preview` + `autogroup_model.make_caller`），且候选集来自
   * `_candidate_sessions` —— 与 `/autogroup/ai/scope` 显示的总数是同一个口径，所以
   * 「已处理 N / 共 total」仍然成立，不会出现两套候选。
   *
   * limit 显式给（0 = 全量）；分批时把已处理的 id 作为 `skip_ids` 带回去，不重复处理。
   */
  function startClassify(limit, opts) {
    if (running) return
    /**
     * 勾选透传（t_8754de3f）：
     *   · 勾了 → 只分析这批：`session_ids` 进请求体，`limit=0`（整批一次跑完）、`scope='all'`
     *     （勾选优先于 A 模式对 applied 的硬排除）、**不传 skip_ids**（用户点名要处理的不能被「已处理过」挡掉）；
     *   · `opts.all === true` → 用户点的「改为分析全部未分组」显式入口，忽略勾选；
     *   · 没勾且不是 all → 与改前逐字段一致。
     */
    var explicitAll = !!(opts && opts.all === true)
    var picked = explicitAll ? [] : selectedIds.slice()
    /**
     * `skip_ids`（分批去重）也只发当前 Agent 作用域内的 id（t_52125d5d）：
     * 用上面按 agent 收口过的 `scopedProcessedIds`，带进请求体就一定是本 Agent 的会话。
     */
    var skipIds = scopedProcessedIds.slice()
    var payload = {
      limit: picked.length ? 0 : limit,
      ai: true,
      scope: picked.length ? 'all' : 'ungrouped'
    }
    if (picked.length) payload.session_ids = picked
    else if (limit > 0) payload.skip_ids = skipIds
    if (props.agent && props.agent !== '__all__') payload.agent = props.agent
    setJob({ phase: 'starting', jobId: '', progress: null, error: '', startedAt: Date.now() })
    notify(null)
    send('/autogroup/scan', payload).then(function (res) {
      if (!res || res.ok === false) {
        setJob({
          phase: 'failed',
          jobId: '',
          progress: null,
          error: (res && (res.message || (res.error && res.error.message))) || '跑批请求失败',
          startedAt: Date.now()
        })
        return
      }
      var body = res.data || {}
      var jobId = body.job_id || (body.job && body.job.id) || (body.data && body.data.job_id) || ''
      if (!jobId) {
        setJob({ phase: 'failed', jobId: '', progress: null,
                 error: '后端没有返回 job_id：' + JSON.stringify(body).slice(0, 160),
                 startedAt: Date.now() })
        return
      }
      setJob({ phase: 'running', jobId: jobId, progress: { done: 0, total: 0 }, error: '',
               startedAt: Date.now() })
    })
  }

  /**
   * job 完成：把这一次 scan 的 AI 结果落成界面上的预览批次。
   *
   * 后端 scan 已经把结果写进建议表（status=suggested），所以这里**只读地**把逐条建议
   * 转成界面行（`rowsFromScanResult`）。**一条都不写**：写库只能走「确认写入」
   * → `POST /autogroup/apply {confirm:true}`。
   *
   * `llm` 块（`llm.used` / `llm.code` / `llm.provider` / `llm.model` / `llm.calls`）原样保留，
   * 界面据此显示「模型真调了几次、用的是哪个 provider/model」，以及 `no_model_configured` 的原因。
   */
  function onJobDone(result) {
    var rows = rowsFromScanResult(result, processed)
    var selected = {}
    var nextProcessed = {}
    Object.keys(processed).forEach(function (key) { nextProcessed[key] = true })
    rows.forEach(function (row) {
      nextProcessed[row.session_id] = true
      if (row.selectable) selected[row.session_id] = true
    })
    processedState[1](nextProcessed)
    if (!rows.length) {
      notify(
        result.empty === true || result.candidates === 0,
        result.empty_reason ||
          (result.ai_error && (result.ai_error.message || result.ai_error.code)) ||
          '本次没有任何可分类的会话（候选 ' + num(result.candidates || 0) + ' 条）'
      )
      scopeQuery.reload()
      return
    }
    var batch = {
      batch_id: job.jobId || ('scan-' + (result.elapsed_ms || 0)),
      status: 'pending',
      phase: 'preview',
      submitted: false,
      rows: rows,
      selected: selected,
      cancelled: {},
      progress: job.progress || {},
      engine: { model: result.llm ? { provider: result.llm.provider, model: result.llm.model } : null },
      llm: result.llm || {},
      summary: { scanned: result.scanned, matched: result.matched, saved: result.saved },
      excluded: rows.filter(function (row) { return !row.selectable })
        .map(function (row) { return { session_id: row.session_id, why: row.selectable_why } }),
      result: null,
      readback: null,
      undo: null,
      feedback: null
    }
    batchesState[1](function (current) { return [batch].concat(current) })
    scopeQuery.reload()
  }

  // 轮询：deps 只用基本类型，进度更新不会重起 effect。
  useEffect(
    function () {
      if (job.phase !== 'running' || !job.jobId) return undefined
      var jobId = job.jobId
      var cancelled = false
      var timer = null
      var startedAt = Date.now()
      function tick() {
        get('/autogroup/job/' + encodeURIComponent(jobId)).then(function (res) {
          if (cancelled) return
          if (!res || res.ok === false) {
            setJob({ phase: 'failed', jobId: jobId, progress: null, error: (res && res.message) || '查询跑批进度失败' })
            return
          }
          var body = res.data || {}
          var data = body.data || body
          var state = body.state || data.state || ''
          var progress = body.progress || data.progress || null
          if (state === 'failed') {
            var failure = body.error || data.error || {}
            setJob({ phase: 'failed', jobId: jobId, progress: progress,
              error: (failure && failure.message) || '跑批失败（后端没给原因）' })
            return
          }
          if (state === 'done') {
            setJob({ phase: 'done', jobId: jobId, progress: progress, error: '' })
            onJobDone(data.result || {})
            return
          }
          if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
            setJob({ phase: 'failed', jobId: jobId, progress: progress,
              error: '轮询超过 30 分钟仍未完成（job ' + jobId + ' 可能还在后台跑，稍后刷新看结果）' })
            return
          }
          setJob({ phase: 'running', jobId: jobId, progress: progress, error: '' })
          timer = setTimeout(tick, POLL_MS)
        })
      }
      tick()
      return function () {
        cancelled = true
        if (timer) clearTimeout(timer)
      }
    },
    [job.phase, job.jobId]
  )
  function findBatch(batchId) {
    var found = null
    batches.forEach(function (batch) {
      if (batch.batch_id === batchId) found = batch
    })
    return found
  }

  /** 单条取消/恢复：只是本地选择，确认时按 selected 提交。 */
  function toggleRow(batchId, sessionId) {
    var batch = findBatch(batchId)
    if (!batch) return
    var cancelled = {}
    var selected = {}
    Object.keys(batch.cancelled).forEach(function (key) { cancelled[key] = true })
    Object.keys(batch.selected).forEach(function (key) { selected[key] = true })
    if (cancelled[sessionId]) {
      delete cancelled[sessionId]
      selected[sessionId] = true
    } else {
      cancelled[sessionId] = true
      delete selected[sessionId]
    }
    patchBatch(batchId, { cancelled: cancelled, selected: selected })
  }

  function pickAll(batchId) {
    var batch = findBatch(batchId)
    if (!batch) return
    var selected = {}
    batch.rows.forEach(function (row) {
      if (row.selectable) selected[row.session_id] = true
    })
    patchBatch(batchId, { selected: selected, cancelled: {} })
  }

  function clearAll(batchId) {
    var batch = findBatch(batchId)
    if (!batch) return
    var cancelled = {}
    batch.rows.forEach(function (row) {
      if (row.selectable) cancelled[row.session_id] = true
    })
    patchBatch(batchId, { selected: {}, cancelled: cancelled })
  }

  /**
   * 重新读批次：可写性/状态按**建议表现状**刷新（不认本地缓存）。
   *
   * 旧实现读 `/autogroup/ai/preview/{batch}`（已删）。现在改成两路现存读接口：
   *   * `POST /autogroup/preview {session_ids}` —— 200 + 零写入，给逐条「会写/会跳过 + 原因」；
   *   * `GET  /autogroup/suggestions?status=suggested` —— 建议表，确认哪几条还在。
   */
  function reloadBatch(batchId) {
    var batch = findBatch(batchId)
    var ids = (batch ? batch.rows : []).map(function (row) { return row.session_id })
    if (!ids.length) return
    send('/autogroup/preview', { session_ids: ids }).then(function (res) {
      if (!res || res.ok === false) {
        patchBatch(batchId, {
          feedback: { ok: false, text: '刷新批次失败：' + errText(res, '后端没给原因') }
        })
        return
      }
      var data = unwrap(res)
      var rows = rowsFromPreview(data, batch ? batch.rows : [])
      var selected = {}
      if (batch) {
        rows.forEach(function (row) {
          if (row.selectable && batch.selected[row.session_id] && !batch.cancelled[row.session_id]) {
            selected[row.session_id] = true
          }
        })
      }
      patchBatch(batchId, { rows: rows, selected: selected })
    })
  }

  /**
   * 提交后无论成功失败都回读：**请求 200 不当验收**。
   *
   * 旧实现读 `/autogroup/ai/readback/{batch}`（已删）。现在按批次号去读三个现存接口：
   *   * `GET /autogroup/suggestions?status=applied&limit=200` —— 这批里哪些真的变成 applied；
   *   * `GET /autogroup/suggestions?status=suggested&limit=200` —— 哪些还停在待确认；
   *   * `GET /autogroup/status` —— 汇总数（applied / suggested / rejected）。
   */
  function refreshAfterWrite(batchId) {
    var batch = findBatch(batchId)
    var ids = (batch && batch.result && batch.result.readback && batch.result.readback.batch_sessions) || []
    if (!ids.length && batch) {
      ids = batch.rows.filter(function (row) {
        return batch.selected[row.session_id] && !batch.cancelled[row.session_id]
      }).map(function (row) { return row.session_id })
    }
    var wanted = {}
    ids.forEach(function (id) { wanted[id] = true })
    Promise.all([fetchAppliedRows(), fetchSuggestedRows(), fetchStatus()]).then(function (results) {
      var applied = unwrap(results[0]).items || []
      var suggested = unwrap(results[1]).items || []
      var summary = unwrap(results[2]).data || {}
      var nowApplied = applied.filter(function (row) { return wanted[row.session_id] })
      var stillSuggested = suggested.filter(function (row) { return wanted[row.session_id] })
      patchBatch(batchId, {
        readback: {
          expected: ids.length,
          applied_now: nowApplied.map(function (row) { return row.session_id }),
          still_suggested: stillSuggested.map(function (row) { return row.session_id }),
          summary: { applied: summary.applied, suggested: summary.suggested, rejected: summary.rejected },
          batch_session_rows: nowApplied.map(function (row) {
            return {
              session_id: row.session_id,
              project_slug: row.project_slug,
              status: row.status,
              batch_id: row.batch_id,
              applied_at: row.applied_at
            }
          })
        },
        status: stillSuggested.length ? 'partial' : 'done'
      })
    })
    scopeQuery.reload()
  }

  /**
   * 确认写入：**唯一写入口**。两段式：
   *   ① `POST /autogroup/preview {session_ids}` —— 200、零写入，拿到「会写几条 / 跳过几条及原因」；
   *   ② `POST /autogroup/apply {confirm:true, session_ids}` —— 真写，自带看板硬排除 + 置信度阈值 + 写后回读。
   *
   * 旧实现打 `/autogroup/ai/confirm`（已删；该路由当初才带「逐条写前校验 + 暂停」语义，
   * 现在这部分由后端 apply 的 `kanban_excluded` / `all_below_threshold` / 回读三件套承担）。
   */
  function confirmBatch(batch) {
    var picked = batch.rows.filter(function (row) {
      return row.selectable && batch.selected[row.session_id] && !batch.cancelled[row.session_id]
    }).map(function (row) { return row.session_id })
    if (!picked.length) return
    patchBatch(batch.batch_id, { phase: 'confirming', feedback: null })
    notify(null)
    // ① 预览（零写入）——失败就停，绝不在看不清的情况下写库
    send('/autogroup/preview', { session_ids: picked, sync_cwd: false }).then(function (pre) {
      if (!pre || pre.ok === false) {
        patchBatch(batch.batch_id, {
          phase: 'preview',
          feedback: { ok: false, text: '写入前的预览失败，已中止（一条都没写）：' + errText(pre, '后端没给原因') }
        })
        return
      }
      var previewData = unwrap(pre)
      patchBatch(batch.batch_id, { preflight: previewData })
      // ② 真写
      send('/autogroup/apply', { confirm: true, session_ids: picked, sync_cwd: false }).then(function (res) {
        var ok = !!(res && res.ok !== false)
        var result = ok ? unwrap(res) : null
        var rb = (result && result.readback) || {}
        var writeResult = (result && result.write_result) || {}
        patchBatch(batch.batch_id, {
          phase: 'preview',
          submitted: ok,
          result: result,
          feedback: {
            ok: ok,
            text: ok
              ? '已提交：写入 ' + num(writeResult.applied) + ' 条（批次 ' + (result.batch_id || '—') +
                '）· 回读 applied 本批 ' + num(rb.batch_count) + ' 条 / 剩余待确认 ' + num(rb.suggested) + ' 条' +
                (writeResult.skipped_low_confidence
                  ? ' · 置信度低于阈值跳过 ' + num(writeResult.skipped_low_confidence) + ' 条'
                  : '')
              : '提交失败：' + errText(res, '后端没有给原因') + '（一条都没写或已按台账回滚，见下方回读）'
          }
        })
        refreshAfterWrite(batch.batch_id)
        if (typeof props.onChanged === 'function') props.onChanged()
      })
    })
  }

  /**
   * 撤销本批次：`POST /autogroup/undo {batch_id}`。
   *
   * 撤销按批次号分流（后端）：先查面板写操作台账（`console_ops`，逐字段还原到写入前整行 + cwd 原值），
   * 没台账再走 `core.undo_batch`（suggested↔applied 互换 + cwd 还原）。两条都是**真还原**。
   */
  function undoBatch(batch) {
    var batchId = (batch.result && batch.result.batch_id) || batch.batch_id
    if (!batchId) {
      patchBatch(batch.batch_id, {
        feedback: { ok: false, text: '这个批次没有后端 batch_id（是扫描预览，不是写入批次），没有可撤销的写入' }
      })
      return
    }
    patchBatch(batch.batch_id, { phase: 'undoing' })
    send('/autogroup/undo', { batch_id: batchId }).then(function (res) {
      if (res && res.ok !== false) {
        var data = unwrap(res)
        var readback = data.readback || {}
        patchBatch(batch.batch_id, {
          phase: 'preview',
          undo: data,
          status: 'undone',
          feedback: {
            ok: (readback.still_applied || []).length === 0,
            text: '已撤销：回到待确认 ' + num(readback.undone_sessions) + ' / ' + num(readback.expected) +
              ' 条 · cwd 还原 ' + num(readback.cwd_restored) + ' 条' +
              ((readback.still_applied || []).length
                ? ' · 仍有 ' + num(readback.still_applied.length) + ' 条 applied（撤销未完全生效）'
                : '')
          }
        })
      } else {
        patchBatch(batch.batch_id, {
          phase: 'preview',
          feedback: { ok: false, text: '撤销失败：' + errText(res, '后端没给原因') }
        })
      }
      refreshAfterWrite(batch.batch_id)
      if (typeof props.onChanged === 'function') props.onChanged()
    })
  }

  /**
   * 新建候选项目：独立的一次确认（**建项目不写归属**）。
   *
   * `POST /autogroup/projects {name, slug}` —— 后端 `core.create_project` + 立刻回读项目表。
   */
  function createProject(batch, candidate) {
    send('/autogroup/projects', { name: candidate.name || candidate.slug, slug: candidate.slug }).then(function (res) {
      if (res && res.ok !== false) {
        var data = unwrap(res)
        patchBatch(batch.batch_id, {
          feedback: {
            ok: true,
            text: '项目已建：' + ((data.project && (data.project.path || data.project.slug)) || candidate.slug) +
              '（不写归属：这些条目仍要按上面的分档确认写入）'
          }
        })
      } else {
        patchBatch(batch.batch_id, {
          feedback: { ok: false, text: '建项目失败：' + errText(res, '后端没给原因') }
        })
      }
      reloadBatch(batch.batch_id)
    })
  }
  // ---- 派生视图：无法判定 / 硬性排除 / 新建候选（跨批次合并，按 session_id 去重） ----
  var unjudged = []
  var seenUnjudged = {}
  var excludedRows = []
  var candidates = []
  var candidateBySlug = {}
  batches.forEach(function (batch) {
    batch.rows.forEach(function (row) {
      if (row.tier === 'unjudged' && !seenUnjudged[row.session_id]) {
        seenUnjudged[row.session_id] = true
        unjudged.push({ batch_id: batch.batch_id, row: row })
      }
      if (row.new_project && row.new_project.slug) {
        var group = candidateBySlug[row.new_project.slug]
        if (!group) {
          group = { slug: row.new_project.slug, name: row.new_project.name || row.new_project.slug,
            rows: [], candidate: !!row.new_project.candidate,
            evidence_count: row.new_project.evidence_count || 0,
            downgrade_reason: row.new_project.downgrade_reason || '',
            target_exists: !!row.target_exists, batch: batch, why: row.selectable_why || '' }
          candidateBySlug[row.new_project.slug] = group
          candidates.push(group)
        }
        group.rows.push(row)
      }
    })
    ;(batch.excluded || []).forEach(function (item) {
      excludedRows.push({ batch_id: batch.batch_id, session_id: item.session_id, why: item.why })
    })
  })

  var progressText
  if (total === null) progressText = '候选总数还没读到 —— 点「刷新」再读一次'
  else if (running) {
    var live = job.progress || {}
    progressText = '正在跑：本批已处理 ' + num(live.done || 0) + ' / ' + num(live.total || 0) +
      ' 条 · 累计已处理 ' + num(processedCount) + ' / 共 ' + num(total) + ' 条候选'
  } else {
    progressText = '已处理 ' + num(processedCount) + ' / 共 ' + num(total) + ' 条候选' +
      (remaining ? '（还剩 ' + num(remaining) + ' 条没跑）' : '（已全部跑过一遍）')
  }

  // 入口唯一性（t_1dea3c49 验收 A）：跑批只留**这一个**主按钮。
  // 文案（t_8754de3f）与扫描面板同一句：勾了 = 分析已选 N 条，没勾 = 分析全部未分组 N 条。
  // 条数输入框 0/留空 = 全量（显式 limit=0），1..200 = 只跑下一批；勾选时输入框停用（整批一次跑完）。
  var batchSize = Number(batchSizeState[0])
  if (!(batchSize >= 0)) batchSize = 0
  var runLabel = analyzeLabel
  var allCount = shownCount == null ? 0 : shownCount
  var runButton = jsx('button', {
    type: 'button',
    className: 'hc-btn hc-btn--accent',
    'data-aic-run': '1',
    'data-aic-run-limit': String(selectedCount ? 0 : batchSize),
    'data-aic-run-blocked': modeBlocked ? '1' : '0',
    'data-aic-run-mode': selectedCount ? 'selected' : 'all',
    'data-aic-run-count': String(selectedCount || allCount),
    disabled: running || modeBlocked || (!selectedCount && !total),
    title: modeBlocked
      ? '全量重建模式下停用：AI 分类只处理未分组/待确认会话（已 applied 一律硬排除），不会重建体系；请用上方「先预览」'
      : selectedCount
        ? '只分析勾选的 ' + num(selectedCount) + ' 条（请求体带 session_ids；limit 固定 0，不传 skip_ids）'
        : total
          ? (batchSize > 0
              ? '显式 limit=' + batchSize + '：只跑下一批，已处理过的会话作为 skip_ids 带回去，不重复处理'
              : '显式 limit=0：一次跑完全部 ' + num(total) + ' 条候选（每条一次模型调用，约 5s/条）')
          : '候选总数还没读到',
    onClick: function () { startClassify(batchSize) },
    children: modeBlocked ? '全量重建模式下停用' : running ? '跑批中…' : runLabel
  }, 'run')

  // 显式入口（t_8754de3f）：勾选状态下仍然能一键处理全部未分组，不藏在菜单/空态里。
  var runAllButton = selectedCount
    ? jsx('button', {
        type: 'button',
        className: 'hc-btn',
        'data-aic-run-all': '1',
        disabled: running || modeBlocked,
        title: '显式入口：忽略当前勾选，分析全部 ' + num(allCount) + ' 条未分组会话（请求体不带 session_ids）',
        onClick: function () { startClassify(0, { all: true }) },
        children: '改为分析全部未分组 ' + num(allCount) + ' 条'
      }, 'run-all')
    : null

  return jsxs('div', {
    className: 'hc-agg__block',
    'data-aic-panel': '1',
    'data-aic-total': total === null ? '' : String(total),
    'data-aic-processed': String(processedCount),
    children: [
      jsxs('div', {
        key: 'gate',
        className: 'hc-agg2__gate',
        'data-agg2-mode-gate': 'ai',
        'data-agg2-mode-gate-state': modeBlocked ? 'blocked' : 'open',
        children: [
          jsx('span', { key: 't', className: 'hc-agg2__gate-title', children: 'AI 分类跑批' }),
          jsx('span', {
            key: 'h',
            className: 'hc-agg2__gate-text',
            children: modeBlocked
              ? '当前模式=全量重建：AI 分类只处理未分组/待确认会话（已 applied 一律硬排除），不会重建项目体系，与 B 口径矛盾，故停用。全量重建请用上方「先预览」。'
              : '当前模式=只归类未分组：AI 分类只补未分组/待确认会话，与 A 模式口径一致，可用。'
          })
        ]
      }),
      jsxs('div', {
        key: 'head',
        className: 'hc-agg__block-head',
        children: [
          jsx('span', { key: 'title', className: 'hc-agg__block-title', children: 'AI 分类（预览 → 人工确认 → 回读 → 撤销）' }),
          jsx('span', {
            key: 'total',
            className: 'hc-chip ' + (total ? 'hc-chip--on' : 'hc-chip--off'),
            'data-aic-total-chip': total === null ? '' : String(total),
            children: total === null ? '候选总数：未读到' : '共 ' + num(total) + ' 条可处理（无归属/待确认）'
          }),
          chip('已 applied 排除 ' + num(scope.excluded_applied || 0) + ' 条', 'ex-applied',
            '归属表里 applied 的行数（按当前 Agent 归属计；跨所有库）。其中落在本池里的 '
            + num(scope.applied_in_pool || 0) + ' 条才是「本池已分组」'),
          chip('rejected 排除 ' + num(scope.excluded_rejected || 0) + ' 条', 'ex-rejected'),
          chip('其它状态排除 ' + num(scope.excluded_other || 0) + ' 条', 'ex-other',
            '非 suggested 状态的归属行：正常排除，不是「漏处理」'),
          chip('桌面可见候选 ' + num(scope.desktop_visible || 0) + ' 条', 'desktop-visible',
            '桌面端真实可见条件：source 不属于 acp/kanban/oneshot/subagent/tool、hidden=0、'
            + 'archived 但 pinned 仍可见、message_count>=1、压缩续接/subagent 子行不列'),
          chip('已排除看板任务 ' + num(scope.excluded_kanban || 0) + ' 条', 'ex-kanban',
            '看板任务会话（sessions.source=kanban）不参与 AI 分类，也不会产生任何模型调用：'
            + '正常排除，不是漏处理'),
          chip('已排除隐藏 ' + num(scope.excluded_hidden || 0) + ' 条', 'ex-hidden',
            'hidden=1 的会话桌面端本来就不显示：正常排除，不是漏处理'),
          chip('已排除不可见来源 ' + num(scope.excluded_source || 0) + ' 条 · 归档未置顶 '
            + num(scope.excluded_archived || 0) + ' 条', 'ex-source',
            'acp/oneshot/subagent/tool 来源，以及 archived=1 且没置顶的会话：正常排除'),
          chip('已排除子会话 ' + num(scope.excluded_child || 0) + ' 条', 'ex-child',
            '压缩续接行与 subagent 运行：宿主会话列表本来就不列，正常排除'),
          chip('本池 ' + num(scope.pool || 0) + ' 条（其中已分组 ' + num(scope.applied_in_pool || 0)
            + ' 条）· 项目 ' + num(scope.projects || 0) + ' 个', 'scope')
        ]
      }),
      jsx('div', {
        key: 'scope-note',
        className: 'hc-agg__hint',
        'data-aic-scope-note': '1',
        children: '口径（' + String(scope.scope_rule || 'desktop-visible') + '）：只处理**桌面端真实可见的普通对话**，'
          + '候选总数与跑批共用引擎 limit=0 同一口径，**数据源是该 Agent 自己的 state.db**'
          + '（与 /overview/sessions?agent=X 同源）。已排除看板任务 ' + num(scope.excluded_kanban || 0) + ' 条、'
          + '隐藏 ' + num(scope.excluded_hidden || 0) + ' 条、不可见来源 ' + num(scope.excluded_source || 0) + ' 条、'
          + '归档未置顶 ' + num(scope.excluded_archived || 0) + ' 条、子会话 ' + num(scope.excluded_child || 0)
          + ' 条 —— 这些都是**正常排除，不是漏处理**。'
          + '「本池已分组 ' + num(scope.applied_in_pool || 0) + ' 条」与「本池 ' + num(scope.pool || 0)
          + ' 条」同源可相加；「已 applied 排除 ' + num(scope.excluded_applied || 0)
          + ' 条」是归属表口径（跨所有 agent），别把两个数混着读。'
      }),
      jsxs('div', {
        key: 'toolbar',
        className: 'hc-agg__toolbar',
        children: [
          runButton,
          runAllButton,
          jsx('input', {
            key: 'size',
            type: 'number',
            min: 0,
            max: scope.batch_max || 200,
            value: batchSizeState[0],
            'data-aic-batch-size': '1',
            disabled: selectedCount > 0,
            title: selectedCount
              ? '已勾选 ' + num(selectedCount) + ' 条：整批一次跑完（limit=0），分批条数不生效'
              : '每批条数：0 或留空 = 全量（limit=0）；1..200 = 只跑这一批。改它不会产生第二个按钮',
            style: { width: '72px' },
            onChange: function (event) {
              var raw = event.target.value
              batchSizeState[1](raw === '' ? 0 : Math.max(0, Math.floor(Number(raw) || 0)))
            }
          }),
          jsx('button', {
            key: 'refresh',
            type: 'button',
            className: 'hc-btn',
            'data-aic-refresh-scope': '1',
            disabled: scopeQuery.busy,
            title: '重新读候选总数与排除数（不跑模型）',
            onClick: function () { scopeQuery.reload() },
            children: '刷新口径'
          }),
          jsx('span', {
            key: 'progress',
            className: 'hc-agg__hint',
            'data-aic-progress': String(processedCount) + '/' + (total === null ? '' : String(total)),
            children: progressText
          })
        ]
      }),
      running
        ? jsxs('div', {
            key: 'live',
            className: 'hc-agg__scan-result hc-agg__scan-result--live',
            'data-aic-live': '1',
            'data-aic-live-phase': job.phase,
            'data-aic-live-job': job.jobId || '',
            children: [
              jsx('span', {
                key: 'lead',
                className: 'hc-agg__scan-live',
                children: job.phase === 'starting'
                  ? '已提交，正在等后端返回 job…'
                  : '正在逐条分类：本批已处理 ' +
                    num((job.progress || {}).done || 0) + ' / ' + num((job.progress || {}).total || 0) + ' 条'
              }),
              jsx('span', {
                key: 'total',
                className: 'hc-agg__note',
                children: '累计已处理 ' + num(processedCount) + ' / 共 ' +
                  (total === null ? '（候选总数还没读到）' : num(total)) + ' 条候选 · 每条约 5s'
              }),
              job.jobId
                ? jsx('span', {
                    key: 'job',
                    className: 'hc-agg__note',
                    'data-aic-live-jobid': job.jobId,
                    children: '任务 ' + job.jobId + '（正在跑，每 2 秒刷新一次）'
                  })
                : null
            ]
          })
        : null,
      job.phase === 'failed' && job.error
        ? jsx(ErrorState, {
            key: 'error',
            title: '跑批失败',
            message: job.error,
            detail: job.jobId ? 'job ' + job.jobId : '',
            onRetry: function () { startClassify(batchSize) },
            retryLabel: '重试这一批'
          })
        : null,
      resultLine('global', feedbackState[0], 'global'),
      batches.length
        ? null
        : jsx(Empty, {
            key: 'empty',
            title: '还没有预览批次',
            // 空态里**不再放按钮**：跑批的唯一入口是上面那个 [data-aic-run]。
            hint: '点上面的「分析…」按钮（勾了 = 只分析已选，没勾 = 分析全部未分组）：服务端会落一个预览批次；' +
              '确认前不写归属、不改 cwd、不建目录。'
          }),
      batches.map(function (batch) {
        return jsxs('div', {
          key: batch.batch_id,
          className: 'hc-agg__block',
          'data-aic-batch-wrap': batch.batch_id,
          children: [
            jsx(AiBatchBlock, {
              key: 'block',
              batch: batch,
              onToggle: toggleRow,
              onPickAll: pickAll,
              onClear: clearAll,
              onConfirm: confirmBatch,
              onReload: reloadBatch
            }),
            jsx(AiBatchResults, { key: 'results', batch: batch }),
            batch.submitted && !batch.undo && batch.status !== 'undone'
              ? jsx('div', {
                  key: 'undo-bar',
                  className: 'hc-agg__toolbar',
                  children: [
                    jsx('button', {
                      key: 'undo',
                      type: 'button',
                      className: 'hc-btn',
                      'data-aic-undo': batch.batch_id,
                      disabled: batch.phase === 'undoing',
                      title: '按台账逐字段还原这次写入（原本不存在的行恢复成不存在；遇后续修改会暂停不覆盖）',
                      onClick: function () { undoBatch(batch) },
                      children: batch.phase === 'undoing' ? '撤销中…' : '撤销本批次（confirm=true）'
                    }),
                    jsx('span', {
                      key: 'hint',
                      className: 'hc-agg__hint',
                      children: '撤销入口就这一条：批次 ' + batch.batch_id + ' · 逐条回读见上表，写入后被人改过的行撤销时会暂停'
                    })
                  ]
                })
              : null
          ]
        }, batch.batch_id)
      }),
      jsxs('div', {
        key: 'unjudged',
        className: 'hc-agg__block',
        'data-aic-unjudged': String(unjudged.length),
        children: [
          jsxs('div', {
            key: 'head',
            className: 'hc-agg__block-head',
            children: [
              jsx('span', { key: 'title', className: 'hc-agg__block-title', children: '无法判定清单（禁止写入，逐条带原因）' }),
              jsx('span', {
                key: 'count',
                className: 'hc-chip ' + (unjudged.length ? 'hc-chip--off' : ''),
                children: num(unjudged.length) + ' 条'
              }),
              jsx('span', {
                key: 'hint',
                className: 'hc-agg__hint',
                children: '低置信度（<0.5）/ 模型输出无效 / 调用失败 / 正文不可用都在这里，一条都不会被静默丢掉'
              })
            ]
          }),
          unjudged.length
            ? compactTable({
                key: 'table',
                name: 'unjudged',
                head: ['会话', '归类', '原因（后端原话）', '批次'],
                rows: unjudged.map(function (entry) {
                  var row = entry.row
                  return {
                    key: row.session_id,
                    ok: false,
                    cells: [
                      (row.title || row.session_id) + '（' + row.session_id + '）',
                      undecidedLabel(row.undecided_code) + (typeof row.suggested.confidence === 'number'
                        ? ' · 置信度 ' + confText(row.suggested.confidence) : ''),
                      row.unjudged_reason || row.selectable_why || '后端没给原因',
                      entry.batch_id
                    ]
                  }
                })
              })
            : jsx('div', { key: 'empty', className: 'hc-agg__hint', children: '本轮没有无法判定的条目。' })
        ]
      }),
      jsxs('div', {
        key: 'newprojects',
        className: 'hc-agg__block',
        'data-aic-newprojects': String(candidates.length),
        children: [
          jsxs('div', {
            key: 'head',
            className: 'hc-agg__block-head',
            children: [
              jsx('span', { key: 'title', className: 'hc-agg__block-title', children: '新建项目候选（≥3 条同主题会话才成立）' }),
              jsx('span', { key: 'count', className: 'hc-chip', children: num(candidates.length) + ' 个' }),
              jsx('span', {
                key: 'hint',
                className: 'hc-agg__hint',
                children: '新建项目是**独立的一次确认**：建项目不写任何归属；支撑不足 3 条一律降级并写明原因，不允许无条件新建'
              })
            ]
          }),
          candidates.length
            ? candidates.map(function (group) {
                var blocked = !group.candidate
                var needCreate = group.candidate && !group.target_exists
                return jsxs('div', {
                  key: group.slug,
                  className: 'hc-agg__row',
                  'data-aic-candidate': group.slug,
                  'data-aic-candidate-state': blocked ? 'downgraded' : needCreate ? 'needs_create' : 'ready',
                  style: { '--agg-cols': 'minmax(0, 1fr) minmax(0, 2fr) minmax(160px, 0.7fr)', display: 'grid' },
                  children: [
                    jsxs('div', {
                      key: 'name',
                      className: 'hc-agg__cell hc-agg__cell--project',
                      children: [
                        jsx('span', { key: 'n', className: 'hc-agg__project', children: group.name + '（' + group.slug + '）' }),
                        jsx('span', { key: 'c', className: 'hc-chip', children: '支撑 ' + num(group.evidence_count) + ' 条' })
                      ]
                    }),
                    jsx('div', {
                      key: 'why',
                      className: 'hc-agg__cell hc-agg__cell--basis',
                      children: (blocked
                        ? '降级/阻止：' + (group.downgrade_reason || group.why || '支撑会话不足 3 条')
                        : needCreate
                          ? '证据 ' + group.rows.length + ' 条会话指向同一主题；项目还没建，先确认新建'
                          : '项目已建：这些条目可以按原分档写入') +
                        ' · 涉及会话：' + group.rows.slice(0, 4).map(function (row) {
                          return row.session_id
                        }).join('、') + (group.rows.length > 4 ? '…' : '')
                    }),
                    jsx('div', {
                      key: 'ops',
                      className: 'hc-agg__cell hc-agg__cell--ops-stack',
                      children: jsx('button', {
                        type: 'button',
                        className: 'hc-btn hc-btn--tiny',
                        'data-aic-create-project': group.slug,
                        disabled: blocked || !needCreate,
                        title: blocked
                          ? '支撑不足 3 条：不允许新建（原因见左）'
                          : needCreate ? '带 confirm=true 建项目 + 目录（不写归属）' : '项目已存在',
                        onClick: function () { createProject(group.batch, group) },
                        children: blocked ? '不可新建（降级）' : needCreate ? '确认新建项目' : '已建'
                      })
                    })
                  ]
                }, group.slug)
              })
            : jsx('div', { key: 'empty', className: 'hc-agg__hint', children: '本轮没有新建项目候选。' })
        ]
      }),
      jsxs('div', {
        key: 'excluded',
        className: 'hc-agg__block',
        'data-aic-excluded': String(excludedRows.length),
        children: [
          jsxs('div', {
            key: 'head',
            className: 'hc-agg__block-head',
            children: [
              jsx('span', { key: 'title', className: 'hc-agg__block-title', children: '被硬性排除的条目（不是漏处理）' }),
              jsx('span', { key: 'count', className: 'hc-chip', children: num(excludedRows.length) + ' 条' })
            ]
          }),
          excludedRows.length
            ? compactTable({
                key: 'table',
                name: 'excluded',
                head: ['会话', '原因', '批次', ''],
                rows: excludedRows.map(function (item, index) {
                  return { key: 'ex' + index, cells: [item.session_id, item.why || '—', item.batch_id, ''] }
                })
              })
            : jsx('div', { key: 'empty', className: 'hc-agg__hint', children: '本批没有被硬性排除的条目。' })
        ]
      }),
      notesBlock([
        '原有 56 条 applied 归属**不在本次处理范围**：它们在建预览批次时就被硬性排除，确认写入也不会碰它们。',
        '生产自动模式保持关闭：本面板只做人工确认，不调用 /autogroup/auto 与 /autogroup/auto/tick。',
        '确认前零写入；确认后逐条回读；写入批次与 op_id 就是撤销入口（重复确认会被后端 409 拒绝）。'
      ], 'foot')
    ]
  })
}
