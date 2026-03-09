"""Transcript highlights (bookmarks + comments) for a bot."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.bot import Bot
from app.models.highlight import Highlight

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot", tags=["Highlights"])


class HighlightCreate(BaseModel):
    timestamp: float
    text_snippet: str
    speaker: str = ""
    comment: str | None = None


class HighlightResponse(BaseModel):
    id: str
    bot_id: str
    timestamp: float
    text_snippet: str
    speaker: str
    comment: str | None
    created_at: str

    model_config = {"from_attributes": True}


@router.post("/{bot_id}/highlight", response_model=HighlightResponse, status_code=201)
async def create_highlight(
    bot_id: str,
    payload: HighlightCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Bookmark a transcript moment with an optional comment."""
    result = await db.execute(select(Bot).where(Bot.id == bot_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")

    h = Highlight(
        bot_id=bot_id,
        timestamp=payload.timestamp,
        text_snippet=payload.text_snippet[:500],
        speaker=payload.speaker[:256],
        comment=payload.comment,
    )
    db.add(h)
    await db.commit()
    await db.refresh(h)
    return HighlightResponse(
        id=h.id, bot_id=h.bot_id, timestamp=h.timestamp,
        text_snippet=h.text_snippet, speaker=h.speaker, comment=h.comment,
        created_at=h.created_at.isoformat(),
    )


@router.get("/{bot_id}/highlight")
async def list_highlights(
    bot_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all highlights for a bot."""
    result = await db.execute(select(Bot).where(Bot.id == bot_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id!r} not found")

    hl_result = await db.execute(
        select(Highlight).where(Highlight.bot_id == bot_id).order_by(Highlight.timestamp)
    )
    highlights = hl_result.scalars().all()
    return [
        {
            "id": h.id, "bot_id": h.bot_id, "timestamp": h.timestamp,
            "text_snippet": h.text_snippet, "speaker": h.speaker,
            "comment": h.comment, "created_at": h.created_at.isoformat(),
        }
        for h in highlights
    ]


@router.delete("/highlight/{highlight_id}", status_code=204)
async def delete_highlight(
    highlight_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a highlight."""
    result = await db.execute(select(Highlight).where(Highlight.id == highlight_id))
    h = result.scalar_one_or_none()
    if h is None:
        raise HTTPException(status_code=404, detail="Highlight not found")
    await db.delete(h)
    await db.commit()
