# STATUS — hermes-console 阶段①

更新时间：2026-10-30 00:2x (+08:00)
本卡：**t_3d4db435 模型分区：moa 补齐 + 单个/多个/全部 agent 批量设模型（预览 → 确认 → 逐 agent 回读 → 一键回滚）**
下面「M4b2 …」及其后内容是更早的卡留档，未删。

## t_3d4db435 做到哪（一句话）

模型分区里 **moa 回来了**（它一直不在 `PROVIDER_REGISTRY`，只在 `HERMES_OVERLAYS` 里声明为
`auth_type="virtual"`，旧枚举两个来源都没有它），并且新增「**批量设置模型**」面板：
勾一个 / 几个 / 全部 agent → 选 provider + 模型（moa 的"模型"= MoA 预设名）→ **预览**
（逐 agent 列出将写哪个字段、从什么变成什么，零写入）→ **二次确认** → 逐 agent 快照 + 写入 +
**逐 agent 回读校验** → 拿 `batch_id` **一键回滚**（每个 agent 各自按字节回到自己的原值）。

### 1. 交付物

| 文件 | 动作 | 说明 |
|---|---|---|
| `dashboard/api_models.py` | 改 | 13 → **17 条路由**：新增 `/models/batch/preview`、`/models/batch/apply`、`/models/batch/rollback`、`/models/batches`；新增 `_virtual_providers()`（从 `HERMES_OVERLAYS` 取 `auth_type=virtual`）并把 moa 并进 provider 枚举与写入路径；`/models/models?provider=moa` 返回该 agent 的 `moa.presets` 预设名；抽出 `_plan_write` / `_apply_one` / `_restore_one`（单 agent 与批量共用同一份实现）；`_write_official` 增加**白名单式删除**（除显式要删的键外，任何缺键仍拒绝） |
| `src/sections/models.js` | 改 | 新增 `BatchPanel`（勾选 + 快捷全选/清空/只选没配的 + provider/模型 + 预览表 + 二次确认条 + 逐 agent 回读表 + 一键回滚 + 最近账本）；provider 目录里给虚拟 provider 加「虚拟 · MoA」标签；顺手修掉两处 React `key` 走 props 的 console error |
| `scripts/probe_models_batch_api.py` | 新增 | 后端契约探针（沙盒）**40 PASS / 0 FAIL** |
| `scripts/probe_models_batch_real.py` | 新增 | 真 home 真改一个 agent 再改回（`media-worker`，sha256 逐字节）**12 PASS / 0 FAIL** |
| `scripts/probe-models-batch.mjs` | 新增 | 真 Chromium 验收 + 截图（批量前 / 预览态 / 批量后回读 / 回滚后 / 窄窗 / 单 agent 选 moa，每张都另带 `-detail` 元素特写）**33 PASS / 0 FAIL** |
| `scripts/models_batch_fixture.py` | 新增 | 沙盒夹具：真实 `plugin_api.py` + 真实 home 的配置副本 |
| `scripts/acceptance-models-batch.sh` | 新增 | 本卡验收一键跑（构建 → 4 个探针 → 回归） |
| `scripts/probe_models.py` / `scripts/probe_registry.py` | 改 | 路由计数 13 → 17（事实变化，其余断言未动） |
| `dashboard/dist/index.js`、`desktop/plugin.js` | 生成 | `node scripts/build.mjs`（450826 B，两份同字节） |

### 2. moa 为什么以前不在列表里（结论 + 行号）

* `hermes-agent/hermes_cli/providers.py:29`：`HERMES_OVERLAYS["moa"] = HermesOverlay(auth_type="virtual", base_url_override="moa://local")`；
* `hermes-agent/hermes_cli/auth.py:242`：`PROVIDER_REGISTRY` 由 `_REGISTRY_ROWS` 生成，**没有 moa 这一行**
  （实测 `len=78`、`"moa" in PROVIDER_REGISTRY == False`）；
* `hermes-agent/hermes_cli/runtime_provider.py:984`：`provider == "moa"` 在 `_resolve_requested_shortcuts` 里短路成 `moa://local`；
* 本文件原枚举只有「`config.yaml` 的 `providers:`」+「`PROVIDER_REGISTRY`」两个来源
  （`_builtin_registry` L424-450 / `_provider_names` L453-459 / `_provider_rows` L488-552）。

⇒ **不是被过滤掉，也不是没进 catalog，是根本没被枚举**：moa 既没写进该 agent 的 `providers:`
（`default` 的 config.yaml 只有 openrouter / gemini / balance），也不在 `PROVIDER_REGISTRY`，
于是 `_provider_names()` 里没有它、`_require_provider()` 还会以 `invalid_argument` 拒 `provider=moa`。

### 3. 关键设计（下一任别改坏）

* **一条一个快照、一条一次回读**：批量写入复用单 agent 的 `_apply_one`，任何一条失败只影响那一条
  （`snapshot_failed` / `verify_mismatch` 各自独立），响应里逐条给 `ok` / `error` / `verify`。
* **整批一份账本**：`<home>/plugins/hermes-console/data/batches/<batch_id>.json`，一键回滚只认账本里
  `ok=true` 的条目（当时就没写成功的没有"原值"可还原）。
* **删除必须过白名单**：切到 moa 会删掉遗留的 `model.key_env`，而官方 `atomic_config_write`
  拒绝任何"缺键"。`_write_official(allow_deletions=[...])` 先按默认路径写，被拒后**逐条核对**
  省略路径是否全在白名单里，有一条不在就原样抛出（不许拿一次 replace 赌运气）。
* **预览是纯读**：`/batch/preview` 不做快照、不落盘（探针拿 sha256 盯着）。

### 4. 实测

| 探针 | 结果 |
|---|---|
| `probe_models_batch_api.py` | **40 PASS / 0 FAIL**（`MODELS-BATCH-API-OK`） |
| `probe_models_batch_real.py` | **12 PASS / 0 FAIL**（`MODELS-BATCH-REAL-OK`）：`media-worker` 真改 `deepseek-v4.1-flash → auto` 再回滚，`sha256 35fed26e…` 前后一致，其余 8 份 config.yaml 一字节未动 |
| `probe-models-batch.mjs` | **33 PASS / 0 FAIL**（`MODELS-BATCH-PROBE-OK`）：9 个 agent 全选 → moa/预设 → 预览零写入 → 逐 agent 回读 4/4 → 一键回滚后 9 份 config.yaml sha256 全等；窄窗 760px 无横向滚动；单 agent 面板选 moa 也出预设名 |
| `probe_models.py`（回归） | 104 PASS / 1 FAIL —— 仅剩 `manifest default_enabled` 一条**本卡之前就红**的断言（见 §5） |
| `probe-models.mjs`（回归） | 45 PASS / 0 FAIL |

截图：`scripts/_probe_out/models-batch/{01-before-batch,02-preview,03-after-batch,04-rollback,05-narrow}.png`

### 5. 已知遗留（不是本卡引入）

* `probe_models.py` 的「manifest 字段逐字正确」与 `probe_registry.py` 的
  `default_enabled=False` 一组断言在**本卡之前就已经红**：真实 `dashboard/modules/*/manifest.json`
  里 `models/autogroup/advisor/upgrade/vcc/remote` 全是 `default_enabled: true`（mtime 10-29 19:10），
  与这两支探针里"带写操作默认关"的期望不符。本卡没动 manifest，也没动默认启用策略。
* `probe_registry.py` 另外 3 条红也是同源（`GET /overview/logs`、`GET /overview/status`、
  `POST /remote/credentials/reveal` 三条更早的卡新增的路由没进它的 BASELINE 列表）。

## M4b2 做到哪（一句话）

设置界面上「开启 / 关闭」按钮的后端补齐了：`PUT /agents/{agent}/plugins/{plugin}`
（路由总数 41 → 42，`failed={}`）**只调** `core.plugin_switch.set_plugin_enabled(strict=True)`——
黑名单优先、`plugins.enabled` 一字节不改、`unavailable → enabled` 走 M2c 已验收的官方准入
`_admit_and_save_plugin_sets`。同时把「按 agent 勾模块开关」的入口补上：
`GET .../plugins/hermes-console/settings` 的 `writable_keys` 现在带 registry 声明的
`modules.<id>.enabled`，`has_settings` 对 hermes-console 变 true，`PUT .../settings` 认这个键路径。

