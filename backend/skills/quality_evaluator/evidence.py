"""验证模型返回的 evidence span，并生成兼容的原文引文。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.app.core.evidence_spans import resolve_span_ids


def resolve_article_evidence(data: dict, articles: Sequence[Mapping[str, Any]]) -> dict:
    by_id = {str(a["article_id"]): a.get("evidence_spans", {}) for a in articles}
    for assessment in data.get("relevance_assessments", []):
        ids = assessment.get("evidence_span_ids", [])
        if ids:
            assessment["evidence_quote"] = "\n".join(resolve_span_ids(by_id[str(assessment["article_id"])], ids))
    for assessment in data.get("quality_assessments", []):
        spans = by_id[str(assessment["article_id"])]
        for criterion in assessment.get("criteria", []):
            ids = criterion.get("evidence_span_ids", [])
            if criterion["status"] == "not_reported":
                if ids:
                    raise ValueError("not_reported 不得引用 evidence_span_ids")
                criterion["evidence_quote"] = "摘要未报告"
            elif criterion["status"] == "not_applicable":
                if ids:
                    raise ValueError("not_applicable 不得引用 evidence_span_ids")
                criterion["evidence_quote"] = "不适用"
            else:
                if not ids:
                    raise ValueError("已报告指标必须引用 evidence_span_ids")
                criterion["evidence_quote"] = "\n".join(resolve_span_ids(spans, ids))
    return data


def resolve_portfolio_evidence(data: dict, articles: Sequence[Mapping[str, Any]]) -> dict:
    by_id = {str(a["article_id"]): a.get("evidence_spans", {}) for a in articles}

    def resolve(article_id: str, ids: list[str]) -> list[str]:
        if article_id not in by_id:
            raise ValueError(f"evidence 引用了候选集外文章：{article_id}")
        return resolve_span_ids(by_id[article_id], ids)

    query = data.get("query_coverage", {})
    items = query.get("keyword_coverage", []) + query.get("constraint_coverage", [])
    for item in items:
        article_ids = [str(x) for x in item.get("evidence_article_ids", [])]
        checked = [str(x) for x in item.get("checked_article_ids", [])]
        if not set(article_ids + checked).issubset(by_id):
            raise ValueError("coverage 引用了候选集外文章")
        ids = item.get("evidence_span_ids", [])
        if item.get("status") == "absent":
            if ids or not checked:
                raise ValueError("absent coverage 必须提供 checked_article_ids 且不得提供正向 span")
        else:
            if not article_ids or not ids:
                raise ValueError("非 absent coverage 必须提供文章和 evidence_span_ids")
            grouped = {aid: [x for x in ids if x.startswith(aid + ":")] for aid in article_ids}
            if sum(map(len, grouped.values())) != len(ids):
                raise ValueError("coverage evidence_span_ids 必须属于 evidence_article_ids")
            if any(not values for values in grouped.values()):
                raise ValueError("coverage 每篇证据文章都必须有 evidence_span_ids")
            item["evidence_quotes"] = {aid: resolve(aid, values) for aid, values in grouped.items()}
    joint = data.get("joint_query_coverage", {})
    joint_articles = {str(aid) for aid in joint.get("evidence_article_ids", [])}
    joint_checked = {str(aid) for aid in joint.get("checked_article_ids", [])}
    evidence_by_article = {str(aid): ids for aid, ids in joint.get("evidence_by_article", {}).items()}
    if joint.get("status") == "absent":
        if joint_articles or evidence_by_article or joint.get("evidence_span_ids") or not joint_checked:
            raise ValueError("absent joint coverage 不得提供正向文章或 span")
    else:
        if not joint_articles or set(evidence_by_article) != joint_articles:
            raise ValueError("非 absent joint coverage 必须逐文章提供 evidence span")
        if any(not ids for ids in evidence_by_article.values()):
            raise ValueError("joint coverage 的每篇证据文章都必须有 span")
        flattened_ids = [span_id for ids in evidence_by_article.values() for span_id in ids]
        if not joint.get("evidence_span_ids"):
            joint["evidence_span_ids"] = flattened_ids
        elif set(flattened_ids) != set(joint.get("evidence_span_ids", [])):
            raise ValueError("joint evidence_span_ids 必须与 evidence_by_article 完全一致")
    if not (joint_articles | joint_checked).issubset(by_id):
        raise ValueError("joint coverage 引用了候选集外文章")
    joint["evidence_quotes_by_article"] = {
        aid: resolve(aid, ids) for aid, ids in evidence_by_article.items()
    }
    for relation in data.get("redundancy_and_conflicts", []):
        per_article = relation.get("evidence_span_ids_by_article", {})
        expected = {str(x) for x in relation.get("article_ids", [])}
        if {str(x) for x in per_article} != expected:
            raise ValueError("重复/冲突关系必须为每篇文章提供证据 span")
        if not expected.issubset(by_id) or any(not ids for ids in per_article.values()):
            raise ValueError("重复/冲突关系引用了候选集外文章或空证据")
        relation["evidence_quotes_by_article"] = {
            str(aid): resolve(str(aid), ids) for aid, ids in per_article.items()
        }
    for contribution in data.get("article_contributions", []):
        ids = contribution.get("evidence_span_ids", [])
        if contribution.get("contribution_level") == "none":
            if ids or not contribution.get("overlap_article_ids"):
                raise ValueError("none 贡献必须引用重叠文章且不得提供正向 span")
        elif not ids:
            raise ValueError("有贡献文章必须提供 evidence_span_ids")
        else:
            contribution["evidence_quote"] = "\n".join(resolve(str(contribution["article_id"]), ids))
    return data
