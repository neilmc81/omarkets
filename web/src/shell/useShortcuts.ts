import { useEffect, useRef } from "react";

export function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

// Single-key shortcuts, keyed by lower-case `event.key`. Keys typed into a
// form field, or pressed with Ctrl/Alt/Meta, are left alone.
export function useShortcuts(keys: Record<string, () => void>) {
  const latest = useRef(keys);
  useEffect(() => { latest.current = keys; });
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey || isTyping(event.target)) return;
      const action = latest.current[event.key.toLowerCase()];
      if (!action) return;
      event.preventDefault();
      action();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
