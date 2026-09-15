from __future__ import annotations
from datetime import datetime, timezone
from uuid import UUID, uuid4
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from backend.app.db.models import ConversationORM, ConversationMessageORM
from backend.app.api.schemas import ConversationResponse, ConversationListItem, ConversationMessageResponse

class ConversationRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]): self.factory=factory
    async def create(self, title: str):
        async with self.factory() as s:
            async with s.begin():
                c=ConversationORM(conversation_id=uuid4(),title=title.strip(),status="active"); s.add(c)
            await s.refresh(c); return ConversationResponse(conversation_id=c.conversation_id,title=c.title,preview=c.preview,status=c.status,updated_at=c.updated_at,messages=[])
    async def list(self):
        async with self.factory() as s:
            rows=list((await s.execute(select(ConversationORM).order_by(ConversationORM.updated_at.desc()))).scalars())
            return [ConversationListItem(conversation_id=c.conversation_id,title=c.title,preview=c.preview,status=c.status,updated_at=c.updated_at) for c in rows]
    async def get(self,cid: UUID):
        async with self.factory() as s:
            c=await s.get(ConversationORM,cid)
            if c: await s.refresh(c, ["messages"])
            return ConversationResponse(conversation_id=c.conversation_id,title=c.title,preview=c.preview,status=c.status,updated_at=c.updated_at,messages=[ConversationMessageResponse.model_validate(m, from_attributes=True) for m in c.messages]) if c else None
    async def append(self,cid,role,content,task_id=None,status="completed"):
        async with self.factory() as s:
            async with s.begin():
                c=await s.get(ConversationORM,cid)
                if not c: return None
                seq=(await s.scalar(select(func.coalesce(func.max(ConversationMessageORM.sequence_no),-1)+1).where(ConversationMessageORM.conversation_id==cid)))
                m=ConversationMessageORM(message_id=uuid4(),conversation_id=cid,task_id=task_id,role=role,content=content,status=status,sequence_no=seq); s.add(m)
                c.preview=content[:160]; c.updated_at=datetime.now(timezone.utc)
            return m
    async def update_status(self,cid,status,task_status=None):
        async with self.factory() as s:
                async with s.begin(): await s.execute(update(ConversationORM).where(ConversationORM.conversation_id==cid).values(status=status,updated_at=datetime.now(timezone.utc)))

    async def rename(self, cid: UUID, title: str):
        async with self.factory() as s:
            async with s.begin():
                c=await s.get(ConversationORM,cid)
                if not c: return None
                c.title=title.strip(); c.updated_at=datetime.now(timezone.utc)
            return True

    async def delete(self, cid: UUID) -> bool:
        async with self.factory() as s:
            async with s.begin():
                result=await s.execute(delete(ConversationORM).where(ConversationORM.conversation_id==cid))
                return result.rowcount == 1
