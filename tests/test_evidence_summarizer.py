from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.graph import create_evidence_summarizer_node
from agent.summarizer import (
    EvidenceSummarizer,
    FinalEvidenceSummary,
    PreferredFullText,
    SummarizerDraft,
    SummarizerRepository,
    SummaryArticle,
    SummaryArticleDraft,
    render_summary_chat,
)
from app.prompts.evidence_summarizer_prompt_template import (
    EVIDENCE_SUMMARIZER_SYSTEM_PROMPT,
)


class FakeRunnable:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeModel:
    def __init__(self, response):
        self.runnable = FakeRunnable(response)
        self.schema = None
        self.include_raw = None

    def with_structured_output(self, schema, *, include_raw=False):
        self.schema = schema
        self.include_raw = include_raw
        return self.runnable


class FakeRepository:
    def __init__(self, articles):
        self.articles = articles
        self.calls = []

    async def hydrate(self, article_ids):
        self.calls.append(list(article_ids))
        by_id = {item.article_id: item for item in self.articles}
        return [by_id[article_id] for article_id in article_ids]


def _ranked(article_ids):
    return [
        {
            "article_id": str(article_id),
            "coverage_contribution_score": 9.0 - index,
            "corrected_composite_score": 8.0 - index,
            "verification_issues": ["摘要未报告长期随访"] if index == 0 else [],
        }
        for index, article_id in enumerate(article_ids)
    ]


def _article(article_id, title):
    return SummaryArticle(
        article_id=article_id,
        title=title,
        abstract=f"{title} abstract.",
        authors=[{"full_name": "First Author"}],
        first_author="First Author",
        journal_title="Example Journal",
        publication_year=2025,
        study_design="randomized_controlled_trial",
        preferred_full_text=PreferredFullText(
            resource_id=uuid4(),
            format="pdf",
            url="https://example.org/article.pdf",
        ),
    )


def test_summarizer_system_prompt_is_valid_xml_and_preserves_program_authority():
    root = ElementTree.fromstring(EVIDENCE_SUMMARIZER_SYSTEM_PROMPT.strip())

    assert root.tag == "evidence_summarizer_prompt"
    assert "推荐篇数不固定" in EVIDENCE_SUMMARIZER_SYSTEM_PROMPT
    assert "不得修改" in EVIDENCE_SUMMARIZER_SYSTEM_PROMPT
    assert "不得输出 URL" in EVIDENCE_SUMMARIZER_SYSTEM_PROMPT


@pytest.mark.anyio
async def test_summarizer_allows_model_selected_count_and_restores_program_order():
    first_id = uuid4()
    second_id = uuid4()
    draft = SummarizerDraft(
        task_id="task-1",
        selected_article_ids=[second_id, first_id],
        article_summaries=[
            SummaryArticleDraft(
                article_id=second_id,
                abstract_summary="第二篇摘要概括。",
                recommendation_reason="补充结局信息。",
            ),
            SummaryArticleDraft(
                article_id=first_id,
                abstract_summary="第一篇摘要概括。",
                recommendation_reason="直接覆盖核心问题。",
                strengths=["核心概念覆盖完整"],
            ),
        ],
        overall_summary="两篇文献共同覆盖用户问题。",
    )
    model = FakeModel(
        {"parsed": draft, "raw": None, "parsing_error": None}
    )
    repository = FakeRepository(
        [_article(first_id, "First article"), _article(second_id, "Second article")]
    )
    summarizer = EvidenceSummarizer(model=model, repository=repository)

    result = await summarizer.summarize(
        task_id="task-1",
        user_query="研究问题",
        intent_analysis={},
        portfolio_evaluation={},
        ranked_candidates=_ranked([first_id, second_id]),
        search_metrics={
            "source_statuses": {"pubmed": "success_with_results"},
            "provider_record_count": 20,
            "unique_record_count": 12,
            "duplicate_record_count": 8,
        },
    )

    assert result.recommended_article_ids == [first_id, second_id]
    assert [item.rank for item in result.recommendations] == [1, 2]
    assert result.search_overview.recommended_count == 2
    assert model.include_raw is True
    assert len(model.runnable.calls) == 1


def test_summarizer_draft_has_no_fixed_recommendation_limit():
    article_ids = [uuid4() for _ in range(12)]
    draft = SummarizerDraft(
        task_id="task-1",
        selected_article_ids=article_ids,
        article_summaries=[
            SummaryArticleDraft(
                article_id=article_id,
                abstract_summary="摘要概括。",
                recommendation_reason="推荐理由。",
            )
            for article_id in article_ids
        ],
        overall_summary="候选文章均有独立价值。",
    )

    assert len(draft.selected_article_ids) == 12


