/**
 * hermes-console · 项目分组分区（C8c：面板写操作全部接通阶段 2 的后端路由）。
 *
 * 本分区回答一个问题：「哪些会话应该归到哪个项目去，谁判的，凭什么判」，以及
 * **手动把归属改对**：建/改名/删项目、多选批量归入、单条内联切换、撤销面板写入、自动模式开关。
 *
 * 写操作一律走既有后端（dashboard/api_autogroup.py + api_autogroup_panel.py），本文件
 * 只做三件事：发请求 → **回读**（重新 GET 归属表/项目表/台账）→ 按实际状态说话。
 *
 * 六条规矩：
 *   1. **确认前不写**：批量归入必须先出预览弹层，用户点「确认归入」才发 confirm=true；
 *   2. **回读才算数**：写成功后重新读 /autogroup/suggestions，逐条比 slug/status/batch_id，
 *      请求 200 不当验收（撤销同理：重新读归属表核对是否还原到写入前）；
 *   3. **口径写脸上**：状态条三个数字各自带一句口径（数字来自后端哪一列）；
 *   4. **扫描是异步 job**：POST /autogroup/scan 只返回 job_id，必须轮询
 *      GET /autogroup/job/{id}（间隔 ≥2s，上限 3 分钟），完成后刷新状态条与列表；
 *      失败一律把后端 error.message 原样交给 ErrorState，不自己编原因；
 *   5. **三态复用** lib/states.js 的 Skeleton / Empty / ErrorState，不另写一套；
 *   6. 颜色只走 var(--ui-*)；本文件不出现字面色值（构建脚本会检查）。
 *
 * 口径边界：数据读的是 Hermes 主目录的 state.db / projects.db / auto-group 库，
 * 与顶部作用域选择器无关（分区标题旁的「全部 agent」chip 说的就是这件事）。
 */
import { jsx as jsxRuntime, jsxs as jsxsRuntime } from 'react/jsx-runtime'
import { useEffect, useState } from 'react'

import { absDate, num, relTime } from '../lib/format.js'
import { refresh, useQuery } from '../lib/query.js'
import { get, send } from '../lib/rest.js'
import { Empty, ErrorState, Skeleton } from '../lib/states.js'
import { AcceptRowButton, assignBlockReason } from './autogroup-actions.js'
import { AutogroupAiPanel } from './autogroup-ai.js'
import { AutogroupModePanel, MODE_REBUILD_ALL, MODE_UNGROUPED_ONLY } from './autogroup-mode.js'
import { AutogroupModelPanel } from './autogroup-model.js'

/**
 * React 19 的 dev 运行时会对「props 里带 key」发出警告（key 必须作为 jsx 的第三个参数）。
 * 本文件不少组件把 key 写在 props 对象里（和兄弟属性放一起更好读），这里统一把
 * `props.key` 提出来当第三个参数传下去：渲染结果不变，控制台也不再吐警告。
 * 只影响本文件；别的分区本来就把 key 放在第三个参数上。
 */
function withoutKey(props) {
  if (!props || !Object.prototype.hasOwnProperty.call(props, 'key')) return null
  var next = {}
  Object.keys(props).forEach(function (name) {
    if (name !== 'key') next[name] = props[name]
  })
  return next
}

function jsx(type, props, key) {
  var cleaned = withoutKey(props)
  if (cleaned) return jsxRuntime(type, cleaned, props.key)
  return jsxRuntime(type, props, key)
}

function jsxs(type, props, key) {
  var cleaned = withoutKey(props)
  if (cleaned) return jsxsRuntime(type, cleaned, props.key)
  return jsxsRuntime(type, props, key)
}

/** 每页条数（卡面要求默认 20 条/页）。 */
var PAGE_SIZE = 20

/** 一次扫描最多看多少个会话（后端把 limit 钳到 1..200）。 */
var SCAN_LIMIT = 60

/** 轮询间隔：卡面要求 ≥2s。第一次立刻打一枪（job 可能已经完成），之后每 2s 一次。 */
var POLL_MS = 2000

/** 轮询总上限：3 分钟。超时按失败处理，并把 job_id 告诉人（去后端日志能追）。 */
var SCAN_TIMEOUT_MS = 180000

/** 状态条与列表的缓存窗口（扫描完会主动 reload，所以这里只防「切来切去反复拉」）。 */
var STALE_MS = 15000

function fetchStatus() {
  return get('/autogroup/status')
}

function fetchPage(offset) {
  return get('/autogroup/suggestions', { params: { status: 'suggested', limit: PAGE_SIZE, offset: offset } })
}

/** 撤销台账一次读多少条（后端把 limit 钳到 1..200）。 */
var OPS_LIMIT = 20

/** 回读核对一次拉多少条 applied 行（后端把 limit 钳到 1..200）。 */
var READBACK_LIMIT = 200

/** 项目管理：GET /autogroup/projects（含目录状态、归属计数、能不能删）。 */
function fetchProjects() {
  return get('/autogroup/projects')
}

/** 自动模式：GET /autogroup/auto（默认关闭；last_tick 是后端记的，前端不算）。 */
function fetchAuto() {
  return get('/autogroup/auto')
}

/** 面板写操作台账：GET /autogroup/ops（倒序，带逐会话 from→to 与是否已撤销）。 */
function fetchOps() {
  return get('/autogroup/ops', { params: { limit: OPS_LIMIT } })
}

/** 回读源：GET /autogroup/suggestions?status=applied —— 写后重新读，不以请求成功当证据。 */
function fetchApplied() {
  return get('/autogroup/suggestions', { params: { status: 'applied', limit: READBACK_LIMIT } })
}

/** 列表载荷 → {session_id: item}（回读核对只信后端回的现状行）。 */
function indexBySession(payload) {
  var items = (payload && payload.data && payload.data.items) || []
  var out = {}
  items.forEach(function (item) {
    if (item && item.session_id) out[item.session_id] = item
  })
  return out
}

/** 回读源（撤销核对用）：applied + suggested 两页合并 —— 撤销可能把行退回 suggested。 */
function fetchAllRows() {
  return Promise.all([
    get('/autogroup/suggestions', { params: { status: 'applied', limit: READBACK_LIMIT } }),
    get('/autogroup/suggestions', { params: { status: 'suggested', limit: READBACK_LIMIT } })
  ]).then(function (results) {
    var first = results[0]
    var second = results[1]
    if (first && first.ok === false) return first
    if (second && second.ok === false) return second
    var merged = {}
    var applied = indexBySession(first && first.data)
    var suggested = indexBySession(second && second.data)
    Object.keys(applied).forEach(function (key) {
      merged[key] = applied[key]
    })
    Object.keys(suggested).forEach(function (key) {
      merged[key] = suggested[key]
    })
    return {
      ok: true,
      data: {
        data: {
          items: Object.keys(merged).map(function (key) {
            return merged[key]
          })
        }
      }
    }
  })
}

/** 项目清单载荷 → items 数组。 */
function projectsOf(payload) {
  return (payload && payload.data && payload.data.projects) || []
}

/** 判定依据一句话：rule 给命中的关键词，llm 给置信度与理由，其它原样给后端说法。 */
function basisText(item) {
  var basis = item.basis || {}
  if (basis.kind === 'rule') {
    var words = basis.keywords || []
    return words.length ? '规则命中：' + words.join('、') : basis.label || basis.reason || '规则命中'
  }
  if (basis.kind === 'llm') {
    var confidence = basis.confidence
    var conf =
      confidence === null || confidence === undefined ? '' : '置信度 ' + Math.round(confidence * 100) + '%'
    var reason = basis.reason ? '：' + basis.reason : ''
    return '模型判断' + (conf ? '（' + conf + '）' : '') + reason
  }
  return basis.label || basis.reason || '无依据'
}


/** ---------------------------------------------------------------- 新布局数据层（t_1dea3c49） */

/** 「全部 agent」的哨兵值。空串代表「还没选」，两者语义不同，不能混用。 */
var AGENT_ALL = '__all__'

/**
 * 桌面端 sidebar 排除的来源（宿主 `SIDEBAR_EXCLUDED_SOURCES` 的同一份名单）。
 * 这里只是**二次过滤**：宿主 SQL 已经把 children / 归档排除掉了，前端再按 source 与
 * message_count 兜一层，口径写进页面的 chip 里，不靠猜。
 */
var EXCLUDED_SOURCES = ['acp', 'kanban', 'oneshot', 'subagent', 'tool']

/**
 * 后端候选口径：`GET /autogroup/ai/scope?agent=X`（t_bba9216d）。
 *
 * **按钮上的 N 必须来自这里，不能自己数会话列表。** 会话列表只取最近 100 条
 * （`SESSIONS_LIMIT`，后端钳 1..100），而且 `archived` / `hidden` / 压缩续接子行
 * 由宿主 SQL 决定；自己数一份就会重演「按钮说 21 条、点下去说 0 条」这场事故 ——
 * 那一版前后端读的还是两套库（前端 profiles/*，后端只读根库）。
 * 现在两边都吃 `/autogroup/ai/scope` 的 `total_available`，与实际跑批的候选集同源。
 */
function fetchScope(agent) {
  var scoped = !!agent && agent !== AGENT_ALL
  return get('/autogroup/ai/scope', scoped ? { params: { agent: agent } } : undefined)
}

/** 每个 agent 一次取多少条会话（后端把 limit 钳到 1..100）。 */
var SESSIONS_LIMIT = 100

/**
 * 单 agent 视图下前端最多翻几页会话列表（t_bba9216d）。
 *
 * 为什么必须翻页：`/overview/sessions` 单次上限 100 条，而会话库动辄几百上千行
 * （实测 `profiles/worker/state.db` 514 行、根库 715 行）。只取「最近 100 条」时，
 * 那些**不在最近 100 条里**的未分组会话在列表里根本不出现 —— 列表说「未分组 0」，
 * 按钮（读后端 `candidate_scope` 全库口径）却说「未分组 4」，同一屏两个数打架。
 * 这正是本卡「按钮 N == 符合条件 N」要求里前端那一半。
 *
 * 上限 2000 条（20 页）：取不到底时由调用方在界面上明写 truncated，不谎称「取全了」，
 * 也不无限翻页把浏览器打满。停止条件有两条 —— 取满上限，或某页不足一页（尾页）。
 */
var SESSIONS_SCAN_MAX = 2000

/**
 * 按 offset 翻页取**一个 agent** 的全部会话行（最多 `SESSIONS_SCAN_MAX` 条）。
 *
 * 返回 `{rows, total, truncated}`：
 *
 *   · `total` = 后端报的该 agent 会话总数（拿不到时 null）；
 *   · `truncated` = **我真的没取完**（撞上限 或 中途某页失败）。**不**用
 *     `rows.length < total` 判定 —— 宿主的 `session_count` 与 `list_sessions_rich`
 *     在某些库上差 1 条（压缩链被 `exclude_superseded_segments` 折叠），拿它当
 *     「没取完」会**永远**报 truncated，把真信号淹掉。停止原因才算数。
 */
function fetchSessionsAllPages(agent) {
  var rows = []
  var total = null
  var failed = false
  function page(offset) {
    return get('/overview/sessions', { params: { agent: agent, limit: SESSIONS_LIMIT, offset: offset } })
      .then(function (res) {
        if (!res || res.ok === false) {
          failed = true
          return null
        }
        var payload = unwrap(res.data)
        var inner = unwrap(payload)
        var got = (payload && Array.isArray(payload.sessions)) ? payload.sessions
          : (inner && Array.isArray(inner.sessions)) ? inner.sessions : []
        var seenTotal = payload && typeof payload.total === 'number' ? payload.total
          : inner && typeof inner.total === 'number' ? inner.total : null
        if (seenTotal != null) total = seenTotal
        got.forEach(function (row) { rows.push(row) })
        if (!got.length) return null                              // 空页 = 到底了
        if (got.length < SESSIONS_LIMIT) return null              // 尾页
        if (total != null && rows.length >= total) return null    // 取满
        if (rows.length >= SESSIONS_SCAN_MAX) return null         // 撞上限
        return page(offset + got.length)
      })
  }
  return page(0).then(function () {
    return {
      rows: rows,
      total: total,
      truncated: failed || rows.length >= SESSIONS_SCAN_MAX
    }
  })
}

/** 一次读多少条 applied 归属行（后端把 limit 钳到 1..200）。 */
var OWNERSHIP_LIMIT = 200

/**
 * 「待确认建议」一页取多少条（后端把 limit 钳到 1..200，所以 200 就是单次上限）。
 *
 * 为什么不是「一次全取」：后端 `SUGGESTIONS_MAX_LIMIT = 200` 是硬上限，建议数 >200 时
 * 一次请求**拿不全**。旧写法固定 limit=200 却把结果整表渲染、徽标用行数算，
 * 于是建议 500 条时状态条 500 / 徽标 200 / 明细 200 行三个数字互相矛盾。
 * 现在：**总数只认后端 `data.total`**，明细按 offset 分页追加（「加载更多」），
 * 抽屉里明写「显示 X / 共 N 条」，剩余条数一直挂在按钮上。
 */
var SUGGESTED_PAGE = 200

/**
 * 单 agent 视图下前端翻页取建议库的上限（t_52125d5d）：10 页 × 200 条。
 * 超过就停手并在界面上写 truncated，不无限翻页把浏览器打满，也不谎称「取全了」。
 */
var SUGGESTED_SCAN_MAX = 2000

/** 「全部 agent」最多并发取几个 profile：profile 再多也不把浏览器打满。 */
var AGENT_MAX = 16

/** `{ok, data:{…}}` 与裸对象两种形状都吃（/agents 是裸的，其余路由都套一层）。 */
function unwrap(payload) {
  if (payload && typeof payload === 'object' && payload.data && typeof payload.data === 'object') {
    return payload.data
  }
  return payload || {}
}

/** agent 清单：GET /agents（裸对象 {agents:[{id,label,…}]}）。 */
function fetchAgents() {
  return get('/agents')
}

/** 项目实体：GET /autogroup/entities（父卡 t_53b48743 的权威读接口，带目录状态与归属计数）。 */
function fetchEntities() {
  return get('/autogroup/entities')
}

/** 归属关系：applied 行 = 已分组；suggested 行 = 待确认建议。 */
function fetchOwnership() {
  return get('/autogroup/suggestions', { params: { status: 'applied', limit: OWNERSHIP_LIMIT } })
}

/**
 * 待确认建议的**一页**（后端回 `data.total` = 全量条数，`items` = 本页）。
 * offset 由「加载更多」递增；不传 = 第一页。
 */
function fetchSuggested(offset) {
  return get('/autogroup/suggestions', {
    params: { status: 'suggested', limit: SUGGESTED_PAGE, offset: offset || 0 }
  })
}

/**
 * 待确认建议**全量**（单 agent 用，t_52125d5d）。
 *
 * 为什么要在前端翻页：`/autogroup/suggestions` **不接受 `agent` 参数**（本卡禁改 `dashboard/*.py`），
 * 它一次只回全库的前 200 行。单 agent 视图下要拿到「属于这个 agent 的建议」的全集，
 * 只能按 offset 翻到后端 `data.total` 为止（封顶 `SUGGESTED_SCAN_MAX`，封顶后明写 truncated，
 * 不装作取全了）。翻完再由调用方按「该 agent 的会话 id」过滤 —— 数字因此是按 agent 真算的。
 */
function fetchSuggestedAllPages() {
  var rows = []
  var seen = {}
  var storeTotal = null
  function page(offset) {
    return fetchSuggested(offset).then(function (payload) {
      if (payload && payload.ok === false) return null
      var body = unwrap(payload)
      var inner = unwrap(body)
      var items = (body && body.items) || (inner && inner.items) || []
      var total = typeof body.total === 'number' ? body.total : typeof inner.total === 'number' ? inner.total : null
      if (total != null) storeTotal = total
      /**
       * 按 session_id 去重（t_c5d47b8e）：offset 翻页期间建议库被改过（重新扫描 / 撤销 / 人工
       * 忽略）时，后一页会与前一页重叠。明细用 session_id 当 React key，重复出现就是重复 key
       * 告警 + 行重复渲染 —— 与「全部 Agent」合并同一个成因，所以在这里也挡住。
       * 保留**先出现**的那条（分页顺序 = 后端排序，越靠前越新），不改变正常路径的顺序。
       */
      items.forEach(function (item) {
        var key = item && item.session_id
        if (key && seen[key]) return
        if (key) seen[key] = true
        rows.push(item)
      })
      if (!items.length) return null
      if (storeTotal != null && rows.length >= storeTotal) return null
      if (rows.length >= SUGGESTED_SCAN_MAX) return null
      return page(offset + items.length)
    })
  }
  return page(0).then(function () {
    return {
      ok: true,
      data: {
        items: rows,
        total: rows.length,
        store_total: storeTotal,
        truncated: storeTotal != null && rows.length < storeTotal
      }
    }
  })
}

/** 规则表（折叠区只读展示）。 */
function fetchRules() {
  return get('/autogroup/rules')
}

/**
 * 会话列表：单 agent 直接打 `/overview/sessions`；「全部」按 /agents 逐个 profile 取再合并。
 *
 * 为什么不传 agent=all：后端明确 400（`unsupported_agent`），跨 profile 没有等价语义。
 * 合并结果里带 `agents` / `failed`，界面据此写明「跨 N 个 agent 合计」与哪些 profile 没读到。
 */
