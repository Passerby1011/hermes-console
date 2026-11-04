# 控制台前端 4 条缺陷修复（2026-11-04）

基线：`eaca7d2`（worktree `wt/t_c8c4c9c0-codex`）。本轮只改 `src/`，未触碰
`dashboard/**` / `scripts/build.mjs` / `README.md` / `STATUS.md` / `.gitignore`。

---

## F-01 顶部「刷新」对设置页无效

**改了什么**

- `src/lib/query.js:136-159`：新增 `export function invalidatePrefix(prefix)`。
  遍历模块级 `cache`（`Map`，未重建），凡 key 以 `prefix` 开头的**已存在**条目
  `at = 0` 后原样调 `ensure(key, entry.fetcher, entry.options)` 重取。
  空前缀直接返回 —— 「全部重取」的语义仍由 `refreshAll()` 承担，避免一个空串参数
  意外变成全量副作用。
- `src/shell.js:32`：import 增加 `invalidatePrefix`。
- `src/shell.js:406-427`：`refresh()` 改为 `refreshAll()` **加**
  `invalidatePrefix('settings')`。注释里写明了为什么选这条而不是「对当前分区调它自己的
  reload」：分区由 `active.Component` 渲染，外壳没有 reload 句柄；为这一个按钮给 11 个
  分区加命令式接口、还要自己管 ref 生命周期，改动面更大，且新分区漏接时会再次静默失效。
  按 key 前缀失效与分区实现解耦。
- `src/shell.js:561`：按钮 `title` 从「重新读取已打开分区的数据」改为
  「重新读取已打开分区的数据（含设置页已加载的插件清单 / 配置；保留当前内容，不闪屏）」，
  与真实覆盖面一致。按钮可见文案（`刷新` / `已刷新`）不变。

**为什么**

设置页的 key 是惰性建的（`settings-plugins|<agent>` / `settings-modules|<agent>` /
`settings-plugin|<agent>|<plugin>`，`src/sections/settings.js:381-442`），而
`refresh(key)` 对缓存里不存在的 key 直接 `return`（`query.js:108-113`）。`refreshAll()`
只遍历缓存已有 key，所以「还没加载过的设置页查询」永远刷不到。

**关于 `enabled:false`**：`invalidatePrefix` 只重置**已存在**的条目，绝不凭空造 key；
尚未创建的 key 仍由 `useQuery` 的 `enabled` 变化自然重跑。因此没有选 agent 时点刷新
不会发任何 `/agents//...` 请求（不会 400）。

---

## F-03 autotitler 文案承诺的 `GET /models/validate` 现在真的会发

选**方案 A（补实现）**。

**改了什么**

- `src/lib/provider-model-picker.js:144-150`：新增 `validateState`
  （`phase: idle | checking | ok | error`）。
- `src/lib/provider-model-picker.js:278-288`：手填输入变化时把上一次校验结论置
  `idle`（旧结论不能贴在一个已经不同的名字上）。
- `src/lib/provider-model-picker.js:302-334`：`applyAdvanced`（「用这个名字」）现在先
  调 `get('/models/validate', { params: { agent, provider, model, allow_unlisted: '1' } })`：
  - `ok === false` ⇒ 把后端 `message` 原文写入 `validateState`，**不放行**（不 emit），
    不会再静默把非法清单外名字交给保存流程；
  - `ok === true` ⇒ 清 `advancedDirty`、正常 `emit(provider, model, true)`；
  - 请求仍是**只读**，后端 `dashboard/api_models.py::validate` 不写任何配置。
  - 带 `allow_unlisted=1` 是为了让校验口径与调用方保存时必然使用的
    `allow_unlisted_model: true` 一致（后端 `_model_value` 的第 4 条规则）；不带它，
    清单外的名字会被 `value_rejected` 拒掉，「高级：手填」这个入口就整个失效了。
- `src/lib/provider-model-picker.js:836-844`：按钮校验期间显示「校验中…」并禁用，
  避免重复点触发并发请求。
- `src/lib/provider-model-picker.js:857-876`：新增 `data-pmp-validate*` 状态块，
  把「校验中 / 校验通过 / 校验没过 + 后端原文」显示在选择器下方（失败态 `role="alert"`、
  `--ui-danger`）。
