import { useEffect, useState } from "react";
import { rateNote, type Rating } from "./api";

const CHOICES: { value: Rating; label: string; text: string }[] = [
  { value: "useful", label: "Mark this note useful", text: "Useful" },
  { value: "not_useful", label: "Mark this note not useful", text: "Not useful" },
];

// Two small buttons. The page only sends the mark and shows what the core stored
// (its answer); pressing the lit button again clears the mark.
export function RateNote({ noteId, rating }: { noteId: string; rating: Rating | null }) {
  const [stored, setStored] = useState<Rating | null>(rating);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => setStored(rating), [rating]);

  const press = async (value: Rating) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const state = await rateNote(noteId, stored === value ? null : value);
      setStored(state.rating);
    } catch (err) {
      setError((err as Error).message || "Could not save");
    } finally {
      setBusy(false);
    }
  };

  return <div className="t-rate" role="group" aria-label="Was this note useful?">
    {CHOICES.map((choice) => <button key={choice.value} type="button" aria-label={choice.label}
      aria-pressed={stored === choice.value} disabled={busy} onClick={() => void press(choice.value)}>{choice.text}</button>)}
    {error && <span className="t-rate-error" role="alert">{error}</span>}
  </div>;
}
