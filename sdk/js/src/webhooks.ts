/**
 * Webhook signature verification.
 *
 * JustHereToListen.io signs every webhook delivery with HMAC-SHA256 over
 * `${timestamp}.${body}`. The signature is sent in `X-MeetingBot-Signature`
 * (`sha256=<hex>`) and the timestamp in `X-MeetingBot-Timestamp` (Unix seconds).
 *
 * Receivers MUST:
 * 1. Verify the signature with a constant-time compare.
 * 2. Reject deliveries whose timestamp is older than `maxAgeSeconds` to
 *    prevent replay (default: 300 s).
 */

import { createHmac, timingSafeEqual } from "node:crypto";

import { MeetingBotError } from "./errors.js";

export class WebhookVerificationError extends MeetingBotError {
  constructor(message: string) {
    super(message);
    this.name = "WebhookVerificationError";
  }
}

export interface VerifyWebhookOptions {
  /** Maximum acceptable age in seconds (default: 300). */
  maxAgeSeconds?: number;
  /** Override the current time in seconds, for testing. */
  now?: number;
}

/**
 * Verify a webhook delivery's signature and freshness.
 *
 * Throws {@link WebhookVerificationError} if the signature is invalid or the
 * timestamp is missing, malformed, or outside the freshness window.
 */
export function verifyWebhook(
  body: string | Buffer,
  timestamp: string | number,
  signature: string,
  secret: string,
  options: VerifyWebhookOptions = {},
): void {
  const { maxAgeSeconds = 300, now } = options;

  if (!signature || !signature.startsWith("sha256=")) {
    throw new WebhookVerificationError("Missing or malformed signature header");
  }

  const tsInt = Number.parseInt(String(timestamp).trim(), 10);
  if (!Number.isFinite(tsInt)) {
    throw new WebhookVerificationError(`Invalid timestamp: ${String(timestamp)}`);
  }

  const current = now ?? Math.floor(Date.now() / 1000);
  if (Math.abs(current - tsInt) > maxAgeSeconds) {
    throw new WebhookVerificationError(
      `Timestamp ${tsInt} is outside the ${maxAgeSeconds}s freshness window`,
    );
  }

  const bodyBuf = typeof body === "string" ? Buffer.from(body, "utf8") : body;
  const signedPayload = Buffer.concat([Buffer.from(`${tsInt}.`, "utf8"), bodyBuf]);
  const expected =
    "sha256=" + createHmac("sha256", secret).update(signedPayload).digest("hex");

  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(signature, "utf8");
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    throw new WebhookVerificationError("Signature mismatch");
  }
}
