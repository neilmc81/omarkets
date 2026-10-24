import { afterEach, describe, expect, it, vi } from "vitest";
import { groupNotes, LAST_SEEN_KEY, newSince, nyDay, readLastSeen, writeLastSeen } from "../../../web/src/today/notesList";

const at = (iso: string, id = iso) => ({ id, created_at: iso });
const keys = (groups: ReturnType<typeof groupNotes<{ id: string; created_at: string }>>) =>
  Object.fromEntries(groups.map((g) => [g.key, g.notes.map((n) => n.id)]));

afterEach(() => { vi.restoreAllMocks(); window.localStorage.clear(); });

describe("groupNotes", () => {
  it("splits at New York midnight, not UTC midnight, newest first", () => {
    const now = Date.parse("2026-10-22T18:00:00Z"); // Thu 14:00 EDT
    const groups = groupNotes([
      at("2026-10-22T03:59:00Z", "wed-late"), // Wed 23:59 EDT
      at("2026-10-22T17:27:00Z", "thu-1327"),
      at("2026-10-22T04:00:00Z", "thu-0000"), // Thu 00:00 EDT
      at("2026-10-21T04:00:00Z", "wed-0000"),
      at("2026-10-21T03:59:00Z", "tue-late"),
    ], now);
    expect(groups.map((g) => g.key)).toEqual(["today", "yesterday", "earlier"]);
    expect(keys(groups)).toEqual({
      today: ["thu-1327", "thu-0000"], yesterday: ["wed-late", "wed-0000"], earlier: ["tue-late"],
    });
  });

  it("follows the fall-back change (Nov 1 2026): midnight moves from 04:00 to 05:00 UTC", () => {
    const now = Date.parse("2026-11-02T15:00:00Z"); // Mon 10:00 EST
    expect(keys(groupNotes([
      at("2026-11-02T05:00:00Z", "mon-0000"), // Mon 00:00 EST
      at("2026-11-02T04:59:00Z", "sun-late"), // Sun 23:59 EST
      at("2026-11-01T04:00:00Z", "sun-0000"), // Sun 00:00 EDT: the 25-hour day
      at("2026-11-01T03:59:00Z", "sat-late"),
    ], now))).toEqual({ today: ["mon-0000"], yesterday: ["sun-late", "sun-0000"], earlier: ["sat-late"] });
  });

  it("follows the spring-forward change (Mar 14 2027): the 23-hour day", () => {
    const now = Date.parse("2027-03-15T14:00:00Z"); // Mon 10:00 EDT
    expect(keys(groupNotes([
      at("2027-03-15T04:00:00Z", "mon-0000"), // Mon 00:00 EDT
      at("2027-03-15T03:59:00Z", "sun-late"),
      at("2027-03-14T05:00:00Z", "sun-0000"), // Sun 00:00 EST
      at("2027-03-14T04:59:00Z", "sat-late"),
    ], now))).toEqual({ today: ["mon-0000"], yesterday: ["sun-late", "sun-0000"], earlier: ["sat-late"] });
  });

  it("finds yesterday across a month end and leaves empty groups out", () => {
    const now = Date.parse("2026-11-01T14:00:00Z");
    expect(keys(groupNotes([at("2026-10-31T16:00:00Z", "oct31")], now))).toEqual({ yesterday: ["oct31"] });
    expect(groupNotes([], now)).toEqual([]);
    expect(nyDay("2026-11-01T03:59:00Z")).toBe("2026-10-31");
  });
});

describe("newSince", () => {
  const notes = [at("2026-10-22T17:27:00Z", "new"), at("2026-10-22T14:49:00Z", "seen")];

  it("marks notes written after the last visit", () => {
    expect([...newSince(notes, Date.parse("2026-10-22T15:00:00Z"))]).toEqual(["new"]);
    expect([...newSince(notes, Date.parse("2026-10-22T17:27:00Z"))]).toEqual([]); // not strictly after
  });

  it("marks nothing new on a first visit (no last-seen time)", () => {
    expect(newSince(notes, null).size).toBe(0);
  });
});

describe("last-seen storage", () => {
  it("round-trips an ISO time", () => {
    writeLastSeen(Date.parse("2026-10-22T15:00:00Z"));
    expect(window.localStorage.getItem(LAST_SEEN_KEY)).toBe("2026-10-22T15:00:00.000Z");
    expect(readLastSeen()).toBe(Date.parse("2026-10-22T15:00:00Z"));
  });

  it("treats missing or garbled values as never seen", () => {
    expect(readLastSeen()).toBeNull();
    window.localStorage.setItem(LAST_SEEN_KEY, "not a date");
    expect(readLastSeen()).toBeNull();
  });

  it("survives blocked or full storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("QuotaExceededError"); });
    expect(readLastSeen()).toBeNull();
    expect(() => writeLastSeen(Date.now())).not.toThrow();
  });
});
