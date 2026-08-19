"""数据库无关检索计划构建与数据库专属查询编译。"""

from enum import StrEnum
from time import perf_counter

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from app.core.intent_recognizer import (
    IntentCategory,
    IntentRecognitionResult,
    ResearchDirection,
    ResearchKeywordKind,
)
from app.core.mesh_normalizer import (
    MeshNormalizationResult,
    MeshNormalizedKeyword,
)


class BooleanOperator(StrEnum):
    """检索概念和术语之间允许使用的布尔运算符。"""

    AND = "AND"
    OR = "OR"


class SearchSource(StrEnum):
    """第一版支持编译检索式的数据源。"""

    PUBMED = "pubmed"
    EUROPE_PMC = "europe_pmc"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    PMC = "pmc"
    BIORXIV = "biorxiv"
    MEDRXIV = "medrxiv"


class SearchMeshHeading(BaseModel):
    """经过本地 MeSH 数据库确认的主题词。"""

    mesh_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=300)


class SearchConceptGroup(BaseModel):
    """一个研究概念及其同组检索术语。"""

    kind: ResearchKeywordKind
    raw_text: str = Field(min_length=1, max_length=200)
    mesh_headings: list[SearchMeshHeading] = Field(default_factory=list)
    free_text_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_has_searchable_terms(self) -> "SearchConceptGroup":
        if not self.mesh_headings and not self.free_text_terms:
            raise ValueError("检索概念组至少需要一个可检索术语")
        return self


class SearchPlan(BaseModel):
    """尚未绑定具体数据库语法的检索计划。"""

    directions: list[ResearchDirection] = Field(default_factory=list)
    concept_groups: list[SearchConceptGroup] = Field(min_length=1)
    within_group_operator: BooleanOperator = BooleanOperator.OR
    between_group_operator: BooleanOperator = BooleanOperator.AND


class CompiledSearchQuery(BaseModel):
    """由 SearchPlan 确定性编译出的单数据库检索式。"""

    source: SearchSource
    query: str = Field(min_length=1)
    concept_group_count: int = Field(ge=1)


def _deduplicate_terms(terms: list[str]) -> list[str]:
    """清理空白并按大小写不敏感规则保持顺序去重。"""

    deduplicated: list[str] = []
    seen: set[str] = set()

    for term in terms:
        cleaned = " ".join(term.strip().split())
        normalized = cleaned.casefold()

        if not cleaned or normalized in seen:
            continue

        seen.add(normalized)
        deduplicated.append(cleaned)

    return deduplicated


def _escape_phrase(term: str) -> str:
    """转义检索短语中的反斜杠和双引号。"""

    return term.replace("\\", "\\\\").replace('"', '\\"')


class SearchPlanBuilder:
    """将意图识别和 MeSH 标准化结果组装为统一 SearchPlan。"""

    def build(
        self,
        intent: IntentRecognitionResult,
        mesh_normalization: MeshNormalizationResult,
    ) -> SearchPlan:
        start_time = perf_counter()

        try:
            if intent.intent_category == IntentCategory.SIMPLE_CHAT:
                raise ValueError("simple_chat 不应生成文献检索计划")

            concept_groups = [
                self._build_concept_group(keyword)
                for keyword in mesh_normalization.normalized_keywords
            ]

            plan = SearchPlan(
                directions=intent.directions,
                concept_groups=concept_groups,
            )
        except Exception as exc:
            latency_ms = (perf_counter() - start_time) * 1000
            logger.bind(
                component="search_plan_builder",
                event="search_plan_build_failed",
                error_type=type(exc).__name__,
                latency_ms=round(latency_ms, 1),
            ).exception("检索计划构建失败")
            raise

        latency_ms = (perf_counter() - start_time) * 1000
        logger.bind(
            component="search_plan_builder",
            event="search_plan_built",
            concept_group_count=len(plan.concept_groups),
            mesh_heading_count=sum(
                len(group.mesh_headings) for group in plan.concept_groups
            ),
            free_text_term_count=sum(
                len(group.free_text_terms) for group in plan.concept_groups
            ),
            latency_ms=round(latency_ms, 1),
        ).info("检索计划构建完成")

        return plan

    def _build_concept_group(
        self,
        keyword: MeshNormalizedKeyword,
    ) -> SearchConceptGroup:
        mesh_headings_by_id: dict[str, SearchMeshHeading] = {}
        free_text_terms = list(keyword.lookup_terms)

        for match in keyword.mesh_matches:
            mesh_headings_by_id.setdefault(
                match.mesh_id,
                SearchMeshHeading(
                    mesh_id=match.mesh_id,
                    label=match.preferred_label,
                ),
            )
            free_text_terms.append(match.preferred_label)
            free_text_terms.extend(match.entry_terms)

        return SearchConceptGroup(
            kind=keyword.kind,
            raw_text=keyword.raw_text,
            mesh_headings=list(mesh_headings_by_id.values()),
            free_text_terms=_deduplicate_terms(free_text_terms),
        )


