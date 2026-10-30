# Desk-note golden set

The yardstick for the Phase 1 prompt rewrite (`docs/PLAN.md` §6–§7). These
`desk:v4` notes are the **historical** baseline. The acceptance test for the new
prompt (`desk:v9`) compares it with the current prompt (`desk:v8`) on the same
captured facts; that needs the facts capture planned for Phase 1, so those
cases will live in their own folders (`v8/`, `v9/`) next to `v4/`.

## The notes

Six stored `desk:v4` notes in `v4/`, copied verbatim from `data/desk.sqlite3`
on 2026-10-28 (a read-only backup, so the live database was not opened for writing):

| File | Kind | Writer | Why it is here |
| --- | --- | --- | --- |
| `premarket_2026-10-27.json` | pre-market | Opus 5.5 | A figure missing at write time (durable goods), yields, an auction |
| `review_2026-10-19.json` | weekly review | Opus 5.5 | The only stored review |
| `release_20261022T1230_INITIAL_CLAIMS.json` | release | Opus 5.5 | A real figure with a verdict |
| `release_20261027T1230_DURABLE_GOODS.json` | release | DeepSeek V4.1 Flash | Figures that never arrived |
| `move_20261027T11.json` | move | DeepSeek V4.1 Flash | Cause attribution for a large move |
| `ask_20261024T074748.json` | ask | DeepSeek V4.1 Flash | A Saturday: closed vs stale |

There is no Fed note yet. The Oct 28 FOMC note (written by `desk:v4`, the live
version until the Oct 29 restart) should be added once it exists.
`lesson_history.json` holds every stored note's Learn box, for the
repeated-lesson check.

## What is and is not stored with a note

Stored: `figures`, `moves`, `headlines`, `upcoming`, `rates`, the draft, model
and cost. **Not stored:** the rest of what the writer saw: the `markets_today`
quote strip (levels and today's changes), the session text, breaking alerts,
and (from `desk:v8`) price location, normal move size and macro trends. So a
stored note cannot be fully re-checked: numbers such as "NQ is up 0.58% today"
come from the quote strip and are reported below as *unverifiable*, not wrong.
Phase 1 should store the full facts with each new note.

## Mechanical rules (scored by `tests/desk/golden.py`)

Run `python -m tests.desk.golden` from the repository root. Counts per note;
`baseline.json` holds the `desk:v4` numbers and `tests/desk/test_golden_notes.py`
pins them.

| Rule (§7) | Check |
| --- | --- |
| No direction word next to a signed number | "rose +0.88%" (say "rose 0.88%") |
| Closed, stale and unavailable are different | a sentence calling a shut market "stale" |
| Missing from the facts is not absent from the world | "there was no event/release/data" without "supplied"/"in the feed" |
| Basis point wording | "1bp is 0.01%" or a yield change in small percents (say "0.01 percentage point") |
| Sign accuracy | a measured move quoted with the opposite direction word |
| Don't repeat lessons | the note's own Learn text teaches the same term as one of the 5 notes written before it (frozen `lesson_history.json`) |
| Length targets | words over every draft field, over **or under**: pre-market/review 200–260; move/release/Fed/earnings/auction 140–180; ask 120–160; primer exempt |
| Number accuracy (partial) | numbers not found in the stored facts; an upper bound, see above |

### desk:v4 baseline

| Note | Words (target) | Signed | Closed as stale | Absent | bp as % | Sign errors | Repeat | Unverifiable | Issues |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pre-market Oct 27 (Opus) | 336 (200–260) | 10 | 0 | 0 | 0 | 0 | auction | 11 | 12 |
| review Oct 19 (Opus) | 347 (200–260) | 0 | 0 | 0 | 1 | 0 | – | 12 | 2 |
| claims Oct 22 (Opus) | 363 (140–180) | 11 | 0 | 0 | 0 | 0 | – | 6 | 12 |
| durable goods Oct 27 (DeepSeek) | 308 (140–180) | 0 | 0 | 0 | 0 | 0 | – | 6 | 1 |
| move Oct 27 (DeepSeek) | 310 (140–180) | 0 | 0 | 0 | 0 | 0 | vix | 10 | 2 |
| ask Oct 24 (DeepSeek) | 296 (120–160) | 10 | 3 | 0 | 0 | 0 | – | 13 | 14 |

Across all 32 stored notes: 19 explain basis points somewhere and 5 make it the
Learn box (checked 2026-10-28). Every note is over its §7 length target.

## Judgment rules (scored by hand, 2026-10-28)

0 = breaks the rule, 1 = partly, 2 = follows it. A new prompt is scored the
same way by two reviewers without seeing which version wrote which note.

| Note | Cause honesty (no ranking, "not confirmed") | Windows kept apart | Missing vs absent | Plain English | Notes |
| --- | --- | --- | --- | --- | --- |
| pre-market Oct 27 | 2 | 2 | 2 | 2 | "The actual numbers are not known yet"; attributes Fed talk to "headlines" |
| review Oct 19 | 2 | 2 | 2 | 2 | Says the Tesla window overlaps Thursday's slide instead of merging them |
| claims Oct 22 | 2 | 2 | 2 | 2 | "Most of NQ's drop came outside this window, and the cause is unclear" |
| durable goods Oct 27 | 2 | 2 | 2 | 2 | "Not in our data, so we cannot say whether it beat or missed" |
| move Oct 27 | 1 | 2 | 2 | 2 | Ranks oil as "the closest candidate"; the v5 move task forbids ranking |
| ask Oct 24 | 2 | 2 | 1 | 1 | Calls weekend prices "stale" three times; "a split tape" is jargon |

**Reading:** the facts are handled honestly; the problems are style rules
(signed numbers, length, repeated lessons) and closed-vs-stale wording, which
the `desk:v9` rules in §7 target directly.

**Known limits of the checker** (to fix before it gates paid candidates, per
the Phase 1 plan): sign checks match a magnitude across all moves, not per
instrument; numbers are checked only against stored facts; the prompt's own
example numbers count as allowed in the production number check
(`core/desk/writer.py:170`).
