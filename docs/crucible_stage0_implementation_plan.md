# Crucible — Stage 0 Implementation Plan

**Scope of this plan:** the first milestone from the Crucible design (§18 "First milestone, precisely" + §14 "Stage 0 — Skeleton"). Six weeks, solo, ending in **one number** and a go / re-scope / kill decision.

**The one number:** the false-verdict delta between *harness-on* (full Crucible-Stage-0) and *harness-off* (bare frontier LLM agent, same model) on a stratified slice of CORE-Bench. If the harness does not measurably reduce false verdicts, the execution layer is not the business.

---

## 1. Objective and definition of done

Build the deterministic skeleton that runs a computational experiment transactionally, verifies every step with hard verifiers, records a fully replayable trace, and — by week 6 — emits a minimal calibrated verdict gated on positive controls.

**Done when all three exist:**

1. A **harness-on vs harness-off table**: false-verdict rate and task-completion on 30 CORE-Bench tasks stratified easy/medium/hard, identical LLM on both sides.
2. **Three reproduction reports**, each a verdict object with a full evidence chain and a `crucible replay` command that reproduces it.
3. A **decision**: continue to Stage 1, re-scope to certification-only, or kill.

Everything in the design past §14 Stage 1 stays frozen until that table exists.

---

## 2. In / out of scope for the six weeks

**In scope**

- Core schemas (Experiment Spec, Execution Plan/Step, Verdict, Reproducibility Certificate) as the API surface.
- Environment Manager, Transactional Executor, Trace Recorder, hard verifiers, Planner + plan validation, repo-only intake.
- Minimal Verdict Adjudicator: positive controls + hard verifiers only.
- Failure taxonomy skeleton + ~10 highest-base-rate playbooks (week 5–6, thin).
- CLI-first surface (`crucible submit`, `crucible replay`).

**Out of scope (deferred to Stage 1+)**

- LLM diagnoser, information-gain probe ordering, the full 25-seed playbook library and lifecycle promotion.
- Statistical/soft verifiers beyond what a positive control needs, verifier auto-generation, calibration tracking.
- Scientific-path repair approval workflow (classification is *recorded* but no repairs run on the scientific path).
- REST API, UI, webhooks, multi-node, GPU autoscaling beyond a single Runner.
- Any trained model. Stage 0 has zero learned components.

---

## 3. Architecture slice being built

```
Intake (repo-only → spec draft, generates positive control)
        │
        ▼
Planner (frontier LLM, structured output → typed Execution Plan)
        │
        ▼   plan validation gates (ontology, verifiers, smoke<full, control<evaluate, static safety)
Transactional Executor
   per step: check preconditions → snapshot → execute → capture ΔS → run verifier
              → commit  |  (Stage 0: fail → stop & report)
        │
        ├── Environment Manager (Docker layers = checkpoints)
        ├── Trace Recorder (structured, timestamped, replayable)
        └── Hard verifiers
        │
        ▼
Verdict Adjudicator (week 5–6: positive control + integrity only)
        │
        ▼
Verdict + Reproducibility Certificate
```

Diagnosis and recovery are stubbed: a failed step stops the run and reports the deepest verified failure. Recovery is added in Stage 1.

---

## 4. Tech stack (from §15)

- **Language:** Python 3.12, strict typing. `pydantic` models for every §4 object — *the schemas are the API*.
- **Isolation:** Docker + BuildKit. Layer snapshots as checkpoints; rollback = layer restore. Prebuilt base-image matrix over (CUDA version × Python version). `uv` for all Python dependency operations (fast, deterministic).
- **Compute:** local Docker (CPU) plus Modal or RunPod behind a single `Runner` interface (GPU); spot with checkpoint-resume.
- **LLM:** Claude/GPT via API with structured outputs; every call logged to the trace; model per role configurable (planner: frontier; extraction: mid; narrative: cheap).
- **Store:** Postgres — one table per §4 object plus failures, repairs, probes. S3-compatible object store for traces, artifacts, container digests. No graph DB, no vector DB yet.
- **Surface:** CLI first (`crucible ...`). The CLI forces the schemas to be right before any product surface exists.

### Suggested repo layout

