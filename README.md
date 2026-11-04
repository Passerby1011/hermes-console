# hermes-console

六个自制面板插件合并成的一个统一面板（阶段①）。一个侧栏标签 `/console`，内部左栏分区。

- **C1**（已完成）：骨架 + 构建链 + 9-agent 枚举端点。
- **C2**（已完成）：统一外壳 + 设计 tokens + 三态 + 作用域。
- **C3**（已完成）：概览分区接真数据 —— home-dashboard 的 6 个路由搬进来（布局文件原地沿用），
  4 张统计卡 + cron / 会话只读列表 + 看板挂件只读预览。
- **C4**（已完成）：天气分区从零建前端 —— weather-card 的 4 个路由搬进来
  （`/weather/{now,daily,hourly,at}` + 自检），当前天气卡 / 逐小时 / 多日 / 「N 小时后」/
  城市输入全部接真数据，每条数据都标**来源 + 取数时间 + 降级过程**。
- **C5**：把 upgrade 分区的真实数据搬进来（仍是占位 stub）。
- **C9b**（已完成）：Advisor 审核台 —— 建议台账（读 `plugins/advisor/audit.py`，不自己读日志）
  + 采纳/拒绝（拒绝必填原因，只写决定不执行）+ 策略设置（`blocker_forced_delivery` 只读）。
- **M1b**（已完成）：模块注册核心 —— 每个功能模块 = `dashboard/modules/<id>/` 一个自包含目录
  （`manifest.json` 声明 + 可选 `api.py`），删目录即下线；单模块 manifest 坏只进 `health().failed`；
  `GET /modules?agent=<id>` 算出该 agent 的启用集（含 `reason`）。旧 `DOMAIN_MODULES` 聚合并行不动。
  规范见 [`dashboard/modules/README.md`](./dashboard/modules/README.md)。

- **M4a**（已完成）：导航动态化 —— tab 从 `GET /modules` 取（按 `nav_order` 排序、只渲染 `enabled`），
  含 `FALLBACK_SECTIONS` 全量降级 + 未知模块占位页。
- **M4b0**（已完成）：后端设置端点 —— `GET /agents/{id}/plugins`（三态）、
  `GET/PUT /agents/{id}/plugins/{pid}/settings`（脱敏 + 来源层 / 白名单 + 乐观锁）。
- **M4b1**（已完成）：**设置分区** —— `src/sections/settings.js`（`nav_order 70`、`default_enabled true`）。
  按 agent 显示插件三态（`已启用` / `已被禁用` / `不在该 Agent 可用范围`，第三态**不渲染开关**），
  命名空间分开（`plugin:<id>` vs `module:<console>/<id>`），配置每项标来源层
  （`模块默认` / `全局` / `本 Agent 覆盖`）+ `恢复为全局值`，`masked_keys` 只读且掩码永不进写请求，
  409 提示「配置已被别处改动」+ 刷新（不静默重试），4xx/5xx 原样透出。

当前进度与已知边界见 [`STATUS.md`](./STATUS.md)。

## 目录

