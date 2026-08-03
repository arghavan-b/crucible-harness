# NeurIPS 2026 "Who Verifies the Agents?" — submission strategy

**Deadline:** Aug 29, 2026 (AoE) · 4–9 pages, NeurIPS 2026 template · double-blind ·
non-archival · dual submission allowed ·
[OpenReview](https://openreview.net/group?id=NeurIPS.cc/2026/Workshop/Verify-Agents)

---

## 1. The finding that changes the plan

The original plan was: run Crucible over real CORE-Bench capsules, report how often
published research code fails to execute, and show the harness catches it.

**That paper is no longer viable.** Nadgir et al., *Life After Benchmark Saturation:
A Case Study of CORE-Bench* ([arXiv:2606.26158](https://arxiv.org/abs/2606.26158)),
reports that CORE-Bench Hard is saturated: CORE-Agent with Opus 4.6 reaches 100%,
and four more agents tie at 97.4%. Two consequences:

1. CORE-Bench capsules are pre-screened to be locally reproducible by construction,
   so "published code doesn't run" cannot be measured on this benchmark. Any
   `EXECUTION_FAILURE` Crucible emits on a CORE-Bench capsule is more likely a
   statement about our environment than about the science.
2. "Can an agent reproduce a paper" is answered. Repeating it adds nothing.

**But the same paper opens a better problem, and hands over the labeled data to
attack it.** Their code, data, and logs are at
`github.com/nnadgi01/corebench-analysis`.

---

## 2. The gap

Three of their results describe a verification failure Crucible is built to fix.

### 2a. Answer-matching verification is shortcut-exploitable

Log analysis of CORE-Bench Hard surfaced **15 task-level errors and 20 tasks with
exploitable shortcuts**. The shortcut rubric is explicit:

> The agent is able to obtain the correct answer to a task by directly reading a
> value that already exists (pre-run) inside static artifacts or rendered
> documents, or applying only extremely trivial operations over values in the
> pre-existing artifacts.

An agent that reads a number out of a shipped `results/` directory scores identically
to one that recomputed it. The grader compares the final answer and cannot tell the
difference. **This is a verifier that measures the wrong thing, and it took manual
LLM-assisted log analysis after the fact to notice.**

Crucible detects this class *mechanically and at runtime*: the transactional
executor snapshots the workspace, computes ΔS per step, and records artifact
provenance. A value present before the run is not a reproduction — that is a
precondition check, not an audit finding.

### 2b. Agents cannot self-assess, and confidence is the wrong instrument

> the mean empirical pass rate across all runs is 93%, the mean reported confidence
> is only 32.1% ... no agent appears to be outperforming a simple random guessing
> baseline telling correct and incorrect tasks apart based on confidence.

Self-reported confidence has **zero discrimination**. This is the strongest possible
motivation for structural abstention: Crucible's `INCONCLUSIVE(reason)` is not the
agent's opinion about its own work, it is a derived consequence of which evidence
requirements went unsatisfied. That gives a published, quantified baseline to beat.

### 2c. The largest failure bucket is a *silent* wrong answer

From their root-cause taxonomy of 56 failures (Table 5):

| Root cause | n |
| --- | --- |
| **Wrong metric / computation** | **18** |
| Timeout (spiraling on fixes) | 14 |
| Gave up (no answer) | 7 |
| Dependency failure | 6 |
| Vision / web fallback | 5 |
| Timeout (environment) | 3 |
| Precision / rounding | 2 |
| Format mismatch | 1 |

The top bucket — the code ran fine, the agent computed the wrong quantity — is
exactly the false-`SUCCESS` category. Timeouts, giving up, and dependency failures
(27 of 56) are `EXECUTION_FAILURE`: honest non-answers that a good verifier should
route away from the decisive path. **A verifier that emits one scalar merges these.**

---

## 3. Proposed paper

> **Process-level verification for agentic reproduction: mechanizing what log
> analysis finds by hand.**

**Claim.** Once answer-matching accuracy saturates, the remaining verification
signal is in *how* the answer was produced. Execution provenance — what existed
before the run, what the run created, which step established which precondition —
turns post-hoc log auditing into a runtime gate, and separates the three outcomes an
answer-matching grader collapses into "correct": genuine reproduction, shortcut, and
lucky wrong-metric agreement.

**Contributions.**

1. A verdict taxonomy that keeps `EXECUTION_FAILURE` and `RESULT_NEGATIVE` distinct,
   with abstention (`INCONCLUSIVE`) as a first-class outcome, and a two-axis
   evaluation — **correctness vs. decisiveness** — instead of single-number accuracy.
2. A mechanical provenance gate that flags pre-existing-artifact shortcuts without an
   LLM judge reading transcripts.
3. **Validation against their released labels.** Their 20 shortcut tasks and 15
   task-level errors are a ground-truth set. Report agreement: of the shortcut tasks
   they found by hand, how many does the provenance gate flag automatically? False
   positives on the clean tasks?
4. Head-to-head on discrimination: Crucible's `INCONCLUSIVE` vs. agent self-reported
   confidence, against their published 32.1%-vs-93% baseline.

**Why this fits the workshop.** Pillar 1 (shortcut/specification-gaming resistance,
red-teaming evaluation harnesses), Pillar 3 (beyond scalar rewards, reflective
verification), cross-cutting (scalable oversight for long-horizon behavior). The
organizers are agents/RL people, not computational-science people — lead with
verifier design, use reproducibility as the testbed.

**Contribution 3 is the one that makes this a real paper rather than a system
description, and it needs no capsule downloads.**

---

## 4. Supporting result already in hand

`scripts/answer_key_variance.py` (run it: `python3 scripts/answer_key_variance.py`)

CORE-Bench ships three independent reference runs per task. Measuring their
disagreement on the 45 `core_train.json` tasks:

```
tasks: 45   numeric questions: 80   with >1 reference run: 80

Python    23 tasks with multi-run answers,  6 nondeterministic (26%)
R         13 tasks with multi-run answers,  0 nondeterministic (0%)
ALL       36 tasks with multi-run answers,  6 nondeterministic (17%)

  capsule              lang   field                     abs       rel
  capsule-3272782      Python Computer Science        0.018  192.86%
  capsule-5286757      Python Computer Science   0.00783238   15.66%
  capsule-3249574      Python Computer Science      0.04445    4.60%
  capsule-9370340      Python Computer Science       0.0256    2.92%
  capsule-0238624      Python Computer Science         0.01    1.14%
  capsule-7038571      Python Computer Science   0.00561462    0.60%

Exceeding the adapter's 0.01 tolerance floor: 4/6
```

Every nondeterministic task is Python; all R tasks are exactly reproducible. The
worst case (`capsule-3272782`, average FNMR) has a reference spread of 0.018 around
a mean of 0.0093 — **the answer key spans a range wider than the value it reports**.

**Scope this honestly.** CORE-Bench's own grader already handles this with a 95%
prediction interval widened by `np.isclose` defaults, so this is *not* a bug in their
benchmark. The two defensible claims are narrower:

- A fixed absolute tolerance floor is the wrong shape. Our own
  `crucible/benchmarks/corebench_data.py` used `max(0.01, max-min)`, which fails in
  both directions: `0.01` exceeds the entire reported value for a small-magnitude
  metric (`capsule-3272782` reports mean FNMR 0.0093) while being enormous for a
  count-valued answer, and `max-min` of three samples understates what a fourth run
  can do. **Fixed** — `tolerance_for()` now takes the max of a 95% prediction
  interval, the rounding interval the printed value denotes, and an `np.isclose`
  floor, and records which one bound. See §6 below.
- Nondeterminism concentrates in a language/field cell (Python CS) rather than being
  uniform, which is a design constraint on any per-task tolerance policy.

This is a supporting paragraph, not a contribution. Do not lead with it.

---

## 5. Cut from scope

- **The leakage dose-response study** (`docs/leakage_dose_response.md`). Good design,
  zero results, needs its own paper. Not in 26 days.
- **Downloading and running 40 capsules.** Given saturation, the marginal value is
  low and the cost is high.
- **The `NaiveAgentArm` harness-off comparison.** A regex scraper is not a baseline
  and a reviewer will say so. Either build a real LLM arm or drop the comparison and
  validate against the released labels instead (preferred — cheaper and stronger).

---

## 6. Tolerance fix (done)

`crucible/benchmarks/corebench_data.py::tolerance_for` replaces `max(0.01, max-min)`
with the max of three independently justified widths, and reports which one bound via
`ToleranceBasis.basis`:

| width | answers |
| --- | --- |
| 95% prediction interval | how far can a *new* run land, given the scatter these runs show? (`None` at n=1) |
| reporting | how much precision did the key discard when it rounded? `0.88` denotes [0.875, 0.885) |
| `isclose` floor | below this, two floats are the same number |

Effect on the six nondeterministic tasks:

| capsule | reported | old tol | new tol | bound by |
| --- | --- | --- | --- | --- |
| capsule-3249574 | 0.96543 | 0.04445 | 0.11625 | prediction interval |
| capsule-9370340 | 0.877233 | 0.0256 | 0.064948 | prediction interval |
| capsule-3272782 | 0.00933 | 0.018 | 0.044806 | prediction interval |
| capsule-0238624 | 0.876667 | 0.01 | 0.028684 | prediction interval |
| capsule-5286757 | 0.0500287 | 0.01 | 0.019489 | prediction interval |
| capsule-7038571 | 0.935587 | 0.01 | 0.015664 | prediction interval |

Deterministic count-valued answers now get a ~6e-5 float floor instead of 0.01, and a
single rounded reference run is sized by its own printed precision (0.005 for `0.88`)
with `estimable=False` recorded rather than a fabricated width.

Two supporting fixes:

- `crucible/adjudicator/stats.py` gains `t_critical` (bisection on the existing
  `_t_two_sided_p`, verified against published tables to 2e-3) and
  `prediction_interval_halfwidth`. Still no scipy/numpy dependency.
- `unscorable_questions()` exposes answer-key questions with non-numeric values.
  These were dropped silently, which let a task fall through to the mechanical
  placeholder claim while looking like it had been checked.

184 tests pass (was 179).

**Note for the paper:** `_reporting_halfwidth` treats integral values as exact,
because Python renders every float with a decimal point — `0.0` would otherwise look
like a deliberate 1-decimal rounding and claim a 0.05 half-width, which as a max
across replicates inflated one question's tolerance by 10x. Worth a footnote if
tolerance sizing appears in the paper; it is the kind of detail a reviewer enjoys.

---

## 7. Language to fix before submitting

`README.md` and the certificate schema use **"calibrated verdict"** throughout, but
`confidence` is currently a hardcoded `1.0` in the emitted certificate. At a
verification workshop this will be challenged. Either produce reliability data or
change the word to "structured" / "evidence-backed." Related: §7.4 of the design doc
promises a TP/FP/TN/FN calibration record before a verifier may gate — contribution 3
above is the first real instance of that, so the two can land together.

---

## 8. Open questions

1. Does `github.com/nnadgi01/corebench-analysis` publish per-task shortcut labels in
   a machine-readable form, or only in the paper's tables? Contribution 3 depends on
   this. **Check first — it gates the whole plan.**
2. CORE-Bench v1.1 is 39 tasks, OOD is 19. Which is the right evaluation surface?
3. Does the provenance gate need the agent's trajectory, or only the workspace
   before/after? If the latter, it applies to any scaffold with no instrumentation —
   that is a much stronger claim and worth stating explicitly.

---

## Sources

- [Who Verifies the Agents? — NeurIPS 2026 Workshop](https://verify-agents-workshop.github.io/)
- [Life After Benchmark Saturation: A Case Study of CORE-Bench (arXiv:2606.26158)](https://arxiv.org/abs/2606.26158)
- [CORE-Bench (arXiv:2409.11363)](https://arxiv.org/abs/2409.11363)