- `src/lib/provider-model-picker.js:852-855`：默认 `advancedNote` 同步说明
  「点『用这个名字』前会先调只读校验」。
- `src/sections/autotitler.js:562-564`：可见文案改为
  「清单外的名字在点『用这个名字』时会先经 GET /models/validate 只读复核，校验没过会把
  原因显示在选择器下方、不放行；通过后再由后端记为未校验。」——承诺与实现一致。

---

## F-04 `.hc-row` 基类写死 4 列

**改了什么**

- `src/style.css:725-776`
  - `.hc-row` 基类**移除** `display: grid; grid-template-columns: minmax(0,1fr) auto auto auto;`，
    只保留外观（`align-items` / `gap` / `min-height` / `padding` / `font-size`）。
  - 新增 `.hc-rows .hc-row` 承载默认四列网格。
  - `.hc-rows--cron .hc-row` 不变（继续用 `var(--row-cols)`）。
  - 新增 `.hc-rows--selfcheck`（`--row-cols: minmax(0,96px) 88px minmax(0,1fr)`）以及
    `.hc-rows--selfcheck .hc-row` / `.hc-rows--selfcheck .hc-rows__head` 的列定义。
  - 新增 `.hc-rows--stack .hc-row`：`display:flex; flex-direction:column; align-items:stretch`。
- `src/style.css:1089-1099`：窄档 3 列覆盖从 `.hc-row` 改成 `.hc-rows .hc-row`；
  并补一条同权重、源码在后的 `.hc-rows--cron .hc-row { grid-template-columns: var(--row-cols) }`，
  让定时任务在窄档仍吃 116px/68px 的定宽列，不被通用三列盖掉。
- `src/sections/models.js`
  - `2519`：写入结果容器 `hc-rows` → `hc-rows hc-rows--stack`
    （原本 2525 的 `.hc-row` 只有一整块内容）。
  - `3701`：批量回滚结果容器 → `hc-rows hc-rows--stack`。
  - `2387`：预览列表 → `hc-rows--stack`（同一类消费者，一并归位）。
  - `3750`：批次账本 → `hc-rows--stack`（同一类消费者）。
- `src/sections/overview.js:1352-1356`：自检表从「借用 `hc-rows--cron` + 行内
  `--row-cols`」改为专用 `hc-rows hc-rows--selfcheck`，3 列定义落回样式表。

**F-04 要求的 `grep -rn "hc-row" src/` 完整结果与逐条判断**

