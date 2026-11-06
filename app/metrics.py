"""Prometheus 指标：对话、模型、工具三个层面。

放在独立模块，避免 `api/meta.py` 与 `agent/loop.py` 循环导入。
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# 对话入口
CHAT_REQUESTS = Counter("fm_agent_chat_requests_total", "对话请求数")

# 模型：按轮计时 + token 用量（prompt/completion 分开，方便算成本）
LLM_ROUND_SECONDS = Histogram(
    "fm_agent_llm_round_seconds",
    "单轮模型调用耗时",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
LLM_TOKENS = Counter("fm_agent_llm_tokens_total", "模型 token 用量", ["direction"])

# 工具：按工具名计时 + 成功/失败计数
TOOL_SECONDS = Histogram(
    "fm_agent_tool_seconds",
    "工具执行耗时",
    ["tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
TOOL_CALLS = Counter("fm_agent_tool_calls_total", "工具调用数", ["tool", "ok"])

# 图片 OCR：预处理（校验/解码/缩放）+ 识别整体耗时
OCR_SECONDS = Histogram(
    "fm_agent_ocr_seconds",
    "图片文字识别耗时",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)
OCR_TOTAL = Counter("fm_agent_ocr_total", "图片文字识别次数", ["tool", "ok"])
