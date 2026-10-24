import { useCallback, useEffect, useRef, useState } from "react";

export type Polled<T> = { data: T | null; error: string | null; loading: boolean };

// Fetch now, then every `everyMs`, keeping the last good value through errors
// (shown alongside it). A change of `key` refetches straight away.
export function usePoll<T>(load: (signal: AbortSignal) => Promise<T>, key: string, everyMs: number): Polled<T> {
  const [state, setState] = useState<Polled<T>>({ data: null, error: null, loading: true });
  const loader = useRef(load);
  loader.current = load;
  const lastKey = useRef(key);

  const run = useCallback(async (signal: AbortSignal) => {
    try {
      const data = await loader.current(signal);
      if (!signal.aborted) setState({ data, error: null, loading: false });
    } catch (err) {
      if (signal.aborted || (err as Error).name === "AbortError") return;
      setState((current) => ({ ...current, error: (err as Error).message || "Unavailable", loading: false }));
    }
  }, []);

  useEffect(() => {
    if (lastKey.current !== key) {
      lastKey.current = key;
      // A different request: drop the old data so it is never shown as the new one.
      setState({ data: null, error: null, loading: true });
    }
    let controller = new AbortController();
    void run(controller.signal);
    const timer = setInterval(() => {
      controller.abort();
      controller = new AbortController();
      void run(controller.signal);
    }, everyMs);
    return () => { clearInterval(timer); controller.abort(); };
  }, [key, everyMs, run]);

  return state;
}
