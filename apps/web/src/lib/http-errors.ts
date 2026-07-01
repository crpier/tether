// Maps raw HTTP status codes to human-readable messages so the UI never
// surfaces bare "Request failed: <code>" text to the user. Callers can pass
// per-flow overrides (e.g. a failed login should read "Incorrect password."
// rather than the generic 401 copy).

export type HttpStatusMessages = Partial<Record<number, string>>;

const DEFAULT_STATUS_MESSAGES: HttpStatusMessages = {
  400: "That request wasn't valid.",
  401: "Please sign in to continue.",
  403: "You don't have permission to do that.",
  404: "We couldn't find what you asked for.",
  409: "That changed elsewhere. Refresh and try again.",
  422: "That request wasn't valid.",
  429: "Too many requests. Please wait a moment and try again.",
  500: "Something went wrong on the server. Please try again.",
  502: "The service is temporarily unavailable. Please try again.",
  503: "The service is temporarily unavailable. Please try again.",
  504: "The service is temporarily unavailable. Please try again.",
};

const GENERIC_MESSAGE = "The request could not be completed.";

export function httpStatusMessage(
  status: number,
  overrides?: HttpStatusMessages,
): string {
  return (
    overrides?.[status] ?? DEFAULT_STATUS_MESSAGES[status] ?? GENERIC_MESSAGE
  );
}