@pytest.mark.anyio
async def test_summarizer_rejects_article_outside_ranked_candidates():
    candidate_id = uuid4()
    unknown_id = uuid4()
    draft = SummarizerDraft(
        task_id="task-1",
        selected_article_ids=[unknown_id],
        article_summaries=[
            SummaryArticleDraft(
                article_id=unknown_id,
                abstract_summary="摘要概括。",
                recommendation_reason="推荐理由。",
            )
        ],
        overall_summary="存在推荐文章。",
    )
    summarizer = EvidenceSummarizer(
        model=FakeModel({"parsed": draft, "raw": None, "parsing_error": None}),
        repository=FakeRepository([_article(candidate_id, "Candidate")]),
    )

    with pytest.raises(ValueError, match="之外"):
        await summarizer.summarize(
            task_id="task-1",
            user_query="研究问题",
            intent_analysis={},
            portfolio_evaluation={},
            ranked_candidates=_ranked([candidate_id]),
            search_metrics={},
        )


def test_repository_prefers_downloadable_open_access_pdf():
    repository = SummarizerRepository(session_factory=None)
    article_id = uuid4()
    html = SimpleNamespace(
        resource_id=uuid4(),
        article_id=article_id,
        format="html",
        resource_url="https://example.org/article.html",
        is_downloadable=True,
        is_open_access=True,
        is_preferred=True,
    )
    pdf = SimpleNamespace(
        resource_id=uuid4(),
        article_id=article_id,
        format="pdf",
        resource_url="https://example.org/article.pdf",
        is_downloadable=True,
        is_open_access=True,
        is_preferred=False,
    )
    closed_pdf = SimpleNamespace(
        resource_id=uuid4(),
        article_id=article_id,
        format="pdf",
        resource_url="https://example.org/closed.pdf",
        is_downloadable=True,
        is_open_access=False,
        is_preferred=True,
    )

    selected = repository._select_full_text([html, closed_pdf, pdf])

    assert selected is not None
    assert selected.resource_id == pdf.resource_id


@pytest.mark.anyio
async def test_summarizer_node_appends_final_ai_message():
    article_id = uuid4()
    summary = FinalEvidenceSummary.model_validate(
        {
            "task_id": "task-1",
            "user_query": "研究问题",
            "search_overview": {
                "sources": ["pubmed"],
                "source_statuses": {"pubmed": "success_with_results"},
                "provider_record_count": 10,
                "unique_record_count": 8,
                "duplicate_record_count": 2,
                "retained_count": 1,
                "recommended_count": 1,
            },
            "overall_summary": "找到一篇直接相关文献。",
            "recommendations": [
                {
                    "article_id": article_id,
                    "rank": 1,
                    "title": "Recommended article",
                    "abstract_summary": "摘要概括。",
                    "recommendation_reason": "直接回答问题。",
                    "corrected_composite_score": 8.0,
                    "coverage_contribution_score": 9.0,
                }
            ],
            "recommended_article_ids": [article_id],
        }
    )

    class FakeSummarizer:
        async def summarize(self, **_kwargs):
            return summary

    node = create_evidence_summarizer_node(FakeSummarizer())
    output = await node(
        {
            "task_id": "task-1",
            "user_query": "研究问题",
            "intent_analysis": {},
            "portfolio_evaluation": {},
            "ranked_candidates": _ranked([article_id]),
            "search_metrics": {},
            "summary_retry_count": 0,
            "messages": [HumanMessage(content="研究问题")],
        }
    )

    assert output["task_stage"] == "completed"
    assert isinstance(output["messages"][0], AIMessage)
    assert "Recommended article" in output["messages"][0].content
    assert output["messages"][0].additional_kwargs["recommended_article_ids"] == [
        str(article_id)
    ]
    assert "final_summary" not in output["messages"][0].additional_kwargs


def test_rendered_message_contains_verified_full_text_link():
    article_id = uuid4()
    summary = FinalEvidenceSummary.model_validate(
        {
            "task_id": "task-1",
            "user_query": "研究问题",
            "search_overview": {
                "recommended_count": 1,
                "retained_count": 1,
            },
            "overall_summary": "找到一篇文献。",
            "recommendations": [
                {
                    "article_id": article_id,
                    "rank": 1,
                    "title": "Article title",
                    "abstract_summary": "摘要概括。",
                    "recommendation_reason": "推荐理由。",
                    "corrected_composite_score": 8.0,
                    "coverage_contribution_score": 9.0,
                    "preferred_full_text": {
                        "resource_id": uuid4(),
                        "format": "pdf",
                        "url": "https://example.org/article.pdf",
                    },
                }
            ],
            "recommended_article_ids": [article_id],
        }
    )

    content = render_summary_chat(summary)

    assert "[PDF](https://example.org/article.pdf)" in content
    assert "不是正式的全文偏倚风险评价" in content
