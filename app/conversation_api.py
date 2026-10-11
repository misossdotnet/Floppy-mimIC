"""Transport HTTP JSON de coordination, séparé des commandes du coordinateur."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.agent_api import authorized_agent, bearer_token, db_session, human, human_project
from app.conversation_models import CoordinationChannel
from app.conversation_service import (MessageMissing, acknowledge, channel_view, history,
                                      message_view, poll_messages, send_message)
from app.identity_service import audit
from app.memory_api import MemoryRoute, SourceRef, private_response

router = APIRouter(prefix="/api/projects/{project_id}/agent-messages", tags=["agent-messages"],
                  route_class=MemoryRoute, dependencies=[Depends(private_response)])
human_router = APIRouter(prefix="/api/projects/{project_id}/agent-conversations", tags=["agent-conversations"],
                        route_class=MemoryRoute, dependencies=[Depends(private_response)])


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["source_document", "context_item_version", "deliverable_version", "technical_incident"]
    id: UUID


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID


class MessageInput(SessionInput):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_key: UUID
    task_id: UUID
    summary: str = Field(min_length=1, max_length=1000)
    content: str = Field(default="", max_length=4000)
    channel_kind: Literal["direct", "broadcast", "thread"] | None = None
    channel_id: UUID | None = None
    recipient_profile_id: UUID | None = None
    attachment: Attachment | None = None
    reply_to_id: UUID | None = None
    kind: Literal["message", "handoff"] = "message"
    target_task_id: UUID | None = None
    artifact_refs: list[SourceRef] = Field(default_factory=list, max_length=10)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    expires_at: datetime | None = None


class PollInput(SessionInput):
    limit: int = Field(default=50, ge=1, le=100, strict=True)


async def agent(db, project_id, session_id, authorization, action):
    capability = {"send": "message.send", "read": "message.read", "ack": "message.ack"}[action]
    permission = "agent.read" if action == "read" else "agent.message.send"
    return await authorized_agent(db, project_id=project_id, session_id=str(session_id),
                                  authorization=authorization, capability=capability, permission=permission)


async def reject(db, session, action, exc):
    project_id, session_id = session.project_id, session.id
    await db.rollback()
    audit(db, action, "denied", project_id=project_id, target_type="agent_session", target_id=session_id)
    await db.commit()
    raise HTTPException(status_code=404 if isinstance(exc, MessageMissing) else 409, detail=str(exc))


@router.post("")
async def send(project_id: str, payload: MessageInput, authorization: str | None = Header(default=None),
               db=Depends(db_session)):
    session = await agent(db, project_id, payload.session_id, authorization, "send")
    try:
        message, created = await send_message(db, session, payload.model_dump(mode="json", exclude={"session_id"}),
                                             sensitive_token=bearer_token(authorization))
        result = await message_view(db, message)
    except ValueError as exc:
        await reject(db, session, "agent.message.send", exc)
    await db.commit()
    return {"message": result, "created": created}


@router.get("")
async def list_messages(project_id: str, session_id: UUID, channel_id: UUID | None = None,
                        limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000),
                        authorization: str | None = Header(default=None), db=Depends(db_session)):
    session = await agent(db, project_id, session_id, authorization, "read")
    result = await history(db, project_id, session=session, channel_id=str(channel_id) if channel_id else None,
                           limit=limit, offset=offset)
    await db.commit()
    return result


@router.post("/poll")
async def poll(project_id: str, payload: PollInput, authorization: str | None = Header(default=None),
               db=Depends(db_session)):
    session = await agent(db, project_id, payload.session_id, authorization, "read")
    try:
        result = await poll_messages(db, session, payload.limit)
    except ValueError as exc:
        await reject(db, session, "agent.message.deliver", exc)
    await db.commit()
    return result


@router.post("/{message_id}/ack")
async def ack(project_id: str, message_id: UUID, payload: SessionInput,
              authorization: str | None = Header(default=None), db=Depends(db_session)):
    session = await agent(db, project_id, payload.session_id, authorization, "ack")
    try:
        message, status = await acknowledge(db, session, str(message_id))
        result = await message_view(db, message)
    except ValueError as exc:
        await reject(db, session, "agent.message.ack", exc)
    await db.commit()
    if status == "expired":
        raise HTTPException(status_code=409, detail="Message expiré ; aucun accusé enregistré.")
    return {"message": result}


@human_router.get("")
async def conversations(project_id: str, limit: int = Query(default=50, ge=1, le=100),
                         offset: int = Query(default=0, ge=0, le=10000), db=Depends(db_session), actor=Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    rows = (await db.scalars(select(CoordinationChannel).where(CoordinationChannel.project_id == project_id)
        .order_by(CoordinationChannel.created_at, CoordinationChannel.id).offset(offset).limit(limit + 1))).all()
    return {"channels": [channel_view(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None}


@human_router.get("/{channel_id}")
async def conversation(project_id: str, channel_id: UUID, limit: int = Query(default=50, ge=1, le=100),
                        offset: int = Query(default=0, ge=0, le=10000), db=Depends(db_session), actor=Depends(human)):
    await human_project(db, actor[0], project_id, "agent.read")
    row = await db.scalar(select(CoordinationChannel).where(CoordinationChannel.id == str(channel_id),
        CoordinationChannel.project_id == project_id))
    if not row:
        raise HTTPException(status_code=404)
    return {"channel": channel_view(row), **await history(db, project_id, channel_id=row.id, limit=limit, offset=offset)}
