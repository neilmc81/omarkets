import { healthResponseSchema, instrumentsResponseSchema } from "./schemas";

export class ApiError extends Error {
  constructor(message: string, readonly status: number | null = null) {
    super(message);
    this.name = "ApiError";
  }
}

export class ApiValidationError extends ApiError {
  constructor(message: string) {
    super(message);
    this.name = "ApiValidationError";
  }
}

export const REQUEST_TIMEOUT_MS = 5000;

// A GET with a timeout whose response is validated before use. Errors never
// echo the response body.
async function requestValidated<T>(url: string, schema: { parse: (data: unknown) => T }, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: "no-store" });
    if (!response.ok) throw new ApiError(`HTTP ${response.status} from ${url}`, response.status);
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      throw new ApiValidationError(`Invalid JSON from ${url}`);
    }
    try {
      return schema.parse(body);
    } catch {
      throw new ApiValidationError(`Invalid response from ${url}`);
    }
  } catch (error) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    if (timedOut) throw new ApiError(`Request timed out: ${url}`);
    if (error instanceof ApiError) throw error;
    throw new ApiError(`Backend unavailable: ${url}`);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export const fetchHealth = (signal?: AbortSignal) => requestValidated("/health", healthResponseSchema, signal);
export const fetchInstruments = (signal?: AbortSignal) => requestValidated("/api/v1/instruments", instrumentsResponseSchema, signal);
