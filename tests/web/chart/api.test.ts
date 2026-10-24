import { describe, expect, it } from "vitest";
import { barsSchema, intervalsFor, levelsSchema, pinsSchema, riskToneSchema, sessionCalendarSchema, sparklinesSchema } from "../../../web/src/chart/api";
import { deskSchema } from "../../../web/src/today/api";
import { desk } from "../deskFixtures";
import { bars, calendar, levels, pins, risk, sparklines } from "../chartFixtures";

describe("chart schemas", () => {
  it("accept the core's payloads", () => {
    expect(barsSchema.parse(bars).bars[2].count).toBe(0);
    expect(levelsSchema.parse(levels).levels).toHaveLength(8);
    expect(pinsSchema.parse(pins).pins[0].note_id).toBe("release:20261022T1230:INITIAL_CLAIMS");
    expect(riskToneSchema.parse(risk).tone).toBe("mixed");
    expect(sessionCalendarSchema.parse(calendar).session.segments).toHaveLength(5);
    expect(sparklinesSchema.parse(sparklines).lines[0].points[1].value).toBeNull();
  });

  it("keep unavailable as null and never coerce it to a number", () => {
    const missing = levelsSchema.parse(levels).levels.find((l) => l.key === "prior_day_high");
    expect(missing?.value).toBeNull();
    expect(() => levelsSchema.parse({ ...levels, levels: [{ ...levels.levels[0], value: undefined }] })).toThrow();
    expect(() => barsSchema.parse({ ...bars, bars: [{ ...bars.bars[0], close: "29150" }] })).toThrow();
  });

  it("reject unknown intervals, ranges, tones and importances", () => {
    expect(() => barsSchema.parse({ ...bars, interval: "2m" })).toThrow();
    expect(() => barsSchema.parse({ ...bars, range: "1y" })).toThrow();
    expect(() => riskToneSchema.parse({ ...risk, tone: "bullish" })).toThrow();
    expect(() => pinsSchema.parse({ ...pins, pins: [{ ...pins.pins[0], importance: "low" }] })).toThrow();
    expect(() => pinsSchema.parse({ ...pins, pins: [{ ...pins.pins[0], note_id: "" }] })).toThrow();
  });

  it("offer only the intervals the core allows per range", () => {
    expect(intervalsFor("1m")).toEqual(["15m", "1h"]);
    expect(intervalsFor("1d")).toEqual(["5m", "15m", "1h"]);
  });

  it("read the desk's next event and quote flags, with safe defaults for an older core", () => {
    const parsed = deskSchema.parse(desk);
    expect(parsed.next_event).toBeNull();
    expect(parsed.quotes[0].closed).toBe(false);
    const next = { at: "2026-10-29T12:30:00Z", title: "PCE + GDP", importance: "critical", kind: "economic",
      explainer: null, forecast_text: null, playbook_text: "No earlier releases measured yet." };
    expect(deskSchema.parse({ ...desk, next_event: next }).next_event?.title).toBe("PCE + GDP");
    expect(() => deskSchema.parse({ ...desk, next_event: { ...next, kind: "earnings" } })).toThrow();
  });
});
