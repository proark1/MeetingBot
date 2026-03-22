/**
 * MeetingBot API client implementation.
 *
 * Node.js 18+ includes a native `fetch`. For older Node versions install a
 * polyfill such as `node-fetch` and assign it to `globalThis.fetch` before
 * importing this module:
 *
 *   import fetch from "node-fetch";
 *   (globalThis as any).fetch = fetch;
 */

import {
  AuthError,
  MeetingBotError,
  NotFoundError,
  RateLimitError,
  ServerError,
  ValidationError,
} from "./errors.js";
import type {
  AccountInfo,
  ActionItemListResponse,
  ActionItemResponse,
  AddWorkspaceMemberParams,
  AnalysisResponse,
  AnalyticsResponse,
  AnalyzeBotParams,
  ApiKeyCreateResponse,
  ApiKeyListResponse,
  ApiUsageResponse,
  AskBotParams,
  AskResponse,
  AuditLogResponse,
  BalanceResponse,
  BotListResponse,
  BotResponse,
  BotStats,
  CalendarFeedListResponse,
  CalendarFeedResponse,
  CallMcpToolParams,
  CheckoutResponse,
  CreateBotParams,
  CreateCalendarFeedParams,
  CreateCheckoutParams,
  CreateIntegrationParams,
  CreateKeywordAlertParams,
  CreateWebhookParams,
  DefaultPromptResponse,
  ExportJsonResponse,
  FollowupEmailParams,
  FollowupEmailResponse,
  GetAuditLogParams,
  HighlightsResponse,
  IntegrationListResponse,
  IntegrationResponse,
  KeywordAlertListResponse,
  KeywordAlertResponse,
  ListActionItemsParams,
  ListAllDeliveriesParams,
  ListBotsParams,
  ListWebhookDeliveriesParams,
  LoginResponse,
  McpCallResponse,
  McpSchemaResponse,
  MeetingBotClientConfig,
  MyAnalyticsResponse,
  NotificationPrefs,
  PlanInfo,
  RecurringAnalyticsResponse,
  RegisterParams,
  RenameSpeakersParams,
  RetentionPolicyResponse,
  SearchMeetingsParams,
  SearchResponse,
  ShareResponse,
  TemplateListResponse,
  TranscriptResponse,
  UpdateAccountTypeParams,
  UpdateActionItemParams,
  UpdateIntegrationParams,
  UpdateKeywordAlertParams,
  UpdateNotificationPrefsParams,
  UpdateRetentionPolicyParams,
  UpdateWebhookParams,
  UpdateWorkspaceParams,
  WebhookDeliveryListResponse,
  WebhookEventsResponse,
  WebhookListResponse,
  WebhookResponse,
  WorkspaceListResponse,
  WorkspaceMemberListResponse,
  WorkspaceResponse,
} from "./types.js";

const SDK_VERSION = "js/1.0.0";
const DEFAULT_BASE_URL = "https://api.yourserver.com";

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function parseErrorDetail(response: Response): Promise<string | undefined> {
  try {
    const body = await response.clone().json();
    return (body as Record<string, unknown>)["detail"] as string |
      (body as Record<string, unknown>)["message"] as string |
      JSON.stringify(body);
  } catch {
    try {
      return await response.clone().text();
    } catch {
      return undefined;
    }
  }
}

async function throwForStatus(response: Response): Promise<void> {
  if (response.ok) return;

  const detail = await parseErrorDetail(response);
  const message = detail
    ? `HTTP ${response.status}: ${detail}`
    : `HTTP ${response.status}`;

  const { status } = response;
  if (status === 401 || status === 403) {
    throw new AuthError(message, status, detail);
  }
  if (status === 404) {
    throw new NotFoundError(message, status, detail);
  }
  if (status === 422) {
    throw new ValidationError(message, status, detail);
  }
  if (status === 429) {
    throw new RateLimitError(message, status, detail);
  }
  if (status >= 500) {
    throw new ServerError(message, status, detail);
  }
  throw new MeetingBotError(message, status, detail);
}

