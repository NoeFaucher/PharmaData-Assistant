"""
Router pour les conversations et le streaming SSE.

Flux SSE (POST /{conv_id}/messages) :
  1. Charger ExcelDataManager depuis la DB (ou cache)
  2. Streamer l'agent LangGraph via astream()
  3. Convertir chaque chunk en events SSE (token, tool_call, tool_result, chart_gen)
  4. Persister user_message + assistant_message en DB après le stream
  5. Si interrupt → mettre à jour pending_interrupt + interrupt_info en conversation
"""

import datetime
import json
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent_manager import evict_dm, get_current_excel_bytes, get_dm, get_or_create_dm
from api.database import AsyncSessionLocal
from api.deps import get_api_graph, get_current_user, get_db
from api.models import Conversation, ExcelFile, Message, User
from api.schemas import (
    ApproveRequest,
    ApproveResponse,
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    MessageCreate,
    MessageOut,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_config(thread_id: str, dm) -> dict:
    return {
        "configurable": {"thread_id": thread_id, "data_manager": dm},
        "recursion_limit": 20,
    }


def _extract_last_content(result: dict) -> str:
    messages = result.get("messages", [])
    if not messages:
        return "(Pas de réponse)"
    last = messages[-1]
    return last.content if hasattr(last, "content") else str(last)


async def _stream_agent(request: Request, conv_id: str, thread_id: str, dm, content: str):
    """
    Générateur async produisant des événements SSE.
    Persiste les messages et met à jour la conversation en fin de stream.
    """
    graph = get_api_graph(request)
    config = _make_config(thread_id, dm)

    # Sauvegarder le message utilisateur
    async with AsyncSessionLocal() as db:
        db.add(
            Message(
                conversation_id=uuid.UUID(conv_id),
                role="user",
                content=content,
            )
        )
        await db.commit()

    accumulated = ""
    tool_steps: list[dict] = []
    final_type = "done"
    interrupt_val = None

    try:
        async for chunk in graph.astream(
            {"messages": [HumanMessage(content=content)]},
            config=config,
            stream_mode=["updates", "messages"],
        ):
            mode, data = chunk

            # ── Tokens LLM ──────────────────────────────────────────────────
            if mode == "messages":
                msg_chunk, meta = data
                token_content = getattr(msg_chunk, "content", "")
                if token_content and meta.get("langgraph_node") == "agent":
                    accumulated += token_content
                    event = {"type": "token", "token": token_content}
                    yield f"data: {json.dumps(event)}\n\n"

            # ── Mises à jour de nœuds ───────────────────────────────────────
            elif mode == "updates":
                if "__interrupt__" in data:
                    interrupts = data["__interrupt__"]
                    intr = interrupts[0] if isinstance(interrupts, (list, tuple)) else interrupts
                    interrupt_val = getattr(intr, "value", str(intr))
                    final_type = "interrupt"
                    event = {"type": "interrupt", "value": interrupt_val}
                    yield f"data: {json.dumps(event)}\n\n"
                    break

                for node_name, node_data in data.items():
                    if not isinstance(node_data, dict):
                        continue

                    all_messages = node_data.get("messages", [])

                    if node_name == "tools":
                        # tool_node retourne state["messages"] complet (toute l'histoire).
                        # Seul le dernier message (AIMessage courant avec tool_calls) est nouveau.
                        if all_messages:
                            msg = all_messages[-1]
                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    event = {
                                        "type": "tool_call",
                                        "name": tc["name"],
                                        "args": tc.get("args", {}),
                                    }
                                    tool_steps.append(
                                        {"type": "call", "name": tc["name"], "args": tc.get("args", {})}
                                    )
                                    yield f"data: {json.dumps(event)}\n\n"
                    else:
                        # read_node / write_node (ToolNode standard) retournent uniquement
                        # les nouveaux ToolMessages — pas de déduplication nécessaire.
                        for msg in all_messages:
                            if hasattr(msg, "tool_call_id"):
                                snippet = str(msg.content)[:600]
                                event = {"type": "tool_result", "content": snippet}
                                tool_steps.append({"type": "result", "content": snippet})
                                yield f"data: {json.dumps(event)}\n\n"

                        if node_name == "reads" and "charts" in node_data:
                            charts = node_data.get("charts", [])
                            if charts:
                                event = {"type": "chart_gen", "chart": charts[-1]}
                                tool_steps.append({"type": "chart", "image": charts[-1]})
                                yield f"data: {json.dumps(event)}\n\n"

    except Exception as exc:
        final_type = "error"
        event = {"type": "error", "error": str(exc)}
        yield f"data: {json.dumps(event)}\n\n"

    # ── Persistance post-stream ──────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        if accumulated.strip() or final_type == "interrupt":
            role = "assistant" if final_type != "interrupt" else "assistant"
            db.add(
                Message(
                    conversation_id=uuid.UUID(conv_id),
                    role=role,
                    content=accumulated or "",
                    tool_steps=tool_steps if tool_steps else None,
                )
            )

        if final_type == "interrupt":
            db.add(
                Message(
                    conversation_id=uuid.UUID(conv_id),
                    role="interrupt",
                    content=json.dumps(interrupt_val),
                )
            )
            await db.execute(
                update(Conversation)
                .where(Conversation.id == uuid.UUID(conv_id))
                .values(
                    pending_interrupt=True,
                    interrupt_info=interrupt_val,
                    updated_at=datetime.datetime.utcnow(),
                )
            )
        else:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == uuid.UUID(conv_id))
                .values(updated_at=datetime.datetime.utcnow())
            )

        await db.commit()

    yield f"data: {json.dumps({'type': final_type})}\n\n"