```
src/sections/models.js
287    className: on ? 'hc-row hc-models__row hc-models__row--on' : 'hc-row hc-models__row',
2387   className: 'hc-rows hc-rows--stack hc-models__pv-list',
2390   className: 'hc-row',
2518   // 不能吃 `.hc-rows .hc-row` 的 4 列网格（F-04）。
2519   className: 'hc-rows hc-rows--stack',
2525   className: 'hc-row',
3200   className: 'hc-rows hc-models__moa-list',
3210   className: 'hc-row',
3397   className: 'hc-row',
3434   className: 'hc-row',
3701   className: 'hc-rows hc-rows--stack',
3705   className: 'hc-row',
3750   className: 'hc-rows hc-rows--stack',
3755   className: 'hc-row',
3870   className: 'hc-rows',
4222   className: 'hc-rows',
4647   className: 'hc-rows',

src/sections/overview.js
1056   // 表头与数据行都是 .hc-rows 的直接子节点 —— 这样 `--row-cols` 与 `行+行` 的
1061   className: 'hc-rows__head',
1068   jsx('span', { className: 'hc-row__scope', children: '作用域' }, 'h-scope')
1083   className: 'hc-row',
1088   { className: 'hc-row__name', children: job.name || job.id || '(未命名任务)' },
1094   className: 'hc-row__next',
1102   className: 'hc-row__next',
1122   jsx('span', { className: 'hc-row__scope', children: job.profile || '' }, 'profile')
1129   className: 'hc-rows hc-rows--cron',
1195   className: 'hc-rows',
1201   className: 'hc-row',
1204   jsx('span', { className: 'hc-row__name', children: title }, 'title'),
1208   className: 'hc-row__meta',
1216   className: 'hc-row__meta hc-row__meta--tail',
1224   { className: 'hc-row__meta hc-row__meta--tail', children: row.profile || '' },
1326   className: 'hc-row',
1329   jsx('span', { className: 'hc-row__name', children: name }, 'name'),
1338   className: 'hc-row__next',
1354   // 列定义在 style.css 的 `.hc-rows--selfcheck`（基类 `.hc-row` 已不带列定义）。
1355   className: 'hc-rows hc-rows--selfcheck',
1361   className: 'hc-rows__head',

src/sections/upgrade.js
544    className: 'hc-row hc-up__row',
609    className: 'hc-rows hc-up__rows',
616    className: 'hc-rows__head',

src/sections/vcc.js
576    className: 'hc-rows',

src/style.css
696    .hc-rows {
708    .hc-rows__head {
726       `.hc-row` 在 `src/` 里不止被表格用 —— `models.js` 的「整块内容塞一行」（1 个子元素、
729     * 表格行：`.hc-rows .hc-row`（默认比列、`--row-cols` 覆盖、窄档 3 列）；
730     * 整块堆叠行：`.hc-rows--stack .hc-row`；
731     * 自检行：`.hc-rows--selfcheck .hc-row`。
732   新增使用 `.hc-row` 的地方，如果不在 `.hc-rows` 容器里，必须显式换成上面某一个或补专用类。 */
733    .hc-row {
741    /* 只有**真正在列表容器里**的行才吃列定义（表头 `.hc-rows__head` 的 `--row-cols` 同源）。 */
742    .hc-rows .hc-row {
749    .hc-rows--cron {
753    .hc-rows--cron .hc-row {
758    .hc-rows--selfcheck {
762    .hc-rows--selfcheck .hc-row,
763    .hc-rows--selfcheck .hc-rows__head {
769      绝不能落到 `.hc-rows .hc-row` 的 4 列网格上（那会把整块塞进第 1 列）。 */
770    .hc-rows--stack .hc-row {
778    .hc-row + .hc-row {
782    .hc-row:hover {
786    .hc-row__name {
793    .hc-row__meta {
799    .hc-row__meta--tail {
805    .hc-row__next {
856    .hc-row__scope {
1080   .hc-rows--cron {
1084   .hc-rows__head > :last-child,
1085   .hc-row__scope {
1090      基类 `.hc-row` 不再有列定义，堆叠/自检行也不会被这条误伤。 */
1091   .hc-rows .hc-row {
1097   .hc-rows--cron .hc-row {
1103   .hc-sessions .hc-row > :nth-child(4) {
1110   .hc-rows--cron {
1114   .hc-rows--cron .hc-row > :nth-child(3),
1115   .hc-rows__head > :nth-child(3) {
3451   * 只用概览已有的 .hc-cards / .hc-card / .hc-block / .hc-rows / .hc-btn 等积木，
3578   .hc-up__rows .hc-row {
3632   * 其余全部复用既有积木（.hc-block / .hc-rows / .hc-row / .hc-btn / .hc-chip /
4148   * 其余继续复用 .hc-block / .hc-rows / .hc-row / .hc-btn / .hc-chip / .hc-cards。
4161   /* agent 表是主体：不套 .hc-rows 的 264px 滚动上限，整表随页面走 */
4162   .hc-models__table .hc-rows {
```

逐条判断（只列 `.hc-row` **类名**的真实使用点；`hc-row__*` 是子元素修饰、不是行容器）：

