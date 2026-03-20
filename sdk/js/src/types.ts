/**
 * TypeScript interfaces for all MeetingBot API types.
 */

// ---------------------------------------------------------------------------
// Bot types
// ---------------------------------------------------------------------------

export type AnalysisMode = "full" | "transcript_only";

export interface CreateBotParams {
  /** The URL of the meeting to join. */
  meeting_url: string;
  /** Display name for the bot. Default: "MeetingBot". */
  bot_name?: string;
  /** Optional URL for the bot's avatar image. */
  bot_avatar_url?: string;
  /** URL to receive webhook events for this bot. */
  webhook_url?: string;
  /** ISO 8601 datetime string for scheduled join time. */
  join_at?: string;
  /** Analysis mode: "full" or "transcript_only". */
  analysis_mode?: AnalysisMode;
  /** Analysis template to use. */
  template?: string;
  /** Custom prompt to override the default analysis prompt. */
  prompt_override?: string;
  /** List of custom vocabulary words to aid transcription. */
  vocabulary?: string[];
  /** Whether the bot should respond when mentioned. */
  respond_on_mention?: boolean;
  /** How to respond on mention. */
  mention_response_mode?: string;
  /** Text-to-speech provider. */
  tts_provider?: string;
  /** Whether the bot should join muted. */
  start_muted?: boolean;
  /** Enable live transcription. */
  live_transcription?: boolean;
  /** Sub-user identifier for multi-tenant usage. */
  sub_user_id?: string;
  /** Arbitrary key-value metadata. */
  metadata?: Record<string, unknown>;
  /** Whether to record video. Default: false. */
  record_video?: boolean;
  /** Optional idempotency key to prevent duplicate bots. */
  idempotency_key?: string;
}

export interface BotResponse {
  id: string;
  meeting_url: string;
  bot_name: string;
  bot_avatar_url?: string | null;
  webhook_url?: string | null;
  join_at?: string | null;
  analysis_mode?: AnalysisMode | null;
  template?: string | null;
  prompt_override?: string | null;
  vocabulary?: string[] | null;
  respond_on_mention?: boolean | null;
  mention_response_mode?: string | null;
  tts_provider?: string | null;
  start_muted?: boolean | null;
  live_transcription?: boolean | null;
  sub_user_id?: string | null;
  metadata?: Record<string, unknown> | null;
  record_video: boolean;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
}

export interface BotSummary {
  id: string;
  meeting_url: string;
  bot_name: string;
  status?: string | null;
  created_at?: string | null;
  sub_user_id?: string | null;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface BotListResponse {
  results: BotSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ListBotsParams {
  limit?: number;
  offset?: number;
  status?: string;
  sub_user_id?: string;
}

export interface BotStats {
  total?: number | null;
  active?: number | null;
  completed?: number | null;
  failed?: number | null;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Webhook types
// ---------------------------------------------------------------------------

export interface CreateWebhookParams {
  /** Destination URL for event delivery. */
  url: string;
  /** List of event type strings to subscribe to. */
  events: string[];
  /** Optional HMAC signing secret. */
  secret?: string;
}

export interface UpdateWebhookParams {
  url?: string;
  events?: string[];
  secret?: string;
}

export interface WebhookResponse {
  id: string;
  url: string;
  events: string[];
  secret?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
}

export interface WebhookListResponse {
  results: WebhookResponse[];
  total?: number | null;
  [key: string]: unknown;
}

export interface WebhookDelivery {
  id: string;
  webhook_id: string;
  event?: string | null;
  payload?: Record<string, unknown> | null;
  response_status?: number | null;
  response_body?: string | null;
  delivered_at?: string | null;
  success?: boolean | null;
  [key: string]: unknown;
}

export interface WebhookDeliveryListResponse {
  results: WebhookDelivery[];
  total?: number | null;
  limit?: number | null;
  offset?: number | null;
}

export interface ListWebhookDeliveriesParams {
  limit?: number;
  offset?: number;
}

// ---------------------------------------------------------------------------
// Auth types
// ---------------------------------------------------------------------------

export interface RegisterParams {
  email: string;
  password: string;
  key_name: string;
  account_type?: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  [key: string]: unknown;
}

export interface ApiKey {
  id: string;
  name: string;
  key_prefix?: string | null;
  created_at?: string | null;
  last_used_at?: string | null;
  [key: string]: unknown;
}

export interface ApiKeyListResponse {
  results: ApiKey[];
  total?: number | null;
}

export interface ApiKeyCreateResponse {
  id: string;
  name: string;
  /** The full API key value — only returned once at creation time. */
  key: string;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface PlanInfo {
  plan?: string | null;
  status?: string | null;
  limits?: Record<string, unknown> | null;
  usage?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface NotificationPrefs {
  email_on_completion?: boolean | null;
  email_on_failure?: boolean | null;
  webhook_on_completion?: boolean | null;
  [key: string]: unknown;
}

export interface UpdateNotificationPrefsParams {
  email_on_completion?: boolean;
  email_on_failure?: boolean;
  webhook_on_completion?: boolean;
}

// ---------------------------------------------------------------------------
// Billing types
// ---------------------------------------------------------------------------

export interface Transaction {
  id: string;
  amount_usd: number;
  description?: string | null;
  created_at?: string | null;
  type?: string | null;
  [key: string]: unknown;
}

export interface BalanceResponse {
  balance_usd: number;
  transactions?: Transaction[] | null;
  [key: string]: unknown;
}

export interface CreateCheckoutParams {
  amount_usd: number;
  success_url: string;
  cancel_url: string;
}

export interface CheckoutResponse {
  checkout_url: string;
  session_id?: string | null;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Export types
// ---------------------------------------------------------------------------

export interface ExportJsonResponse {
  id?: string | null;
  transcript?: Array<Record<string, unknown>> | null;
  analysis?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Client config
// ---------------------------------------------------------------------------

export interface MeetingBotClientConfig {
  /** Your MeetingBot API key (Bearer token). */
  apiKey: string;
  /** Base URL for the API. Defaults to https://api.yourserver.com */
  baseUrl?: string;
  /** Request timeout in milliseconds. Defaults to 30000. */
  timeoutMs?: number;
}
