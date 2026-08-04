# Crucible

**An execution-reliability layer for autonomous computational science.**

Autonomous-science systems — AI co-scientists, hypothesis generators, paper-writing
agents — share one unsolved dependency: when an experiment produces a bad result,
nothing in the loop can reliably answer **"did the experiment fail, or is the
hypothesis false?"**

Crucible answers that question. It takes an experiment specification, runs it
transactionally in a controlled environment, verifies every stage, and emits a
**calibrated verdict** with an evidence chain and a replayable reproducibility
certificate:

| Verdict | Meaning |
| --- | --- |
| `SUCCESS` | the hypothesis test ran correctly; the result is trustworthy |
| `RESULT_NEGATIVE` | the experiment ran correctly; the hypothesis is not supported |
| `EXECUTION_FAILURE(cause)` | infrastructure/code/env failure; the result is meaningless |
| `INCONCLUSIVE(reason)` | cannot certify either way; here is what is missing |

A false `SUCCESS` or false `RESULT_NEGATIVE` is the cardinal failure. Everything
here is organized around making those rare and measurable. The design: a frontier
LLM proposes plans and bindings; a deterministic harness enforces everything that
must not hallucinate.

This repository is the Stage-0 build. It is **not** an AI scientist — it sits
under one (or under a human researcher) as the component that certifies execution.

## Pipeline

```
paper.pdf ─┐
           ├─▶ Intake ──▶ Experiment Spec ──▶ Planner ──▶ Execution Plan
repo ──────┘   (extract claims,    (claims,      (LLM proposes,   (typed, ontology-
                baselines,          tolerances,   harness gates)    conformant)
                ground to code)     positive control)
                                                        │
                                                        ▼
                                            Validation gates  ──▶ (regenerate if invalid)
                                                        │
                                                        ▼
                                            Transactional Executor
                                    preconditions ▸ snapshot ▸ run ▸ ΔS ▸ verify ▸ commit
                                                        │
                                                        ▼
                                        Verdict Adjudicator ──▶ Verdict + Certificate
                                                                         │
                                                                         ▼
                                                                 crucible replay
```

## What's implemented (Stage 0)