### 1. 交付物

| 文件 | 动作 | 说明 |
|---|---|---|
| `dashboard/api_console_settings.py` | 改 | **+1 路由** `PUT /agents/{agent_id:path}/plugins/{plugin_id:path}`；模块开关白名单（registry 声明）+ `module_switches` 只读字段 + `has_settings` 修正 |
| `scripts/probe_plugin_toggle_api.py` | 新增 | 本卡验收（120 PASS / 0 FAIL / `PLUGIN-TOGGLE-API-PROBE-OK`） |
| `scripts/probe_registry.py` | 改 | 路由总数 41 → 42（新增 `M4B2_ROUTES` 并单独断言它）
| `scripts/probe_console_settings_api.py` | 改 | 路由总数 41 → 42、`/agents/{agent_id:path}` 前缀路由 4 → 5（事实变化，其余断言未动） |
| `scripts/probe_plugin_catalog.py` | 改 | 路由总数 41 → 42（同上） |
| `README.md` / `STATUS.md` | 改 | 探针清单 + 本卡留档 |

**未改** `src/`（前端零改动，`togglePlugin()` 契约本来就对）；**未改** `dashboard/core/*`
（三态判定与名单合并没有第二份实现）。

### 2. 端点契约（真实请求/响应见本卡交付说明）

```
PUT /api/plugins/hermes-console/agents/{agent}/plugins/{plugin}
请求体  {"enabled": <bool>, "expected_config": "<该 agent config.yaml 摘要，可选>"}
成功 200 {"ok": true, "data": { ...set_plugin_enabled 的返回..., "state_before", "state_after",
                                "state_before_detail", "state_after_detail", "steps", "verified",
                                "config_digest_before", "config_digest_after" }}
失败    {"ok": false, "error": {"code", "message", ...}}
```

错误码：非法 agent → 400 `invalid_argument`（带合法 id 列表）；非法 plugin → 400；
`enabled` 非真 bool → 400；该 agent 无 `config.yaml` → 400 `config_missing`；
`expected_config` 不一致 → 409 `config_changed`（零写入）；托管/管理员键 → 403 `permission_denied`；
准入通道不可用 → 503 `admission_unavailable`（**绝不**退化成只摘黑名单）；
准入权威拒绝 → 400 `admission_refused` / `dependency_conflict`（原样透出）；
写后回读与自述不符 → 500 `write_not_applied`。

### 3. 已知边界（必须知道的一条）

- 插件**只在 `plugins.disabled` 里、不在 `plugins.enabled` 里**时，点「开启」按 M2 口径只摘黑名单，
  回读仍是 `unavailable`，响应带 `warning`（原文：「已从黑名单移除，但该 agent 的 plugins.enabled
  里也没有它，实际仍未启用；需要另加进 enabled（本卡不代劳）」）。本层**不**顺手改写 `plugins.enabled`，
  也不再点一次就自动补准入——那属于用户的下一次显式操作（下一次就会走官方准入）。界面按
  `state_after` 如实显示，不翻转成「已启用」。

## M4b1 做到哪（上一张卡留档，未删）

控制台多了第 7 个分区「设置」（`src/sections/settings.js`，`nav_order=70`，`default_enabled=true`）：
左列是该 agent 的**插件三态清单**（`enabled` / `disabled` / `unavailable` 三套文案 + 三套动作），
右列是选中插件的**配置项**（每项标来源层 `模块默认 < 全局 < 本 Agent 覆盖`、`masked_keys` 只读、
带 `expected_config` 乐观锁、409 不静默重试、4xx/5xx 原样透出）。

### 1. 交付物

| 文件 | 动作 | 说明 |
|---|---|---|
| `src/sections/settings.js` | 新增 | 设置分区（三态清单 + 配置编辑 + 命名空间 + 乐观锁） |
| `src/shell.js` | 改 | `import { SettingsSection }`、`COMPONENTS.settings`、`FALLBACK_SECTIONS` 加一条、图标 `settings` |
| `src/style.css` | 改 | 新增 `.hc-set*` 一段（三态/来源层/窄窗，零字面色值） |
| `dashboard/modules/settings/manifest.json` | 新增 | 纯前端模块（`api_module: null`、无 `api.py`）：`nav_order 70`、`default_enabled true` |
| `scripts/probe-settings.mjs` | 新增 | 本卡验收（86 PASS，真 React 19 + jsdom） |
| `scripts/probe-shell.mjs` | 改 | 桩里补第 7 个模块 + 6 项硬编码断言按新事实更新（未放宽） |
| `scripts/probe_registry.py` | 改 | 模块集合加 `settings`；新增 `FRONTEND_ONLY_IDS` 支持纯前端模块 |

### 2. 三条硬要求的实现位置

1. **三态不压成一个开关**：`STATE_TEXT` / `STATE_HINT` / `rowAction()`。
   `unavailable` 的 `rowAction()` 返回 `null` ⇒ 只渲染「无可用开关」文本，**不渲染 `<button>`**；
   文案是 `不在该 Agent 可用范围`，与 `已启用` 无交集（探针第 3 条逐条断言）。
2. **命名空间**：`pluginKey()` = `plugin:<id>`，`moduleKey()` = `module:<console>/<id>`；
   后端 catalog 给了 `ref` / `module_ref` 就直接用，缺了才自己拼。
   真实数据里 `advisor` 插件（`plugin:advisor`）与 Advisor 面板（`module:hermes-console/advisor`）
   同行并列显示、元素不同。
3. **脱敏只读**：`masked_keys` 命中 ⇒ 输入框 `disabled` + `***` + 「已脱敏，请到配置文件修改」+
   该字段不渲染保存/重置按钮；提交路径上 `buildPutBody()` 见到 `***` 直接返回错误、**不发请求**。

### 3. 已知边界（M4b2 已补掉前两条）

- ~~**`PUT /agents/{agent}/plugins/{plugin}` 后端还没有**（实测 404）~~ → **M4b2 已补齐**
  （`dashboard/api_console_settings.py`，路由总数 42）。前端兜底文案仍在（后端真 404 时照样显示原话）。
- ~~`writable_keys` 对 hermes-console 是空数组 ⇒ 模块开关没有可写入口~~ → **M4b2 已补齐**
  （白名单从 registry 声明推导，`has_settings` 同步修正）。
- 面板关联对第三方插件（无 catalog 条目）按 `/agents/<id>/modules` 的 `section` 匹配；
  匹配不到就写「无控制台模块」，不猜成有面板。

### 4. 验收（真实输出，2026-10-29 01:2x）

```
node scripts/probe-settings.mjs   PASS: 86   FAIL: 0   SETTINGS-UI-PROBE-OK
node scripts/probe-shell.mjs      PASS: 197  FAIL: 0   SHELL-PROBE-OK
node scripts/probe-dom.mjs        PASS: 127  FAIL: 0   DOM-PROBE-OK
node scripts/build.mjs            BUILD-OK
grep -cE "#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(" src/   → 0（逐文件求和）
```

三份产物 sha256 逐字节相同：`ef4724cf5727212f98257b0c36b69b8d0b5e6dca92528282dec2b23242fc885a`
（`dashboard/dist/index.js` / `desktop/plugin.js` / `desktop-plugins/hermes-console/plugin.js`）

后端探针全部 EXIT=0：`probe_console_settings_api` / `probe_core_settings` / `probe_plugin_switch` /
`probe_plugin_settings_write` / `probe_plugin_switch_admission` / `probe_plugin_catalog` / `selftest`
（M4b2 追加：`probe_plugin_toggle_api` 120 PASS、`probe_registry` 91 PASS）。

---

## C9b 做到哪（上一张卡留档）


控制台多了第 6 个分区「Advisor」：能看到 advisor 产出的**全部建议**（服务端分页 + 状态/severity/关键词
筛选）、每条**是否采纳/拒绝/原因**、每条的事件时间线，以及**策略设置**；数据全部来自 C9a 的
`plugins/advisor/audit.py` 台账（本卡不读日志、不拼状态），采纳/拒绝只往台账追加一行决定，
`blocker_forced_delivery` 只读展示且后端拒绝修改。

