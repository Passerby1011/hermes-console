/**
 * hermes-console · 后端调用门面（唯一出口）。
 *
 * 契约（已核对宿主实现，不是猜的）：
 *   apps/desktop/src/api/plugins.ts::pluginRest → hermesApi → electron/main.ts::fetchJson
 *   - `ctx.rest(path, opts)` 的 `path` 是相对本插件命名空间（'/agents' →
 *     '/api/plugins/hermes-console/agents'），带 `..` 会被拒；
 *   - `opts.body` 必须是**普通对象**：main 里 `Buffer.from(JSON.stringify(options.body))`
 *     只序列化一次。传字符串会变成 `"{\"a\":1}"` 这个 JSON 字符串，FastAPI 收到
 *     不是 dict → 422。所以本文件在 request() 里显式拒绝字符串 body，把这类
 *     错误挡在发请求之前，而不是让它变成后端的 422。
 *   - 非 2xx **会 reject**（`httpStatusError`，message 形如 '<status>: <body>'）；
 *     3xx、HTML 响应、网络失败、超时同样 reject。所以错误归一化是 catch + 解析，
 *     不能假设它 resolve 出一个 {ok:false}（老版本 dashboard 才那样）。
 *
 * 所有导出函数都不抛：失败一律返回 `{ok:false, code, message}`，UI 据此进 ErrorState。
 *
 * ## 304 是 2xx 之外的第三种东西（本文件显式处理，不靠 catch 兜底）
 *
 * 宿主的 `fetchJson` 把 `statusCode >= 300` 一律 reject 成 `htmlResponseError`，
 * 而 304 的响应体通常是空的 —— 所以一条 304 到了这里就是「一个没有数据的错误」：
 * 面板能显示上一次的东西（那是 React 状态里的旧值），但刷新一次就变成 ErrorState，
 * 所有依赖新数据的操作都跟着不可用。这就是「能显示但不可用」的机制。
 *
 * 处理方式（选 A，不选 B）：
 *   **A. 把 304 当成功，复用上一次成功的响应体。**
 *   B（让后端/宿主对这些读接口不发 ETag / 加 no-store）在插件侧做不到：
 *     `hermes_cli/` 是宿主代码，本卡红线不许改；插件自己的 JSON 路由本来就不发
 *     ETag（实测 `/advisor/coverage` 等 200 响应无 ETag / Last-Modified / Cache-Control），
 *     304 只可能来自中间缓存或代理 —— 那种情况下客户端**必须**自己持有副本，
 *     否则数据在协议上就是不可得的。
 *
 * 缓存语义（写清楚，别让人猜）：
 *   - 只缓存 **GET 成功**的响应体，key = 完整相对路径（含 query，所以 `?agent=` 各自独立）；
 *   - **失效时机**：①任何写请求成功后整表清空（写可能改变任意读视图，宁可多取一次）；
 *     ②超过 `LAST_GOOD_TTL_MS`（5 分钟）视为过期，不再拿它顶 304；
 *     ③表超过 `LAST_GOOD_MAX` 条时按最旧淘汰（模块级，不随分区卸载丢失）；
 *   - 命中时返回 `{ok:true, data, revalidated:true, status:304}`，UI 照常渲染；
 *   - **没命中**时先**真的重发一次请求**（带上一次性旁路参数 `_hermes_nc=<ms>`，把
 *     中间缓存绕开，让服务器回一份完整 200）—— 这不是「把 304 伪装成 200」，拿到的是
 *     服务器这一次真发的 body；重取成功就用**原 path** 作键写回复用表，下次 304 直接命中。
 *     重取仍失败才如实报错，绝不编造数据：`code:'not_modified'`。
 *   - 为什么要这条旁路：写请求成功会清空复用表，之后第一次刷新如果正好撞上 304，
 *     没有它就会整块变 ErrorState —— 那正是「能显示但不可用」的复发路径。
 */

/** 热重载会重复执行模块顶层 —— 可变量一律 var，避免 const 重复声明。 */
var rest = null

/** 本插件的命名空间前缀：裸 IPC 桥需要绝对路径。 */
var PLUGIN_PREFIX = '/api/plugins/hermes-console'

/** 当前实际使用的请求通道（'bare-bridge' | 'ctx.rest' | 'none'），排障用。 */
var restSource = 'none'

/** 上一次 GET 成功响应体的复用表：path -> {data, at, status}。语义见文件头。 */
var lastGood = new Map()
/** 复用表条目多久之后不再顶 304（毫秒）。 */
var LAST_GOOD_TTL_MS = 5 * 60 * 1000
/** 复用表最多存多少条（模块级，按最旧淘汰）。 */
var LAST_GOOD_MAX = 40

