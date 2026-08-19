"""研究类型到摘要初筛 Rubric 的固定注册表。"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import StudyType


@dataclass(frozen=True)
class RubricDefinition:
    study_type: StudyType
    domain: str
    rules: str


def _rules(*items: tuple[str, str]) -> str:
    return "\n".join(
        f'{index}. criterion_id="{criterion_id}"：{description}'
        for index, (criterion_id, description) in enumerate(items, start=1)
    )


RUBRIC_REGISTRY: dict[StudyType, RubricDefinition] = {
    StudyType.RCT: RubricDefinition(
        StudyType.RCT,
        "随机对照试验（RCT）摘要初筛",
        _rules(
            ("rct_pico_clarity", "人群、干预、对照和主要结局是否清楚。"),
            ("rct_multicenter_setting", "是否明确研究场景及多中心信息；单中心不自动等于低质量。"),
            ("rct_intervention_timing", "干预持续时间、观察窗口与目标结局是否合理且可判断。"),
            ("rct_statistical_reporting", "是否报告适合结局的效应量、分母、不确定性和关键时间点。"),
            ("rct_recruitment_period", "结合样本量、场景和结局判断招募周期；证据不足时使用 unclear。"),
        ),
    ),
    StudyType.COHORT: RubricDefinition(
        StudyType.COHORT,
        "队列研究摘要初筛",
        _rules(
            ("cohort_pico_and_population", "人群、结局、数据来源及覆盖范围是否清楚。"),
            ("cohort_exposure_assignment", "暴露定义、分配层级和识别方法是否清楚，包括出生队列或政策资格。"),
            ("cohort_comparator_confounding", "比较队列来源及匹配、调整、分层或加权等混杂控制是否报告。"),
            ("cohort_outcome_and_time", "结局确认方式和暴露至结局的观察窗口是否适合研究问题。"),
            ("cohort_statistical_reporting", "是否报告效应量、置信区间、分母、模型和关键稳定性结果。"),
        ),
    ),
    StudyType.CASE_CONTROL: RubricDefinition(
        StudyType.CASE_CONTROL,
        "病例对照研究摘要初筛",
        _rules(
            ("case_definition", "病例是否明确定义，包括新发病例、诊断方式和数量。"),
            ("control_source", "对照是否来自与病例相同或可比的基础人群，并明确无目标疾病。"),
            ("case_control_comparability", "是否通过匹配或统计调整控制重要混杂；匹配不是必需条件。"),
            ("case_control_exposure", "病例和对照的暴露定义、测量一致性和时间顺序是否可判断。"),
            ("case_control_statistics", "是否报告 OR、95% CI、分母及必要的趋势或亚组结果。"),
        ),
    ),
    StudyType.CROSS_SECTIONAL_ANALYTICAL: RubricDefinition(
        StudyType.CROSS_SECTIONAL_ANALYTICAL,
        "分析型横断面研究摘要初筛",
        _rules(
            ("cross_sectional_population_sampling", "目标人群、来源、纳入方式和研究场景是否清楚。"),
            ("cross_sectional_exposure_measurement", "暴露定义、分类或测量方法是否清楚且一致。"),
            ("cross_sectional_outcome_measurement", "结局定义及测量工具或标准是否清楚、合理。"),
            ("cross_sectional_confounding_control", "是否识别并通过设计或多变量分析控制重要混杂。"),
            ("cross_sectional_statistical_reporting", "是否报告关联效应量、精确度和必要的调整结果。"),
        ),
    ),
    StudyType.CROSS_SECTIONAL_PREVALENCE: RubricDefinition(
        StudyType.CROSS_SECTIONAL_PREVALENCE,
        "患病率横断面研究摘要初筛",
        _rules(
            ("prevalence_sampling_frame", "抽样框是否覆盖目标人群，来源、地区和时间范围是否清楚。"),
            ("prevalence_recruitment_coverage", "招募方式、响应率或最终纳入人数是否足以判断选择性参与。"),
            ("prevalence_sample_size_precision", "样本量和置信区间等精度信息是否适合患病率估计。"),
            ("prevalence_condition_measurement", "病例定义、诊断标准或测量工具是否可靠并一致应用。"),
            ("prevalence_statistics", "是否报告样本基数、患病率、95% CI 及适用的复杂抽样校正。"),
        ),
    ),
    StudyType.SYSTEMATIC_REVIEW: RubricDefinition(
        StudyType.SYSTEMATIC_REVIEW,
        "系统评价摘要初筛",
        _rules(
            ("systematic_question_inclusion", "研究问题和纳入排除标准是否可识别。"),
            ("systematic_search_coverage", "主要数据库、检索时间和补充检索是否报告。"),
            ("systematic_appraisal_extraction", "是否评价研究质量/偏倚风险并说明资料提取或复核。"),
            ("systematic_synthesis_method", "合成方法是否适合问题和数据，并处理研究间差异。"),
            ("systematic_conclusion_support", "结论是否与结果一致并考虑质量、异质性和局限。"),
        ),
    ),
    StudyType.META_ANALYSIS: RubricDefinition(
        StudyType.META_ANALYSIS,
        "Meta 分析摘要初筛",
        _rules(
            ("meta_question_inclusion", "研究问题和纳入排除标准是否可识别。"),
            ("meta_search_coverage", "主要数据库、检索时间和补充检索是否报告。"),
            ("meta_appraisal_extraction", "是否评价研究质量/偏倚风险并说明资料提取或复核。"),
            ("meta_synthesis_method", "定量合成方法是否适合问题和数据。"),
            ("meta_conclusion_support", "结论是否与合并结果一致并考虑局限。"),
            ("meta_effect_measure_model", "是否报告效应量、95% CI 和固定/随机效应模型。"),
            ("meta_heterogeneity", "是否报告并解释 I²、tau² 或 Q 等异质性信息。"),
            ("meta_sensitivity_subgroup", "适用时是否报告敏感性、亚组或 Meta 回归并避免过度解释。"),
            ("meta_publication_bias", "纳入研究数量允许时是否评价小样本效应或发表偏倚；不适用时用 not_applicable。"),
        ),
    ),
    StudyType.QUALITATIVE_SYSTEMATIC_REVIEW: RubricDefinition(
        StudyType.QUALITATIVE_SYSTEMATIC_REVIEW,
        "定性系统评价摘要初筛",
        _rules(
            ("qualitative_systematic_question_inclusion", "研究问题、现象和纳入标准是否清楚。"),
            ("qualitative_systematic_search_coverage", "数据库、检索时段和补充检索是否报告。"),
            ("qualitative_systematic_appraisal_extraction", "是否评价研究质量并说明提取或复核。"),
            ("qualitative_systematic_thematic_synthesis", "编码、主题或其他定性合成过程是否可判断。"),
            ("qualitative_systematic_conclusion_support", "解释是否由合成结果支持并反映局限。"),
        ),
    ),
    StudyType.UMBRELLA_REVIEW: RubricDefinition(
        StudyType.UMBRELLA_REVIEW,
        "伞状评价摘要初筛",
        _rules(
            ("umbrella_question_inclusion", "问题及系统评价纳入标准是否清楚。"),
            ("umbrella_search_coverage", "系统评价检索来源和时段是否报告。"),
            ("umbrella_review_appraisal", "是否评价所纳入系统评价的质量。"),
            ("umbrella_synthesis_method", "跨评价合成方法是否清楚且适合。"),
            ("umbrella_conclusion_support", "结论是否由评价结果和证据质量支持。"),
            ("umbrella_overlap_management", "是否识别和处理系统评价间原始研究重叠。"),
            ("umbrella_conflicting_evidence", "是否识别并解释不同评价间的冲突结果。"),
        ),
    ),
    StudyType.NARRATIVE_REVIEW: RubricDefinition(
        StudyType.NARRATIVE_REVIEW,
        "叙述性综述摘要初筛",
        _rules(
            ("narrative_topic_scope", "主题范围、目标人群和关键问题是否清楚。"),
            ("narrative_evidence_selection", "证据选择来源和基本过程是否透明。"),
            ("narrative_balanced_discussion", "是否平衡呈现不同结果、争议和局限。"),
            ("narrative_conclusion_support", "结论是否由摘要所述证据支持且不过度外推。"),
        ),
    ),
}


def get_rubric(study_type: StudyType) -> RubricDefinition:
    if not isinstance(study_type, StudyType):
        raise TypeError("study_type 必须先通过 StudyType 枚举校验")
    return RUBRIC_REGISTRY[study_type]
