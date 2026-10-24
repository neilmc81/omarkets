import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";

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

const CLOSE_DELAY_MS = 150;
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
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState<Position | null>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const open = hovered || focused || pinned;
  const actions = Boolean(noteId || learnLink);

  const close = useCallback(() => {
    clearTimeout(closeTimer.current);
    setHovered(false);
    setFocused(false);
    setPinned(false);
  }, []);

  useEffect(() => () => clearTimeout(closeTimer.current), []);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, close]);

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

  const enter = () => {
    clearTimeout(closeTimer.current);
    setHovered(true);
  };
  const leave = () => {
    clearTimeout(closeTimer.current);
    closeTimer.current = setTimeout(() => setHovered(false), CLOSE_DELAY_MS);
  };
  // Click (or Enter) pins the card open; a second click closes it.
  const toggle = () => {
    if (pinned) close();
    else setPinned(true);
  };
  const follow = useFollow(close);

  const Wrapper = block ? "div" : "span";
  const Trigger = block ? "div" : "span";
  return <Wrapper className={`explain ${block ? "explain-block" : "explain-inline"}`} onMouseEnter={enter} onMouseLeave={leave}
    onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close();
    }}>
    <Trigger ref={(node: HTMLElement | null) => { triggerRef.current = node; }} className={`explain-trigger ${className}`}
      role="button" tabIndex={0} aria-expanded={open} aria-describedby={open ? id : undefined}
      onFocus={() => setFocused(true)}
      onClick={toggle}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggle();
        }
      }}>
      {children}
      {block && <span className="explain-cue" aria-hidden="true">ⓘ</span>}
    </Trigger>
    {open && <div ref={cardRef} id={id} className="explain-card" role={actions ? "dialog" : "tooltip"} aria-label={title}
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
