"""质量评价前的可审计候选排序。

该模块只做纯排序：它不知道 LangGraph、Celery 和数据库。调用方负责构造文章候选和
EmbeddingClient，并决定排序后是否应用 Top-K 或研究设计配额。
"""

from __future__ import annotations

import math
import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from openai import AsyncOpenAI


_DEFAULT_CATEGORY_WEIGHTS = {
    "drug": 0.45,
    "intervention": 0.45,
    "population": 0.30,
    "outcome": 0.25,
}
_CORE_INTERVENTION_KINDS = frozenset({"drug", "intervention"})
_WORD_SEPARATOR = re.compile(r"[^\w]+", flags=re.UNICODE)


@dataclass(frozen=True)
class CandidateArticle:
    """排序器所需的最小文章视图。"""

    article_id: str
    title: str
    abstract: str | None = None
    publication_date: date | None = None
    publication_year: int | None = None
    study_design: str | None = None


@dataclass(frozen=True)
class ResearchConcept:
    """一个用户关键词和它的英文替代表达。"""

    kind: str
    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class ResearchProfile:
    """供关键词匹配和 embedding 使用的标准化研究问题。"""

    query_text: str
    concepts: tuple[ResearchConcept, ...]

    @classmethod
    def from_intent(
        cls,
        intent: Mapping[str, Any],
        mesh_normalization: Mapping[str, Any] | None = None,
    ) -> "ResearchProfile":
        """从 Intent 与 MeSH 结果构建带概念角色的英文研究 profile。"""

        raw_keywords = intent.get("keywords", {})
        if not isinstance(raw_keywords, Mapping):
            raise ValueError("intent_analysis.keywords 必须是对象")

        mesh_terms: dict[tuple[str, str], str] = {}
        for item in (mesh_normalization or {}).get("normalized_keywords", []):
            if not isinstance(item, Mapping):
                continue
            matches = item.get("mesh_matches", [])
            if isinstance(matches, Sequence) and matches and isinstance(matches[0], Mapping):
                preferred = _clean_text(matches[0].get("preferred_label"))
                if preferred:
                    mesh_terms[(str(item.get("kind", "")).casefold(), _clean_text(item.get("raw_text")).casefold())] = preferred

        concepts: list[ResearchConcept] = []
        query_lines = ["Research question:"]
        for kind, candidates in raw_keywords.items():
            if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
                continue
            rendered_terms: list[str] = []
            for candidate in candidates:
                if not isinstance(candidate, Mapping):
                    continue
                label = _clean_text(candidate.get("raw_text"))
                english_candidates = candidate.get("english_candidates", [])
                terms = _unique_terms(
                    english_candidates if isinstance(english_candidates, Sequence) else []
                )
                preferred = mesh_terms.get((str(getattr(kind, "value", kind)).casefold(), label.casefold()))
                if preferred:
                    terms = [preferred, *[term for term in terms if term.casefold() != preferred.casefold()]]
                if not terms:
                    continue
                concepts.append(
                    ResearchConcept(
                        kind=str(getattr(kind, "value", kind)).casefold(),
                        label=label or terms[0],
                        terms=tuple(terms),
                    )
                )
                rendered_terms.append(terms[0])
            if rendered_terms:
                display_kind = str(getattr(kind, "value", kind)).upper()
                query_lines.append(f"{display_kind}: {', '.join(_unique_terms(rendered_terms))}")

        if not concepts:
            raise ValueError("研究型 intent 至少需要一个英文关键词候选")
        return cls(query_text="\n".join(query_lines), concepts=tuple(concepts))


@dataclass(frozen=True)
class CandidateScore:
    """一篇文章的分项分数与可审计排序结果。"""

    article: CandidateArticle
    keyword_similarity_score: float
    embedding_similarity_score: float
    embedding_cosine_similarity: float
    recency_score: float
    candidate_score: float
    core_intervention_matched: bool
    matched_concepts: tuple[str, ...]
    selection_reason: tuple[str, ...]

    def as_dict(self, rank: int) -> dict[str, Any]:
        return {
            "selection_rank": rank,
            "article_id": self.article.article_id,
            "title": self.article.title,
            "study_design": self.article.study_design,
            "publication_date": (
                self.article.publication_date.isoformat()
                if self.article.publication_date is not None
                else None
            ),
            "publication_year": self.article.publication_year,
            "candidate_score": round(self.candidate_score, 2),
            "keyword_similarity_score": round(self.keyword_similarity_score, 2),
            "embedding_similarity_score": round(self.embedding_similarity_score, 2),
            "embedding_cosine_similarity": round(self.embedding_cosine_similarity, 6),
            "recency_score": round(self.recency_score, 2),
            "core_intervention_matched": self.core_intervention_matched,
            "matched_concepts": list(self.matched_concepts),
            "selection_reason": list(self.selection_reason),
        }


class EmbeddingClient(Protocol):
    """将同一 embedding model 应用于一批文本。"""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """返回与输入顺序和长度完全一致的向量。"""


