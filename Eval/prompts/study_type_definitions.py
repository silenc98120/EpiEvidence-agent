"""10 种流行病学研究类型的鉴别特征与偏倚优先级。

Routing Judge 使用 definition / key_features / confusion_boundaries 判断研究类型；
Ranking Judge 使用 bias_priority 给出偏倚致命度排序。

本模块只包含静态流行病学分类知识，不依赖 rubric_registry（Rubric 由
Criterion Judge 的 Prompt Loader 在运行时从生产注册表读取）。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.skills.quality_evaluator.schema import StudyType


@dataclass(frozen=True)
class StudyTypeDefinition:
    study_type: StudyType
    chinese_name: str
    definition: str
    key_features: str
    confusion_boundaries: str
    bias_priority: str


STUDY_TYPE_DEFINITIONS: dict[StudyType, StudyTypeDefinition] = {
    StudyType.RCT: StudyTypeDefinition(
        study_type=StudyType.RCT,
        chinese_name="随机对照试验",
        definition="研究者将参与者随机分配到干预组和对照组，前瞻性观察结局差异。",
        key_features=(
            "- 明确的随机分配（randomized allocation）\n"
            "- 干预组与对照组的前瞻性比较\n"
            "- 研究者控制干预措施"
        ),
        confusion_boundaries=(
            "- 非随机试验（quasi-experimental）：没有随机分配\n"
            "- 队列研究：暴露是自然发生的，非研究者分配\n"
            "- 前后对比研究：无同期对照组"
        ),
        bias_priority="随机化与分配隐藏缺陷 > 盲法破缺 > 选择性报告结局",
    ),
    StudyType.COHORT: StudyTypeDefinition(
        study_type=StudyType.COHORT,
        chinese_name="队列研究",
        definition="按暴露状态定义或识别研究人群，前瞻或回顾性随访观察结局发生。",
        key_features=(
            "- 按暴露状态分组（暴露/未暴露）\n"
            "- 存在时间序列：暴露先于结局\n"
            "- 可前瞻性或回顾性"
        ),
        confusion_boundaries=(
            "- 横断面研究：同一时点测暴露和结局，无时序\n"
            "- 病例对照：按结局选人后回溯暴露，方向相反\n"
            "- 注意出生队列、政策队列等特殊形式仍属队列"
        ),
        bias_priority="未控制混杂 > 暴露测量偏差 > 失访偏倚",
    ),
    StudyType.CASE_CONTROL: StudyTypeDefinition(
        study_type=StudyType.CASE_CONTROL,
        chinese_name="病例对照研究",
        definition="先按结局状态选择病例和对照，再回溯比较两组的暴露差异。",
        key_features=(
            "- 按结局状态选人：病例（有病）vs 对照（无病）\n"
            "- 回溯性暴露测量\n"
            "- 常用 OR 作为效应量"
        ),
        confusion_boundaries=(
            "- 队列研究中的巢式病例对照：仍属病例对照设计\n"
            "- 横断面研究：同时测量，无先选结局再溯暴露\n"
            "- 病例系列：只有病例，没有对照组"
        ),
        bias_priority="对照来源不可比 > 暴露回忆偏倚 > 选择偏倚",
    ),
    StudyType.CROSS_SECTIONAL_ANALYTICAL: StudyTypeDefinition(
        study_type=StudyType.CROSS_SECTIONAL_ANALYTICAL,
        chinese_name="分析型横断面研究",
        definition="在同一时间点同时测量暴露和结局，分析两者之间的关联。",
        key_features=(
            "- 同一时间点采集数据\n"
            "- 同时测量暴露和结局\n"
            "- 分析暴露-结局关联（有因果假设方向）"
        ),
        confusion_boundaries=(
            "- 患病率横断面：目的是估计频率，无暴露-结局分析\n"
            "- 队列研究：有暴露→结局的时间序列\n"
            "- 区分依据：是否检验暴露-结局关联假设"
        ),
        bias_priority="混杂控制不足 > 暴露-结局同时测量（因果方向不明） > 选择偏倚",
    ),
    StudyType.CROSS_SECTIONAL_PREVALENCE: StudyTypeDefinition(
        study_type=StudyType.CROSS_SECTIONAL_PREVALENCE,
        chinese_name="患病率横断面研究",
        definition="在特定时间点对目标人群进行调查，估计疾病或健康状态的频率。",
        key_features=(
            "- 主要目的是估计患病率或频率\n"
            "- 无暴露-结局因果假设\n"
            "- 关注抽样框、响应率和诊断标准"
        ),
        confusion_boundaries=(
            "- 分析型横断面：有暴露-结局关联分析\n"
            "- 区分依据：研究目的是估计频率还是分析关联"
        ),
        bias_priority="抽样框不覆盖目标人群 > 低响应率/选择性参与 > 诊断标准不一致",
    ),
    StudyType.SYSTEMATIC_REVIEW: StudyTypeDefinition(
        study_type=StudyType.SYSTEMATIC_REVIEW,
        chinese_name="系统评价",
        definition="预设研究方案，系统检索多个数据库，评价纳入研究质量，对结果进行定性合成。",
        key_features=(
            "- 预设方案和明确的纳入排除标准\n"
            "- 系统性、可重复的文献检索\n"
            "- 纳入研究的质量评价\n"
            "- 定性合成（非定量合并）"
        ),
        confusion_boundaries=(
            "- Meta 分析：在系统评价基础上进行了定量统计合并\n"
            "- 叙述性综述：没有系统检索和预设方案\n"
            "- 区分依据：是否有系统检索；是否有定量合并"
        ),
        bias_priority="检索不全面（遗漏重要数据库） > 质量评价缺失/不当 > 合成方法不适合",
    ),
    StudyType.META_ANALYSIS: StudyTypeDefinition(
        study_type=StudyType.META_ANALYSIS,
        chinese_name="Meta 分析",
        definition="在系统评价的基础上，使用统计方法定量合并多项研究的效应量。",
        key_features=(
            "- 系统评价的全部特征（系统检索、质量评价）\n"
            "- 定量统计合并（pooled effect）\n"
            "- 报告合并效应量和异质性"
        ),
        confusion_boundaries=(
            "- 纯定性系统评价：无定量合并\n"
            "- 叙述性综述：无系统检索\n"
            "- 网状 Meta 分析仍属 Meta 分析"
        ),
        bias_priority="异质性未充分解释 > 发表偏倚未评估 > 效应模型选择不当",
    ),
    StudyType.QUALITATIVE_SYSTEMATIC_REVIEW: StudyTypeDefinition(
        study_type=StudyType.QUALITATIVE_SYSTEMATIC_REVIEW,
        chinese_name="定性系统评价",
        definition="系统检索并纳入定性研究，通过主题分析、框架合成等方法整合定性证据。",
        key_features=(
            "- 纳入定性研究（质性研究）\n"
            "- 系统检索（与定量系统评价相同）\n"
            "- 使用主题分析、元民族志或框架合成等方法"
        ),
        confusion_boundaries=(
            "- 定量系统评价：纳入定量研究并合成数值结果\n"
            "- 混合方法系统评价：同时纳入定性和定量研究\n"
            "- 区分依据：纳入研究类型和合成方法"
        ),
        bias_priority="合成过程不透明（编码/主题生成不清） > 研究者反思性缺失 > 检索不全面",
    ),
    StudyType.UMBRELLA_REVIEW: StudyTypeDefinition(
        study_type=StudyType.UMBRELLA_REVIEW,
        chinese_name="伞状评价",
        definition="以系统评价或 Meta 分析为纳入单位，跨评价合成和比较证据。",
        key_features=(
            "- 纳入单位是已发表的系统评价/Meta 分析（非原始研究）\n"
            "- 评价纳入综述的方法学质量\n"
            "- 识别原始研究重叠"
        ),
        confusion_boundaries=(
            "- 常规系统评价：纳入原始研究\n"
            "- 区分依据：纳入单位是综述还是原始研究"
        ),
        bias_priority="原始研究重叠（独立性假设失效） > 纳入 SR 质量差异大 > 冲突结果未解释",
    ),
    StudyType.NARRATIVE_REVIEW: StudyTypeDefinition(
        study_type=StudyType.NARRATIVE_REVIEW,
        chinese_name="叙述性综述",
        definition="由专家根据经验和判断选择文献，以叙述方式综合论述某一主题。",
        key_features=(
            "- 非系统性文献选择\n"
            "- 无预设检索方案或纳入排除标准\n"
            "- 专家叙述和综合论述"
        ),
        confusion_boundaries=(
            "- 系统评价：有系统检索和预设方案\n"
            "- 只有在摘要明确显示系统检索、预设标准和规范合成时，才可将叙述性综述"
            "重新归类为系统评价\n"
            "- 区分依据：是否有系统、可重复的检索过程"
        ),
        bias_priority="证据选择偏倚（选择性引用） > 讨论不平衡（忽略反面证据） > 结论过度外推",
    ),
}


def get_study_type_definition(study_type: StudyType) -> StudyTypeDefinition:
    """按研究类型获取鉴别特征定义。"""
    if not isinstance(study_type, StudyType):
        raise TypeError("study_type 必须先通过 StudyType 枚举校验")
    return STUDY_TYPE_DEFINITIONS[study_type]
