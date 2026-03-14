"""Speaker profiles API — cross-meeting participant identity and stats."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.speaker_profile import SpeakerProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/speakers", tags=["Speakers"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class SpeakerProfileResponse(BaseModel):
    id: str
    canonical_name: str
    aliases: list[str] = []
    email: str | None = None
    avatar_initials: str | None = None
    notes: str | None = None
    meeting_count: int = 0
    total_talk_time_s: float = 0.0
    avg_talk_pct: float = 0.0
    total_questions: int = 0
    total_filler_words: int = 0
    last_seen_at: str | None = None
    created_at: str

    model_config = {"from_attributes": True}


class SpeakerProfileUpdate(BaseModel):
    canonical_name: str | None = None
    aliases: list[str] | None = None
    email: str | None = None
    notes: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=list[SpeakerProfileResponse])
async def list_speakers(
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
):
    """List all known speaker profiles, sorted by meeting count."""
    query = select(SpeakerProfile).order_by(SpeakerProfile.meeting_count.desc())
    if search:
        pattern = f"%{search}%"
        query = query.where(SpeakerProfile.canonical_name.ilike(pattern))
    profiles = (await db.execute(query.limit(limit).offset(offset))).scalars().all()
    return [_to_response(p) for p in profiles]


@router.get("/{speaker_id}", response_model=SpeakerProfileResponse)
async def get_speaker(
    speaker_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get a single speaker profile by ID."""
    profile = await _get_or_404(db, speaker_id)
    return _to_response(profile)


@router.patch("/{speaker_id}", response_model=SpeakerProfileResponse)
async def update_speaker(
    speaker_id: str,
    payload: SpeakerProfileUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update speaker profile metadata (name, aliases, email, notes)."""
    profile = await _get_or_404(db, speaker_id)

    if payload.canonical_name is not None:
        # Check uniqueness
        existing = (
            await db.execute(
                select(SpeakerProfile).where(
                    SpeakerProfile.canonical_name == payload.canonical_name,
                    SpeakerProfile.id != speaker_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"A profile with name '{payload.canonical_name}' already exists",
            )
        profile.canonical_name = payload.canonical_name

    if payload.aliases is not None:
        profile.aliases = payload.aliases
    if payload.email is not None:
        profile.email = payload.email
    if payload.notes is not None:
        profile.notes = payload.notes

    await db.commit()
    await db.refresh(profile)
    return _to_response(profile)


@router.delete("/{speaker_id}", status_code=204)
async def delete_speaker(
    speaker_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete a speaker profile (does not affect historical meeting data)."""
    profile = await _get_or_404(db, speaker_id)
    await db.delete(profile)
    await db.commit()


@router.get("/{speaker_id}/meetings")
async def get_speaker_meetings(
    speaker_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent meetings where this speaker participated."""
    from app.models.bot import Bot

    profile = await _get_or_404(db, speaker_id)

    # Find bots whose participants list contains this speaker's name or any alias
    all_names = [profile.canonical_name] + list(profile.aliases or [])

    bots_result = await db.execute(
        select(Bot)
        .where(Bot.status == "done")
        .order_by(Bot.created_at.desc())
        .limit(limit * 5)  # over-fetch then filter in Python
    )
    bots = bots_result.scalars().all()

    name_set = {n.lower() for n in all_names}
    matching = [
        b for b in bots
        if any(p.lower() in name_set for p in (b.participants or []))
    ][:limit]

    return {
        "speaker_id": speaker_id,
        "canonical_name": profile.canonical_name,
        "meetings": [
            {
                "id": b.id,
                "meeting_url": b.meeting_url,
                "meeting_platform": b.meeting_platform,
                "started_at": b.started_at.isoformat() if b.started_at else None,
                "ended_at": b.ended_at.isoformat() if b.ended_at else None,
                "participant_count": len(b.participants or []),
                "speaker_stats": _find_speaker_stat(b.speaker_stats, all_names),
            }
            for b in matching
        ],
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, speaker_id: str) -> SpeakerProfile:
    result = await db.execute(
        select(SpeakerProfile).where(SpeakerProfile.id == speaker_id)
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Speaker {speaker_id!r} not found")
    return profile


def _to_response(p: SpeakerProfile) -> SpeakerProfileResponse:
    return SpeakerProfileResponse(
        id=p.id,
        canonical_name=p.canonical_name,
        aliases=list(p.aliases or []),
        email=p.email,
        avatar_initials=p.avatar_initials,
        notes=p.notes,
        meeting_count=p.meeting_count,
        total_talk_time_s=p.total_talk_time_s,
        avg_talk_pct=round(p.avg_talk_pct, 1),
        total_questions=p.total_questions,
        total_filler_words=p.total_filler_words,
        last_seen_at=p.last_seen_at.isoformat() if p.last_seen_at else None,
        created_at=p.created_at.isoformat(),
    )


def _find_speaker_stat(speaker_stats: list | None, names: list[str]) -> dict | None:
    if not speaker_stats:
        return None
    name_set = {n.lower() for n in names}
    for stat in speaker_stats:
        if (stat.get("name") or "").lower() in name_set:
            return stat
    return None


# ── Public helper used by bot_service ─────────────────────────────────────────

async def upsert_speaker_profiles(db: AsyncSession, bot) -> None:
    """Update or create SpeakerProfile rows from a completed bot's speaker_stats."""
    from datetime import datetime, timezone

    stats: list[dict] = bot.speaker_stats or []
    if not stats:
        return

    ended_at = bot.ended_at or datetime.now(timezone.utc)

    for stat in stats:
        name = (stat.get("name") or "").strip()
        if not name:
            continue

        # Try to find existing profile by canonical name or alias
        profile = (
            await db.execute(
                select(SpeakerProfile).where(
                    SpeakerProfile.canonical_name == name
                )
            )
        ).scalar_one_or_none()

        if profile is None:
            # Check aliases
            all_profiles = (await db.execute(select(SpeakerProfile))).scalars().all()
            for p in all_profiles:
                if name.lower() in [a.lower() for a in (p.aliases or [])]:
                    profile = p
                    break

        talk_time = stat.get("talk_time_s", 0.0)
        talk_pct = stat.get("talk_pct", 0.0)
        questions = stat.get("questions", 0)
        fillers = stat.get("filler_words", 0)

        if profile is None:
            # Create new profile
            initials = "".join(w[0].upper() for w in name.split()[:2])
            profile = SpeakerProfile(
                canonical_name=name,
                avatar_initials=initials or name[:2].upper(),
                aliases=[],
                meeting_count=1,
                total_talk_time_s=talk_time,
                avg_talk_pct=talk_pct,
                total_questions=questions,
                total_filler_words=fillers,
                last_seen_at=ended_at,
            )
            db.add(profile)
        else:
            # Update running averages and totals
            n = profile.meeting_count
            profile.avg_talk_pct = (profile.avg_talk_pct * n + talk_pct) / (n + 1)
            profile.meeting_count += 1
            profile.total_talk_time_s += talk_time
            profile.total_questions += questions
            profile.total_filler_words += fillers
            profile.last_seen_at = ended_at

    try:
        await db.commit()
        logger.info("Updated speaker profiles for bot %s", bot.id)
    except Exception as exc:
        await db.rollback()
        logger.warning("Failed to upsert speaker profiles for bot %s: %s", bot.id, exc)