| 位置 | 所在容器 | 判断 |
| --- | --- | --- |
| `models.js:287` AgentRow | 父级 `hc-rows`（4222） | 保留。行内 `display:flex` 覆盖网格，`.hc-rows .hc-row` 只提供外观，行为不变。 |
| `models.js:2390` 预览行 | `hc-rows--stack`（2387） | 归属堆叠容器，保留 `hc-row` 外观类。 |
| `models.js:2525` 写入结果行 | `hc-rows--stack`（2519） | 同上（本卡点名的一处）。 |
| `models.js:3210` MoA 预设行 | `hc-rows`（3200） | 保留。行内 `display:flex; flex-wrap:wrap` 本就覆盖网格，4 个以上子元素按需换行。 |
| `models.js:3397` SnapshotRow | 父级 `hc-rows`（3870） | 保留。行内 `flex`。 |
| `models.js:3434` PresetRow | 父级 `hc-rows`（4647） | 保留。行内 `flex`。 |
| `models.js:3705` 回滚结果行 | `hc-rows--stack`（3701） | 归属堆叠容器（本卡点名的一处）。 |
| `models.js:3755` 批次账本行 | `hc-rows--stack`（3750） | 属「整块内容」消费者，一并归位。 |
| `overview.js:1083` 定时任务行 | `hc-rows--cron`（1129） | 保留，四列定宽由 `--row-cols` 给出。 |
| `overview.js:1201` 会话行 | `hc-rows`（1195） | 保留，真表格（4 个子元素 = 4 列）。 |
| `overview.js:1326` 自检行 | `hc-rows--selfcheck`（1355） | 保留，三列定义落回样式表。 |
| `upgrade.js:544` 进程行 | `hc-rows hc-up__rows`（609） | 保留，列由 `.hc-up__rows .hc-row` 的 `--row-cols` 给出。 |
| `vcc.js:576` | 只有 `hc-rows` 容器，**无** `.hc-row` 子元素 | 不受影响（日志行用行内 style）。 |

结论：`src/` 里**不存在**「不在 `.hc-rows` 容器内、却依赖隐式四列」的 `.hc-row`。
`style.css` 中 `.hc-sessions .hc-row > :nth-child(4)` 仍在 `.hc-rows` 内（会话表），
是窄档收起第 4 列，保留。

---

## F-07 扫描轮询无清理

**改了什么**

- `src/sections/autogroup-mode.js:26`：`import { useEffect, useRef, useState } from 'react'`。
- `src/sections/autogroup-mode.js:248-272`：新增 `cancelledRef` / `timerRef`，
  以及挂载 effect：挂载时 `cancelledRef.current = false`；清理函数置
  `cancelledRef.current = true` 并 `clearTimeout(timerRef.current)`（再置 `null`）。
- `src/sections/autogroup-mode.js:352-356`：`pollScan` 入口与 Promise 回调入口都先判
  `cancelledRef.current`，为真直接返回，不再 `setState`、不再排下一轮。
- `src/sections/autogroup-mode.js:392-395`：递归轮询改为
  `timerRef.current = setTimeout(...)`，回调里先清空 `timerRef.current` 再进下一轮。

**为什么**：原来的递归 `setTimeout` 不在任何 effect 里，组件卸载后无句柄可清；切走分区
会一直打到 `SCAN_POLL_TIMEOUT_MS`（4 分钟）并对已卸载组件 `setState`。对照
`autogroup.js:3504-3507` / `autogroup-ai.js:1005-1008` 的 `cancelled + clearTimeout`。
挂载 effect 重置 `cancelledRef`，保证「卸载后再挂载」以及 StrictMode 的
「挂载→卸载→再挂载」仍能正常扫描。

---

## 构建与验收

- 颜色纪律：本轮新增样式只使用 `var(--ui-*)` 与 `currentColor` / `transparent`，
  没有新增任何字面色值。
- 构建：**未能在本会话内执行** —— 本环境的 Bash 档位对
  `node scripts/build.mjs`（以及任何「解释器 + 脚本」形式的命令）持续返回
  `This command requires approval`，无法在本 worktree 里生成 `dashboard/dist/*` 与
  `desktop/plugin.js`。产物为 gitignore 文件，未越界改动。
  需要在可执行构建的环境里补跑一次 `node scripts/build.mjs`（预期输出 `BUILD-OK`，
  且 `dashboard/dist/index.js` 与 `desktop/plugin.js` 同字节）。

FE-FIXED：F-01,F-03,F-04,F-07

未完成: 构建未执行（环境权限拒绝了 `node scripts/build.mjs`）；代码修改全部落盘，
产物需在放行构建的环境补跑。