/**
 * MeetingBot JavaScript/TypeScript SDK
 *
 * @example
 * ```ts
 * import { MeetingBotClient } from "meetingbot-sdk";
 *
 * const client = new MeetingBotClient({ apiKey: "sk_live_..." });
 *
 * const bot = await client.createBot({
 *   meeting_url: "https://zoom.us/j/123456789",
 *   bot_name: "My Recorder",
 * });
 * console.log(bot.id, bot.status);
 * ```
 */

// Main client
export { MeetingBotClient } from "./client.js";

// Errors
export {
  AuthError,
  MeetingBotError,
  NotFoundError,
  RateLimitError,
  ServerError,
  ValidationError,
} from "./errors.js";

// Webhook signature verification
export { verifyWebhook, WebhookVerificationError } from "./webhooks.js";
export type { VerifyWebhookOptions } from "./webhooks.js";

// All TypeScript types/interfaces
export type {
  // Bot types
  AnalysisMode,
  BotResponse,
  BotSummary,
  BotListResponse,
  BotStats,
  CreateBotParams,
  ListBotsParams,

  // Webhook types
  CreateWebhookParams,
  UpdateWebhookParams,
  WebhookResponse,
  WebhookListResponse,
  WebhookDelivery,
  WebhookDeliveryListResponse,
  ListWebhookDeliveriesParams,
  WebhookEventsResponse,
  ListAllDeliveriesParams,

  // Auth types
  RegisterParams,
  LoginResponse,
  ApiKey,
  ApiKeyListResponse,
  ApiKeyCreateResponse,
  PlanInfo,
  NotificationPrefs,
  UpdateNotificationPrefsParams,

  // Billing types
  Transaction,
  BalanceResponse,
  CreateCheckoutParams,
  CheckoutResponse,

  // Export types
  ExportJsonResponse,

  // Transcript / Analysis types
  TranscriptEntry,
  TranscriptResponse,
  AnalysisResponse,
  HighlightsResponse,
  AskResponse,
  FollowupEmailResponse,
  ShareResponse,
  AnalyzeBotParams,
  AskBotParams,
  FollowupEmailParams,
  RenameSpeakersParams,

  // Template types
  TemplateInfo,
  TemplateListResponse,
  DefaultPromptResponse,

  // Analytics types
  AnalyticsResponse,
  RecurringAnalyticsResponse,
  ApiUsageResponse,
  MyAnalyticsResponse,
  SearchResult,
  SearchResponse,
  AuditLogEntry,
  AuditLogResponse,
  SearchMeetingsParams,
  GetAuditLogParams,

  // Action Item types
  ActionItemResponse,
  ActionItemListResponse,
  ActionItemStatsResponse,
  ListActionItemsParams,
  UpdateActionItemParams,

  // Keyword Alert types
  KeywordAlertResponse,
  KeywordAlertListResponse,
  CreateKeywordAlertParams,
  UpdateKeywordAlertParams,

  // Calendar Feed types
  CalendarFeedResponse,
  CalendarFeedListResponse,
  CreateCalendarFeedParams,

  // Integration types
  IntegrationResponse,
  IntegrationListResponse,
  CreateIntegrationParams,
  UpdateIntegrationParams,

  // Workspace types
  WorkspaceMemberResponse,
  WorkspaceResponse,
  WorkspaceListResponse,
  WorkspaceMemberListResponse,
  UpdateWorkspaceParams,
  AddWorkspaceMemberParams,

  // Retention types
  RetentionPolicyResponse,
  UpdateRetentionPolicyParams,

  // MCP types
  McpSchemaResponse,
  McpCallResponse,
  CallMcpToolParams,

  // Account types
  AccountInfo,
  UpdateAccountTypeParams,

  // Client config
  MeetingBotClientConfig,
} from "./types.js";