### 1. 文件清单

| 文件 | 性质 | 说明 |
|---|---|---|
| `dashboard/api_advisor.py` | **新增** 7 路由 | `/advisor`（自检）、`/advisor/advice`（分页+筛选）、`/advisor/advice/{id}`（详情+时间线）、`POST /advisor/decision`、`/advisor/summary`（统计+7 天趋势）、`/advisor/settings` GET/PUT |
| `src/sections/advisor.js` | **新增** 分区 | 状态条 4 卡 + 趋势、筛选条、建议列表、详情（含决定表单）、策略设置；**自包含**，不 import 其它分区 |
| `src/style.css` | 追加 `.hc-adv__*` | 状态条/列表/详情/设置 + `<900px` 折叠成卡片（无横向滚动条），颜色全 `var(--ui-*)` |
| `dashboard/plugin_api.py` | 最小改动 | `DOMAIN_MODULES` 加一项 + 文档表格加一行（不动其它分区） |
| `src/shell.js` | 最小改动 | 一行 import + `SECTIONS` 一行（放在「项目分组」与「升级」之间，与概览/天气/升级同级）+ `ICONS.advisor` |
| `src/lib/rest.js` | 最小改动 | `splitStatus` 额外抽出后端 `error.code`，`normalizeError` 把它拼进 message（`（后端错误码 upstream_error）`）—— 分区要求「ErrorState 含后端 error.code」 |
| `scripts/probe_advisor.py` | **新增** 探针 | 76 项：7 条路由真 HTTP、决定落盘回读、设置读写、`blocker_forced_delivery` 被拒、import 失败降级 503、既有分区回归 |
| `scripts/probe-advisor.mjs` | **新增** 探针 | 61 项：真 React 19 + jsdom，导航/列表/详情/决定/设置/三态/窄窗 |
| `scripts/probe-dom.mjs` | 最小改动 | 两处「左栏 5 个分区按钮」→ 6（新增了 Advisor 入口，断言跟着走） |

未改：`plugins/advisor/`（只 import 它的 `audit` 模块）、`hermes-agent/`、`config.yaml`、`manifest.json`。

### 2. 后端契约（要点）

- **惰性 import**：每次请求才取 `advisor.audit`；顺序 = 已加载模块复用 → `import advisor.audit`
  （先把 `<home>/plugins`、`<home>/hermes-agent` 补进 `sys.path`）→ 按文件路径加载 `audit.py`
  （**不执行** `advisor/__init__.py`，否则会把 hooks / 模型客户端拉进 dashboard 进程）。
- **降级**：加载失败 → `503 {ok:false,error:{code:"upstream_error", message:"…加载失败（import: …；按路径加载: …）"}}`，
  响应里带 `stage` / `module` / `module_path`。其它分区各自 import 各自的域模块，不受影响（实测见验收 3）。
- **错误码只有三个**：`not_found`(404) / `invalid_argument`(400) / `upstream_error`(503 不可用、500 其它上游故障)。
- **`q` 与分页**：台账 `list_advice` 不支持关键词，本层取全部匹配行后过滤再切页，`total` 是**过滤后**的条数。
- **7 天趋势**：用台账 `read_events()` 的 `ts`（带 offset，先 `astimezone()` 再取 date）分桶数
  `raised` / `delivered` / `accepted+rejected`，某天没有事件就是 0，不按平均补齐。
- **决定**：`audit.record_decision(id, decision, reason)`，写完**回读** `get_advice(id)` 确认
  `status == decision`，否则 500 报「回读不一致」；响应里 `executed: false` + 一句
  「只记录了决定，没有执行建议内容」。`rejected` 缺 reason 在**本层**就 400（台账也会再抛一次）。

### 3. 验收（真实输出）

**(1) 七组路由真 HTTP（`probe_advisor.py`，TestClient + 真实台账 17 条）**

| 用例 | 状态码 | 响应前 200 字符 |
|---|---|---|
| `GET /advisor` | 200 | `{"ok": true, "data": {"module": "advisor.audit", "ledger": "D:\\hermes-data\\hermes\\advisor\\audit\\advice.jsonl", "events": 17, "advice_total": 17, …}}` |
| `GET /advisor/advice?limit=5&offset=0` | 200 | `{"ok": true, "data": {"status": null, "severity": null, "q": "", "total": 17, "limit": 5, "offset": 0, "count": 5, "items": [{"id": "50c4b6d6ebc19cba", "note": "C6 的桌面验收缺口仍未补上：…"` |
| `GET /advisor/advice?limit=5&offset=5` | 200 | 第二页 5 条，与第一页 id 无交集（`offset` 真翻页） |
| `GET /advisor/advice?severity=blocker` | 200 | `{"total": 1, "count": 1, "items": [{"id": "616926ae1f25679e", "severity": "blocker", …}]}` |
| `GET /advisor/advice?severity=fatal` | 400 | `{"ok": false, "error": {"code": "invalid_argument", "message": "severity 只能是 nit / concern / blocker：收到 'fatal'"}}` |
| `GET /advisor/advice/{id}` | 200 | `{"ok": true, "data": {"id": "50c4b6d6ebc19cba", "status": "pending", "first_raised_at": "2026-10-28T20:08:03+08:00", "raise_count": 1, "deliver_count": 0, "events": [{"event": "raised", …}]}}` |
| `GET /advisor/advice/0123456789abcdef` | 404 | `{"ok": false, "error": {"code": "not_found", "message": "台账里没有这条建议：0123456789abcdef", "id": "…", "ledger": "…advice.jsonl"}}` |
| `POST /advisor/decision {accepted}` | 200 | `{"ok": true, "data": {"id": "50c4b6d6ebc19cba", "decision": "accepted", "written_event": {"event": "accepted", "actor": "user", "source": "console"}, "readback": {"status": "accepted", "decision": "accepted"}, "executed": false}}` |
| `POST /advisor/decision {rejected, reason:""}` | 400 | `{"ok": false, "error": {"code": "invalid_argument", "message": "拒绝必须填写原因（reason 不能为空）", "id": "72ff7b05195b4a77"}}` |
| `POST /advisor/decision {rejected, reason:"探针：这条建议与本机无关"}` | 200 | `{"ok": true, "data": {"decision": "rejected", "reason": "探针：这条建议与本机无关", "readback": {"status": "rejected", "reason": "探针：这条建议与本机无关"}}}` |
| `GET /advisor/settings` | 200 | `{"ok": true, "data": {"settings": {"enabled": true, "concern_default": "hold", "min_interval_seconds": 0, "model": "", "provider": "", "blocker_forced_delivery": true, "readonly_fields": ["blocker_forced_delivery"]}, "writable": [...], "readonly": ["blocker_forced_delivery"], "forced": {"blocker_forced_delivery": true}}}` |
| `PUT /advisor/settings {enabled:false, min_interval_seconds:45, model:"probe-model"}` | 200 | `{"ok": true, "data": {"values": {"concern_default": "hold", "enabled": false, "min_interval_seconds": 45, "model": "probe-model", "provider": ""}}}`（`settings.json` 磁盘回读一致） |
| `PUT /advisor/settings {blocker_forced_delivery:false}` | **400** | `{"ok": false, "error": {"code": "invalid_argument", "message": "blocker_forced_delivery 是只读常量：单向阀门不可关闭（blocker 永远强制交付），没有、也不会有把 blocker 降级或关闭提示的入口", "field": "blocker_forced_delivery", "value": false, "readonly": true}}` |
| `PUT /advisor/settings {unknown_knob:1}` | 400 | `{"ok": false, "error": {"code": "invalid_argument", "message": "设置未写入：unknown setting 'unknown_knob'"}}`（整批拒绝，不半写） |

**(2) 写操作真实落盘**（`POST /decision` 后从磁盘回读 `advisor/audit/advice.jsonl`，行数 17 → 18）