function fetchSessionsOf(agent) {
  if (agent !== AGENT_ALL) {
    /**
     * 单 agent：**翻页取全**（t_bba9216d）。只取最近 100 条会让「不在最近 100 条里」的
     * 未分组会话在列表上消失，于是列表说「未分组 0」、按钮（后端全库口径）说「未分组 4」。
     * 返回形状与改前一致（`{ok, data:{sessions,total}}`），额外带 `truncated`。
     */
    return fetchSessionsAllPages(agent).then(function (res) {
      return { ok: true, data: { sessions: res.rows, total: res.total, truncated: res.truncated } }
    })
  }
  return fetchAgents().then(function (res) {
    if (!res || res.ok === false) return res
    var payload = unwrap(res.data)
    var ids = (payload.agents || [])
      .map(function (item) {
        return item && item.id
      })
      .filter(Boolean)
      .slice(0, AGENT_MAX)
    if (!ids.length) return { ok: false, code: 'no_agents', message: 'agent 清单是空的 —— 后端没返回任何 agent' }
    return Promise.all(
      ids.map(function (id) {
        return fetchSessionsAllPages(id).then(function (one) {
          return { id: id, res: one }
        })
      })
    ).then(function (rows) {
      /**
       * 跨 profile 合并必须**按 session id 去重**（t_c5d47b8e）。
       *
       * 同一个 session_id 可能同时出现在两个库里：实测真 home 里
       * `local-96ba94b6…ab7` 同时存在于 `state.db`（default）与
       * `profiles/code-worker/state.db`。逐个 profile 取再 push 会把同一 id 放进来两次，
       * 而下游列表拿 id 当 React key → duplicate key 告警 + 行重复/漏渲染。
       *
       * 保留哪条：**最近活跃的那条**（`last_active` 最大）。理由有两条——
       *   1. 紧接着就是按 `last_active` 倒序排，保留旧的那条会让排序与显示自相矛盾；
       *   2. 实测跨库同名行里，profile 侧常常是空壳（`message_count=0` / `source=unknown`
       *      / `last_activity_at=NULL`），保留它会顺带被 isDesktopVisible 过滤掉整条会话。
       * 不能用 Map 覆盖成「最先遇到的那条」：那是 profile 遍历顺序决定的，会保留空壳行。
       * `last_active` 相等（含两边都缺）时保留先遇到的那条 —— 不引入新的行为。
       */
      var merged = []
      var seenAt = {}
      var failed = []
      rows.forEach(function (row) {
        if (!row.res || row.res.ok === false) {
          failed.push(row.id)
          return
        }
        sessionsOf(row.res).forEach(function (session) {
          var key = session && session.id
          if (!key) {
            merged.push(session)
            return
          }
          var at = seenAt[key]
          if (at === undefined) {
            seenAt[key] = merged.length
            merged.push(session)
            return
          }
          var prev = merged[at]
          /**
           * 可见优先，再比最近活跃（t_41e89d63 边界缺陷）。
           *
           * 只比 `last_active` 会有一个丢行场景：更新的那份是空壳
           * （`message_count=0` / `source=unknown`），被选中后又被
           * 下游 `isDesktopVisible` 过滤掉 —— 结果是**整条会话从列表消失**，
           * 而 console error 仍是 0，S5 断言抓不到。
           * 所以：可见的那份优先；可见性相同才比 `last_active`（同 `merged.sort` 语义）。
           */
          var prevVisible = isDesktopVisible(prev)
          var nextVisible = isDesktopVisible(session)
          if (prevVisible !== nextVisible) {
            if (nextVisible) merged[at] = session
            return
          }
          var prevActive = Number(prev && prev.last_active) || 0
          var nextActive = Number(session.last_active) || 0
          if (nextActive > prevActive) merged[at] = session
        })
      })
      merged.sort(function (a, b) {
        return (Number(b.last_active) || 0) - (Number(a.last_active) || 0)
      })
      /**
       * `truncated`（t_bba9216d）：任何一个 profile 没取到底，这份合并结果就**不是完整口径**。
       * 不藏起来 —— 界面据此说明「列表可能不全」，而不是让「未分组 0」冒充真相。
       */
      var truncated = rows.some(function (row) {
        return !!(row.res && row.res.data && row.res.data.truncated)
      })
      return { ok: true, data: { sessions: merged, total: merged.length, agents: ids,
                                 failed: failed, truncated: truncated } }
    })
  })
}

/** 会话载荷 → 数组（单 agent 与合并结果两种形状统一在这里）。 */
function sessionsOf(res) {
  var payload = unwrap(res)
  if (payload && Array.isArray(payload.sessions)) return payload.sessions
  if (payload && payload.data && Array.isArray(payload.data.sessions)) return payload.data.sessions
  return []
}

/**
 * 会话列表是否因为翻页上限没取到底（t_bba9216d）。
 *
 * 「全部 agent」合并结果与单 agent 结果都带这个标记；`true` 时界面必须明说
 * 「列表不是全量」，否则「未分组 N」会被当成后端候选数来读。
 */
function sessionsTruncated(res) {
  var payload = unwrap(res)
  if (payload && payload.truncated === true) return true
  if (payload && payload.data && payload.data.truncated === true) return true
  return false
}

/** 合并结果里的附加信息（「全部」才有；单 agent 返回 null）。 */
function sessionsMeta(res) {
  var payload = unwrap(res)
  if (payload && Array.isArray(payload.agents)) return payload
  return null
}

/**
 * 桌面端可见口径（前端二次过滤），必须与后端 `autogroup_ai._is_listable_child` /
 * `desktop_visible_pool` **逐条对齐**（t_bba9216d 验收 C）：
 *
 *   · `source` 不属于排除名单；
 *   · `message_count >= 1`（缺字段时不冒充 0，交给后端口径）；
 *   · `hidden` 为真 → 不显示（宿主 `include_hidden=False`）；
 *   · `archived` 为真且未 `pinned` → 不显示（宿主 `include_pinned=True`：置顶的归档仍可见）；
 *   · 压缩续接 / subagent 子行 → 不显示（宿主 `_LISTABLE_CHILD_SQL` + `_delegate_from IS NULL`）。
 *     判断依据是 `_delegate_from` 标记、`archived`/`hidden` 列与后端一并返回的可见性字段，
 *     不复刻宿主的递归 CTE（列表里拿不到父行 `end_reason`），拿不到就不猜。
 */
function isDesktopVisible(row) {
  var source = String((row && row.source) || '').toLowerCase()
  if (EXCLUDED_SOURCES.indexOf(source) >= 0) return false
  if (row && Number(row.hidden) === 1) return false
  if (row && Number(row.archived) === 1 && Number(row.pinned) !== 1) return false
  var count = row && row.message_count
  if (count === null || count === undefined) return true
  return Number(count) >= 1
}

/** 会话的显示标题：display_name 优先，其次 title，最后给占位文案。 */
function sessionTitle(row) {
  return (row && (row.display_name || row.title)) || '（这条会话没有标题）'
}

/** applied 载荷 → {session_id: item}（归属现状只信后端回的行）。 */
function ownershipMap(payload) {
  var data = unwrap(payload)
  var items = (data && data.items) || []
  var out = {}
  items.forEach(function (item) {
    if (item && item.session_id) out[item.session_id] = item
  })
  return out
}

/** 项目实体载荷 → 数组。 */
function entitiesOf(payload) {
  var data = unwrap(payload)
  return (data && data.projects) || []
}

/** agent 清单载荷 → [{id,label,is_default}]。 */
function agentsOf(payload) {
  var data = unwrap(payload)
  return (data && data.agents) || []
}

/** 项目实体 → 面板里用得到的极简形状（slug/name/path/归属计数/目录状态）。 */
function projectBrief(item) {
  var assign = (item && item.assignments) || {}
  return {
    slug: (item && item.slug) || '',
    name: (item && item.name) || (item && item.slug) || '',
    path: (item && item.path) || '',
    status: (item && item.status) || 'active',
    dir: (item && item.dir) || {},
    applied: Number(assign.applied) || 0,
    total: Number(assign.total) || 0,
    suggested: Number(assign.suggested) || 0
  }
}

/** 目录状态一句话（后端 `dir` 字段；读不到就说读不到，不编）。 */
function dirText(dir) {
  var state = dir || {}
  if (state.exists === null || state.exists === undefined) return '目录读不了'
  if (!state.exists) return '目录不存在'
  return state.empty ? '目录存在（空）' : '目录存在（非空）'
}

/**
 * ① 顶部状态条：项目数 / 已分组 / 未分组 / 待确认 / 批次。
 *
 * 一条横排、每格可点（点了就切列表范围），口径写在 title 里。
 * 「待确认」这里**只写数字与短标签**——「待确认建议」这个区块名全页只允许出现在折叠区抽屉头上，
 * 否则同名区块又会出现两次（本卡验收 A）。
 */
function StatusStrip(props) {
  var metrics = props.metrics || []
  return jsxs('div', {
    className: 'hc-agg2__strip',
    'data-agg2-strip': '1',
    children: [
      jsx('div', {
        key: 'metrics',
        className: 'hc-agg2__strip-metrics',
        children: metrics.map(function (metric) {
          var active = !!metric.active
          return jsxs(
            'button',
            {
              type: 'button',
              key: metric.id,
              className:
                'hc-agg2__metric' +
                (active ? ' hc-agg2__metric--on' : '') +
                (metric.tone === 'warn' ? ' hc-agg2__metric--warn' : ''),
              'data-agg2-metric': metric.id,
              'data-agg2-metric-value':
                metric.value === null || metric.value === undefined ? '' : String(metric.value),
              title: metric.title || '',
              onClick: metric.onClick || function () {},
              children: [
                jsx('span', {
                  key: 'num',
                  className: 'hc-agg2__metric-num',
                  children: metric.value === null || metric.value === undefined ? '—' : num(metric.value)
                }),
                jsx('span', { key: 'label', className: 'hc-agg2__metric-label', children: metric.label })
              ]
            },
            metric.id
          )
        })
      }),
      jsx('span', {
        key: 'scope',
        className: 'hc-chip hc-agg2__scope',
        'data-agg2-scope': props.scope || '',
        title: props.scopeTitle || '',
        children: props.scope || ''
      })
    ]
  })
}

/** 建议列表的一行：勾选 / 会话标题 / agent / 内联切换项目 / 判定依据 / 生成时间。 */
function SuggestRow(props) {
  var item = props.item
  var projects = props.projects || []
  var note = props.switchNote
  var known = false
  projects.forEach(function (project) {
    if (project.slug === item.project_slug) known = true
  })
  var options = [jsx('option', { value: '', disabled: true, children: '切到…' }, '__hint')]
  if (item.project_slug && !known) {
    options.push(jsx('option', { value: item.project_slug, children: (item.project_name || item.project_slug) + '（不在项目表）' }, '__cur'))
  }
  projects.forEach(function (project) {
    options.push(jsx('option', { value: project.slug, children: project.name || project.slug }, project.slug))
  })
  return jsxs('div', {
    className: 'hc-agg__row',
    'data-session-id': item.session_id || '',
    children: [
      jsxs('div', {
        className: 'hc-agg__cell hc-agg__cell--title',
        children: [
          jsx('input', {
            type: 'checkbox',
            'data-agg-select': item.session_id || '',
            checked: !!props.selected,
            disabled: !!props.busy,
            onChange: function () {
              props.onToggle(item.session_id)
            }
          }),
          jsx('span', {
            className: 'hc-agg__title',
            title: item.title || item.session_id || '',
            children: item.title || '（这条会话没有标题）'
          }),
          item.message_count === null || item.message_count === undefined
            ? null
            : jsx('span', { className: 'hc-agg__msg', children: num(item.message_count) + ' 条消息' })
        ]
      }),
      jsx('div', { className: 'hc-agg__cell hc-agg__cell--agent', title: item.session_id || '', children: item.profile || '—' }),
      jsxs('div', {
        className: 'hc-agg__cell hc-agg__cell--project',
        children: [
          jsx('span', {
            className: 'hc-agg__project',
            title: (item.project_path || item.project_slug || '') + (item.project_exists ? '' : '（项目库里没有这个 slug）'),
            children: item.project_name || item.project_slug || '—'
          }),
          item.project_exists
            ? null
            : jsx('span', { className: 'hc-agg__warn', children: '项目缺失' }),
          jsx('select', {
            className: 'hc-select',
            'data-agg-switch': item.session_id || '',
            value: item.project_slug || '',
            disabled: !!props.busy,
            title: '内联切换归属（写台账，可撤销）',
            onChange: function (event) {
              props.onSwitch(item.session_id, event.target.value)
            },
            children: options
          }),
          note
            ? jsx('span', {
                className: note.ok ? 'hc-agg__msg' : 'hc-agg__warn',
                'data-agg-switch-note': item.session_id || '',
                children: note.text
              })
            : null
        ]
      }),
      jsx('div', {
        className: 'hc-agg__cell hc-agg__cell--basis',
        title: (item.basis && item.basis.reason) || '',
        children: basisText(item)
      }),
      jsx('div', {
        className: 'hc-agg__cell hc-agg__cell--time',
        title: item.updated_at ? absDate(item.updated_at) : '',
        children: relTime(item.updated_at)
      }),
      jsx('div', {
        className: 'hc-agg__cell hc-agg__cell--ops',
        children: jsx(AcceptRowButton, {
          item: item,
          accepted: props.accepted || null,
          onAccepted: props.onAccepted
        })
      })
    ]
  })
}

/**
 * ② 项目条（首屏左侧，常驻）：项目清单 + 每行四个动作（重命名 / 改目录 / 解散 / 删除）。
 *
 * 写操作全部走父卡 t_53b48743 的实体契约：
 *   POST   /autogroup/project/create   {slug,name,path,confirm:true}
 *   PATCH  /autogroup/project/name     {slug,name,confirm:true}          （目录不动）
 *   POST   /autogroup/project/slug     {slug,new_slug,confirm:true,confirm_slug:true}（高风险）
 *   POST   /autogroup/project/path     {slug,new_path,mode,confirm:true}
 *   POST   /autogroup/project/dissolve {slug,to:'ungrouped'|'move',target_slug,confirm:true}
 *   POST   /autogroup/project/delete   {slug,confirm:true}
 *
 * 四条规矩：
 *   1. **先预览再确认**：点动作只展开预览（要改什么、目录怎么动），点确认才带 confirm=true 发请求；
 *   2. **回读才算数**：写完立刻 GET /autogroup/entities 逐字段核对（名字/目录/slug/实体是否还在）；
 *   3. 解散必须显式选归属去向，没有「静默丢归属」这条路径；
 *   4. 每个项目行都是拖拽落点：会话行拖进来 = 发起一次单条改分组（仍然要确认）。
 */
