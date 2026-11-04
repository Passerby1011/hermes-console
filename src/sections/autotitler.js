/**
 * hermes-console · 自动命名（hermes-auto-titler）分区。
 *
 * 用户在卡里的话：「我有一个自动给会话重命名的插件，希望加入它的面板，可以设置它的各项
 * 参数设置，监控它的运行」。
 *
 * 本分区只做两件事，**一件都不多做**：
 *   ① 监控：它到底在不在工作。核心是失败率 —— 插件 94 次失败 / 2 次成功，失败原因
 *      是「模型返回散文而不是 JSON 决策载荷」。这个数字必须一眼看见，否则面板等于没说话。
 *   ② 设置：按插件 ``config_schema`` **动态**渲染全部参数（不硬编码 23 个键），每项显示
 *      默认值 / 当前生效值 / 值来自哪一层，写入走宿主官方 settings 通道。
 *
 * 六条规矩：
 *   1. **口径写脸上**：每个数字都说清"从哪个文件、哪一类日志行算出来的"，不让人猜。
 *   2. **不猜状态**：三态（enabled/disabled/unavailable）来自后端 `plugin_switch`，
 *      本文件不自己判；没有 `registered` 证据就说"日志窗口里没看到它加载"。
 *   3. **未改动 = 零写请求**：只有值真的变了才进 `dirty`，保存只提交 dirty 的键。
 *   4. **前端先拦一次**：范围/枚举在这里先拒并给可读错误，后端会再拒一次（两边都做）。
 *   5. 三态复用 lib/states.js（Skeleton / Empty / ErrorState），不另写一套。
 *   6. 颜色只走 var(--ui-*)，本文件不出现字面色值（构建脚本会检查）。
 *
 * 手动操作（立即重命名 / 立即重试）**没有做成按钮**：那需要插件运行态（AutoTitler 实例），
 * 它只在 agent 进程里；控制台后端另起一个实例会与网关里的实例并发写 state.json。面板改成
 * 把"该对哪个会话执行哪条命令"写清楚，由人去对应会话里执行。
 */
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useMemo, useState } from 'react'

