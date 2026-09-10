"""Quality Evaluator 的输入、LLM 输出和研究类型 Rubric 数据契约。"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, ClassVar, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StudyType(StrEnum):
    RCT = "randomized_controlled_trial"
    COHORT = "cohort"
    CASE_CONTROL = "case_control"
    CROSS_SECTIONAL_ANALYTICAL = "cross_sectional_analytical"
    CROSS_SECTIONAL_PREVALENCE = "cross_sectional_prevalence"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    QUALITATIVE_SYSTEMATIC_REVIEW = "qualitative_systematic_review"
    UMBRELLA_REVIEW = "umbrella_review"
    NARRATIVE_REVIEW = "narrative_review"


class RelevanceLevel(StrEnum):
    HIGH = "high"
    PARTIAL = "partial"
    LOW = "low"


class CriterionStatus(StrEnum):
    FAVORABLE = "favorable"
    UNCLEAR = "unclear"
    NOT_REPORTED = "not_reported"
    NOT_APPLICABLE = "not_applicable"


class EvidenceSource(StrEnum):
    TITLE = "title"
    ABSTRACT = "abstract"
    PROVIDED_METADATA = "provided_metadata"
    RUBRIC = "rubric"


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class CoverageStatus(StrEnum):
    DIRECT = "direct"
    PARTIAL = "partial"
    INDIRECT = "indirect"
    ABSENT = "absent"
    CONFLICTING = "conflicting"


class ContributionRole(StrEnum):
    CORE_EVIDENCE = "core_evidence"
    POPULATION_COMPLEMENT = "population_complement"
    OUTCOME_COMPLEMENT = "outcome_complement"
    DESIGN_COMPLEMENT = "design_complement"
    TIME_COMPLEMENT = "time_complement"
    REPLICATION = "replication"
    LIMITED_CONTRIBUTION = "limited_contribution"


class RCTCriterionId(StrEnum):
    PICO_CLARITY = "rct_pico_clarity"
    MULTICENTER_SETTING = "rct_multicenter_setting"
    INTERVENTION_TIMING = "rct_intervention_timing"
    STATISTICAL_REPORTING = "rct_statistical_reporting"
    RECRUITMENT_PERIOD = "rct_recruitment_period"


class CohortCriterionId(StrEnum):
    PICO_AND_POPULATION = "cohort_pico_and_population"
    EXPOSURE_ASSIGNMENT = "cohort_exposure_assignment"
    COMPARATOR_CONFOUNDING = "cohort_comparator_confounding"
    OUTCOME_AND_TIME = "cohort_outcome_and_time"
    STATISTICAL_REPORTING = "cohort_statistical_reporting"


class CaseControlCriterionId(StrEnum):
    CASE_DEFINITION = "case_definition"
    CONTROL_SOURCE = "control_source"
    COMPARABILITY = "case_control_comparability"
    EXPOSURE = "case_control_exposure"
    STATISTICS = "case_control_statistics"


class AnalyticalCrossSectionalCriterionId(StrEnum):
    POPULATION_SAMPLING = "cross_sectional_population_sampling"
    EXPOSURE_MEASUREMENT = "cross_sectional_exposure_measurement"
    OUTCOME_MEASUREMENT = "cross_sectional_outcome_measurement"
    CONFOUNDING_CONTROL = "cross_sectional_confounding_control"
    STATISTICAL_REPORTING = "cross_sectional_statistical_reporting"


class PrevalenceCrossSectionalCriterionId(StrEnum):
    SAMPLING_FRAME = "prevalence_sampling_frame"
    RECRUITMENT_COVERAGE = "prevalence_recruitment_coverage"
    SAMPLE_SIZE_PRECISION = "prevalence_sample_size_precision"
    CONDITION_MEASUREMENT = "prevalence_condition_measurement"
    PREVALENCE_STATISTICS = "prevalence_statistics"


class SystematicReviewCriterionId(StrEnum):
    QUESTION_INCLUSION = "systematic_question_inclusion"
    SEARCH_COVERAGE = "systematic_search_coverage"
    APPRAISAL_EXTRACTION = "systematic_appraisal_extraction"
    SYNTHESIS_METHOD = "systematic_synthesis_method"
    CONCLUSION_SUPPORT = "systematic_conclusion_support"


class MetaAnalysisCriterionId(StrEnum):
    QUESTION_INCLUSION = "meta_question_inclusion"
    SEARCH_COVERAGE = "meta_search_coverage"
    APPRAISAL_EXTRACTION = "meta_appraisal_extraction"
    SYNTHESIS_METHOD = "meta_synthesis_method"
    CONCLUSION_SUPPORT = "meta_conclusion_support"
    EFFECT_MEASURE_MODEL = "meta_effect_measure_model"
    HETEROGENEITY = "meta_heterogeneity"
    SENSITIVITY_SUBGROUP = "meta_sensitivity_subgroup"
    PUBLICATION_BIAS = "meta_publication_bias"


class QualitativeSystematicReviewCriterionId(StrEnum):
    QUESTION_INCLUSION = "qualitative_systematic_question_inclusion"
    SEARCH_COVERAGE = "qualitative_systematic_search_coverage"
    APPRAISAL_EXTRACTION = "qualitative_systematic_appraisal_extraction"
    THEMATIC_SYNTHESIS = "qualitative_systematic_thematic_synthesis"
    CONCLUSION_SUPPORT = "qualitative_systematic_conclusion_support"


class UmbrellaReviewCriterionId(StrEnum):
    QUESTION_INCLUSION = "umbrella_question_inclusion"
    SEARCH_COVERAGE = "umbrella_search_coverage"
    REVIEW_APPRAISAL = "umbrella_review_appraisal"
    SYNTHESIS_METHOD = "umbrella_synthesis_method"
    CONCLUSION_SUPPORT = "umbrella_conclusion_support"
    OVERLAP_MANAGEMENT = "umbrella_overlap_management"
    CONFLICTING_EVIDENCE = "umbrella_conflicting_evidence"


class NarrativeReviewCriterionId(StrEnum):
    TOPIC_SCOPE = "narrative_topic_scope"
    EVIDENCE_SELECTION = "narrative_evidence_selection"
    BALANCED_DISCUSSION = "narrative_balanced_discussion"
    CONCLUSION_SUPPORT = "narrative_conclusion_support"


class QualityEvaluationJob(_StrictModel):
    """LangGraph 分支中只携带 UUID 的质量评价任务。"""

    task_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    study_type: StudyType
    article_ids: list[UUID] = Field(min_length=1, max_length=10)

    @field_validator("article_ids")
    @classmethod
    def validate_unique_article_ids(cls, article_ids: list[UUID]) -> list[UUID]:
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("article_ids 不能重复")
        return article_ids


class EvaluationArticle(_StrictModel):
    """从 PostgreSQL articles 表补全的摘要评价输入。"""

    article_id: UUID
    title: str = Field(min_length=1)
    abstract: str | None = None
    abstract_available: bool | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    authors: list[dict[str, str | None]] = Field(default_factory=list)
    first_author: str | None = None
    journal_title: str | None = None
    journal_abbreviation: str | None = None
    publication_date: date | None = None
    publication_year: int | None = Field(default=None, ge=1500, le=3000)
    publication_types: list[str] = Field(default_factory=list)
    study_design: str | None = None
    publication_status: str | None = None
    language: str | None = None
    is_retracted: bool = False


class HydratedArticleBatch(_StrictModel):
    """准备序列化为用户消息的完整同类文章批次。"""

    job: QualityEvaluationJob
    user_query: str = Field(min_length=1)
    articles: list[EvaluationArticle] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_articles_match_job(self) -> "HydratedArticleBatch":
        hydrated_ids = [article.article_id for article in self.articles]
        if len(hydrated_ids) != len(set(hydrated_ids)):
            raise ValueError("数据库返回的文章 UUID 不能重复")
        if set(hydrated_ids) != set(self.job.article_ids):
            raise ValueError("数据库返回的文章 UUID 必须与任务完全一致")
        return self


class RelevanceAssessment(_StrictModel):
    article_id: UUID
    score: float = Field(ge=0.0, le=10.0)
    level: RelevanceLevel
    reason: str = Field(min_length=1)
    answerability_score: float = Field(ge=0.0, le=10.0)
    answerability_reason: str = Field(min_length=1)
    matched_dimensions: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_level_matches_score(self) -> "RelevanceAssessment":
        expected = (
            RelevanceLevel.HIGH
            if self.score >= 8
            else RelevanceLevel.PARTIAL
            if self.score >= 4
            else RelevanceLevel.LOW
        )
        if self.level != expected:
            raise ValueError("相关性等级与分数不一致")
        return self


CriterionIdT = TypeVar("CriterionIdT", bound=StrEnum)


class CriterionAssessment(_StrictModel, Generic[CriterionIdT]):
    criterion_id: CriterionIdT
    status: CriterionStatus
    score: float | None
    evidence_quote: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_source: EvidenceSource

    @model_validator(mode="after")
    def validate_status_contract(self) -> "CriterionAssessment[CriterionIdT]":
        expected_scores: dict[CriterionStatus, float | None] = {
            CriterionStatus.FAVORABLE: 1.0,
            CriterionStatus.UNCLEAR: -0.5,
            CriterionStatus.NOT_REPORTED: -1.0,
            CriterionStatus.NOT_APPLICABLE: None,
        }
        if self.score != expected_scores[self.status]:
            raise ValueError("score 必须等于 status 对应的固定分值")
        if (
            self.status == CriterionStatus.NOT_REPORTED
            and self.evidence_quote != "摘要未报告"
        ):
            raise ValueError("not_reported 的 evidence_quote 必须为“摘要未报告”")
        return self


class RCTCriterionAssessment(CriterionAssessment[RCTCriterionId]):
    pass


class StudyQualityAssessmentBase(_StrictModel, Generic[CriterionIdT]):
    article_id: UUID
    study_type: StudyType
    criteria: list[CriterionAssessment[CriterionIdT]] = Field(min_length=1)
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    criterion_enum: ClassVar[type[StrEnum]]

    @model_validator(mode="after")
    def validate_complete_rubric(self) -> "StudyQualityAssessmentBase[CriterionIdT]":
        expected = {criterion.value for criterion in self.criterion_enum}
        actual = [criterion.criterion_id.value for criterion in self.criteria]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("criteria 必须完整覆盖当前研究类型 Rubric 且不能重复")
        return self


class RCTQualityAssessment(StudyQualityAssessmentBase[RCTCriterionId]):
    study_type: Literal[StudyType.RCT]
    criteria: list[RCTCriterionAssessment] = Field(min_length=5, max_length=5)
    criterion_enum = RCTCriterionId


class CohortQualityAssessment(StudyQualityAssessmentBase[CohortCriterionId]):
    study_type: Literal[StudyType.COHORT]
    criteria: list[CriterionAssessment[CohortCriterionId]] = Field(
        min_length=5,
        max_length=5,
    )
    criterion_enum = CohortCriterionId


class CaseControlQualityAssessment(StudyQualityAssessmentBase[CaseControlCriterionId]):
    study_type: Literal[StudyType.CASE_CONTROL]
    criteria: list[CriterionAssessment[CaseControlCriterionId]] = Field(
        min_length=5,
        max_length=5,
    )
    criterion_enum = CaseControlCriterionId


class AnalyticalCrossSectionalQualityAssessment(
    StudyQualityAssessmentBase[AnalyticalCrossSectionalCriterionId]
):
    study_type: Literal[StudyType.CROSS_SECTIONAL_ANALYTICAL]
    criteria: list[CriterionAssessment[AnalyticalCrossSectionalCriterionId]] = Field(
        min_length=5,
        max_length=5,
    )
    criterion_enum = AnalyticalCrossSectionalCriterionId


class PrevalenceCrossSectionalQualityAssessment(
    StudyQualityAssessmentBase[PrevalenceCrossSectionalCriterionId]
):
    study_type: Literal[StudyType.CROSS_SECTIONAL_PREVALENCE]
    criteria: list[CriterionAssessment[PrevalenceCrossSectionalCriterionId]] = Field(
        min_length=5,
        max_length=5,
    )
    criterion_enum = PrevalenceCrossSectionalCriterionId


class SystematicReviewQualityAssessment(
    StudyQualityAssessmentBase[SystematicReviewCriterionId]
):
    study_type: Literal[StudyType.SYSTEMATIC_REVIEW]
    criteria: list[CriterionAssessment[SystematicReviewCriterionId]] = Field(
        min_length=5,
        max_length=5,
    )
    criterion_enum = SystematicReviewCriterionId


class MetaAnalysisQualityAssessment(
    StudyQualityAssessmentBase[MetaAnalysisCriterionId]
):
    study_type: Literal[StudyType.META_ANALYSIS]
    criteria: list[CriterionAssessment[MetaAnalysisCriterionId]] = Field(
        min_length=9,
        max_length=9,
    )
    criterion_enum = MetaAnalysisCriterionId


class QualitativeSystematicReviewQualityAssessment(
    StudyQualityAssessmentBase[QualitativeSystematicReviewCriterionId]
):
    study_type: Literal[StudyType.QUALITATIVE_SYSTEMATIC_REVIEW]
    criteria: list[CriterionAssessment[QualitativeSystematicReviewCriterionId]] = Field(
        min_length=5,
        max_length=5,
    )
    criterion_enum = QualitativeSystematicReviewCriterionId


class UmbrellaReviewQualityAssessment(
    StudyQualityAssessmentBase[UmbrellaReviewCriterionId]
):
    study_type: Literal[StudyType.UMBRELLA_REVIEW]
    criteria: list[CriterionAssessment[UmbrellaReviewCriterionId]] = Field(
        min_length=7,
        max_length=7,
    )
    criterion_enum = UmbrellaReviewCriterionId


class NarrativeReviewQualityAssessment(
    StudyQualityAssessmentBase[NarrativeReviewCriterionId]
):
    study_type: Literal[StudyType.NARRATIVE_REVIEW]
    criteria: list[CriterionAssessment[NarrativeReviewCriterionId]] = Field(
        min_length=4,
        max_length=4,
    )
    criterion_enum = NarrativeReviewCriterionId


StudyQualityAssessment = Annotated[
    RCTQualityAssessment
    | CohortQualityAssessment
    | CaseControlQualityAssessment
    | AnalyticalCrossSectionalQualityAssessment
    | PrevalenceCrossSectionalQualityAssessment
    | SystematicReviewQualityAssessment
    | MetaAnalysisQualityAssessment
    | QualitativeSystematicReviewQualityAssessment
    | UmbrellaReviewQualityAssessment
    | NarrativeReviewQualityAssessment,
    Field(discriminator="study_type"),
]


CRITERION_ENUM_BY_STUDY_TYPE: dict[StudyType, type[StrEnum]] = {
    StudyType.RCT: RCTCriterionId,
    StudyType.COHORT: CohortCriterionId,
    StudyType.CASE_CONTROL: CaseControlCriterionId,
    StudyType.CROSS_SECTIONAL_ANALYTICAL: AnalyticalCrossSectionalCriterionId,
    StudyType.CROSS_SECTIONAL_PREVALENCE: PrevalenceCrossSectionalCriterionId,
    StudyType.SYSTEMATIC_REVIEW: SystematicReviewCriterionId,
    StudyType.META_ANALYSIS: MetaAnalysisCriterionId,
    StudyType.QUALITATIVE_SYSTEMATIC_REVIEW: QualitativeSystematicReviewCriterionId,
    StudyType.UMBRELLA_REVIEW: UmbrellaReviewCriterionId,
    StudyType.NARRATIVE_REVIEW: NarrativeReviewCriterionId,
}

QUALITY_MODEL_BY_STUDY_TYPE: dict[
    StudyType,
    type[StudyQualityAssessmentBase],
] = {
    StudyType.RCT: RCTQualityAssessment,
    StudyType.COHORT: CohortQualityAssessment,
    StudyType.CASE_CONTROL: CaseControlQualityAssessment,
    StudyType.CROSS_SECTIONAL_ANALYTICAL: AnalyticalCrossSectionalQualityAssessment,
    StudyType.CROSS_SECTIONAL_PREVALENCE: PrevalenceCrossSectionalQualityAssessment,
    StudyType.SYSTEMATIC_REVIEW: SystematicReviewQualityAssessment,
    StudyType.META_ANALYSIS: MetaAnalysisQualityAssessment,
    StudyType.QUALITATIVE_SYSTEMATIC_REVIEW: (
        QualitativeSystematicReviewQualityAssessment
    ),
    StudyType.UMBRELLA_REVIEW: UmbrellaReviewQualityAssessment,
    StudyType.NARRATIVE_REVIEW: NarrativeReviewQualityAssessment,
}


class ArticleAssessmentBatch(_StrictModel):
    """单次同类批量调用返回的相关性和质量评价。"""

    task_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    study_type: StudyType
    article_ids: list[UUID] = Field(min_length=1, max_length=10)
    relevance_assessments: list[RelevanceAssessment] = Field(
        min_length=1,
        max_length=10,
    )
    quality_assessments: list[StudyQualityAssessment] = Field(
        min_length=1,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_batch_alignment(self) -> "ArticleAssessmentBatch":
        expected_ids = set(self.article_ids)
        if len(expected_ids) != len(self.article_ids):
            raise ValueError("article_ids 不能重复")

        relevance_ids = [item.article_id for item in self.relevance_assessments]
        quality_ids = [item.article_id for item in self.quality_assessments]
        if (
            len(relevance_ids) != len(set(relevance_ids))
            or len(quality_ids) != len(set(quality_ids))
            or set(relevance_ids) != expected_ids
            or set(quality_ids) != expected_ids
        ):
            raise ValueError("两个评价部分必须完整覆盖 article_ids 且不能重复")

        if any(item.study_type != self.study_type for item in self.quality_assessments):
            raise ValueError("质量评价类型必须与批次 study_type 一致")
        return self


_ARTICLE_BATCH_MODEL_BY_STUDY_TYPE: dict[
    StudyType,
    type[ArticleAssessmentBatch],
] = {}


def article_assessment_model_for(
    study_type: StudyType,
) -> type[ArticleAssessmentBatch]:
    """返回只暴露当前研究类型 quality schema 的 batch 模型。"""

    if not isinstance(study_type, StudyType):
        raise TypeError("study_type 必须先通过 StudyType 枚举校验")
    cached = _ARTICLE_BATCH_MODEL_BY_STUDY_TYPE.get(study_type)
    if cached is not None:
        return cached

    quality_model = QUALITY_MODEL_BY_STUDY_TYPE[study_type]
    batch_model = create_model(
        f"{quality_model.__name__}Batch",
        __base__=ArticleAssessmentBatch,
        quality_assessments=(
            list[quality_model],  # type: ignore[valid-type]
            Field(min_length=1, max_length=10),
        ),
    )
    _ARTICLE_BATCH_MODEL_BY_STUDY_TYPE[study_type] = batch_model
    return batch_model


def _expected_criterion_score(status: CriterionStatus) -> float | None:
    return {
        CriterionStatus.FAVORABLE: 1.0,
        CriterionStatus.UNCLEAR: -0.5,
        CriterionStatus.NOT_REPORTED: -1.0,
        CriterionStatus.NOT_APPLICABLE: None,
    }[status]


def _expected_relevance_level(score: float) -> RelevanceLevel:
    if score >= 8:
        return RelevanceLevel.HIGH
    if score >= 4:
        return RelevanceLevel.PARTIAL
    return RelevanceLevel.LOW


class RelevanceVerificationItem(_StrictModel):
    status: VerificationStatus
    original_level: RelevanceLevel
    original_score: float = Field(ge=0.0, le=10.0)
    corrected_level: RelevanceLevel
    corrected_score: float = Field(ge=0.0, le=10.0)
    evidence_quote_valid: bool
    reason_supported: bool
    issue: str | None = None
    verification_evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_verification(self) -> "RelevanceVerificationItem":
        if self.original_level != _expected_relevance_level(self.original_score):
            raise ValueError("原始相关性等级与分数不一致")
        if self.corrected_level != _expected_relevance_level(self.corrected_score):
            raise ValueError("修正相关性等级与分数不一致")
        if self.status == VerificationStatus.PASSED:
            if (
                self.original_level != self.corrected_level
                or self.original_score != self.corrected_score
            ):
                raise ValueError("passed 核查不得改变相关性评分")
            if not self.evidence_quote_valid or not self.reason_supported:
                raise ValueError("passed 核查必须确认引文和理由")
            if self.issue is not None:
                raise ValueError("passed 核查不能包含 issue")
        elif not self.issue:
            raise ValueError("failed 核查必须说明 issue")
        return self


class QualityVerificationItem(_StrictModel):
    criterion_id: str = Field(min_length=1)
    status: VerificationStatus
    original_status: CriterionStatus
    original_score: float | None
    corrected_status: CriterionStatus
    corrected_score: float | None
    evidence_quote_valid: bool
    reason_supported: bool
    issue: str | None = None
    verification_evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_verification(self) -> "QualityVerificationItem":
        if self.original_score != _expected_criterion_score(self.original_status):
            raise ValueError("原始质量状态对应的固定分值不正确")
        if self.corrected_score != _expected_criterion_score(self.corrected_status):
            raise ValueError("修正质量状态对应的固定分值不正确")
        if (
            self.corrected_status == CriterionStatus.NOT_REPORTED
            and self.verification_evidence != "摘要未报告"
        ):
            raise ValueError("not_reported 的核查证据必须为“摘要未报告”")
        if self.status == VerificationStatus.PASSED:
            if (
                self.original_status != self.corrected_status
                or self.original_score != self.corrected_score
            ):
                raise ValueError("passed 核查不得改变质量评分")
            if not self.evidence_quote_valid or not self.reason_supported:
                raise ValueError("passed 核查必须确认引文和理由")
            if self.issue is not None:
                raise ValueError("passed 核查不能包含 issue")
        elif not self.issue:
            raise ValueError("failed 核查必须说明 issue")
        return self


class AnswerabilityVerificationItem(_StrictModel):
    status: VerificationStatus
    original_score: float = Field(ge=0.0, le=10.0)
    corrected_score: float = Field(ge=0.0, le=10.0)
    reason_supported: bool
    issue: str | None = None
    verification_evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_verification(self) -> "AnswerabilityVerificationItem":
        if self.status == VerificationStatus.PASSED:
            if self.original_score != self.corrected_score:
                raise ValueError("passed 核查不得改变回答问题能力评分")
            if not self.reason_supported:
                raise ValueError("passed 核查必须确认回答问题能力理由")
            if self.issue is not None:
                raise ValueError("passed 核查不能包含 issue")
        elif not self.issue:
            raise ValueError("failed 核查必须说明 issue")
        return self


class ArticleVerification(_StrictModel):
    article_id: UUID
    original_total_score: float = Field(ge=0.0, le=10.0)
    status: VerificationStatus
    relevance: RelevanceVerificationItem
    answerability: AnswerabilityVerificationItem
    quality_items: list[QualityVerificationItem] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_article_status(self) -> "ArticleVerification":
        item_statuses = [
            self.relevance.status,
            self.answerability.status,
            *(item.status for item in self.quality_items),
        ]
        expected = (
            VerificationStatus.FAILED
            if VerificationStatus.FAILED in item_statuses
            else VerificationStatus.PASSED
        )
        if self.status != expected:
            raise ValueError("文章核查状态必须与逐项核查结果一致")
        criterion_ids = [item.criterion_id for item in self.quality_items]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("quality_items 的 criterion_id 不能重复")
        return self


class VerificationBatch(_StrictModel):
    task_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    study_type: StudyType
    article_ids: list[UUID] = Field(min_length=1, max_length=10)
    article_verifications: list[ArticleVerification] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_batch_alignment(self) -> "VerificationBatch":
        expected_ids = self.article_ids
        actual_ids = [item.article_id for item in self.article_verifications]
        if (
            len(expected_ids) != len(set(expected_ids))
            or len(actual_ids) != len(set(actual_ids))
            or set(actual_ids) != set(expected_ids)
        ):
            raise ValueError("article_verifications 必须完整覆盖 article_ids 且不能重复")

        expected_criteria = {
            criterion.value for criterion in CRITERION_ENUM_BY_STUDY_TYPE[self.study_type]
        }
        for article in self.article_verifications:
            actual_criteria = {item.criterion_id for item in article.quality_items}
            if actual_criteria != expected_criteria:
                raise ValueError("核查项目必须完整覆盖当前研究类型 Rubric")
        return self


class CoverageAssessment(_StrictModel):
    concept: str = Field(min_length=1)
    status: CoverageStatus
    evidence_article_ids: list[UUID] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> "CoverageAssessment":
        if len(self.evidence_article_ids) != len(set(self.evidence_article_ids)):
            raise ValueError("coverage evidence_article_ids 不能重复")
        if self.status != CoverageStatus.ABSENT and not self.evidence_article_ids:
            raise ValueError("非 absent 覆盖判断必须引用文章 UUID")
        return self


class QueryCoverage(_StrictModel):
    required_concepts: list[str] = Field(min_length=1)
    explicit_constraints: list[str] = Field(default_factory=list)
    keyword_coverage: list[CoverageAssessment] = Field(min_length=1)
    constraint_coverage: list[CoverageAssessment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_concept_coverage(self) -> "QueryCoverage":
        concepts = self.required_concepts
        covered = [item.concept for item in self.keyword_coverage]
        if (
            len(concepts) != len(set(concepts))
            or len(covered) != len(set(covered))
            or set(concepts) != set(covered)
        ):
            raise ValueError("keyword_coverage 必须完整覆盖 required_concepts")
        constraints = self.explicit_constraints
        covered_constraints = [item.concept for item in self.constraint_coverage]
        if (
            len(constraints) != len(set(constraints))
            or len(covered_constraints) != len(set(covered_constraints))
            or set(constraints) != set(covered_constraints)
        ):
            raise ValueError("constraint_coverage 必须完整覆盖 explicit_constraints")
        return self


class JointQueryCoverage(_StrictModel):
    status: CoverageStatus
    evidence_article_ids: list[UUID] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_ids(self) -> "JointQueryCoverage":
        if len(self.evidence_article_ids) != len(set(self.evidence_article_ids)):
            raise ValueError("joint coverage evidence_article_ids 不能重复")
        if self.status != CoverageStatus.ABSENT and not self.evidence_article_ids:
            raise ValueError("非 absent 联合覆盖必须引用文章 UUID")
        return self


class RedundancyOrConflict(_StrictModel):
    status: Literal[CoverageStatus.CONFLICTING, "redundant"]
    article_ids: list[UUID] = Field(min_length=2)
    reason: str = Field(min_length=1)

    @field_validator("article_ids")
    @classmethod
    def validate_unique_ids(cls, article_ids: list[UUID]) -> list[UUID]:
        if len(article_ids) != len(set(article_ids)):
            raise ValueError("重复或冲突文章 UUID 不能重复")
        return article_ids


class ArticleContribution(_StrictModel):
    article_id: UUID
    role: ContributionRole
    covered_concepts: list[str] = Field(default_factory=list)
    coverage_contribution_score: float = Field(ge=0.0, le=10.0)
    reason: str = Field(min_length=1)


class PortfolioEvaluation(_StrictModel):
    task_id: str = Field(min_length=1)
    candidate_article_ids: list[UUID] = Field(min_length=1)
    query_coverage: QueryCoverage
    joint_query_coverage: JointQueryCoverage
    coverage_gaps: list[str] = Field(default_factory=list)
    redundancy_and_conflicts: list[RedundancyOrConflict] = Field(default_factory=list)
    article_contributions: list[ArticleContribution] = Field(min_length=1)
    recommendation: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_candidate_alignment(self) -> "PortfolioEvaluation":
        expected_ids = self.candidate_article_ids
        contribution_ids = [item.article_id for item in self.article_contributions]
        if (
            len(expected_ids) != len(set(expected_ids))
            or len(contribution_ids) != len(set(contribution_ids))
            or set(expected_ids) != set(contribution_ids)
        ):
            raise ValueError(
                "article_contributions 必须完整覆盖 candidate_article_ids 且不能重复"
            )
        referenced_ids = {
            article_id
            for item in self.query_coverage.keyword_coverage
            for article_id in item.evidence_article_ids
        }
        referenced_ids.update(
            article_id
            for item in self.query_coverage.constraint_coverage
            for article_id in item.evidence_article_ids
        )
        referenced_ids.update(self.joint_query_coverage.evidence_article_ids)
        for item in self.redundancy_and_conflicts:
            referenced_ids.update(item.article_ids)
        if not referenced_ids.issubset(set(expected_ids)):
            raise ValueError("集合评价不得引用候选集之外的文章 UUID")
        return self