function ProjectRail(props) {
  var items = props.items || []
  var root = props.root || ''
  var activeSlug = props.activeSlug || ''
  var formState = useState({ open: false, name: '', slug: '', path: '', preview: null })
  var editorState = useState({ slug: '', mode: '', value: '', pathMode: 'migrate' })
  var dissolveState = useState({ slug: '', to: 'ungrouped', target: '' })
  var confirmState = useState('')
  var busyState = useState('')
  var resultState = useState(null)
  var form = formState[0]
  var editor = editorState[0]
  var dissolve = dissolveState[0]
  var busy = busyState[0]

  function changed() {
    if (typeof props.onChanged === 'function') props.onChanged()
  }

  function fail(what, res) {
    resultState[1]({ ok: false, text: what + '：' + ((res && res.message) || '后端没有给原因') })
  }

  function readEntity(slug) {
    return get('/autogroup/entities').then(function (res) {
      if (!res || res.ok === false) return null
      var list = entitiesOf(res.data)
      for (var i = 0; i < list.length; i += 1) {
        if (list[i] && list[i].slug === slug) return list[i]
      }
      return null
    })
  }

  /** 新建：先出预览（slug / 显示名 / 目录落点），确认后才写。 */
  function createPreview() {
    var name = form.name.trim()
    var slug = form.slug.trim()
    if (!name) {
      resultState[1]({ ok: false, text: '先填显示名（1-60 个字符）' })
      return
    }
    if (!slug) slug = name.toLowerCase().replace(/\s+/g, '-')
    var dir = form.path.trim() || (root ? root.replace(/[\\/]+$/, '') + '/' + slug : '')
    formState[1]({ open: true, name: name, slug: slug, path: form.path, preview: { slug: slug, name: name, dir: dir } })
    resultState[1]({ ok: true, text: '预览：将新建项目实体 ' + slug + '（显示名「' + name + '」），目录 ' + dir + '。点「确认新建」才写。' })
  }

  function createConfirm() {
    var plan = form.preview
    if (!plan) return
    busyState[1]('create')
    var body = { slug: plan.slug, name: plan.name, confirm: true, created_by: 'manual' }
    if (form.path.trim()) body.path = form.path.trim()
    send('/autogroup/project/create', body).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        fail('新建失败（后端拒绝，未写任何东西）', res)
        return
      }
      var data = unwrap(res.data)
      var slug = (data.project && data.project.slug) || plan.slug
      readEntity(slug).then(function (got) {
        changed()
        if (!got) {
          resultState[1]({ ok: false, text: '后端说成功，但回读实体里没有 ' + slug + '：不算成功' })
          return
        }
        formState[1]({ open: false, name: '', slug: '', path: '', preview: null })
        resultState[1]({
          ok: true,
          text: '已新建 ' + slug + '：目录' + (data.dir_created ? '已建' : '复用已有') + ' · 回读实体一致（' + dirText(got.dir) + '）'
        })
      })
    })
  }

  /** 改显示名：只写 name，目录与归属都不动（回读里逐项核对 path 没变）。 */
  function saveName(item) {
    var value = editor.value.trim()
    if (!value) {
      resultState[1]({ ok: false, text: '新名字不能为空' })
      return
    }
    busyState[1](item.slug)
    send('/autogroup/project/name', { slug: item.slug, name: value, confirm: true }, { method: 'PATCH' }).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        fail('改名失败（未写任何东西）', res)
        return
      }
      readEntity(item.slug).then(function (got) {
        changed()
        if (!got) {
          resultState[1]({ ok: false, text: '后端说成功，但回读实体里已经没有 ' + item.slug })
          return
        }
        var sameDir = String(got.path || '') === String(item.path || '')
        editorState[1]({ slug: '', mode: '', value: '', pathMode: 'migrate' })
        resultState[1]({
          ok: sameDir && String(got.name || '') === value,
          text:
            '改名回读：name=' + (got.name || '—') +
            ' · 目录' + (sameDir ? '未变（' + (got.path || '—') + '）' : '变了！' + (got.path || '—'))
        })
      })
    })
  }

  /** 改 slug（高风险）：迁移目录 + 迁移归属；必须两次确认（展开 + 确认按钮）。 */
  function saveSlug(item) {
    var next = editor.value.trim()
    if (!next) {
      resultState[1]({ ok: false, text: '新 slug 不能为空' })
      return
    }
    busyState[1](item.slug)
    send('/autogroup/project/slug', {
      slug: item.slug,
      new_slug: next,
      confirm: true,
      confirm_slug: true,
      move_dir: true
    }).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        fail('改 slug 被拒（未写任何东西）', res)
        return
      }
      readEntity(next).then(function (got) {
        changed()
        editorState[1]({ slug: '', mode: '', value: '', pathMode: 'migrate' })
        if (!got) {
          resultState[1]({ ok: false, text: '后端说成功，但回读实体里没有 ' + next })
          return
        }
        resultState[1]({ ok: true, text: '已改 slug：' + item.slug + ' → ' + next + ' · 回读目录 ' + (got.path || '—') })
      })
    })
  }

  /** 改项目文件夹：mode=migrate 搬内容 / mode=rebind 只改指向。 */
  function savePath(item) {
    var next = editor.value.trim()
    if (!next) {
      resultState[1]({ ok: false, text: '新目录不能为空' })
      return
    }
    busyState[1](item.slug)
    send('/autogroup/project/path', {
      slug: item.slug,
      new_path: next,
      mode: editor.pathMode || 'migrate',
      confirm: true
    }).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        fail('改目录被拒（未写任何东西）', res)
        return
      }
      readEntity(item.slug).then(function (got) {
        changed()
        editorState[1]({ slug: '', mode: '', value: '', pathMode: 'migrate' })
        if (!got) {
          resultState[1]({ ok: false, text: '后端说成功，但回读实体里没有 ' + item.slug })
          return
        }
        var ok = String(got.path || '') === next
        resultState[1]({
          ok: ok,
          text: '改目录回读：' + (got.path || '—') + (ok ? '（与目标一致）' : '（与目标不一致！）')
        })
      })
    })
  }

  /** 解散：必须选去向（退回未分组 / 迁到另一个项目）；写后核对归属行没有残留。 */
  function dissolveConfirm(item) {
    if (!dissolve.to) {
      resultState[1]({ ok: false, text: '先选归属会话的去向：退回未分组，或迁移到另一个项目' })
      return
    }
    if (dissolve.to === 'move' && !dissolve.target) {
      resultState[1]({ ok: false, text: '选了「迁移到另一个项目」就必须选目标项目' })
      return
    }
    busyState[1](item.slug)
    var body = { slug: item.slug, to: dissolve.to, confirm: true, dir_action: 'keep' }
    if (dissolve.to === 'move') body.target_slug = dissolve.target
    send('/autogroup/project/dissolve', body).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        fail('解散被拒（未写任何东西）', res)
        return
      }
      var data = unwrap(res.data)
      var touched = (data.moved || []).concat(data.ungrouped || [])
      get('/autogroup/suggestions', { params: { status: 'applied', limit: OWNERSHIP_LIMIT } }).then(function (again) {
        changed()
        dissolveState[1]({ slug: '', to: 'ungrouped', target: '' })
        var map = ownershipMap(again && again.data)
        var orphans = touched.filter(function (sid) {
          var row = map[sid]
          return !!row && row.project_slug === item.slug
        })
        resultState[1]({
          ok: orphans.length === 0,
          text:
            '解散 ' + item.slug + '：迁移 ' + num((data.moved || []).length) + ' 条 · 退回未分组 ' +
            num((data.ungrouped || []).length) + ' 条 · 回读残留归属 ' + num(orphans.length) + ' 条' +
            (orphans.length ? '（' + orphans.slice(0, 5).join('、') + '）' : '（无孤儿）')
        })
      })
    })
  }

  function deleteConfirm(item) {
    busyState[1](item.slug)
    confirmState[1]('')
    send('/autogroup/project/delete', { slug: item.slug, confirm: true, dir_action: 'keep' }).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        fail('删除被拒（未写任何东西）', res)
        return
      }
      readEntity(item.slug).then(function (got) {
        changed()
        resultState[1]({
          ok: !got,
          text: got ? '后端说成功，但回读实体里还有 ' + item.slug + '：不算成功' : '已删除 ' + item.slug + '（回读实体里已没有它）'
        })
      })
    })
  }

  function renderEditor(item) {
    var mode = editor.mode
    var input = jsx('input', {
      key: 'input',
      className: 'hc-set__input hc-agg2__inline-input',
      'data-agg2-project-input': item.slug,
      value: editor.value,
      disabled: busy === item.slug,
      placeholder: mode === 'path' ? '新目录绝对路径' : mode === 'slug' ? '新 slug' : '新显示名',
      onChange: function (event) {
        editorState[1]({ slug: item.slug, mode: mode, value: event.target.value, pathMode: editor.pathMode })
      }
    })
    var extra = null
    if (mode === 'path') {
      extra = jsx('select', {
        key: 'mode',
        className: 'hc-select hc-agg2__inline-select',
        'data-agg2-project-path-mode': item.slug,
        value: editor.pathMode || 'migrate',
        onChange: function (event) {
          editorState[1]({ slug: item.slug, mode: mode, value: editor.value, pathMode: event.target.value })
        },
        children: [
          jsx('option', { key: 'migrate', value: 'migrate', children: 'migrate：把内容搬到新目录' }, 'migrate'),
          jsx('option', { key: 'rebind', value: 'rebind', children: 'rebind：只改指向，不搬' }, 'rebind')
        ]
      })
    }
    if (mode === 'slug') {
      extra = jsx('span', {
        key: 'warn',
        className: 'hc-agg__warn',
        'data-agg2-project-slug-warn': item.slug,
        children: '高风险：会迁移目录与归属行。确认按钮就是第二次确认。'
      })
    }
    return jsxs('div', {
      key: 'editor',
      className: 'hc-agg2__editor',
      'data-agg2-project-editor': item.slug + ':' + mode,
      children: [
        input,
        extra,
        jsx('button', {
          key: 'save',
          type: 'button',
          className: 'hc-btn hc-btn--accent hc-btn--tiny',
          'data-agg2-project-save': item.slug + ':' + mode,
          disabled: busy === item.slug,
          onClick: function () {
            if (mode === 'name') saveName(item)
            else if (mode === 'slug') saveSlug(item)
            else savePath(item)
          },
          children:
            busy === item.slug
              ? '写入中…'
              : mode === 'slug'
                ? '确认改 slug（迁移目录）'
                : mode === 'path'
                  ? '确认改目录'
                  : '保存显示名'
        }),
        jsx('button', {
          key: 'cancel',
          type: 'button',
          className: 'hc-btn hc-btn--tiny',
          'data-agg2-project-cancel': item.slug,
          disabled: busy === item.slug,
          onClick: function () {
            editorState[1]({ slug: '', mode: '', value: '', pathMode: 'migrate' })
          },
          children: '取消'
        })
      ]
    })
  }

  function renderRow(item) {
    var editing = editor.slug === item.slug
    var dissolving = dissolve.slug === item.slug
    var confirming = confirmState[0] === item.slug
    var rowBusy = busy === item.slug
    var isActive = activeSlug === item.slug
    var ops = editing
      ? [renderEditor(item)]
      : [
          jsx('button', {
            key: 'name',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg-project-rename': item.slug,
            'data-agg2-project-name': item.slug,
            disabled: rowBusy,
            title: '改显示名（目录不动）：PATCH /autogroup/project/name',
            onClick: function () {
              editorState[1]({ slug: item.slug, mode: 'name', value: item.name || item.slug, pathMode: 'migrate' })
            },
            children: '重命名'
          }, 'name'),
          jsx('button', {
            key: 'slug',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-project-slug': item.slug,
            disabled: rowBusy,
            title: '改 slug（高风险，会迁移目录与归属）',
            onClick: function () {
              editorState[1]({ slug: item.slug, mode: 'slug', value: item.slug, pathMode: 'migrate' })
            },
            children: '改 slug'
          }, 'slug'),
          jsx('button', {
            key: 'path',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-project-path': item.slug,
            disabled: rowBusy,
            title: '改项目文件夹',
            onClick: function () {
              editorState[1]({ slug: item.slug, mode: 'path', value: item.path || '', pathMode: 'migrate' })
            },
            children: '改目录'
          }, 'path'),
          jsx('button', {
            key: 'dissolve',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-project-dissolve': item.slug,
            disabled: rowBusy,
            title: '解散项目（必须先选归属会话去哪）',
            onClick: function () {
              dissolveState[1]({ slug: dissolving ? '' : item.slug, to: 'ungrouped', target: '' })
            },
            children: dissolving ? '收起解散' : '解散'
          }, 'dissolve'),
          jsx('button', {
            key: 'delete',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg-project-delete': item.slug,
            'data-agg2-project-delete': item.slug,
            disabled: rowBusy,
            title: '删项目实体（有归属会被后端拒绝）',
            onClick: function () {
              if (confirming) deleteConfirm(item)
              else confirmState[1](item.slug)
            },
            children: confirming ? '确认删除（不可逆）' : '删除'
          }, 'delete')
        ]

    return jsxs('div', {
      key: item.slug,
      className: 'hc-agg2__proj' + (isActive ? ' hc-agg2__proj--on' : ''),
      'data-agg-project': item.slug,
      'data-agg2-project': item.slug,
      'data-agg2-project-applied': String(item.applied),
      onDragOver: function (event) {
        event.preventDefault()
      },
      onDrop: function (event) {
        event.preventDefault()
        var sessionId = ''
        try {
          sessionId = event.dataTransfer.getData('text/plain') || ''
        } catch (e) {
          sessionId = ''
        }
        if (sessionId && typeof props.onDropSession === 'function') props.onDropSession(sessionId, item.slug)
      },
      children: [
        jsxs('div', {
          key: 'head',
          className: 'hc-agg2__proj-head',
          children: [
            jsx('button', {
              key: 'pick',
              type: 'button',
              className: 'hc-agg2__proj-name',
              'data-agg2-project-pick': item.slug,
              title: '只看这个项目的会话',
              onClick: function () {
                if (typeof props.onPick === 'function') props.onPick(isActive ? '' : item.slug)
              },
              children: item.name || item.slug
            }),
            jsx('span', {
              key: 'count',
              className: 'hc-agg2__proj-count',
              'data-agg2-project-count': item.slug,
              title: '归属表里这个项目 applied 的行数（后端 assignments 统计）',
              children: num(item.applied) + ' 条'
            })
          ]
        }),
        jsx('div', {
          key: 'meta',
          className: 'hc-agg2__proj-meta',
          title: item.path || '',
          children: item.slug + ' · ' + dirText(item.dir) + (item.status && item.status !== 'active' ? ' · ' + item.status : '')
        }),
        jsx('div', { key: 'ops', className: 'hc-agg2__proj-ops', children: ops }),
        dissolving
          ? jsxs('div', {
              key: 'dissolve',
              className: 'hc-agg2__editor',
              'data-agg2-dissolve': item.slug,
              children: [
                jsx('span', {
                  key: 'warn',
                  className: 'hc-agg__warn',
                  children: '解散前必须选归属会话去哪（当前 ' + num(item.applied) + ' 条）：'
                }),
                jsxs('label', {
                  key: 'ungrouped',
                  className: 'hc-agg2__radio',
                  children: [
                    jsx('input', {
                      type: 'radio',
                      name: 'dissolve-to-' + item.slug,
                      checked: dissolve.to === 'ungrouped',
                      onChange: function () {
                        dissolveState[1]({ slug: item.slug, to: 'ungrouped', target: '' })
                      }
                    }),
                    jsx('span', { children: '退回未分组' })
                  ]
                }),
                jsxs('label', {
                  key: 'move',
                  className: 'hc-agg2__radio',
                  children: [
                    jsx('input', {
                      type: 'radio',
                      name: 'dissolve-to-' + item.slug,
                      checked: dissolve.to === 'move',
                      onChange: function () {
                        dissolveState[1]({ slug: item.slug, to: 'move', target: '' })
                      }
                    }),
                    jsx('span', { children: '迁移到' })
                  ]
                }),
                jsx('select', {
                  key: 'target',
                  className: 'hc-select hc-agg2__inline-select',
                  'data-agg2-dissolve-target': item.slug,
                  value: dissolve.target || '',
                  disabled: dissolve.to !== 'move',
                  onChange: function (event) {
                    dissolveState[1]({ slug: item.slug, to: 'move', target: event.target.value })
                  },
                  children: [jsx('option', { key: '', value: '', children: '选目标项目…' }, 'hint')].concat(
                    items
                      .filter(function (other) {
                        return other.slug !== item.slug
                      })
                      .map(function (other) {
                        return jsx('option', { key: other.slug, value: other.slug, children: other.name || other.slug }, other.slug)
                      })
                  )
                }),
                jsx('button', {
                  key: 'go',
                  type: 'button',
                  className: 'hc-btn hc-btn--accent hc-btn--tiny',
                  'data-agg2-dissolve-confirm': item.slug,
                  disabled: rowBusy,
                  onClick: function () {
                    dissolveConfirm(item)
                  },
                  children: rowBusy ? '解散中…' : '确认解散'
                }),
                jsx('button', {
                  key: 'cancel',
                  type: 'button',
                  className: 'hc-btn hc-btn--tiny',
                  'data-agg2-dissolve-cancel': item.slug,
                  onClick: function () {
                    dissolveState[1]({ slug: '', to: 'ungrouped', target: '' })
                  },
                  children: '取消'
                })
              ]
            })
          : null
      ]
    })
  }

  return jsxs('section', {
    className: 'hc-agg2__rail',
    'data-agg-projects': String(items.length),
    'data-agg2-projects': String(items.length),
    children: [
      jsxs('div', {
        key: 'head',
        className: 'hc-agg2__pane-head',
        children: [
          jsx('span', { key: 'title', className: 'hc-agg2__pane-title', children: '项目' }),
          jsx('span', { key: 'count', className: 'hc-chip', 'data-agg2-projects-count': String(items.length), children: num(items.length) + ' 个' }),
          jsx('button', {
            key: 'new',
            type: 'button',
            className: 'hc-btn hc-btn--accent hc-btn--tiny',
            'data-agg2-project-create': '1',
            onClick: function () {
              formState[1]({ open: !form.open, name: form.name, slug: form.slug, path: form.path, preview: null })
            },
            children: form.open ? '收起新建' : '＋ 新建项目'
          })
        ]
      }),
      form.open
        ? jsxs('div', {
            key: 'form',
            className: 'hc-agg2__editor',
            'data-agg2-project-form': '1',
            children: [
              jsx('input', {
                key: 'name',
                className: 'hc-set__input hc-agg2__inline-input',
                'data-agg2-create-name': '1',
                placeholder: '显示名（必填）',
                value: form.name,
                onChange: function (event) {
                  formState[1]({ open: true, name: event.target.value, slug: form.slug, path: form.path, preview: null })
                }
              }),
              jsx('input', {
                key: 'slug',
                className: 'hc-set__input hc-agg2__inline-input',
                'data-agg2-create-slug': '1',
                placeholder: 'slug（留空按显示名生成）',
                value: form.slug,
                onChange: function (event) {
                  formState[1]({ open: true, name: form.name, slug: event.target.value, path: form.path, preview: null })
                }
              }),
              jsx('input', {
                key: 'path',
                className: 'hc-set__input hc-agg2__inline-input',
                'data-agg2-create-path': '1',
                placeholder: '目录（留空 = 项目根/slug）',
                value: form.path,
                onChange: function (event) {
                  formState[1]({ open: true, name: form.name, slug: form.slug, path: event.target.value, preview: null })
                }
              }),
              form.preview
                ? jsx('div', {
                    key: 'preview',
                    className: 'hc-agg__hint',
                    'data-agg2-create-preview': form.preview.slug,
                    children: '预览（还没写）：实体 ' + form.preview.slug + ' · 显示名「' + form.preview.name + '」· 目录 ' + form.preview.dir
                  })
                : null,
              jsxs('div', {
                key: 'ops',
                className: 'hc-agg2__editor-ops',
                children: [
                  jsx('button', {
                    key: 'preview',
                    type: 'button',
                    className: 'hc-btn hc-btn--tiny',
                    'data-agg2-create-preview-btn': '1',
                    disabled: busy === 'create',
                    onClick: createPreview,
                    children: '预览'
                  }),
                  jsx('button', {
                    key: 'confirm',
                    type: 'button',
                    className: 'hc-btn hc-btn--accent hc-btn--tiny',
                    'data-agg2-create-confirm': '1',
                    disabled: busy === 'create' || !form.preview,
                    title: form.preview ? 'confirm=true 才真的建实体与目录' : '先点「预览」',
                    onClick: createConfirm,
                    children: busy === 'create' ? '创建中…' : '确认新建'
                  })
                ]
              })
            ]
          })
        : null,
      items.length
        ? jsx('div', { key: 'list', className: 'hc-agg2__proj-list', children: items.map(renderRow) })
        : jsx(Empty, {
            key: 'empty',
            title: '还没有项目',
            hint: 'projects.db 里没有未归档项目 —— 点上面的「＋ 新建项目」建一个（会同时建目录并记进实体库）。'
          }),
      resultState[0]
        ? jsx('div', {
            key: 'result',
            className: resultState[0].ok ? 'hc-adv__formsaved' : 'hc-adv__formerr',
            'data-agg2-project-result': resultState[0].ok ? 'ok' : 'fail',
            children: resultState[0].text
          })
        : null
    ]
  })
}

/** 操作结果一行：成功走 accent 文案，失败原样给后端 message（不自己编原因）。 */
function ResultLine(props) {
  if (!props.result) return null
  var attrs = { 'data-agg-note': props.name || 'note', 'data-agg-note-ok': props.result.ok ? 'true' : 'false' }
  if (!props.result.ok) {
    return jsx('div', Object.assign({ className: 'hc-adv__formerr', children: props.result.text }, attrs))
  }
  return jsx('div', Object.assign({ className: 'hc-adv__formsaved', children: props.result.text }, attrs))
}

/** 自动模式块：开关（可随时暂停）+ 后端记的 last_tick；关着时 tick 零副作用。 */
function AutoPanel(props) {
  var query = useQuery('autogroup.auto', fetchAuto, { staleMs: STALE_MS })
  var busyState = useState('')
  var feedbackState = useState(null)
  var data = (query.data && query.data.data) || {}
  var notes = (query.data && query.data.notes) || []
  var enabled = data.enabled === true
  var daemon = data.daemon || {}
  var busy = busyState[0]
  /** 模式门（t_f2338eb2）：全量重建模式下 tick 只补未分组会话，与 B 口径矛盾 ⇒ 停用并说明。 */
  var blocked = props.modeBlocked === true

  function apply(next, what) {
    busyState[1](what)
    send('/autogroup/auto', { enabled: next, interval_minutes: Number(data.interval_minutes) || 30 }).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        feedbackState[1]({ ok: false, text: what + '失败：' + ((res && res.message) || '后端没有给原因') })
        return
      }
      var payload = res.data || {}
      var after = payload.data || {}
      var extra = (payload.notes || []).join(' · ')
      feedbackState[1]({
        ok: true,
        text: '已' + (after.enabled ? '开启' : '暂停') + '自动模式（回读 enabled=' + String(after.enabled) + '）' + (extra ? ' · ' + extra : '')
      })
      query.reload()
      if (typeof props.onChanged === 'function') props.onChanged()
    })
  }

  function tick() {
    busyState[1]('tick')
    send('/autogroup/auto/tick', {}).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        feedbackState[1]({ ok: false, text: 'tick 失败：' + ((res && res.message) || '后端没有给原因') })
        return
      }
      var payload = res.data || {}
      var after = payload.data || {}
      feedbackState[1]({
        ok: true,
        text: after.skipped
          ? '自动模式关着：本次 tick 直接跳过，没读也没写归属表'
          : '本次 tick 扫了 ' + num(after.scanned) + ' 个会话 · 直接归入 ' + num(after.applied) +
            ' 条（applied）· 留建议 ' + num(after.suggested) + ' 条 · 跳过 ' + num(after.skipped_existing) +
            ' 条（已 applied / 无变化）' + (after.op_id ? ' · op ' + after.op_id : '')
      })
      query.reload()
    })
  }

  if (query.status === 'loading') return jsx(Skeleton, { lines: 2, label: '正在读取自动模式状态' })
  if (query.status === 'error') {
    return jsx(ErrorState, { title: '自动模式状态没读到', message: query.error, onRetry: query.reload, retryLabel: '重试' })
  }

  return jsxs('div', {
    className: 'hc-agg__block',
    'data-agg-auto': enabled ? 'on' : 'off',
    children: [
      jsxs('div', {
        className: 'hc-agg__block-head',
        children: [
          jsx('span', { className: 'hc-agg__block-title', children: '自动模式' }),
          jsx('span', {
            className: 'hc-chip ' + (enabled ? 'hc-chip--on' : 'hc-chip--off'),
            'data-agg-auto-state': enabled ? 'on' : 'off',
            children: enabled ? '运行中（enabled=true）' : '已暂停（enabled=false）'
          }),
          jsx('span', {
            className: 'hc-chip',
            title: data.state_file || '',
            children: '间隔 ' + num(data.interval_minutes) + ' 分钟 · 来源 ' + (data.source || '—')
          }),
          jsx('span', {
            className: 'hc-chip ' + (daemon.running ? 'hc-chip--on' : 'hc-chip--off'),
            'data-agg-auto-daemon': daemon.running ? 'on' : 'off',
            title: (daemon.state_file || '还没有守护进程心跳') +
              '（周期触发由独立守护进程负责：auto_mode.json 一开，它自己按间隔跑 tick）',
            children: daemon.running
              ? '守护进程在跑 · pid ' + String(daemon.pid || '—') + ' · 已跑 ' + num(daemon.ticks || 0) + ' 轮'
              : '守护进程没在跑'
          })
        ]
      }),
      jsxs('div', {
        key: 'gate',
        className: 'hc-agg2__gate',
        'data-agg2-mode-gate': 'auto',
        'data-agg2-mode-gate-state': blocked ? 'blocked' : 'open',
        children: [
          jsx('span', { key: 't', className: 'hc-agg2__gate-title', children: '自动模式 tick' }),
          jsx('span', {
            key: 'h',
            className: 'hc-agg2__gate-text',
            children: blocked
              ? '当前模式=全量重建：tick 只按规则补未分组会话，不会重建体系，与 B 口径矛盾，故停用。全量重建请用上方「先预览」。'
              : '当前模式=只归类未分组：tick 只补未分组会话，与 A 模式口径一致，可用。'
          })
        ]
      }),
      jsxs('div', {
        className: 'hc-agg__toolbar',
        children: [
          jsx('button', {
            type: 'button',
            className: 'hc-btn ' + (enabled ? '' : 'hc-btn--accent'),
            'data-agg-auto-toggle': enabled ? 'pause' : 'start',
            disabled: !!busy,
            title: enabled ? '暂停：之后 tick 一律跳过，守护进程什么也不写' : '开启：拉起守护进程，按间隔自己跑，达阈值直接写 applied',
            onClick: function () {
              apply(!enabled, enabled ? '暂停' : '开启')
            },
            children: busy === '暂停' || busy === '开启' ? '写入中…' : enabled ? '暂停自动模式' : '开启自动模式'
          }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn',
            'data-agg-auto-tick': '1',
            'data-agg-auto-tick-blocked': blocked ? '1' : '0',
            disabled: !!busy || blocked,
            title: blocked
              ? '全量重建模式下停用：tick 只按规则补未分组会话；请用上方「先预览」重建体系'
              : '手动跑一次 tick：关着就跳过（零副作用），开着按规则归组（达阈值直接 applied）',
            onClick: tick,
            children: blocked ? '全量重建模式下停用' : busy === 'tick' ? '跑 tick…' : '立即跑一次 tick'
          }),
          jsx('span', {
            className: 'hc-agg__hint',
            'data-agg-auto-last-tick': data.last_tick ? String(data.last_tick) : '',
            title: data.last_tick ? absDate(data.last_tick) : '',
            children: data.last_tick
              ? '后端记的上次 tick：' + relTime(data.last_tick) + '（' + absDate(data.last_tick) + '）'
              : '后端还没有 tick 记录（last_tick 为空）'
          })
        ]
      }),
      jsx(ResultLine, { name: 'auto', result: feedbackState[0] }),
      notes.length
        ? jsx('div', {
            className: 'hc-agg__notes',
            children: notes.map(function (note, index) {
              return jsx('div', { className: 'hc-agg__note', children: note }, 'an' + index)
            })
          })
        : null
    ]
  })
}

