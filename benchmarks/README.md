# Harness comparison

The criterion, metrics, run budget, and grader versions for the current
comparison are fixed in [EVALUATION.md](EVALUATION.md), agreed before the runs
it governs. Read that first; this file is the record of what was run.

**Grader versions.** Every result below the "Initial pilot" heading was produced
by queue grader **version 1** (12 checks), TTL (11 checks), or ledger (12
checks). Queue grader version 2 adds two checks and is used from the paired
comparison onward. Version 1 scores are historical and are never recomputed or
compared against a version 2 score.

Run one trial at a time with the same Gemini 3.8 Flash medium model:

```sh
python3 benchmarks/run.py gcodex
python3 benchmarks/run.py agy
python3 benchmarks/run.py agy --task ledger
python3 benchmarks/run.py gcodex --task ledger
python3 benchmarks/run.py agy --task queue
python3 benchmarks/run.py gcodex --task queue
```

The whole agreed set, in the agreed order, then the summary:

```sh
python3 benchmarks/paired.py          # 6 pairs, serial, order alternated
python3 benchmarks/report.py          # tokens, gated on correctness
```

Every trial records the harness's own token usage in `result.json`. Nothing is
estimated; a trial whose harness reported no usage is marked unavailable and is
excluded from the token comparison rather than guessed at.

Each run receives the same specification, starter code, and three visible tests
in a new ignored directory inside this repository. The independent grader stays
outside that directory. It checks 11 behaviors, including a deterministic
300-operation comparison against a separate reference model. The trial has a
five-minute process budget; there are no automatic retries.

The ledger task adds a multi-file Python package and JSONL command-line program.
Its 12 independent checks cover exact large-number arithmetic, validation,
idempotent transaction IDs, input immutability, physical error line numbers,
stdin/files, and atomic output. Both harnesses receive the same specification
and visible tests. Alternating which harness runs first reduces order bias.

Score correctness and successful session completion first. Record elapsed time,
but do not infer a general speed advantage from one run. These runs compare the
installed, configured harnesses; their built-in instructions and tooling differ.
They are not a controlled model-only experiment.

## Initial pilot

| Harness | Independent checks | Session exit | Seconds |
| --- | --- | --- | --- |
| gcodex, before request-lifecycle fix | 11/11 | 1: local busy timeout | 69.82 |
| agy | 11/11 | 0 | 187.44 |
| gcodex, after request-lifecycle fix | 11/11 | 0 | 74.51 |

The first pilot does **not** establish that gcodex is better. It exposed a local
request-slot leak despite correct generated code. New cancellation/lifecycle
tests and a request-owned lease cleanup fix were added before the next trial.
That next run completed successfully with the same correctness score in 74.51
seconds. One task and one completed trial per harness do not establish general
superiority; more task types and repeated paired trials are still required.

Raw results and private transcripts live under `.benchmarks/`. The initial runs
are `gcodex-ttl-gl9admwx` and `agy-ttl-9en5gv2x`. Both used specification SHA-256
`8d7196c6068fc25acebe19fc3f94b071368e276bcffc6c1a0041931fae522281`.
The successful post-fix trial is `gcodex-ttl-918v7t9p` and uses that same hash.

## Multi-file ledger pilot

| Harness | Independent checks | Session exit | Seconds |
| --- | --- | --- | --- |
| agy | 12/12 | 0 | 201.91 |
| gcodex | 12/12 | 0 | 94.64 |

Both left the visible tests unchanged. The two runs used specification SHA-256
`b7441708743cec2fbc658dda73354c73b0d1b622b264d654b2c576787fe35b9c`.
Raw runs are `agy-ledger-xcfwed12` and `gcodex-ledger-zyzzl7zo`.
This adds a second task type, but it remains a pilot: correctness is tied on the
measured cases, and repeated trials are needed before attributing the timing
difference to the harness rather than run-to-run variation.

