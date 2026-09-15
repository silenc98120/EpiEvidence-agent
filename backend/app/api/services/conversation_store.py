from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4
from backend.app.api.schemas import ConversationListItem, ConversationMessageResponse, ConversationResponse, TaskStatus

class ConversationStore:
    def __init__(self): self._items: dict[UUID, ConversationResponse] = {}; self._lock = asyncio.Lock()
    async def create(self, title: str) -> ConversationResponse:
        now=datetime.now(timezone.utc); cid=uuid4()
        item=ConversationResponse(conversation_id=cid,title=title,preview=None,status="active",updated_at=now,messages=[])
        async with self._lock: self._items[cid]=item
        return item
    async def get(self,cid: UUID):
        async with self._lock: return self._items.get(cid)
    async def list(self):
        async with self._lock:
            return sorted([ConversationListItem(**x.model_dump(exclude={"messages"})) for x in self._items.values()], key=lambda x:x.updated_at, reverse=True)
    async def append(self,cid: UUID, role: str, content: str, task_id: UUID|None=None, status: str="completed"):
        async with self._lock:
            c=self._items.get(cid)
            if not c: return None
            msg=ConversationMessageResponse(message_id=uuid4(),conversation_id=cid,task_id=task_id,role=role,content=content,status=status,sequence_no=len(c.messages),created_at=datetime.now(timezone.utc))
            c.messages.append(msg); c.preview=content[:160]; c.updated_at=msg.created_at
            return msg
    async def update_status(self,cid: UUID,status: str, task_status: TaskStatus|None=None):
        async with self._lock:
            c=self._items.get(cid)
            if c: c.status=status; c.latest_task_status=task_status; c.updated_at=datetime.now(timezone.utc)
