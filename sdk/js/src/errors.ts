/**
 * Typed error classes for the MeetingBot SDK.
 */

export class MeetingBotError extends Error {
  public readonly statusCode: number | undefined;
  public readonly detail: string | undefined;

  constructor(message: string, statusCode?: number, detail?: string) {
    super(message);
    this.name = "MeetingBotError";
    this.statusCode = statusCode;
    this.detail = detail;
    // Restore prototype chain (needed when targeting ES5 with TypeScript)
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Thrown on HTTP 401 or 403 responses. */
export class AuthError extends MeetingBotError {
  constructor(message: string, statusCode?: number, detail?: string) {
    super(message, statusCode, detail);
    this.name = "AuthError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Thrown on HTTP 404 responses. */
export class NotFoundError extends MeetingBotError {
  constructor(message: string, statusCode?: number, detail?: string) {
    super(message, statusCode, detail);
    this.name = "NotFoundError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Thrown on HTTP 422 responses. */
export class ValidationError extends MeetingBotError {
  constructor(message: string, statusCode?: number, detail?: string) {
    super(message, statusCode, detail);
    this.name = "ValidationError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Thrown on HTTP 429 responses. */
export class RateLimitError extends MeetingBotError {
  constructor(message: string, statusCode?: number, detail?: string) {
    super(message, statusCode, detail);
    this.name = "RateLimitError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

/** Thrown on HTTP 5xx responses. */
export class ServerError extends MeetingBotError {
  constructor(message: string, statusCode?: number, detail?: string) {
    super(message, statusCode, detail);
    this.name = "ServerError";
    Object.setPrototypeOf(this, new.target.prototype);
  }
}