The queue task is a constructed stateful bug-fix fixture, not an external
real-world repository issue. Its 12 checks add SQLite restart persistence,
canonical idempotency, payload snapshots, stale acknowledgement handling,
lease boundaries, invalid-input atomicity, and six-process claim races.
The starter is confirmed to fail the independent grader before either trial.

## Repeated TTL pair

| Harness | Independent checks | Session exit | Seconds |
| --- | --- | --- | --- |
| gcodex | 11/11 | 0 | 79.61 |
| agy | 11/11 | 0 | 122.82 |

Both left the visible tests unchanged and used the original TTL specification
hash. Raw runs are `gcodex-ttl-xc44ml93` and `agy-ttl-4x42dxp5`.

Across the three post-fix comparisons (two TTL pairs and one ledger pair), each
harness completed all three sessions and passed all 34 independent checks
across those runs. These are repeated checks, not 34 distinct behaviors.
gcodex was faster in each comparison; correctness and completion were tied.
The agy TTL time changed from 187.44 to 122.82 seconds, illustrating substantial
run-to-run variation. This small, two-task sample supports the observed timing
result, not a claim of generally better code or proven long-duration stability.

## Paired token comparison — 2026-09-06

Run under [EVALUATION.md](EVALUATION.md): fewer tokens per task, gated on
correctness. Four of the six agreed pairs completed before the account's `agy`
quota was exhausted; the set was stopped rather than continue against a
quota-blocked endpoint.

| Pair | Task | Harness | Checks | Tokens | Input | Output | Seconds |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| 0 | ttl | gcodex | 11/11 | 258,172 | 256,018 | 2,154 | 20.50 |
| 0 | ttl | agy | 11/11 | 252,610 | 211,063 | 41,547 | 154.88 |
| 1 | ttl | gcodex | 11/11 | 311,547 | 308,803 | 2,744 | 39.69 |
| 1 | ttl | agy | 11/11 | 185,495 | 162,417 | 23,078 | 115.82 |
| 2 | ledger | gcodex | 12/12 | 1,410,423 | 1,404,670 | 5,753 | 131.55 |
| 2 | ledger | agy | quota error | 260,192 | 214,537 | 45,655 | 176.32 |
| 3 | ledger | gcodex | 12/12 | 964,896 | 960,003 | 4,893 | 97.29 |
| 3 | ledger | agy | quota error | 6,999 | 6,380 | 619 | 11.13 |

**agy won both comparable pairs**, by 2.2% and 40.5%. Both harnesses passed
every check in both, so correctness did not separate them. gcodex was faster in
both; speed is a secondary metric here and does not decide the criterion.

Both ledger `agy` runs returned `Individual quota reached`. Their grader scores
say nothing about code quality and are excluded by the correctness gate rather
than dropped quietly. No ledger or queue pair is comparable.

### What explains the gap

Input cost is dominated by the fixed prompt multiplied by the number of model
calls. Measured on a trivial prompt where history is negligible, gcodex sent
21,169 input tokens per call against agy's 13,349. Dividing each session's input
by that base implies 12.1 and 14.6 calls for gcodex and 15.8 and 12.2 for agy,
which matches the tool-call counts observed in the gcodex transcripts.

Capturing one real request against a local server (no Google traffic) showed
where gcodex's 21,169 went: of 114,643 bytes, 81,113 were the installed skill
catalog and ~21,000 were MCP tool schemas, against ~12,500 of task and coding
tools. Neither was used by any trial — no transcript contains a skill
invocation.

### These results describe a configuration that no longer exists

The launcher now trims both, cutting the per-request prompt from 21,169 tokens
to 6,672 with a keep-list of actively used skills, or 2,209 with none. That
change lands directly on the measured gap and was made *after* these pairs ran.
Re-run the full set before drawing any conclusion from the table above; treat it
as the pre-trim baseline, not as a verdict.
