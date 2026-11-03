/**
 * hermes-console — 统一面板的 ESM 入口（唯一源码，两个产物同源）。
 *
 * 同一份源码经 `scripts/build.mjs` 产出两个文件：
 *   - `dashboard/dist/index.js` —— manifest 的 `entry`，dashboard tab 渲染的包；
 *   - `desktop/plugin.js`      —— Electron 半，由桌面壳拷到 `desktop-plugins/hermes-console/`。
 * 两者字节相同，所以不会出现"改了一边、面板不生效"的历史问题。
 *
 * 硬约束：
 * 1. 命名导入 `react` / `react/jsx-runtime`（运行时提供，构建时 external），无 TS 注解；
 * 2. 顶层可变量用 `var`（热重载会重复执行模块顶层，`const` 会抛重复声明）；
 * 3. 颜色只在 `style.css` 里以 `var(--ui-*)` 出现，本文件不写字面色值；
 * 4. 面板不重启任何进程、不写任何配置：本期只读 /agents。
 */
import { jsx } from 'react/jsx-runtime'

import { ConsoleApp } from './shell.js'
import { bindRest } from './lib/rest.js'
import { bindStorage } from './lib/storage.js'
import { injectConsoleStyles } from './lib/style-inject.js'

var ID = 'hermes-console'
var NAME = '控制台'
var TAB_PATH = '/console'

// 模块初始化就注入样式：桌面端加载门牌后先有 CSS 再挂载组件。
// 桌面端不读 manifest.css（只加载 plugin.js），这是唯一能让面板有样式的位置。
// 幂等：重复求值（热重载）不会堆叠 <style>。
injectConsoleStyles()

var plugin = {
  id: ID,
  name: NAME,
  register(ctx) {
    bindRest(ctx)
    bindStorage(ctx)
    // 二次保证：模块被求值时 document.head 未必可用（加载器时序），register 时一定在 DOM 里。
    injectConsoleStyles()
    ctx.registerMany([
      {
        id: 'page',
        area: 'routes',
        data: { path: TAB_PATH },
        render: () => jsx(ConsoleApp, {})
      },
      {
        id: 'nav',
        area: 'sidebar.nav',
        order: 60,
        data: { path: TAB_PATH, label: NAME, codicon: 'dashboard' }
      }
    ])
  }
}

export default plugin

// 探针入口：验收项「连续两次调用注入函数后仍只有 1 个 <style>」需要有可调用的函数，
// 只靠模块顶层的副作用无法二次调用。
export { injectConsoleStyles, STYLE_ATTR } from './lib/style-inject.js'