```json
{"actor": "user", "decision": "accepted", "event": "accepted", "id": "50c4b6d6ebc19cba",
 "note": "C6 的桌面验收缺口仍未补上：…", "profile": "default", "reason": "",
 "schema_version": 1, "session_id": "local-342da196…", "severity": "concern",
 "source": "console", "ts": "2026-10-28T20:31:28.408+08:00", "turn_id": ""}
```

第二条（拒绝）：`{"actor": "user", "decision": "rejected", "event": "rejected", "id": "72ff7b05195b4a77", "reason": "探针：这条建议与本机无关", …}`。
两行都是**新追加**的（断言「前 17 行字节未变」通过），决定后再 `GET /advice/{id}` 状态即为 `accepted`/`rejected`
（折叠自文件，不是内存）。**探针收工按字节原样还原**：台账 sha256 前后都是
`1b2f9201c2077807e1a826698e32407b526920d4b0a82173d2c2ca6a114feb12`、17 行；`settings.json` 探针前不存在 → 删除。

**(3) 降级（模拟 `advisor.audit` import 失败）**

- `GET /advisor/summary` → **503** `{"ok": false, "error": {"code": "upstream_error", "message": "advisor 台账模块不可用，本分区暂时读不到建议（其它分区不受影响）：advisor.audit 加载失败（import: ImportError：探针模拟：advisor 插件不可导入；按路径加载: locate 找不到 advisor 台账模块：…）", "stage": "import", …}}`
  （`GET /advisor/advice`、`PUT /advisor/settings` 同时刻同样 503）
- 同一时刻其它分区：`GET /agents` **200**、`GET /upgrade` **200**、`GET /autogroup/status` **200**、
  `GET /overview/layout` **200**、`GET /health` **200** `{"ok": true, "loaded": [..., "api_advisor"], "failed": {}}`
- 摘掉故障后 `GET /advisor/summary` 立刻回 200。

**(4) 前端探针**：`node scripts/probe-advisor.mjs` → **PASS 61 / FAIL 0 → ADVISOR-DOM-PROBE-OK**
（导航入口、4 个数字卡 + 口径 + 7 天趋势、20 行列表 + severity 三档 class、分页 offset=20、
详情全文/计数/4 条时间线、空原因拒绝不发 POST 且给行内校验、带原因 POST `decision=rejected`、
采纳 POST `decision=accepted`、设置 5 控件 + 只读徽标「程序强制，不可关闭」且徽标内 0 个控件、
PUT 白名单 5 字段、后端 400 原话回显、Skeleton/Empty/ErrorState 三态、窄窗容器查询块 + 无 `overflow-x` 滚动）。

**(5) 构建幂等**：连跑两次 `node scripts/build.mjs`

```
dashboard/dist/index.js   197078 B  sha256=e709d42ed9161d86ef04f50abe8522837afd59d3e5fadaf5b0062c4ff4f800fa
dashboard/dist/style.css   47114 B  sha256=74baaf8562464897f454792d02c6c246189915feca669ef47b105cdf64a3518a
desktop/plugin.js         197078 B  sha256=e709d42ed9161d86ef04f50abe8522837afd59d3e5fadaf5b0062c4ff4f800fa
```

两次输出 diff 为空（`IDENTICAL-OUTPUT`），三个产物字节一致。门牌已同步：
`desktop-plugins/hermes-console/plugin.js` = `e709d42e…`（与 `dist/index.js` 同字节）。

**(6) 既有分区回归**：`/overview/system` 200、`/weather/now` 200、`/upgrade` 200、`/agents` 200、
`/autogroup/status` 200、`/advisor/summary` 200；`probe-shell.mjs` **154 PASS / 0 FAIL**、
`probe-dom.mjs` **124 PASS / 0 FAIL**、`selftest.py` **SELFTEST-OK**。

### 4. 已知边界 / 未做

- **台账按 profile 隔离**：目录由 `audit.audit_dir()`（`get_hermes_home()`，`ADVISOR_AUDIT_DIR` 可覆盖）决定，
  分区**不接受** `?agent=`（和项目分组一样标 `ignoresScope`）；解析到的目录与主目录不一致时，响应 `notes`
  会同时写出两边路径，不静默读错地方。
- **没有实时推送**：决定写完靠 reload 三个 query（列表/统计/详情），别的窗口做的决定要手动「刷新数据」。
- **没有截图**：窄窗只做了结构断言（容器查询块存在、`overflow-x` 非 auto/scroll、列全 `minmax(0, …)`），
  没在真浏览器里量像素。
- **`q` 是内存过滤**：台账没有关键词接口，本层取全部匹配行再过滤，台账上万条时该改成台账侧过滤。
- 本卡不动 `plugins/advisor/` 内部逻辑，也不新增任何「关闭 blocker / 降级 blocker」的入口。

---

# 附：C6 阶段记录（上一张卡，保留备查）

更新时间：2026-10-28 15:35 (+08:00)
本卡：**C6 桌面端样式注入 + 视觉打磨**
下面「C4 天气域搬移」及其后内容是上一张卡留档，未删。

## C6 做到哪（一句话）

面板不再是裸文本：样式**以字符串进 bundle、模块自己幂等注入 `<style>`**（桌面端唯一生效路径），
并按 6 条视觉问题逐条改了 CSS。产物已构建，两套 JS 探针 + 三套 Python 探针全绿。

### 1. 注入链路（必交付 1）

| 环节 | 实现 | 文件 |
|---|---|---|
| CSS 作为**字符串**进 JS bundle | `--loader:.css=text`（JS_ARGS） | `scripts/build.mjs` |
| `@import tokens.css` 就地剥离 + tokens 前拼 | 正则替换 + tokens 文本前置（**不给浏览器留外链 @import**，否则按页面 URL 解析必定 404） | `src/lib/style-inject.js` |
| 尺寸 token 展开 | `var(--s4)` → `16px`（值仍取自 `tokens.css`，无第二份常量表）；`var(--ui-*)` **一律不动**，留给宿主主题改深浅色 | 同上 |
| 幂等注入 | `style[data-hermes-console-style]`：已存在则复用，历史多余节点摘掉，内容变才写回 | 同上 |
| 触发时机 | 模块顶层求值 + `register()` 各一次（首帧就有 CSS） | `src/main.js` |
| `manifest.json` 的 `css` 键 | **保留**（web dashboard SPA 用），代码不依赖 | `dashboard/manifest.json`（未改） |

### 2. 六条视觉问题的处置

| # | 问题 | 改成什么 | 对应 CSS |
|---|---|---|---|
| 1 | 统计卡是竖排文本 | 响应式网格：≥1200px 四列 / ≥900px 两列 / 否则一列；卡间 & 卡内 `--s4`；1px `var(--ui-stroke-secondary)` 描边 + `--r-md` 圆角 | `.hc-cards`（+两档 `@container hc`）、`.hc-card` |
| 2 | 数字层级不分 | 三档：主数值 `--fs-xl`+`--ui-text-primary` · 单位 `--fs-sm`+`--ui-text-quaternary` · 口径 `--fs-xs`+弱色 | `.hc-card__num` / `.hc-card__unit` / `.hc-card__caliber` |
| 3 | cron 输出粘连 | 真 grid 定宽列（任务名/下次执行/状态/作用域），表头与数据行共用 `--row-cols`；行高 `--row-h`；hover 换底色 | `.hc-rows--cron` / `.hc-rows--cron .hc-row` / `.hc-rows__head` / `.hc-row:hover` |
| 4 | 挂件是「clock×3 …」串文本 | 小卡片网格（每格：挂件名 + 出现次数 + 摆位），复用统计卡视觉 | `.hc-widgets` / `.hc-widget` / `.hc-widget__label|__num|__unit|__meta` |
| 5 | 分区看不出选中 | 选中态三重信号：左描边 `--nav-mark` 宽 + 底色 + 文字提亮 + 600 字重；未选中只有 hover 才变 | `.hc-nav__item--active`（`::before` 内嵌标记）/ `.hc-nav__item:hover` |
| 6 | 状态色无纪律 | 正常 `--ui-text-secondary` / 异常危险 `--ui-warning` / 禁用 `--ui-text-quaternary`；形状（左圆点、虚线）同时变化，不只靠色 | `.hc-status` / `.hc-status--ok|warn|off` |

