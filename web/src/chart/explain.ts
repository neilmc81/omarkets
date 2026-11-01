import { explainerLines, type Explainer } from "../explainers/library";
import type { KeyLevel, Pin } from "./api";

// Times on the chart are ET first; "your time" goes in the cards.

const et = (options: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour12: false, ...options });
const clock = et({ hour: "2-digit", minute: "2-digit" });
const dayClock = et({ weekday: "short", hour: "2-digit", minute: "2-digit" });
const weekday = et({ weekday: "short" });
const local = new Intl.DateTimeFormat(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });

const date = (value: number | string) => new Date(typeof value === "number" ? value * 1000 : value);

/** "08:30" in New York, from UTC seconds. */
export const etClock = (time: number) => clock.format(date(time));
/** "Thu 08:30" in New York. */
export const etDayClock = (time: number | string) => dayClock.format(date(time));
/** "Thu" in New York. */
export const etWeekday = (time: number) => weekday.format(date(time));
/** "Thu 19:30 your time". */
export const yourTime = (iso: string) => `${local.format(date(iso))} your time`;

export function pinTitle(pin: Pin): string {
  return `${etDayClock(pin.at)} ET · ${pin.title}`;
}

export function pinLines(pin: Pin): string[] {
  return [
    yourTime(pin.at),
    ...(!pin.released ? ["Scheduled: not out yet."] : []),
    ...(pin.result_text ? [pin.result_text] : []),
    ...(pin.nq_15m_text ? [pin.nq_15m_text] : pin.released ? ["NQ's first 15 minutes: not measured yet."] : []),
    ...(pin.explainer ? [pin.explainer] : []),
    ...(pin.released && !pin.note_id ? ["No desk note for this release."] : []),
  ];
}

/** A level's card lines; the library entry (when loaded) replaces the short explainer. */
export function levelLines(level: KeyLevel, entry?: Explainer): string[] {
  return [
    ...(level.value === null ? [`Unavailable: ${level.unavailable_reason ?? "not measured"}.`] : []),
    ...(level.partial && level.value !== null ? ["So far: the window is still running."] : []),
    ...(entry ? explainerLines(entry) : [level.explainer]),
    ...(level.window_start && level.window_end ? [`Measured ${etDayClock(level.window_start)} – ${etDayClock(level.window_end)} ET.`] : []),
    "A rule of thumb, not advice.",
  ];
}
