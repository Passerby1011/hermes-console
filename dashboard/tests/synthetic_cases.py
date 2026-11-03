# -*- coding: utf-8 -*-
"""**全部为人工合成测试数据**（不含任何真实会话内容，不来自生产库）。

10 条合成会话 + 3 个合成项目 + 每条的人工预期标签，供两处复用：

1. ``tests/test_autogroup_ai_integration.py``（确定性链路，mock 模型）；
2. ``tests/verify_real_model.py``（真实配置通道 + 预期标签对比）。

三档覆盖（原卡要求）：
* 已有项目匹配：``synth-01``…``synth-04``（含 0.80 / 0.50 两个边界值）；
* 同主题新建候选：``synth-05``…``synth-07``（物联网网关，3 条不同会话 ⇒ 成立）；
* 无法判定：``synth-08``…``synth-10``（闲聊/非项目主题）。
"""
from __future__ import annotations

SYNTH_NOTE = "人工合成测试数据，非真实会话"

PROJECTS = [("memory", "记忆库"), ("console", "控制台"), ("trading", "量化交易")]

#: 每条：id / 标题 / cwd 相对路径 / 用户消息 / 人工预期标签 / 脚本化模型输出
SESSIONS = [
    {
        "id": "synth-01", "title": "[合成] 记忆库索引重建排查", "cwd_slug": "memory",
        "messages": ["记忆库索引重建以后老条目搜不到了，帮我看看重建脚本",
                     "重建时看到索引文件从 3 个变成 2 个", "要不要先备份再重建", "重建完怎么验证召回没掉"],
        "expected": {"kind": "existing", "project_slug": "memory", "tier": "apply",
                     "confidence": 0.93},
        "verdict": {"project_slug": "memory", "confidence": 0.93,
                    "reason": "记忆库索引重建与召回问题，属记忆库项目", "new_project": None},
    },
    {
        "id": "synth-02", "title": "[合成] 记忆检索召回率调优", "cwd_slug": "memory",
        "messages": ["记忆检索的召回率最近掉到 0.6", "想调一下向量检索的 top_k", "会不会是分块策略变了",
                     "顺便看下 rerank 有没有生效"],
        "expected": {"kind": "existing", "project_slug": "memory", "tier": "apply",
                     "confidence": 0.80},
        "verdict": {"project_slug": "memory", "confidence": 0.80,
                    "reason": "记忆检索召回与 rerank 调优", "new_project": None},
    },
    {
        "id": "synth-03", "title": "[合成] 控制台面板图表渲染异常", "cwd_slug": "console",
        "messages": ["控制台的概览面板折线图渲染成空白", "控制台里只有 token 卡片正常", "换浏览器还是一样",
                     "是不是图表库版本升级挂了"],
        "expected": {"kind": "existing", "project_slug": "console", "tier": "apply",
                     "confidence": 0.88},
        "verdict": {"project_slug": "console", "confidence": 0.88,
                    "reason": "控制台面板图表渲染问题", "new_project": None},
    },
    {
        "id": "synth-04", "title": "[合成] 控制台设置项文案统一", "cwd_slug": "console",
        "messages": ["控制台设置页的文案一会儿中文一会儿英文", "想统一成中文", "顺便看看有没有重复的设置项",
                     "改完要不要重新构建前端"],
        "expected": {"kind": "existing", "project_slug": "console", "tier": "suggest",
                     "confidence": 0.50},
        "verdict": {"project_slug": "console", "confidence": 0.50,
                    "reason": "控制台设置页文案统一，证据较弱", "new_project": None},
    },
    {
        "id": "synth-05", "title": "[合成] 网关固件 OTA 升级失败回滚", "cwd_slug": None,
        "messages": ["网关固件 OTA 升级到一半断电了", "重启后卡在 bootloader", "怎么回滚到上一版固件",
                     "串口能不能救回来"],
        "expected": {"kind": "new_project", "project_slug": None, "tier": "suggest",
                     "new_slug": "iot-gateway", "confidence": 0.72},
        "verdict": {"project_slug": None, "confidence": 0.72,
                    "reason": "嵌入式网关固件升级，与现有项目无关",
                    "new_project": {"slug": "iot-gateway", "name": "物联网网关"}},
    },
    {
        "id": "synth-06", "title": "[合成] 网关串口日志抓取脚本", "cwd_slug": None,
        "messages": ["网关串口日志要抓下来分析", "波特率 115200 但全是乱码", "想写个脚本自动抓",
                     "抓完怎么解析成结构化日志"],
        "expected": {"kind": "new_project", "project_slug": None, "tier": "suggest",
                     "new_slug": "iot-gateway", "confidence": 0.68},
        "verdict": {"project_slug": None, "confidence": 0.68,
                    "reason": "嵌入式网关串口调试，与现有项目无关",
                    "new_project": {"slug": "iot-gateway", "name": "物联网网关"}},
    },
    {
        "id": "synth-07", "title": "[合成] 网关 MQTT 桥接掉线重连", "cwd_slug": None,
        "messages": ["网关的 MQTT 桥接每天掉线几次", "重连以后订阅丢失", "要不要加心跳检测",
                     "断线期间的数据怎么补传"],
        "expected": {"kind": "new_project", "project_slug": None, "tier": "suggest",
                     "new_slug": "iot-gateway", "confidence": 0.70},
        "verdict": {"project_slug": None, "confidence": 0.70,
                    "reason": "嵌入式网关 MQTT 桥接稳定性，与现有项目无关",
                    "new_project": {"slug": "iot-gateway", "name": "物联网网关"}},
    },
    {
        "id": "synth-08", "title": "[合成] 晚饭吃什么", "cwd_slug": None,
        "messages": ["晚上吃点什么好", "附近有家面馆", "算了随便吧", "明天再说"],
        "expected": {"kind": "unjudged", "project_slug": None, "tier": "undecided",
                     "confidence": 0.15},
        "verdict": {"project_slug": None, "confidence": 0.15,
                    "reason": "日常闲聊，不涉及任何项目", "new_project": None},
    },
    {
        "id": "synth-09", "title": "[合成] 打印机驱动装不上", "cwd_slug": None,
        "messages": ["打印机驱动装不上", "设备管理器里显示黄色感叹号", "官网驱动也装失败", "换了根数据线还是一样"],
        "expected": {"kind": "unjudged", "project_slug": None, "tier": "undecided",
                     "confidence": 0.30},
        "verdict": {"project_slug": None, "confidence": 0.30,
                    "reason": "硬件驱动问题，与现有项目无关且证据不足", "new_project": None},
    },
    {
        "id": "synth-10", "title": "[合成] 今天天气怎么样", "cwd_slug": None,
        "messages": ["今天天气怎么样", "会下雨吗", "要不要带伞", "周末呢"],
        "expected": {"kind": "unjudged", "project_slug": None, "tier": "undecided",
                     "confidence": 0.10},
        "verdict": {"project_slug": None, "confidence": 0.10,
                    "reason": "天气闲聊，不涉及任何项目", "new_project": None},
    },
]

BY_ID = {row["id"]: row for row in SESSIONS}


def cwd_for(row: dict, sandbox_root: str) -> str | None:
    """合成 cwd：指向沙盒里的项目目录（或 None）。"""
    slug = row.get("cwd_slug")
    if not slug:
        return None
    return str(sandbox_root) + "\\projects\\" + slug


def scripted_verdicts() -> dict:
    """标题 → 模型输出文本（确定性测试用；**不是**真实模型验收）。"""
    import json

    return {row["title"]: json.dumps(row["verdict"], ensure_ascii=False) for row in SESSIONS}