# ─── CRUD conversations ───────────────────────────────────────────────────────

@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    excel_file = await db.get(ExcelFile, body.file_id)
    if not excel_file or excel_file.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Fichier introuvable")

    conv = Conversation(
        user_id=current_user.id,
        file_id=body.file_id,
        thread_id=str(uuid.uuid4()),
        title=body.title or excel_file.filename,
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
    )
    return result.scalars().all()


@router.get("/{conv_id}", response_model=ConversationDetail)
async def get_conversation(
    conv_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    msgs_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv_id)
        .order_by(Message.created_at)
    )
    messages = msgs_result.scalars().all()

    detail = ConversationDetail.model_validate(conv)
    detail.messages = [MessageOut.model_validate(m) for m in messages]
    return detail


@router.delete("/{conv_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conv_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    evict_dm(str(conv_id))
    await db.delete(conv)
    await db.commit()


# ─── Stream message ──────────────────────────────────────────────────────────

@router.post("/{conv_id}/messages")
async def send_message(
    conv_id: UUID,
    body: MessageCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    if conv.pending_interrupt:
        raise HTTPException(
            status_code=400,
            detail="Une action est en attente d'approbation. Utilisez POST /conversations/{id}/approve.",
        )

    if not conv.file_id:
        raise HTTPException(status_code=400, detail="Aucun fichier associé à cette conversation")

    excel_file = await db.get(ExcelFile, conv.file_id)
    if not excel_file:
        raise HTTPException(status_code=404, detail="Fichier Excel introuvable en base")

    dm, _ = get_or_create_dm(str(conv_id), bytes(excel_file.file_data))

    return StreamingResponse(
        _stream_agent(request, str(conv_id), conv.thread_id, dm, body.content),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Approve / Reject interrupt ───────────────────────────────────────────────

@router.post("/{conv_id}/approve", response_model=ApproveResponse)
async def approve_action(
    conv_id: UUID,
    body: ApproveRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision doit être 'approve' ou 'reject'")

    conv = await db.get(Conversation, conv_id)
    if not conv or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    if not conv.pending_interrupt:
        raise HTTPException(status_code=400, detail="Aucune action en attente d'approbation")

    # Récupérer ou recréer le DataManager
    entry = get_dm(str(conv_id))
    if entry is None:
        if not conv.file_id:
            raise HTTPException(status_code=400, detail="Aucun fichier associé")
        excel_file = await db.get(ExcelFile, conv.file_id)
        if not excel_file:
            raise HTTPException(status_code=404, detail="Fichier Excel introuvable")
        dm, _ = get_or_create_dm(str(conv_id), bytes(excel_file.file_data))
    else:
        dm, _ = entry

    graph = get_api_graph(request)
    config = _make_config(conv.thread_id, dm)

    result = await graph.ainvoke(Command(resume=body.decision), config=config)

    # Si approuvé, synchroniser le fichier Excel modifié en DB
    if body.decision == "approve" and conv.file_id:
        updated_bytes = get_current_excel_bytes(str(conv_id))
        if updated_bytes:
            excel_file = await db.get(ExcelFile, conv.file_id)
            if excel_file:
                excel_file.file_data = updated_bytes

    # Réponse de l'agent après la reprise
    agent_response = _extract_last_content(result)
    prefix = "✅ Modification appliquée.\n\n" if body.decision == "approve" else "🚫 Modification annulée.\n\n"
    full_content = prefix + agent_response

    # Supprimer le dernier message "interrupt" de l'historique
    last_interrupt = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conv_id,
            Message.role == "interrupt",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    last_interrupt_msg = last_interrupt.scalar_one_or_none()
    if last_interrupt_msg:
        await db.delete(last_interrupt_msg)

    # Sauvegarder la réponse finale
    db.add(
        Message(
            conversation_id=conv_id,
            role="assistant",
            content=full_content,
        )
    )

    # Réinitialiser l'état d'interruption
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conv_id)
        .values(
            pending_interrupt=False,
            interrupt_info=None,
            updated_at=datetime.datetime.utcnow(),
        )
    )
    await db.commit()

    return ApproveResponse(message=full_content)


