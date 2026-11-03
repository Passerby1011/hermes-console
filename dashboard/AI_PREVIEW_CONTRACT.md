# AI 分类预览契约（`ai-preview/1`）

引擎卡（`autogroup_ai.py`，只产建议）与写卡（`api_autogroup_ai_apply.py` / `api_autogroup_ai_undo.py`）
之间**唯一**的数据约定。规范实现见 `autogroup_ai_contract.py`（纯函数、无 IO），本文件是给人看的副本。

## 1. 谁负责什么

| 环节 | 谁做 | 写库？ | 建目录？ |
|---|---|---|---|
| 分类（标题+摘要+cwd → 建议项目/置信度/理由/新建候选） | 引擎 `autogroup_ai.py` | ❌ | ❌ |
| 预览落库（服务端批次 + 逐条快照） | `api_autogroup_ai_apply.py` `POST /autogroup/ai/preview` | 只写 AI 三张表 | ❌ |
| 确认执行（批次 + 逐条选中 + `confirm=true`） | `POST /autogroup/ai/confirm` | ✅ 归属表 + 台账 | ❌ |
| 新建项目（独立一次确认） | `POST /autogroup/ai/project` | ✅ projects.db | ✅ `<home>/projects/<slug>` |
| 逐条回读 | `GET /autogroup/ai/readback/{batch_id}` | ❌ | ❌ |
| 批次撤销 | `POST /autogroup/ai/undo` | ✅ 逐字段还原 | 仅空目录且无引用时清理 |

## 2. 引擎输出（引擎 → 预览接口）

```json
{
  "contract_version": "ai-preview/1",
  "generated_at": 1793340000.0,
  "engine": {
    "name": "autogroup_ai",
    "model": "<真实模型 id>",
    "elapsed_ms": 1234,
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    "usage_estimated": false,
    "usage_note": "缺失 usage 时的估算口径"
  },
  "items": [
    {
      "session_id": "20261030_...",
      "title": "会话标题",
      "snapshot": {"cwd": "D:\\...", "message_count": 4, "last_activity_at": 1793339000.0},
      "suggested": {
        "project_slug": "memory",
        "project_name": "记忆",
        "confidence": 0.86,
        "reason": "一句话理由",
        "new_project": null,
        "support_session_ids": []
      },
      "unjudged_reason": null
    }
  ],
  "notes": ["..."]
}
```

`new_project` 非空时：`{"slug": "kebab-case", "name": "中文名"}`，且 `support_session_ids` 必须
是**至少 3 个不同会话 id**（同主题证据）。

**兼容形状**：若引擎按原卡 prompt 直接输出顶层平铺字段（`project_slug` / `confidence` / `reason` /
`new_project` / `support_session_ids`，无 `suggested` 包裹），服务端同样接收（字段名一致，只是换个位置）。

## 3. 校验口径（服务端，`normalize_item` / `normalize_preview`）

| 情况 | 结果 |
|---|---|
| `confidence` 不是数字 / 是 bool / 越界 [0,1] | `ContractError` → 400 `invalid_field_type` / `confidence_out_of_range` |
| `project_slug` 形状非法 | 400 `invalid_slug` |
| `project_slug` 不在现有项目里且没有 `new_project` | 400 `unknown_project`（**禁止猜项目**） |
| `new_project.slug` 与已有项目撞名 | 400 `new_project_conflicts_existing`（那是改名/覆盖） |
| 会话已 applied（含原 56 条） | 从 `items` 剔除，进 `excluded` 并写明原因 |
| 同一会话重复出现 | 保留第一条，其余进 `excluded` |
| `confidence < 0.5` | `tier=unjudged`，`write_policy.can_write=false`，进「无法判定」清单 |
| `0.5 ≤ confidence < 0.8` | `tier=suggest`，确认后只写 `suggested`，**不动 cwd** |
| `confidence ≥ 0.8` | `tier=apply`，确认后写 `applied` 并同步 cwd |
| 新建候选支持 < 3 条 | 自动降级 `suggest`（`can_write=false`） |
| 新建候选项目尚未创建 | `can_write=false`（先走 `POST /autogroup/ai/project`） |

**服务端不采信引擎自报的 `tier` / 归属现状**：分档由服务端重算，`state`（归属行 9 列指纹）与
`session_snapshot`（会话存在性/cwd/消息数）由服务端在预览落库时自读并冻结进批次。

## 4. 预览批次（服务端 → UI）

`POST /autogroup/ai/preview` 返回 `batch_id`（`aip-...`）与规范化预览。UI 逐条展示：
`title / current(slug,status) / suggested(slug,name,confidence,reason,new_project) / tier /
status_if_confirmed / selectable / selectable_why / stale_warning / unjudged_reason`。

