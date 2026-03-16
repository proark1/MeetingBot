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

// All TypeScript types/interfaces
export type {
  AnalysisMode,
  ApiKey,
  ApiKeyCreateResponse,
  ApiKeyListResponse,
  BalanceResponse,
  BotListResponse,
  BotResponse,
  BotStats,
  BotSummary,
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
  Transaction,
  UpdateNotificationPrefsParams,
  UpdateWebhookParams,
  WebhookDelivery,
  WebhookDeliveryListResponse,
  WebhookListResponse,
  WebhookResponse,
} from "./types.js";