- **Claims** (`crucible/claims/`) — the typed `Claim` object (subject/relation/
  comparator/margin, endpoint, dataset, split, representation) extracted from a
  paper or report, plus the **AcceptancePolicy** that states the constraints and
  requirements a verdict is measured against ("no acceptance policy, no
  verdict" — intake generates a domain default and marks it `generated`). A
  policy + claim compile deterministically into a closed set of
  **evidence requirements**. The **Procedure Compiler** locates the artifacts
  those checks need in a repo — split code, molecule lists, featurizer, metric
  function, baseline, preprocessing — scores each Present/Reconstructible/Missing,
  classifies scientific vs infrastructure files, and emits an **auditability
  score**. It also extracts the **run config** — entry point, the reproduce
  command sequence (run script > Makefile > README), the config files actually
  named on those commands, their decision-relevant parameters, and argparse
  choices — so the split the code *ran with* (`train_ratio: 0.5`) can be checked
  against the split the paper *claims*. Missing split code *and* molecule lists
  caps the claim at `INCONCLUSIVE(artifacts_unavailable)`; a split declared as
  config ratios with no pinned seed caps at `INCONCLUSIVE(split_not_regenerable)`.
  Static only: no execution, no network.
  An adapter maps a Claim down to an `ExperimentSpec` so the existing executor
  and adjudicator run unchanged.
- **Intake** (`crucible/intake/`) — PDF parsing (text, tables via pdfplumber,
  figure images via PyMuPDF), LLM extraction of claims + baselines with
  provenance and confidence (Claude/GPT, vision-capable), and **claim→repo
  grounding** that maps each claim to the concrete script/config/command that
  reproduces it. Offline heuristic + LLM paths.
- **Planner** (`crucible/planner/`) — deterministic repo analysis; a
  `TemplatePlanner` that emits an ontology-conformant, dataflow-valid plan (using
  grounded commands when available), and an `LLMPlanner` with a
  parse → validate → regenerate loop.
- **Validation** (`crucible/validation/`) — six gate families: ontology &
  verifiers, verifier arg-schemas, a closed predicate grammar + **dataflow**
  (every precondition must be established by an earlier step), smoke-before-full,
  control-before-eval, and static safety (network allowlist, credential
  exfiltration, budget). Findings carry **severity**; **waivers** override with a
  recorded justification; the full record is written to the trace and certificate.
- **Executor** (`crucible/executor/`) — transactional per-step lifecycle with
  checkpoints; validates every plan before any side effect. Scientific workload
  steps and their recovery commands cross a separate monitored-runner boundary;
  every consistency-checked command envelope is retained on the step, in the
  SQLite trace, and in the certificate.
- **Verifiers** (`crucible/verifiers/`) — hard verifier catalog with typed
  arg-schemas (`exit_code_zero`, `file_exists`, `imports_resolvable`).
- **Reproducibility** (`crucible/certificate/`) — build/save/load certificates,
  `replay` that re-runs from a certificate and diffs artifacts, and a
  **nondeterminism policy** (exempt / numeric-tolerance / normalize) that
  classifies divergence as expected vs a real reproduction failure.
- **Adjudicator** (`crucible/adjudicator/`) — the §8 decision procedure
  (positive control → execution integrity → scientific-path repairs → claim
  comparison) with a dependency-free Welch t-test.

Not yet built: recovery/diagnosis (Stage 1), the CORE-Bench eval harness, the
Docker environment path (written, untested), and Postgres/S3 storage (runs on
SQLite + local files for now).

Paper-analysis tooling includes a pinned CORE-Bench public-log downloader and
annotation-to-public-ID bridge. See [`data/corebench/README.md`](data/corebench/README.md)
for provenance, reproduction commands, and redistribution caveats.

The two development-only controlled provenance tasks are under
[`benchmarks/provenance/pilot/`](benchmarks/provenance/pilot/README.md). They include frozen
contracts, initial manifests, trusted construction labels, and a fixture self-check; they are
excluded from confirmatory paper results.

Two collectors implement the same monitored-runner interface:

- `crucible-command-envelope-v1` records the command, decoded-output digests,
  outcome, timing, and pre/post regular-file SHA-256 snapshots. It cannot prove
  process-tree quiescence, so snapshots remain incomplete and causal facets are
  explicitly unsupported.
- `crucible-linux-strace-v1` follows the Linux process tree with `strace -ff`,
  retains normalized exec/spawn/exit and file read/write/rename events, hashes
  every raw per-PID trace, and marks normal, losslessly parsed collections as
  causally captured. Timeouts, undecodable lines, `io_uring`, and other parser
  uncertainty downgrade causal facets to `incomplete`.

Both the configured `MonitoredRunner` and `strace` are part of the harness
trusted computing base. The Linux profile covers the successful syscalls named
in its retained `syscall_filter`; it is not a claim about unobserved kernel,
network, or hardware activity. Certificates still mark provenance adjudication
as `not_performed`: the event evidence is now available, but the policy gate that
decides whether those events make a scientific result admissible remains the
next increment.

## Install

```bash
uv sync                      # core
uv sync --extra intake       # + PDF parsing and LLM SDKs (pdfplumber, pymupdf, anthropic, openai)
# or: pip install -e ".[intake]"
```

Python 3.12+. For paper-driven intake, set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

## CLI

```bash
# Extract typed claims + acceptance policies from a paper/report, and report
# which required artifacts the repo actually has (static, no execution).
crucible claim --paper paper.pdf --repo ./repo
crucible claim --paper report.md --repo ./repo --llm --out claims.json --spec-out specs.json
crucible claim --repo ./repo            # auditability score only — can this ever be checked?

# Draft an experiment spec from a repo, optionally extracting claims from a paper.
crucible intake ./repo
crucible intake ./repo --paper paper.pdf --out spec.json

# Analyze a repo, generate a plan, and run it through the validation gates.
crucible plan ./repo

# Full pipeline: intake ▸ plan ▸ validate ▸ execute ▸ adjudicate ▸ verdict + certificate.
crucible submit ./repo --out cert.json

# Linux host: retain causal process and filesystem events for scientific steps.
crucible submit ./repo --runner linux-strace --out cert.json

# Reproduce a run byte-for-byte from its certificate (exit 0 iff reproduced).
crucible replay cert.json

# Harness-on vs harness-off comparison — the false-verdict table.
crucible bench
```

`crucible intake --paper` parses the PDF, extracts claims/baselines with an LLM,
grounds each claim to a reproduce command, and prints them with provenance and
confidence; the positive control reproduces the paper's own baseline number.
`crucible submit` runs the whole harness end-to-end and emits a replayable
certificate. `crucible bench` runs the built-in tasks through the real pipeline
(harness-on) and a bare-agent stand-in (harness-off) and prints the
false-verdict / decisiveness / correctness table.

## Status (Stage 0)

Implemented and tested end-to-end: **paper/repo → claims (+ acceptance policy +
artifact report) → intake (extract + ground) → plan → validate → execute →
verify → adjudicate → certify → replay → benchmark**, 263 passing tests. Runs
self-contained local repos on a subprocess runner.

Docker isolation is implemented (persistent container + bind-mount workspace, CPU
tested locally, GPU-ready — see below). Not yet: a real LLM harness-off arm,
Stage-1 recovery/diagnosis, `docker commit` checkpointing, and Postgres/S3 storage.

## Develop

```bash
uv run pytest -q             # 263 tests
uv run python -m examples.demo_local        # end-to-end: run ▸ verify ▸ adjudicate ▸ certify ▸ replay
uv run python -m examples.score_extraction  # scored claim/config extraction vs a real answer key
```

The demo runs a synthetic repo through the full loop with no Docker or network,
printing the trace, the adjudicated verdict, and a `REPRODUCED` check.

## Docker isolation

By default the executor runs on `LocalSubprocessRunner` (no isolation). The
Docker path runs each experiment in a **persistent container** (one per run) with
the workspace bind-mounted, so installed dependencies persist across steps while
the workspace stays host-visible for seeding, manifests, and replay. Network is
default-deny (`--network none`); GPUs attach on request. Checkpoints are host-dir
copies of the workspace (produced artifacts); `docker commit` layer capture is a
later refinement.

```bash
# 1. Build the base image (CPU; see docker/base.Dockerfile for the CUDA variant)
docker build -f docker/base.Dockerfile -t crucible/base:py3.12 .

# 2. Run a repo under container isolation
crucible submit ./repo --runner docker --image crucible/base:py3.12

# GPU (Linux host with the NVIDIA Container Toolkit):
crucible submit ./repo --runner docker --image crucible/base:cuda12.4-py3.12 --gpus all
```

macOS runs the CPU path fine; GPU passthrough (`--gpus`) requires a Linux host —
use a cloud GPU box for CUDA experiments. Docker integration tests live in
`tests/test_docker.py` and skip automatically when no daemon is present.

`--runner linux-strace` is a host-subprocess backend, not Docker isolation. It
requires Linux plus `strace` 6.6 or newer, with `-ff`, `-yy`, and
`--kill-on-exit`. Raw trace files are normalized and deleted after their SHA-256
digests and typed events are retained in the trace and certificate. Run the
collector inside an isolated Linux VM when the evaluated workload is untrusted.

Still a refinement, not yet done: `docker commit`-based checkpointing (so
rollback also undoes dependency installs) and an allowlist egress proxy (so the
`network_allowlist` gate becomes a live boundary rather than default-deny-all).

## Layout

```
crucible/
  schemas/        pydantic models — the API surface (spec, plan, verdict, certificate, policy)
  claims/         typed Claim + AcceptancePolicy, Procedure Compiler, ExperimentSpec adapter
  intake/         paper parsing, LLM extraction, claim→repo grounding
  planner/        repo analysis + template/LLM planners
  validation/     plan-validation gates, predicate grammar, dataflow
  executor/       transactional executor + state machine
  verifiers/      hard verifier catalog (arg-schemas)
  trace/          SQLite trace recorder + command/Linux event evidence
  adjudicator/    verdict decision procedure + stats
  certificate/    reproducibility certificates, replay, nondeterminism policy
  runners/        subprocess + Linux strace + docker runners
  envmgr/         local + docker environment managers
  pipeline.py     end-to-end submit orchestration
  benchmarks/     CORE-Bench tasks + harness-on/off arms
  eval/           scoring + the false-verdict table
  cli/            crucible claim | intake | plan | submit | replay | bench
docs/             the Stage-0 implementation plan
examples/         end-to-end demo
tests/            pytest suite
```

See `docs/crucible_stage0_implementation_plan.md` for the full plan.
