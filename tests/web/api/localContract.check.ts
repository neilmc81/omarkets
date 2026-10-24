import { describe, expect, it } from "vitest";
import { healthResponseSchema } from "../../../web/src/api/schemas";
import { calendarBoardSchema } from "../../../web/src/calendar/api";
import { macroBoardSchema } from "../../../web/src/macro/api";
import { newsBoardSchema } from "../../../web/src/newsboard/api";
import { deskSchema, learnSchema, playbookSchema } from "../../../web/src/today/api";

// Run with `npm run test:live` against a running core: every page's payload
// must pass the schema the page parses it with.
const BASE = process.env.OMARKETS_TEST_BACKEND ?? "http://127.0.0.1:8765";
const reachable = await fetch(`${BASE}/health`, { signal: AbortSignal.timeout(1500) }).then((response) => response.ok).catch(() => false);
const get = async (path: string) => (await fetch(`${BASE}${path}`)).json();

describe.skipIf(!reachable)("live backend contract", () => {
  it("validates the real health and page payloads against the web schemas", async () => {
    expect(Array.isArray(healthResponseSchema.parse(await get("/health")).economic_providers)).toBe(true);
    expect(deskSchema.parse(await get("/api/v1/desk")).quotes.length).toBeGreaterThan(0);
    expect(calendarBoardSchema.parse(await get("/api/v1/desk/calendar")).week).toBe("this");
    expect(macroBoardSchema.parse(await get("/api/v1/desk/macro")).groups).toHaveLength(4);
    expect(Array.isArray(newsBoardSchema.parse(await get("/api/v1/desk/news")).items)).toBe(true);
    expect(Array.isArray(playbookSchema.parse(await get("/api/v1/desk/playbook")).indicators)).toBe(true);
    expect(learnSchema.parse(await get("/api/v1/desk/learn")).count).toBeGreaterThanOrEqual(0);
  }, 30_000);
});