/** 归入预览弹层：逐条 from→to，**确认前一条都不写**；取消就关掉。 */
function AssignPreviewModal(props) {
  var rows = props.rows || []
  var target = props.target || {}
  return jsx('div', {
    style: {
      position: 'fixed',
      inset: '0',
      zIndex: '40',
      display: 'flex',
      alignItems: 'flex-start',
      justifyContent: 'center',
      padding: 'var(--s5, 24px)',
      background: 'var(--ui-bg-editor, transparent)'
    },
    'data-agg-preview': String(rows.length),
    children: jsxs('div', {
      style: {
        width: 'min(980px, 100%)',
        maxHeight: '82vh',
        overflow: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 'var(--s3, 12px)',
        padding: 'var(--s4, 16px)',
        border: 'var(--hairline) solid var(--ui-stroke-secondary, currentColor)',
        borderRadius: 'var(--r-md, 8px)',
        background: 'var(--ui-bg-secondary, transparent)'
      },
      children: [
        jsxs('div', {
          className: 'hc-agg__block-head',
          children: [
            jsx('span', { className: 'hc-agg__block-title', children: '归入预览（还没写）' }),
            jsx('span', { className: 'hc-chip', children: '目标：' + (target.name || target.slug || '—') }),
            jsx('span', { className: 'hc-chip', children: num(rows.length) + ' 条会话' })
          ]
        }),
        jsx('div', {
          className: 'hc-agg__hint',
          children: 'from 取自归属表当前行，to 是这次要写的目标；点「确认归入」之后后端才会写，写完立刻逐条回读。'
        }),
        jsxs('div', {
          className: 'hc-agg__table',
          children: [
            jsxs('div', {
              className: 'hc-agg__head',
              children: [
                jsx('div', { className: 'hc-agg__cell hc-agg__cell--title', children: '会话' }),
                jsx('div', { className: 'hc-agg__cell hc-agg__cell--project', children: 'from → to' }),
                jsx('div', { className: 'hc-agg__cell hc-agg__cell--basis', children: '动作' }),
                jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', children: '现状' })
              ]
            }),
            rows.map(function (row) {
              return jsxs('div', {
                className: 'hc-agg__row',
                'data-agg-preview-row': row.session_id,
                'data-agg-preview-action': row.action,
                children: [
                  jsx('div', {
                    className: 'hc-agg__cell hc-agg__cell--title',
                    children: jsx('span', { className: 'hc-agg__title', title: row.session_id, children: row.title || row.session_id })
                  }),
                  jsx('div', {
                    className: 'hc-agg__cell hc-agg__cell--project',
                    children: (row.from_name || '未归入') + ' → ' + (row.to_name || row.to_slug)
                  }),
                  jsx('div', { className: 'hc-agg__cell hc-agg__cell--basis', children: row.action_label }),
                  jsx('div', {
                    className: 'hc-agg__cell hc-agg__cell--time',
                    children: row.from_status || '没有归属行'
                  })
                ]
              }, row.session_id)
            })
          ]
        }),
        jsxs('div', {
          className: 'hc-agg__toolbar',
          children: [
            jsx('button', {
              type: 'button',
              className: 'hc-btn hc-btn--accent',
              'data-agg-preview-confirm': String(rows.length),
              disabled: props.busy || !rows.length,
              onClick: props.onConfirm,
              children: props.busy ? '写入中…' : '确认归入 ' + num(rows.length) + ' 条'
            }),
            jsx('button', {
              type: 'button',
              className: 'hc-btn',
              'data-agg-preview-cancel': '1',
              disabled: props.busy,
              onClick: props.onCancel,
              children: '取消（什么都不写）'
            }),
            jsx('span', { className: 'hc-agg__hint', children: '只改归属表（sync_cwd=false）：不动桌面端项目树，也不动会话 cwd' })
          ]
        })
      ]
    })
  })
}

/** 批量归入：多选 → 预览弹层 → 用户确认 → 后端写 → 逐条回读（请求成功不算验收）。 */
function BatchAssignPanel(props) {
  var selected = props.selected || []
  var items = props.items || []
  var projects = props.projects || []
  var targetState = useState('')
  var phaseState = useState('idle')
  var errorState = useState('')
  var readbackState = useState(null)
  var phase = phaseState[0]
  var readback = readbackState[0]
  var targetSlug = targetState[0] || ''
  var target = null
  projects.forEach(function (project) {
    if (project.slug === targetSlug) target = project
  })
  target = target || { slug: targetSlug }

  function titleOf(sessionId) {
    var found = ''
    items.forEach(function (item) {
      if (item.session_id === sessionId) found = item.title || ''
    })
    return found
  }

  /** 预览行：from = 归属表现状行（后端同一口径），to = 这次要写的目标；不重新判定。 */
  function previewRows() {
    return items.map(function (item) {
      var fromSlug = item.project_slug || ''
      var fromName = item.project_name || fromSlug
      var same = fromSlug === targetSlug && item.status === 'applied'
      return {
        session_id: item.session_id,
        title: item.title || '',
        from_slug: fromSlug,
        from_name: fromName,
        from_status: item.status || '',
        to_slug: targetSlug,
        to_name: target.name || targetSlug,
        action: same ? 'noop' : fromSlug ? 'update' : 'insert',
        action_label:
          fromSlug === targetSlug
            ? '同一项目：' + (item.status || 'suggested') + ' → applied（后端只改状态）'
            : fromSlug
              ? '改归属：' + fromName + ' → ' + (target.name || targetSlug)
              : '新归入（原来没有归属行）'
      }
    })
  }

  /** 确认之后才发写请求：confirm=true 是后端硬门槛，缺了它一律 400。 */
  function confirmAssign() {
    phaseState[1]('writing')
    errorState[1]('')
    send('/autogroup/assign', {
      slug: targetSlug,
      session_ids: selected,
      confirm: true,
      sync_cwd: false,
      reason: '面板批量归入'
    }).then(function (res) {
      if (!res || res.ok === false) {
        phaseState[1]('preview')
        errorState[1]('写入失败：' + ((res && res.message) || '后端没有给原因'))
        return
      }
      var payload = res.data || {}
      var data = payload.data || {}
      var batch = data.batch_id || ''
      var skippedIds = (data.skipped || []).map(function (row) { return row.session_id })
      fetchApplied().then(function (again) {
        var now = indexBySession(again && again.data)
        var rows = selected.map(function (sessionId) {
          var got = now[sessionId]
          var ok = !!got && got.project_slug === targetSlug && got.status === 'applied' && (!batch || got.batch_id === batch)
          return {
            session_id: sessionId,
            title: (got && got.title) || titleOf(sessionId),
            want: targetSlug,
            want_name: target.name || targetSlug,
            got_slug: got ? got.project_slug : '（回读没有这一行）',
            got_name: got ? got.project_name || got.project_slug : '（回读没有这一行）',
            got_status: got ? got.status : '—',
            got_batch: got ? got.batch_id : '—',
            skipped: skippedIds.indexOf(sessionId) >= 0,
            ok: ok
          }
        })
        readbackState[1]({
          batch_id: batch,
          op_id: data.op_id || '',
          rows: rows,
          written: data.written,
          backend: data.readback || {},
          notes: payload.notes || [],
          readback_error: again && again.ok === false ? again.message : ''
        })
        phaseState[1]('done')
        if (typeof props.onChanged === 'function') props.onChanged()
      })
    })
  }

  function renderReadback() {
    if (!readback) return null
    var bad = readback.rows.filter(function (row) { return !row.ok }).length
    return jsxs('div', {
      className: 'hc-agg__table',
      'data-agg-readback': String(readback.rows.length),
      'data-agg-readback-bad': String(bad),
      children: [
        jsxs('div', {
          className: 'hc-agg__head',
          children: [
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--title', children: '回读（已应用的建议）' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--project', children: '期望 → 实际' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--agent', children: 'status' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', children: 'batch_id' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--ops', children: '结论' })
          ]
        }),
        readback.rows.map(function (row) {
          return jsxs('div', {
            className: 'hc-agg__row',
            'data-agg-readback-row': row.session_id,
            'data-agg-readback-ok': row.ok ? 'true' : 'false',
            children: [
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--title',
                children: jsx('span', { className: 'hc-agg__title', title: row.session_id, children: row.title || row.session_id })
              }),
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--project',
                title: row.want + ' → ' + row.got_slug,
                children: (row.want_name || row.want) + ' → ' + (row.got_name || row.got_slug)
              }),
              jsx('div', { className: 'hc-agg__cell hc-agg__cell--agent', children: row.got_status }),
              jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', title: row.got_batch, children: row.got_batch === readback.batch_id ? '本批次' : String(row.got_batch).slice(0, 12) }),
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--ops',
                children: row.ok
                  ? jsx('span', { className: 'hc-agg__project', children: row.skipped ? '原本已在（未改写）' : '回读一致' })
                  : jsx('span', { className: 'hc-agg__warn', children: '回读不一致' })
              })
            ]
          }, row.session_id)
        })
      ]
    })
  }

  // 「预览归入」为什么点不动：空串才允许点，非空必须写在按钮旁边（不许点了没反应也没提示）。
  var blockReason = assignBlockReason({
    selectedCount: selected.length,
    targetSlug: targetSlug,
    busy: phase === 'writing'
  })
  var pageCount = props.pageCount || 0

  return jsxs('div', {
    className: 'hc-agg__block',
    'data-agg-assign': String(selected.length),
    'data-agg-assign-block': blockReason,
    children: [
      jsxs('div', {
        className: 'hc-agg__block-head',
        children: [
          jsx('span', { className: 'hc-agg__block-title', children: '批量归入项目' }),
          jsx('span', { className: 'hc-chip', 'data-agg-selected': String(selected.length), children: '已勾选 ' + num(selected.length) + ' 条' }),
          jsx('span', { className: 'hc-chip', title: '预览 → 你点确认 → 后端写 → 逐条回读', children: '四步不可跳' })
        ]
      }),
      jsxs('div', {
        className: 'hc-agg__toolbar',
        children: [
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg-select-all-btn': '1',
            disabled: !pageCount,
            title: '勾上本页全部会话（再点一次就是全不选）',
            onClick: props.onSelectAll,
            children: '全选本页 ' + num(pageCount) + ' 条'
          }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg-select-invert': '1',
            disabled: !pageCount,
            title: '本页勾选状态整体反转',
            onClick: props.onInvert,
            children: '反选'
          }),
          jsx('select', {
            className: 'hc-select',
            'data-agg-assign-target': targetSlug,
            value: targetSlug,
            disabled: phase === 'writing',
            title: '目标项目（默认不预选：必须自己选一个）',
            onChange: function (event) { targetState[1](event.target.value) },
            children: [jsx('option', { value: '', disabled: true, children: '— 未选择目标项目 —' }, '__none')].concat(
              projects.map(function (project) {
                return jsx('option', { value: project.slug, children: project.name || project.slug }, project.slug)
              })
            )
          }),
          jsx('button', {
            type: 'button',
            className: 'hc-btn hc-btn--accent',
            'data-agg-assign-preview': '1',
            disabled: !!blockReason,
            title: blockReason || '先出预览（一条都不写），确认之后才真归入',
            onClick: function () {
              errorState[1]('')
              phaseState[1]('preview')
            },
            children: '预览归入 ' + num(selected.length) + ' 条'
          }),
          blockReason
            ? jsx('span', { className: 'hc-agg__warn', 'data-agg-assign-reason': blockReason, children: '点不动的原因：' + blockReason })
            : jsx('span', { className: 'hc-agg__hint', children: '勾上左侧会话 → 选目标项目 → 预览 → 确认' }),
          readback
            ? jsx('span', { className: 'hc-agg__hint', children: '批次 ' + readback.batch_id + ' · op ' + readback.op_id + ' · 写入 ' + num(readback.written) + ' 条' })
            : null
        ]
      }),
      jsx(ResultLine, { name: 'assign', result: errorState[0] ? { ok: false, text: errorState[0] } : null }),
      readback && readback.readback_error
        ? jsx(ResultLine, { name: 'assign-readback', result: { ok: false, text: '回读请求失败：' + readback.readback_error } })
        : null,
      readback && readback.notes.length
        ? jsx('div', {
            className: 'hc-agg__notes',
            children: readback.notes.map(function (note, index) {
              return jsx('div', { className: 'hc-agg__note', children: note }, 'rn' + index)
            })
          })
        : null,
      renderReadback(),
      phase === 'preview' || phase === 'writing'
        ? jsx(AssignPreviewModal, {
            rows: previewRows(),
            target: target,
            busy: phase === 'writing',
            onConfirm: confirmAssign,
            onCancel: function () {
              phaseState[1]('idle')
            }
          })
        : null
    ]
  })
}

/** 撤销入口：列出最近面板写入批次，每条一个撤销按钮；撤销后重新回读核对（不认 200 当成功）。 */
function OpsPanel(props) {
  var query = useQuery('autogroup.ops', fetchOps, { staleMs: STALE_MS })
  var busyState = useState('')
  var resultState = useState(null)
  var ops = (query.data && query.data.data && query.data.data.ops) || []
  var busy = busyState[0]
  var result = resultState[0]

  function undo(op) {
    busyState[1](op.op_id)
    resultState[1](null)
    send('/autogroup/undo', { op_id: op.op_id }).then(function (res) {
      busyState[1]('')
      if (!res || res.ok === false) {
        resultState[1]({
          op_id: op.op_id,
          error: '撤销失败：' + ((res && res.message) || '后端没有给原因'),
          rows: [],
          notes: [],
          bad: 0
        })
        return
      }
      var payload = res.data || {}
      var data = payload.data || {}
      var backend = data.readback || {}
      var badIds = (backend.mismatched || []).map(function (item) {
        return item.session_id
      })
      var beforeById = {}
      ;(op.sessions || []).forEach(function (row) {
        beforeById[row.session_id] = row
      })
      fetchAllRows().then(function (again) {
        var now = indexBySession(again && again.data)
        var rows = (data.sessions || []).map(function (row) {
          var before = beforeById[row.session_id] || {}
          var got = now[row.session_id]
          var want = row.slug_before || ''
          var ok
          if (!before.row_existed_before) ok = !got // 写入前没有这一行 ⇒ 撤销后也不该有
          else ok = !!got && got.project_slug === want
          if (badIds.indexOf(row.session_id) >= 0) ok = false
          return {
            session_id: row.session_id,
            title: before.title || '',
            want: want || '（无归属行）',
            got_slug: got ? got.project_slug : '（回读没有这一行）',
            got_name: got ? got.project_name || got.project_slug : '（回读没有这一行）',
            got_status: got ? got.status : '—',
            assign: row.assign || '',
            cwd: row.cwd || '',
            ok: ok
          }
        })
        resultState[1]({
          op_id: op.op_id,
          error: again && again.ok === false ? '回读请求失败：' + again.message : '',
          rows: rows,
          notes: payload.notes || [],
          backend: backend,
          bad: rows.filter(function (row) {
            return !row.ok
          }).length
        })
        query.reload()
        if (typeof props.onChanged === 'function') props.onChanged()
      })
    })
  }

  function renderOps() {
    if (query.status === 'loading') return jsx(Skeleton, { lines: 3, label: '正在读取写入台账' })
    if (query.status === 'error') {
      return jsx(ErrorState, { title: '写入台账没读到', message: query.error, onRetry: query.reload, retryLabel: '重试台账' })
    }
    if (!ops.length) {
      return jsx(Empty, {
        title: '还没有面板写入',
        hint: '台账是空的 —— 先在下面做一次批量归入或单条切换，这里就会出现可撤销的批次。'
      })
    }
    return jsxs('div', {
      className: 'hc-agg__table',
      children: [
        jsxs('div', {
          className: 'hc-agg__head',
          children: [
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--title', children: '批次 / 操作' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--project', children: '逐会话 from → to' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', children: '时间' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--ops', children: '撤销' })
          ]
        }),
        ops.map(function (op) {
          var moves = (op.sessions || []).map(function (row) {
            return (row.from || '未归入') + ' → ' + (row.to || '—')
          })
          return jsxs('div', {
            className: 'hc-agg__row',
            'data-agg-op': op.op_id,
            'data-agg-op-undone': op.undone ? 'true' : 'false',
            children: [
              jsxs('div', {
                className: 'hc-agg__cell hc-agg__cell--title',
                children: [
                  jsx('span', { className: 'hc-agg__title', title: op.op_id, children: op.op + ' · ' + num(op.session_count) + ' 条会话' }),
                  jsx('span', { className: 'hc-agg__msg', children: op.undone ? '已撤销' : op.batch_id || '无 batch_id' })
                ]
              }),
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--project',
                title: moves.join(' / '),
                children: moves.length ? moves.slice(0, 3).join(' · ') + (moves.length > 3 ? ' …' : '') : '（无逐会话记录）'
              }),
              jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', title: absDate(op.created_at), children: relTime(op.created_at) }),
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--ops',
                children: jsx('button', {
                  type: 'button',
                  className: 'hc-btn hc-btn--tiny',
                  'data-agg-op-undo': op.op_id,
                  disabled: op.undone || !!busy || !(op.sessions || []).length,
                  title: op.undone
                    ? '这批已经撤销过（后端会 409）'
                    : (op.sessions || []).length
                      ? '逐字段还原到写入前，并回读核对'
                      : '这类操作（建/删项目）不自动反做，请用项目表单手动处理',
                  onClick: function () {
                    undo(op)
                  },
                  children: busy === op.op_id ? '撤销中…' : op.undone ? '已撤销' : '撤销这批'
                })
              })
            ]
          }, op.op_id)
        })
      ]
    })
  }

  function renderResult() {
    if (!result) return null
    if (result.error) {
      return jsx(ResultLine, { name: 'undo', result: { ok: false, text: result.error } })
    }
    return jsxs('div', {
      className: 'hc-agg__table',
      'data-agg-undo-readback': String(result.rows.length),
      'data-agg-undo-bad': String(result.bad),
      children: [
        jsxs('div', {
          className: 'hc-agg__head',
          children: [
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--title', children: '撤销后回读（重新读归属表，不信 200）' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--project', children: '期望还原 → 实际' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--agent', children: 'status' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', children: 'cwd' }),
            jsx('div', { className: 'hc-agg__cell hc-agg__cell--ops', children: '结论' })
          ]
        }),
        result.rows.map(function (row) {
          return jsxs('div', {
            className: 'hc-agg__row',
            'data-agg-undo-row': row.session_id,
            'data-agg-undo-ok': row.ok ? 'true' : 'false',
            children: [
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--title',
                children: jsx('span', { className: 'hc-agg__title', title: row.session_id, children: row.title || row.session_id })
              }),
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--project',
                title: row.want + ' → ' + row.got_slug,
                children: row.want + ' → ' + (row.got_name || row.got_slug)
              }),
              jsx('div', { className: 'hc-agg__cell hc-agg__cell--agent', children: row.got_status }),
              jsx('div', { className: 'hc-agg__cell hc-agg__cell--time', children: row.cwd || '未动' }),
              jsx('div', {
                className: 'hc-agg__cell hc-agg__cell--ops',
                children: row.ok
                  ? jsx('span', { className: 'hc-agg__project', children: '已还原（' + (row.assign || 'ok') + '）' })
                  : jsx('span', { className: 'hc-agg__warn', children: '还原不一致' })
              })
            ]
          }, row.session_id)
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-agg__block',
    'data-agg-ops': String(ops.length),
    children: [
      jsxs('div', {
        className: 'hc-agg__block-head',
        children: [
          jsx('span', { className: 'hc-agg__block-title', children: '撤销（最近写入批次）' }),
          jsx('span', { className: 'hc-chip', children: '最近 ' + num(ops.length) + ' 批' }),
          jsx('span', { className: 'hc-chip', title: '撤销 = 逐字段还原整行 + cwd 还原原值，然后回读核对', children: '撤销后回读' })
        ]
      }),
      renderOps(),
      renderResult(),
      result && result.notes.length
        ? jsx('div', {
            className: 'hc-agg__notes',
            children: result.notes.map(function (note, index) {
              return jsx('div', { className: 'hc-agg__note', children: note }, 'un' + index)
            })
          })
        : null
    ]
  })
}

