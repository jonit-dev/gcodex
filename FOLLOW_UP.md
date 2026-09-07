# gcodex follow-up tasks

Status: updated 2026-09-06 after a second working session. Tasks 1-3 and 5 are
done. Task 4 is **blocked until 2026-09-13** by an exhausted `agy` quota, not by
anything in this repository. Nothing is committed; the working tree holds all of
it.

The original goal — showing generally better code quality than `agy` — is still
**not** established, and the evidence now available argues for a narrower claim.

## What changed this session

**`/goal` fixed.** The gcodex profile shipped `goals = false`, and Codex gates
the `/goal` slash command on that feature flag, so the command silently did not
exist under `gcodex` while plain `codex` had it. Now `goals = true`, verified in
a real TUI, with a regression test and a README troubleshooting entry.

**Success criterion replaced.** "Better" now means **fewer tokens per task at
comparable correctness**, agreed before the runs it governs and recorded in
[benchmarks/EVALUATION.md](benchmarks/EVALUATION.md) with metrics, the
correctness gate, the run budget, and the reporting rules.

**Queue grader version 2.** The two post-hoc defects are now real acceptance
cases (14 checks). Validated three ways: `benchmarks/reference_queue.py` passes
14/14, the starter fails, and each new case fails only the saved run carrying
its defect — the gcodex run on the pre-existing-schema case, the agy run on the
circular payload. `tests/test_benchmark_tasks.py` re-proves both against
deliberately broken copies of the reference, so the cases cannot silently rot
into checks that can never fail. Version 1 scores stay historical and are never
recomputed.

**Token accounting.** `benchmarks/run.py` records each harness's own reported
usage — Codex `turn.completed` under `exec --json`, agy's result object under
`--output-format json`. Nothing is estimated; a run whose harness reported no
usage is marked unavailable rather than guessed at. `benchmarks/report.py`
summarizes pairs and applies the correctness gate.

**Prompt trimming — the largest finding.** `codex --profile` layers config
files, so the profile could not remove what `~/.codex/config.toml` declares.
Every installed skill and MCP server was shipped on every model call. Measured
by pointing Codex at a local capture server (no Google traffic): of a
114,643-byte request, 81,113 bytes were the skill catalog and ~21,000 were MCP
tool schemas — ~87% overhead, resent 12-15 times per task. The launcher now
trims both, driven by a user-editable keep-list, with `GCODEX_SKILLS=1` and
`GCODEX_MCP=1` opt-outs. Per-request input tokens on this machine:

| Configuration | Input tokens |
| --- | ---: |
| Before | 21,169 |
| Keep-list of 24 actively-used skills | 6,672 |
| Skills dropped entirely | 2,209 |

**Verification guidance.** `developer_instructions` in the profile now asks for
existing tests to be run first and for changes to persisted formats to be
verified against data built by the *previous* version. It is task-neutral by
design and tested to stay that way. Confirmed by capture to **append** to
Codex's developer message: skills, AGENTS.md and tools all survive; cost is 643
characters. Its behavioural effect is **unverified** — that needs task 4.

## Comparison results, and why they do not settle the question

Four pairs ran before the quota ended the set. Grader version 1 for `ttl`.

| Pair | Task | Harness | Checks | Tokens | Seconds |
| --- | --- | --- | --- | ---: | ---: |
| 0 | ttl | gcodex | 11/11 | 258,172 | 20.50 |
| 0 | ttl | agy | 11/11 | 252,610 | 154.88 |
| 1 | ttl | gcodex | 11/11 | 311,547 | 39.69 |
| 1 | ttl | agy | 11/11 | 185,495 | 115.82 |
| 2 | ledger | gcodex | 12/12 | 1,410,423 | 131.55 |
| 2 | ledger | agy | quota error | 260,192 | 176.32 |
| 3 | ledger | gcodex | 12/12 | 964,896 | 97.29 |
| 3 | ledger | agy | quota error | 6,999 | 11.13 |

Only the two `ttl` pairs are comparable. **agy used fewer tokens in both** — 2.2%
and 40.5%. Both harnesses passed every check in both pairs, so on the agreed
criterion agy won the only two valid pairs. gcodex was faster in both, which is
a secondary metric and not the criterion.

Both ledger `agy` runs failed with `Individual quota reached`; their grader
scores are quota casualties and carry no information about code quality. They
are excluded by the correctness gate, not silently dropped.

The shape of the token gap is understood: input cost is dominated by the fixed
prompt times the number of model calls. gcodex sent ~21,200 tokens per call
against agy's ~13,300, and the implied call counts match the observed tool-call
counts closely. **The prompt trimming above lands squarely on that gap and has
not been measured in a paired trial yet.** Re-running task 4 after the trim is
the single highest-value remaining experiment.

## Quota, and what is actually limited

`agy -p "/quota"` reports `Gemini Models  Weekly Limit Remaining  0%`, resetting
**2026-09-13T23:00:07Z**. gcodex was unaffected: it completed two full ledger
sessions between two quota-blocked `agy` runs and has answered every request
since. The two reach Gemini through different OAuth clients — the gateway uses
the Antigravity IDE consumer client — and are metered separately.

`agy` is otherwise only needed at install time, as a *file* that `setup.sh`
parses the OAuth client out of. gcodex never invokes it at runtime. The README
health check was corrected accordingly: an `agy` failure no longer implies a
gcodex failure.

Where gcodex's own ceiling sits is **unknown** and was not established.

## Remaining work, in order

1. **Re-run task 4 after the trim — blocked until 2026-09-13.** Same six pairs,
   same budget, same graders, queue on grader version 2. The prompt trim changed
   gcodex's dominant cost after the only valid pairs were recorded, so those two
   results describe a configuration that no longer exists. Acceptance: six pairs
   under the correctness gate, losses and ties reported alongside wins.

2. **Verify the verification guidance actually changes behaviour.** It is
   installed and proven non-destructive, but no trial has shown it preventing a
   backward-compatibility defect. Acceptance: on a task whose subject it does not
   name, a fresh run preserves old data/schema behaviour, without a correctness
   regression or an unacceptable token increase.

3. **Establish where gcodex's quota ceiling sits.** Read-only inspection and
   documentation first; do not probe by exhausting it.

4. **Decide what belongs in the public repository.** `github.com/jonit-dev/gcodex`
   is public. A `commit -A` would add 25 files including `benchmarks/` and this
   handoff note. Two personal paths were already removed from `STABILITY.md`;
   the tree is now scanned clean of emails, home paths and account data, and
   `.benchmarks/` plus `__pycache__/` are ignored. Committing is deliberately
   left undone.

## Resume commands

```sh
python3 -B -m unittest discover -s tests -v     # 68 tests
python3 patch-gateway.py --check
python3 benchmarks/report.py                    # paired results, gated
python3 benchmarks/paired.py                    # the full set, when quota allows
```

Reproduce the two post-hoc findings without Google requests:

```sh
python3 benchmarks/probe_queue_edges.py \
  .benchmarks/agy-queue-gev8h12b \
  .benchmarks/gcodex-queue-jw8vuuke
```

Raw benchmark artifacts are local and ignored by Git. Preserve them while
reviewing. Existing repository changes are uncommitted; do not reset or
overwrite them. If a worktree is needed, use an ignored directory under this
repository's `.worktrees/`, per the project instructions.

Next action (under 2 minutes): run `python3 benchmarks/report.py` to see the
gated comparison as it stands.
