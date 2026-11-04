/**
 * hermes-console · 请求缓存（React Query 的语义，不引依赖）。
 *
 * 为什么不是 `@tanstack/react-query`：宿主的插件 import 白名单只有
 * `react` / `react/jsx-runtime` / `@hermes/plugin-sdk`
 * （apps/desktop/src/sdk/runtime.ts::sdkImportMap），插件 bundle 里 import
 * `@tanstack/react-query` 会直接被加载器拒绝。所以这里用 120 行实现同样的
 * 三个关键语义，满足本卡的易用性要求：
 *   1. **切分区不重拉**：数据挂在模块级 Map 上，分区卸载/切回不丢；
 *   2. **作用域切换只让受影响区块进 skeleton**：key 里带 agent，没缓存的
 *      key 才是 loading，外壳与其它分区不受影响；
 *   3. **陈旧-but-有数据就先用着**：有缓存但过期时先渲染旧值再后台刷新，
 *      不会闪成骨架屏。
 *
 * 另加：同一 key 并发只发一次（inflight 去重）、busy 标记（刷新按钮用）。
 */
import { useEffect, useState } from 'react'

/** 缓存多久算新鲜（毫秒）。窗口期内重复挂载直接命中。 */
var STALE_MS = 20000

/** key -> {data} | {error, code} + at / fetcher / options */
var cache = new Map()
/** key -> 进行中的 Promise（去重，避免 StrictMode 双跑） */
var inflight = new Map()
/** key -> Set<() => void>（订阅者，缓存变化时重渲染） */
var listeners = new Map()

function subscribe(key, fn) {
  var set = listeners.get(key)
  if (!set) {
    set = new Set()
    listeners.set(key, set)
  }
  set.add(fn)
}

function unsubscribe(key, fn) {
  var set = listeners.get(key)
  if (!set) return
  set.delete(fn)
  if (!set.size) listeners.delete(key)
}

function emit(key) {
  var set = listeners.get(key)
  if (!set) return
  set.forEach(function (fn) {
    fn()
  })
}

function staleFor(options) {
  var ms = options && options.staleMs
  return typeof ms === 'number' ? ms : STALE_MS
}

/**
 * 取一次数据（有缓存且新鲜就直接返回）。fetcher 必须返回
 * `{ok:true, data}` 或 `{ok:false, message}` —— 即 lib/rest.js 的形状。
 */
function ensure(key, fetcher, options) {
  var now = Date.now()
  var entry = cache.get(key)
  var fresh = entry && !entry.error && now - entry.at < staleFor(options)
  if (fresh || inflight.has(key)) return
  var task = Promise.resolve()
    .then(fetcher)
    .then(function (result) {
      if (result && result.ok === false) {
        cache.set(key, {
          error: result.message || '请求失败',
          code: result.code || '',
          at: Date.now(),
          fetcher: fetcher,
          options: options
        })
        return
      }
      cache.set(key, {
        data: result ? result.data : null,
        at: Date.now(),
        fetcher: fetcher,
        options: options
      })
    })
    .catch(function (failure) {
      cache.set(key, {
        error: String((failure && failure.message) || failure || '请求失败'),
        code: 'throw',
        at: Date.now(),
        fetcher: fetcher,
        options: options
      })
    })
    .then(function () {
      inflight.delete(key)
      emit(key)
    })
  inflight.set(key, task)
  emit(key)
}

/**
 * 让某个 key 立刻重取（保留旧数据，不闪骨架）。
 * @param {string} key
 */
export function refresh(key) {
  var entry = cache.get(key)
  if (!entry) return
  entry.at = 0
  ensure(key, entry.fetcher, entry.options)
}

/**
 * 把一个前缀下的所有 key 标记过期并重取；`prefix` 省略 = 全部。
 * 顶部「刷新」按钮用它：外壳和已渲染分区都保留当前内容，后台换新。
 * @param {string} [prefix]
 */
export function refreshAll(prefix) {
  Array.from(cache.keys()).forEach(function (key) {
    if (prefix && key.indexOf(prefix) !== 0) return
    refresh(key)
  })
}

/**
 * 按**前缀**把缓存里**已经存在**的条目全部置陈并重取（`at = 0` + 立即 `ensure()`）。
 *
 * 和 `refreshAll(prefix)` 的差别看着只有一个，但决定了顶部「刷新」能不能覆盖设置页：
 * `refresh(key)` 对**不在缓存里**的 key 直接 return —— 而设置页的查询是「按选中 agent
 * 惰性建 key」（`settings-plugins|<agent>` / `settings-modules|<agent>` /
 * `settings-plugin|<agent>|<plugin>`，见 `sections/settings.js`），用户没点开那个 agent /
 * 插件时 key 根本不存在，`refreshAll` 于是**永远刷不到**它们。
 *
 * 这里只重置**已存在**的条目，绝不凭空造 key：造 key = 对 `enabled:false` 的查询强发
 * 请求（还没选 agent 时会直接 400）。尚未创建的 key 由 `useQuery` 的 `enabled` 变化
 * 自然重跑 —— 那是唯一正确的时机。
 *
 * `cache` 是模块级 Map（切分区不丢数据），本函数只原地改条目，不重建它。
 *
 * @param {string} prefix key 前缀（如 `'settings'`）；空串 = 什么都不做（要全刷请用 `refreshAll()`）
 */
export function invalidatePrefix(prefix) {
  var head = String(prefix || '')
  if (!head) return
  Array.from(cache.keys()).forEach(function (key) {
    if (key.indexOf(head) !== 0) return
    var entry = cache.get(key)
    if (!entry) return
    entry.at = 0
    ensure(key, entry.fetcher, entry.options)
  })
}

/**
 * 读缓存快照（非 React 上下文用；也方便探针断言）。
 * @param {string} key
 */
export function peek(key) {
  return cache.get(key) || null
}

/** 清空全部缓存（探查/测试用；正常 UI 不该调用）。 */
export function resetCache() {
  cache.clear()
  inflight.clear()
}

/**
 * 订阅一个 key。
 * @param {string} key 必须是稳定字符串（内部参与缓存键）
 * @param {() => {ok: boolean, data?: any, message?: string}} fetcher
 * @param {{staleMs?: number, enabled?: boolean}} [options] `enabled:false` 时不发请求，
 *   状态保持 loading（用于等作用域确定后再取数，避免先用空作用域取一遍再重取）。
 * @returns {{status: 'loading'|'ok'|'error', data: any, error: string, busy: boolean, reload: Function}}
 */
export function useQuery(key, fetcher, options) {
  var enabled = !(options && options.enabled === false)
  // tick 只用来在缓存变化时逼一次重渲染，值本身不参与逻辑
  var tickState = useState(0)
  var setTick = tickState[1]

  var entry = cache.get(key)

  useEffect(
    function () {
      var bump = function () {
        setTick(function (n) {
          return n + 1
        })
      }
      subscribe(key, bump)
      return function () {
        unsubscribe(key, bump)
      }
    },
    [key]
  )

  useEffect(
    function () {
      if (!enabled) return
      ensure(key, fetcher, options)
    },
    [key, enabled]
  )

  var status = 'loading'
  var data = null
  var error = ''
  if (entry) {
    if (entry.error) {
      status = 'error'
      error = entry.error
    } else {
      status = 'ok'
      data = entry.data
    }
  }

  return {
    status: status,
    data: data,
    error: error,
    busy: inflight.has(key),
    reload: function () {
      refresh(key)
    }
  }
}