/**
 * 扫描状态条：**四态都有明确画面**（进行中 / 空结果 / 有结果 / 失败）。
 *
 * 症状①「点了没反应」的根因有两个，这里都堵掉：
 *   1. 提交后到第一次轮询之间没有任何文字（只有按钮字换了）——现在一按下去就有
 *      「已提交，正在扫描」，并实时显示 已处理/总数、已发现候选数、已耗时；
 *   2. 空结果时只渲染一句「扫描 0 个」，视觉上等于没反应 ——现在给**独立空态**：
 *      「没有未分组会话」并附池子规模 / 已分组数（后端 `empty_reason` 原话）。
 *
 * `llm` 文案（卡面要求修正 line:1955 的旧语义）：后端现在如实上报
 *   `llm.code = llm_used | llm_not_called | no_model_configured | ai_not_requested`，
 *   `llm.used` 只是布尔镜像。所以这里按 `code` 分档，不再用 `llm.used === false` 猜。
 */
function ScanStatus(props) {
  var scan = props.scan
  var phase = scan.phase

  if (phase === 'idle') return null

  if (phase === 'failed') {
    return jsx(ErrorState, {
      title: '扫描失败',
      message: scan.error || '后端没有给出原因',
      detail: scan.jobId ? 'job ' + scan.jobId : '请求还没拿到 job_id 就失败了',
      onRetry: props.onRetry,
      retryLabel: '重新扫描'
    })
  }

  if (phase === 'submitting' || phase === 'running') {
    var progress = scan.progress || {}
    var known = typeof progress.total === 'number' && progress.total > 0
    var discovered = scan.discovered || {}
    var elapsed = scan.elapsedMs || 0
    return jsxs('div', {
      className: 'hc-agg__scan-result hc-agg__scan-result--live',
      'data-agg2-scan-phase': phase,
      'data-agg2-scan-job': scan.jobId || '',
      'data-agg2-scan-done': String(progress.done || 0),
      'data-agg2-scan-total': String(progress.total || 0),
      children: [
        jsx('span', {
          key: 'lead',
          className: 'hc-agg__scan-live',
          children: phase === 'submitting'
            ? '已提交，正在扫描…（等待后端返回 job）'
            : known
              ? '正在扫描：已处理 ' + num(progress.done) + ' / ' + num(progress.total) + ' 条'
              : '正在扫描：后端还在建候选池（还没有进度）'
        }),
        typeof discovered.candidates === 'number'
          ? jsx('span', {
              key: 'disc',
              className: 'hc-agg__note',
              'data-agg2-scan-candidates': String(discovered.candidates),
              children:
                '已发现未分组候选 ' + num(discovered.candidates) + ' 条（本池 ' + num(discovered.pool) +
                ' 条，其中已分组 ' + num(discovered.already_grouped) + ' 条'
                + (typeof discovered.already_grouped_all === 'number' && discovered.already_grouped_all
                  ? '；全机器归属表已分组 ' + num(discovered.already_grouped_all) + ' 条'
                  : '') + '）'
            })
          : null,
        jsx('span', {
          key: 'elapsed',
          className: 'hc-agg__note',
          'data-agg2-scan-elapsed': String(elapsed),
          children: '已耗时 ' + (elapsed / 1000).toFixed(1) + 's · 每 2s 刷新一次进度'
        })
      ]
    })
  }

  if (phase === 'done' && scan.result) {
    var result = scan.result
    var llm = result.llm || {}
    var llmCode = llm.code || ''
    var llmText = null
    if (llmCode === 'llm_used') {
      llmText = '模型已真实调用 ' + num(llm.calls) + ' 次（provider=' + (llm.provider || '—') +
        ' model=' + (llm.model || '—') + '，来源 ' + (llm.source || '—') + '）'
    } else if (llmCode === 'no_model_configured') {
      llmText = '没有可用模型（no_model_configured）：' + (llm.message || '')
    } else if (llmCode === 'llm_not_called') {
      llmText = '配了模型但一次都没调通（llm_not_called）：' + (llm.message || '')
    } else if (llmCode === 'ai_not_requested') {
      llmText = '本次只跑关键词规则，没有请求模型（ai_not_requested）'
    } else if (llmCode) {
      llmText = '模型状态 ' + llmCode + '：' + (llm.message || '')
    }
    var head = [
      jsx('span', {
        key: 'main',
        'data-agg2-scan-summary': '1',
        children:
          '上次扫描：扫描 ' + num(result.scanned) + ' 个会话 · 命中 ' + num(result.matched) +
          ' · 未命中 ' + num(result.unmatched) + ' · 落库 ' + num(result.saved) + ' 条' +
          '（耗时 ' + num(Math.round((result.elapsed_ms || 0) / 100) / 10) + 's）'
      }),
      llmText
        ? jsx('span', {
            key: 'llm',
            className: 'hc-agg__note',
            'data-agg2-scan-llm': llmCode,
            children: llmText
          })
        : null
    ]
    if (result.empty) {
      /**
       * 空态是**唯一一处**把几个数摊开给人看的地方，所以每个数都必须自证口径
       * （t_bba9216d）：
       *
       *   · 第一行 = 后端 `empty_reason` 原话（它已经按「本池 = 候选 + 已分组 + 忽略 + 其它」
       *     写成显式等式，并把「本池之外」与「全机器归属表」分开成句）；
       *   · 第二行只补两件**不与第一行冲突**的事：可用项目数、以及点明「这不是没反应」。
       *
       * 以前这里自己又拼了一遍「本池共 N 条会话，其中已分组 M 条」，与第一行的口径对不上，
       * 才写得出「池里共 0 条，其中已分组 91 条」这种字面不成立的句子。**不重复拼接**是
       * 根治办法 —— 数字只由后端口径产生一处。
       */
      var poolSize = typeof result.pool === 'number' ? result.pool : 0
      var poolApplied = typeof result.already_grouped === 'number' ? result.already_grouped : 0
      var appliedAll = typeof result.already_grouped_all === 'number' ? result.already_grouped_all : null
      head.unshift(
        jsxs('div', {
          key: 'empty',
          className: 'hc-agg__scan-empty',
          'data-agg2-scan-empty': '1',
          'data-agg2-scan-empty-pool': String(poolSize),
          'data-agg2-scan-empty-applied': String(poolApplied),
          'data-agg2-scan-empty-applied-all': appliedAll == null ? '' : String(appliedAll),
          children: [
            jsx('strong', {
              key: 't',
              'data-agg2-scan-empty-title': '1',
              children: result.empty_reason || '没有未分组会话'
            }),
            jsx('span', {
              key: 'd',
              className: 'hc-agg__note',
              'data-agg2-scan-empty-detail': '1',
              // 只补项目数与本句结论：数字一律引用第一行，不在这里重算第二套。
              children: '可用项目 ' + num((result.projects_available || []).length) +
                ' 个。这不是「没反应」——按当前 Agent 与口径确实没有可归类的会话。'
            }, 'd')
          ]
        })
      )
    }
    // ---- 五个数（操作员要求：不许只显示一个「0 条」了事）----
    // 当前 Agent ／ 口径（仅未分组 or 全量） ／ 符合条件总数 ／ 本次扫描条数 ／ 是否还有下一页
    var available = typeof result.available === 'number' ? result.available : result.candidates
    var scannedNow = typeof result.candidates === 'number' ? result.candidates : result.scanned
    var hasNext = result.truncated === true && typeof result.next_offset === 'number'
    var excludedInfo = result.excluded || {}
    var agentLabel = props.agentLabel || (result.agent === '__all__' ? '全部 Agent' : result.agent || '—')
    head.push(
      jsxs('div', {
        key: 'five',
        className: 'hc-agg__scan-five',
        'data-agg2-scan-five': '1',
        'data-agg2-scan-agent': String(result.agent || ''),
        'data-agg2-scan-scope': String(result.scope || ''),
        'data-agg2-scan-available': String(available == null ? '' : available),
        'data-agg2-scan-scanned': String(scannedNow == null ? '' : scannedNow),
        'data-agg2-scan-hasnext': hasNext ? '1' : '0',
        children: [
          jsx('span', { key: 'a', 'data-agg2-scan-five-item': 'agent',
                        children: '当前 Agent：' + agentLabel }),
          jsx('span', { key: 'b', 'data-agg2-scan-five-item': 'scope',
                        children: '口径：' + (result.scope === 'all' ? '全量重建（含已分组）' : '仅未分组') }),
          jsx('span', { key: 'c', 'data-agg2-scan-five-item': 'available',
                        children: '符合条件：' + num(available) + ' 条' }),
          jsx('span', { key: 'd', 'data-agg2-scan-five-item': 'scanned',
                        children: '本次扫描：' + num(scannedNow) + ' 条' }),
          jsx('span', { key: 'e', 'data-agg2-scan-five-item': 'hasnext',
                        children: hasNext
                          ? '还有下一页（limit=' + num(result.limit) + '，下一批从 ' + num(result.next_offset) + ' 开始）'
                          : '没有下一页（已扫完全部符合条件的会话）' }),
          jsx('span', {
            key: 'f',
            className: 'hc-agg__note',
            'data-agg2-scan-excluded': '1',
            children: '已排除：看板任务 ' + num(excludedInfo.kanban || 0) +
              ' 条 · 已分组 ' + num(excludedInfo.applied || 0) +
              ' 条 · 已撤销/其它状态 ' + num(excludedInfo.other_status || 0) +
              ' 条 · 手动忽略 ' + num(excludedInfo.rejected || 0) +
              ' 条 · 别的 Agent ' + num(excludedInfo.agent || 0) + ' 条'
          })
        ]
      })
    )
    // 「有会话但没生成建议」必须与「没有未分组会话」分开说
    if (!result.empty && scannedNow > 0 && (result.saved || 0) === 0) {
      head.push(
        jsx('span', {
          key: 'nosuggest',
          className: 'hc-agg__note',
          'data-agg2-scan-no-suggestion': '1',
          children: '注意：扫描了 ' + num(scannedNow) + ' 条，但没有生成新建议' +
            (result.matched ? '（命中 ' + num(result.matched) + ' 条，但都已存在同样的建议或已归入）'
                            : '（没有任何会话命中关键词规则或模型判定）') +
            ' —— 这与「没有未分组会话」是两回事。'
        })
      )
    }
    if ((result.notes || []).length) {
      result.notes.forEach(function (note, index) {
        head.push(jsx('span', { key: 'n' + index, className: 'hc-agg__note', children: note }))
      })
    }
    return jsxs('div', {
      className: 'hc-agg__scan-result',
      'data-job-id': scan.jobId,
      'data-agg2-scan-phase': 'done',
      children: head
    })
  }
  return null
}

/**
 * ③ 会话区（首屏右侧主体）：Agent 筛选 + 搜索 + 「已分组 / 未分组」两个分区。
 *
 * 分区是**两块同时存在**的，未分组那块带醒目但克制的左边框与计数，
 * 让人一眼看出「还有 N 条没归」。
 */
function AgentChips(props) {
  var agents = props.agents || []
  var value = props.value
  var chips = [
    jsxs(
      'button',
      {
        type: 'button',
        key: AGENT_ALL,
        className: 'hc-agg2__agent' + (value === AGENT_ALL ? ' hc-agg2__agent--on' : ''),
        'data-agg2-agent': AGENT_ALL,
        title: '逐个 profile 取会话再合并（界面上会写明跨几个 agent）',
        onClick: function () {
          props.onChange(AGENT_ALL)
        },
        children: ['全部', jsx('span', { key: 'n', className: 'hc-agg2__agent-n', children: num(agents.length) })]
      },
      AGENT_ALL
    )
  ]
  agents.forEach(function (agent) {
    chips.push(
      jsxs(
        'button',
        {
          type: 'button',
          key: agent.id,
          className: 'hc-agg2__agent' + (value === agent.id ? ' hc-agg2__agent--on' : ''),
          'data-agg2-agent': agent.id,
          title: agent.is_default ? '默认 profile' : '',
          onClick: function () {
            props.onChange(agent.id)
          },
          children: [agent.label || agent.id]
        },
        agent.id
      )
    )
  })
  return jsx('div', { className: 'hc-agg2__agents', 'data-agg2-agents': String(agents.length), children: chips })
}

/** 一条会话：勾选 / 标题 / agent / 项目（或未分组）/ 消息数 / 最后活动 / 快速改分组下拉。 */
function SessionRow(props) {
  var row = props.row
  var projects = props.projects || []
  var options = []
  if (!row.projectSlug) {
    options.push(jsx('option', { key: '__none', value: '', children: '未分组' }, '__none'))
  } else {
    options.push(jsx('option', { key: '__cur', value: row.projectSlug, children: row.project || row.projectSlug }, '__cur'))
  }
  options.push(jsx('option', { key: '__hint', value: '', disabled: true, children: '改到…' }, '__hint'))
  projects.forEach(function (project) {
    if (project.slug === row.projectSlug) return
    options.push(jsx('option', { key: project.slug, value: project.slug, children: project.name || project.slug }, project.slug))
  })
  options.push(
    jsx('option', { key: '__remove', value: '__remove', disabled: true, children: '移出项目（后端缺接口）' }, '__remove')
  )
  return jsxs('div', {
    className: 'hc-agg2__sess' + (row.projectSlug ? '' : ' hc-agg2__sess--loose'),
    'data-agg2-session': row.id,
    'data-agg2-session-grouped': row.projectSlug ? '1' : '0',
    draggable: 'true',
    onDragStart: function (event) {
      try {
        event.dataTransfer.setData('text/plain', row.id)
        event.dataTransfer.effectAllowed = 'move'
      } catch (e) {
        /* 某些浏览器不给 setData：拖拽降级，不影响下拉 */
      }
    },
    children: [
      jsx('input', {
        key: 'pick',
        type: 'checkbox',
        'data-agg2-pick': row.id,
        checked: !!props.selected,
        onChange: function () {
          props.onToggle(row.id)
        }
      }),
      jsx('span', {
        key: 'title',
        className: 'hc-agg2__sess-title',
        title: row.id,
        children: row.title
      }),
      jsx('span', { key: 'agent', className: 'hc-chip hc-agg2__sess-agent', 'data-agg2-session-agent': row.agent, children: row.agent || '—' }),
      jsx('span', {
        key: 'project',
        className: 'hc-agg2__sess-project' + (row.projectSlug ? '' : ' hc-agg2__sess-project--none'),
        'data-agg2-session-project': row.projectSlug || '',
        children: row.projectSlug ? row.project || row.projectSlug : '未分组'
      }),
      jsx('span', { key: 'msg', className: 'hc-agg2__sess-msg', children: num(row.message_count) + ' 条' }),
      jsx('span', {
        key: 'time',
        className: 'hc-agg2__sess-time',
        title: row.last_active ? absDate(row.last_active) : '',
        children: row.last_active ? relTime(row.last_active) : '—'
      }),
      jsx('select', {
        key: 'quick',
        className: 'hc-select hc-agg2__quick',
        'data-agg2-quick': row.id,
        value: row.projectSlug || '',
        title: '快速改分组：选完先出预览，确认才写',
        onChange: function (event) {
          var value = event.target.value
          if (!value || value === '__remove') return
          props.onQuick(row.id, value)
        },
        children: options
      })
    ]
  })
}

