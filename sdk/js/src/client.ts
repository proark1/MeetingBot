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
  ApiKeyCreateResponse,
  ApiKeyListResponse,
  BalanceResponse,
  BotListResponse,
  BotResponse,
  BotStats,
  CheckoutResponse,
  CreateBotParams,
  CreateCheckoutParams,
  CreateWebhookParams,
  ExportJsonResponse,
  ListBotsParams,
  ListWebhookDeliveriesParams,
  LoginResponse,
  MeetingBotClientConfig,
  NotificationPrefs,
  PlanInfo,
  RegisterParams,
  UpdateNotificationPrefsParams,
  UpdateWebhookParams,
  WebhookDeliveryListResponse,
  WebhookListResponse,
  WebhookResponse,
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
}