### 3. token / 调用量 加载态（必交付 3）

- 加载中 = **骨架屏**（`.hc-skel-card` 四段占位：标签/大数值/构成/口径），**不出现「读取中」三个字**；
- analytics 给 12s 明确上限（`ANALYTICS_TIMEOUT_MS`），超时/失败 → `ErrorState` + 原因（超时文案是「取数超时」，不是假的「连不上后端」）；
- 坏掉的那张卡仍在统计卡栅格里，不把布局顶歪。

### 4. 窄窗（必交付 4）

`<900px`：统计卡一列、cron 收敛到 3 列并隐藏「作用域」列、导航收成图标条、内容面板 `overflow-x: hidden`（不横向滚动）。
`<599px`：cron 再收到 2 列。全部走 `@container hc`（面板被拖窄时同样降列，不是只看窗口宽度）。

### 5. 产物与探针（真实输出）

| 产物 | 字节 | sha256 |
|---|---|---|
| `dashboard/dist/index.js` | 99,635 | `cb55ce6d05c53cda663613343df70902876875e1db5117a3e47ccedac5e89f65` |
| `dashboard/dist/style.css` | 23,906 | `603b61c101ed9e7e5e9c18d072d2e77b03bcf4cf55b9ac5d14f161ee538fae17` |
| `desktop/plugin.js` | 99,635 | 与 index.js 逐字节相同 |

连跑两次 `node scripts/build.mjs` 的 index.js sha256 相同（幂等）。
探针：`probe-dom.mjs` 65/0、`probe-shell.mjs` 150/0、`probe_overview.py` OVERVIEW-PROBE-OK、
`probe_weather.py` 60/0、`selftest.py` SELFTEST-OK；`src/` 与产物 0 处字面色值。

---

## C4 做到哪（上一张卡留档）

**4 个后端路由 + 天气分区前端（从零）全部落地，四套探针真实执行 0 失败**
（probe_weather 60、probe-shell 126、probe-dom 45、selftest 全绿），产物已构建并验幂等。

| 交付项 | 文件 | 状态 |
|---|---|---|
| 4 个路由搬进统一 router（`/api/plugins/hermes-console/weather/{now,daily,hourly,at}`） | `dashboard/api_weather.py`（28 KB） | ✅ 语义等价搬移 + `/weather` 自检（共 5 条） |
| 数据源不换、降级顺序不换 | 仍走 `scripts/weather_lib.py` | ✅ 彩云 → 和风 → Open-Meteo，本模块只读不改 |
| 显式返回 `source` + 取数时间 | 每条响应带 `source` / `source_label` / `fetched_at` / `fetched_at_ms` / `sources`（三源各自 ✓/×空 + 调用次数）/ `units` | ✅ 来源不靠猜：实况用 `weather_lib` 自报的 `source`，逐日/逐时用**只读探针**记录三源返回值 + 按降级顺序与返回体键判定 |
| 当前天气卡 | `src/sections/weather.js` | ✅ 温度 / 体感 / 现象 / 湿度 / 风速 / PM2.5（+AQI 与等级）/ 今日 / 明日 / 未来 2 小时 / 预警 |
| 逐小时（默认 24h） | 同上 `.hc-wx-hours` | ✅ 横向滚动，每小时给 时:分 / 温度 / 现象 / 降水概率 |
| 多日（默认 7 天） | 同上 `.hc-wx-days` | ✅ 一行一天：日期+周几 / 现象 / 最低~最高 / 降水概率 |
| 「N 小时后」查询 | 同上 `.hc-wx-seg` | ✅ 1/6/12/24/48h 分段选择，结果内联（**照抄** weather_lib 原话） |
| 城市/坐标输入 | 同上 `.hc-wx-toolbar` | ✅ 默认后端配置地点（`HERMES_WEATHER_PLACE`，缺省「家」= 肥西/方岗翡翠苑北门），输入框可临时查任何城市或「纬度,经度」，带「复位」 |
| 三态复用 C2 | `lib/states.js` | ✅ 加载 Skeleton / 无数据 Empty / 失败 ErrorState（带后端原话，含逐源 ×空） |
| 拿不到就 `—`，不填 0 | `dec()/unit()/popText()/descText()` 四个纯函数 | ✅ 探针用「逐小时 pop=null」「多日缺 pop 键」两处桩数据证明渲染成 `降水 —` |
| 单位标在界面 | 后端 `units` 表驱动 | ✅ ℃ / % / km/h / μg/m³ |
| 温度与现象只照抄 | 前端不做合并/翻译 | ✅ 后端 `18.83` → 界面 `18.8℃`；`☀️晴` 原样 |

## C4 四条路由实测（FastAPI TestClient，真打上游，2026-10-28 14:07）

| 路径 | HTTP | 耗时 | 实际命中源 | 响应前 300 字符 |
|---|---|---|---|---|
| `/weather/now` | **200** | 2.2s | **彩云**（和风 ×空、OM ✓ 但未用） | `{"ok":true,"place":"家","scope":{"kind":"global","agent":null,"note":"天气是全局数据，不随 agent 变化；agent 参数只做回显"},"fetched_at":"2026-10-28T14:07:19+08:00","fetched_at_ms":1793167639666,"sources":[{"source":"caiyun","label":"彩云天气","calls":3,"ok":true},{"source":"qweather","label":"和风天气","calls":3,"ok":false},{…` |
| `/weather/daily` | **200** | 1.0s | **Open-Meteo**（和风 ×空 → 降级） | `{"ok":true,"place":"家",…,"fetched_at":"2026-10-28T14:07:21+08:00",…,"sources":[{"source":"qweather","label":"和风天气","calls":1,"ok":false},{"source":"open-meteo","label":"Open-Meteo","calls":1,"o…` |
| `/weather/hourly` | **200** | 0.3s | **彩云** | `{"ok":true,"place":"家",…,"sources":[{"source":"caiyun","label":"彩云天气","calls":1,"ok":true}],"units":{"temp":"℃","humidity":"%","wind":"km/h","pm25":"μ…` |
| `/weather/at?hours=6` | **200** | 0.2s | **彩云** | `{"ok":true,"place":"家",…,"sources":[{"source":"caiyun","label":"彩云天气","calls":1,"ok":true}],"units":{…`，`text` = `📍家 6小时后(20时): 15°C 多云` |

真实数据（同一次）：`realtime = {"source":"彩云","name":"家","temp":18.83,"feels":16.7,"humidity":46,"wind_kmh":44,"desc":"☀️晴","aqi_chn":39}`；
`air = {"pm25":14,"pm25_unit":"μg/m³","aqi_chn":39,"aqi_level":"优",…}`；`daily[0] = {"date":"2026-10-28","text":"阵雨","min":12.4,"max":19.1,"pop":69,…}`（`pop` 来自 Open-Meteo 原始返回，和风 7d 只有降水量、**不冒充概率**）。

**一个现网事实**：和风这一源今天**三处都返回空**（`ok:false`，`.env` 里有 key 但接口没给数据），
所以实况/逐时落在彩云、逐日落在 Open-Meteo —— 这正是"降级"在真实环境里发生的样子，
也是为什么每条响应必须标 `sources`：面板上能直接看到「和风天气 ×空」。

## C4 降级验证（探针掐域名，不是替换取数函数）

`weather_lib._get_json` 按 URL 域名抛异常 → `fetch_caiyun/hf_get/fetch_openmeteo` 自己返回 None，
降级链原样跑：

| 场景 | 结果 |
|---|---|
| 只掐 `caiyunapp.com` | `source=open-meteo`；`sources=[彩云×空, 和风×空, OM ✓]`（和风本来就空） |
| 掐彩云 + 和风 | `source=open-meteo`，`/weather/daily` 仍返回 7 条 |
| 三个域名全掐 | **HTTP 200** + `{ok:false,error:{code:"all_sources_failed",message:"三个天气源都没返回实况（彩云天气×空；和风天气×空；Open-Meteo×空）"}}` |
| 地点非法（`place=这个城市不存在zzz`） | **400** + `error.code=invalid_place`（不是 500） |
| 前端：只有 `/weather/now` 503 | 当前卡进 ErrorState（保留 503 原话），逐小时 24 项 / 多日 7 行 / N 小时后照常 |

