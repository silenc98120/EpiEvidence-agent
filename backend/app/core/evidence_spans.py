"""为质量评估生成稳定、可追溯的标题和摘要证据片段。"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID


SEGMENTER_VERSION = "sentence-window-v1"
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])(?:[\s\u00a0]+|(?=[\u4e00-\u9fff]))")
_NON_TERMINAL = ("et al.", "e.g.", "i.e.", "vs.", "dr.", "fig.", "no.", "inc.", "approx.")


def _sentences(text: str) -> list[tuple[int, int, str]]:
    text = text or ""
    if not text.strip():
        return []
    parts = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if text[max(start, match.start() - 12):match.start() + 1].casefold().endswith(_NON_TERMINAL):
            continue
        end = match.start()
        if text[start:end].strip():
            left = start + len(text[start:end]) - len(text[start:end].lstrip())
            right = start + len(text[start:end].rstrip())
            parts.append((left, right, text[left:right]))
        start = match.end()
    if text[start:].strip():
        left = start + (len(text[start:]) - len(text[start:].lstrip()))
        right = len(text.rstrip())
        parts.append((left, right, text[left:right]))
    return parts


def build_evidence_spans(article_id: UUID | str, title: str, abstract: str | None) -> dict[str, Any]:
    """切分 canonical 文本；每段最多三句，保留原文偏移。"""
    article_id = str(article_id)
    result: dict[str, Any] = {"version": SEGMENTER_VERSION, "title": [], "abstract": []}
    for source, text in (("title", title), ("abstract", abstract or "")):
        sentences = _sentences(text)
        if not sentences and text.strip():
            sentences = [(0, len(text), text)]
        for index in range(0, len(sentences), 3):
            group = sentences[index:index + 3]
            start, end = group[0][0], group[-1][1]
            result[source].append({
                "span_id": f"{article_id}:{source}:{index // 3 + 1:03d}",
                "sequence": index // 3 + 1,
                "text": text[start:end],
                "start_offset": start,
                "end_offset": end,
            })
    return result


def resolve_span_ids(spans: dict[str, Any], span_ids: list[str]) -> list[str]:
    """校验并按原文顺序解析 span ID；不接受跨文章或未知 ID。"""
    lookup = {item["span_id"]: item for source in ("title", "abstract") for item in spans.get(source, [])}
    selected = [lookup[span_id] for span_id in span_ids]
    if len({item["span_id"] for item in selected}) != len(selected):
        raise ValueError("evidence_span_ids 不能重复")
    source_order = {"title": 0, "abstract": 1}
    selected.sort(key=lambda item: (source_order[item["span_id"].split(":")[-2]], item["sequence"]))
    return [item["text"] for item in selected]
