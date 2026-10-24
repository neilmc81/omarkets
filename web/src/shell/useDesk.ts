import { useCallback, useEffect, useState } from "react";
import { fetchHealth } from "../api/client";
import type { HealthResponse } from "../api/schemas";
import { fetchDesk, type Desk } from "../today/api";

const POLL_MS = 30_000;

const message = (err: unknown, fallback: string) => (err as Error).message || fallback;
const aborted = (err: unknown) => (err as Error).name === "AbortError";

// The desk and the core's health, polled together for every page: Today
// reads the desk, the header reads both for the feeds dot.
export function useDesk() {
  const [desk, setDesk] = useState<Desk | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    await Promise.all([
      fetchDesk(signal).then((next) => { setDesk(next); setError(null); })
        .catch((err) => { if (!aborted(err)) setError(message(err, "Omarkets unavailable")); }),
      fetchHealth(signal).then((next) => { setHealth(next); setHealthError(null); })
        .catch((err) => { if (!aborted(err)) setHealthError(message(err, "Health check failed")); }),
    ]);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    const timer = setInterval(() => void load(), POLL_MS);
    return () => { controller.abort(); clearInterval(timer); };
  }, [load]);

  return { desk, error, health, healthError, reload: load };
}
