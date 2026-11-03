/**
 * hermes-console · 样式自注入（桌面端唯一能让 CSS 生效的路径）
 *
 * 为什么需要它：桌面端插件运行时只加载门牌 `plugin.js`，**不读 `manifest.json`
 * 的 `css` 键**（hermes-remote / model-switch / upgrade-console / vcc-console 四个
 * 在桌面端显示正常的面板，manifest 里 `css` 全是 None，它们靠 React 内联 style）。
 * 外链样式表在桌面侧永远到不了页面，所以样式必须以字符串进 bundle，
 * 由模块自己在 `document.head` 里插一个 `<style>`。
 *
 * 三条规矩：
 *   1. **幂等**：页面上永远只有一份 `style[data-hermes-console-style]`。
 *      插件热重载会重新求值模块顶层，若只是 append，样式会一份份堆起来（内存与层叠都失控）。
 *   2. **内容变了就替换**：同一份 bundle 重复调用是零成本；换了内容（重新构建）才写回。
 *   3. **@import 就地剥掉**：text loader 是原样读文件，`@import "./tokens.css";` 留在字符串里
 *      会被浏览器按**当前页面 URL** 解析（`<style>` 里的 @import 没有所属文件概念），必然 404。
 *      所以 tokens 单独 import 后按源码顺序拼在前面 —— 效果与 CSS 的 @import 相同。
 *
 * 尺寸 token 展开成字面量（`var(--s4)` → `16px`）：
 *   - 值仍然取自 `tokens.css`（这里只是把 `--name: value` 读出来再替换，没有第二份常量表）；
 *   - `var(--ui-*)` 一律**不动** —— 那是宿主主题变量，要留给宿主在深浅色之间改写；
 *   - 理由：`getComputedStyle` 在 jsdom 下完全不解析 `var()`（实测 `var(--r-md)` 与
 *     `var(--r-md, 10px)` 都原样返回），而「注入后统计卡的 borderRadius 真的是 10px」
 *     正是本卡的验收断言。展开后浏览器与 jsdom 算出的值一致，断言才是真证据。
 */
import styleText from '../style.css'
import tokensText from '../tokens.css'

/** 标记属性：既是幂等键，也是验收 grep 的锚点（别改成别的名字）。 */
export var STYLE_ATTR = 'data-hermes-console-style'

/** 单行 @import（本项目只用这种形式；多行 @import 没有出现过，出现会在这里暴露）。 */
var AT_IMPORT = /@import[^;]*;/g

/** `--name: value;` 定义（只从 tokens.css 取；键保留前导 `--`，与 TOKEN_USE 的捕获保持同一形状）。 */
var TOKEN_DEF = /(-{2}[A-Za-z0-9_-]+)\s*:\s*([^;{}]+);/g

/** `var(--name)`：不留逗号，所以 `var(--ui-accent, currentColor)` 这种带兜底的引用天然不匹配。 */
var TOKEN_USE = /var\((--[A-Za-z0-9_-]+)\)/g

/** 拼好的样式文本（模块级缓存：热重载重新求值时本来就会重新算）。 */
var bundled = ''

/** 读出 tokens.css 里定义的尺寸 token。@param {string} text @returns {Record<string,string>} */
export function readTokens(text) {
  var values = {}
  var m
  TOKEN_DEF.lastIndex = 0
  while ((m = TOKEN_DEF.exec(text)) !== null) values[m[1]] = m[2].trim()
  return values
}

/** 把 `var(--token)` 换成 tokens.css 里的字面值；未定义的（含全部 `--ui-*`）原样保留。 */
export function expandTokens(text, values) {
  return text.replace(TOKEN_USE, function (whole, name) {
    var value = values[name]
    return value === undefined ? whole : value
  })
}

/**
 * 拼出注入用 CSS 文本：tokens 在前，style.css 在后，@import 已剥、尺寸 token 已展开。
 * @returns {string}
 */
export function consoleStyleText() {
  if (!bundled) {
    var values = readTokens(tokensText)
    bundled =
      tokensText +
      '\n' +
      // 替换文本里**不能出现 @import 字样**（哪怕是注释）：注释里的同一个字符串会让
      // 「产物里没有外链导入」这条验收 grep 变成假阳性。
      expandTokens(styleText.replace(AT_IMPORT, '/* tokens.css 已前置合并，外链导入已剥离 */'), values)
  }
  return bundled
}

/**
 * 幂等注入样式表。重复调用只会得到同一个 `<style>` 节点。
 *
 * @param {string} [css] 覆盖文本（探针/调试用；默认是 bundle 里的样式）
 * @returns {HTMLStyleElement | null} 注入的节点；没有 DOM（SSR/纯 node）时返回 null
 */
export function injectConsoleStyles(css) {
  if (typeof document === 'undefined') return null
  var head = document.head
  if (!head || typeof document.createElement !== 'function') return null

  var text = typeof css === 'string' && css ? css : consoleStyleText()
  var existing = document.querySelectorAll('style[' + STYLE_ATTR + ']')
  var node = existing.length ? existing[0] : null

  // 历史残留（上一次加载的旧 bundle / 双份 entry）：只留第一份，其余摘掉。
  for (var i = 1; i < existing.length; i += 1) {
    if (existing[i].parentNode) existing[i].parentNode.removeChild(existing[i])
  }

  if (node) {
    // 内容一致就不碰 DOM（热重载反复求值时不产生无谓的重排）
    if (node.textContent !== text) node.textContent = text
    return node
  }

  node = document.createElement('style')
  node.setAttribute(STYLE_ATTR, '')
  node.textContent = text
  head.appendChild(node)
  return node
}
