import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NavigateContext, NoteLink } from "../../../web/src/shell/Explain";
import { playbookSchema } from "../../../web/src/today/api";

const NOTE = "release:20260917T1230:CPI";

const release = (note_id?: string | null) => ({
  at: "2026-09-17T12:30:00Z", title: "CPI", alongside: [],
  figure: { name: "CPI m/m", unit: "percent", actual: 0.4, forecast: 0.3, previous: 0.2, decimals: 1,
    verdict: "above", actual_text: "0.4%", forecast_text: "0.3%", previous_text: "0.2%" },
  nq_15m: null, us2y_15m: null, nq_close: null, close_pending: true, held: null,
  ...(note_id === undefined ? {} : { note_id }),
});

const book = (r: unknown) => ({ generated_at: "2026-10-23T00:00:00Z", since: null, min_releases: 5,
  indicators: [{ name: "CPI m/m", count: 1, enough: false, text: "Only 1.", buckets: [], releases: [r] }] });

describe("note_id fields", () => {
  it("playbook releases carry a note id or an explicit null", () => {
    expect(playbookSchema.parse(book(release(NOTE))).indicators[0].releases[0].note_id).toBe(NOTE);
    expect(playbookSchema.parse(book(release(null))).indicators[0].releases[0].note_id).toBeNull();
    expect(() => playbookSchema.parse(book(release()))).toThrow(); // the core always sends it
    expect(() => playbookSchema.parse(book(release("")))).toThrow();
  });
});

describe("Read the note links", () => {
  it("navigates in-app to ?note=", () => {
    const navigate = vi.fn();
    render(<NavigateContext.Provider value={navigate}><NoteLink noteId={NOTE} /></NavigateContext.Provider>);
    const link = screen.getByRole("link", { name: "Read the note →" });
    expect(link).toHaveAttribute("href", "?note=release%3A20260917T1230%3ACPI");
    fireEvent.click(link);
    expect(navigate).toHaveBeenCalledWith("?note=release%3A20260917T1230%3ACPI");
  });

  it("leaves modified clicks (new tab) to the browser", () => {
    const navigate = vi.fn();
    // The outer handler stands in for the browser (jsdom cannot open tabs).
    render(<NavigateContext.Provider value={navigate}>
      <div onClick={(event) => event.preventDefault()}><NoteLink noteId={NOTE} /></div>
    </NavigateContext.Provider>);
    fireEvent.click(screen.getByRole("link"), { ctrlKey: true });
    expect(navigate).not.toHaveBeenCalled();
  });
});
