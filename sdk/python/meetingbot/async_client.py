"""
Asynchronous MeetingBot API client.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from .exceptions import (
    AuthError,
    MeetingBotError,
    NotFoundError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .models import (
    ApiKey,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    BalanceResponse,
    BotListResponse,
    BotResponse,
    BotStats,
    CheckoutResponse,
    ExportJsonResponse,
    LoginResponse,
    NotificationPrefs,
    PlanInfo,
    WebhookDeliveryListResponse,
    WebhookListResponse,
    WebhookResponse,
)

_SDK_VERSION_HEADER = "python/1.0.0"


def _raise_for_status(response: httpx.Response) -> None:
    """Parse error responses and raise a typed exception."""
    if response.is_success:
        return

    detail: str | None = None
    try:
        body = response.json()
        detail = body.get("detail") or body.get("message") or str(body)
    except Exception:
        detail = response.text or None

    message = f"HTTP {response.status_code}"
    if detail:
        message = f"{message}: {detail}"

    status = response.status_code
    if status in (401, 403):
        raise AuthError(message, status_code=status, detail=detail)
    if status == 404:
        raise NotFoundError(message, status_code=status, detail=detail)
    if status == 422:
        raise ValidationError(message, status_code=status, detail=detail)
    if status == 429:
        raise RateLimitError(message, status_code=status, detail=detail)
    if status >= 500:
        raise ServerError(message, status_code=status, detail=detail)
    raise MeetingBotError(message, status_code=status, detail=detail)


class AsyncMeetingBotClient:
    """
    Asynchronous client for the MeetingBot API.

    Usage::

        async with AsyncMeetingBotClient(api_key="sk_live_...") as client:
            bot = await client.create_bot(
                meeting_url="https://zoom.us/j/123",
                bot_name="My Bot",
                webhook_url="https://myapp.com/webhook",
            )
            print(bot.id)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.yourserver.com",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-SDK-Version": _SDK_VERSION_HEADER,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        """Close the underlying async HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncMeetingBotClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        response = await self._client.get(path, params=params)
        _raise_for_status(response)
        return response

    async def _post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        headers = extra_headers or {}
        if data is not None:
            response = await self._client.post(path, data=data, headers=headers)
        else:
            response = await self._client.post(path, json=json, headers=headers)
        _raise_for_status(response)
        return response

    async def _patch(self, path: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
        response = await self._client.patch(path, json=json)
        _raise_for_status(response)
        return response

    async def _put(self, path: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
        response = await self._client.put(path, json=json)
        _raise_for_status(response)
        return response

    async def _delete(self, path: str) -> httpx.Response:
        response = await self._client.delete(path)
        _raise_for_status(response)
        return response

    # ------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------

    async def create_bot(
        self,
        meeting_url: str,
        bot_name: str = "MeetingBot",
        bot_avatar_url: Optional[str] = None,
        webhook_url: Optional[str] = None,
        join_at: Optional[str] = None,
        analysis_mode: Optional[str] = None,
        template: Optional[str] = None,
        prompt_override: Optional[str] = None,
        vocabulary: Optional[List[str]] = None,
        respond_on_mention: Optional[bool] = None,
        mention_response_mode: Optional[str] = None,
        tts_provider: Optional[str] = None,
        start_muted: Optional[bool] = None,
        live_transcription: Optional[bool] = None,
        sub_user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        record_video: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> BotResponse:
        """
        Create a new meeting bot.

        :param meeting_url: The URL of the meeting to join.
        :param bot_name: Display name for the bot (default: "MeetingBot").
        :param bot_avatar_url: Optional URL for the bot's avatar image.
        :param webhook_url: URL to receive webhook events for this bot.
        :param join_at: ISO 8601 datetime string for scheduled join time.
        :param analysis_mode: "full" or "transcript_only".
        :param template: Analysis template to use.
        :param prompt_override: Custom prompt to override the default.
        :param vocabulary: List of custom vocabulary words.
        :param respond_on_mention: Whether the bot should respond when mentioned.
        :param mention_response_mode: How to respond on mention.
        :param tts_provider: Text-to-speech provider to use.
        :param start_muted: Whether the bot should join muted.
        :param live_transcription: Enable live transcription.
        :param sub_user_id: Sub-user identifier for multi-tenant usage.
        :param metadata: Arbitrary key-value metadata.
        :param record_video: Whether to record video (default: False).
        :param idempotency_key: Optional idempotency key to prevent duplicate bots.
        :returns: BotResponse
        """
        body: Dict[str, Any] = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "record_video": record_video,
        }
        if bot_avatar_url is not None:
            body["bot_avatar_url"] = bot_avatar_url
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if join_at is not None:
            body["join_at"] = join_at
        if analysis_mode is not None:
            body["analysis_mode"] = analysis_mode
        if template is not None:
            body["template"] = template
        if prompt_override is not None:
            body["prompt_override"] = prompt_override
        if vocabulary is not None:
            body["vocabulary"] = vocabulary
        if respond_on_mention is not None:
            body["respond_on_mention"] = respond_on_mention
        if mention_response_mode is not None:
            body["mention_response_mode"] = mention_response_mode
        if tts_provider is not None:
            body["tts_provider"] = tts_provider
        if start_muted is not None:
            body["start_muted"] = start_muted
        if live_transcription is not None:
            body["live_transcription"] = live_transcription
        if sub_user_id is not None:
            body["sub_user_id"] = sub_user_id
        if metadata is not None:
            body["metadata"] = metadata
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key

        extra_headers: Dict[str, str] = {}
        if idempotency_key is not None:
            extra_headers["Idempotency-Key"] = idempotency_key

        response = await self._post("/api/v1/bot", json=body, extra_headers=extra_headers)
        return BotResponse.model_validate(response.json())

    async def list_bots(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        status: Optional[str] = None,
        sub_user_id: Optional[str] = None,
    ) -> BotListResponse:
        """
        List bots with optional filtering and pagination.

        :param limit: Maximum number of results to return.
        :param offset: Number of results to skip.
        :param status: Filter by bot status.
        :param sub_user_id: Filter by sub-user ID.
        :returns: BotListResponse
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if status is not None:
            params["status"] = status
        if sub_user_id is not None:
            params["sub_user_id"] = sub_user_id

        response = await self._get("/api/v1/bot", params=params)
        return BotListResponse.model_validate(response.json())

    async def get_bot(self, bot_id: str) -> BotResponse:
        """
        Retrieve a bot by ID.

        :param bot_id: The bot's unique identifier.
        :returns: BotResponse
        """
        response = await self._get(f"/api/v1/bot/{bot_id}")
        return BotResponse.model_validate(response.json())

    async def cancel_bot(self, bot_id: str) -> Dict[str, Any]:
        """
        Cancel (delete) a bot.

        :param bot_id: The bot's unique identifier.
        :returns: Raw JSON response dict.
        """
        response = await self._delete(f"/api/v1/bot/{bot_id}")
        return response.json()

    async def download_recording(self, bot_id: str) -> bytes:
        """
        Download the audio recording for a bot session.

        :param bot_id: The bot's unique identifier.
        :returns: Raw audio bytes.
        """
        response = await self._get(f"/api/v1/bot/{bot_id}/recording")
        return response.content

    async def download_video(self, bot_id: str) -> bytes:
        """
        Download the video recording for a bot session.

        :param bot_id: The bot's unique identifier.
        :returns: Raw video bytes.
        """
        response = await self._get(f"/api/v1/bot/{bot_id}/video")
        return response.content

    async def get_bot_stats(self) -> BotStats:
        """
        Get aggregate bot counts.

        :returns: BotStats
        """
        response = await self._get("/api/v1/bot/stats")
        return BotStats.model_validate(response.json())

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    async def create_webhook(
        self,
        url: str,
        events: List[str],
        secret: Optional[str] = None,
    ) -> WebhookResponse:
        """
        Register a new webhook endpoint.

        :param url: The URL to deliver events to.
        :param events: List of event types to subscribe to.
        :param secret: Optional signing secret.
        :returns: WebhookResponse
        """
        body: Dict[str, Any] = {"url": url, "events": events}
        if secret is not None:
            body["secret"] = secret
        response = await self._post("/api/v1/webhook", json=body)
        return WebhookResponse.model_validate(response.json())

    async def list_webhooks(self) -> WebhookListResponse:
        """
        List all registered webhooks.

        :returns: WebhookListResponse
        """
        response = await self._get("/api/v1/webhook")
        return WebhookListResponse.model_validate(response.json())

    async def get_webhook(self, webhook_id: str) -> WebhookResponse:
        """
        Retrieve a webhook by ID.

        :param webhook_id: The webhook's unique identifier.
        :returns: WebhookResponse
        """
        response = await self._get(f"/api/v1/webhook/{webhook_id}")
        return WebhookResponse.model_validate(response.json())

    async def update_webhook(
        self,
        webhook_id: str,
        url: Optional[str] = None,
        events: Optional[List[str]] = None,
        secret: Optional[str] = None,
    ) -> WebhookResponse:
        """
        Update a webhook's configuration.

        :param webhook_id: The webhook's unique identifier.
        :param url: New URL (optional).
        :param events: New event list (optional).
        :param secret: New secret (optional).
        :returns: WebhookResponse
        """
        body: Dict[str, Any] = {}
        if url is not None:
            body["url"] = url
        if events is not None:
            body["events"] = events
        if secret is not None:
            body["secret"] = secret
        response = await self._patch(f"/api/v1/webhook/{webhook_id}", json=body)
        return WebhookResponse.model_validate(response.json())

    async def delete_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """
        Delete a webhook.

        :param webhook_id: The webhook's unique identifier.
        :returns: Raw JSON response dict.
        """
        response = await self._delete(f"/api/v1/webhook/{webhook_id}")
        return response.json()

    async def list_webhook_deliveries(
        self,
        webhook_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> WebhookDeliveryListResponse:
        """
        List delivery logs for a webhook.

        :param webhook_id: The webhook's unique identifier.
        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :returns: WebhookDeliveryListResponse
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        response = await self._get(f"/api/v1/webhook/{webhook_id}/deliveries", params=params)
        return WebhookDeliveryListResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def register(
        self,
        email: str,
        password: str,
        key_name: str,
        account_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Register a new account.

        :param email: Account email address.
        :param password: Account password.
        :param key_name: Name for the initial API key.
        :param account_type: Optional account type.
        :returns: Raw JSON response dict.
        """
        body: Dict[str, Any] = {"email": email, "password": password, "key_name": key_name}
        if account_type is not None:
            body["account_type"] = account_type
        response = await self._post("/api/v1/auth/register", json=body)
        return response.json()

    async def login(self, username: str, password: str) -> LoginResponse:
        """
        Log in and obtain a JWT token.

        :param username: Account email/username.
        :param password: Account password.
        :returns: LoginResponse containing the JWT.
        """
        response = await self._post(
            "/api/v1/auth/login",
            data={"username": username, "password": password},
        )
        return LoginResponse.model_validate(response.json())

    async def list_api_keys(self) -> ApiKeyListResponse:
        """
        List all API keys for the current account.

        :returns: ApiKeyListResponse
        """
        response = await self._get("/api/v1/auth/keys")
        return ApiKeyListResponse.model_validate(response.json())

    async def create_api_key(self, name: str) -> ApiKeyCreateResponse:
        """
        Create a new API key.

        :param name: Human-readable name for the key.
        :returns: ApiKeyCreateResponse (contains full key value once).
        """
        response = await self._post("/api/v1/auth/keys", json={"name": name})
        return ApiKeyCreateResponse.model_validate(response.json())

    async def revoke_api_key(self, key_id: str) -> Dict[str, Any]:
        """
        Revoke an API key.

        :param key_id: The key's unique identifier.
        :returns: Raw JSON response dict.
        """
        response = await self._delete(f"/api/v1/auth/keys/{key_id}")
        return response.json()

    async def get_plan(self) -> PlanInfo:
        """
        Get current account plan information.

        :returns: PlanInfo
        """
        response = await self._get("/api/v1/auth/plan")
        return PlanInfo.model_validate(response.json())

    async def get_notification_prefs(self) -> NotificationPrefs:
        """
        Get notification preferences.

        :returns: NotificationPrefs
        """
        response = await self._get("/api/v1/auth/notify")
        return NotificationPrefs.model_validate(response.json())

    async def update_notification_prefs(
        self,
        email_on_completion: Optional[bool] = None,
        email_on_failure: Optional[bool] = None,
        webhook_on_completion: Optional[bool] = None,
    ) -> NotificationPrefs:
        """
        Update notification preferences.

        :returns: NotificationPrefs
        """
        body: Dict[str, Any] = {}
        if email_on_completion is not None:
            body["email_on_completion"] = email_on_completion
        if email_on_failure is not None:
            body["email_on_failure"] = email_on_failure
        if webhook_on_completion is not None:
            body["webhook_on_completion"] = webhook_on_completion
        response = await self._put("/api/v1/auth/notify", json=body)
        return NotificationPrefs.model_validate(response.json())

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------

    async def get_balance(self) -> BalanceResponse:
        """
        Get account balance and transaction history.

        :returns: BalanceResponse
        """
        response = await self._get("/api/v1/billing/balance")
        return BalanceResponse.model_validate(response.json())

    async def create_checkout(
        self,
        amount_usd: float,
        success_url: str,
        cancel_url: str,
    ) -> CheckoutResponse:
        """
        Create a Stripe checkout session to top up balance.

        :param amount_usd: Amount in US dollars to charge.
        :param success_url: Redirect URL on successful payment.
        :param cancel_url: Redirect URL if payment is cancelled.
        :returns: CheckoutResponse with the checkout URL.
        """
        body = {
            "amount_usd": amount_usd,
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        response = await self._post("/api/v1/billing/stripe/checkout", json=body)
        return CheckoutResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    async def export_pdf(self, bot_id: str) -> bytes:
        """
        Export bot session as a PDF document.

        :param bot_id: The bot's unique identifier.
        :returns: PDF bytes.
        """
        response = await self._get(f"/api/v1/bot/{bot_id}/export/pdf")
        return response.content

    async def export_json(self, bot_id: str) -> ExportJsonResponse:
        """
        Export bot session as structured JSON.

        :param bot_id: The bot's unique identifier.
        :returns: ExportJsonResponse
        """
        response = await self._get(f"/api/v1/bot/{bot_id}/export/json")
        return ExportJsonResponse.model_validate(response.json())

    async def export_srt(self, bot_id: str) -> bytes:
        """
        Export bot session as an SRT subtitle file.

        :param bot_id: The bot's unique identifier.
        :returns: SRT file bytes.
        """
        response = await self._get(f"/api/v1/bot/{bot_id}/export/srt")
        return response.content
