from typing import Annotated, Any, Optional, TypedDict
from operator import add

from langgraph.graph import MessagesState


class SingleEvidenceEvaluation(TypedDict):
    """记录每条检索结果质量评价的State变量。"""
    # 检索结果的唯一标识符
    result_id:str
    # 文献质量评分结果
    quality_score:Optional[int]
    # 证据质量等级
    quality_level:Optional[str]
    # 排除原因
    exclusion_reason:Optional[str]


class EvidenceState(MessagesState):
    """一次 EpiEvidence 任务在 LangGraph 中流转的可序列化状态。

    ``messages`` 由父类 ``MessagesState`` 提供，使用 LangGraph 自己的
    ``add_messages`` reducer；这里不重复声明，避免把消息退化成普通字符串列表。
    """
    # 任务编号
    task_id:str
    # 当前任务阶段
    task_stage: str
    # 用户查询原始输入
    user_query:str
    # 是否需要追问
    query_clarification:bool
    # 用户意图识别结果
    intent_analysis:dict[str,Any]
    # MeSH 术语标准化
    mesh_normalization: dict[str, Any]
    # 数据库无关检索计划
    search_plan:dict[str,Any]
    # Agent 根据研究问题选择的检索数据源及理由
    search_source_decision: dict[str, Any]
    # 允许本次任务调用的数据源；为空时由图使用已注册的全部后端
    search_sources: list[str]
    # 每个 Send 分支返回的统一检索结果，fan-in 节点消费该字段
    search_branch_results: Annotated[list[dict[str, Any]], add]
    # fan-in 去重后的规范文献摘要；正文仍从 PostgreSQL 读取
    search_results: list[dict[str, Any]]
    # 检索源状态、数量、去重冲突等监测指标
    search_metrics: dict[str, Any]
    # 按研究类型形成的质量评价分组
    article_groups: dict[str, list[str]]
    quality_groups: list[dict[str, Any]]
    # 每个质量评价 Send 分支的结果，质量评价 fan-in 节点消费该字段
    quality_evaluation_results: Annotated[list[dict[str, Any]], add]
    quality_evaluation_failures: list[dict[str, Any]]
    # 评价结果；保留旧字段名称以兼容已有调用方
    evaluated_evidence: list[dict[str, Any]]
    # 核查与程序重算后的派生结果；原始 reducer 结果不删除
    verification_results: list[dict[str, Any]]
    verified_assessments: list[dict[str, Any]]
    excluded_candidates: list[dict[str, Any]]
    retained_candidates: list[dict[str, Any]]
    # 集合级覆盖评价和确定性排序结果
    portfolio_evaluation: Optional[dict[str, Any]]
    ranked_candidates: list[dict[str, Any]]
    # Summarizer 的内部结构化结果；用户只看到 messages 中渲染后的中文回复
    final_summary: Optional[dict[str, Any]]
    # 搜索重试次数
    search_retry_count:int = 0 # type: ignore
    # 评估重试次数
    eval_retry_count:int = 0 # type: ignore
    # 摘要重试次数
    summary_retry_count:int = 0 # type: ignore