```
plugins/hermes-console/
├── dashboard/
│   ├── manifest.json      # name/label/tab{path:/console,position:end}/entry/css/api/version
│   ├── plugin_api.py      # 只聚合：sys.path → import 4 个域模块 → include_router
│   ├── api_agents.py      # GET /agents —— 动态枚举，禁写死（主目录每请求重新解析）
│   ├── api_overview.py    # 概览 6 路由（搬自 home-dashboard；布局文件原地沿用）
│   ├── api_weather.py     # 天气 5 路由（搬自 weather-card；显式标 source/fetched_at）
│   ├── api_upgrade.py     # /upgrade/*（6 条真实路由，搬自 upgrade-console）
│   ├── api_advisor.py     # Advisor 审核台 7 路由（C9b：只读台账 + 写决定 + 白名单设置）
│   ├── core/              # M1a+M1b 配置/注册核心：scope（agent→路径）/ settings（分层合并）/ registry（模块注册）
│   ├── modules/           # M1b 模块目录（目录即模块）：overview/ weather/ + README（manifest 规范）
│   └── dist/              # 构建产物，禁止手改
├── src/                   # 唯一源码（ESM）
│   ├── main.js            # 插件对象 + register(ctx)：routes + sidebar.nav，绑定 rest/storage
│   ├── shell.js           # 外壳：左栏分区导航 + 顶栏（标题/作用域/刷新/连接点）+ 面板容器
│   ├── tokens.css         # 设计 tokens（唯一尺寸来源：间距/字号/圆角）
│   ├── style.css          # 全部样式（颜色只允许 var(--ui-*)）
│   ├── lib/rest.js        # ctx.rest 门面 + 错误归一化（body 只收对象；结构化降级单独一条路）
│   ├── lib/query.js       # 请求缓存（切分区不重拉、作用域切换只让受影响区块进 skeleton）
│   ├── lib/storage.js     # ctx.storage 偏好读写（scope.agent / nav.section）
│   ├── lib/scope.js       # agent 枚举 + 默认当前 profile + 持久化 + /agents 失败退化
│   ├── lib/states.js      # 三态：Skeleton / Empty / ErrorState
│   ├── lib/format.js      # 相对时间 / 千分位 / 亿·万 / 时长 / KB-MB
│   └── sections/          # overview（真数据）/ weather / upgrade（仍占位）
│                          # + agents / autogroup / advisor（Advisor 审核台，C9b，自包含）
├── desktop/plugin.js      # 由 src 构建而来的镜像（与 dist/index.js 字节相同）
└── scripts/
    ├── build.mjs          # src → dist + desktop/plugin.js（幂等、含颜色纪律检查、支持 --watch）
    ├── probe-shell.mjs    # 外壳 + 概览行为验收·逻辑层（102 项断言，真的求值 dist/index.js）
    ├── probe-dom.mjs      # 外壳 + 概览行为验收·真 DOM 层（31 项断言，真 React 19 + jsdom）
    ├── probe_overview.py  # 概览 6 路由真 HTTP 验收（TestClient + 降级 + agent 作用域 + 布局未改写）
    ├── probe_advisor.py   # Advisor 7 路由真 HTTP 验收（分页/详情/决定落盘/设置/降级/回归）
    ├── probe_registry.py  # M1b 模块注册核心验收（发现/坏模块隔离/删目录下线/启用集/依赖//modules）
    ├── probe_console_settings_api.py  # M4b0 设置端点（三态/脱敏带来源/拒掩码/乐观锁）
    ├── probe_plugin_toggle_api.py     # M4b2 插件启停端点（黑名单优先/官方准入/幂等/模块开关白名单）
    ├── probe-advisor.mjs  # Advisor 分区真 DOM 验收（61 项，真 React 19 + jsdom）
    ├── selftest.py        # 后端 + manifest 契约 + /agents 退化 + 概览自检
    └── install.ps1        # 可选：门文件拷贝 + plugins.enabled 写入（由操作者执行）
```

## 构建

```bash
node plugins/hermes-console/scripts/build.mjs            # 一次性构建（默认）
node plugins/hermes-console/scripts/build.mjs --watch    # 开发时监听（不刷新 desktop 镜像）
```

- 产物：`dashboard/dist/index.js`、`dashboard/dist/style.css`、`desktop/plugin.js`
- esbuild 从 `hermes-agent/node_modules` 取（本脚本 spawn 它的 CLI 入口，绕开裸模块解析）；
  可用 `HERMES_ESBUILD=<path>` 覆盖。
- 构建前扫描 `src/**` 与产物，出现 `#rrggbb` / `rgb()` / `hsl()` / 字面 `black|white` 即失败
  —— 颜色只能来自主题变量。
- 同一输入两次构建字节相同（脚本无时间戳），因此 `dist/` 可安全重复生成。

## 验收

```bash
node plugins/hermes-console/scripts/probe-shell.mjs      # SHELL-PROBE-OK（102 项）
node plugins/hermes-console/scripts/probe-dom.mjs        # DOM-PROBE-OK（31 项，真 React + jsdom）
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/selftest.py          # SELFTEST-OK
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/probe_overview.py    # OVERVIEW-PROBE-OK（真 HTTP + 降级）
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/probe_weather.py     # PROBE-WEATHER-OK（4 路由真 HTTP + 掐源降级）
node plugins/hermes-console/scripts/probe-advisor.mjs     # ADVISOR-DOM-PROBE-OK（61 项，真 DOM）
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/probe_advisor.py      # ADVISOR-PROBE-OK（76 项，真 HTTP + 落盘 + 降级）
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/probe_registry.py     # REGISTRY-PROBE-OK（91 项，模块注册核心）
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/probe_console_settings_api.py  # CONSOLE-SETTINGS-API-PROBE-OK
D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
     plugins/hermes-console/scripts/probe_plugin_toggle_api.py     # PLUGIN-TOGGLE-API-PROBE-OK（120 项）
```

`probe-shell.mjs` 用迷你 hook 运行时装真的把 `dist/index.js` 展开成节点树，点击真实回调，
验证分区切换、缓存命中、作用域切换的局部骨架、`ctx.storage` 写入，以及 `/agents` 503 时的退化。
C3 起它还会喂一份按后端真实响应形状构造的概览桩数据，断言 4 张统计卡、口径行、cron/会话行数、
分页文案，以及"只有 cron 这一路 503 时其它卡照常、坏的那张显示 `—`"。

