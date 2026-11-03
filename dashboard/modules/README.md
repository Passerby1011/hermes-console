# dashboard/modules —— 模块目录规范（M1b）

**一个功能模块 = 这里的一个目录。** 目录里放 `manifest.json`（必需）和 `api.py`（可选，后端路由）。
**删掉目录 = 该模块下线**：不需要改 `plugin_api.py`、不需要改前端、不需要改任何清单文件。

注册与启用集的实现在 [`dashboard/core/registry.py`](../core/registry.py)：

| 函数 | 作用 |
| --- | --- |
| `discover_modules()` | 扫描本目录，返回**可用**的 `ModuleSpec`（按 `nav_order` 排序） |
| `enabled_modules(agent_id)` | 某 agent 启用的模块（`ModuleSpec` 列表） |
| `resolve_modules(agent_id)` | 同上，但**含未启用的**，每条带 `reason` |
| `health()` | `{"loaded": [...], "failed": {目录名: 原因}}` |
| `module_status(agent_id)` | `GET /modules` 里 `modules` 数组的内容 |

对外 HTTP：`GET /api/plugins/hermes-console/modules?agent=<id>`。

---

## manifest.json 字段

| 字段 | 类型 | 必填 | 缺省 | 含义 |
| --- | --- | --- | --- | --- |
| `id` | string | **是** | — | 模块唯一标识。小写字母开头，1–64 位小写字母/数字/`_`/`-`。与 settings 路径 `modules.<id>.enabled` 一致；重复会让**后发现的**那份进 `failed`。建议与目录名相同（删目录即下线靠它对齐）。 |
| `label` | string | **是** | — | 人可读名字（导航里显示的中文名）。空串视为缺失。 |
| `version` | string | 否 | `"0.0.0"` | 模块版本号（自述用；升级/兼容判断的锚点）。 |
| `nav_order` | int | 否 | `1000` | 导航排序，小的在前；相同则按 `id` 字典序。 |
| `default_enabled` | bool | 否 | `true` | 该模块的**出厂默认**开关。优先级最低，会被全局 / 该 agent 的 settings 覆盖。 |
| `api_module` | string \| null | 否 | `null` | 后端模块名（`dashboard/api_*.py` 的模块名，例如 `"api_overview"`）。纯前端模块留空/不写。 |
| `section` | string \| null | 否 | `null` | 前端分区 id（`src/sections/*.js` 注册的 key）。纯后端模块留空/不写。 |
| `settings_schema` | object | 否 | `{}` | 该模块的设置项声明，供设置界面（M4）渲染。键是设置名，值是 `{type, default, label, ...}`；`enabled` 是注册核心自用的开关，建议声明出来。 |
| `requires` | string[] | 否 | `[]` | 依赖的其它模块 `id`。**依赖未启用 ⇒ 本模块不启用**（`reason` 会点名缺哪个 / 哪个没启用 / 成环）。 |

### 校验与故障隔离

单个目录出现下列任一情况，**只有它自己**进 `health().failed`（键是**目录名**，值是中文原因），
其它模块照常加载：

* 缺 `manifest.json`
* `manifest.json` 不是合法 JSON（含 BOM 也能读）
* 根不是对象
* 缺必填字段 `id` / `label`，或 `id` / `label` 是空串
* 字段类型不对（`nav_order` 非整数、`default_enabled` 非布尔、`settings_schema` 非对象、`requires` 非字符串数组…）
* `id` 与另一个模块重复（后发现的这份失败）

### 启用优先级（低 → 高）

1. `manifest.json` 的 `default_enabled`
2. 全局 settings：根 `config.yaml` 的 `plugins.entries.hermes-console.settings.modules.<id>.enabled`
3. 该 agent settings：`<home>/profiles/<agent>/config.yaml` 的同路径

合并由 [`dashboard/core/settings.py`](../core/settings.py) 的 `effective_settings` 完成（每个值带
`source`，`/modules` 的 `reason` 就把它翻成人话）。

---

## 最小可用示例

`dashboard/modules/hello/manifest.json`：

```json
{
  "id": "hello",
  "label": "你好"
}
```

就这样：其余字段全部取缺省（`version="0.0.0"`、`nav_order=1000`、`default_enabled=true`、
无后端、无前端分区、无依赖、无设置项），模块会立刻出现在 `discover_modules()` 与
`GET /modules` 里，并因 `default_enabled=true` 对所有 agent 默认启用。

带后端 + 设置项的完整示例见同目录的 [`overview/manifest.json`](overview/manifest.json) 与
[`weather/manifest.json`](weather/manifest.json)。