/** ③ 会话区主体。 */
function SessionBrowser(props) {
  var rows = props.rows || []
  var grouped = props.grouped || []
  var ungrouped = props.ungrouped || []
  var scope = props.scope
  var meta = props.meta
  var showGrouped = scope !== 'ungrouped'
  var showUngrouped = scope !== 'grouped'
  return jsxs('section', {
    className: 'hc-agg2__sessions',
    'data-agg2-sessions': String(rows.length),
    'data-agg2-grouped-count': String(grouped.length),
    'data-agg2-ungrouped-count': String(ungrouped.length),
    children: [
      jsxs('div', {
        key: 'head',
        className: 'hc-agg2__pane-head hc-agg2__pane-head--wrap',
        children: [
          jsx('span', { key: 'title', className: 'hc-agg2__pane-title', children: '会话' }),
          jsx(AgentChips, {
            key: 'agents',
            agents: props.agents,
            value: props.agent,
            onChange: props.onAgent
          }),
          jsx('input', {
            key: 'search',
            className: 'hc-set__input hc-agg2__search',
            'data-agg2-search': '1',
            placeholder: '按标题关键词过滤',
            value: props.search,
            onChange: function (event) {
              props.onSearch(event.target.value)
            }
          }),
          jsxs('div', {
            key: 'scope',
            className: 'hc-agg2__scopes',
            children: [
              ['all', '全部'],
              ['grouped', '已分组'],
              ['ungrouped', '未分组']
            ].map(function (pair) {
              return jsx(
                'button',
                {
                  type: 'button',
                  key: pair[0],
                  className: 'hc-btn hc-btn--tiny' + (scope === pair[0] ? ' hc-btn--accent' : ''),
                  'data-agg2-scope-btn': pair[0],
                  onClick: function () {
                    props.onScope(pair[0])
                  },
                  children: pair[1]
                },
                pair[0]
              )
            })
          })
        ]
      }),
      jsx('div', {
        key: 'note',
        className: 'hc-agg2__note',
        'data-agg2-scope-note': '1',
        children:
          '口径：只列桌面端可见会话（source 不含 acp/kanban/oneshot/subagent/tool，message_count≥1；' +
          'children 与归档由宿主 SQL 排除）' +
          (meta && meta.agents ? ' · 本次合并 ' + num(meta.agents.length) + ' 个 agent' : '') +
          (meta && meta.failed && meta.failed.length ? ' · 没读到：' + meta.failed.join('、') : '')
      }),
      showGrouped
        ? jsxs('div', {
            key: 'grouped',
            className: 'hc-agg2__sect',
            'data-agg2-sect': 'grouped',
            children: [
              jsxs('div', {
                key: 'h',
                className: 'hc-agg2__sect-head',
                children: [
                  jsx('span', { key: 't', className: 'hc-agg2__sect-title', children: '已分组' }),
                  jsx('span', {
                    key: 'c',
                    className: 'hc-chip',
                    'data-agg2-sect-count': 'grouped',
                    children: num(grouped.length) + ' 条'
                  })
                ]
              }),
              grouped.length
                ? jsx('div', {
                    key: 'list',
                    className: 'hc-agg2__sess-list',
                    children: grouped.map(function (row) {
                      return jsx(SessionRow, {
                        row: row,
                        projects: props.projects,
                        selected: props.selected.indexOf(row.id) >= 0,
                        onToggle: props.onToggle,
                        onQuick: props.onQuick
                      }, row.id)
                    })
                  })
                : jsx('div', {
                    key: 'empty',
                    className: 'hc-agg2__sect-empty',
                    children: '当前筛选下没有已分组的会话'
                  })
            ]
          })
        : null,
      showUngrouped
        ? jsxs('div', {
            key: 'ungrouped',
            className: 'hc-agg2__sect hc-agg2__sect--warn',
            'data-agg2-sect': 'ungrouped',
            children: [
              jsxs('div', {
                key: 'h',
                className: 'hc-agg2__sect-head',
                children: [
                  jsx('span', { key: 't', className: 'hc-agg2__sect-title', children: '未分组' }),
                  jsx('span', {
                    key: 'c',
                    className: 'hc-chip hc-chip--on',
                    'data-agg2-sect-count': 'ungrouped',
                    children: num(ungrouped.length) + ' 条'
                  }),
                  jsx('span', {
                    key: 'w',
                    className: 'hc-agg2__sect-warn',
                    children: ungrouped.length ? '还有 ' + num(ungrouped.length) + ' 条没归' : '全部归好了'
                  })
                ]
              }),
              ungrouped.length
                ? jsx('div', {
                    key: 'list',
                    className: 'hc-agg2__sess-list',
                    children: ungrouped.map(function (row) {
                      return jsx(SessionRow, {
                        row: row,
                        projects: props.projects,
                        selected: props.selected.indexOf(row.id) >= 0,
                        onToggle: props.onToggle,
                        onQuick: props.onQuick
                      }, row.id)
                    })
                  })
                : jsx('div', { key: 'empty', className: 'hc-agg2__sect-empty', children: '没有未分组的会话' })
            ]
          })
        : null
    ]
  })
}

/**
 * ④ 检查器：勾选摘要 + 单条改分组的二次确认 + 批量归入（复用既有 BatchAssignPanel）。
 *
 * 单条改分组（下拉 / 拖到项目行）只**发起**一个待确认动作，确认前零写入。
 */
function Inspector(props) {
  var selected = props.selected || []
  var pending = props.pending
  var projects = props.projects || []
  var items = props.items || []
  var target = null
  if (pending) {
    projects.forEach(function (project) {
      if (project.slug === pending.toSlug) target = project
    })
  }
  return jsxs('section', {
    className: 'hc-agg2__inspector',
    'data-agg2-inspector': '1',
    children: [
      jsxs('div', {
        key: 'head',
        className: 'hc-agg2__pane-head',
        children: [
          jsx('span', { key: 't', className: 'hc-agg2__pane-title', children: '操作' }),
          jsx('span', {
            key: 'c',
            className: 'hc-chip',
            'data-agg2-selected-count': String(selected.length),
            'data-agg2-selected-agent': props.agent || '',
            children: '选中 ' + num(selected.length) + ' 条'
          }),
          props.dropped
            ? jsx('span', {
                key: 'd',
                className: 'hc-agg__warn',
                'data-agg2-selected-dropped': String(props.dropped),
                children: '另有 ' + num(props.dropped) + ' 条不属于当前 Agent，已从勾选里剔除'
              })
            : null
        ]
      }),
      pending
        ? jsxs('div', {
            key: 'pending',
            className: 'hc-agg2__pending',
            'data-agg2-pending': pending.sessionId,
            'data-agg2-pending-to': pending.toSlug,
            children: [
              jsx('div', {
                key: 't',
                className: 'hc-agg2__pending-title',
                children: '待确认：单条改分组（还没写）'
              }),
              jsx('div', {
                key: 'row',
                className: 'hc-agg2__pending-row',
                children:
                  (pending.title || pending.sessionId) + '：' +
                  (pending.fromName || '未分组') + ' → ' + ((target && (target.name || target.slug)) || pending.toSlug)
              }),
              jsxs('div', {
                key: 'ops',
                className: 'hc-agg2__editor-ops',
                children: [
                  jsx('button', {
                    key: 'go',
                    type: 'button',
                    className: 'hc-btn hc-btn--accent hc-btn--tiny',
                    'data-agg2-pending-confirm': pending.sessionId,
                    disabled: props.busy,
                    title: '确认之后才写，写完立刻回读',
                    onClick: props.onConfirmPending,
                    children: props.busy ? '写入中…' : '确认切换'
                  }),
                  jsx('button', {
                    key: 'cancel',
                    type: 'button',
                    className: 'hc-btn hc-btn--tiny',
                    'data-agg2-pending-cancel': pending.sessionId,
                    disabled: props.busy,
                    onClick: props.onCancelPending,
                    children: '取消（不写）'
                  })
                ]
              })
            ]
          })
        : jsx('div', {
            key: 'hint',
            className: 'hc-agg2__note',
            'data-agg2-pending': '',
            children: '单条改分组：改行内下拉或把会话拖到左侧项目行，这里会出预览，确认后才写。'
          }),
      props.switchNote
        ? jsx('div', {
            key: 'note',
            className: props.switchNote.ok ? 'hc-adv__formsaved' : 'hc-adv__formerr',
            'data-agg2-switch-note': props.switchNote.ok ? 'ok' : 'fail',
            children: props.switchNote.text
          })
        : null,
      jsxs('div', {
        key: 'sel',
        className: 'hc-agg2__editor-ops',
        children: [
          jsx('button', {
            key: 'all',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-select-all': '1',
            disabled: !props.visibleIds.length,
            title: '全选当前列表里的会话（' + num(props.visibleIds.length) + ' 条）',
            onClick: props.onSelectAll,
            children: '全选本页'
          }),
          jsx('button', {
            key: 'invert',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-invert': '1',
            disabled: !props.visibleIds.length,
            onClick: props.onInvert,
            children: '反选'
          }),
          jsx('button', {
            key: 'clear',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-clear': '1',
            disabled: !selected.length,
            onClick: props.onClear,
            children: '清空选择'
          })
        ]
      }),
      selected.length
        ? jsx('div', {
            key: 'titles',
            className: 'hc-agg2__sel-list',
            children: selected.slice(0, 6).map(function (id) {
              var found = null
              items.forEach(function (item) {
                if (item.session_id === id) found = item
              })
              return jsx('div', { className: 'hc-agg2__sel-row', children: (found && found.title) || id }, id)
            })
          })
        : null,
      jsx('div', {
        key: 'batch',
        className: 'hc-agg2__batch',
        children: jsx(BatchAssignPanel, {
          selected: selected,
          items: items,
          projects: projects,
          pageCount: props.visibleIds.length,
          onSelectAll: props.onSelectAll,
          onInvert: props.onInvert,
          onChanged: props.onChanged
        })
      }),
      jsx('div', {
        key: 'gap',
        className: 'hc-agg2__note',
        'data-agg2-remove-gap': '1',
        children:
          '「移出项目」（单条退回未分组）当前**没有后端写接口**：/autogroup/switch 与 /autogroup/assign ' +
          '都要求目标 slug，/autogroup/reject 只写 rejected 表、不删归属行。界面上保留控件但置灰，缺口已写进报告。'
      })
    ]
  })
}

/** 折叠区抽屉：原生 <details>，默认收起（不传 open，避免受控后点不开）。 */
function Drawer(props) {
  return jsxs('details', {
    className: 'hc-agg2__drawer',
    'data-agg2-drawer': props.id,
    children: [
      jsx('summary', {
        key: 'head',
        className: 'hc-agg2__drawer-head',
        'data-agg2-drawer-head': props.id,
        children: [
          jsx('span', { key: 't', className: 'hc-agg2__drawer-title', children: props.title }),
          props.badge ? jsx('span', { key: 'b', className: 'hc-chip', children: props.badge }) : null,
          props.hint ? jsx('span', { key: 'h', className: 'hc-agg2__drawer-hint', children: props.hint }) : null
        ]
      }),
      jsx('div', { key: 'body', className: 'hc-agg2__drawer-body', children: props.children })
    ]
  })
}

/** 规则（只读）：slug → 关键词。折叠区里看，不占首屏。 */
function RulesPanel() {
  var query = useQuery('autogroup.rules', fetchRules, { staleMs: 60000 })
  if (query.status === 'loading') return jsx(Skeleton, { lines: 3, label: '正在读规则表' })
  if (query.status === 'error') {
    return jsx(ErrorState, { title: '规则表没读到', message: query.error, onRetry: query.reload, retryLabel: '重试规则表' })
  }
  var data = unwrap(query.data)
  var rules = data.rules || []
  if (!rules.length) return jsx(Empty, { title: '没有规则', hint: 'core.DEFAULT_RULES 也没读到，属异常（后端会带 notes 说明）' })
  return jsxs('div', {
    className: 'hc-agg2__rules',
    children: [
      jsx('div', {
        key: 'note',
        className: 'hc-agg2__note',
        children: '共 ' + num(rules.length) + ' 个项目有关键词规则 · rules.json ' + (data.extra_exists ? '存在' : '不存在（只有内置规则）')
      }),
      jsx('div', {
        key: 'list',
        className: 'hc-agg2__rules-list',
        children: rules.map(function (rule) {
          return jsxs(
            'div',
            {
              className: 'hc-agg2__rules-row',
              'data-agg2-rule': rule.slug,
              children: [
                jsx('span', { key: 'slug', className: 'hc-agg2__rules-slug', children: rule.slug }),
                jsx('span', { key: 'kw', className: 'hc-agg2__rules-kw', children: (rule.keywords || []).join('、') || '（没有关键词）' })
              ]
            },
            rule.slug
          )
        })
      })
    ]
  })
}

/**
 * 项目分组分区（t_1dea3c49 重排后的入口）。
 *
 * 一屏三块 + 折叠区，按「用户要做的事」排，不按模块堆：
 *
 *   ① 状态条：项目 / 已分组 / 未分组 / 待确认 / 批次（每格可点，切列表范围）
 *   ② 项目条（左，常驻）：项目清单 + 每行四个动作（重命名 / 改 slug / 改目录 / 解散 / 删除）
 *   ③ 会话区（中，首屏主体）：Agent 筛选 + 搜索 + 已分组 / 未分组两个分区 + 行内快速改分组
 *   ④ 检查器（右）：勾选摘要 + 单条改分组确认 + 批量归入（复用 BatchAssignPanel）
 *   ⑤ 折叠区（默认收起）：待确认建议 / AI 分类 / 自动模式 / 规则
 *   ⑥ 底部抽屉（默认收起）：撤销 / 批次历史
 *
 * 入口唯一性：扫描并生成建议只在这里出现一次；「跑一批」只在 AI 分类抽屉里（autogroup-ai.js
 * 已收敛成一个按钮）；「立即 tick」只在自动模式抽屉里。
 */
