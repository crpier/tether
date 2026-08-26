import { httpStatusMessage, type HttpStatusMessages } from "../lib/http-errors";

// Carries status across the host boundary so conflict-aware UI can distinguish
// optimistic-concurrency failures while still showing normalized messages.
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, messages?: HttpStatusMessages) {
    super(httpStatusMessage(status, messages));
    this.name = "ApiError";
    this.status = status;
  }
}
