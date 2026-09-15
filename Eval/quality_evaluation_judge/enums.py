from enum import StrEnum


class BatchEligibility(StrEnum):
    ELIGIBLE = "eligible"
    INVALID = "invalid"
    FAILED = "failed"
    PENDING = "pending"


class JudgeType(StrEnum):
    ROUTING = "routing"
    CRITERION = "criterion"
    RANKING = "ranking"


class JudgeVerdict(StrEnum):
    """3 个 Judge 统一使用的四态裁定。"""

    AGREE = "agree"
    PARTIALLY_AGREE = "partially_agree"
    DISAGREE = "disagree"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