复跑：`python plugins/hermes-console/scripts/probe_weather.py`（60 项断言）。

## C4 产物与纪律

| 产物 | 字节 | sha256 |
|---|---|---|
| `dashboard/dist/index.js` | **61,989 B** | `25cb9d610f3d0b324baaab18429baa07356eabb7a8b881e48b888c5929b0e855` |
| `dashboard/dist/style.css` | **20,442 B** | `b705b8f33305f414dff612aa38b79d98885e8eecfb38513f39381fddd932664d` |
| `desktop/plugin.js` | **61,989 B** | 与 `index.js` 逐字节相同 |

连跑两次构建 sha256 一致（幂等）；颜色纪律由构建脚本强制（`src/` 与产物 0 处字面色值）。

- `plugins/weather/`、`plugins/weather-card/` **今日 0 文件改动**（`find -newermt` 实测），只读参考。
- `desktop-plugins/` 5 个目录原样，未删未改；未重启桌面端/网关。
- `scripts/_envcheck_tmp.py`（含明文 key 的环境自检残留）已删除，交付物里没有任何密钥。

## C4 还没做

- **没有真浏览器截图**：天气分区只做到「迷你 hook 层 126 项 + 真 React 19/jsdom 45 项」，未截图。
- **降水概率只对 Open-Meteo 有值**：和风 7d 只有 `precip`（毫米），彩云逐时 `pop` 是 `mm×100` 的近似（`weather_lib` 原语义）；两条路都不伪造概率，界面显示 `—`。
- **PM2.5 只有彩云有**：和风 `weather/now`、Open-Meteo 当前请求都不含颗粒物，界面显示 `—` 并注明「该数据源不提供颗粒物」。
- 面板要等桌面端重启才看得到（按约束未重启）；`desktop-plugins/hermes-console/plugin.js` 未同步（收尾卡处理）。
- C5 `/upgrade` 仍是 stub。

## 做到哪（C3 概览，留档）

**C3 全部交付项已完成，并用四套探针真实执行验证（102 + 31 + 37 + 52 = 222 项断言，0 失败）**，产物已落盘。

| 交付项 | 文件 | 状态 |
|---|---|---|
| 6 个路由搬到统一 router（`/api/plugins/hermes-console/overview/...`） | `dashboard/api_overview.py` | ✅ layout GET/PUT、system、analytics、cron、sessions + `/overview` 自检 |
| 布局文件原地沿用 | `LAYOUT_RELATIVE = ("plugins","home-dashboard","layout.json")` | ✅ 常量集中定义 + 注释说明原因；本卡跑完 mtime/md5 均未变 |
| 后端退化（旧路径 ImportError 顺手修） | 同文件 `_load()` 惰性 import | ✅ 走 `hermes_cli.web_routers/{status,analytics,cron,sessions}.py`；import 失败 → `{ok:false,code:"import_failed"}` |
| 超时 / 异常兜底 | `_await_async` / `_await_thread` + `_describe_error` | ✅ 轻查询 8s、重查询 25s；不裸抛、不 500 |
| 概览分区 UI | `src/sections/overview.js` | ✅ 4 张统计卡（带口径）+ cron 只读列表 + 会话只读列表（服务端分页）+ 看板挂件只读网格 |
| 拿不到的指标显示 `—` | 卡片 `tone: warn/error` + `没读到：<原因>` | ✅ 前端探针用「只有 cron 503」的用例证明：坏的那张显示 `—`，其它三张照常 |
| `PUT /layout` 保留但不做拖拽 UI | 同文件 | ✅ 写回能力保留（校验 + 临时文件 + 原子替换）；**拖拽编辑 UI 留给后续卡片**（见"还没做"） |

## 6 个路由实测（FastAPI TestClient，`agent=default`）

| 方法 | 路径 | HTTP | 响应前 200 字符 |
|---|---|---|---|
| GET | `/overview/layout` | **200** | `{"ok":true,"scope":{"kind":"shared_file","agent":"default","note":"布局只有一个文件（layout:home-dashboard/layout.json），所有 agent 共用；agent 参数不影响结果"},"source":"layout:home-dashboard/layout.json","data":{"path":"D:\…` |
| PUT | `/overview/layout`（故意传非法文档） | **400** | `{"ok":false,"scope":{"kind":"shared_file","agent":"","note":"布局只有一个文件，所有 agent 共用"},"source":"layout:home-dashboard/layout.json","error":{"code":"invalid_layout","message":"布局文档不合法：需要 version=1 且 widg…` |
| GET | `/overview/system` | **200** | `{"ok":true,"scope":{"kind":"host","agent":"default","note":"系统指标是宿主与本后端进程的读数，不按 agent 取数；agent 参数只回显"},"source":"hermes_cli.web_routers.status.get_system_stats","data":{"os":"Windows","os_release":"11…` |
| GET | `/overview/analytics` | **200** | `{"ok":true,"scope":{"kind":"profile","agent":"default","note":"按 agent=default 的 profile 取数"},"source":"hermes_cli.web_routers.analytics.get_usage_analytics","data":{"daily":[{"day":"2026-09-28","inpu…` |
| GET | `/overview/cron` | **200** | `{"ok":true,"scope":{"kind":"profile","agent":"default","note":"按 agent=default 的 profile 取数"},"source":"hermes_cli.web_routers.cron.list_cron_jobs","data":[{"id":"885a7f3f9153","name":"oai.sb 额度监控","e…` |
| GET | `/overview/sessions` | **200** | `{"ok":true,"scope":{"kind":"profile","agent":"default","note":"按 agent=default 的 profile 取数"},"source":"hermes_cli.web_routers.sessions.get_sessions","data":{"sessions":[{"id":"local-cdf9dcd0e27df904f…` |

`PUT` 用非法文档是为了**同时**验证两件事：400 + 结构化错误体，以及布局文件没有被写。
复跑：`python plugins/hermes-console/scripts/probe_overview.py`（52 项断言）。

## `GET /overview/system` 完整 JSON（真实输出）

```json
{
  "ok": true,
  "scope": {"kind": "host", "agent": "default", "note": "系统指标是宿主与本后端进程的读数，不按 agent 取数；agent 参数只回显"},
  "source": "hermes_cli.web_routers.status.get_system_stats",
  "data": {
    "os": "Windows", "os_release": "11", "os_version": "10.0.26300",
    "platform": "Windows-11-10.0.26300-SP0", "arch": "AMD64",
    "hostname": "SQ-WANGTIEJUN", "python_version": "3.11.15", "python_impl": "CPython",
    "hermes_version": "0.22.5", "cpu_count": 16,
    "memory": {"total": 34160373760, "available": 8116994048, "used": 26043379712, "percent": 76.2},
    "disk": {"total": 249590452224, "used": 186792534016, "free": 62797918208, "percent": 74.8},
    "cpu_percent": 8.1,
    "load_avg": [0.0, 0.0, 0.0],
    "uptime_seconds": 170648,
    "process": {"pid": 13812, "rss": 71413760, "create_time": 1793164806, "num_threads": 7},
    "psutil": true
  },
  "calibers": {
    "memory.percent": "psutil.virtual_memory().percent —— 宿主整机内存占用率，不是 Hermes 进程内存",
    "cpu_percent": "psutil.cpu_percent(interval=0.1) —— 采样 0.1 秒的瞬时值，不是平均值",
    "uptime_seconds": "int(now - psutil.boot_time()) —— 主机开机至今，不是 Hermes 运行时长",
    "disk.percent": "psutil.disk_usage(HERMES_HOME).percent —— 只算 Hermes 主目录所在盘",
    "process.rss": "psutil.Process().memory_info().rss —— 仅本 dashboard 后端进程的常驻内存",
    "load_avg": "psutil.getloadavg()/os.getloadavg() —— 宿主 1/5/15 分钟平均负载",
    "hermes_version": "hermes_cli.version.get_version_info().base_version —— 安装版本号",
    "cpu_count": "os.cpu_count() —— 逻辑核数"
  }
}
```