确认：`POST /autogroup/ai/confirm {"batch_id": "...", "confirm": true, "selected": ["<session_id>", ...]}`

- 缺 `confirm=true` ⇒ 400 `confirmation_required` + 逐条预览（一行都不写）；
- `selected` 里有不在该批次的 id ⇒ 400 `selection_rejected`（整批不写）；
- `unjudged` 或「新建项目未建」的条目不可选；
- 批次状态 `done` / `undone` ⇒ 409 `batch_consumed`（要重来请重新预览）。

## 5. 执行语义

1. 逐条、按批次内顺序执行，**每条写前重新校验**：会话仍在 / 归属行指纹未变 / 目标项目仍在 /
   `primary_path` 是真实目录；
2. 任何一条不符 ⇒ **跳过该条 + 暂停整批**，返回 `results`（逐条 outcome）、`not_executed`、
   `paused{why,done,remaining}`；一条都没写成时 HTTP 409 `batch_paused`；
3. 台账 `console_ops`：header（`op='ai.apply'`，带 `batch_id`）+ 逐会话行（`row_before` 写前整行、
   `cwd_before` 原值、`cwd_written`）；归属写入与台账**同事务**；
4. cwd 同步：`UPDATE sessions SET cwd=? WHERE id=? AND cwd IS ?` + `changes()==1` + 回读；
   失败 ⇒ 归属行按台账回滚（**没有跨库事务，绝不谎报原子性**）；
5. 写后逐条回读（slug/status/cwd），不一致即 `readback_mismatch` 并暂停。

## 6. 撤销语义

`POST /autogroup/ai/undo {"op_id"|"batch_id", "confirm": true}`

- `row_before=null` ⇒ 删除该归属行（**恢复为「不存在」**，不是退回 suggested）；
- `row_before` 有值 ⇒ 9 列整行 `INSERT OR REPLACE` 逐字段还原；
- cwd 只在 `cwd_written=1` 时按 `WHERE id=? AND cwd IS ?` 回滚（期望值来自 `cwd_backup.new_cwd`）；
- 当前行 ≠「我们写进去的样子」⇒ **暂停不覆盖**，409 `undo_paused`，报告已还原/未还原；
- 本批次新建的项目/目录：仅当「无归属引用 + 无其它批次引用 + 目录为空」时删除注册与目录，
  否则保留并写明原因。

## 7. 跑批入口（`api_autogroup_ai_run.py`，面板「AI 分类」用）

引擎是同步函数、一条会话一次模型调用（实测 ~5s/条），所以**不能**在一次 HTTP 请求里跑全量。
面板走「登记 job + 轮询」这条路（与 `/autogroup/scan` 同套路），口径全部来自引擎返回体：

| 路由 | 作用 |
|---|---|
| `GET /autogroup/ai/scope` | **不调模型**的口径统计：`total_available` / `excluded_applied` / `excluded_rejected` / `excluded_other` / `sessions_total` / `projects` / `batch_default` / `batch_max` |
| `POST /autogroup/ai/classify` | 登记跑批 job（202 + `job_id`）。body：`{limit, offset?, skip_ids?, note?, store?}` |
| `GET /autogroup/ai/classify/{job_id}` | `{state: running|done|failed, progress:{done,total}, error, data:{result}}` |

**`limit` 必须显式传**（缺了直接 400 `limit_required`）：`0` = 全量，`1..200` = 分批。
引擎默认 `limit=40` 只是历史兼容值，界面沿用会**静默少处理**候选 —— 这是硬约束，不是建议。

分批续跑用 `skip_ids`（上一批的 `session_id` 数组，优先于 `offset`）：两次调用之间候选列表变了
也不会重复处理同一会话。进度字段（都来自引擎 `scope`）：

```
progress = {total_available, processed, processed_now, processed_before,
            remaining, truncated, omitted, limit, offset, next_offset}
```

`truncated=true` 表示「本次只处理了候选的一个前缀」，`omitted` 是被截掉的条数 —— 界面必须显示
「已处理 N / 共 total_available」，不许让用户以为已经覆盖全部。job 结果里的 `rows` 是界面行的
原料（标题 / 当前归属 / 建议项目 / 置信度 / 理由 / 证据 / 分档 / 确认后状态 / 可写性），
`batch_id` 就是服务端预览批次，后续确认 / 回读 / 撤销都按它走。

跑批**只写 AI 三张表**（预览批次），不写归属、不改 cwd、不建目录。