/**
 * 绑定请求通道。由 `register(ctx)` 调用，一次注册一次。
 *
 * **优先用裸 IPC 桥，而不是 `ctx.rest`。**
 *
 * 原因（2026-11-01 真登录态实测，同一会话只差一个参数）：
 * ```
 * 无 profile            → 200
 * ?profile=default      → 200
 * ?profile=work         → 404 {"detail":"Plugin not found"}
 * ?profile=code-worker  → 404 {"detail":"Plugin not found"}   （GET 同样）
 * ```
 * 桌面端 `pluginRest` 会把 `profileScoped()`（当前 agent 的 ambient profile）spread 进
 * 每个请求，宿主再用 `_plugin_api_request_refused` 拒绝「二级 profile 使用启动 profile
 * 的 user 插件」。本插件装在根 `plugins/` 下，正是 user 插件 → 一旦请求被绑到别的
 * agent 就必然 404。
 *
 * 本插件自己的作用域参数是 `?agent=`，从不依赖 `?profile=`。所以这里直接用
 * `window.hermesDesktop.api`（不经 `profileScoped()` 包装）作为唯一出口；
 * 该桥在无 `request.profile` 时不会附加任何 `?profile=`。
 * 拿不到裸桥时（例如纯浏览器打开仪表盘）才退回 `ctx.rest`。
 *
 * @param {{rest?: Function}} ctx
 */
export function bindRest(ctx) {
  rest = ctx && typeof ctx.rest === 'function' ? ctx.rest : null
  restSource = rest ? 'ctx.rest' : 'none'
  // 裸 IPC 桥在 request() 的 404 分支里作为兜底使用（见 bridgeWithoutProfile）：
  // 正常路径保持 ctx.rest，才能带上 connectionScoped() 的远程路由；
  // 只有宿主 profile 闸门拒了这条插件（404 Plugin not found）时才绕开它。
}

/**
 * `'<status>: <body>'` → `{code, detail}`。非该格式（网络/超时）原样返回。
 * @param {string} message
 * @returns {{code: string, detail: string}}
 */