**每个数字的口径来源**（也是 UI 上那行"口径："文案的唯一来源）：

| 数字 | 口径 |
|---|---|
| `memory.percent = 76.2` | `psutil.virtual_memory().percent`：宿主**整机**内存占用率，不是 Hermes 进程内存 |
| `memory.used/total` | 同上，字节；UI 显示为 `已用 24.3 GB / 共 31.8 GB` |
| `cpu_percent = 8.1` | `psutil.cpu_percent(interval=0.1)`：**0.1 秒瞬时采样**，不是平均负载 |
| `cpu_count = 16` | `os.cpu_count()`：逻辑核数 |
| `disk.percent = 74.8` | `psutil.disk_usage(HERMES_HOME)`：**只算 Hermes 主目录所在盘**，不是全盘 |
| `uptime_seconds = 170648` | `now - psutil.boot_time()`：**主机开机至今**（≈1 天 23 小时），不是 Hermes 运行时长 |
| `process.rss = 71413760` | 仅**本 dashboard 后端进程**的常驻内存（≈68 MB），不是整机占用 |
| `load_avg = [0,0,0]` | Windows 上由 psutil 提供，三个值通常为 0（Windows 无 Unix loadavg） |
| `hermes_version = 0.22.5` | `get_version_info().base_version`：安装版本 |
| `psutil = true` | psutil 可用；为 false 时上述 CPU/内存/磁盘/开机时长**全部缺失**，UI 显示 `—` 并说明"宿主未提供读数" |

## `GET /overview/cron` 完整 JSON（节选首条 + counts，全量见探针落盘）

```json
{
  "ok": true,
  "scope": {"kind": "profile", "agent": "default", "note": "按 agent=default 的 profile 取数"},
  "source": "hermes_cli.web_routers.cron.list_cron_jobs",
  "counts": {"total": 12, "enabled": 11, "paused": 0},
  "data": [
    {
      "id": "885a7f3f9153", "name": "oai.sb 额度监控",
      "enabled": true, "paused_at": null, "paused_reason": null, "state": "scheduled",
      "next_run_at": "2026-10-28T14:00:00+08:00",
      "last_run_at": "2026-10-28T13:00:50.551036+08:00", "last_status": "ok", "last_error": null,
      "schedule": {"kind": "cron", "expr": "0 * * * *", "display": "0 * * * *"},
      "schedule_display": "0 * * * *",
      "profile": "default", "is_default_profile": true,
      "repeat": {"times": null, "completed": 1668},
      "scheduler_heartbeat_age_s": 19.97
    }
  ],
  "calibers": {
    "counts.total": "该 profile 的 cron/jobs.json 全部任务数（含已暂停、含已完成的一次性任务）",
    "counts.enabled": "enabled=true 且未暂停（paused_at 为空）的任务数",
    "counts.paused": "paused_at 非空的任务数（暂停的任务 enabled 仍可能为 true）",
    "next_run_at": "cron 调度器写入的下次执行时间；已暂停/已完成的任务可能为空",
    "last_status": "最近一次执行的终态；从未执行过时为空",
    "agent=all": "agent=all 时跨全部 profile 聚合（底层按 id 去重，default 优先）"
  }
}
```

**每个数字的口径来源**：

| 数字 | 口径 |
|---|---|
| `counts.total = 12` | `default` profile 的 `cron/jobs.json` 里全部任务（含已完成的一次性任务） |
| `counts.enabled = 11` | `enabled=true` **且** `paused_at` 为空；`paused_at` 非空的任务即使 `enabled=true` 也算暂停 |
| `counts.paused = 0` | `paused_at` 非空的任务数 |
| `next_run_at` | 调度器写入的下次执行时间；已暂停/已完成任务可能为空（UI 显示"无下次执行"） |
| `last_status = "ok"` | 最近一次执行的终态，不是本次请求的结果 |
| `scheduler_heartbeat_age_s = 19.97` | 该 profile 的 ticker 上次迭代距今秒数（调度器停摆时，`next_run_at` 停在过去只有它能解释） |

> 注：字段做了**投影**（只留 UI 展示需要的 16 个），`prompt` / `model_snapshot` / `provider_snapshot`
> 这类大字段默认不回传（12 个 job 全量约 20 KB，其中大半是 prompt）。要全量加 `?full=1`。

## `agent` 参数支持矩阵（真实情况，不是承诺）

| 数据 | 真的按 agent 取？ | 实现 | 响应里的 `scope.kind` |
|---|---|---|---|
| `system` | **否** | 宿主/进程级读数，无 profile 概念 | `host`（agent 只回显） |
| `layout` | **否** | 只有一个布局文件，所有 agent 共用 | `shared_file` |
| `analytics` | **是** | `profile=` 透传给 `get_usage_analytics`，按该 profile 的 `state.db` 聚合 | `profile`；`agent=all` → 400 `unsupported_agent` |
| `cron` | **是** | `profile=` 透传给 `list_cron_jobs`，读该 profile 的 cron store | `profile`；`agent=all` → `all`（跨 profile 聚合） |
| `sessions` | **是** | `profile=` 透传给 `get_sessions`，打开该 profile 的 `state.db` | `profile`；`agent=all` → 400 `unsupported_agent` |
| 作用域未定（前端没传 `agent`，例如 `/agents` 失败） | 只能取**本进程**那份 profile | `profile=None` | `current_profile`（并在 note 里写明是哪个 profile） |

实测差异（同一台机器同一时刻）：

| 取值 | cron total | sessions total |
|---|---|---|
| `agent=default` | 12 | 150 |
| `agent=apm-worker` | 0 | 23 |
| `agent=all`（仅 cron） | 23 | —（不支持） |

不存在的 agent（`?agent=__no_such_profile__`）→ **200 + `{ok:false, error:{code:"invalid_agent"}}`**，
message 是宿主原话（"not a valid profile name…"），**不会静默返回 default 的数据**。

## `layout.json` 没被改写（验收 3）

```
before : {'exists': True, 'mtime': 1789755473.4369829, 'size': 761, 'md5': '2bce31b6c859584d326e66175863a9e8'}
after  : {'exists': True, 'mtime': 1789755473.4369829, 'size': 761, 'md5': '2bce31b6c859584d326e66175863a9e8'}
```

另外 `find plugins/home-dashboard -newermt "2026-10-28 00:00"` → **0 个文件**（整卡没有碰过旧插件目录）。

## 产物字节数（最后一次构建，连跑两次 sha256 相同）

| 文件 | 字节 | sha256 |
|---|---|---|
| `dashboard/dist/index.js` | **44,663** | `2dd0fcaf2e66f26272f360553ebca58ce07e4d494c41e9f444f6013bfa80a6fa` |
| `dashboard/dist/style.css` | **12,805** | `b48216b534586f1a852a8c08fdd573c56f7e3aed9af40b31bc3f74e58cc38680` |
| `desktop/plugin.js` | 44,663 | 与 `dist/index.js` 逐字节相同 |

颜色纪律：构建脚本内置扫描 + 外部 grep 双确认，`src/` 与 `dist/` 里 0 处字面色值。

## 四套探针（都是真执行，不是退出码）

| 命令 | 覆盖面 | 结果 |
|---|---|---|
| `node scripts/build.mjs` | 构建 + 颜色纪律 + desktop 镜像一致性 | `BUILD-OK`，两次 sha256 相同 |
| `node scripts/probe-shell.mjs` | 迷你 hook 运行时展开组件树：外壳 + 概览（4 卡 / 口径行 / 行数 / 分页文案 / 单路 503 降级） | **102 PASS / 0 FAIL** |
| `node scripts/probe-dom.mjs` | 真 React 19 + react-dom + jsdom：真事件、真 DOM 节点、`disabled` 属性、卸载清理 | **31 PASS / 0 FAIL** |
| `python scripts/selftest.py` | manifest 契约、聚合入口、`/agents`、概览自检（6 子路由 + 4 宿主接口）、`plugins.enabled`、目录变动 | **37 PASS / 0 FAIL** |
| `python scripts/probe_overview.py` | **6 个路由真 HTTP**（TestClient）+ 降级（import 失败 / 超时）+ agent 作用域 + 布局文件未改写 | **52 PASS / 0 FAIL** |

