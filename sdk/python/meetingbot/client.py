"""
Synchronous JustHereToListen.io API client.
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
    AccountInfo,
    ActionItemListResponse,
    ActionItemResponse,
    AnalysisResponse,
    AnalyticsResponse,
    ApiKey,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiUsageResponse,
    AskResponse,
    AuditLogResponse,
    BalanceResponse,
    BotListResponse,
    BotResponse,
    BotStats,
    CalendarFeedListResponse,
    CalendarFeedResponse,
    CheckoutResponse,
    DefaultPromptResponse,
    ExportJsonResponse,
    FollowupEmailResponse,
    HighlightsResponse,
    IntegrationListResponse,
    IntegrationResponse,
    KeywordAlertListResponse,
    KeywordAlertResponse,
    LoginResponse,
    McpCallResponse,
    McpSchemaResponse,
    MyAnalyticsResponse,
    NotificationPrefs,
    PlanInfo,
    RecurringAnalyticsResponse,
    RetentionPolicyResponse,
    SearchResponse,
    ShareResponse,
    TemplateListResponse,
    TranscriptResponse,
    WebhookDeliveryListResponse,
    WebhookEventsResponse,
    WebhookListResponse,
    WebhookResponse,
    WorkspaceListResponse,
    WorkspaceMemberListResponse,
    WorkspaceResponse,
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


class MeetingBotClient:
    """
    Synchronous client for the MeetingBot API.

    Usage::

        client = MeetingBotClient(api_key="sk_live_...")
        bot = client.create_bot(
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
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-SDK-Version": _SDK_VERSION_HEADER,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> "MeetingBotClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> httpx.Response:
        response = self._client.get(path, params=params)
        _raise_for_status(response)
        return response

    def _post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        headers = extra_headers or {}
        if data is not None:
            response = self._client.post(path, data=data, headers=headers)
        else:
            response = self._client.post(path, json=json, headers=headers)
        _raise_for_status(response)
        return response

    def _patch(self, path: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
        response = self._client.patch(path, json=json)
        _raise_for_status(response)
        return response

    def _put(self, path: str, json: Optional[Dict[str, Any]] = None) -> httpx.Response:
        response = self._client.put(path, json=json)
        _raise_for_status(response)
        return response

    def _delete(self, path: str) -> httpx.Response:
        response = self._client.delete(path)
        _raise_for_status(response)
        return response

    def _delete_no_content(self, path: str) -> None:
        """Send DELETE and discard the body (for 204 No Content endpoints)."""
        response = self._client.delete(path)
        _raise_for_status(response)

    # ------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------

    def create_bot(
        self,
        meeting_url: str,
        bot_name: str = "JustHereToListen.io",
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
        :param bot_name: Display name for the bot (default: "JustHereToListen.io").
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

        response = self._post("/api/v1/bot", json=body, extra_headers=extra_headers)
        return BotResponse.model_validate(response.json())

    def list_bots(
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

        response = self._get("/api/v1/bot", params=params)
        return BotListResponse.model_validate(response.json())

    def get_bot(self, bot_id: str) -> BotResponse:
        """
        Retrieve a bot by ID.

        :param bot_id: The bot's unique identifier.
        :returns: BotResponse
        """
        response = self._get(f"/api/v1/bot/{bot_id}")
        return BotResponse.model_validate(response.json())

    def cancel_bot(self, bot_id: str) -> None:
        """
        Cancel (delete) a bot.

        :param bot_id: The bot's unique identifier.
        """
        self._delete_no_content(f"/api/v1/bot/{bot_id}")

    def download_recording(self, bot_id: str) -> bytes:
        """
        Download the audio recording for a bot session.

        :param bot_id: The bot's unique identifier.
        :returns: Raw audio bytes.
        """
        response = self._get(f"/api/v1/bot/{bot_id}/recording")
        return response.content

    def download_video(self, bot_id: str) -> bytes:
        """
        Download the video recording for a bot session.

        :param bot_id: The bot's unique identifier.
        :returns: Raw video bytes.
        """
        response = self._get(f"/api/v1/bot/{bot_id}/video")
        return response.content

    def get_bot_stats(self) -> BotStats:
        """
        Get aggregate bot counts.

        :returns: BotStats
        """
        response = self._get("/api/v1/bot/stats")
        return BotStats.model_validate(response.json())

    # ------------------------------------------------------------------
    # Webhooks
    # ------------------------------------------------------------------

    def create_webhook(
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
        response = self._post("/api/v1/webhook", json=body)
        return WebhookResponse.model_validate(response.json())

    def list_webhooks(self) -> WebhookListResponse:
        """
        List all registered webhooks.

        :returns: WebhookListResponse
        """
        response = self._get("/api/v1/webhook")
        return WebhookListResponse.model_validate(response.json())

    def get_webhook(self, webhook_id: str) -> WebhookResponse:
        """
        Retrieve a webhook by ID.

        :param webhook_id: The webhook's unique identifier.
        :returns: WebhookResponse
        """
        response = self._get(f"/api/v1/webhook/{webhook_id}")
        return WebhookResponse.model_validate(response.json())

    def update_webhook(
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
        response = self._patch(f"/api/v1/webhook/{webhook_id}", json=body)
        return WebhookResponse.model_validate(response.json())

    def delete_webhook(self, webhook_id: str) -> None:
        """
        Delete a webhook.

        :param webhook_id: The webhook's unique identifier.
        """
        self._delete_no_content(f"/api/v1/webhook/{webhook_id}")

    def list_webhook_deliveries(
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
        response = self._get(f"/api/v1/webhook/{webhook_id}/deliveries", params=params)
        return WebhookDeliveryListResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def register(
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
        response = self._post("/api/v1/auth/register", json=body)
        return response.json()

    def login(self, username: str, password: str) -> LoginResponse:
        """
        Log in and obtain a JWT token.

        :param username: Account email/username.
        :param password: Account password.
        :returns: LoginResponse containing the JWT.
        """
        response = self._post(
            "/api/v1/auth/login",
            data={"username": username, "password": password},
        )
        return LoginResponse.model_validate(response.json())

    def list_api_keys(self) -> ApiKeyListResponse:
        """
        List all API keys for the current account.

        :returns: ApiKeyListResponse
        """
        response = self._get("/api/v1/auth/keys")
        return ApiKeyListResponse.model_validate(response.json())

    def create_api_key(self, name: str) -> ApiKeyCreateResponse:
        """
        Create a new API key.

        :param name: Human-readable name for the key.
        :returns: ApiKeyCreateResponse (contains full key value once).
        """
        response = self._post("/api/v1/auth/keys", json={"name": name})
        return ApiKeyCreateResponse.model_validate(response.json())

    def revoke_api_key(self, key_id: str) -> None:
        """
        Revoke an API key.

        :param key_id: The key's unique identifier.
        """
        self._delete_no_content(f"/api/v1/auth/keys/{key_id}")

    def get_plan(self) -> PlanInfo:
        """
        Get current account plan information.

        :returns: PlanInfo
        """
        response = self._get("/api/v1/auth/plan")
        return PlanInfo.model_validate(response.json())

    def get_notification_prefs(self) -> NotificationPrefs:
        """
        Get notification preferences.

        :returns: NotificationPrefs
        """
        response = self._get("/api/v1/auth/notify")
        return NotificationPrefs.model_validate(response.json())

    def update_notification_prefs(
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
        response = self._put("/api/v1/auth/notify", json=body)
        return NotificationPrefs.model_validate(response.json())

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------

    def get_balance(self) -> BalanceResponse:
        """
        Get account balance and transaction history.

        :returns: BalanceResponse
        """
        response = self._get("/api/v1/billing/balance")
        return BalanceResponse.model_validate(response.json())

    def create_checkout(
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
        response = self._post("/api/v1/billing/stripe/checkout", json=body)
        return CheckoutResponse.model_validate(response.json())

    def subscribe(
        self,
        plan: str,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
    ) -> "SubscribeResponse":
        """
        Create a Stripe subscription checkout for a plan upgrade.

        :param plan: Target plan: 'starter', 'pro', or 'business'.
        :param success_url: Redirect URL after successful subscription.
        :param cancel_url: Redirect URL if user cancels.
        :returns: SubscribeResponse with checkout URL.
        """
        body: Dict[str, Any] = {"plan": plan}
        if success_url is not None:
            body["success_url"] = success_url
        if cancel_url is not None:
            body["cancel_url"] = cancel_url
        response = self._post("/api/v1/billing/subscribe", json=body)
        from .models import SubscribeResponse
        return SubscribeResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Exports
    # ------------------------------------------------------------------

    def export_pdf(self, bot_id: str) -> bytes:
        """
        Export bot session as a PDF document.

        :param bot_id: The bot's unique identifier.
        :returns: PDF bytes.
        """
        response = self._get(f"/api/v1/bot/{bot_id}/export/pdf")
        return response.content

    def export_json(self, bot_id: str) -> ExportJsonResponse:
        """
        Export bot session as structured JSON.

        :param bot_id: The bot's unique identifier.
        :returns: ExportJsonResponse
        """
        response = self._get(f"/api/v1/bot/{bot_id}/export/json")
        return ExportJsonResponse.model_validate(response.json())

    def export_srt(self, bot_id: str) -> bytes:
        """
        Export bot session as an SRT subtitle file.

        :param bot_id: The bot's unique identifier.
        :returns: SRT file bytes.
        """
        response = self._get(f"/api/v1/bot/{bot_id}/export/srt")
        return response.content

    def export_markdown(self, bot_id: str) -> bytes:
        """
        Export bot session as Markdown.

        :param bot_id: The bot's unique identifier.
        :returns: Markdown bytes.
        """
        response = self._get(f"/api/v1/bot/{bot_id}/export/markdown")
        return response.content

    # ------------------------------------------------------------------
    # Bots — Advanced
    # ------------------------------------------------------------------

    def get_transcript(self, bot_id: str) -> TranscriptResponse:
        """
        Get the raw transcript for a bot session.

        :param bot_id: The bot's unique identifier.
        :returns: TranscriptResponse
        """
        response = self._get(f"/api/v1/bot/{bot_id}/transcript")
        return TranscriptResponse.model_validate(response.json())

    def analyze_bot(
        self,
        bot_id: str,
        template: Optional[str] = None,
        prompt_override: Optional[str] = None,
    ) -> AnalysisResponse:
        """
        Re-run AI analysis on a bot's transcript.

        :param bot_id: The bot's unique identifier.
        :param template: Analysis template to use.
        :param prompt_override: Custom analysis prompt.
        :returns: AnalysisResponse
        """
        body: Dict[str, Any] = {}
        if template is not None:
            body["template"] = template
        if prompt_override is not None:
            body["prompt_override"] = prompt_override
        response = self._post(f"/api/v1/bot/{bot_id}/analyze", json=body)
        return AnalysisResponse.model_validate(response.json())

    def get_highlights(self, bot_id: str) -> HighlightsResponse:
        """
        Get curated highlights from a meeting.

        :param bot_id: The bot's unique identifier.
        :returns: HighlightsResponse
        """
        response = self._get(f"/api/v1/bot/{bot_id}/highlight")
        return HighlightsResponse.model_validate(response.json())

    def ask_bot(self, bot_id: str, question: str) -> AskResponse:
        """
        Ask a freeform question about a completed bot's transcript.

        :param bot_id: The bot's unique identifier.
        :param question: The question to ask.
        :returns: AskResponse
        """
        response = self._post(f"/api/v1/bot/{bot_id}/ask", json={"question": question})
        return AskResponse.model_validate(response.json())

    def ask_live_bot(self, bot_id: str, question: str) -> AskResponse:
        """
        Ask a question about a live in-progress bot's transcript.

        :param bot_id: The bot's unique identifier.
        :param question: The question to ask.
        :returns: AskResponse
        """
        response = self._post(f"/api/v1/bot/{bot_id}/ask-live", json={"question": question})
        return AskResponse.model_validate(response.json())

    def generate_followup_email(
        self,
        bot_id: str,
        participants: Optional[List[str]] = None,
        tone: Optional[str] = None,
    ) -> FollowupEmailResponse:
        """
        Generate a follow-up email for a meeting.

        :param bot_id: The bot's unique identifier.
        :param participants: Optional list of participant names.
        :param tone: Optional tone (e.g. "formal", "casual").
        :returns: FollowupEmailResponse
        """
        body: Dict[str, Any] = {}
        if participants is not None:
            body["participants"] = participants
        if tone is not None:
            body["tone"] = tone
        response = self._post(f"/api/v1/bot/{bot_id}/followup-email", json=body)
        return FollowupEmailResponse.model_validate(response.json())

    def rename_speakers(self, bot_id: str, mapping: Dict[str, str]) -> Dict[str, Any]:
        """
        Rename speaker labels in a bot's transcript.

        :param bot_id: The bot's unique identifier.
        :param mapping: Dict mapping old speaker names to new names.
        :returns: Raw JSON response dict.
        """
        response = self._patch(f"/api/v1/bot/{bot_id}/speakers", json={"mapping": mapping})
        return response.json()

    def share_bot(self, bot_id: str) -> ShareResponse:
        """
        Generate a shareable link for a meeting.

        :param bot_id: The bot's unique identifier.
        :returns: ShareResponse
        """
        response = self._post(f"/api/v1/bot/{bot_id}/share", json={})
        return ShareResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Webhooks — Extended
    # ------------------------------------------------------------------

    def list_webhook_events(self) -> WebhookEventsResponse:
        """
        List all supported webhook event types.

        :returns: WebhookEventsResponse
        """
        response = self._get("/api/v1/webhook/events")
        return WebhookEventsResponse.model_validate(response.json())

    def test_webhook(self, webhook_id: str) -> Dict[str, Any]:
        """
        Send a test event to a webhook.

        :param webhook_id: The webhook's unique identifier.
        :returns: Raw JSON response dict.
        """
        response = self._post(f"/api/v1/webhook/{webhook_id}/test", json={})
        return response.json()

    def list_all_deliveries(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> WebhookDeliveryListResponse:
        """
        List all webhook deliveries across all webhooks.

        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :returns: WebhookDeliveryListResponse
        """
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        response = self._get("/api/v1/webhook/deliveries", params=params)
        return WebhookDeliveryListResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Auth — Extended
    # ------------------------------------------------------------------

    def get_me(self) -> AccountInfo:
        """
        Get current account information.

        :returns: AccountInfo
        """
        response = self._get("/api/v1/auth/me")
        return AccountInfo.model_validate(response.json())

    def list_test_keys(self) -> ApiKeyListResponse:
        """
        List all test (sandbox) API keys.

        :returns: ApiKeyListResponse
        """
        response = self._get("/api/v1/auth/test-keys")
        return ApiKeyListResponse.model_validate(response.json())

    def create_test_key(self, name: str) -> ApiKeyCreateResponse:
        """
        Create a new test (sandbox) API key.

        :param name: Human-readable name for the key.
        :returns: ApiKeyCreateResponse
        """
        response = self._post("/api/v1/auth/test-keys", json={"name": name})
        return ApiKeyCreateResponse.model_validate(response.json())

    def delete_account(self) -> Dict[str, Any]:
        """
        Delete the current account.

        :returns: Raw JSON response dict.
        """
        response = self._delete("/api/v1/auth/account")
        return response.json()

    def update_account_type(self, account_type: str) -> Dict[str, Any]:
        """
        Change the account type.

        :param account_type: New account type.
        :returns: Raw JSON response dict.
        """
        response = self._put("/api/v1/auth/account-type", json={"account_type": account_type})
        return response.json()

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------

    def list_templates(self) -> TemplateListResponse:
        """
        List all available analysis templates.

        :returns: TemplateListResponse
        """
        response = self._get("/api/v1/templates")
        return TemplateListResponse.model_validate(response.json())

    def get_default_prompt(self) -> DefaultPromptResponse:
        """
        Get the default analysis prompt.

        :returns: DefaultPromptResponse
        """
        response = self._get("/api/v1/templates/default-prompt")
        return DefaultPromptResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_analytics(self) -> AnalyticsResponse:
        """
        Get account analytics dashboard.

        :returns: AnalyticsResponse
        """
        response = self._get("/api/v1/analytics")
        return AnalyticsResponse.model_validate(response.json())

    def get_recurring_analytics(
        self, attendees: Optional[str] = None,
    ) -> RecurringAnalyticsResponse:
        """
        Get recurring meeting insights.

        :param attendees: Optional attendee filter.
        :returns: RecurringAnalyticsResponse
        """
        params: Dict[str, Any] = {}
        if attendees is not None:
            params["attendees"] = attendees
        response = self._get("/api/v1/analytics/recurring", params=params)
        return RecurringAnalyticsResponse.model_validate(response.json())

    def get_api_usage(self) -> ApiUsageResponse:
        """
        Get API usage statistics.

        :returns: ApiUsageResponse
        """
        response = self._get("/api/v1/analytics/api-usage")
        return ApiUsageResponse.model_validate(response.json())

    def get_my_analytics(self) -> MyAnalyticsResponse:
        """
        Get personal analytics.

        :returns: MyAnalyticsResponse
        """
        response = self._get("/api/v1/analytics/me")
        return MyAnalyticsResponse.model_validate(response.json())

    def get_usage(self) -> "UsageResponse":
        """
        Get monthly usage breakdown: bots used, limit, credits spent, daily usage.

        :returns: UsageResponse
        """
        response = self._get("/api/v1/analytics/usage")
        from .models import UsageResponse
        return UsageResponse.model_validate(response.json())

    def get_trends(self, days: int = 30) -> "TrendsResponse":
        """
        Get longitudinal analytics: meetings/day, sentiment, topics, cost trends.

        :param days: Number of days to look back (7–365, default 30).
        :returns: TrendsResponse
        """
        response = self._get("/api/v1/analytics/trends", params={"days": days})
        from .models import TrendsResponse
        return TrendsResponse.model_validate(response.json())

    def search_meetings(
        self,
        q: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> SearchResponse:
        """
        Search meetings and transcripts.

        :param q: Search query string.
        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :returns: SearchResponse
        """
        params: Dict[str, Any] = {"q": q}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        response = self._get("/api/v1/search", params=params)
        return SearchResponse.model_validate(response.json())

    def get_audit_log(
        self,
        action: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> AuditLogResponse:
        """
        Get account audit log.

        :param action: Optional action filter.
        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :returns: AuditLogResponse
        """
        params: Dict[str, Any] = {}
        if action is not None:
            params["action"] = action
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        response = self._get("/api/v1/audit-log", params=params)
        return AuditLogResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Action Items
    # ------------------------------------------------------------------

    def list_action_items(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ActionItemListResponse:
        """
        List action items from meetings.

        :param status: Filter by status (e.g. "open", "done").
        :param assignee: Filter by assignee.
        :param limit: Maximum number of results.
        :param offset: Number of results to skip.
        :returns: ActionItemListResponse
        """
        params: Dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if assignee is not None:
            params["assignee"] = assignee
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        response = self._get("/api/v1/action-items", params=params)
        return ActionItemListResponse.model_validate(response.json())

    def update_action_item(
        self,
        item_id: str,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        due_date: Optional[str] = None,
    ) -> ActionItemResponse:
        """
        Update an action item.

        :param item_id: The action item's unique identifier.
        :param status: New status.
        :param assignee: New assignee.
        :param due_date: New due date (ISO 8601).
        :returns: ActionItemResponse
        """
        body: Dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if assignee is not None:
            body["assignee"] = assignee
        if due_date is not None:
            body["due_date"] = due_date
        response = self._patch(f"/api/v1/action-items/{item_id}", json=body)
        return ActionItemResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # Keyword Alerts
    # ------------------------------------------------------------------

    def list_keyword_alerts(self) -> KeywordAlertListResponse:
        """
        List all keyword alerts.

        :returns: KeywordAlertListResponse
        """
        response = self._get("/api/v1/keyword-alerts")
        return KeywordAlertListResponse.model_validate(response.json())

    def create_keyword_alert(
        self,
        keywords: List[str],
        webhook_url: Optional[str] = None,
        events: Optional[List[str]] = None,
    ) -> KeywordAlertResponse:
        """
        Create a new keyword alert.

        :param keywords: List of keywords to monitor.
        :param webhook_url: Optional webhook URL for alerts.
        :param events: Optional list of event types.
        :returns: KeywordAlertResponse
        """
        body: Dict[str, Any] = {"keywords": keywords}
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if events is not None:
            body["events"] = events
        response = self._post("/api/v1/keyword-alerts", json=body)
        return KeywordAlertResponse.model_validate(response.json())

    def get_keyword_alert(self, alert_id: str) -> KeywordAlertResponse:
        """
        Get a keyword alert by ID.

        :param alert_id: The alert's unique identifier.
        :returns: KeywordAlertResponse
        """
        response = self._get(f"/api/v1/keyword-alerts/{alert_id}")
        return KeywordAlertResponse.model_validate(response.json())

    def update_keyword_alert(
        self,
        alert_id: str,
        keywords: Optional[List[str]] = None,
        is_active: Optional[bool] = None,
    ) -> KeywordAlertResponse:
        """
        Update a keyword alert.

        :param alert_id: The alert's unique identifier.
        :param keywords: Updated keywords list.
        :param is_active: Enable or disable the alert.
        :returns: KeywordAlertResponse
        """
        body: Dict[str, Any] = {}
        if keywords is not None:
            body["keywords"] = keywords
        if is_active is not None:
            body["is_active"] = is_active
        response = self._patch(f"/api/v1/keyword-alerts/{alert_id}", json=body)
        return KeywordAlertResponse.model_validate(response.json())

    def delete_keyword_alert(self, alert_id: str) -> None:
        """
        Delete a keyword alert.

        :param alert_id: The alert's unique identifier.
        """
        self._delete_no_content(f"/api/v1/keyword-alerts/{alert_id}")

    # ------------------------------------------------------------------
    # Calendar Feeds
    # ------------------------------------------------------------------

    def list_calendar_feeds(self) -> CalendarFeedListResponse:
        """
        List all calendar feeds.

        :returns: CalendarFeedListResponse
        """
        response = self._get("/api/v1/calendar")
        return CalendarFeedListResponse.model_validate(response.json())

    def create_calendar_feed(
        self,
        url: str,
        name: Optional[str] = None,
        auto_record: Optional[bool] = None,
        bot_name: Optional[str] = None,
    ) -> CalendarFeedResponse:
        """
        Add a calendar feed.

        :param url: iCal feed URL.
        :param name: Human-readable name.
        :param auto_record: Whether to auto-record discovered meetings.
        :param bot_name: Bot name for auto-created bots.
        :returns: CalendarFeedResponse
        """
        body: Dict[str, Any] = {"url": url}
        if name is not None:
            body["name"] = name
        if auto_record is not None:
            body["auto_record"] = auto_record
        if bot_name is not None:
            body["bot_name"] = bot_name
        response = self._post("/api/v1/calendar", json=body)
        return CalendarFeedResponse.model_validate(response.json())

    def delete_calendar_feed(self, feed_id: str) -> None:
        """
        Delete a calendar feed.

        :param feed_id: The feed's unique identifier.
        """
        self._delete_no_content(f"/api/v1/calendar/{feed_id}")

    def sync_calendar_feed(self, feed_id: str) -> Dict[str, Any]:
        """
        Trigger a sync for a calendar feed.

        :param feed_id: The feed's unique identifier.
        :returns: Raw JSON response dict.
        """
        response = self._post(f"/api/v1/calendar/{feed_id}/sync", json={})
        return response.json()

    # ------------------------------------------------------------------
    # Integrations
    # ------------------------------------------------------------------

    def list_integrations(self) -> IntegrationListResponse:
        """
        List all integrations.

        :returns: IntegrationListResponse
        """
        response = self._get("/api/v1/integrations")
        return IntegrationListResponse.model_validate(response.json())

    def create_integration(
        self,
        type: str,
        config: Dict[str, Any],
    ) -> IntegrationResponse:
        """
        Create a new integration.

        :param type: Integration type (e.g. "slack", "notion").
        :param config: Integration configuration dict.
        :returns: IntegrationResponse
        """
        body: Dict[str, Any] = {"type": type, "config": config}
        response = self._post("/api/v1/integrations", json=body)
        return IntegrationResponse.model_validate(response.json())

    def update_integration(
        self,
        integration_id: str,
        config: Optional[Dict[str, Any]] = None,
        is_active: Optional[bool] = None,
    ) -> IntegrationResponse:
        """
        Update an integration.

        :param integration_id: The integration's unique identifier.
        :param config: Updated configuration.
        :param is_active: Enable or disable the integration.
        :returns: IntegrationResponse
        """
        body: Dict[str, Any] = {}
        if config is not None:
            body["config"] = config
        if is_active is not None:
            body["is_active"] = is_active
        response = self._patch(f"/api/v1/integrations/{integration_id}", json=body)
        return IntegrationResponse.model_validate(response.json())

    def delete_integration(self, integration_id: str) -> None:
        """
        Delete an integration.

        :param integration_id: The integration's unique identifier.
        """
        self._delete_no_content(f"/api/v1/integrations/{integration_id}")

    # ------------------------------------------------------------------
    # Workspaces
    # ------------------------------------------------------------------

    def list_workspaces(self) -> WorkspaceListResponse:
        """
        List workspaces the current account owns or is a member of.

        :returns: WorkspaceListResponse
        """
        response = self._get("/api/v1/workspaces")
        return WorkspaceListResponse.model_validate(response.json())

    def create_workspace(self, name: str) -> WorkspaceResponse:
        """
        Create a new workspace.

        :param name: Workspace name (max 100 chars).
        :returns: WorkspaceResponse
        """
        response = self._post("/api/v1/workspaces", json={"name": name})
        return WorkspaceResponse.model_validate(response.json())

    def get_workspace(self, workspace_id: str) -> WorkspaceResponse:
        """
        Get workspace details.

        :param workspace_id: The workspace's unique identifier.
        :returns: WorkspaceResponse
        """
        response = self._get(f"/api/v1/workspaces/{workspace_id}")
        return WorkspaceResponse.model_validate(response.json())

    def update_workspace(
        self,
        workspace_id: str,
        name: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> WorkspaceResponse:
        """
        Update a workspace.

        :param workspace_id: The workspace's unique identifier.
        :param name: New name.
        :param settings: New settings dict.
        :returns: WorkspaceResponse
        """
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if settings is not None:
            body["settings"] = settings
        response = self._patch(f"/api/v1/workspaces/{workspace_id}", json=body)
        return WorkspaceResponse.model_validate(response.json())

    def delete_workspace(self, workspace_id: str) -> None:
        """
        Delete a workspace (owner only).

        :param workspace_id: The workspace's unique identifier.
        """
        self._delete_no_content(f"/api/v1/workspaces/{workspace_id}")

    def list_workspace_members(self, workspace_id: str) -> WorkspaceMemberListResponse:
        """
        List members of a workspace.

        :param workspace_id: The workspace's unique identifier.
        :returns: WorkspaceMemberListResponse
        """
        response = self._get(f"/api/v1/workspaces/{workspace_id}/members")
        return WorkspaceMemberListResponse.model_validate(response.json())

    def add_workspace_member(
        self,
        workspace_id: str,
        account_id: str,
        role: str = "member",
    ) -> Dict[str, Any]:
        """
        Add a member to a workspace.

        :param workspace_id: The workspace's unique identifier.
        :param account_id: The account ID to add.
        :param role: Role to assign ("admin", "member", or "viewer").
        :returns: Raw JSON response dict.
        """
        body = {"account_id": account_id, "role": role}
        response = self._post(f"/api/v1/workspaces/{workspace_id}/members", json=body)
        return response.json()

    def remove_workspace_member(self, workspace_id: str, account_id: str) -> None:
        """
        Remove a member from a workspace.

        :param workspace_id: The workspace's unique identifier.
        :param account_id: The account ID to remove.
        """
        self._delete_no_content(f"/api/v1/workspaces/{workspace_id}/members/{account_id}")

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def get_retention_policy(self) -> RetentionPolicyResponse:
        """
        Get the current retention policy.

        :returns: RetentionPolicyResponse
        """
        response = self._get("/api/v1/retention")
        return RetentionPolicyResponse.model_validate(response.json())

    def update_retention_policy(
        self,
        retention_days: Optional[int] = None,
        anonymize_speakers: Optional[bool] = None,
    ) -> RetentionPolicyResponse:
        """
        Update the retention policy.

        :param retention_days: Number of days to retain data.
        :param anonymize_speakers: Whether to anonymize speaker names.
        :returns: RetentionPolicyResponse
        """
        body: Dict[str, Any] = {}
        if retention_days is not None:
            body["retention_days"] = retention_days
        if anonymize_speakers is not None:
            body["anonymize_speakers"] = anonymize_speakers
        response = self._put("/api/v1/retention", json=body)
        return RetentionPolicyResponse.model_validate(response.json())

    # ------------------------------------------------------------------
    # MCP
    # ------------------------------------------------------------------

    def get_mcp_schema(self) -> McpSchemaResponse:
        """
        Get the MCP server manifest and tool list.

        :returns: McpSchemaResponse
        """
        response = self._get("/api/v1/mcp/schema")
        return McpSchemaResponse.model_validate(response.json())

    def call_mcp_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> McpCallResponse:
        """
        Execute an MCP tool.

        :param tool_name: Name of the MCP tool to call.
        :param arguments: Optional arguments dict for the tool.
        :returns: McpCallResponse
        """
        body: Dict[str, Any] = {"tool": tool_name}
        if arguments is not None:
            body["arguments"] = arguments
        response = self._post("/api/v1/mcp/call", json=body)
        return McpCallResponse.model_validate(response.json())
