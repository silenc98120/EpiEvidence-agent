"""研究类型专家负责专业路由，单篇执行委托给 BatchExecutor。"""
from typing import TypedDict
from langgraph.config import get_stream_writer
from backend.skills.quality_evaluator.execution import BatchExecutor
from backend.skills.quality_evaluator.schema import StudyType

class SpecializedAgentInput(TypedDict):
    task_id: str
    batch_id: str
    study_type: str

def create_specialized_expert_node(study_type: StudyType, executor: BatchExecutor):
    async def expert_node(state: SpecializedAgentInput) -> dict:
        if state["study_type"] != study_type.value:
            raise ValueError("文章任务与专家研究类型不一致")
        row = await executor.execute(state["task_id"], state["batch_id"], get_stream_writer())
        return {"quality_batch_results": [{"batch_id": row["batch_id"], "status": row["status"]}]}
    return expert_node