## 后端

`plugin_api.py` 是 manifest `api` 键指向的**唯一**被加载文件，其余模块靠它把本目录插进
`sys.path` 再 import。挂在 `/api/plugins/hermes-console/` 下。

- `GET /agents` → `{"agents":[{"id","label","is_default","has_config"}],"current"}`，
  agent 由 `<home>/profiles/` 下含 `config.yaml` 的目录 + `default` 动态枚举（当前 9 个）。
  新增一个 profile 目录自动出现，不需要改代码、不需要重启。
- 注意 `HERMES_HOME` 在 worker/gateway 进程里是 `<home>/profiles/<p>`（profile 语义），
  所以 `api_agents.hermes_home()` 按布局正向探测共同父目录，不直接采信该变量。
- **概览分区（C3）**：`/overview/{layout,system,analytics,cron,sessions}` + `/overview` 自检。
  取数一律走 `hermes_cli.web_routers/{status,analytics,cron,sessions}.py`（旧
  `hermes_cli.web_server` 里的同名函数已迁走，旧路径 ImportError），且是**请求内惰性 import**：
  某个域不可用只让那一张卡进错误态，不会让插件整体挂不上。
  失败一律 `{ok:false, error:{code,message}}`（数据源坏 → 200，请求本身非法 → 400），不裸抛。
- **天气分区（C4）**：`/weather/{now,daily,hourly,at}` + `/weather` 自检。取数仍走
  `scripts/weather_lib.py`（彩云 → 和风 → Open-Meteo，**降级顺序原样不动**），本模块只读不改；
  每条响应都带 `source` / `source_label` / `fetched_at` / `sources`（三源各自 ✓ 或 ×空）/
  `units`，来源不靠猜（实况用 weather_lib 自报的 `source`，逐日/逐时用只读探针记录三源返回值后判定）。
  地点默认取 `HERMES_WEATHER_PLACE`（缺省按后端配置的地点，可设为任何城市或「纬度,经度」）
  都可临时查。`pm25` 只有彩云命中时才有，和风/Open-Meteo 那条路不伪造，前端显示 `—`。
- **Advisor 审核台（C9b）**：`/advisor/{advice,advice/{id},decision,summary,settings}` + 自检。
  数据源**只有** `plugins/advisor/audit.py`（C9a 的 append-only 台账），本模块不读日志、不拼状态。
  `advisor.audit` 是**请求内惰性 import**（先 `import advisor.audit`，不行再按文件路径加载
  `plugins/advisor/audit.py`，且不执行该包的 `__init__.py`，避免把整个 advisor 运行时拉进 dashboard
  进程）；加载失败一律 503 `{ok:false,error:{code:"upstream_error"}}`，控制台其它分区不受影响。
  `POST /advisor/decision` **只往台账追加一行决定**（`rejected` 缺 `reason` → 400），不执行建议内容；
  `PUT /advisor/settings` 只写白名单五字段，`blocker_forced_delivery` 是只读常量，改成 `false` → 400
  「单向阀门不可关闭」。台账目录由 `audit.audit_dir()` 决定（`ADVISOR_AUDIT_DIR` 可覆盖），
  与主目录不一致时响应 `notes` 会写明两边路径。
- **模块注册（M1b）**：`GET /modules?agent=<id>` →
  `{ok:true, data:{agent, modules:[{id,label,nav_order,section,enabled,reason,…}], health:{loaded,failed,…}}}`。
  数据全部来自 `core/registry.py`（扫 `dashboard/modules/*/manifest.json`）；**含未启用的模块**并给出
  `reason`（模块默认 / 全局设置 / 该 agent 设置 / 缺依赖）。非法 `agent` → 400 + 合法 id 列表，
  注册核心不可用 → 503，**都不是 500**。与上面的 `DOMAIN_MODULES` 旧聚合**并行**：旧结构与
  `GET /health` 一字未改，`/health` 仍只报域模块加载结果。
- 各域路由从 C3 起接受可选 `?agent=<id>` 作用域参数（缺省 `default`）；前端
  `lib/rest.js` 的 `get(path,{agent})` 统一拼这个参数。`/overview/cron` 额外接受
  `agent=all` 做跨 profile 聚合；`system`（宿主级）与 `layout`（单一文件）在响应里用
  `scope.kind` 明说"与 agent 无关"，不冒充。

## 启用

`hermes config set plugins.enabled` 是整表覆盖：必须读全量 → 追加 → 整体写回 → 读回核对。
`scripts/install.ps1` 封装了这一步，但**由操作者执行**；后端路由只在桌面端启动时挂载，
改完需要重启桌面端。
