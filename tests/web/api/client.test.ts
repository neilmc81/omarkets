import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, ApiValidationError, fetchHealth } from "../../../web/src/api/client";
import { health } from "../fixtures";

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("validated health request", () => {
  it("returns the parsed health payload", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(health)));
    vi.stubGlobal("fetch", fetch);
    await expect(fetchHealth()).resolves.toEqual(health);
    expect(fetch.mock.calls[0][0]).toBe("/health");
  });

  it("reports HTTP errors without trusting response content", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("untrusted", { status: 503 })));
    await expect(fetchHealth()).rejects.toMatchObject({ status: 503 });
  });

  it("rejects invalid JSON and malformed shapes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{")));
    await expect(fetchHealth()).rejects.toBeInstanceOf(ApiValidationError);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}")));
    await expect(fetchHealth()).rejects.toBeInstanceOf(ApiValidationError);
  });

  it("reports network failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(fetchHealth()).rejects.toBeInstanceOf(ApiError);
  });

  it("times out and cancels pending requests", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn((_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    })));
    const timeout = expect(fetchHealth()).rejects.toThrow("timed out");
    await vi.advanceTimersByTimeAsync(5000);
    await timeout;
    const controller = new AbortController();
    const cancellation = expect(fetchHealth(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await cancellation;
    expect(vi.getTimerCount()).toBe(0);
  });
});
