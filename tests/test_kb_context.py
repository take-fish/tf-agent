"""检索结果打包：邻居扩展 / 整节回捞 / 预算与降级。"""

from __future__ import annotations

import pytest

from app.agent import loop
from app.kb import context
from app.tools.base import ToolResult


def _hit(seq: int, text: str | None = None, **extra) -> dict:
    item = {
        "doc_id": "doc-1",
        "kb_id": "kb-1",
        "title": "07-游戏中心",
        "seq": seq,
        "text": text if text is not None else f"第 {seq} 块正文",
        "score": 0.8,
    }
    item.update(extra)
    return item


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    # 不依赖 Nacos 运行时，统一用代码默认值
    monkeypatch.setattr(context, "_param", lambda name, default: default)
    monkeypatch.setattr(context, "db_available", lambda: True)


def test_off_mode_returns_raw_blocks():
    blocks = context.expand_hits([_hit(2)], mode="off")
    assert blocks == [
        {
            "title": "07-游戏中心",
            "doc_id": "doc-1",
            "kb_id": "kb-1",
            "seq_start": 2,
            "seq_end": 2,
            "text": "第 2 块正文",
            "section": "",
            "section_seq": None,
            "expanded": False,
            "score": 0.8,
        }
    ]


def test_neighbor_mode_merges_adjacent_rows_and_removes_overlap(monkeypatch):
    rows = {
        3: "发布前先备份数据库。\n数据库备份完成后再执行迁移。",
        4: "数据库备份完成后再执行迁移。\n迁移成功后更新镜像。",
        5: "迁移成功后更新镜像。\n最后重启服务。",
    }
    monkeypatch.setattr(
        context,
        "_load_neighbors",
        lambda hit, radius: [
            {"seq": seq, "text": text, "section": "发布流程", "section_seq": 2} for seq, text in sorted(rows.items())
        ],
    )

    blocks = context.expand_hits([_hit(4, "数据库备份完成后再执行迁移。")], mode="neighbor")

    assert len(blocks) == 1
    block = blocks[0]
    assert block["expanded"] is True
    assert (block["seq_start"], block["seq_end"]) == (3, 5)
    assert block["section"] == "发布流程"
    # 重叠区只保留一份
    assert block["text"].count("数据库备份完成后再执行迁移。") == 1
    assert block["text"].startswith("发布前先备份数据库")
    assert block["text"].endswith("最后重启服务。")


def test_only_top_hits_are_expanded_and_second_adjacent_hit_is_not_duplicated(monkeypatch):
    calls: list[int] = []

    def load(hit, radius):
        calls.append(int(hit["seq"]))
        seq = int(hit["seq"])
        return [
            {"seq": item, "text": f"块 {item}", "section": "", "section_seq": 0}
            for item in range(max(0, seq - 1), seq + 2)
        ]

    monkeypatch.setattr(context, "_load_neighbors", load)
    hits = [_hit(1), _hit(2), _hit(8), _hit(9)]
    blocks = context.expand_hits(hits, mode="neighbor")

    # 只扩前两个命中；第二个已被第一个的范围覆盖，不再扩
    assert calls == [1]
    assert [block["expanded"] for block in blocks] == [True, False, False]
    assert [block["seq_start"] for block in blocks] == [0, 8, 9]


def test_database_unavailable_falls_back_to_raw(monkeypatch):
    monkeypatch.setattr(context, "db_available", lambda: False)
    monkeypatch.setattr(context, "_load_neighbors", lambda hit, radius: pytest.fail("不应查库"))

    blocks = context.expand_hits([_hit(1)], mode="neighbor")
    assert blocks[0]["expanded"] is False
    assert blocks[0]["text"] == "第 1 块正文"


def test_section_mode_uses_section_loader(monkeypatch):
    seen: list[int] = []

    def load(hit, max_chunks):
        seen.append(int(hit["section_seq"]))
        return [
            {"seq": 10, "text": "第一节开头。", "section": "07-游戏中心 / 赛制", "section_seq": 3},
            {"seq": 11, "text": "第一节结尾。", "section": "07-游戏中心 / 赛制", "section_seq": 3},
        ]

    monkeypatch.setattr(context, "_load_section", load)
    monkeypatch.setattr(context, "_load_neighbors", lambda hit, radius: pytest.fail("不应走邻居扩展"))

    blocks = context.expand_hits([_hit(10, section="07-游戏中心 / 赛制", section_seq=3)], mode="section")

    assert seen == [3]
    assert blocks[0]["expanded"] is True
    assert blocks[0]["section"] == "07-游戏中心 / 赛制"
    assert "第一节开头" in blocks[0]["text"] and "第一节结尾" in blocks[0]["text"]


def test_section_mode_without_metadata_falls_back_to_neighbors(monkeypatch):
    monkeypatch.setattr(context, "_load_section", lambda hit, max_chunks: pytest.fail("没有章节字段不应整节回捞"))
    monkeypatch.setattr(
        context,
        "_load_neighbors",
        lambda hit, radius: [
            {"seq": 0, "text": "上一块", "section": "", "section_seq": None},
            {"seq": 1, "text": "当前块", "section": "", "section_seq": None},
        ],
    )

    blocks = context.expand_hits([_hit(1)], mode="section")
    assert blocks[0]["expanded"] is True
    assert "上一块" in blocks[0]["text"]


def test_total_budget_caps_expanded_text(monkeypatch):
    monkeypatch.setattr(
        context,
        "_load_neighbors",
        lambda hit, radius: [
            {"seq": 0, "text": "甲" * 700, "section": "", "section_seq": 0},
            {"seq": 1, "text": "乙" * 700, "section": "", "section_seq": 0},
        ],
    )
    blocks = context.expand_hits([_hit(0), _hit(1)], mode="neighbor")
    expanded = [block for block in blocks if block["expanded"]]
    assert expanded
    assert sum(len(block["text"]) for block in expanded) <= 1600


def test_format_kb_hits_prefers_blocks_and_keeps_total_budget():
    long_text = "行内容\n" * 400
    result = ToolResult(
        text="工具输出",
        rows=2,
        data={
            "chunks": [{"title": "旧块", "seq": 0, "text": long_text}],
            "blocks": [
                {
                    "title": "文档",
                    "seq_start": 2,
                    "seq_end": 3,
                    "section": "发布流程",
                    "text": long_text,
                    "expanded": True,
                },
                {
                    "title": "文档",
                    "seq_start": 8,
                    "seq_end": 8,
                    "section": "",
                    "text": long_text,
                    "expanded": False,
                },
            ],
        },
    )
    text = loop._format_kb_hits(result)
    assert "《文档》发布流程 第3–4块" in text
    assert "《文档》第9块" in text
    assert "工具输出" not in text
    assert len(text) <= loop._KB_NOTE_MAX_CHARS
    assert len(text.split("\n\n")[0].split("\n", 1)[1]) <= loop._KB_EXPANDED_MAX_CHARS