// ---------------------------------------------------------------------------
// MeetingBotClient
// ---------------------------------------------------------------------------

/**
 * JavaScript/TypeScript client for the MeetingBot API.
 *
 * @example
 * ```ts
 * const client = new MeetingBotClient({ apiKey: "sk_live_..." });
 *
 * const bot = await client.createBot({
 *   meeting_url: "https://zoom.us/j/123456789",
 *   bot_name: "My Recorder",
 * });
 * console.log(bot.id, bot.status);
 * ```
 */
export class MeetingBotClient {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;

  constructor(config: MeetingBotClientConfig) {
    if (!config.apiKey) {
      throw new Error("MeetingBotClient: apiKey is required");
    }
    this.apiKey = config.apiKey;
    this.baseUrl = (config.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeoutMs = config.timeoutMs ?? 30_000;
  }

  // -------------------------------------------------------------------------
  // Internal request helpers
  // -------------------------------------------------------------------------

  private buildHeaders(extra?: Record<string, string>): Record<string, string> {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      "X-SDK-Version": SDK_VERSION,
      "Content-Type": "application/json",
      Accept: "application/json",
      ...extra,
    };
  }

  private url(path: string, params?: Record<string, string | number | boolean | undefined>): string {
    const url = new URL(path, this.baseUrl + "/");
    // Re-apply the full path directly to avoid base URL manipulation
    const fullUrl = `${this.baseUrl}${path}`;
    if (!params) return fullUrl;
    const u = new URL(fullUrl);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        u.searchParams.set(key, String(value));
      }
    }
    return u.toString();
  }

  private async request<T>(
    method: string,
    path: string,
    options: {
      params?: Record<string, string | number | boolean | undefined>;
      body?: unknown;
      formData?: URLSearchParams;
      extraHeaders?: Record<string, string>;
      binary?: false;
    }
  ): Promise<T>;
  private async request(
    method: string,
    path: string,
    options: {
      params?: Record<string, string | number | boolean | undefined>;
      binary: true;
      extraHeaders?: Record<string, string>;
    }
  ): Promise<ArrayBuffer>;
  private async request<T>(
    method: string,
    path: string,
    options: {
      params?: Record<string, string | number | boolean | undefined>;
      body?: unknown;
      formData?: URLSearchParams;
      extraHeaders?: Record<string, string>;
      binary?: boolean;
    } = {}
  ): Promise<T | ArrayBuffer> {
    const { params, body, formData, extraHeaders, binary } = options;

    const fullUrl = this.url(path, params);
    const headers = this.buildHeaders(extraHeaders);

    let requestBody: BodyInit | undefined;
    if (formData) {
      requestBody = formData;
      // Remove Content-Type so the browser/node sets it with boundary
      delete headers["Content-Type"];
    } else if (body !== undefined) {
      requestBody = JSON.stringify(body);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);

    let response: Response;
    try {
      response = await fetch(fullUrl, {
        method,
        headers,
        body: requestBody,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }

    await throwForStatus(response);

    if (binary) {
      return response.arrayBuffer();
    }

    // Some DELETE endpoints return 204 No Content
    if (response.status === 204) {
      return {} as T;
    }

    return response.json() as Promise<T>;
  }

  // -------------------------------------------------------------------------
  // Bots
  // -------------------------------------------------------------------------

  /**
   * Create a new meeting bot.
   */
  async createBot(params: CreateBotParams): Promise<BotResponse> {
    const { idempotency_key, ...body } = params;
    const extraHeaders: Record<string, string> = {};
    if (idempotency_key) {
      extraHeaders["Idempotency-Key"] = idempotency_key;
      (body as Record<string, unknown>)["idempotency_key"] = idempotency_key;
    }
    return this.request<BotResponse>("POST", "/api/v1/bot", {
      body: { bot_name: "MeetingBot", record_video: false, ...body },
      extraHeaders,
    });
  }

  /**
   * List bots with optional filtering and pagination.
   */
  async listBots(params: ListBotsParams = {}): Promise<BotListResponse> {
    return this.request<BotListResponse>("GET", "/api/v1/bot", {
      params: params as Record<string, string | number | undefined>,
    });
  }

  /**
   * Retrieve a bot by ID.
   */
  async getBot(id: string): Promise<BotResponse> {
    return this.request<BotResponse>("GET", `/api/v1/bot/${id}`, {});
  }

  /**
   * Cancel (delete) a bot.
   */
  async cancelBot(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/bot/${id}`, {});
  }

  /**
   * Download the audio recording for a bot. Returns an ArrayBuffer.
   */
  async downloadRecording(id: string): Promise<ArrayBuffer> {
    return this.request("GET", `/api/v1/bot/${id}/recording`, { binary: true });
  }

  /**
   * Download the video recording for a bot. Returns an ArrayBuffer.
   */
  async downloadVideo(id: string): Promise<ArrayBuffer> {
    return this.request("GET", `/api/v1/bot/${id}/video`, { binary: true });
  }

  /**
   * Get aggregate bot counts.
   */
  async getBotStats(): Promise<BotStats> {
    return this.request<BotStats>("GET", "/api/v1/bot/stats", {});
  }

  // -------------------------------------------------------------------------
  // Webhooks
  // -------------------------------------------------------------------------

  /**
   * Register a new webhook endpoint.
   */
  async createWebhook(params: CreateWebhookParams): Promise<WebhookResponse> {
    return this.request<WebhookResponse>("POST", "/api/v1/webhook", { body: params });
  }

  /**
   * List all registered webhooks.
   */
  async listWebhooks(): Promise<WebhookListResponse> {
    return this.request<WebhookListResponse>("GET", "/api/v1/webhook", {});
  }

  /**
   * Retrieve a webhook by ID.
   */
  async getWebhook(id: string): Promise<WebhookResponse> {
    return this.request<WebhookResponse>("GET", `/api/v1/webhook/${id}`, {});
  }

  /**
   * Update a webhook's configuration.
   */
  async updateWebhook(id: string, params: UpdateWebhookParams): Promise<WebhookResponse> {
    return this.request<WebhookResponse>("PATCH", `/api/v1/webhook/${id}`, { body: params });
  }

  /**
   * Delete a webhook.
   */
  async deleteWebhook(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/webhook/${id}`, {});
  }

  /**
   * List delivery logs for a webhook.
   */
  async listWebhookDeliveries(
    id: string,
    params: ListWebhookDeliveriesParams = {}
  ): Promise<WebhookDeliveryListResponse> {
    return this.request<WebhookDeliveryListResponse>(
      "GET",
      `/api/v1/webhook/${id}/deliveries`,
      { params: params as Record<string, number | undefined> }
    );
  }

  // -------------------------------------------------------------------------
  // Auth
  // -------------------------------------------------------------------------

  /**
   * Register a new account.
   */
  async register(params: RegisterParams): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("POST", "/api/v1/auth/register", {
      body: params,
    });
  }

  /**
   * Log in with username/password (form data) and obtain a JWT token.
   */
  async login(username: string, password: string): Promise<LoginResponse> {
    const formData = new URLSearchParams({ username, password });
    return this.request<LoginResponse>("POST", "/api/v1/auth/login", {
      formData,
    });
  }

  /**
   * List all API keys for the current account.
   */
  async listApiKeys(): Promise<ApiKeyListResponse> {
    return this.request<ApiKeyListResponse>("GET", "/api/v1/auth/keys", {});
  }

  /**
   * Create a new API key.
   */
  async createApiKey(name: string): Promise<ApiKeyCreateResponse> {
    return this.request<ApiKeyCreateResponse>("POST", "/api/v1/auth/keys", {
      body: { name },
    });
  }

  /**
   * Revoke an API key.
   */
  async revokeApiKey(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/auth/keys/${id}`, {});
  }

  /**
   * Get current account plan information.
   */
  async getPlan(): Promise<PlanInfo> {
    return this.request<PlanInfo>("GET", "/api/v1/auth/plan", {});
  }

  /**
   * Get notification preferences.
   */
  async getNotificationPrefs(): Promise<NotificationPrefs> {
    return this.request<NotificationPrefs>("GET", "/api/v1/auth/notify", {});
  }

  /**
   * Update notification preferences.
   */
  async updateNotificationPrefs(
    params: UpdateNotificationPrefsParams
  ): Promise<NotificationPrefs> {
    return this.request<NotificationPrefs>("PUT", "/api/v1/auth/notify", { body: params });
  }

  // -------------------------------------------------------------------------
  // Billing
  // -------------------------------------------------------------------------

  /**
   * Get account balance and transaction history.
   */
  async getBalance(): Promise<BalanceResponse> {
    return this.request<BalanceResponse>("GET", "/api/v1/billing/balance", {});
  }

  /**
   * Create a Stripe checkout session to top up account balance.
   */
  async createCheckout(params: CreateCheckoutParams): Promise<CheckoutResponse> {
    return this.request<CheckoutResponse>("POST", "/api/v1/billing/stripe/checkout", {
      body: params,
    });
  }

  // -------------------------------------------------------------------------
  // Exports
  // -------------------------------------------------------------------------

  /**
   * Export a bot session as a PDF. Returns an ArrayBuffer.
   */
  async exportPdf(id: string): Promise<ArrayBuffer> {
    return this.request("GET", `/api/v1/bot/${id}/export/pdf`, { binary: true });
  }

  /**
   * Export a bot session as structured JSON.
   */
  async exportJson(id: string): Promise<ExportJsonResponse> {
    return this.request<ExportJsonResponse>("GET", `/api/v1/bot/${id}/export/json`, {});
  }

  /**
   * Export a bot session as an SRT subtitle file. Returns an ArrayBuffer.
   */
  async exportSrt(id: string): Promise<ArrayBuffer> {
    return this.request("GET", `/api/v1/bot/${id}/export/srt`, { binary: true });
  }

  /**
   * Export a bot session as Markdown. Returns an ArrayBuffer.
   */
  async exportMarkdown(id: string): Promise<ArrayBuffer> {
    return this.request("GET", `/api/v1/bot/${id}/export/markdown`, { binary: true });
  }

  // -------------------------------------------------------------------------
  // Bots — Advanced
  // -------------------------------------------------------------------------

  /**
   * Get the raw transcript for a bot session.
   */
  async getTranscript(id: string): Promise<TranscriptResponse> {
    return this.request<TranscriptResponse>("GET", `/api/v1/bot/${id}/transcript`, {});
  }

  /**
   * Re-run AI analysis on a bot's transcript.
   */
  async analyzeBot(id: string, params: AnalyzeBotParams = {}): Promise<AnalysisResponse> {
    return this.request<AnalysisResponse>("POST", `/api/v1/bot/${id}/analyze`, { body: params });
  }

  /**
   * Get curated highlights from a meeting.
   */
  async getHighlights(id: string): Promise<HighlightsResponse> {
    return this.request<HighlightsResponse>("GET", `/api/v1/bot/${id}/highlight`, {});
  }

  /**
   * Ask a freeform question about a completed bot's transcript.
   */
  async askBot(id: string, params: AskBotParams): Promise<AskResponse> {
    return this.request<AskResponse>("POST", `/api/v1/bot/${id}/ask`, { body: params });
  }

  /**
   * Ask a question about a live in-progress bot's transcript.
   */
  async askLiveBot(id: string, params: AskBotParams): Promise<AskResponse> {
    return this.request<AskResponse>("POST", `/api/v1/bot/${id}/ask-live`, { body: params });
  }

  /**
   * Generate a follow-up email for a meeting.
   */
  async generateFollowupEmail(id: string, params: FollowupEmailParams = {}): Promise<FollowupEmailResponse> {
    return this.request<FollowupEmailResponse>("POST", `/api/v1/bot/${id}/followup-email`, { body: params });
  }

  /**
   * Rename speaker labels in a bot's transcript.
   */
  async renameSpeakers(id: string, params: RenameSpeakersParams): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("PATCH", `/api/v1/bot/${id}/speakers`, { body: params });
  }

  /**
   * Generate a shareable link for a meeting.
   */
  async shareBot(id: string): Promise<ShareResponse> {
    return this.request<ShareResponse>("POST", `/api/v1/bot/${id}/share`, { body: {} });
  }

  // -------------------------------------------------------------------------
  // Webhooks — Extended
  // -------------------------------------------------------------------------

  /**
   * List all supported webhook event types.
   */
  async listWebhookEvents(): Promise<WebhookEventsResponse> {
    return this.request<WebhookEventsResponse>("GET", "/api/v1/webhook/events", {});
  }

  /**
   * Send a test event to a webhook.
   */
  async testWebhook(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("POST", `/api/v1/webhook/${id}/test`, { body: {} });
  }

  /**
   * List all webhook deliveries across all webhooks.
   */
  async listAllDeliveries(params: ListAllDeliveriesParams = {}): Promise<WebhookDeliveryListResponse> {
    return this.request<WebhookDeliveryListResponse>("GET", "/api/v1/webhook/deliveries", {
      params: params as Record<string, number | undefined>,
    });
  }

  // -------------------------------------------------------------------------
  // Auth — Extended
  // -------------------------------------------------------------------------

  /**
   * Get current account information.
   */
  async getMe(): Promise<AccountInfo> {
    return this.request<AccountInfo>("GET", "/api/v1/auth/me", {});
  }

  /**
   * List all test (sandbox) API keys.
   */
  async listTestKeys(): Promise<ApiKeyListResponse> {
    return this.request<ApiKeyListResponse>("GET", "/api/v1/auth/test-keys", {});
  }

  /**
   * Create a new test (sandbox) API key.
   */
  async createTestKey(name: string): Promise<ApiKeyCreateResponse> {
    return this.request<ApiKeyCreateResponse>("POST", "/api/v1/auth/test-keys", { body: { name } });
  }

  /**
   * Delete the current account.
   */
  async deleteAccount(): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", "/api/v1/auth/account", {});
  }

  /**
   * Change the account type.
   */
  async updateAccountType(params: UpdateAccountTypeParams): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("PUT", "/api/v1/auth/account-type", { body: params });
  }

  // -------------------------------------------------------------------------
  // Templates
  // -------------------------------------------------------------------------

  /**
   * List all available analysis templates.
   */
  async listTemplates(): Promise<TemplateListResponse> {
    return this.request<TemplateListResponse>("GET", "/api/v1/templates", {});
  }

  /**
   * Get the default analysis prompt.
   */
  async getDefaultPrompt(): Promise<DefaultPromptResponse> {
    return this.request<DefaultPromptResponse>("GET", "/api/v1/templates/default-prompt", {});
  }

  // -------------------------------------------------------------------------
  // Analytics
  // -------------------------------------------------------------------------

  /**
   * Get account analytics dashboard.
   */
  async getAnalytics(): Promise<AnalyticsResponse> {
    return this.request<AnalyticsResponse>("GET", "/api/v1/analytics", {});
  }

  /**
   * Get recurring meeting insights.
   */
  async getRecurringAnalytics(attendees?: string): Promise<RecurringAnalyticsResponse> {
    const params: Record<string, string | undefined> = {};
    if (attendees !== undefined) {
      params.attendees = attendees;
    }
    return this.request<RecurringAnalyticsResponse>("GET", "/api/v1/analytics/recurring", { params });
  }

  /**
   * Get API usage statistics.
   */
  async getApiUsage(): Promise<ApiUsageResponse> {
    return this.request<ApiUsageResponse>("GET", "/api/v1/analytics/api-usage", {});
  }

  /**
   * Get personal analytics.
   */
  async getMyAnalytics(): Promise<MyAnalyticsResponse> {
    return this.request<MyAnalyticsResponse>("GET", "/api/v1/analytics/me", {});
  }

  /**
   * Search meetings and transcripts.
   */
  async searchMeetings(params: SearchMeetingsParams): Promise<SearchResponse> {
    return this.request<SearchResponse>("GET", "/api/v1/search", {
      params: params as Record<string, string | number | undefined>,
    });
  }

  /**
   * Get account audit log.
   */
  async getAuditLog(params: GetAuditLogParams = {}): Promise<AuditLogResponse> {
    return this.request<AuditLogResponse>("GET", "/api/v1/audit-log", {
      params: params as Record<string, string | number | undefined>,
    });
  }

  // -------------------------------------------------------------------------
  // Action Items
  // -------------------------------------------------------------------------

  /**
   * List action items from meetings.
   */
  async listActionItems(params: ListActionItemsParams = {}): Promise<ActionItemListResponse> {
    return this.request<ActionItemListResponse>("GET", "/api/v1/action-items", {
      params: params as Record<string, string | number | undefined>,
    });
  }

  /**
   * Update an action item.
   */
  async updateActionItem(id: string, params: UpdateActionItemParams): Promise<ActionItemResponse> {
    return this.request<ActionItemResponse>("PATCH", `/api/v1/action-items/${id}`, { body: params });
  }

  // -------------------------------------------------------------------------
  // Keyword Alerts
  // -------------------------------------------------------------------------

  /**
   * List all keyword alerts.
   */
  async listKeywordAlerts(): Promise<KeywordAlertListResponse> {
    return this.request<KeywordAlertListResponse>("GET", "/api/v1/keyword-alerts", {});
  }

  /**
   * Create a new keyword alert.
   */
  async createKeywordAlert(params: CreateKeywordAlertParams): Promise<KeywordAlertResponse> {
    return this.request<KeywordAlertResponse>("POST", "/api/v1/keyword-alerts", { body: params });
  }

  /**
   * Get a keyword alert by ID.
   */
  async getKeywordAlert(id: string): Promise<KeywordAlertResponse> {
    return this.request<KeywordAlertResponse>("GET", `/api/v1/keyword-alerts/${id}`, {});
  }

  /**
   * Update a keyword alert.
   */
  async updateKeywordAlert(id: string, params: UpdateKeywordAlertParams): Promise<KeywordAlertResponse> {
    return this.request<KeywordAlertResponse>("PATCH", `/api/v1/keyword-alerts/${id}`, { body: params });
  }

  /**
   * Delete a keyword alert.
   */
  async deleteKeywordAlert(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/keyword-alerts/${id}`, {});
  }

  // -------------------------------------------------------------------------
  // Calendar Feeds
  // -------------------------------------------------------------------------

  /**
   * List all calendar feeds.
   */
  async listCalendarFeeds(): Promise<CalendarFeedListResponse> {
    return this.request<CalendarFeedListResponse>("GET", "/api/v1/calendar", {});
  }

  /**
   * Add a calendar feed.
   */
  async createCalendarFeed(params: CreateCalendarFeedParams): Promise<CalendarFeedResponse> {
    return this.request<CalendarFeedResponse>("POST", "/api/v1/calendar", { body: params });
  }

  /**
   * Delete a calendar feed.
   */
  async deleteCalendarFeed(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/calendar/${id}`, {});
  }

  /**
   * Trigger a sync for a calendar feed.
   */
  async syncCalendarFeed(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("POST", `/api/v1/calendar/${id}/sync`, { body: {} });
  }

  // -------------------------------------------------------------------------
  // Integrations
  // -------------------------------------------------------------------------

  /**
   * List all integrations.
   */
  async listIntegrations(): Promise<IntegrationListResponse> {
    return this.request<IntegrationListResponse>("GET", "/api/v1/integrations", {});
  }

  /**
   * Create a new integration.
   */
  async createIntegration(params: CreateIntegrationParams): Promise<IntegrationResponse> {
    return this.request<IntegrationResponse>("POST", "/api/v1/integrations", { body: params });
  }

  /**
   * Update an integration.
   */
  async updateIntegration(id: string, params: UpdateIntegrationParams): Promise<IntegrationResponse> {
    return this.request<IntegrationResponse>("PATCH", `/api/v1/integrations/${id}`, { body: params });
  }

  /**
   * Delete an integration.
   */
  async deleteIntegration(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/integrations/${id}`, {});
  }

  // -------------------------------------------------------------------------
  // Workspaces
  // -------------------------------------------------------------------------

  /**
   * List workspaces the current account owns or is a member of.
   */
  async listWorkspaces(): Promise<WorkspaceListResponse> {
    return this.request<WorkspaceListResponse>("GET", "/api/v1/workspaces", {});
  }

  /**
   * Create a new workspace.
   */
  async createWorkspace(name: string): Promise<WorkspaceResponse> {
    return this.request<WorkspaceResponse>("POST", "/api/v1/workspaces", { body: { name } });
  }

  /**
   * Get workspace details.
   */
  async getWorkspace(id: string): Promise<WorkspaceResponse> {
    return this.request<WorkspaceResponse>("GET", `/api/v1/workspaces/${id}`, {});
  }

  /**
   * Update a workspace.
   */
  async updateWorkspace(id: string, params: UpdateWorkspaceParams): Promise<WorkspaceResponse> {
    return this.request<WorkspaceResponse>("PATCH", `/api/v1/workspaces/${id}`, { body: params });
  }

  /**
   * Delete a workspace (owner only).
   */
  async deleteWorkspace(id: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/workspaces/${id}`, {});
  }

  /**
   * List members of a workspace.
   */
  async listWorkspaceMembers(id: string): Promise<WorkspaceMemberListResponse> {
    return this.request<WorkspaceMemberListResponse>("GET", `/api/v1/workspaces/${id}/members`, {});
  }

  /**
   * Add a member to a workspace.
   */
  async addWorkspaceMember(id: string, params: AddWorkspaceMemberParams): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("POST", `/api/v1/workspaces/${id}/members`, { body: params });
  }

  /**
   * Remove a member from a workspace.
   */
  async removeWorkspaceMember(workspaceId: string, accountId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("DELETE", `/api/v1/workspaces/${workspaceId}/members/${accountId}`, {});
  }

  // -------------------------------------------------------------------------
  // Retention
  // -------------------------------------------------------------------------

  /**
   * Get the current retention policy.
   */
  async getRetentionPolicy(): Promise<RetentionPolicyResponse> {
    return this.request<RetentionPolicyResponse>("GET", "/api/v1/retention", {});
  }

  /**
   * Update the retention policy.
   */
  async updateRetentionPolicy(params: UpdateRetentionPolicyParams): Promise<RetentionPolicyResponse> {
    return this.request<RetentionPolicyResponse>("PUT", "/api/v1/retention", { body: params });
  }

  // -------------------------------------------------------------------------
  // MCP
  // -------------------------------------------------------------------------

  /**
   * Get the MCP server manifest and tool list.
   */
  async getMcpSchema(): Promise<McpSchemaResponse> {
    return this.request<McpSchemaResponse>("GET", "/api/v1/mcp/schema", {});
  }

  /**
   * Execute an MCP tool.
   */
  async callMcpTool(params: CallMcpToolParams): Promise<McpCallResponse> {
    return this.request<McpCallResponse>("POST", "/api/v1/mcp/call", { body: params });
  }
}
