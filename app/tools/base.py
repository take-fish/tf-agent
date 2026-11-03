"""工具协议：所有工具都是"参数 → 结构化结果"，不接受自由 SQL / 任意 URL。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """工具执行结果。

    text  —— 喂回模型的内容（已按上下文预算截断）
    rows  —— 命中行数，写审计
    ok    —— 是否成功
    data  —— 结构化数据，给 SSE 事件/前端用（不是模型输入）
    """

    text: str
    rows: int = 0
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, *, rows: int = 0) -> ToolResult:
        return cls(text=f"工具执行失败：{message}", ok=False, rows=rows, data={"error": message})


ToolHandler = Callable[..., Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    read_only: bool = True
    timeout: float = 20.0
    # 该工具是否必须在管理台配置后才能使用（例如发布）
    requires_config: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def obj_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
