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
  checkpoints; validates every plan before any side effect.
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

## Install

```bash
uv sync                      # core
uv sync --extra intake       # + PDF parsing and LLM SDKs (pdfplumber, pymupdf, anthropic, openai)
# or: pip install -e ".[intake]"
```

Python 3.12+. For paper-driven intake, set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

## CLI

```bash
# Draft an experiment spec from a repo, optionally extracting claims from a paper.
crucible intake ./repo
crucible intake ./repo --paper paper.pdf --out spec.json

# Analyze a repo, generate a plan, and run it through the validation gates.
crucible plan ./repo

# Reproduce a run byte-for-byte from its certificate (exit 0 iff reproduced).
crucible replay certificate.json
```

`crucible intake --paper` parses the PDF, extracts claims/baselines with an LLM,
grounds each claim to a reproduce command, and prints them with provenance and
confidence; the positive control reproduces the paper's own baseline number.

## Develop

```bash
uv run pytest -q             # 78 tests
uv run python -m examples.demo_local   # end-to-end: run ▸ verify ▸ adjudicate ▸ certify ▸ replay
```

The demo runs a synthetic repo through the full loop with no Docker or network,
printing the trace, the adjudicated verdict, and a `REPRODUCED` check.

## Layout

```
crucible/
  schemas/        pydantic models — the API surface (spec, plan, verdict, certificate, policy)
  intake/         paper parsing, LLM extraction, claim→repo grounding
  planner/        repo analysis + template/LLM planners
  validation/     plan-validation gates, predicate grammar, dataflow
  executor/       transactional executor + state machine
  verifiers/      hard verifier catalog (arg-schemas)
  trace/          SQLite trace recorder
  adjudicator/    verdict decision procedure + stats
  certificate/    reproducibility certificates, replay, nondeterminism policy
  runners/        subprocess + docker runners
  envmgr/         local + docker environment managers
  cli/            crucible intake | plan | replay
docs/             the Stage-0 implementation plan
examples/         end-to-end demo
tests/            pytest suite
```

See `docs/crucible_stage0_implementation_plan.md` for the full plan.