```
crucible/
  schemas/        # pydantic: spec, plan, step, verdict, certificate, playbook
  intake/         # repo/paper → spec draft, positive-control generation
  planner/        # LLM planner + structured-output enforcement
  validation/     # plan gates (ontology, verifier presence, ordering, safety)
  envmgr/         # Docker/BuildKit, base-image matrix, layer snapshot/restore, uv
  executor/       # transactional state machine, checkpoints, step lifecycle
  verifiers/      # hard verifier catalog
  trace/          # structured recorder → Postgres + S3
  adjudicator/    # decision procedure (control + integrity)
  runners/        # local + Modal/RunPod behind one interface
  cli/            # submit, replay, report
  benchmarks/     # CORE-Bench harness, stratified sampling, scoring
  eval/           # harness-on/off table generation
```

---

## 5. Component task breakdown

### 5.1 Core schemas (foundation — build first)

- Implement `ExperimentSpec` (hypothesis, claims_under_test with metric/comparison/tolerance/seeds, positive_controls, budget, scale_policy, environment_constraints).
- Implement `ExecutionPlan` + `Step` (type from fixed ontology, preconditions, action, postconditions, verifier ref, rollback, budget, irreversible flag).
- Implement `Verdict` (status enum: SUCCESS / EXECUTION_FAILURE(cause) / RESULT_NEGATIVE / INCONCLUSIVE(reason); evidence; provenance with trace_id, container_digest, replay_command).
- Implement `ReproducibilityCertificate` (spec + plan + container digest + pinned inputs + trace + verdict).
- Freeze the **step ontology v1**: `acquire_source, inspect_project, build_environment, provision_dependencies, acquire_data, configure, smoke_run, positive_control_run, full_run, collect_artifacts, evaluate_claims`.

### 5.2 Environment Manager

- Docker as the unit of isolation; each mutating step commits a layer → checkpoint; rollback = layer restore.
- Record filesystem diffs between layers as state deltas (ΔS).
- Build the deterministic (CUDA × Python) base-image matrix; `uv` for dependency operations.
- Expose snapshot / restore / diff to the executor.

### 5.3 Transactional Executor

- Per-step lifecycle: `check preconditions → snapshot → execute → capture ΔS → run verifier → commit | (Stage 0: stop & report)`.
- State machine: PENDING → READY → RUNNING → VERIFYING → {SUCCEEDED | FAILED}.
- Enforce hard budgets (wall time, cost, per-step timeout/retries) — kill, checkpoint, report rather than overrun.
- `Runner` abstraction so the same executor drives local CPU and remote GPU.

### 5.4 Trace Recorder

- Capture, structured and timestamped: commands, exit codes, full compressed stdout/stderr, per-step filesystem deltas, package-manager events, GPU/memory telemetry, network requests, and **all LLM calls with prompts and outputs** (planner is part of the experiment record).
- Write to Postgres (indexed events) + S3 (blobs); this is also the observability system — do not build a second one.
- Implement `crucible replay <trace_id>` reproducing the run to a byte-comparable result or a documented list of nondeterminism sources.

### 5.5 Hard verifiers

- Catalog v1: `exit_code_zero`, `file_exists / artifact_exists(path, min_size)`, `checksum_matches`, `imports_resolvable(top_level_packages)`, JSON/schema validity, expected-process-running.
- Verifiers are versioned objects, not inline assertions. A step with no verifier does not execute.

### 5.6 Planner + plan validation

- Frontier LLM with three inputs: the spec, an automated repo analysis (file tree, manifests, README, entry points, CI configs), and the step ontology + schemas.
- Structured-output (JSON schema) enforcement: a plan either parses+validates or is regenerated.
- Validation gates before any execution:
  1. every step type in ontology; every step has a verifier; irreversible steps flagged;
  2. `smoke_run` precedes `full_run` when `scale_policy.smoke_first`;
  3. `positive_control_run` precedes `evaluate_claims`;
  4. static safety: no network calls outside allowlist, no credential-exfiltration patterns, resource requests within budget.

### 5.7 Intake (repo-only)

- Repo URL (optionally paper) → spec draft: claims table, entry points, and a **generated positive control** (usually "reproduce the paper's own baseline number").
- Rule: **no positive control, no verdict** — intake must always produce one.

### 5.8 Minimal Verdict Adjudicator (week 5–6)

- Decision procedure per claim:
  1. positive control passed? no → INCONCLUSIVE(control_failed), stop;
  2. all steps SUCCEEDED with gating verifiers passed? no → EXECUTION_FAILURE(deepest cause), stop;
  3. (Stage 0) no scientific-path repairs exist yet → proceed;
  4. compare observed metric to spec tolerance across seeds → SUCCESS or RESULT_NEGATIVE.