class ArticleEmbeddingStore(Protocol):
    """按文章内容与 embedding 配置读取和保存跨任务向量。"""

    async def get_many(self, keys: Sequence["ArticleEmbeddingKey"]) -> dict["ArticleEmbeddingKey", list[float]]: ...

    async def put_many(self, values: Mapping["ArticleEmbeddingKey", Sequence[float]]) -> None: ...


@dataclass(frozen=True)
class ArticleEmbeddingKey:
    article_id: str
    model_name: str
    model_revision: str
    representation_version: str
    content_hash: str


class OpenAICompatibleEmbeddingClient:
    """OpenAI-compatible `/embeddings` API 的最小适配器。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        batch_size: int = 64,
    ) -> None:
        if not api_key.strip():
            raise ValueError("embedding API key 不能为空")
        if not model.strip():
            raise ValueError("embedding model 不能为空")
        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            chunk = list(texts[start : start + self._batch_size])
            response = await self._client.embeddings.create(model=self._model, input=chunk)
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(chunk):
                raise RuntimeError("embedding 服务返回的向量数量与请求不一致")
            vectors.extend([list(item.embedding) for item in ordered])
        return vectors

    async def close(self) -> None:
        await self._client.close()

    @property
    def model_name(self) -> str:
        """供审计产物记录本次向量模型。"""

        return self._model


class QualityCandidateSelector:
    """以关键词、语义向量和发表时间对文章做稳定排序。"""

    def __init__(
        self,
        embedding_client: EmbeddingClient,
        *,
        keyword_weight: float = 0.50,
        embedding_weight: float = 0.40,
        recency_weight: float = 0.10,
        reference_date: date | None = None,
        abstract_character_limit: int = 12_000,
        embedding_store: ArticleEmbeddingStore | None = None,
    ) -> None:
        total_weight = keyword_weight + embedding_weight + recency_weight
        if not math.isclose(total_weight, 1.0, abs_tol=1e-9):
            raise ValueError("关键词、向量和发表时间权重之和必须为 1.0")
        if abstract_character_limit < 1:
            raise ValueError("abstract_character_limit 必须大于 0")
        self._embedding_client = embedding_client
        self._keyword_weight = keyword_weight
        self._embedding_weight = embedding_weight
        self._recency_weight = recency_weight
        self._reference_date = reference_date or date.today()
        self._abstract_character_limit = abstract_character_limit
        self._embedding_store = embedding_store

    async def rank(
        self,
        *,
        profile: ResearchProfile,
        articles: Sequence[CandidateArticle],
    ) -> list[CandidateScore]:
        """计算所有文章分数；不会在此处删除任何候选。"""

        if not articles:
            return []
        query_vectors = await self._embedding_client.embed([profile.query_text])
        if len(query_vectors) != 1:
            raise RuntimeError("embedding 服务没有返回唯一 query 向量")
        query_vector = query_vectors[0]
        article_vectors = await self._article_vectors(articles)
        cosine_values = _cosine_similarities(query_vector, article_vectors)

        scores = [
            self._score_article(profile, article, cosine)
            for article, cosine in zip(articles, cosine_values, strict=True)
        ]
        return sorted(
            scores,
            key=lambda item: (
                -item.candidate_score,
                not item.core_intervention_matched,
                item.article.article_id,
            ),
        )

    def _score_article(
        self,
        profile: ResearchProfile,
        article: CandidateArticle,
        cosine: float,
    ) -> CandidateScore:
        title = _normalize_match_text(article.title)
        abstract = _normalize_match_text(article.abstract or "")
        concept_scores = {
            concept: _concept_match_score(concept, title=title, abstract=abstract)
            for concept in profile.concepts
        }
        keyword_score = _weighted_keyword_score(concept_scores)
        semantic_score = ((cosine + 1.0) / 2.0) * 100.0
        recency_score = _recency_score(
            publication_date=article.publication_date,
            publication_year=article.publication_year,
            reference_date=self._reference_date,
        )
        total_score = (
            self._keyword_weight * keyword_score
            + self._embedding_weight * semantic_score
            + self._recency_weight * recency_score
        )
        matched_concepts = tuple(
            concept.label for concept, score in concept_scores.items() if score > 0
        )
        core_concepts = [
            concept
            for concept in profile.concepts
            if concept.kind in _CORE_INTERVENTION_KINDS
        ]
        # 只有研究问题明确包含药物/干预概念时才启用核心概念硬过滤。
        # 纯人群、结局或病因类问题没有 core concept，不应因此排除全部文献。
        core_matched = not core_concepts or all(
            concept_scores[concept] > 0 for concept in core_concepts
        )
        reasons = _selection_reasons(
            matched_concepts=matched_concepts,
            core_intervention_matched=core_matched,
            publication_date=article.publication_date,
            publication_year=article.publication_year,
        )
        return CandidateScore(
            article=article,
            keyword_similarity_score=keyword_score,
            embedding_similarity_score=semantic_score,
            embedding_cosine_similarity=cosine,
            recency_score=recency_score,
            candidate_score=total_score,
            core_intervention_matched=core_matched,
            matched_concepts=matched_concepts,
            selection_reason=tuple(reasons),
        )

    def _article_embedding_text(self, article: CandidateArticle) -> str:
        abstract = (article.abstract or "").strip()[: self._abstract_character_limit]
        return f"Title: {article.title.strip()}\n\nAbstract: {abstract}"

    async def _article_vectors(self, articles: Sequence[CandidateArticle]) -> list[list[float]]:
        texts = [self._article_embedding_text(article) for article in articles]
        if self._embedding_store is None:
            return await self._embedding_client.embed(texts)
        model_name = str(getattr(self._embedding_client, "model_name", type(self._embedding_client).__name__))
        model_revision = str(getattr(self._embedding_client, "model_revision", "unknown"))
        keys = [
            ArticleEmbeddingKey(
                article_id=article.article_id,
                model_name=model_name,
                model_revision=model_revision,
                representation_version="article-title-abstract-v1",
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
            for article, text in zip(articles, texts, strict=True)
        ]
        cached = await self._embedding_store.get_many(keys)
        missing_indexes = [index for index, key in enumerate(keys) if key not in cached]
        if missing_indexes:
            generated = await self._embedding_client.embed([texts[index] for index in missing_indexes])
            if len(generated) != len(missing_indexes):
                raise RuntimeError("embedding 服务返回的文章向量数量与请求不一致")
            new_values = {keys[index]: vector for index, vector in zip(missing_indexes, generated, strict=True)}
            await self._embedding_store.put_many(new_values)
            cached.update(new_values)
        return [cached[key] for key in keys]


def _unique_terms(values: Sequence[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        term = _clean_text(value)
        normalized = _normalize_match_text(term)
        if not term or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(term)
    return result


def _clean_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_WORD_SEPARATOR.sub(" ", normalized).split())


def _concept_match_score(concept: ResearchConcept, *, title: str, abstract: str) -> float:
    normalized_terms = [_normalize_match_text(term) for term in concept.terms]
    if any(_contains_phrase(title, term) for term in normalized_terms):
        return 100.0
    if any(_contains_phrase(abstract, term) for term in normalized_terms):
        return 70.0
    return 0.0


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def _weighted_keyword_score(concept_scores: Mapping[ResearchConcept, float]) -> float:
    weighted_scores: list[tuple[float, float]] = []
    fallback_scores: list[float] = []
    for concept, score in concept_scores.items():
        weight = _DEFAULT_CATEGORY_WEIGHTS.get(concept.kind)
        if weight is None:
            fallback_scores.append(score)
        else:
            weighted_scores.append((weight, score))
    if weighted_scores:
        weight_total = sum(weight for weight, _ in weighted_scores)
        base = sum(weight * score for weight, score in weighted_scores) / weight_total
        if not fallback_scores:
            return base
        return 0.9 * base + 0.1 * (sum(fallback_scores) / len(fallback_scores))
    return sum(fallback_scores) / len(fallback_scores) if fallback_scores else 0.0


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding 向量必须非空且维度一致")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("embedding 向量不能为零向量")
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _cosine_similarities(
    query_vector: Sequence[float],
    article_vectors: Sequence[Sequence[float]],
) -> list[float]:
    """优先使用 NumPy 矩阵计算；异常输入保持明确失败。"""

    if not article_vectors:
        return []
    try:
        import numpy as np
    except ImportError:  # 兼容尚未同步新增依赖的开发环境。
        return [_cosine_similarity(query_vector, vector) for vector in article_vectors]
    query = np.asarray(query_vector, dtype=np.float32)
    matrix = np.asarray(article_vectors, dtype=np.float32)
    if query.ndim != 1 or matrix.ndim != 2 or matrix.shape[1] != query.shape[0]:
        raise ValueError("embedding 向量必须非空且维度一致")
    query_norm = np.linalg.norm(query)
    article_norms = np.linalg.norm(matrix, axis=1)
    if query_norm == 0 or np.any(article_norms == 0):
        raise ValueError("embedding 向量不能为零向量")
    values = (matrix @ query) / (article_norms * query_norm)
    return np.clip(values, -1.0, 1.0).astype(float).tolist()


def _recency_score(
    *,
    publication_date: date | None,
    publication_year: int | None,
    reference_date: date,
) -> float:
    effective_date = publication_date
    if effective_date is None and publication_year is not None:
        effective_date = date(publication_year, 7, 1)
    if effective_date is None:
        return 50.0
    age_years = max(0.0, (reference_date - effective_date).days / 365.25)
    return 20.0 + 80.0 * math.exp(-age_years / 7.0)


def _selection_reasons(
    *,
    matched_concepts: Sequence[str],
    core_intervention_matched: bool,
    publication_date: date | None,
    publication_year: int | None,
) -> list[str]:
    reasons: list[str] = []
    if core_intervention_matched:
        reasons.append("命中核心干预或药物术语")
    if matched_concepts:
        reasons.append("关键词命中：" + "、".join(matched_concepts))
    if publication_date is not None or publication_year is not None:
        reasons.append("已纳入发表时间近似分数")
    else:
        reasons.append("发表时间缺失，使用中性分")
    return reasons