class PubMedQueryCompiler:
    """将统一 SearchPlan 编译为 PubMed 查询语法。"""

    source = SearchSource.PUBMED

    def compile(self, plan: SearchPlan) -> CompiledSearchQuery:
        start_time = perf_counter()
        groups = [self._compile_group(group, plan) for group in plan.concept_groups]
        query = f" {plan.between_group_operator.value} ".join(groups)

        compiled_query = CompiledSearchQuery(
            source=self.source,
            query=query,
            concept_group_count=len(groups),
        )

        _log_compilation(compiled_query, start_time)
        return compiled_query

    def _compile_group(
        self,
        group: SearchConceptGroup,
        plan: SearchPlan,
    ) -> str:
        clauses = [
            f'"{_escape_phrase(heading.label)}"[Mesh]'
            for heading in group.mesh_headings
        ]
        clauses.extend(
            f'"{_escape_phrase(term)}"[Title/Abstract]'
            for term in group.free_text_terms
        )

        joined = f" {plan.within_group_operator.value} ".join(clauses)
        return f"({joined})"


class EuropePMCQueryCompiler:
    """将统一 SearchPlan 编译为 Europe PMC 查询语法。"""

    source = SearchSource.EUROPE_PMC

    def compile(self, plan: SearchPlan) -> CompiledSearchQuery:
        start_time = perf_counter()
        groups = [self._compile_group(group, plan) for group in plan.concept_groups]
        query = f" {plan.between_group_operator.value} ".join(groups)

        compiled_query = CompiledSearchQuery(
            source=self.source,
            query=query,
            concept_group_count=len(groups),
        )

        _log_compilation(compiled_query, start_time)
        return compiled_query

    def _compile_group(
        self,
        group: SearchConceptGroup,
        plan: SearchPlan,
    ) -> str:
        clauses = [
            f'MESH:"{_escape_phrase(heading.label)}"'
            for heading in group.mesh_headings
        ]
        clauses.extend(
            f'TITLE_ABS:"{_escape_phrase(term)}"'
            for term in group.free_text_terms
        )

        joined = f" {plan.within_group_operator.value} ".join(clauses)
        return f"({joined})"


class PMCQueryCompiler(PubMedQueryCompiler):
    """PMC ESearch 使用与 PubMed 相同的字段和布尔语法。"""

    source = SearchSource.PMC


class SemanticScholarQueryCompiler:
    """将 SearchPlan 降级为 Semantic Scholar 支持的普通文本查询。"""

    source = SearchSource.SEMANTIC_SCHOLAR

    def compile(self, plan: SearchPlan) -> CompiledSearchQuery:
        start_time = perf_counter()
        representatives = [
            self._representative_term(group) for group in plan.concept_groups
        ]
        query = " ".join(f'"{_escape_phrase(term)}"' for term in representatives)
        compiled_query = CompiledSearchQuery(
            source=self.source,
            query=query,
            concept_group_count=len(representatives),
        )
        _log_compilation(compiled_query, start_time)
        return compiled_query

    @staticmethod
    def _representative_term(group: SearchConceptGroup) -> str:
        if group.mesh_headings:
            return group.mesh_headings[0].label
        if group.free_text_terms:
            return group.free_text_terms[0]
        raise ValueError("Semantic Scholar 概念组缺少代表性检索词")


def _log_compilation(
    compiled_query: CompiledSearchQuery,
    start_time: float,
) -> None:
    """记录编译指标，不写入原始医学检索式。"""

    latency_ms = (perf_counter() - start_time) * 1000
    logger.bind(
        component="search_query_compiler",
        event="search_query_compiled",
        source=compiled_query.source.value,
        concept_group_count=compiled_query.concept_group_count,
        query_length=len(compiled_query.query),
        latency_ms=round(latency_ms, 1),
    ).info("数据库检索式编译完成")