- INCONCLUSIVE is the default under uncertainty.

### 5.9 Taxonomy + thin playbooks (week 5–6)

- Encode the failure taxonomy skeleton (environment / dependency / resource / configuration / input / implementation).
- Author ~10 highest-base-rate playbooks as **static, parameterized repairs** on the infrastructure path only (e.g. `cuda_torch_repin`, python-version pin, missing-system-lib install). No LLM diagnosis, no promotion lifecycle yet — that is Stage 1.

---

## 6. Week-by-week schedule

| Weeks | Build | Target / gate |
|---|---|---|
| **1–2** | Schemas + Environment Manager (Docker + layer checkpoints) + Transactional Executor + Trace Recorder + hard verifiers. | **5 CORE-Bench tasks** manually specced, run end-to-end, every run replayable. |
| **3** | Planner + plan validation + repo-only intake. | **15 CORE-Bench-easy tasks** end-to-end from repo URL, plans auto-generated and validated. |
| **4** | Kill-signal experiment harness + stratified sampling. | **Bare frontier agent vs Crucible-Stage-0** on 30 CORE-Bench tasks (easy/medium/hard). If the bare agent wins everywhere → stop and re-scope per §14 Stage 0. |
| **5–6** | Failure taxonomy + 10 highest-base-rate playbooks + minimal adjudicator (positive controls + hard verifiers). | Re-run the 30 tasks. Produce the **harness-on/off table** + **3 reproduction reports** with evidence chains. |

**Final deliverable:** a table, three verdict reports, and a decision — continue, re-scope to certification, or kill.

---

## 7. Benchmark and data setup

- **Primary benchmark: CORE-Bench** (270 tasks, 90 papers, 3 disciplines) — it directly measures install/execute/interpret. Report harness-on vs harness-off with an *identical* LLM.
- Select and pin a stratified 30-task slice (easy/medium/hard); keep the sampling script deterministic and versioned.
- Pin every input: repo commit, dataset checksums, container digest. Use mirrors with checksum verification when original links are dead (record as a caveat).
- Contamination note: CORE-Bench is likely in model training data, so harness-off looks better than reality — treat absolute numbers cautiously; the *delta* is the signal. (A post-cutoff internal benchmark is a Stage 1 item.)

---

## 8. The week-4 kill-signal experiment (design)

- **Both arms use the same frontier model.** Harness-off = the model as a bare agent planning and executing directly; harness-on = Crucible-Stage-0.
- 30 tasks stratified easy/medium/hard.
- Primary comparison: **false-verdict rate** (a run the arm reports as done/succeeded that is actually wrong) and task completion.
- **Kill criterion (from §17):** if harness-on vs harness-off shows **< 2× false-verdict delta** on the hard stratum with the current frontier model, the execution layer is not the business — pivot to verdict certification or stop.

---

## 9. Metrics (in priority order, §12.3)

1. **False-verdict rate** — `P(verdict ∈ {SUCCESS, RESULT_NEGATIVE} ∧ verdict wrong)`. The cardinal metric; target < 2%, measured per class. A false RESULT_NEGATIVE is fabricated negative science; a false SUCCESS is fabricated positive science.
2. **Decisiveness** — fraction of experiments ending in a definitive (non-INCONCLUSIVE) verdict. Guards against INCONCLUSIVE collapse.
3. Task completion / reproduction rate on the slice.
4. Replayability — every run reproduces from its certificate.

---

## 10. Risks specific to the milestone

- **INCONCLUSIVE collapse** — a control-conservative adjudicator says "don't know" too often. Track decisiveness from day one; every INCONCLUSIVE must carry an actionable reason.
- **Benchmark contamination** — report the delta, not absolute numbers; flag CORE-Bench vintage.
- **Cost** — GPU reproduction is expensive. Lean on smoke-first, cheap positive controls, and easy-stratum tasks for weeks 1–3; reserve GPU budget for the week-4/6 runs.
- **Solo bandwidth** — the design is 6+ months to a pitchable result; this milestone is the 4-week kill signal plus a 6-week fundable artifact. Do not build anything past Stage 1 before this table exists.

---

## 11. Exit decision (end of week 6)

- **Continue to Stage 1** if the harness-on/off table shows a meaningful false-verdict reduction (≥ 2× on hard) and decisiveness is trending usable.
- **Re-scope to certification** if execution value is thin but the verdict/reproducibility-certificate layer still holds trust value.
- **Kill** if the bare agent matches the harness on false verdicts across strata.
