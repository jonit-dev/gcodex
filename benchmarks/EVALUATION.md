# Evaluation plan

Agreed 2026-09-06, before running the comparison it governs. Fixed in advance so
the result cannot be chosen after the fact.

## Success criterion

**Fewer tokens consumed per task, at comparable correctness.**

- **Primary metric.** Total tokens for one task, `input + output`, as reported by
  the harness itself. Lower wins.
- **Correctness gate.** A trial counts only if the session exits cleanly, leaves
  the visible tests unmodified, and passes the independent grader. A harness that
  spends fewer tokens and fails the grader has not won; report it as a loss.
  "Mostly correct" is the bar: a single failed check is reported, not silently
  tolerated, and never traded for a token saving.
- **Secondary, reported not optimized.** Wall-clock seconds, turns, and cached
  and reasoning token counts. These are context for the token number.

## Token accounting

Neither figure is estimated. Codex emits `turn.completed` usage under
`exec --json`; agy emits a result object with `usage` under
`--output-format json`. `benchmarks/run.py` records both raw and normalized.

Known limits of the comparison, which is between two configured harnesses and
not a model-only experiment:

- Each harness counts its own history resends inside its input total. That is the
  intended quantity — an agentic loop that resends more history costs more.
- Cached input and reasoning tokens are included in the totals and also reported
  separately. They are not subtracted; no billing model is assumed.
- The two harnesses ship different system prompts and different tool surfaces.
  That difference is part of what is being measured, not noise to remove.

## Run budget

Fixed at **6 paired trials**: three tasks (`ttl`, `ledger`, `queue`) times two
pairs each, with the run order alternated within every task so ordering, cache
warmth, and drift cannot systematically favour one side. Trials are serial: the
gateway permits one in-flight request. No trial is retried, and no task is added
after seeing a result.

## Graders

Queue trials are graded by **grader version 2** (14 checks). Version 1's 12-check
scores in `README.md` are historical and are never recomputed or compared across
versions. `ttl` (11 checks) and `ledger` (12 checks) are unchanged.

## Reporting

Report every pair, including losses and ties. State the token totals, the grader
result, and the elapsed time for both harnesses in each pair. Six pairs on
constructed tasks cannot establish general superiority; the claim this evidence
can support is about token cost on these tasks, at these settings, on this day.