import { relTime } from '../lib/format.js'
import { ProviderModelPicker } from '../lib/provider-model-picker.js'
import { refresh, useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'

/** 缓存窗口：切分区回来不重拉；手动点刷新才重算。 */
var STALE_MS = 20000

/** 最近失败明细的条数（列表只给一眼能扫的量）。 */
var RECENT_FAILURES = 12

/** 分组标题（后端 groups 给了就用它，这只是后端没给时的兜底）。 */
var GROUP_FALLBACK = '其它'

/**
 * 时间区间。
 *
 * 为什么必须能切：插件曾经累计失败 109 次（模型回散文而非 JSON），修好之后失败归零。
 * 只给一个「累计失败率」的界面会永远显示 98%，用户看到的就是「还在报错」——
 * 面板必须让他一眼看出「现在」和「历史」是两件事。
 *
 * 缺省看 24 小时：既不会因为刚修完就报喜，也不会把几天前的账算成今天的。
 */
var WINDOWS = [
  { id: '1h', label: '最近 1 小时' },
  { id: '24h', label: '最近 24 小时' },
  { id: 'all', label: '全部历史' }
]

var DEFAULT_WINDOW = '24h'

/** 运行日志页一次拉多少条（只读列表，翻不了页，够看就行）。 */
var LOG_LIMIT = 200

/** 日志页的 action 过滤项（后端 acts 里有别的就照它显示）。 */
var LOG_ACTS = ['failed', 'renamed', 'keep', 'skipped', 'throttled', 'pending']

function settingsKey(agent) {
  return 'autotitler.settings|' + (agent || 'default')
}

function overviewKey(agent) {
  return 'autotitler.overview|' + (agent || 'default')
}

function logsKey(agent, act, sid) {
  return 'autotitler.logs|' + (agent || 'default') + '|' + (act || '*') + '|' + (sid || '*')
}

function fetchSettings(agent) {
  return get('/autotitler/settings', { agent: agent })
}

function fetchOverview(agent, windowId) {
  return get('/autotitler/overview', {
    agent: agent,
    params: { recent: RECENT_FAILURES, window: windowId || DEFAULT_WINDOW },
    timeoutMs: 20000
  })
}

function fetchLogs(agent, act, sid) {
  var params = { limit: LOG_LIMIT }
  if (act) params.act = act
  if (sid) params.sid = sid
  return get('/autotitler/logs', { agent: agent, params: params, timeoutMs: 20000 })
}

/** rest.js 返回 `{ok, data}`（前端外壳），后端 body 又是 `{ok, data}`：取里层。 */
function unwrap(payload) {
  if (payload && typeof payload === 'object' && payload.data && typeof payload.data === 'object') {
    return payload.data
  }
  return payload && typeof payload === 'object' ? payload : null
}

/** 0.98 → "98.0%"；null → "—"（不是 0%，空数据不能伪装成"零失败"）。 */
function percent(value) {
  if (typeof value !== 'number' || !isFinite(value)) return '—'
  return (value * 100).toFixed(1) + '%'
}

/** epoch 秒 → "3 分钟前"；None → "—"。插件写的是**秒**（不是毫秒）。 */
function fromSeconds(value) {
  if (typeof value !== 'number' || !isFinite(value) || value <= 0) return '—'
  return relTime(value * 1000)
}

/** 值的展示形态：空串要看得见（"（空）"比一片空白好排查）。 */
function showValue(value) {
  if (value === null) return '（未设置）'
  if (value === undefined) return '（无）'
  if (value === '') return '（空）'
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  return String(value)
}

// ---------------------------------------------------------------------------
// 小组件
// ---------------------------------------------------------------------------
function Chip(props) {
  return jsx('span', {
    className: 'hc-atl__chip' + (props.tone ? ' hc-atl__chip--' + props.tone : ''),
    title: props.title || undefined,
    children: props.children
  })
}

function Stat(props) {
  return jsxs('div', {
    className: 'hc-atl__stat' + (props.tone ? ' hc-atl__stat--' + props.tone : ''),
    children: [
      jsx('div', { className: 'hc-atl__stat-value', children: props.value }),
      jsx('div', { className: 'hc-atl__stat-label', children: props.label }),
      props.hint ? jsx('div', { className: 'hc-atl__stat-hint', children: props.hint }) : null
    ]
  })
}

/**
 * 健康度大条：**先说清「哪个区间」的失败率**，再说数字。
 *
 * 为什么不是一排等权重的数字：这个插件曾经 98 次失败 / 2 次成功，如果"成功 2 / 失败 98"
 * 并排成两个同尺寸方块，看起来像"两种正常结果"。失败率单独占一条，且过 50% 就换警示底色。
 *
 * 为什么必须标区间：修好之后失败已经归零，但累计口径永远是 98%。所以：
 *   · 大数字永远是**选中区间**的失败率，标题里带区间名；
 *   · 下面永远附一行「最近 1 小时」的实况 —— 用户第一眼要看到「现在还好不好」；
 *   · 时间范围与最后更新时间直接写在条上，不藏在 tooltip 里。
 */
function HealthBanner(props) {
  var health = props.health || {}
  // 选中区间的完整视图（后端一次算好三个区间，切换不发请求也不闪骨架）
  var view = props.view || health.window || {}
  var windows = health.windows || {}
  var counts = view.counts || health.counts || {}
  var rate = typeof view.failure_rate === 'number' ? view.failure_rate : health.failure_rate
  var decisions = typeof view.decisions === 'number' ? view.decisions : health.decisions
  var bad = typeof rate === 'number' && rate >= 0.5 && decisions > 0
  var diagnosis = view.diagnosis || null

  var now = windows['1h'] || null
  var nowFailed = now && now.counts ? now.counts.failed || 0 : null
  var nowRate = now && typeof now.failure_rate === 'number' ? now.failure_rate : null
  var nowClean = nowFailed === 0 && now && now.counts && now.counts.total > 0

  return jsxs('div', {
    className: 'hc-atl__banner' + (bad ? ' hc-atl__banner--bad' : ''),
    'data-window': view.id || '',
    children: [
      jsxs('div', {
        className: 'hc-atl__banner-head',
        children: [
          jsx('div', { className: 'hc-atl__banner-rate', children: percent(rate) }),
          jsxs('div', {
            className: 'hc-atl__banner-copy',
            children: [
              jsx('div', {
                className: 'hc-atl__banner-title',
                children:
                  (view.label || '选中区间') +
                  ' · ' +
                  (bad ? '失败率偏高：它在跑，但基本没改成名' : '决策类调用的失败率')
              }),
              jsx('div', {
                className: 'hc-atl__banner-sub',
                children:
                  '失败 ' +
                  (counts.failed || 0) +
                  ' / 成功改名 ' +
                  (counts.renamed || 0) +
                  '（分母 = 改名 + 失败 = ' +
                  (decisions || 0) +
                  '；跳过 ' +
                  (counts.skipped || 0) +
                  ' 次不计入分母）'
              }),
              jsxs('div', {
                className: 'hc-atl__banner-window',
                children: [
                  jsx('span', {
                    children:
                      '区间：' +
                      (view.from_ts || '（不限）') +
                      ' → ' +
                      (view.to_ts || '现在')
                  }),
                  jsx('span', { children: '最后更新：' + (health.generated_at || '—') })
                ]
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        className:
          'hc-atl__nowline' + (nowClean ? ' hc-atl__nowline--good' : nowFailed ? ' hc-atl__nowline--bad' : ''),
        children: [
          jsx('b', { children: '现在（最近 1 小时）：' }),
          now
            ? jsx('span', {
                children:
                  nowFailed === 0 && now.counts.total > 0
                    ? '零失败 · 改名 ' + (now.counts.renamed || 0) + ' 次 · 审计 ' + now.counts.total + ' 条'
                    : now.counts.total === 0
                      ? '这段时间没有审计记录（没触发，不等于出错）'
                      : '失败 ' + nowFailed + ' 次 · 失败率 ' + percent(nowRate)
              })
            : jsx('span', { children: '—' })
        ]
      }),
      diagnosis
        ? jsxs('div', {
            className: 'hc-atl__diagnosis',
            children: [
              jsx('div', { className: 'hc-atl__diagnosis-head', children: diagnosis.headline }),
              jsx('div', { className: 'hc-atl__diagnosis-body', children: diagnosis.detail }),
              jsx('div', {
                className: 'hc-atl__diagnosis-fix',
                children: '该去哪里修（不在本面板）：'
              }),
              jsx('ul', {
                className: 'hc-atl__diagnosis-list',
                children: (diagnosis.where_to_fix || []).map(function (item, index) {
                  return jsx('li', { children: item }, 'fix' + index)
                })
              }),
              jsx('div', { className: 'hc-atl__diagnosis-note', children: diagnosis.note })
            ]
          })
        : null
    ]
  })
}

/** 区间切换条：三个按钮 + 「这一排数字是哪个区间的」明示。 */
function WindowBar(props) {
  var value = props.value
  var onChange = props.onChange
  var windows = (props.health && props.health.windows) || {}
  return jsxs('div', {
    className: 'hc-atl__windowbar',
    children: [
      jsx('span', { className: 'hc-atl__windowbar-label', children: '统计区间' }),
      jsx('div', {
        className: 'hc-atl__windowbar-buttons',
        role: 'tablist',
        children: WINDOWS.map(function (item) {
          var active = item.id === value
          var info = windows[item.id] || {}
          var count = info.counts ? info.counts.total : null
          return jsxs(
            'button',
            {
              type: 'button',
              role: 'tab',
              'aria-selected': active ? 'true' : 'false',
              className: 'hc-atl__windowbtn' + (active ? ' hc-atl__windowbtn--active' : ''),
              'data-window': item.id,
              title: info.from_ts ? '区间 ' + info.from_ts + ' → ' + (info.to_ts || '现在') : '',
              onClick: function () {
                onChange(item.id)
              },
              children: [
                jsx('span', { children: item.label }),
                count === null ? null : jsx('span', { className: 'hc-atl__windowbtn-count', children: count + ' 条' })
              ]
            },
            item.id
          )
        })
      }),
      jsx('span', {
        className: 'hc-atl__muted',
        children: '切换区间只改口径，不写任何东西；三个区间的数字后端一次算好'
      })
    ]
  })
}

/**
 * 基线（修复点）面板：一条时间线说明「此前 N 次失败，此后 0 次」。
 *
 * 这是「历史 vs 当前」的正面回答：不否认历史，但把历史关进「基线之前」那一侧。
 */
function BaselinePanel(props) {
  var health = props.health || {}
  var baseline = health.baseline || {}
  var timeline = baseline.timeline || health.timeline || []

  if (!baseline.available) {
    return jsx('div', {
      className: 'hc-atl__panel',
      children: [
        jsx('div', { className: 'hc-atl__panel-title', children: '修复点 / 基线' }),
        jsx('div', {
          className: 'hc-atl__note',
          children: baseline.source_label || '日志里没有带时间戳的 audit 行，画不出时间线。'
        })
      ]
    })
  }

  var before = baseline.before || {}
  var after = baseline.after || {}
  var beforeCounts = before.counts || {}
  var afterCounts = after.counts || {}

  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsx('div', { className: 'hc-atl__panel-title', children: '修复点 / 基线（历史 vs 当前）' }),
      jsxs('div', {
        className: 'hc-atl__baseline-head',
        children: [
          jsx('div', { className: 'hc-atl__baseline-ts', children: baseline.ts || '—' }),
          jsx('div', { className: 'hc-atl__baseline-copy', children: baseline.headline || '' }),
          jsx('div', { className: 'hc-atl__muted', children: '基线来源：' + (baseline.source_label || '—') })
        ]
      }),
      jsxs('div', {
        className: 'hc-atl__baseline-sides',
        children: [
          jsxs('div', {
            className: 'hc-atl__side hc-atl__side--before',
            children: [
              jsx('div', { className: 'hc-atl__side-title', children: '基线之前（历史账）' }),
              jsx('div', {
                className: 'hc-atl__side-value',
                children: '失败 ' + (beforeCounts.failed || 0) + ' 次'
              }),
              jsx('div', {
                className: 'hc-atl__muted',
                children:
                  '审计 ' +
                  (before.total || 0) +
                  ' 条 · 改名 ' +
                  (beforeCounts.renamed || 0) +
                  ' · 跳过 ' +
                  (beforeCounts.skipped || 0) +
                  (before.last_ts ? ' · 至 ' + before.last_ts : '')
              })
            ]
          }),
          jsxs('div', {
            className: 'hc-atl__side hc-atl__side--after',
            children: [
              jsx('div', { className: 'hc-atl__side-title', children: '基线之后（当前）' }),
              jsx('div', {
                className: 'hc-atl__side-value',
                children: '失败 ' + (afterCounts.failed || 0) + ' 次'
              }),
              jsx('div', {
                className: 'hc-atl__muted',
                children:
                  '审计 ' +
                  (after.total || 0) +
                  ' 条 · 改名 ' +
                  (afterCounts.renamed || 0) +
                  ' · keep ' +
                  (afterCounts.keep || 0) +
                  (after.last_ts ? ' · 至 ' + after.last_ts : '')
              })
            ]
          })
        ]
      }),
      timeline.length
        ? jsxs('div', {
            className: 'hc-atl__timeline',
            children: [
              jsx('div', { className: 'hc-atl__timeline-label', children: '时间线（每格 = 一段时间，红 = 有失败）' }),
              jsx('div', {
                className: 'hc-atl__timeline-bars',
                children: timeline.map(function (bucket, index) {
                  var total = bucket.total || 0
                  var failed = bucket.failed || 0
                  var height = Math.max(6, Math.min(100, total * 12))
                  var tone = failed ? 'bad' : bucket.renamed ? 'good' : 'idle'
                  return jsx(
                    'div',
                    {
                      className:
                        'hc-atl__timeline-bar hc-atl__timeline-bar--' +
                        tone +
                        (bucket.before_baseline ? ' hc-atl__timeline-bar--before' : ''),
                      style: { height: height + '%' },
                      title:
                        (bucket.start_ts || '') +
                        ' → ' +
                        (bucket.end_ts || '') +
                        '：失败 ' +
                        failed +
                        ' · 改名 ' +
                        (bucket.renamed || 0) +
                        ' · 共 ' +
                        total
                    },
                    'tl' + index
                  )
                })
              }),
              jsx('div', {
                className: 'hc-atl__muted',
                children:
                  '最早 ' +
                  (timeline[0] && timeline[0].start_ts ? timeline[0].start_ts : '—') +
                  ' · 最晚 ' +
                  (timeline[timeline.length - 1] ? timeline[timeline.length - 1].end_ts : '—')
              })
            ]
          })
        : null,
      jsx('div', {
        className: 'hc-atl__note',
        children:
          '基线默认取**最后一次失败的时间**（「此后还有没有失败」的天然分界）；要对着你记得的修复时刻看，' +
          '后端支持 `?baseline=YYYY-MM-DD HH:MM:SS`。'
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// A. 设置表单
// ---------------------------------------------------------------------------

/**
 * 前端侧校验：返回错误文案（空串 = 合法）。
 *
 * **与后端 `api_autotitler._check_bounds` 同一套规则**，但两边都写一份是故意的：
 * 前端要先拦（不用等一次网络往返才告诉人填错了），后端必须再拦（前端可以被绕过）。
 */
export function validateField(field, raw) {
  var source = field || {}
  var type = String(source.type || '')
  var key = source.key || ''

  if (type === 'bool') {
    if (raw !== true && raw !== false) return '只能是开 / 关'
    return ''
  }

  if (type === 'int' || type === 'float') {
    var text = String(raw === undefined || raw === null ? '' : raw).trim()
    if (text === '') return '请填一个数字'
    var number = Number(text)
    if (!isFinite(number)) return '「' + text + '」不是数字'
    if (type === 'int' && Math.floor(number) !== number) return '这里只能是整数'
    // max_title_length 的两个合法区段是 0 与 10..100，标准 min/max 表达不了
    if (source.special === 'zero_or_range') {
      if (number === 0) return ''
      if (number < 0) return '不能是负数：0 = 不限制，要限制请填 10..100'
      if (number < 10 || number > 100) {
        return '合法区段只有「0（不限制）」与「10..100」：' + number + ' 越界了'
      }
      return ''
    }
    if (typeof source.min === 'number' && number < source.min) {
      return (source.range_hint || '不得小于 ' + source.min) + '（收到 ' + number + '）'
    }
    if (typeof source.max === 'number' && number > source.max) {
      return (source.range_hint || '不得大于 ' + source.max) + '（收到 ' + number + '）'
    }
    return ''
  }

  if (type === 'str') {
    var choices = source.choices || []
    if (choices.length) {
      var allowed = choices.map(function (item) {
        return item.value
      })
      if (allowed.indexOf(String(raw)) === -1) {
        return '只能是 ' + allowed.join(' / ') + ' 之一'
      }
    }
    return ''
  }
  return ''
}

/** 输入值 → 提交值（数字项在体里保持数字类型，否则后端写进去就是字符串）。 */
function coerceForSubmit(field, raw) {
  var type = String((field && field.type) || '')
  if (type === 'int') return parseInt(String(raw).trim(), 10)
  if (type === 'float') return parseFloat(String(raw).trim())
  if (type === 'bool') return raw === true || raw === 'true'
  return raw === undefined || raw === null ? '' : String(raw)
}

/** 当前生效值与默认值是否相同（相同就说明这层默认值没被任何人改过）。 */
function sameAsDefault(field) {
  var value = field.value
  var fallback = field.default
  if (value === fallback) return true
  if (typeof value === 'number' && typeof fallback === 'number') return value === fallback
  if (value === '' && fallback === '') return true
  return false
}

/**
 * 一项参数。
 *
 * 每项必须同时给出三样东西（卡片验收 A 的原文）：**当前生效值 + 默认值 + 值来自哪一层**。
 * 只显示生效值会让人以为"面板上的数就是文件里的数"，而这里有三层覆盖。
 */
function FieldRow(props) {
  var field = props.field
  var draft = props.draft
  var error = props.error
  var dirty = props.dirty
  var onEdit = props.onEdit
  var onReset = props.onReset
  var disabled = props.disabled
  var type = String(field.type || '')
  var choices = field.choices || []

  var control
  if (field.key === 'title_model') {
    // ★ 两级联动（共享组件）：provider 下拉按该 agent 的可用 provider 筛，
    //   模型下拉只来自当前 provider；provider 变了旧模型立即清空。
    //   这里一次改**两个键**（provider + title_model），保存时 provider 先落盘。
    var pair = props.pair && typeof props.pair === 'object' ? props.pair : {}
    control = jsx(ProviderModelPicker, {
      agent: props.agent,
      value: { provider: String(pair.provider || ''), model: String(pair.title_model || '') },
      allowFollow: true,
      allowEmptyModel: true,
      sourceKind: String(pair.provider || '') ? 'agent' : 'global',
      idPrefix: 'autotitler',
      disabled: disabled,
      advancedNote: '标题模型允许留空（= 走 Hermes 的 title_generation 路由）。'
        + '清单外的名字在点「用这个名字」时会先经 GET /models/validate 只读复核，'
        + '校验没过会把原因显示在选择器下方、不放行；通过后再由后端记为未校验。',
      onChange: function (next) {
        onEdit('provider', next.provider)
        onEdit('title_model', next.model)
      }
    })
  } else if (field.key === 'provider') {
    // provider 的控件在上面的 title_model 行（同一个两级选择器）里，这里只留一句联动说明，
    // 不再放第二个文本框（两个入口改同一个值 = 迟早不一致）。
    control = jsx('div', {
      className: 'hc-atl__linked',
      'data-atl-linked': 'provider',
      children: '与上面「Title model」的提供商 / 模型两级选择器联动（这里不再单独给输入框）'
    })
  } else if (type === 'bool') {
    control = jsxs('label', {
      className: 'hc-atl__switch',
      children: [
        jsx('input', {
          type: 'checkbox',
          checked: draft === true || draft === 'true',
          disabled: disabled,
          onChange: function (event) {
            onEdit(field.key, event.target.checked)
          }
        }),
        jsx('span', {
          className: 'hc-atl__switch-text',
          children: draft === true || draft === 'true' ? '开启' : '关闭'
        })
      ]
    })
  } else if (choices.length) {
    control = jsx('select', {
      className: 'hc-atl__input',
      value: String(draft === undefined || draft === null ? '' : draft),
      disabled: disabled,
      onChange: function (event) {
        onEdit(field.key, event.target.value)
      },
      children: choices.map(function (item) {
        return jsx('option', { value: item.value, children: item.label }, item.value)
      })
    })
  } else if (type === 'str' && String(field.key).indexOf('instructions') !== -1) {
    control = jsx('textarea', {
      className: 'hc-atl__input hc-atl__input--area',
      rows: 3,
      value: draft === undefined || draft === null ? '' : String(draft),
      disabled: disabled,
      onChange: function (event) {
        onEdit(field.key, event.target.value)
      }
    })
  } else {
    control = jsx('input', {
      className: 'hc-atl__input',
      type: type === 'int' || type === 'float' ? 'number' : 'text',
      step: type === 'float' ? 'any' : undefined,
      value: draft === undefined || draft === null ? '' : String(draft),
      disabled: disabled,
      onChange: function (event) {
        onEdit(field.key, event.target.value)
      }
    })
  }

  var sourceTone =
    field.source === 'default'
      ? 'muted'
      : field.source === 'plugin_config_yaml'
        ? 'file'
        : 'host'

  return jsxs('div', {
    className: 'hc-atl__field' + (dirty ? ' hc-atl__field--dirty' : '') + (error ? ' hc-atl__field--bad' : ''),
    'data-key': field.key,
    children: [
      jsxs('div', {
        className: 'hc-atl__field-head',
        children: [
          jsx('code', { className: 'hc-atl__field-key', children: field.key }),
          jsx('span', { className: 'hc-atl__field-label', children: field.label }),
          field.needs_restart ? jsx(Chip, { tone: 'warn', title: '插件 config_schema 明写要重启', children: '需重启' }) : null,
          dirty ? jsx(Chip, { tone: 'dirty', children: '已改动' }) : null,
          jsx('button', {
            type: 'button',
            className: 'hc-atl__link',
            title: '把这一项改回它现在的生效值（不提交）',
            disabled: disabled || !dirty,
            onClick: function () {
              onReset(field)
            },
            children: '还原'
          })
        ]
      }),
      field.description
        ? jsx('div', { className: 'hc-atl__field-desc', children: field.description })
        : null,
      jsxs('div', {
        className: 'hc-atl__field-body',
        children: [
          control,
          jsxs('div', {
            className: 'hc-atl__field-meta',
            children: [
              jsxs('span', {
                className: 'hc-atl__from hc-atl__from--' + sourceTone,
                title: '这个值当前是从哪一层读出来的（优先级：该 agent settings > 全局 settings > 插件 config.yaml > 默认）',
                children: ['来自：', jsx('b', { children: field.source_label })]
              }),
              jsxs('span', {
                className: 'hc-atl__default',
                title: '插件 config_schema 声明的默认值（上面那层都没设过时就用它）',
                children: ['默认：', jsx('b', { children: showValue(field.default) })]
              }),
              sameAsDefault(field) && field.source === 'default'
                ? jsx('span', { className: 'hc-atl__muted', children: '当前 = 默认（没人改过）' })
                : null,
              field.range_hint
                ? jsx('span', { className: 'hc-atl__muted', children: '范围：' + field.range_hint })
                : null,
              field.special === 'zero_or_range'
                ? jsx('span', { className: 'hc-atl__muted', children: '0 = 不限制；>0 时最小 10、最大 100' })
                : null
            ]
          }),
          error ? jsx('div', { className: 'hc-atl__field-error', role: 'alert', children: error }) : null
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// B. 监控面板
// ---------------------------------------------------------------------------
function PluginOverview(props) {
  var plugin = props.plugin || {}
  var runtime = props.runtime || {}
  var counts = props.health && props.health.counts ? props.health.counts : {}

  var kinds = runtime.kind_counts || {}
  var renames = runtime.rename_counts || {}
  var kindText = Object.keys(kinds).length
    ? Object.keys(kinds)
        .sort()
        .map(function (name) {
          return name + ' × ' + kinds[name]
        })
        .join(' · ')
    : '无'
  var renameText = Object.keys(renames).length
    ? Object.keys(renames)
        .sort(function (a, b) {
          return Number(a) - Number(b)
        })
        .map(function (name) {
          return name + ' 次 × ' + renames[name] + ' 会话'
        })
        .join(' · ')
    : '无'

  var hooks = plugin.declared_hooks || []
  var registered = plugin.registered

  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsx('div', { className: 'hc-atl__panel-title', children: '运行态概览' }),
      jsxs('div', {
        className: 'hc-atl__kv',
        children: [
          kv('插件', plugin.id || '—'),
          kv('版本', plugin.version || '未读到'),
          kv(
            '宿主三态',
            plugin.state === 'enabled'
              ? '已启用'
              : plugin.state === 'disabled'
                ? '已禁用'
                : plugin.state === 'unavailable'
                  ? '两份名单里都没有（等于未启用）'
                  : '未读到',
            plugin.state === 'enabled' ? 'good' : plugin.state ? 'bad' : 'muted'
          ),
          kv('声明 hooks', hooks.length ? hooks.join(' · ') : 'manifest 没声明'),
          kv(
            'hooks 加载证据',
            registered
              ? '日志里见到 register 行（' + registered.ts + '，first_title_mode=' + registered.first_title_mode + '）'
              : '本次日志窗口里没见到 register 行',
            registered ? 'good' : 'warn',
            '插件 register() 会打一条 coexistence mode 行；没有它说明这个进程没加载插件（或日志已轮转掉）'
          ),
          kv('enabled 生效值', showValue(plugin.enabled_effective) + '（' + (plugin.enabled_source_label || '——') + '）')
        ]
      }),
      jsxs('div', {
        className: 'hc-atl__stats',
        children: [
          jsx(Stat, {
            value: runtime.sessions_total === undefined ? '—' : String(runtime.sessions_total),
            label: '跟踪中的会话',
            hint: 'state.json 的 sessions 条数'
          }),
          jsx(Stat, { value: String(kindText.split(' · ').length ? Object.keys(kinds).length : 0), label: 'kind 种类', hint: kindText }),
          jsx(Stat, {
            value: String(counts.renamed === undefined ? '—' : counts.renamed),
            label: '成功改名',
            hint: '日志 act=renamed',
            tone: 'good'
          }),
          jsx(Stat, {
            value: String(counts.failed === undefined ? '—' : counts.failed),
            label: '失败',
            hint: '日志 act=failed',
            tone: 'bad'
          })
        ]
      }),
      jsx('div', { className: 'hc-atl__note', children: '改名次数分布（rename_count → 会话数）：' + renameText })
    ]
  })
}

function kv(label, value, tone, hint) {
  return jsxs(
    'div',
    {
      className: 'hc-atl__kv-row',
      title: hint || undefined,
      children: [
        jsx('span', { className: 'hc-atl__kv-key', children: label }),
        jsx('span', { className: 'hc-atl__kv-value' + (tone ? ' hc-atl__kv-value--' + tone : ''), children: value })
      ]
    },
    label
  )
}

/** 失败原因分类 + 最近失败明细。分类聚合是"该去修什么"的直接依据（按选中区间）。 */
function FailurePanel(props) {
  var health = props.health || {}
  var view = props.view || health.window || {}
  var classes = view.failure_classes || []
  var failures = view.recent_failures || []
  var counts = view.counts || {}

  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsx('div', { className: 'hc-atl__panel-title', children: '健康度 · 失败原因' }),
      classes.length
        ? jsx('div', {
            className: 'hc-atl__bars',
            children: classes.map(function (item) {
              var width = Math.max(2, Math.round((item.share || 0) * 100))
              return jsxs(
                'div',
                {
                  className: 'hc-atl__bar-row',
                  children: [
                    jsx('code', { className: 'hc-atl__bar-key', children: item.class }),
                    jsxs('div', {
                      className: 'hc-atl__bar-track',
                      children: [
                        jsx('div', {
                          className: 'hc-atl__bar-fill hc-atl__bar-fill--' + (item.class === 'invalid_decision_payload' ? 'bad' : 'warn'),
                          style: { width: width + '%' }
                        })
                      ]
                    }),
                    jsx('span', {
                      className: 'hc-atl__bar-count',
                      children: item.count + ' 次 · ' + percent(item.share)
                    }),
                    jsx('span', { className: 'hc-atl__bar-label', children: item.label })
                  ]
                },
                item.class
              )
            })
          })
        : jsx('div', { className: 'hc-atl__note', children: '窗口内没有失败记录。' }),
      health.invalid_payload_total
        ? jsx('div', {
            className: 'hc-atl__note',
            children:
              '其中 ' +
              health.invalid_payload_total +
              ' 条在日志里另有明确证据（model returned invalid decision payload）—— ' +
              '按会话 id 与 act=failed 对上的条数。'
          })
        : null,
      jsx('div', { className: 'hc-atl__sub-title', children: '最近 ' + failures.length + ' 条失败（新 → 旧）' }),
      failures.length
        ? jsx('div', {
            className: 'hc-atl__table',
            children: [
              jsxs('div', {
                className: 'hc-atl__table-head',
                children: [
                  jsx('span', { children: '会话' }),
                  jsx('span', { children: '原因分类' }),
                  jsx('span', { children: '耗时' }),
                  jsx('span', { children: '重试' }),
                  jsx('span', { children: '时间' })
                ]
              }, 'head')
            ].concat(
              failures.map(function (item, index) {
                return jsxs(
                  'div',
                  {
                    className: 'hc-atl__table-row',
                    children: [
                      jsx('code', { className: 'hc-atl__sid', title: item.session_id, children: item.session_id }),
                      jsx('span', {
                        className: 'hc-atl__cell',
                        title: item.reason || '',
                        children: item.class_label || item.class
                      }),
                      jsx('span', {
                        className: 'hc-atl__cell hc-atl__num',
                        children: item.dur_ms ? item.dur_ms + ' ms' : '—'
                      }),
                      jsx('span', {
                        className: 'hc-atl__cell hc-atl__num',
                        children: item.attempts ? '第 ' + item.attempts + ' 次' : '—'
                      }),
                      jsx('span', { className: 'hc-atl__cell hc-atl__muted', children: item.ts || '—' })
                    ]
                  },
                  'fail' + index
                )
              })
            )
          })
        : jsx('div', { className: 'hc-atl__note', children: '没有失败明细可列。' }),
      jsx('div', {
        className: 'hc-atl__note',
        children:
          '跳过 ' +
          (counts.skipped || 0) +
          ' 次不计入失败率（跳过 = 判过但没动手，例如' +
          (health.skip_reasons && health.skip_reasons.length ? '「' + health.skip_reasons[0].reason + '」' : '消息太少') +
          '）。'
      })
    ]
  })
}

/** 重试队列：带 finalize / retry 块的会话，多久之后重试。 */
function RetryPanel(props) {
  var retry = props.retry || {}
  var entries = retry.entries || []
  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsx('div', { className: 'hc-atl__panel-title', children: '重试队列' }),
      jsxs('div', {
        className: 'hc-atl__stats',
        children: [
          jsx(Stat, { value: String(retry.count || 0), label: '在等重试的会话', hint: 'finalize = ' + (retry.finalize_count || 0) + ' · retry = ' + (retry.retry_count || 0) }),
          jsx(Stat, { value: fromSeconds(retry.earliest), label: '最早重试时间', hint: '插件写的是秒级 epoch，这里换算成相对时间' }),
          jsx(Stat, { value: fromSeconds(retry.latest), label: '最晚重试时间', hint: '队列里最大的 next_retry_at' })
        ]
      }),
      entries.length
        ? jsx('div', {
            className: 'hc-atl__table',
            children: [
              jsxs('div', {
                className: 'hc-atl__table-head',
                children: [
                  jsx('span', { children: '会话' }),
                  jsx('span', { children: '块' }),
                  jsx('span', { children: '尝试' }),
                  jsx('span', { children: '类别' }),
                  jsx('span', { children: '下次重试' })
                ]
              }, 'head')
            ].concat(
              entries.map(function (item, index) {
                return jsxs(
                  'div',
                  {
                    className: 'hc-atl__table-row',
                    children: [
                      jsx('code', { className: 'hc-atl__sid', title: item.session_id, children: item.session_id }),
                      jsx('span', { className: 'hc-atl__cell', children: item.block }),
                      jsx('span', { className: 'hc-atl__cell hc-atl__num', children: String(item.attempts || 0) }),
                      jsx('span', {
                        className: 'hc-atl__cell',
                        title: item.reason || '',
                        children: item.retry_class || '—'
                      }),
                      jsx('span', { className: 'hc-atl__cell hc-atl__muted', children: fromSeconds(item.next_retry_at) })
                    ]
                  },
                  'retry' + index + item.block
                )
              })
            )
          })
        : jsx('div', { className: 'hc-atl__note', children: '队列是空的：没有会话在等重试。' }),
      jsx('div', {
        className: 'hc-atl__note',
        children: '队列内容来自 state.json 的 finalize / retry 块，**本面板只读它**（插件自己负责推进）。'
      })
    ]
  })
}

/** 每会话明细：当前标题 / 期望标题 / kind / rename_count，能跳到会话。 */
function SessionsPanel(props) {
  var runtime = props.runtime || {}
  var rows = runtime.sessions || []
  var onOpen = props.onOpen
  var manual = props.manual || {}

  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsx('div', { className: 'hc-atl__panel-title', children: '每会话明细' }),
      rows.length
        ? jsx('div', {
            className: 'hc-atl__table hc-atl__table--wide',
            children: [
              jsxs('div', {
                className: 'hc-atl__table-head',
                children: [
                  jsx('span', { children: '会话 id' }),
                  jsx('span', { children: '当前标题' }),
                  jsx('span', { children: '期望标题' }),
                  jsx('span', { children: 'kind' }),
                  jsx('span', { children: '改名次数' }),
                  jsx('span', { children: '操作' })
                ]
              }, 'head')
            ].concat(
              rows.map(function (item, index) {
                var differs =
                  item.expected_title !== null &&
                  item.expected_title !== undefined &&
                  item.expected_title !== item.base_title
                return jsxs(
                  'div',
                  {
                    className: 'hc-atl__table-row',
                    children: [
                      jsx('code', { className: 'hc-atl__sid', title: item.session_id, children: item.session_id }),
                      jsx('span', {
                        className: 'hc-atl__cell hc-atl__title',
                        title: item.base_title || '',
                        children: showValue(item.base_title)
                      }),
                      jsx('span', {
                        className: 'hc-atl__cell hc-atl__title' + (differs ? ' hc-atl__title--diff' : ''),
                        title: differs ? '与当前标题不同：插件认为该改成这个' : '与当前标题一致',
                        children: showValue(item.expected_title)
                      }),
                      jsx('span', { className: 'hc-atl__cell', children: item.kind }),
                      jsx('span', { className: 'hc-atl__cell hc-atl__num', children: String(item.rename_count) }),
                      jsx('span', {
                        className: 'hc-atl__cell',
                        children: onOpen
                          ? jsx('button', {
                              type: 'button',
                              className: 'hc-atl__link',
                              title: '在桌面端跳到这个会话（没有宿主导航能力时说明原因）',
                              onClick: function () {
                                onOpen(item.session_id)
                              },
                              children: '打开会话'
                            })
                          : jsx('span', { className: 'hc-atl__muted', children: '—' })
                      })
                    ]
                  },
                  item.session_id + index
                )
              })
            )
          })
        : jsx('div', {
            className: 'hc-atl__note',
            children: 'state.json 里没有被跟踪的会话（插件只在需要跟踪时才写记录）。'
          }),
      jsxs('div', {
        className: 'hc-atl__manual',
        children: [
          jsx('div', { className: 'hc-atl__manual-title', children: '手动操作（没有做成按钮，原因在下面）' }),
          jsx('div', { className: 'hc-atl__note', children: manual.reason || '' }),
          jsxs('div', {
            className: 'hc-atl__cmds',
            children: [
              jsxs('div', {
                className: 'hc-atl__cmd',
                children: [
                  jsx('span', { className: 'hc-atl__cmd-label', children: '重命名单个会话' }),
                  jsx('code', { children: manual.session_command || '/autotitler rename-now <id>' })
                ]
              }),
              jsxs('div', {
                className: 'hc-atl__cmd',
                children: [
                  jsx('span', { className: 'hc-atl__cmd-label', children: '看整批重算会动哪些会话（只预览）' }),
                  jsx('code', { children: manual.queue_command || '/autotitler retitle-all --dry-run' })
                ]
              })
            ]
          }),
          jsx('div', { className: 'hc-atl__note', children: manual.note || '' })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// C. 运行日志（只读 · 脱敏）
// ---------------------------------------------------------------------------
/**
 * titler 的 audit 行。
 *
 * 只读：没有删除/清空入口，后端也没有对应路由。脱敏：只显示 sid / action / reason / 耗时，
 * 会话正文与提示词不在这份数据里（后端只回这五个字段）。
 */
function LogPanel(props) {
  var agent = props.agent
  var ready = props.ready !== false

  var actState = useState('')
  var act = actState[0]
  var setAct = actState[1]
  var sidInputState = useState('')
  var sidInput = sidInputState[0]
  var setSidInput = sidInputState[1]
  var sidState = useState('')
  var sid = sidState[0]
  var setSid = sidState[1]

  var query = useQuery(
    logsKey(agent, act, sid),
    function () {
      return fetchLogs(agent, act, sid)
    },
    { staleMs: 5000, enabled: ready }
  )

  var view = query.status === 'ok' ? unwrap(query.data) : null
  var rows = (view && view.rows) || []
  var facets = (view && view.acts) || []

  function applySid() {
    setSid(sidInput.trim())
  }

  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsxs('div', {
        className: 'hc-atl__panel-head',
        children: [
          jsx('div', { className: 'hc-atl__panel-title', children: '运行日志（audit 行）' }),
          jsxs('div', {
            className: 'hc-atl__panel-actions',
            children: [
              jsx('span', {
                className: 'hc-atl__muted',
                children: view
                  ? '命中 ' + (view.matched || 0) + ' 条 / 共 ' + (view.total || 0) + ' 条 · 显示 ' + rows.length + ' 条'
                  : '—'
              }),
              jsx('button', {
                type: 'button',
                className: 'hc-btn',
                title: '重读日志（只读）',
                onClick: function () {
                  query.reload()
                },
                children: '刷新'
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        className: 'hc-atl__logfilters',
        children: [
          jsx('span', { className: 'hc-atl__logfilter-label', children: '按 action' }),
          jsxs('div', {
            className: 'hc-atl__logfilter-chips',
            children: [
              jsx(
                'button',
                {
                  type: 'button',
                  className: 'hc-atl__chipbtn' + (act === '' ? ' hc-atl__chipbtn--active' : ''),
                  'data-act': 'all',
                  onClick: function () {
                    setAct('')
                  },
                  children: '全部'
                },
                'actall'
              )
            ].concat(
              facets.map(function (item) {
                return jsx(
                  'button',
                  {
                    type: 'button',
                    className:
                      'hc-atl__chipbtn' +
                      (act === item.act ? ' hc-atl__chipbtn--active' : '') +
                      (item.act === 'failed' ? ' hc-atl__chipbtn--bad' : ''),
                    'data-act': item.act,
                    onClick: function () {
                      setAct(act === item.act ? '' : item.act)
                    },
                    children: item.act + ' × ' + item.count
                  },
                  'act' + item.act
                )
              })
            )
          }),
          jsx('span', { className: 'hc-atl__logfilter-label', children: '按会话 id' }),
          jsx('input', {
            className: 'hc-atl__input hc-atl__input--narrow',
            placeholder: '例如 local-ee',
            value: sidInput,
            'data-role': 'sid-input',
            onChange: function (event) {
              setSidInput(event.target.value)
            },
            onKeyDown: function (event) {
              if (event.key === 'Enter') applySid()
            }
          }),
          jsx('button', { type: 'button', className: 'hc-btn', 'data-role': 'sid-apply', onClick: applySid, children: '筛选' }),
          jsx('button', {
            type: 'button',
            className: 'hc-atl__link',
            onClick: function () {
              setSidInput('')
              setSid('')
              setAct('')
            },
            children: '清除筛选'
          })
        ]
      }),
      query.status === 'error'
        ? jsx('div', { className: 'hc-atl__warn', children: '日志读取失败：' + query.error })
        : null,
      query.status === 'loading' && !view
        ? jsx(Skeleton, { rows: 4, label: '正在读日志…' })
        : rows.length
          ? jsx('div', {
              className: 'hc-atl__table hc-atl__table--log',
              children: [
                jsxs('div', {
                  className: 'hc-atl__table-head',
                  children: [
                    jsx('span', { children: '时间' }),
                    jsx('span', { children: '会话' }),
                    jsx('span', { children: 'action' }),
                    jsx('span', { children: '原因' }),
                    jsx('span', { children: '耗时' })
                  ]
                }, 'head')
              ].concat(
                rows.map(function (item, index) {
                  return jsxs(
                    'div',
                    {
                      className: 'hc-atl__table-row hc-atl__table-row--' + (item.act === 'failed' ? 'bad' : 'ok'),
                      'data-act': item.act,
                      children: [
                        jsx('span', { className: 'hc-atl__cell hc-atl__muted', children: item.ts || '—' }),
                        jsx('code', { className: 'hc-atl__sid', title: item.sid, children: item.sid }),
                        jsx('span', { className: 'hc-atl__cell', children: item.act }),
                        jsx('span', {
                          className: 'hc-atl__cell hc-atl__cell--reason',
                          title: (item.class ? item.class + '：' : '') + (item.reason || ''),
                          children: item.reason || (item.class ? '(' + item.class + ')' : '—')
                        }),
                        jsx('span', {
                          className: 'hc-atl__cell hc-atl__num',
                          children: item.dur_ms ? item.dur_ms + ' ms' : '—'
                        })
                      ]
                    },
                    'log' + index + item.ts
                  )
                })
              )
            })
          : jsx('div', {
              className: 'hc-atl__note',
              children: query.status === 'ok' ? '这个过滤条件下没有 audit 行。' : '正在读日志…'
            }),
      jsx('div', {
        className: 'hc-atl__note',
        children:
          '只读 · 脱敏 · ' +
          ((view && view.redaction_note) || '只显示 sid / action / reason / 耗时，不含会话正文；没有删除或清空日志的入口。')
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// A2. 设置编辑器（按 config_schema 动态渲染）
// ---------------------------------------------------------------------------
function SettingsEditor(props) {
  var view = props.view || {}
  var fields = view.fields || []
  var groups = view.groups && view.groups.length
    ? view.groups
    : [{ id: 'all', label: GROUP_FALLBACK, keys: fields.map(function (f) { return f.key }) }]

  var byKey = useMemo(
    function () {
      var map = {}
      fields.forEach(function (field) {
        map[field.key] = field
      })
      return map
    },
    [view]
  )

  // 用户点名的四件事置顶显示；它们不再在下方的语义分组里重复出现（同一个键只渲染一次）
  var pinned = view.pinned && view.pinned.length ? view.pinned : []
  var pinnedKeys = {}
  pinned.forEach(function (item) {
    ;(item.keys || []).forEach(function (key) {
      pinnedKeys[key] = true
    })
  })

  var draftState = useState({})
  var drafts = draftState[0]
  var setDrafts = draftState[1]
  var errorState = useState({})
  var errors = errorState[0]
  var setErrors = errorState[1]
  var statusState = useState({ busy: false, message: '', tone: '', saved: [] })
  var status = statusState[0]
  var setStatus = statusState[1]

  /** 生效值 → 草稿的初始形态（数字保持数字，字符串项保持字符串）。 */
  function seed(field) {
    return field.value === undefined || field.value === null ? '' : field.value
  }

  /**
   * 两级选择器的成对草稿：``provider`` 与 ``title_model`` 共用一个控件，
   * 所以两个键要一起喂给 ProviderModelPicker（各自取自己的草稿，没有就取生效值）。
   */
  function pairDraft() {
    var read = function (key) {
      var field = byKey[key]
      var raw = drafts[key] === undefined ? (field ? seed(field) : '') : drafts[key]
      return raw === undefined || raw === null ? '' : String(raw)
    }
    return { provider: read('provider'), title_model: read('title_model') }
  }

  var dirtyKeys = useMemo(
    function () {
      var out = []
      Object.keys(drafts).forEach(function (key) {
        var field = byKey[key]
        if (!field) return
        var next = drafts[key]
        if (coerceForSubmit(field, next) !== coerceForSubmit(field, seed(field))) out.push(key)
      })
      return out
    },
    [drafts, view]
  )

  function onEdit(key, value) {
    setDrafts(function (prev) {
      var next = {}
      Object.keys(prev).forEach(function (name) {
        next[name] = prev[name]
      })
      next[key] = value
      return next
    })
    var field = byKey[key]
    var message = field ? validateField(field, value) : ''
    setErrors(function (prev) {
      var next = {}
      Object.keys(prev).forEach(function (name) {
        next[name] = prev[name]
      })
      if (message) next[key] = message
      else delete next[key]
      return next
    })
  }

  function onReset(field) {
    onEdit(field.key, seed(field))
  }

  function currentError(field) {
    if (errors[field.key]) return errors[field.key]
    return validateField(field, drafts[field.key] === undefined ? seed(field) : drafts[field.key])
  }

  function save() {
    // 先跑一遍全量校验：一个都不许带着错误提交（后端也会再拒，但别浪费往返）。
    var blocking = []
    dirtyKeys.forEach(function (key) {
      var message = currentError(byKey[key])
      if (message) blocking.push(key + '：' + message)
    })
    if (blocking.length) {
      setStatus({ busy: false, message: '有 ' + blocking.length + ' 项填得不对，先改好再保存：' + blocking.join('；'), tone: 'bad', saved: [] })
      return
    }
    // 未修改 = 零写请求：没有 dirty 就什么都不发。
    if (!dirtyKeys.length) {
      setStatus({ busy: false, message: '没有改动，未发送任何写请求。', tone: 'muted', saved: [] })
      return
    }

    setStatus({ busy: true, message: '正在写 ' + dirtyKeys.length + ' 项…', tone: '', saved: [] })
    // 顺序：**provider 先写**。两级联动的服务端校验按「写完之后」的组合判，
    // 若先写 title_model 就会拿旧 provider 校验新模型名（必然误拒）。
    var queue = dirtyKeys.slice().sort(function (left, right) {
      var rank = function (key) { return key === 'provider' ? 0 : key === 'title_model' ? 1 : 2 }
      return rank(left) - rank(right)
    })
    var saved = []
    var failed = []
    // 乐观锁的哈希链（2026-11-03 修 409）：**必须用局部变量持有当前哈希**。
    // 队列里每个键都读同一个 view.config_digest 的话，第一个键写成功后 config.yaml 已被改写、
    // 哈希变了，第二个键仍带旧哈希 → 后端 409「配置已变化」，一批 N 项只能成功第一项。
    // 后端 PUT 返回体带写后的新 config_digest，写成功一个就用它滚动更新，供下一个键使用。
    // 不直接改 view.config_digest：那是缓存里的共享状态，末尾 refresh() 会重新读取，
    // 在这里改会让别的读取方看到「还没回读确认」的哈希。
    var currentDigest = view.config_digest || ''

    function next() {
      if (!queue.length) {
        setStatus({
          busy: false,
          message: failed.length
            ? '成功 ' + saved.length + ' 项，失败 ' + failed.length + ' 项：' + failed.join('；')
            : '已写入 ' + saved.length + ' 项并回读确认一致。',
          tone: failed.length ? 'bad' : 'good',
          saved: saved
        })
        if (saved.length) {
          refresh(settingsKey(view.agent))
          refresh(overviewKey(view.agent))
        }
        return
      }
      var key = queue.shift()
      var field = byKey[key]
      var body = { key: key, value: coerceForSubmit(field, drafts[key]) }
      if (currentDigest) body.expected_config = currentDigest
      send('/autotitler/settings', body, { method: 'PUT', agent: view.agent }).then(function (result) {
        if (result && result.ok) {
          saved.push(key)
          // 滚动到下一位：只有后端真回了新哈希才更新（老后端不带就保持原样）。
          var written = unwrap(result.data) || {}
          if (typeof written.config_digest === 'string' && written.config_digest) {
            currentDigest = written.config_digest
          }
        } else {
          failed.push(key + '：' + ((result && result.message) || '写入失败'))
        }
        next()
      })
    }
    next()
  }

  var needsRestart = view.restart_keys || []
  var restartTouched = dirtyKeys.filter(function (key) {
    return needsRestart.indexOf(key) !== -1
  })

  return jsxs('div', {
    className: 'hc-atl__panel',
    children: [
      jsxs('div', {
        className: 'hc-atl__panel-head',
        children: [
          jsx('div', { className: 'hc-atl__panel-title', children: '参数设置（按 config_schema 动态渲染）' }),
          jsxs('div', {
            className: 'hc-atl__panel-actions',
            children: [
              jsx('span', {
                className: 'hc-atl__muted',
                children: '共 ' + fields.length + ' 项 · 已改动 ' + dirtyKeys.length + ' 项'
              }),
              jsx('button', {
                type: 'button',
                className: 'hc-btn' + (status.busy ? ' hc-btn--done' : ''),
                disabled: status.busy || !dirtyKeys.length || !view.exists,
                title: view.exists
                  ? '只提交真正改过的项（未改动时一个写请求都不发）'
                  : '该 agent 没有自己的 config.yaml，写不进去',
                onClick: save,
                children: status.busy ? '写入中…' : '保存改动'
              })
            ]
          })
        ]
      }),
      jsx('div', { className: 'hc-atl__source-legend', children: (view.notes || [])[0] || '' }),
      restartTouched.length
        ? jsx('div', {
            className: 'hc-atl__warn',
            children: '本次改动里有 ' + restartTouched.join('、') + '：这两项是插件加载期读的开关，**需要重启 Hermes 才生效**。'
          })
        : null,
      status.message
        ? jsx('div', {
            className: 'hc-atl__save-status hc-atl__save-status--' + (status.tone || 'muted'),
            role: 'status',
            children: status.message
          })
        : null,
      pinned.length
        ? jsxs('div', {
            className: 'hc-atl__pinned',
            children: [
              jsx('div', {
                className: 'hc-atl__pinned-title',
                children: '常用四项（你点名要能改的）'
              }),
              jsx('div', {
                className: 'hc-atl__pinned-note',
                children: '下面还有全部 ' + fields.length + ' 项，按语义分组；这四项不重复出现。'
              })
            ].concat(
              pinned.map(function (item) {
                return jsxs(
                  'div',
                  {
                    className: 'hc-atl__pinned-block',
                    'data-pinned': item.id,
                    children: [
                      jsx('div', { className: 'hc-atl__pinned-head', children: item.label }),
                      jsx('div', { className: 'hc-atl__pinned-hint', children: item.hint || '' })
                    ].concat(
                      (item.keys || []).map(function (key) {
                        var field = byKey[key]
                        if (!field) return null
                        var draft = drafts[key] === undefined ? seed(field) : drafts[key]
                        return jsx(
                          FieldRow,
                          {
                            field: field,
                            draft: draft,
                            error: currentError(field),
                            dirty: dirtyKeys.indexOf(key) !== -1,
                            disabled: status.busy || !view.exists || field.writable === false,
                            agent: view.agent,
                            pair: pairDraft(),
                            onEdit: onEdit,
                            onReset: onReset
                          },
                          'pin-' + key
                        )
                      })
                    )
                  },
                  'pinned-' + item.id
                )
              })
            )
          })
        : null,
      groups
        .filter(function (group) {
          return (group.keys || []).filter(function (key) {
            return !pinnedKeys[key]
          }).length
        })
        .map(function (group) {
          var visible = (group.keys || []).filter(function (key) {
            return !pinnedKeys[key]
          })
          return jsxs(
            'div',
            {
              className: 'hc-atl__group',
              children: [
                jsx('div', {
                  className: 'hc-atl__group-title',
                  children: group.label + '（' + visible.length + '）'
                }),
                jsx('div', {
                  className: 'hc-atl__group-body',
                  children: visible.map(function (key) {
                    var field = byKey[key]
                    if (!field) return null
                    var draft = drafts[key] === undefined ? seed(field) : drafts[key]
                    return jsx(
                      FieldRow,
                      {
                        field: field,
                        draft: draft,
                        error: currentError(field),
                        dirty: dirtyKeys.indexOf(key) !== -1,
                        disabled: status.busy || !view.exists || field.writable === false,
                        agent: view.agent,
                        pair: pairDraft(),
                        onEdit: onEdit,
                        onReset: onReset
                      },
                      key
                    )
                  })
                })
              ]
            },
            group.id
          )
        })
    ]
  })
}




// ---------------------------------------------------------------------------
// 主分区
// ---------------------------------------------------------------------------
/**
 * 打开某个会话。
 *
 * 桌面端提供 `host.openSession(storedSessionId)`（`apps/desktop/src/sdk/index.ts`），
 * 但 `ctx` 上没有这个门（`contrib/plugin.ts::createPluginContext` 只有 rest/os/pet/…）。
 * 所以这里**先探测**再决定：拿不到就说清"需要在对应会话里手动打开"，绝不假装跳过去了。
 */
function makeSessionOpener() {
  var sdk =
    typeof window !== 'undefined' && window.__HERMES_PLUGIN_SDK__
      ? window.__HERMES_PLUGIN_SDK__
      : null
  var open =
    sdk && typeof sdk.openSession === 'function'
      ? sdk.openSession
      : sdk && sdk.api && typeof sdk.api.openSession === 'function'
        ? function (id) {
            return sdk.api.openSession(id)
          }
        : null
  if (!open) return null
  return function (sessionId) {
    try {
      var result = open(sessionId)
      if (result && typeof result.catch === 'function') result.catch(function () {})
      return true
    } catch (e) {
      return false
    }
  }
}

/**
 * 分区主体。
 *
 * props 由外壳注入：`agent`（顶部作用域的当前值）、`ready`（作用域是否已确定）。
 * `agent` 一变两个 query 的 key 就变 —— 数据和作用域天然对齐，不会出现"看着 code-worker
 * 的标题，读的是 default 的配置"。
 */
export function AutotitlerSection(props) {
  var agent = (props && props.agent) || 'default'
  var ready = !props || props.ready !== false

  var tabState = useState('monitor')
  var tab = tabState[0]
  var setTab = tabState[1]
  var toastState = useState('')
  var toast = toastState[0]
  var setToast = toastState[1]

  // 统计区间：缺省 24 小时。切换只改口径（后端三个区间一次算好），不写任何东西。
  var windowState = useState(DEFAULT_WINDOW)
  var windowId = windowState[0]
  var setWindowId = windowState[1]

  var settingsQuery = useQuery(
    settingsKey(agent),
    function () {
      return fetchSettings(agent)
    },
    { staleMs: STALE_MS, enabled: ready }
  )
  var overviewQuery = useQuery(
    overviewKey(agent),
    function () {
      // 一次取三个区间（后端把 1h / 24h / all 都算好），切换区间不再发请求：
      // 少一次往返、也不会因为 refetch 把整屏闪成骨架。
      return fetchOverview(agent, DEFAULT_WINDOW)
    },
    { staleMs: STALE_MS, enabled: ready }
  )

  var settingsView = settingsQuery.status === 'ok' ? unwrap(settingsQuery.data) : null
  var overviewView = overviewQuery.status === 'ok' ? unwrap(overviewQuery.data) : null

  // 会话跳转能力的探测只做一次（SDK 在模块求值时可能还没装上，所以放在 effect 里）
  var openerState = useState(null)
  var opener = openerState[0]
  var setOpener = openerState[1]
  useEffect(function () {
    setOpener(makeSessionOpener())
  }, [])

  function onOpenSession(sessionId) {
    if (!opener) {
      setToast('当前宿主没有暴露会话跳转接口，请在左侧会话列表里手动打开：' + sessionId)
      return
    }
    var ok = opener(sessionId)
    setToast(ok ? '已请求打开会话 ' + sessionId : '打开会话失败：' + sessionId)
  }

  if (!ready || settingsQuery.status === 'loading' || overviewQuery.status === 'loading') {
    return jsx('div', { className: 'hc-pane__body', children: jsx(Skeleton, { rows: 8, label: '正在读插件参数与运行日志…' }) })
  }

  if (settingsQuery.status === 'error' && overviewQuery.status === 'error') {
    return jsx('div', {
      className: 'hc-pane__body',
      children: jsx(ErrorState, {
        title: '自动命名插件读不出来',
        message: settingsQuery.error || overviewQuery.error,
        onRetry: function () {
          settingsQuery.reload()
          overviewQuery.reload()
        }
      })
    })
  }

  if (!settingsView && !overviewView) {
    return jsx(Empty, {
      title: '没有可显示的内容',
      hint: '后端两个端点都返回了空结构。先确认 hermes-auto-titler 装在这个 home 下。'
    })
  }

  var health = (overviewView && overviewView.health) || {}
  var selectedView = (health.windows && health.windows[windowId]) || health.window || health
  var counts = selectedView.counts || health.counts || {}

  var tabs = [
    { id: 'monitor', label: '运行监控' },
    { id: 'logs', label: '运行日志' },
    { id: 'settings', label: '参数设置' + (settingsView && settingsView.field_count ? '（' + settingsView.field_count + '）' : '') }
  ]

  return jsxs('div', {
    className: 'hc-pane__body hc-atl',
    children: [
      jsxs('div', {
        className: 'hc-atl__tabs',
        children: [
          tabs.map(function (item) {
            var active = item.id === tab
            return jsx(
              'button',
              {
                type: 'button',
                className: 'hc-atl__tab' + (active ? ' hc-atl__tab--active' : ''),
                'aria-current': active ? 'page' : undefined,
                onClick: function () {
                  setTab(item.id)
                },
                children: item.label
              },
              item.id
            )
          }),
          jsxs('div', {
            className: 'hc-atl__tabs-right',
            children: [
              jsx('span', {
                className: 'hc-atl__muted',
                title: '作用域来自顶部选择器；两个端点都按这个 agent 取数',
                children: '作用域 ' + agent
              }),
              jsx('button', {
                type: 'button',
                className: 'hc-btn',
                title: '重读参数与日志（不清屏）',
                onClick: function () {
                  settingsQuery.reload()
                  overviewQuery.reload()
                },
                children: '重读'
              })
            ]
          }, 'tabs-right')
        ]
      }, 'tabs'),
      toast
        ? jsx('div', {
            className: 'hc-atl__toast',
            role: 'status',
            children: [
              jsx('span', { children: toast }, 'msg'),
              jsx('button', {
                type: 'button',
                className: 'hc-atl__link',
                onClick: function () {
                  setToast('')
                },
                children: '知道了'
              }, 'dismiss')
            ]
          }, 'toast')
        : null,
      settingsQuery.status === 'error'
        ? jsx('div', { className: 'hc-atl__warn', children: '参数读取失败：' + settingsQuery.error }, 'settings-error')
        : null,
      overviewQuery.status === 'error'
        ? jsx('div', { className: 'hc-atl__warn', children: '运行数据读取失败：' + overviewQuery.error }, 'overview-error')
        : null,
      tab === 'monitor'
        ? jsxs('div', {
            className: 'hc-atl__stack',
            children: [
              jsx(WindowBar, { value: windowId, onChange: setWindowId, health: health }, 'windowbar'),
              jsx(HealthBanner, { health: health, view: selectedView }, 'banner'),
              jsx(BaselinePanel, { health: health }, 'baseline'),
              jsxs('div', {
                className: 'hc-atl__grid',
                children: [
                  jsx(PluginOverview, {
                    plugin: overviewView ? overviewView.plugin : {},
                    runtime: overviewView ? overviewView.runtime : {},
                    health: selectedView
                  }, 'plugin'),
                  jsx(RetryPanel, { retry: overviewView ? overviewView.retry : {} }, 'retry')
                ]
              }, 'grid'),
              jsx(FailurePanel, { health: health, view: selectedView }, 'failures'),
              jsx(SessionsPanel, {
                runtime: overviewView ? overviewView.runtime : {},
                manual: overviewView ? overviewView.manual : {},
                onOpen: opener ? onOpenSession : null
              }, 'sessions'),
              (overviewView && overviewView.notes && overviewView.notes.length)
                ? jsx('div', {
                    className: 'hc-atl__panel',
                    children: [
                      jsx('div', { className: 'hc-atl__panel-title', children: '这一版读数的口径与限制' }, 'title'),
                      jsx('ul', {
                        className: 'hc-atl__notes',
                        children: overviewView.notes.map(function (note, index) {
                          return jsx('li', { children: note }, 'note' + index)
                        })
                      }, 'list')
                    ]
                  }, 'notes')
                : null
            ]
          }, 'monitor')
        : tab === 'logs'
          ? jsx('div', {
              className: 'hc-atl__stack',
              children: jsx(LogPanel, { agent: agent, ready: ready })
            }, 'logs')
          : jsxs('div', {
              className: 'hc-atl__stack',
              children: [
                settingsView
                  ? jsx(SettingsEditor, { view: settingsView }, 'editor')
                  : jsx(Empty, {
                      title: '参数清单为空',
                      hint: '没读到插件 manifest 的 config_schema：确认 hermes-auto-titler 装在这个 home 下。'
                    }, 'empty'),
                (settingsView && settingsView.notes && settingsView.notes.length)
                  ? jsx('div', {
                      className: 'hc-atl__panel',
                      children: [
                        jsx('div', { className: 'hc-atl__panel-title', children: '读写规则' }, 'title'),
                        jsx('ul', {
                          className: 'hc-atl__notes',
                          children: settingsView.notes.map(function (note, index) {
                            return jsx('li', { children: note }, 'snote' + index)
                          })
                        }, 'list')
                      ]
                    }, 'notes')
                  : null,
                jsx('div', {
                  className: 'hc-atl__note',
                  children:
                    '改名成功 ' +
                    (counts.renamed || 0) +
                    ' 次 / 失败 ' +
                    (counts.failed || 0) +
                    ' 次（' +
                    ((selectedView && selectedView.label) || '全部历史') +
                    '口径，随时可切回「运行监控」看明细）。'
                }, 'summary')
              ]
            }, 'settings')
    ]
  })
}