## 顺手修掉的旧问题（旧 `home-dashboard/dashboard/plugin_api.py` 的）

1. **旧导入路径已死**：`from hermes_cli.web_server import get_system_stats / get_usage_analytics /
   list_cron_jobs / get_sessions` 四个函数 2026-10-26 已迁到 `hermes_cli.web_routers/*`，
   旧路径 `ImportError` → 旧面板那四张卡必然 500。新实现走新路径，且**请求内惰性 import**。
2. **裸抛**：旧路由直接 `return await get_system_stats()`，任何异常都是 500 栈。现在
   超时/异常统一转成 `{ok:false, error:{code, message}}`，并区分 `timeout` / `import_failed` /
   `unknown_agent` / `invalid_agent` / `upstream_http` / `upstream_error`。
3. **`PUT /layout` 非原子写**：旧代码直接 `write_text`，写一半崩掉就把老板的布局弄坏了。
   现在先校验 → 写 `.tmp` → `os.replace` 原子替换，失败清理临时文件。
4. **前端把"数据源降级"说成"连不上后端"**：`rest.js` 原来把 200+`{ok:false}` 当成网络错误。
   新增 `normalizeBodyError()` 按后端的 `error.code` 说话（`取数超时` / `宿主接口不可用` /
   `这个 agent 取不到数据` …），探针里专门断言"不再说连不上后端"。

## 三个容易踩的 UI 细节（都在实现里处理了）

1. **换 agent 回到第 1 页**：否则在 default 的第 3 页切到只有 20 个会话的 profile，会拿到空页、
   而空列表里不渲染分页器 —— 用户会以为"这个 agent 没会话"。`useEffect([agent]) → setOffset(0)`。
2. **越界页也给出口**：万一还是拿到空页（数据被归档/清理），空态标题变成「这一页没有内容了」
   并给一个「回到第一页」按钮，不是一个死胡同。
3. **统计卡与列表共用同一条请求**：会话数卡与下面那个列表读同一个 `useQuery` key，
   不是各拉一次 —— 概览一共 5 条请求（system / analytics / cron / layout / sessions），
   切分区回来全部命中缓存（探针实测 before=5 after=5）。

## 还没做 / 留给后续

1. **`PUT /layout` 的拖拽编辑 UI**：本卡只保留写回能力（后端 + 校验 + 原子写），
   **概览分区里不做拖拽**（避免范围爆炸）。看板挂件这一块是**只读网格预览**。
   拖拽编辑留给后续卡片；那时前端只需调 `PUT /overview/layout`，后端不用改。
2. **C4 / C5**：`/weather`、`/upgrade` 仍是 `{"ok":true,"stub":true}`。
3. **桌面端未重启**（按约束）：`/console` 要重启后才能在真实宿主里看到；
   重启后请顺手确认概览分区四张卡与两个列表有数据、无 console 报错。
4. **`desktop-plugins/hermes-console/plugin.js` 未同步**：`scripts/install.ps1` 已备好，
   按约束不重启、不写宿主门文件（C1 遗留项，收尾卡统一处理）。
5. **`/overview/analytics` 偏慢**：实测 2.4–4.2 秒（`InsightsEngine` + 全表聚合，
   default profile 217 个会话、46 亿 cache_read token）。已给重查询单独一档 25 秒超时，
   没有把"慢"当"坏"；真要提速得在宿主侧加缓存或增量聚合，属于宿主优化，不在本卡范围。
6. **会话列表是服务端分页（limit/offset）**，没有做虚拟滚动 —— 20 条一页够用；
   将来要"一屏到底"再加虚拟滚动，后端 `limit` 上限 100。
7. **没有真浏览器截图**：C2 用过 Edge headless 截图人工核对，C3 的概览 UI 只做了
   逻辑层（probe-shell）+ 真 DOM 层（probe-dom，真 React 19 + jsdom）验证，没有截图。
   要看图得等桌面端重启后在 `/console` 里看。

## 复跑（五条命令）

```bash
node   plugins/hermes-console/scripts/build.mjs            # BUILD-OK，打印字节数 + sha256
node   plugins/hermes-console/scripts/probe-shell.mjs      # 102 项，SHELL-PROBE-OK
node   plugins/hermes-console/scripts/probe-dom.mjs        # 31 项，DOM-PROBE-OK
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
       plugins/hermes-console/scripts/selftest.py          # 37 项，SELFTEST-OK
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
       plugins/hermes-console/scripts/probe_overview.py    # 52 项，OVERVIEW-PROBE-OK
```

> `probe_overview.py` 与 `selftest.py` 会把 `<home>/hermes-agent` 插进 `sys.path`：
> 生产里后端就是从那个目录起的（`sys.path[0]` 就是它），概览取数要 import
> `hermes_cli.web_routers.*`（间接用到顶层模块 `hermes_yaml`）。不补这一条会出现
> "探针能过、面板全灰"的假绿。

---

# 附：C2 阶段记录（上一张卡，保留备查）

更新时间：2026-10-28 13:00 (+08:00) · 本卡：C2 统一外壳 + 设计 tokens + 三态与作用域

**C2 全部交付项已完成，并通过三套探针真实执行验证（79 + 27 + 29 = 135 项断言，0 失败）**。
C3 在这套外壳上只换了分区内容、给 `rest.js` 补了降级归一化，外壳/三态/缓存/作用域逻辑没改
（C2 的探针断言仍在跑，只有"概览是 Empty 态"这类被 C3 自然取代的断言改成了"概览是真数据"）。

| 交付项 | 文件 | 状态 |
|---|---|---|
| 设计 tokens（唯一尺寸来源） | `src/tokens.css` | ✅ 间距 `--s1..--s8`、字号 `--fs-xs..xl`、圆角 `--r-sm/md/lg`；颜色 0 处 |
| 外壳（左栏导航 + 顶栏 + 内容区） | `src/shell.js` | ✅ 180px 左栏、选中态左描边+提亮、作用域选择器、刷新、连接状态点 |
| `ctx.rest` 封装 | `src/lib/rest.js` | ✅ GET/写请求 + 错误归一化；body 传字符串直接拒（防 422 的双重序列化） |
| 三态组件 | `src/lib/states.js` | ✅ `Skeleton` / `Empty` / `ErrorState` |
| 格式化 | `src/lib/format.js` | ✅ 相对时间、千分位、KB/MB、百分比（C3 加了 `compact` / `duration`） |
| 构建链 | `scripts/build.mjs` | ✅ 一次性构建 + `--watch`；产物幂等、含颜色纪律检查 |

C2 的交互决定（C3 继续沿用，未改）：切分区不丢滚动（面板只切 `visibility`，不卸载）、
切分区不重拉（`lib/query.js` 模块级缓存 + inflight 去重）、切作用域只让受影响区块进骨架、
窄窗（<900px）容器查询收成图标条、刷新按钮 800ms「已刷新」反馈。

作用域持久化：`scope.agent`（宿主命名空间化为 `hermes.plugin.hermes-console.scope.agent`），
首次 render 前同步读、用户改动即写、失效 id 自动回落后端 `current`；另存 `nav.section`。

已知边界（C2 记录，仍然有效）：

- **没有 `@tanstack/react-query`**：宿主插件 import 白名单只有 `react` / `react/jsx-runtime` /
  `@hermes/plugin-sdk`，所以 `lib/query.js` 是同一语义的 120 行实现。
- **`ctx.rest` 非 2xx 会 reject**（不是 resolve 错误体），错误消息形如 `'<status>: <body>'`；
  `lib/rest.js` 按此解析。C3 补了"200 + `{ok:false}`"这条降级路径（见上文"顺手修掉的旧问题 4"）。
- **`api_agents` 主目录每次请求重新解析**：新增/删除 profile 目录后下一次请求就能看到，不需要重启。
- **`--watch` 不刷新 `desktop/plugin.js`**：发布前跑一次不带 `--watch` 的构建。

C2 的视觉验证：用宿主自己的主题变量渲染，Edge headless 截了浅色/深色/780px 窄窗三张图人工核对
（截图脚本没有进插件交付物）。
