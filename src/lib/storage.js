/**
 * hermes-console · 插件级偏好存储（ctx.storage 的唯一入口）。
 *
 * 宿主契约（apps/desktop/src/contrib/plugin.ts::PluginStorage）：
 *   `ctx.storage.get(key, fallback)` 同步读，缺省返回 fallback；
 *   `ctx.storage.set(key, value)` 同步写；
 *   `ctx.storage.remove(key)`。
 * 键被宿主命名空间化到 `hermes.plugin.hermes-console.<key>`，插件之间不会串。
 *
 * 这些调用**是同步的**，所以偏好可以在首次 render 之前读到 —— 选中的作用域
 * 不会先渲染成 default 再跳成上次的选择。老宿主没有 storage 时（契约里写明
 * 要 feature-detect）退化为内存值：本窗口内可用，重启后丢失。
 */

/** 热重载会重复执行模块顶层 —— 可变量一律 var。 */
var storage = null
var memory = {}

/**
 * 绑定 ctx.storage。由 `register(ctx)` 调用。
 * @param {{storage?: {get?: Function, set?: Function, remove?: Function}}} ctx
 */
export function bindStorage(ctx) {
  var candidate = ctx && ctx.storage
  storage = candidate && typeof candidate.get === 'function' ? candidate : null
}

/**
 * 读一个偏好。永远有返回值，永远不抛。
 * @param {string} key
 * @param {*} fallback
 */
export function readPref(key, fallback) {
  try {
    if (storage) {
      var value = storage.get(key, fallback)
      return value === undefined || value === null ? fallback : value
    }
  } catch (e) {
    // 受限上下文（禁用 localStorage）读当缺省：不让偏好失败拦住整个外壳
  }
  return Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : fallback
}

/**
 * 写一个偏好。写失败只丢偏好，不影响界面（不弹错、不阻塞）。
 * @param {string} key
 * @param {*} value
 */
export function writePref(key, value) {
  memory[key] = value
  try {
    if (storage) storage.set(key, value)
  } catch (e) {
    // 同上：偏好是便利功能，写不进去不能变成错误状态
  }
}
