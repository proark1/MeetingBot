"""Keyword alert service.

Scans transcript entries for configured keywords and fires webhook events
when matches are found.

Two sources of keyword rules:
1. Per-bot `keyword_alerts` list (set at bot creation time)
2. Account-level `KeywordAlert` records in the database

When a keyword is triggered, a `bot.keyword_alert` event is dispatched to:
- All global webhooks (via webhook_service.dispatch_event)
- The per-alert `webhook_url` if specified
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _matches_keyword(text: str, keyword: str) -> bool:
    """Case-insensitive whole-word-ish keyword match."""
    return keyword.lower().strip() in text.lower()


async def _load_account_keyword_alerts(account_id: str) -> list[dict]:
    """Load active KeywordAlert rules for an account from the database."""
    try:
        import json
        from app.db import AsyncSessionLocal
        from app.models.account import KeywordAlert
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(KeywordAlert).where(
                    KeywordAlert.account_id == account_id,
                    KeywordAlert.is_active == True,  # noqa: E712
                )
            )
            rows = result.scalars().all()

        rules = []
        for row in rows:
            try:
                keywords = json.loads(row.keywords or "[]")
            except Exception:
                keywords = []
            rules.append({
                "id": row.id,
                "name": row.name,
                "keywords": keywords,
                "webhook_url": row.webhook_url,
            })
        return rules
    except Exception as exc:
        logger.error("Failed to load keyword alerts for account %s: %s", account_id, exc)
        return []


async def _update_trigger_count(alert_id: str) -> None:
    """Increment trigger_count and update last_triggered_at for a KeywordAlert row."""
    try:
        from datetime import datetime, timezone
        from app.db import AsyncSessionLocal
        from app.models.account import KeywordAlert
        from sqlalchemy import select, update

        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(KeywordAlert)
                .where(KeywordAlert.id == alert_id)
                .values(
                    trigger_count=KeywordAlert.trigger_count + 1,
                    last_triggered_at=now,
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("Failed to update trigger count for alert %s: %s", alert_id, exc)


async def scan_live_entry(
    bot,
    entry: dict,
    fired_keys: set,
) -> None:
    """Check a single live transcript entry against keyword alert rules and fire immediately.

    Unlike ``scan_and_fire_alerts`` (post-call), this runs during the meeting in real-time.
    Fires ``bot.live_keyword_alert`` events (distinct from post-call ``bot.keyword_alert``).

    ``fired_keys`` is a mutable set owned by the caller (the live entry closure) used to
    deduplicate: a (keyword, minute-bucket) pair fires at most once per minute.
    """
    from app.services import webhook_service
    from app.services.intelligence_service import get_sentiment

    text = entry.get("text", "") or ""
    if not text:
        return

    speaker = entry.get("speaker", "Unknown")
    timestamp = entry.get("timestamp", 0)
    bot_id = bot.id
    account_id = bot.account_id

    # Collect rules
    rules: list[dict] = []
    for item in (getattr(bot, "keyword_alerts", None) or []):
        keyword = item.get("keyword", "").strip()
        if keyword:
            rules.append({"id": None, "name": f"bot-rule:{keyword}", "keywords": [keyword], "webhook_url": item.get("webhook_url")})

    if account_id:
        try:
            account_rules = await _load_account_keyword_alerts(account_id)
            rules.extend(account_rules)
        except Exception:
            pass

    if not rules:
        return

    for rule in rules:
        for keyword in rule.get("keywords", []):
            if not keyword:
                continue
            if not _matches_keyword(text, keyword):
                continue

            # Deduplicate: fire at most once per (keyword, 60-second bucket)
            dedup_key = f"{keyword.lower()}_{int(timestamp // 60)}"
            if dedup_key in fired_keys:
                continue
            fired_keys.add(dedup_key)

            # Enrich with sentiment
            try:
                sentiment = await get_sentiment(text)
            except Exception:
                sentiment = "neutral"

            payload = {
                "bot_id": bot_id,
                "account_id": account_id,
                "keyword": keyword,
                "alert_name": rule.get("name", ""),
                "speaker": speaker,
                "text": text,
                "timestamp": timestamp,
                "sentiment": sentiment,
                "live": True,
            }
            try:
                await webhook_service.dispatch_event(
                    "bot.live_keyword_alert",
                    payload,
                    extra_webhook_url=rule.get("webhook_url"),
                    account_id=account_id,
                )
                logger.info(
                    "Bot %s: live keyword alert — '%s' by %s @ %.1fs (sentiment: %s)",
                    bot_id, keyword, speaker, timestamp, sentiment,
                )
            except Exception as exc:
                logger.warning("Bot %s: failed to dispatch live keyword alert: %s", bot_id, exc)

            if rule.get("id"):
                try:
                    await _update_trigger_count(rule["id"])
                except Exception:
                    pass

            break  # only fire once per (entry, rule)


async def scan_and_fire_alerts(
    bot_id: str,
    account_id: Optional[str],
    transcript: list[dict],
    per_bot_alerts: Optional[list[dict]] = None,
) -> list[dict]:
    """Scan transcript entries against keyword rules and fire webhook events.

    Returns a list of fired alert records:
    [{"keyword": str, "alert_name": str, "speaker": str, "text": str, "timestamp": float}]
    """
    from app.services import webhook_service

    if not transcript:
        return []

    # Collect all rules: per-bot alerts + account-level alerts
    rules: list[dict] = []

    # Per-bot rules (from BotSession.keyword_alerts)
    for item in (per_bot_alerts or []):
        keyword = item.get("keyword", "").strip()
        if keyword:
            rules.append({
                "id": None,
                "name": f"bot-rule:{keyword}",
                "keywords": [keyword],
                "webhook_url": item.get("webhook_url"),
            })

    # Account-level rules
    if account_id:
        account_rules = await _load_account_keyword_alerts(account_id)
        rules.extend(account_rules)

    if not rules:
        return []

    fired: list[dict] = []

    for entry in transcript:
        text = entry.get("text", "") or ""
        speaker = entry.get("speaker", "Unknown")
        timestamp = entry.get("timestamp", 0)

        for rule in rules:
            for keyword in rule.get("keywords", []):
                if not keyword:
                    continue
                if _matches_keyword(text, keyword):
                    alert_record = {
                        "keyword": keyword,
                        "alert_name": rule.get("name", ""),
                        "alert_id": rule.get("id"),
                        "speaker": speaker,
                        "text": text,
                        "timestamp": timestamp,
                        "bot_id": bot_id,
                    }
                    fired.append(alert_record)

                    # Fire the webhook event
                    payload = {
                        "bot_id": bot_id,
                        "account_id": account_id,
                        "keyword": keyword,
                        "alert_name": rule.get("name", ""),
                        "speaker": speaker,
                        "text": text,
                        "timestamp": timestamp,
                    }
                    try:
                        await webhook_service.dispatch_event(
                            "bot.keyword_alert",
                            payload,
                            extra_webhook_url=rule.get("webhook_url"),
                            account_id=account_id,
                        )
                        logger.info(
                            "Bot %s: keyword alert fired — '%s' by %s @ %.1fs",
                            bot_id, keyword, speaker, timestamp,
                        )
                    except Exception as exc:
                        logger.warning("Failed to dispatch keyword alert for bot %s: %s", bot_id, exc)

                    # Update DB trigger count for named account rules
                    if rule.get("id"):
                        try:
                            await _update_trigger_count(rule["id"])
                        except Exception:
                            pass

                    # Only fire once per (entry, rule) — don't fire for every keyword match in the same rule
                    break

    return fired
