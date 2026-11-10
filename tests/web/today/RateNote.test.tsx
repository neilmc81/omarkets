import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RateNote } from "../../../web/src/today/RateNote";
import { ok } from "../deskFixtures";

afterEach(() => vi.unstubAllGlobals());

const state = (rating: string | null) => ({ note_id: "move:NQ:1", rating, rated_at: rating ? "2026-10-22T18:00:00Z" : null });

describe("RateNote", () => {
  it("shows the stored mark and nothing is pressed when unrated", () => {
    render(<RateNote noteId="move:NQ:1" rating={null} />);
    expect(screen.getByRole("button", { name: "Mark this note useful" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Mark this note not useful" })).toHaveAttribute("aria-pressed", "false");
    render(<RateNote noteId="move:NQ:2" rating="not_useful" />);
    expect(screen.getAllByRole("button", { name: "Mark this note not useful" })[1]).toHaveAttribute("aria-pressed", "true");
  });

  it("sends a PUT, shows the stored answer, changes it, and clears it with a DELETE", async () => {
    const answers = [state("useful"), state("not_useful"), state(null)];
    const fetcher = vi.fn(() => ok(answers.shift()));
    vi.stubGlobal("fetch", fetcher);
    render(<RateNote noteId="move:NQ:1" rating={null} />);
    const useful = screen.getByRole("button", { name: "Mark this note useful" });
    const notUseful = screen.getByRole("button", { name: "Mark this note not useful" });

    fireEvent.click(useful);
    await waitFor(() => expect(useful).toHaveAttribute("aria-pressed", "true"));
    expect(fetcher).toHaveBeenLastCalledWith("/api/v1/desk/notes/move%3ANQ%3A1/rating",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ rating: "useful" }) }));

    fireEvent.click(notUseful);
    await waitFor(() => expect(notUseful).toHaveAttribute("aria-pressed", "true"));
    expect(useful).toHaveAttribute("aria-pressed", "false");
    expect(fetcher).toHaveBeenLastCalledWith(expect.any(String),
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ rating: "not_useful" }) }));

    fireEvent.click(notUseful); // pressing the lit button clears it
    await waitFor(() => expect(notUseful).toHaveAttribute("aria-pressed", "false"));
    expect(fetcher).toHaveBeenLastCalledWith(expect.any(String), expect.objectContaining({ method: "DELETE" }));
  });

  it("keeps the old mark and says so when the core refuses", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ detail: "Note not found" }), { status: 404 }))));
    render(<RateNote noteId="move:NQ:1" rating="useful" />);
    fireEvent.click(screen.getByRole("button", { name: "Mark this note not useful" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Note not found");
    expect(screen.getByRole("button", { name: "Mark this note useful" })).toHaveAttribute("aria-pressed", "true");
  });

  it("is reachable by keyboard: native buttons in a labelled group", () => {
    render(<RateNote noteId="move:NQ:1" rating={null} />);
    const group = screen.getByRole("group", { name: "Was this note useful?" });
    expect(group.querySelectorAll("button")).toHaveLength(2);
    for (const button of group.querySelectorAll("button")) expect(button.tabIndex).toBe(0);
  });
});