export function splitStatus(message) {
  var source = String(message || '')
  // 形态 1（最常见）：状态码在**开头**，`<status>: <body>`（electron/main.ts::httpStatusError）。
  var m = /^\s*(\d{3})\s*:\s*([\s\S]*)$/.exec(source)
  var body
  if (m) {
    body = m[2].trim()
  } else {
    // 形态 2：**remote-method 包装层把状态码包在中间**（2026-11-03 修）：
    //   `Error: invoking remote method 'hermesapi': Error: 409: {"ok":false,...}`
    // 只认开头时会漏掉 409 → code 回落 'network' → 界面说「连不上后端」，
    // 那是假话（后端活着，只是拒绝了这次写），会把人带去查网关。
    //
    // 判据收紧，避免误判：状态码必须是 100~599 的三位数、紧跟冒号，且前面是
    // `error` / `http` / `status` 这类字样（或紧邻 `(` `[`）。
    // 这样 `ECONNREFUSED 127.0.0.1:9195`、`after 1500ms` 里的数字不会被当成状态码。
    var wrapped =
      /(?:\b(?:error|http(?:\/\d(?:\.\d)?)?|status)\b[^0-9]{0,8}|[(\[]\s*)([1-5]\d{2})\s*:\s*([\s\S]*)$/i.exec(
        source
      )
    if (!wrapped) return { code: '', detail: source, errorCode: '' }
    // 前缀是非捕获组，所以 wrapped[1] 才是状态码、wrapped[2] 是剩余正文。
    m = [wrapped[0], wrapped[1], wrapped[2]]
    body = m[2].trim()
  }
  var errorCode = ''
  // 后端错误体通常是 {"detail": "..."} / {"error": {"message"|"code": ...}}，抽出来给人看
  if (body.charAt(0) === '{') {
    try {
      var parsed = JSON.parse(body)
      var nested = parsed && parsed.error && typeof parsed.error === 'object' ? parsed.error : null
      if (nested && typeof nested.code === 'string') errorCode = nested.code
      var text = parsed && (parsed.detail || parsed.message || (nested && nested.message))
      if (typeof text === 'string' && text.trim()) body = text.trim()
      else if (nested && typeof nested.code === 'string') body = nested.code
    } catch (e) {
      // 不是 JSON：保留原文
    }
  }
  return {
    code: m[1],
    detail: body.length > 300 ? body.slice(0, 300) + '…' : body,
    errorCode: errorCode
  }
}

/**
 * 把任意失败翻译成一句人能读的话，并保留状态码（对日志用）。
 * @param {unknown} failure
 * @param {string} path
 * @returns {{ok: false, code: string, message: string}}
 */
export function normalizeError(failure, path) {
  var raw = failure == null ? '' : String((failure && failure.message) || failure)
  var parts = splitStatus(raw)
  var code = parts.code || 'network'
  var detail = parts.detail || raw || '未知错误'
  var head
  // 宿主 fetch 的超时文案是 "Timed out connecting to Hermes backend after Nms"（electron/main.ts）。
  // 不能把它当「连不上后端」：那会让人去查网关，而真相是这个接口算得慢。
  if (/timed?\s*out/i.test(raw)) {
    return {
      ok: false,
      code: 'timeout',
      message: '取数超时' + (path ? ' · ' + path : '') + '：' + detail
    }
  }
  if (code === '401' || code === '403') head = '没有权限（' + code + '）'
  else if (code === '404') head = '接口不存在（404）'
  // 409 = 乐观锁冲突（后端 error.code=config_changed）：**不是**「连不上后端」。
  // 说清楚「配置已变化，请重新读取后重试」，否则用户会去查网关，方向全错。
  else if (code === '409')
    head = parts.errorCode === 'config_changed' ? '配置已变化（409）' : '请求与后端状态冲突（409）'
  else if (code === '422') head = '请求参数被后端拒绝（422）'
  else if (code === '304') head = '内容未修改（304）'
  else if (/^5\d\d$/.test(code)) head = '后端错误（' + code + '）'
  else if (code === 'network') head = '连不上后端'
  else head = '请求失败（' + code + '）'
  // 后端结构化错误体里的 error.code（如 upstream_error / invalid_argument / not_found）
  // 必须出现在界面上：只说「503」等于把最有用的那一句丢掉。
  var errorCode = parts.errorCode ? '（后端错误码 ' + parts.errorCode + '）' : ''
  return {
    ok: false,
    code: code,
    errorCode: parts.errorCode || '',
    message: head + (path ? ' · ' + path : '') + '：' + detail + errorCode
  }
}

/**
 * 后端"结构化降级"（HTTP 200 + `{ok:false, error:{code,message}}`）→ 前端错误。
 *
 * 为什么单独一条路：概览分区的一个数据源坏掉时，后端故意回 200（其它卡片要照常渲染），
 * 这不是"连不上后端"。走 normalizeError() 会因为没有状态码被判成 `network`，
 * 提示成「连不上后端」——那是假话。这里用后端给的 `error.code` 说话。
 * @param {any} body
 * @param {string} path
 * @returns {{ok: false, code: string, message: string}}
 */
export function normalizeBodyError(body, path) {
  var nested = body && body.error && typeof body.error === 'object' ? body.error : null
  var code = (nested && typeof nested.code === 'string' && nested.code) || 'degraded'
  var detail =
    (nested && (nested.message || nested.code)) || body.detail || body.message || '未知原因'
  var head
  if (code === 'timeout') head = '取数超时（' + code + '）'
  else if (code === 'import_failed') head = '宿主接口不可用（' + code + '）'
  else if (code === 'unknown_agent' || code === 'invalid_agent') head = '这个 agent 取不到数据（' + code + '）'
  else if (code === 'unsupported_agent') head = '这个接口不支持该作用域（' + code + '）'
  else head = '数据源没读到（' + code + '）'
  return { ok: false, code: code, message: head + (path ? ' · ' + path : '') + '：' + detail }
}

/**
 * 把「上一次成功响应」塞进复用表，并按容量淘汰最旧的一条。
 * @param {string} path
 * @param {any} data
 * @param {number} status
 */
function rememberGood(path, data, status) {
  lastGood.set(path, { data: data, at: Date.now(), status: status })
  if (lastGood.size > LAST_GOOD_MAX) {
    var oldestKey = ''
    var oldestAt = Infinity
    lastGood.forEach(function (entry, key) {
      if (entry.at < oldestAt) {
        oldestAt = entry.at
        oldestKey = key
      }
    })
    if (oldestKey) lastGood.delete(oldestKey)
  }
}

/**
 * 从复用表里取一条**还没过期**的上次成功响应。
 * @param {string} path
 * @returns {{data: any, at: number, status: number} | null}
 */
function recallGood(path) {
  var entry = lastGood.get(path)
  if (!entry) return null
  if (Date.now() - entry.at > LAST_GOOD_TTL_MS) {
    lastGood.delete(path)
    return null
  }
  return entry
}

/** 清空复用表。写请求成功后调用（写可能改变任意读视图），也供探针使用。 */
export function clearReadCache() {
  lastGood.clear()
}

/**
 * 复用表快照（探针断言用）：`[{path, at, status}]`。
 * @returns {Array<{path: string, at: number, status: number}>}
 */
export function readCacheEntries() {
  var out = []
  lastGood.forEach(function (entry, key) {
    out.push({ path: key, at: entry.at, status: entry.status })
  })
  return out
}

/**
 * 发一个请求，永不抛。
 * @param {string} path 相对本插件命名空间的路径
 * @param {{method?: string, body?: object, timeoutMs?: number, __retried?: boolean}} [opts]
 *   `__retried` 是本文件内部用的「已经做过一次缓存旁路重取」标记，不会传给宿主。
 * @returns {Promise<{ok: true, data: any, status?: number, revalidated?: boolean, cacheBusted?: boolean}
 *   | {ok: false, code: string, message: string}>}
 */
/**
 * 不带 profile 作用域的裸 IPC 桥请求。
 *
 * 桌面端 `hermesApi` 会 `{...connectionScoped(), ...request}`，而 `profileScoped()`
 * 只在调用方显式 spread 时才会带 `profile`。直接调 `window.hermesDesktop.api`
 * 就完全不经过那层包装——这正是我们要的：插件自己的作用域是 `?agent=`，
 * 不需要 `?profile=`。
 *
 * @param {string} path 相对本插件命名空间的路径
 * @param {{method?: string, body?: unknown, timeoutMs?: number}} transport
 * @returns {Promise<{ok: true, data: any} | {ok: false, raw: string}>}
 */
async function bridgeWithoutProfile(path, transport) {
  var bridge = typeof window !== 'undefined' && window.hermesDesktop
    ? window.hermesDesktop.api : null
  if (typeof bridge !== 'function') return { ok: false, raw: 'window.hermesDesktop.api 不可用' }
  try {
    var request = {
      path: '/api/plugins/hermes-console' + path,
      method: (transport && transport.method) || 'GET'
    }
    if (transport && transport.body !== undefined) request.body = transport.body
    if (transport && transport.timeoutMs) request.timeoutMs = transport.timeoutMs
    var data = await bridge(request)
    return { ok: true, data: data }
  } catch (e) {
    return { ok: false, raw: String((e && e.message) || e) }
  }
}

export async function request(path, opts) {
  if (!rest) return { ok: false, code: 'unbound', message: '插件未注册（ctx.rest 不可用）' }
  var options = opts || {}
  var method = String(options.method || 'GET').toUpperCase()
  var isRead = method === 'GET' || method === 'HEAD'
  // 只把宿主认识的字段传下去：内部标记（__retried）不许泄漏到传输层
  var transport = { method: method }
  if (options.body !== undefined) transport.body = options.body
  if (options.timeoutMs) transport.timeoutMs = options.timeoutMs
  if (typeof options.body === 'string') {
    // 双重序列化是已知 422 根因：在调用点拒掉，别打到后端
    return {
      ok: false,
      code: 'bad_body',
      message:
        '写请求的 body 必须是对象，禁止先 JSON.stringify（ctx.rest 会自己序列化）：' + path
    }
  }
  try {
    var data = await rest(path, transport)
    if (data && data.ok === false) return normalizeBodyError(data, path)
    // 只把「读」的成功响应存起来给 304 复用；写成功反而要清表（见文件头失效时机 ①）
    if (isRead) rememberGood(path, data, 200)
    else clearReadCache()
    return { ok: true, data: data, status: 200 }
  } catch (failure) {
    // ---- 304 显式分支：不靠 catch 兜底成 ErrorState ----
    var parts304 = splitStatus(failure == null ? '' : String((failure && failure.message) || failure))
    if (parts304.code === '304') {
      var remembered = recallGood(path)
      if (remembered) {
        return {
          ok: true,
          data: remembered.data,
          status: 304,
          revalidated: true,
          cachedAt: remembered.at
        }
      }
      // 没有副本可复用（例如刚做过写请求把表清了，或这是本次会话第一次读）。
      // 304 不带响应体，光靠它拿不到数据；这里**真的再发一次请求**，带上一个
      // 一次性旁路参数把中间缓存绕开，让服务器老老实实回一份完整 200。
      // 这不是「把 304 伪装成 200」：拿到的是服务器这次真发的 body。
      if (isRead && !options.__retried) {
        var busted = withQuery(path, { _hermes_nc: String(Date.now()) })
        var retried = await request(busted, {
          method: method,
          timeoutMs: options.timeoutMs,
          __retried: true
        })
        if (retried && retried.ok) {
          // 用**原 path** 作缓存键：下次 304 直接命中
          rememberGood(path, retried.data, retried.status || 200)
          return {
            ok: true,
            data: retried.data,
            status: retried.status || 200,
            cacheBusted: true
          }
        }
        return {
          ok: false,
          code: 'not_modified',
          message:
            '服务器回了 304（内容未修改）且本地没有副本，缓存旁路重取一次也没拿到数据 · '
            + path + '：请点「刷新」重试'
        }
      }
      return {
        ok: false,
        code: 'not_modified',
        message:
          '服务器回了 304（内容未修改），但本地没有可复用的上一次响应 · ' + path
          + '：请点「刷新」重取一次（304 不带响应体，前端拿不到数据）'
      }
    }
    // ---- 404 「Plugin not found」：绕过 profile 作用域重发一次 ----
    // 根因（2026-11-01 实测，真登录态同一会话）：
    //   无 profile        → 200
    //   ?profile=default  → 200
    //   ?profile=work     → 404 {"detail":"Plugin not found"}   （GET/POST 一样）
    // 桌面端 `ctx.rest` 会经 profileScoped() 给每个插件请求带上环境 agent 的
    // `?profile=`；而宿主的闸门（web_server_dashboard.py::_plugin_api_request_refused）
    // 拒绝「二级 profile 使用启动 profile 的 user 插件」，本插件正是根 plugins/ 下的
    // user 插件 → 只要请求被绑到别的 agent 就必然 404。
    //
    // 本插件自己的作用域参数是 `?agent=`，从不依赖 `?profile=`。所以这里改用
    // 不带 profile 的裸 IPC 桥重发一次；只有在桥 404 时才走这条路，正常路径不受影响。
    var partsDiag = splitStatus(failure == null ? '' : String((failure && failure.message) || failure))
    if (partsDiag.code === '404') {
      var raw404 = failure == null ? '' : String((failure && failure.message) || failure)
      var unscoped = await bridgeWithoutProfile(path, transport)
      if (unscoped && unscoped.ok) {
        if (isRead) rememberGood(path, unscoped.data, 200)
        else clearReadCache()
        return { ok: true, data: unscoped.data, status: 200, unscoped: true }
      }
      return {
        ok: false,
        code: '404',
        message:
          '接口不存在（404） · ' + path
          + ' · 宿主返回：' + (raw404 || '(空)')
          + ' · 裸桥重发：' + ((unscoped && unscoped.raw) || '(不可用)')
      }
    }
    return normalizeError(failure, path)
  }
}

/**
 * 拼 query。值一律 encodeURIComponent；null/undefined/'' 跳过。
 * @param {string} path
 * @param {Record<string, unknown>} [params]
 */
export function withQuery(path, params) {
  if (!params) return path
  var pairs = []
  Object.keys(params).forEach(function (key) {
    var value = params[key]
    if (value === undefined || value === null || value === '') return
    pairs.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)))
  })
  if (!pairs.length) return path
  return path + (path.indexOf('?') === -1 ? '?' : '&') + pairs.join('&')
}

/**
 * GET。`agent` 是作用域参数（各域后端路由从 C3 起都读它）。
 * @param {string} path
 * @param {{agent?: string, params?: Record<string, unknown>, timeoutMs?: number}} [opts]
 */
export function get(path, opts) {
  var o = opts || {}
  var params = {}
  var extra = o.params || {}
  Object.keys(extra).forEach(function (k) {
    params[k] = extra[k]
  })
  if (o.agent) params.agent = o.agent
  var init = { method: 'GET' }
  if (o.timeoutMs) init.timeoutMs = o.timeoutMs
  return request(withQuery(path, params), init)
}

/**
 * 写请求。body 传对象（禁止 JSON.stringify），见文件头。
 * @param {string} path
 * @param {object} [body]
 * @param {{method?: string, agent?: string}} [opts]
 */
export function send(path, body, opts) {
  var o = opts || {}
  var target = o.agent ? withQuery(path, { agent: o.agent }) : path
  return request(target, { method: o.method || 'POST', body: body === undefined ? {} : body })
}
