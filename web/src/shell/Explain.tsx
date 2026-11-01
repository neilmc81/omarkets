import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode, type RefObject } from "react";

// In-app navigation for links inside cards (`?note=...`, `?view=learn`). The
// shell provides it; without it the links fall back to a normal page load.
export const NavigateContext = createContext<((search: string) => void) | null>(null);

export function noteSearch(noteId: string): string {
  return `?note=${encodeURIComponent(noteId)}`;
}

// Follow an in-app link without a page load. Modified clicks (new tab,
// new window) and a missing shell keep the browser's normal behaviour.
function useFollow(before?: () => void) {
  const navigate = useContext(NavigateContext);
  return (search: string) => (event: MouseEvent) => {
    if (!navigate || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    before?.();
    navigate(search);
  };
}

/** "Read the note →": opens Today with that desk note expanded. */
export function NoteLink({ noteId, children = "Read the note →" }: { noteId: string; children?: ReactNode }) {
  const follow = useFollow();
  return <a className="note-link" href={noteSearch(noteId)} onClick={follow(noteSearch(noteId))}>{children}</a>;
}

const EDGE = 8;
const GAP = 6;

type Position = { left: number; top: number };

// Keep the card inside the viewport: below the trigger when it fits, above
// it otherwise, and never past the left or right edge.
export function placeCard(trigger: DOMRect, card: { width: number; height: number }, viewport: { width: number; height: number }): Position {
  const left = Math.max(EDGE, Math.min(trigger.left, viewport.width - card.width - EDGE));
  const below = trigger.bottom + GAP;
  const top = below + card.height <= viewport.height - EDGE ? below : Math.max(EDGE, trigger.top - card.height - GAP);
  return { left, top };
}

// Only one explain card is open at a time, anywhere in the app: opening one
// closes the last. An open card also closes on Esc or a press outside `root`.
let openCard: { close: () => void } | null = null;

export function useCardDismiss(open: boolean, close: () => void, root: RefObject<HTMLElement | null>) {
  const latestClose = useRef(close);
  useEffect(() => { latestClose.current = close; });
  useEffect(() => {
    if (!open) return;
    const self = { close: () => latestClose.current() };
    openCard?.close();
    openCard = self;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") self.close();
    };
    const onPress = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node | null)) self.close();
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPress, true);
    return () => {
      if (openCard === self) openCard = null;
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPress, true);
    };
  }, [open, root]);
}

export type ExplainProps = {
  title: string;
  lines: string[];
  noteId?: string | null;
  learnLink?: string | null;
  /** Block mode wraps a whole tile and marks it with ⓘ; inline mode underlines text. */
  block?: boolean;
  className?: string;
  children: ReactNode;
};

export function Explain({ title, lines, noteId, learnLink, block = false, className = "", children }: ExplainProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<Position | null>(null);
  const rootRef = useRef<HTMLElement | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const actions = Boolean(noteId || learnLink);
  // Opened with Enter/Space: focus moves into the card once it is placed, so
  // the keyboard can scroll a long card and reach its links.
  const keyboardOpen = useRef(false);

  // Esc, a second toggle or one card replacing another: if focus was inside
  // the card it goes back to the trigger instead of falling to the page.
  const close = useCallback(() => {
    if (cardRef.current?.contains(document.activeElement)) triggerRef.current?.focus();
    setOpen(false);
  }, []);
  useCardDismiss(open, close, rootRef);

  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const place = () => {
      if (!triggerRef.current || !cardRef.current) return;
      const card = cardRef.current.getBoundingClientRect();
      setPosition(placeCard(triggerRef.current.getBoundingClientRect(), card, { width: window.innerWidth, height: window.innerHeight }));
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open || !position || !keyboardOpen.current) return;
    keyboardOpen.current = false;
    cardRef.current?.focus({ preventScroll: true });
  }, [open, position]);

  // Only a click (or Enter/Space) opens the card; a second one closes it.
  // Hover and keyboard focus never open it on their own.
  const toggle = () => (open ? close() : setOpen(true));
  const follow = useFollow(close);

  const Wrapper = block ? "div" : "span";
  const Trigger = block ? "div" : "span";
  return <Wrapper ref={(node: HTMLElement | null) => { rootRef.current = node; }}
    className={`explain ${block ? "explain-block" : "explain-inline"}`}
    onBlur={(event) => {
      // Focus already left for something else: let it stay there.
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
    }}>
    <Trigger ref={(node: HTMLElement | null) => { triggerRef.current = node; }} className={`explain-trigger ${className}`}
      role="button" tabIndex={0} aria-expanded={open} aria-describedby={open ? id : undefined}
      onClick={() => {
        keyboardOpen.current = false;
        toggle();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          keyboardOpen.current = !open;
          toggle();
        }
      }}>
      {children}
      {block && <span className="explain-cue" aria-hidden="true">ⓘ</span>}
    </Trigger>
    {open && <div ref={cardRef} id={id} className="explain-card" role={actions ? "dialog" : "tooltip"} aria-label={title}
      tabIndex={0}
      style={position ? { left: position.left, top: position.top } : { visibility: "hidden", left: 0, top: 0 }}>
      <b className="explain-title">{title}</b>
      {lines.map((line, index) => <p key={index}>{line}</p>)}
      {actions && <p className="explain-links">
        {noteId && <a href={noteSearch(noteId)} onClick={follow(noteSearch(noteId))}>Read the note →</a>}
        {learnLink && <a href={learnLink} onClick={follow(learnLink)}>Learn more →</a>}
      </p>}
    </div>}
  </Wrapper>;
}