export function AutogroupSection(props) {
  var statusQuery = useQuery('autogroup.status', fetchStatus, { staleMs: STALE_MS })
  var entitiesQuery = useQuery('autogroup.entities', fetchEntities, { staleMs: STALE_MS })
  var agentsQuery = useQuery('agents', fetchAgents, { staleMs: 60000 })
  var agentState = useState(AGENT_ALL)
  var agent = agentState[0]
  var sessionsQuery = useQuery(
    'agg.sessions|' + agent,
    function () {
      return fetchSessionsOf(agent)
    },
    { staleMs: STALE_MS }
  )
  var ownershipQuery = useQuery('autogroup.ownership', fetchOwnership, { staleMs: STALE_MS })
  /**
   * 后端候选口径（t_bba9216d）：按钮上的「未分组 N 条」**必须**来自这里。
   *
   * 与真正跑批用的是同一个后端函数（`candidate_scope` ←→ `_candidate_snapshot`），
   * 也读同一批库（该 agent 自己的 `state.db`）。自己数会话列表会差三处：
   * 列表只取最近 100 条、`archived/hidden` 由宿主 SQL 决定、压缩续接子行前端拿不到父行，
   * 于是「按钮说 N 条、点下去说 M 条」—— 这正是要修的那个 bug。
   */
  var scopeQuery = useQuery(
    'autogroup.scope|' + agent,
    function () {
      return fetchScope(agent)
    },
    { staleMs: STALE_MS }
  )
  var suggestedQuery = useQuery(
    'agg.suggested|' + agent,
    function () {
      return agent === AGENT_ALL ? fetchSuggested(0) : fetchSuggestedAllPages()
    },
    { staleMs: STALE_MS }
  )

  var searchState = useState('')
  var scopeState = useState('all')
  var projectFilterState = useState('')
  var selectionState = useState([])
  var selected = selectionState[0]
  var pendingState = useState(null)
  var pending = pendingState[0]
  var switchBusyState = useState(false)
  var switchNoteState = useState(null)
  var scanState = useState({ phase: 'idle', jobId: '', error: '', progress: null, result: null })
  var scan = scanState[0]
  var setScan = scanState[1]
  var busy = scan.phase === 'submitting' || scan.phase === 'running'
  /**
   * 分组模式（t_f2338eb2）：A「只归类未分组」/ B「全量重建」。
   * 由上方 AutogroupModePanel 持有，下面三个旧入口（扫描 / AI 分类 / 自动模式 tick）都按它收口 ——
   * 选了 B 却还能点「只扫未分组」的扫描，正是本卡要消灭的口径矛盾。
   */
  var modeState = useState(MODE_UNGROUPED_ONLY)
  var mode = modeState[0]
  var rebuildMode = mode === MODE_REBUILD_ALL
  var acceptedState = useState({})
  var acceptedMap = acceptedState[0]
  var switchNotesState = useState({})
  var switchNotes = switchNotesState[0]
  /**
   * 「加载更多」追加的页（t_4d192e1b）：第一页走 useQuery 缓存，后续页按 offset 追加到这里。
   * 后端单次上限 200，建议数 >200 时只有这样才能取全；`reloadAll` 会清空它，
   * 否则重扫之后「第一页换了、旧追加页还在」，同一会话会重复出现。
   */
  var suggestedMoreState = useState({ items: [], busy: false, error: '' })
  var suggestedMore = suggestedMoreState[0]

  var projects = entitiesOf(entitiesQuery.data).map(projectBrief)
  var projectMap = {}
  projects.forEach(function (project) {
    projectMap[project.slug] = project
  })
  var owned = ownershipMap(ownershipQuery.data)
  var statusData = unwrap(statusQuery.data)
  var entitiesData = unwrap(entitiesQuery.data)
  var agents = agentsOf(agentsQuery.data)
  var sessionsMetaInfo = sessionsMeta(sessionsQuery.data)

  /** 会话行：桌面端可见口径 + 归属现状（applied 行）。 */
  var rows = sessionsOf(sessionsQuery.data)
    .filter(isDesktopVisible)
    .map(function (row) {
      var hit = owned[row.id]
      var slug = (hit && hit.project_slug) || ''
      var project = slug ? projectMap[slug] : null
      return {
        id: row.id,
        title: sessionTitle(row),
        agent: row.profile || (agent === AGENT_ALL ? '' : agent),
        message_count: row.message_count,
        last_active: row.last_active,
        projectSlug: slug,
        project: slug ? (project ? project.name || slug : (hit && hit.project_name) || slug) : ''
      }
    })

  var keyword = searchState[0].trim().toLowerCase()
  var projectFilter = projectFilterState[0]
  var scoped = rows.filter(function (row) {
    if (keyword && row.title.toLowerCase().indexOf(keyword) === -1) return false
    if (projectFilter && row.projectSlug !== projectFilter) return false
    return true
  })
  var grouped = scoped.filter(function (row) {
    return !!row.projectSlug
  })
  var ungrouped = scoped.filter(function (row) {
    return !row.projectSlug
  })
  var visibleIds = scoped.map(function (row) {
    return row.id
  })

  /**
   * 当前 Agent 的会话 id 表（t_52125d5d）：`/overview/sessions?agent=X` 的返回就是**作用域真相**。
   * 两个用途：
   *   1. 过滤建议列表（后端那条路由不收 agent 参数，只能按会话 id 归属）；
   *   2. 当勾选的第二道闸 —— 只有这张表里的 id 才可能进请求体。
   */
  var agentSessionIds = {}
  rows.forEach(function (row) {
    agentSessionIds[row.id] = true
  })
  /**
   * 勾选的作用域守卫：`selected` 里只留当前 Agent 会话列表真实存在的 id。
   *
   * 切换 Agent 时 `onAgent` 已经把 `selected` 清空，这里是**第二道闸**：即便有缓存 / 竞态 /
   * 旧组件回调把上一个 Agent 的 id 塞回来，也进不了按钮文案、扫描请求体、AI 请求体。
   * 「全部 agent」模式不做过滤（它本来就是跨 profile 合并口径，与改前一致）。
   */
  var scopedSelection = agent === AGENT_ALL
    ? selected
    : selected.filter(function (id) {
        return !!agentSessionIds[id]
      })
  /**
   * `selected` 里被作用域守卫丢掉的 id 数（正常应为 0）。非 0 时界面上明说一句，
   * 而不是让用户看到「勾了 3 条但按钮说已选 0 条」这种自相矛盾。
   */
  var droppedSelection = selected.length - scopedSelection.length
  /**
   * 后端候选口径（t_bba9216d）：`total_available` 是「能真正被分析」的条数，
   * 与跑批的候选集**同一个函数**（`candidate_scope` ⟷ `_candidate_snapshot`）算出来，
   * 也读同一批库。`null` = 还没读到（不冒充 0）。
   */
  var backendScope = unwrap(scopeQuery.data)
  var backendAvailable = typeof backendScope.total_available === 'number'
    ? backendScope.total_available
    : null
  /**
   * 扫描按钮与 AI 按钮**共用的一句文案**（t_8754de3f / t_bba9216d）：勾选状态是父层唯一真相源。
   *   · 未勾选 → 「分析全部未分组 N 条」
   *   · 已勾选 N 条 → 「分析已选 N 条」
   * 两处按钮都从这里取，不各写一套口径。
   *
   * **N 的来源**：优先后端的 `total_available`（= 点下去之后真正会处理的那 N 条）；
   * 读不到时才退回列表计数 `ungrouped.length`，并在下面把「来源」写进 title，
   * 不让人误以为两个数是同一个口径。
   */
  var listUngroupedCount = ungrouped.length
  var ungroupedCount = backendAvailable == null ? listUngroupedCount : backendAvailable
  var ungroupedCountSource = backendAvailable == null ? 'list' : 'backend'
  var selectedCount = scopedSelection.length
  var analyzeLabel = selectedCount
    ? '分析已选 ' + num(selectedCount) + ' 条'
    : '分析全部未分组 ' + num(ungroupedCount) + ' 条'

  var suggestedData = unwrap(suggestedQuery.data)
  var suggestedFirstPage = suggestedData.items || []
  var suggestedMoreItems = suggestedMore.items || []
  /**
   * 明细行 = 第一页 + 「加载更多」追加页，**按 session_id 去重**（t_c5d47b8e）。
   *
   * 两页的合并已经在 `loadMoreSuggested` 里挡过一次，但那只挡得住「追加页内部 / 追加页 vs
   * 之前追加页」；第一页与追加页之间的重叠（扫描或撤销改了建议库，第一页重载后内容前移）
   * 只有这里能挡。明细用 session_id 当 React key，重复就是重复 key 告警 + 行重复渲染。
   * 保留先出现的（第一页在前 = 后端排序靠前 = 更新）。
   */
  var suggestedRows = []
  var suggestedSeen = {}
  suggestedFirstPage.concat(suggestedMoreItems).forEach(function (item) {
    var sid = item && item.session_id
    if (sid) {
      if (suggestedSeen[sid]) return
      suggestedSeen[sid] = true
    }
    suggestedRows.push(item)
  })
  /**
   * Agent 隔离（t_52125d5d）：**只显示属于当前 Agent 会话列表的建议行**。
   *
   * 归属表 `auto-group/assignments.db` 是全局一份（所有 profile 的会话都在里面），
   * 而后端 `/autogroup/suggestions` 不收 `agent` 参数（本卡禁改 `dashboard/*.py`），
   * 所以隔离点落在前端：用上面那张 `agentSessionIds`（= `/overview/sessions?agent=X` 的会话 id）
   * 当权威作用域，建议行的 session_id 不在集合里 = 不属于当前 Agent。
   * 「全部 agent」模式不做过滤（全机器口径与改前逐字段一致）。
   */
  var suggestedItems = agent === AGENT_ALL
    ? suggestedRows
    : suggestedRows.filter(function (item) {
        return !!agentSessionIds[item.session_id]
      })
  /** 被 agent 过滤掉的行数：界面上明说「另有 N 条属于别的 Agent」，而不是悄悄吞掉。 */
  var suggestedOtherAgent = suggestedRows.length - suggestedItems.length
  var statusSuggested = Number(statusData.suggested) || 0
  var detailTotal = typeof suggestedData.total === 'number' ? suggestedData.total : null
  /** 建议库全量条数（单 agent 翻页取全时的 store_total；「全部」模式就等于明细 total）。 */
  var storeTotal = typeof suggestedData.store_total === 'number' ? suggestedData.store_total : detailTotal
  var suggestedTruncated = suggestedData.truncated === true
  /**
   * 总数口径（t_4d192e1b + t_52125d5d）：
   *   · 「全部 agent」= 后端明细 `data.total`（>200 条时状态条 / 徽标 / 明细不打架）；
   *   · 单 Agent = **本 agent 过滤后的行数**（已翻页取全；翻页封顶时下面明写「≥」）。
   * 不再拿 `/autogroup/status` 的全库数冒充单 agent 的数 —— 那正是「切了 agent 数字不变」的病根。
   */
  var suggestedTotal = agent === AGENT_ALL
    ? (detailTotal == null ? statusSuggested : detailTotal)
    : suggestedItems.length
  var suggestedRemaining = Math.max(0, suggestedTotal - suggestedItems.length)
  var suggestedCountMismatch =
    agent === AGENT_ALL && detailTotal != null && statusData.suggested != null && statusSuggested !== detailTotal

  /** 勾选 → BatchAssignPanel 需要的 item 形状（applied 行 status=applied，其余按 suggested）。 */
  function itemFor(sessionId) {
    var found = null
    rows.forEach(function (row) {
      if (row.id !== sessionId) return
      found = {
        session_id: row.id,
        title: row.title,
        project_slug: row.projectSlug,
        project_name: row.project,
        status: row.projectSlug ? 'applied' : 'suggested'
      }
    })
    if (found) return found
    suggestedItems.forEach(function (item) {
      if (item.session_id === sessionId) found = item
    })
    return found || { session_id: sessionId, title: sessionId, project_slug: '', project_name: '', status: 'suggested' }
  }

  function reloadAll() {
    statusQuery.reload()
    entitiesQuery.reload()
    ownershipQuery.reload()
    suggestedQuery.reload()
    // 追加页必须跟着第一页一起作废：重扫后第一页换了、旧追加页还留着就会重复/错位。
    suggestedMoreState[1]({ items: [], busy: false, error: '' })
    /**
     * 只刷新**当前 agent** 的会话列表（t_52125d5d）。
     * 旧写法 `refreshAll('agg.sessions|')` 会把缓存里每个 agent（含 `__all__` 合并 key）
     * 的会话都重取一遍 —— 在 xiaomo 下点扫描，后台却去打 default 的会话，
     * 既浪费又把「当前 Agent」的作用域说糊了。别的 agent 的 key 保持原样，
     * 切回去时 useQuery 按各自 staleMs 自己决定要不要重取。
     */
    refresh('agg.sessions|' + agent)
    refresh('autogroup.ops')
  }

  /**
   * 「加载更多」：按 offset = 已显示行数再取一页（后端单次上限 200），追加到明细尾部。
   *
   * 三条纪律：
   *   1. **按 session_id 去重**——第一页与追加页在并发重载时可能重叠，宁可少一条也不重复渲染；
   *   2. 后端 `ok:false` 不当成功：把 message 原样显示在按钮下面（`data-agg2-suggested-more-error`）；
   *   3. 取不到新行时明说（offset 是多少），不装作「已经取完」。
   */
  function loadMoreSuggested() {
    if (suggestedMore.busy) return
    /**
     * 单 Agent 视图下不需要「加载更多」（t_52125d5d）：那个模式已经在前端翻页取全，
     * `suggestedTotal` 就等于当前 agent 的行数，`suggestedRemaining` 恒为 0。
     * 这里留一道硬闸：万一按钮被渲染出来，也不许按「全库 offset」去取别个 agent 的行。
     */
    if (agent !== AGENT_ALL) return
    var offset = suggestedItems.length
    suggestedMoreState[1]({ items: suggestedMoreItems, busy: true, error: '' })
    fetchSuggested(offset).then(function (payload) {
      if (!payload || payload.ok === false) {
        suggestedMoreState[1]({
          items: suggestedMoreItems,
          busy: false,
          error: (payload && payload.message) || '加载更多失败（后端没给原因）'
        })
        return
      }
      // 两层信封：rest.js 先包一层 `{ok,data}`，后端 body 自己还有一层 `{ok,data:{items,total}}`。
      // 只拆一层会拿到 `{ok,data:{…}}`，`.items` 就成了 undefined —— 表现是「offset=200 没返回新行」。
      var body = unwrap(payload)
      var page = (body && body.items) || unwrap(body).items || []
      suggestedMoreState[1](function (prev) {
        var seen = {}
        var merged = []
        ;(prev.items || []).forEach(function (item) {
          if (seen[item.session_id]) return
          seen[item.session_id] = true
          merged.push(item)
        })
        page.forEach(function (item) {
          if (seen[item.session_id]) return
          seen[item.session_id] = true
          merged.push(item)
        })
        return {
          items: merged,
          busy: false,
          error: page.length ? '' : '后端 offset=' + offset + ' 没返回新行（建议库可能刚被改过，点「刷新」重读）'
        }
      })
    })
  }

  function toggleSelect(sessionId) {
    selectionState[1](function (current) {
      if (current.indexOf(sessionId) === -1) return current.concat([sessionId])
      return current.filter(function (id) {
        return id !== sessionId
      })
    })
  }

  function toggleSelectAll() {
    selectionState[1](selected.length && visibleIds.every(function (id) { return selected.indexOf(id) >= 0 }) ? [] : visibleIds)
  }

  function invertSelection() {
    selectionState[1](function (current) {
      var next = current.filter(function (id) {
        return visibleIds.indexOf(id) === -1
      })
      visibleIds.forEach(function (id) {
        if (current.indexOf(id) === -1) next.push(id)
      })
      return next
    })
  }

  /** 单条改分组：只登记待确认动作（下拉与拖拽都走这里），**确认前零写入**。 */
  function askSwitch(sessionId, slug) {
    var row = null
    rows.forEach(function (item) {
      if (item.id === sessionId) row = item
    })
    if (!row || row.projectSlug === slug) {
      pendingState[1](null)
      return
    }
    switchNoteState[1](null)
    pendingState[1]({
      sessionId: sessionId,
      title: row.title,
      fromSlug: row.projectSlug,
      fromName: row.project,
      toSlug: slug
    })
  }

  /** 确认之后才发写请求，写完**自己再读一遍归属表**核对（不认 200 当成功）。 */
  function confirmSwitch() {
    if (!pending) return
    var want = pending
    switchBusyState[1](true)
    send('/autogroup/switch', { session_id: want.sessionId, slug: want.toSlug, sync_cwd: false }).then(function (res) {
      if (!res || res.ok === false) {
        switchBusyState[1](false)
        switchNoteState[1]({ ok: false, text: '切换失败：' + ((res && res.message) || '后端没有给原因') })
        return
      }
      var data = unwrap(res.data)
      var backendReadback = data.readback || {}
      var backendBad =
        (backendReadback.mismatched || []).length + (backendReadback.cwd_mismatched || []).length
      get('/autogroup/suggestions', { params: { status: 'applied', limit: OWNERSHIP_LIMIT } }).then(function (again) {
        switchBusyState[1](false)
        var map = ownershipMap(again && again.data)
        var got = map[want.sessionId]
        var ok = !!got && got.project_slug === want.toSlug
        pendingState[1](null)
        switchNoteState[1]({
          ok: ok && backendBad === 0,
          text: ok
            ? '已切到 ' + want.toSlug + '（回读 applied 行一致' +
              (backendBad ? '，但后端报 ' + num(backendBad) + ' 条不一致' : '') + '）'
            : '回读不一致：' + want.sessionId + ' 现在读到的项目是 ' + ((got && got.project_slug) || '（没有 applied 行）')
        })
        reloadAll()
      })
    })
  }

  function setSwitchNote(sessionId, note) {
    switchNotesState[1](function (current) {
      var next = {}
      Object.keys(current).forEach(function (key) {
        next[key] = current[key]
      })
      next[sessionId] = note
      return next
    })
  }

  /** 待确认建议里行内「内联切换」：直接写（该按钮自带二次确认，见 autogroup-actions.js）。 */
  function switchSuggestion(sessionId, slug) {
    if (!slug) return
    send('/autogroup/switch', { session_id: sessionId, slug: slug, sync_cwd: false }).then(function (res) {
      if (!res || res.ok === false) {
        setSwitchNote(sessionId, { ok: false, text: '切换失败：' + ((res && res.message) || '后端没有给原因') })
        return
      }
      var data = unwrap(res.data)
      var readback = data.readback || {}
      var bad = (readback.mismatched || []).length + (readback.cwd_mismatched || []).length
      setSwitchNote(sessionId, {
        ok: bad === 0,
        text:
          bad === 0
            ? '已切到 ' + ((data.project && data.project.slug) || slug) + '（回读 ' + num(readback.checked) + ' 行一致）'
            : '回读不一致：' + JSON.stringify(readback.mismatched).slice(0, 120)
      })
      reloadAll()
    })
  }

  function onAccepted(sessionId, info) {
    acceptedState[1](function (current) {
      var next = {}
      Object.keys(current).forEach(function (key) {
        next[key] = current[key]
      })
      next[sessionId] = info || { text: '已归入' }
      return next
    })
    selectionState[1](function (current) {
      return current.filter(function (id) {
        return id !== sessionId
      })
    })
    reloadAll()
  }

  /**
   * 扫描：POST /autogroup/scan 拿 job_id（202），再轮询 GET /autogroup/job/{id}。
   *
   * 三处「点了没反应」的修法：
   *   1. 按下即 `phase='submitting'` ⇒ 状态条**立刻**有「已提交，正在扫描」；
   *   2. 请求体显式带 `agent`（当前 Agent 筛选）与 `scope`（模式口径），不再扫一批和界面无关的会话；
   *   3. 失败分支把后端 `error.message` 原样带出（不是「扫描请求失败」四个字），并保留 job_id 供追溯。
   */
  function startScan(opts) {
    if (busy) return
    /**
     * 勾选透传（t_8754de3f）：**勾了就把这批 id 放进请求体**，只处理它们；没勾才处理全部未分组。
     *   · `opts.all === true` = 用户点的是「改为分析全部未分组」这个显式入口，忽略当前勾选；
     *   · 勾选时 `scope='all'`：用户明确点名的会话即便已有 applied 归属也要进候选
     *     （`_candidate_snapshot` 在 scope=ungrouped 下会把这些行排除并只记进 skipped，等于静默少处理）；
     *   · 没勾时与改前逐字段一致（scope 仍由分组模式决定）。
     */
    var explicitAll = !!(opts && opts.all === true)
    /**
     * 只发**当前 Agent 作用域内**的勾选（t_52125d5d）：`scopedSelection` 已按当前 agent 的会话
     * 列表过滤过，所以请求体里不可能出现上一个 Agent 的 session_id。
     */
    var picked = explicitAll ? [] : scopedSelection.slice()
    var payload = {
      limit: SCAN_LIMIT,
      scope: picked.length ? 'all' : rebuildMode ? 'all' : 'ungrouped'
    }
    if (picked.length) payload.session_ids = picked
    if (agent && agent !== AGENT_ALL) payload.agent = agent
    setScan({
      phase: 'submitting',
      jobId: '',
      error: '',
      progress: null,
      discovered: null,
      result: null,
      startedAt: Date.now(),
      elapsedMs: 0
    })
    send('/autogroup/scan', payload).then(function (res) {
      if (!res || res.ok === false) {
        setScan({
          phase: 'failed',
          jobId: '',
          error: (res && (res.message || (res.error && res.error.message))) || '扫描请求失败',
          progress: null,
          discovered: null,
          result: null,
          startedAt: Date.now(),
          elapsedMs: 0
        })
        return
      }
      var body = unwrap(res.data)
      var jobId = body.job_id || (body.job && body.job.id) || ''
      if (!jobId) {
        setScan({
          phase: 'failed',
          jobId: '',
          error: '后端没有返回 job_id：' + JSON.stringify(body).slice(0, 160),
          progress: null,
          discovered: null,
          result: null,
          startedAt: Date.now(),
          elapsedMs: 0
        })
        return
      }
      setScan({
        phase: 'running',
        jobId: jobId,
        error: '',
        progress: (body.job && body.job.progress) || null,
        discovered: (body.job && body.job.discovered) || null,
        result: null,
        startedAt: Date.now(),
        elapsedMs: 0
      })
    })
  }

  useEffect(
    function () {
      if (scan.phase !== 'running' || !scan.jobId) return undefined
      var jobId = scan.jobId
      var cancelled = false
      var timer = null
      var startedAt = scan.startedAt || Date.now()
      function tick() {
        get('/autogroup/job/' + encodeURIComponent(jobId)).then(function (res) {
          if (cancelled) return
          var elapsedMs = Date.now() - startedAt
          if (!res || res.ok === false) {
            setScan({
              phase: 'failed',
              jobId: jobId,
              error: (res && (res.message || (res.error && res.error.message))) || '查询扫描进度失败',
              progress: null,
              discovered: null,
              result: null,
              startedAt: startedAt,
              elapsedMs: elapsedMs
            })
            return
          }
          var body = unwrap(res.data)
          var job = body.data || body
          var state = body.state || job.state || ''
          var progress = body.progress || job.progress || null
          var discovered = body.discovered || job.discovered || null
          if (state === 'failed') {
            var failure = body.error || job.error || {}
            setScan({
              phase: 'failed',
              jobId: jobId,
              error: (failure && failure.message) || '扫描失败（后端没给原因）',
              progress: progress,
              discovered: discovered,
              result: null,
              startedAt: startedAt,
              elapsedMs: elapsedMs
            })
            return
          }
          if (state === 'done') {
            var result = job.result || null
            setScan({
              phase: 'done',
              jobId: jobId,
              error: '',
              progress: progress,
              discovered: discovered,
              result: result,
              startedAt: startedAt,
              elapsedMs: result && result.elapsed_ms ? result.elapsed_ms : elapsedMs
            })
            reloadAll()
            return
          }
          if (elapsedMs > SCAN_TIMEOUT_MS) {
            setScan({
              phase: 'failed',
              jobId: jobId,
              error: '轮询超过 3 分钟仍未完成（job ' + jobId + ' 可能还在后台跑，稍后刷新看结果）',
              progress: progress,
              discovered: discovered,
              result: null,
              startedAt: startedAt,
              elapsedMs: elapsedMs
            })
            return
          }
          setScan({
            phase: 'running',
            jobId: jobId,
            error: '',
            progress: progress,
            discovered: discovered,
            result: null,
            startedAt: startedAt,
            elapsedMs: elapsedMs
          })
          timer = setTimeout(tick, POLL_MS)
        })
      }
      tick()
      return function () {
        cancelled = true
        if (timer) clearTimeout(timer)
      }
    },
    [scan.phase, scan.jobId]
  )

  var appliedTotal = statusData.applied
  var metrics = [
    {
      id: 'projects',
      label: '项目',
      value: projects.length,
      active: false,
      title: 'projects.db 里未归档的项目实体数（按项目库回读）',
      onClick: function () {
        projectFilterState[1]('')
      }
    },
    {
      id: 'grouped',
      label: '已分组',
      value: grouped.length,
      active: scopeState[0] === 'grouped',
      tone: 'ok',
      title:
        '当前 agent/搜索筛选下、桌面端可见且已有 applied 归属行的会话数；' +
        '归属表 applied 合计 ' + num(appliedTotal) + ' 行（含其它 profile 与不可见来源）',
      onClick: function () {
        scopeState[1](scopeState[0] === 'grouped' ? 'all' : 'grouped')
      }
    },
    {
      id: 'ungrouped',
      label: '未分组',
      value: ungrouped.length,
      active: scopeState[0] === 'ungrouped',
      tone: 'warn',
      title: '当前筛选下桌面端可见、但归属表里没有 applied 行的会话数（= 列表里未分组分区的条数）',
      onClick: function () {
        scopeState[1](scopeState[0] === 'ungrouped' ? 'all' : 'ungrouped')
      }
    },
    {
      id: 'suggested',
      label: '待确认',
      // 与抽屉徽标同一个值（明细接口的 data.total）：建议数 >200 时状态条不会和明细行数打架。
      // 单 Agent 视图下这个值 = 该 Agent 会话列表里的建议行数（t_52125d5d），不再拿全库数冒充。
      value: suggestedTotal,
      title:
        agent === AGENT_ALL
          ? '归属表里 status=suggested 的条数（还没应用的）。' +
            '总数取自后端汇总值；明细一次最多 200 条，' +
            '抽屉里明写「显示 X / 共 N 条」，剩余的可点「加载更多」取完。'
          : '当前 Agent（' + agent + '）会话列表里 status=suggested 的条数（还没应用的）。' +
            '归属表是全局一份、后端该路由不收 agent 参数，所以这里按本 Agent 的会话 id 现算：' +
            '别的 Agent 的建议不计入，也不显示。',
      onClick: function () {
        var node = document.querySelector('[data-agg2-drawer="suggested"]')
        if (node) node.open = true
      }
    },
    {
      id: 'batches',
      label: '批次',
      value: statusData.batches_total,
      title: 'auto-group 库 batches 表的行数（一次 apply 一行，不是会话数）',
      onClick: function () {
        var node = document.querySelector('[data-agg2-drawer="ops"]')
        if (node) node.open = true
      }
    }
  ]

  var scopeText =
    agent === AGENT_ALL
      ? '全机器 · 所有 agent' + (sessionsMetaInfo ? '（合并 ' + num(sessionsMetaInfo.agents.length) + ' 个）' : '')
      : '单 agent · ' + agent

  function renderSuggestionTable() {
    if (suggestedQuery.status === 'loading') return jsx(Skeleton, { lines: 3, label: '正在读取待确认建议' })
    if (suggestedQuery.status === 'error') {
      return jsx(ErrorState, {
        title: '建议列表没读到',
        message: suggestedQuery.error,
        onRetry: suggestedQuery.reload,
        retryLabel: '重试列表'
      })
    }
    if (!suggestedItems.length) {
      return jsx(Empty, {
        title: '还没有待确认建议',
        hint: '归属表里没有 status=suggested 的行 —— 点上面的「分析…」按钮（勾选时只分析已选，未勾选时分析全部未分组）：按关键词规则把最近的会话分配成建议（只写建议，不动会话、不改 cwd）。'
      })
    }
    return jsxs('div', {
      className: 'hc-agg__table',
      'data-agg2-suggested-table': String(suggestedItems.length),
      children: [
        jsxs('div', {
          key: 'head',
          className: 'hc-agg__head',
          children: [
            jsx('div', { key: 't', className: 'hc-agg__cell hc-agg__cell--title', children: '会话标题' }),
            jsx('div', { key: 'a', className: 'hc-agg__cell hc-agg__cell--agent', children: 'agent' }),
            jsx('div', { key: 'p', className: 'hc-agg__cell hc-agg__cell--project', children: '建议项目（可内联切换）' }),
            jsx('div', { key: 'b', className: 'hc-agg__cell hc-agg__cell--basis', children: '判定依据' }),
            jsx('div', { key: 'd', className: 'hc-agg__cell hc-agg__cell--time', children: '生成时间' }),
            jsx('div', { key: 'o', className: 'hc-agg__cell hc-agg__cell--ops', children: '' })
          ]
        }),
        suggestedItems.map(function (item, index) {
          return jsx(
            SuggestRow,
            {
              item: item,
              projects: projects,
              selected: selected.indexOf(item.session_id) >= 0,
              switchNote: switchNotes[item.session_id] || null,
              accepted: acceptedMap[item.session_id] || null,
              busy: false,
              onToggle: toggleSelect,
              onSwitch: switchSuggestion,
              onAccepted: onAccepted
            },
            item.session_id || 's' + index
          )
        })
      ]
    })
  }

  return jsxs('div', {
    className: 'hc-agg hc-agg2',
    'data-agg2-root': '1',
    children: [
      jsxs('div', {
        key: 'head',
        className: 'hc-agg2__head',
        children: [
          jsx('span', { key: 't', className: 'hc-agg2__title', children: '项目分组' }),
          jsx('span', {
            key: 'c',
            className: 'hc-chip hc-agg2__scope',
            'data-agg2-scope': scopeText,
            title: '会话来自 /overview/sessions（按 agent 逐个 profile 取）；归属与项目来自 auto-group 库与 projects.db',
            children: scopeText
          }),
          jsx('span', {
            key: 'n',
            className: 'hc-agg2__hint',
            children: busy
              ? '扫描中：已处理 ' + num((scan.progress || {}).done) + ' / ' + num((scan.progress || {}).total)
              : '同一功能只留一个入口：扫描在「待确认建议」抽屉、跑批在「AI 分类」抽屉、tick 在「自动模式」抽屉'
          }),
          jsx('button', {
            key: 'r',
            type: 'button',
            className: 'hc-btn hc-btn--tiny',
            'data-agg2-refresh': '1',
            disabled: statusQuery.busy,
            title: '重新读取状态条 / 项目 / 会话 / 归属（不触发扫描、不跑模型）',
            onClick: reloadAll,
            children: '刷新'
          })
        ]
      }),
      jsx(StatusStrip, {
        key: 'strip',
        metrics: metrics,
        scope: scopeText,
        scopeTitle: '会话范围（与项目/归属表的「全机器口径」分开写，两个数不混在一组里）'
      }),
      jsx(AutogroupModePanel, {
        key: 'mode',
        mode: mode,
        onMode: function (next) {
          modeState[1](next)
        },
        onChanged: reloadAll
      }),
      jsxs('div', {
        key: 'work',
        className: 'hc-agg2__workspace',
        children: [
          jsx(ProjectRail, {
            key: 'rail',
            items: projects,
            root: entitiesData.root || '',
            activeSlug: projectFilter,
            onPick: function (slug) {
              projectFilterState[1](slug)
            },
            onDropSession: askSwitch,
            onChanged: reloadAll
          }),
          jsx(SessionBrowser, {
            key: 'sessions',
            rows: scoped,
            grouped: grouped,
            ungrouped: ungrouped,
            agents: agents,
            agent: agent,
            meta: sessionsMetaInfo,
            scope: scopeState[0],
            search: searchState[0],
            selected: scopedSelection,
            projects: projects,
            onAgent: function (next) {
              /**
               * 切换 Agent = 换作用域（t_52125d5d）。**同一个 Agent 再点一次不算切换**，
               * 不做无意义的清空与重拉。
               *
               * 四件事必须同时发生，缺一件就会串数据：
               *   1. `selected = []` —— 勾选立即清空，且**切回来也不恢复**（避免拿旧 id 误操作）；
               *   2. 待确认动作 / 行内提示 / 接受回执清掉 —— 它们都钉着具体 session_id，
               *      留着就等于把上一个 Agent 的 id 送进下一个 Agent 的写请求；
               *   3. 扫描面板回 idle —— 否则「当前 Agent：xiaomo」下面挂着 default 的扫描数字；
               *   4. 数据重拉 —— 会话列表按 agent 分 key（换 key 即重取），
               *      建议 / 状态 / 归属 / 操作面板显式刷新（切回原 Agent 也要新数据，不吃旧缓存）。
               */
              if (next === agent) return
              selectionState[1]([])
              pendingState[1](null)
              switchNoteState[1](null)
              acceptedState[1]({})
              switchNotesState[1]({})
              suggestedMoreState[1]({ items: [], busy: false, error: '' })
              setScan({ phase: 'idle', jobId: '', error: '', progress: null, result: null })
              agentState[1](next)
              /**
               * 会话列表**只刷新目标 agent 那一份**（t_52125d5d）。
               *
               * 不用 `refreshAll('agg.sessions|')`：那会把缓存里**所有** agent 的会话 key
               * （含 `__all__` 那个合并 key，它内部还会逐个 profile 回填）一起重取 ——
               * 于是「当前 Agent 是 xiaomo」的时候后台仍在打 `agent=default` 的请求，
               * 正是本卡要杜绝的跨作用域流量。目标 key 不存在时 refresh 是空操作，
               * 新 key 挂载时 useQuery 自己会取。
               */
              refresh('agg.sessions|' + next)
              refresh('agg.suggested|' + next)
              statusQuery.reload()
              entitiesQuery.reload()
              ownershipQuery.reload()
              refresh('autogroup.ops')
            },
            onSearch: function (value) {
              searchState[1](value)
            },
            onScope: function (value) {
              scopeState[1](value)
            },
            onToggle: toggleSelect,
            onQuick: askSwitch
          }),
          jsx(Inspector, {
            key: 'inspector',
            selected: scopedSelection,
            agent: agent === AGENT_ALL ? '全部' : agent,
            dropped: droppedSelection,
            items: scopedSelection.map(itemFor),
            projects: projects,
            visibleIds: visibleIds,
            pending: pending,
            busy: switchBusyState[0],
            switchNote: switchNoteState[0],
            onSelectAll: toggleSelectAll,
            onInvert: invertSelection,
            onClear: function () {
              selectionState[1]([])
            },
            onConfirmPending: confirmSwitch,
            onCancelPending: function () {
              pendingState[1](null)
            },
            onChanged: reloadAll
          })
        ]
      }),
      jsxs('div', {
        key: 'drawers',
        className: 'hc-agg2__drawers',
        children: [
          jsxs(
            Drawer,
            {
              id: 'suggested',
              title: '待确认建议',
              // 徽标 = 后端总数（不是已渲染行数）：>200 条时徽标不会再和状态条差 300。
              badge: num(suggestedTotal) + ' 条',
              hint: '扫描入口已收敛到上方「模式 + 先预览」；这里只列已落库的建议',
              children: [
                jsxs('div', {
                  key: 'gate',
                  className: 'hc-agg2__gate',
                  'data-agg2-mode-gate': 'scan',
                  'data-agg2-mode-gate-state': rebuildMode ? 'blocked' : 'open',
                  children: [
                    jsx('span', { key: 't', className: 'hc-agg2__gate-title', children: '扫描并生成建议' }),
                    jsx('span', {
                      key: 'h',
                      className: 'hc-agg2__gate-text',
                      children: rebuildMode
                        ? '当前模式=全量重建：关键词扫描只处理未分组会话，与全量重建口径矛盾，故停用。全量重建请用上方「先预览」——它按当前模式扫全部可见会话。'
                        : '当前模式=只归类未分组：关键词扫描只处理未分组会话，与 A 模式口径一致，可用。'
                    })
                  ]
                }),
                jsxs('div', {
                  key: 'toolbar',
                  className: 'hc-agg__toolbar',
                  children: [
                    jsx('button', {
                      key: 'scan',
                      type: 'button',
                      className: 'hc-btn hc-btn--accent',
                      'data-agg2-scan': '1',
                      'data-agg2-scan-blocked': rebuildMode ? '1' : '0',
                      'data-agg2-scan-mode': selectedCount ? 'selected' : 'all',
                      'data-agg2-scan-count': String(selectedCount || ungroupedCount),
                      'data-agg2-scan-count-source': ungroupedCountSource,
                      disabled: busy || rebuildMode,
                      title: rebuildMode
                        ? '全量重建模式下停用：关键词扫描只处理未分组会话，与 B 模式口径矛盾；请用上方「先预览」'
                        : busy
                          ? '扫描进行中，完成前不重复触发'
                          : selectedCount
                            ? '只分析勾选的 ' + num(selectedCount) + ' 条（请求体带 session_ids）'
                            : '按关键词规则扫描全部 ' + num(ungroupedCount) + ' 条未分组会话（请求体不带 session_ids）'
                              + (ungroupedCountSource === 'backend'
                                ? '；N 来自 /autogroup/ai/scope?agent=' + encodeURIComponent(agent) +
                                  '（与跑批同一个后端口径）'
                                : '；N 来自当前会话列表计数（后端口径还没读到）'),
                      onClick: function () {
                        startScan()
                      },
                      children: busy ? '扫描中…' : rebuildMode ? '全量重建模式下停用' : analyzeLabel
                    }),
                    selectedCount && !rebuildMode
                      ? jsx('button', {
                          key: 'scan-all',
                          type: 'button',
                          className: 'hc-btn',
                          'data-agg2-scan-all': '1',
                          disabled: busy,
                          title:
                            '显式入口：忽略当前勾选，处理全部 ' + num(ungroupedCount) +
                            ' 条未分组会话（请求体不带 session_ids）',
                          onClick: function () {
                            startScan({ all: true })
                          },
                          children: '改为分析全部未分组 ' + num(ungroupedCount) + ' 条'
                        })
                      : null,
                    jsx('span', {
                      key: 'hint',
                      className: 'hc-agg__hint',
                      children: busy
                        ? '已处理 ' + num((scan.progress || {}).done) + ' / ' + num((scan.progress || {}).total) + (scan.jobId ? '（job ' + scan.jobId + '）' : '')
                        : rebuildMode
                          ? '要重建全部项目结构：请用上方「模式 → 先预览 → 确认应用」，预览零写入、可整批撤销'
                          : '扫描是异步 job：先拿 job_id，再每 2 秒轮询一次进度，最长 3 分钟'
                    })
                  ]
                }),
                jsx(ScanStatus, {
                  key: 'status',
                  scan: scan,
                  agentLabel: agent === AGENT_ALL ? '全部 Agent' : agent,
                  onRetry: startScan
                }),
                jsxs('div', {
                  key: 'counts',
                  className: 'hc-agg2__counts',
                  'data-agg2-suggested-counts':
                    String(suggestedItems.length) + '/' + String(suggestedTotal),
                  children: [
                    // 一句话说清三个数字：显示多少行 / 后端一共多少条（徽标与状态条都取后一个）。
                    jsx('span', {
                      key: 'text',
                      className: 'hc-agg2__counts-text',
                      'data-agg2-suggested-loaded': String(suggestedItems.length),
                      'data-agg2-suggested-total': String(suggestedTotal),
                      children:
                        '显示 ' + num(suggestedItems.length) + ' / 共 ' + num(suggestedTotal) + ' 条'
                    }),
                    agent === AGENT_ALL
                      ? null
                      : jsx('span', {
                          key: 'scope',
                          className: 'hc-agg2__counts-note',
                          'data-agg2-suggested-scope': agent,
                          'data-agg2-suggested-other': String(suggestedOtherAgent),
                          'data-agg2-suggested-truncated': suggestedTruncated ? '1' : '0',
                          children:
                            '只看 ' + agent + ' 的会话' +
                            (suggestedOtherAgent
                              ? '（另有 ' + num(suggestedOtherAgent) + ' 条属于别的 Agent，不显示）'
                              : '') +
                            (suggestedTruncated
                              ? '；建议库共 ' + num(storeTotal) + ' 条，前端只翻了 ' +
                                num(SUGGESTED_SCAN_MAX) + ' 条，这个数可能偏小'
                              : '')
                        }),
                    suggestedRemaining > 0
                      ? jsx('button', {
                          key: 'more',
                          type: 'button',
                          className: 'hc-btn',
                          'data-agg2-suggested-more': '1',
                          'data-agg2-suggested-remaining': String(suggestedRemaining),
                          disabled: suggestedMore.busy,
                          title:
                            '后端单次最多 200 条：点这里按 offset 继续取，取完按钮自动消失（剩余条数就写在这）',
                          onClick: loadMoreSuggested,
                          children: suggestedMore.busy
                            ? '加载中…'
                            : '加载更多（还有 ' + num(suggestedRemaining) + ' 条）'
                        })
                      : null,
                    suggestedMore.error
                      ? jsx('span', {
                          key: 'err',
                          className: 'hc-agg2__counts-err',
                          'data-agg2-suggested-more-error': '1',
                          children: suggestedMore.error
                        })
                      : null,
                    suggestedCountMismatch
                      ? jsx('span', {
                          key: 'warn',
                          className: 'hc-agg2__counts-err',
                          'data-agg2-count-mismatch': '1',
                          children:
                            '口径不一致：/autogroup/status 报 ' + num(statusSuggested) +
                            ' 条，明细接口报 ' + num(detailTotal) +
                            ' 条（建议库可能正被写入，点「刷新」重读）'
                        })
                      : null
                  ]
                }),
                jsx('div', { key: 'table', className: 'hc-agg2__drawer-table', children: renderSuggestionTable() })
              ]
            },
            'suggested'
          ),
          jsx(
            Drawer,
            {
              id: 'ai',
              title: 'AI 分类',
              badge: '',
              hint: '受模式约束：只处理未分组/待确认会话；全量重建请用上方「先预览」',
              children: jsx(AutogroupAiPanel, {
                // key = 当前 agent（t_52125d5d）：换 Agent 时组件重挂载，面板内部的批次预览 /
                // 已处理计数 / 轮询 job 状态全部归零 —— 上一个 Agent 的预览行不会被当成这个 Agent 的。
                key: 'ai-' + agent,
                onChanged: reloadAll,
                modeBlocked: rebuildMode,
                agent: agent,
                // 共用勾选（t_8754de3f）：父层的 selected 数组原样传下去，AI 面板不再自己一套口径。
                // 作用域守卫（t_52125d5d）：只传当前 Agent 会话列表里真实存在的 id，
                // 并把这张 id 表也交给面板 —— 面板的 skip_ids 也按它收口。
                selectedIds: scopedSelection,
                scopeIds: agent === AGENT_ALL ? null : agentSessionIds,
                ungroupedCount: ungroupedCount,
                //: 这个 N 到底来自后端同源口径还是列表计数（探针/界面都要能说清）
                ungroupedCountSource: ungroupedCountSource,
                scopeTotal: backendAvailable,
                modeLabel: rebuildMode ? '全量重建' : '只归类未分组'
              })
            },
            'ai'
          ),
          jsx(
            Drawer,
            {
              id: 'model',
              title: 'AI 模型',
              badge: '',
              hint: 'provider/model 两级配置：按 Agent 独立，保存后回读盘上真值',
              children: jsx(AutogroupModelPanel, { agents: agents, onSaved: reloadAll })
            },
            'model'
          ),
          jsx(
            Drawer,
            {
              id: 'auto',
              title: '自动模式',
              badge: '',
              hint: '受模式约束：tick 只按规则补未分组会话',
              children: jsx(AutoPanel, { onChanged: reloadAll, modeBlocked: rebuildMode })
            },
            'auto'
          ),
          jsx(Drawer, { id: 'rules', title: '规则', badge: '', hint: '只读关键词表', children: jsx(RulesPanel, {}) }, 'rules')
        ]
      }),
      jsx('div', {
        key: 'bottom',
        className: 'hc-agg2__drawers',
        children: jsx(
          Drawer,
          {
            id: 'ops',
            title: '撤销 / 批次历史',
            badge: num(statusData.batches_total) + ' 批',
            hint: '最近写入批次，可逐批撤销并回读核对',
            children: jsx(OpsPanel, { onChanged: reloadAll })
          },
          'ops'
        )
      })
    ]
  })
}
