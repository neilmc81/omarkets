import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Explain, NavigateContext, placeCard } from "../../../web/src/shell/Explain";

const card = () => screen.queryByRole("tooltip");

function setup(props: Partial<Parameters<typeof Explain>[0]> = {}) {
  render(<div>
    <Explain title="2-year yield" lines={["Last: 4.658%", "As of Thu 08:30 ET"]} {...props}>4.658%</Explain>
    <button>elsewhere</button>
  </div>);
  return screen.getByRole("button", { name: /4\.658%/ });
}

describe("<Explain>", () => {
  it("never opens on hover or pointer movement", () => {
    const trigger = setup();
    fireEvent.mouseEnter(trigger);
    fireEvent.mouseOver(trigger);
    fireEvent.mouseMove(trigger);
    fireEvent.pointerMove(trigger);
    expect(card()).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("never opens on keyboard focus alone, and closes when focus leaves", () => {
    const trigger = setup();
    act(() => trigger.focus());
    expect(card()).toBeNull();
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(card()).toBeInTheDocument();
    act(() => screen.getByRole("button", { name: "elsewhere" }).focus());
    expect(card()).toBeNull();
  });

  it("toggles on click, Enter and Space, and stays open after the pointer leaves", async () => {
    const trigger = setup();
    fireEvent.click(trigger);
    expect(card()).toHaveTextContent("2-year yield");
    expect(card()).toHaveTextContent("As of Thu 08:30 ET");
    expect(trigger).toHaveAttribute("aria-describedby", card()!.id);
    fireEvent.mouseLeave(trigger);
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(card()).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(card()).toBeNull();
    expect(trigger).not.toHaveAttribute("aria-describedby");
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(card()).toBeInTheDocument();
    fireEvent.keyDown(trigger, { key: " " });
    expect(card()).toBeNull();
  });

  it("closes on Escape and on a press outside, but not on a press inside the card", () => {
    const trigger = setup();
    fireEvent.click(trigger);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(card()).toBeNull();
    fireEvent.click(trigger);
    fireEvent.pointerDown(card()!);
    expect(card()).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(card()).toBeNull();
  });

  it("keeps only one card open at a time", () => {
    render(<div>
      <Explain title="First" lines={["one"]}>first</Explain>
      <Explain title="Second" lines={["two"]}>second</Explain>
    </div>);
    fireEvent.click(screen.getByRole("button", { name: "first" }));
    expect(screen.getByRole("tooltip", { name: "First" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "second" }));
    expect(screen.getAllByRole("tooltip")).toHaveLength(1);
    expect(screen.getByRole("tooltip", { name: "Second" })).toBeInTheDocument();
  });

  it("shows a visible cue: dotted text inline, ⓘ on blocks", () => {
    const { container } = render(<Explain block title="NQ" lines={["Last: 1"]}>tile</Explain>);
    expect(container.querySelector(".explain-block .explain-cue")).toHaveTextContent("ⓘ");
    const trigger = setup();
    expect(trigger).toHaveClass("explain-trigger");
    expect(trigger.parentElement).toHaveClass("explain-inline");
  });

  it("becomes a dialog with note and Learn links, followed in-app", () => {
    const navigate = vi.fn();
    render(<NavigateContext.Provider value={navigate}>
      <Explain title="Jobless claims" lines={["Lower than expected"]} noteId="release:a b" learnLink="?view=learn">claims</Explain>
    </NavigateContext.Provider>);
    fireEvent.click(screen.getByRole("button", { name: "claims" }));
    const dialog = screen.getByRole("dialog", { name: "Jobless claims" });
    const read = screen.getByRole("link", { name: "Read the note →" });
    expect(read).toHaveAttribute("href", "?note=release%3Aa%20b");
    expect(dialog).toContainElement(read);
    fireEvent.click(read);
    expect(navigate).toHaveBeenCalledWith("?note=release%3Aa%20b");
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "claims" }));
    fireEvent.click(screen.getByRole("link", { name: "Learn more →" }));
    expect(navigate).toHaveBeenLastCalledWith("?view=learn");
  });
});

describe("placeCard", () => {
  const viewport = { width: 1000, height: 800 };
  const rect = (left: number, top: number, width = 80, height = 20) =>
    ({ left, top, right: left + width, bottom: top + height, width, height, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  const size = { width: 300, height: 120 };

  it("sits below the trigger when it fits", () => {
    expect(placeCard(rect(100, 100), size, viewport)).toEqual({ left: 100, top: 126 });
  });
  it("never runs past the right or left edge", () => {
    expect(placeCard(rect(950, 100), size, viewport).left).toBe(1000 - 300 - 8);
    expect(placeCard(rect(-20, 100), size, viewport).left).toBe(8);
  });
  it("flips above the trigger near the bottom, and never above the top", () => {
    expect(placeCard(rect(100, 760), size, viewport).top).toBe(760 - 120 - 6);
    expect(placeCard(rect(100, 50), { width: 300, height: 790 }, viewport).top).toBe(8);
  });
});
