from uuid import uuid4

from dotenv import load_dotenv
from langfuse import Langfuse

load_dotenv()

langfuse = Langfuse()
task_id = f"langfuse-smoke-{uuid4().hex[:8]}"

with langfuse.start_as_current_observation(
    name="langfuse_connection_check",
    as_type="span",
    metadata={"task_id": task_id},
) as span:
    span.update(output={"success": True})

langfuse.flush()
print(f"已发送测试 Trace，task_id={task_id}")