---

## 当前模块（M3：6 个分区全部登记完成）

| 目录 | id | nav_order | 默认 | 后端 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `overview/` | `overview` | 10 | 开 | `api_overview` | 概览分区，`api.py` 是**薄封装**：`router = api_overview.router`，逻辑没搬 |
| `weather/` | `weather` | 20 | 开 | `api_weather` | 天气分区，同上 |
| `models/` | `models` | 25 | **关** | `api_models` | 模型（V5：`/models/*` 13 条路由：overview/health/providers/providers{agent}/models/snapshots/model-library/apply/rollback/presets×4）。**apply/rollback 会改真实 profile 的 config.yaml**（写前按字节快照 + 写后回读校验 + 服务端 `confirm=true` 二次确认，一次只作用一个 agent，不做批量），凭据经 `mask_sensitive` 打码，预设只写 console 自己的 `data/presets.json`，出厂关闭 |
| `agents/` | `agents` | 30 | 开 | `api_agents` | Agent 总览，纯只读，同上 |
| `autogroup/` | `autogroup` | 40 | **关** | `api_autogroup` | 项目分组；扫描会写建议记录，按"带写默认关"出厂关闭 |
| `advisor/` | `advisor` | 50 | **关** | `api_advisor` | Advisor 审核台；有 decision / settings 写入，出厂关闭 |
| `upgrade/` | `upgrade` | 60 | **关** | `api_upgrade` | 升级（V2：`/upgrade/*` 6 条真实路由：scan/readiness/prepare/restore/wait/kill），有 cron 写操作，出厂关闭 |
| `vcc/` | `vcc` | 80 | **关** | `api_vcc` | 上下文引擎（V3a：`/vcc/*` 6 条路由：overview/log/compaction/engine/search_archived/engine_status，**引擎按 agent 解析**），`POST /vcc/engine` 会写 `context.engine`，出厂关闭 |
| `remote/` | `remote` | 90 | **关** | `api_remote` | 远端后端（V4：`/remote/*` 5 条路由：agents/status/agent_start/agent_stop/credentials，**agent 列表动态枚举** + 凭据脱敏），启停会真的拉起/停掉对外 `hermes serve` 进程，出厂关闭 |

> **`vcc/`、`remote/` 与 `models/` 目前只有 `manifest.json`，没有 `api.py`。** 它们的后端在
> `dashboard/api_vcc.py` / `dashboard/api_remote.py` / `dashboard/api_models.py`，由 `plugin_api.py` 的
> `DOMAIN_MODULES` 聚合注册（路由前缀 `/vcc` / `/remote` / `/models`）。不建 `api.py` 是有意的：
> 模块登记（本目录）与域路由（`api_*.py`）是两件事。

> **「模型」分区的写操作有三道闸**（下一任改它之前先读这段）：① 界面上的「当前 → 目标」对比条 +
> 二次确认；② 服务端 `confirm=true` 强制（缺了就 400 `confirmation_required`）；③ 写前按字节快照、
> 写后回读校验，不符则用快照自动还原并报 500 `verify_mismatch`（带 `restore_path`）。
> 它**拒绝**旧插件的 `targets: [...]` 批量写法（400 `multi_agent_forbidden`）：一次只作用一个 agent。

> **开关策略：纯只读默认开，带写操作默认关。** 想给某个 agent 打开，把
> `plugins.entries.hermes-console.settings.modules.<id>.enabled = true` 写进该 agent 的 `config.yaml`。

> **不搬逻辑。** 这一版只做"登记"；每个 `api.py` 只是把现有 `dashboard/api_*.py` 的 `router`
> 引用过来（注意导入的两条路径：包上下文 / 裸名）。`plugin_api.py` 的旧 `DOMAIN_MODULES`
> 聚合仍然原样工作，两套结构并行。

## 新增一个模块（清单）

1. `mkdir dashboard/modules/<id>`，写 `manifest.json`（至少 `id` + `label`）
2. 需要后端：写 `api.py`，暴露模块级 `router`（`APIRouter()`），并在 `manifest.api_module` 写明后端模块名
3. 跑 `python scripts/probe_registry.py`（会检查发现、隔离、启用集、依赖、`/modules` 端点）
4. 想让某个 agent 关掉它：把 `plugins.entries.hermes-console.settings.modules.<id>.enabled = false`
   写进该 agent 的 `config.yaml`（走 `core.settings.save_setting`，**不要手写 YAML**）
