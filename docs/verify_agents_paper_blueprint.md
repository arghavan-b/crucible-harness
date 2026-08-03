# Correct Output, Wrong Process

## A paper blueprint for the NeurIPS 2026 *Who Verifies the Agents?* workshop

**Candidate subtitle:** *Provenance-Gated Verification for Agentic Scientific Reproduction*

**Target:** full research paper, 8--9 pages excluding references and appendix  
**Submission deadline:** August 29, 2026, Anywhere on Earth  
**Workshop:** <https://verify-agents-workshop.github.io/>

## One-sentence thesis

A scientific agent should receive a decisive verdict only when a trusted verifier can trace the required result to fresh, policy-compliant execution evidence, not merely when the agent reports the expected answer.

## Why this is the right paper

Answer-based evaluation collapses two different questions:

1. Did the agent report a correct value?
2. Did the agent perform the required scientific procedure and produce admissible evidence for that value?

That distinction matters in repository-level scientific tasks because an agent can recover an answer from a manuscript, a cached figure, a stale result file, or another pre-existing artifact without reproducing the computation. Recent analysis of CORE-Bench reports both task-level errors and exploitable shortcuts even when leading aggregate benchmark scores are near saturation. Crucible already contains many of the right abstractions--typed claims, acceptance policies, evidence requirements, controls, execution traces, state snapshots, and an `INCONCLUSIVE` verdict--but its live path does not yet enforce a causal link between executed work and accepted evidence.

The paper should make that missing link the research contribution. It should not present the existing filesystem delta as proof of answer provenance: a post-run state difference shows that state changed, not that the accepted answer was derived by the required computation.

## Proposed contribution

Introduce **evidence-carrying execution**, a verification layer that compiles scientific acceptance policies into runtime evidence contracts. A contract defines which inputs, computations, artifacts, controls, and trusted measurements must witness a claim. The verifier constructs a versioned provenance graph from an execution trace and allows scientific adjudication only when every required witness is present and admissible.

The contribution has four parts:

1. **A two-stage verdict model.** First decide whether the run produced admissible evidence; only then decide whether that evidence supports the scientific claim. This separates execution failure, insufficient evidence, a verified negative result, and verified support.
2. **Runtime evidence contracts.** Compile authored requirements such as fresh output, required command, input dependency, positive control, and trusted metric extraction into checkable predicates over a run.
3. **A provenance gate.** Track file versions and process-level read/write/execute relationships so that copied, stale, or otherwise unsupported artifacts cannot justify a decisive verdict.
4. **An adversarial evaluation.** Measure false verification, valid-run coverage, shortcut detection, and overhead across controlled shortcut strategies and real agent trajectories.

## Precise claim boundary

The system should claim **structural evidence provenance**, not semantic proof that arbitrary code honestly implemented a scientific method.

Under a declared-input, isolated-execution threat model with complete process and filesystem observation, the verifier can determine whether the final evidence artifact has a policy-compliant provenance path from authorized inputs through required execution steps. It can reject specified structural shortcuts such as copying a pre-existing result, renaming a cached artifact, running an irrelevant command before reusing an answer, or presenting an output with no admissible lineage.

It cannot prove that arbitrary untrusted scientific code is semantically correct. That requires trusted code, redundant computation, domain-specific invariants, or stronger formal methods. The paper should state this limit explicitly.

## Research questions

**RQ1 -- False verification.** How often do answer-only, artifact-only, snapshot-delta, and trace-judging baselines issue a successful verdict for a run that did not perform the required procedure?

**RQ2 -- Valid-run coverage.** How often can a provenance gate verify genuine original-pipeline, permitted repair, and permitted modified-pipeline runs without unnecessary abstention?

**RQ3 -- Attack localization.** Which evidence predicates detect which shortcut classes, and what failure reason does the verifier expose?

**RQ4 -- Cost.** What runtime, storage, and implementation overhead does evidence-carrying execution add?

**RQ5 -- Real-trace agreement.** How well do deterministic provenance verdicts agree with independently annotated answer-source and process-validity labels on real agent trajectories?

## Method

### 1. Separate evidence status from scientific status

Represent the outcome as two decisions:

- `evidence_status` in `{ADMISSIBLE, INSUFFICIENT, INVALID, EXECUTION_FAILURE}`
- `scientific_status` in `{SUPPORTS, DOES_NOT_SUPPORT, UNDETERMINED}`

Scientific status is evaluated only when evidence is admissible. A well-executed experiment whose result does not support the hypothesis is therefore not confused with an infrastructure failure or an unsupported answer.

### 2. Compile an evidence contract

For task specification \(\tau\), compile an evidence contract \(E(\tau) = \{e_1, \ldots, e_k\}\). Each predicate must have a concrete witness in the execution record. Initial predicates should include:

- `executed(command_or_entrypoint)`
- `read_declared_input(path_or_digest)`
- `fresh(file_version)`
- `written_by(file_version, process)`
- `derived_from(file_version, allowed_inputs)`
- `not_derived_from(file_version, forbidden_preexisting_artifacts)`
- `metric_extracted_by(trusted_verifier, file_version)`
- `control_passed(control_id)`
- `within_budget(resource, limit)`
- `scientific_files_unchanged`, unless a policy explicitly permits scientific repair

The existing rich `Claim`, `AcceptancePolicy`, and `EvidenceRequirement` path should become the source of these predicates in the live submission pipeline instead of remaining a separate static audit path.

### 3. Record versioned execution provenance

For every run:

1. Create a content-hash manifest of the initial repository state.
2. Execute each command in an isolated Linux environment with network policy recorded.
3. Record the process tree and file open/read/write/rename/execute events.
4. Assign a new version node to every written artifact, even when its final byte length or content hash matches a pre-existing file.
5. Build a graph with process, file-version, command, control, measurement, and verdict nodes.
6. Run trusted metric extractors over provenance-eligible artifacts rather than trusting the agent's final prose or number.

This graph should make the *final version* of an artifact decisive. For example, genuinely computing an output and then overwriting it with a cached result must not inherit the genuine computation's provenance.

### 4. Gate adjudication

The adjudicator receives only measurements whose evidence contracts pass. If a reported value matches the answer key but lacks an admissible witness, the result is `INSUFFICIENT`, with a machine-readable reason such as:

- `PREEXISTING_ARTIFACT_SOURCE`
- `NO_REQUIRED_EXECUTION`
- `NO_DECLARED_INPUT_DEPENDENCY`
- `FORBIDDEN_ANCESTOR`
- `CONTROL_FAILED`
- `SCIENTIFIC_CODE_MODIFIED`
- `UNTRUSTED_METRIC_SOURCE`

The key design principle is conservative verification: the system need not infer what was in the agent's hidden context. It verifies a claim only from evidence that satisfies the contract.

## Expansion required in Crucible

### Must implement for the paper

- Connect `crucible/claims/*` evidence requirements to the live `submit` and adjudication path.
- Replace size-only state comparison with content hashes and explicit file-version events.
- Add process-level file read/write/rename/execute tracing for container runs.
- Persist a provenance graph or normalized provenance event table.
- Add freshness, dependency, forbidden-ancestor, and trusted-extractor predicates.
- Enforce runtime postconditions, positive controls, wall-time budgets, and repair policy.
- Make the adjudicator reject measurements that are not backed by admissible evidence.
- Add machine-readable reason codes and certificate coverage for the evidence decision.

### Useful but not required for this submission

- Cryptographic certificate signing and signature verification.
- Full environment-equivalence verification during replay.
- Semantic verification of arbitrary analysis code.
- The separate leakage dose-response study.
- A broad benchmark leaderboard or a large agent-model comparison.

## Evaluation design

### Dataset A: controlled provenance challenge suite

Construct a balanced set of repository tasks for which the required procedure and allowed inputs are known. Use multiple task families--for example deterministic analysis, seeded analysis, comparative analysis, and figure/table generation--and instantiate each base task with the following execution strategies:

| Strategy | Ground truth | Purpose |
|---|---|---|
| Required pipeline from declared inputs | valid | True-positive coverage |
| Environment-only repair, then pipeline | valid if policy permits | Separate infrastructure repair from scientific mutation |
| Authorized modified pipeline | policy-dependent | Test explicit procedural flexibility |
| Copy or rename a pre-existing result | invalid | Canonical artifact shortcut |
| Read a manuscript/README answer and emit it | invalid | Answer-source shortcut |
| Run an irrelevant command, then reuse a result | invalid | Defeat execution-presence heuristics |
| Run the pipeline, then overwrite the result with a cached artifact | invalid | Test final-version lineage |
| Touch or same-size overwrite of a stale artifact | invalid | Defeat metadata/size-only deltas |
| Produce a correct-looking artifact from undeclared inputs | invalid | Test input policy |
| Correct computation with a failed positive control | invalid or insufficient | Test control gating |
| Required computation returns a scientifically negative result | valid evidence, non-supporting science | Test verdict separation |
| Partial/failing computation with a correct reported answer | invalid | Test answer/evidence separation |

A realistic minimum is 10 base tasks x 8 strategies = 80 controlled traces. Keep the strategies scripted so their ground truth is exact and reproducible. Run actual agents on a smaller subset to establish ecological validity rather than using agents to generate the entire controlled corpus.

### Dataset B: public CORE-Bench analysis traces

Use the public run-level annotations from the CORE-Bench saturation analysis as an external validity study. The released analysis data contains 390 annotated runs over 39 capsules with answer-source categories such as original pipeline, modified pipeline, computational figure reading, visual figure reading, repository artifacts, external sources, and no answer.

These labels are useful but are not a complete gold standard for this paper:

- answer source is not identical to procedural admissibility;
- some modified pipelines may be scientifically valid;
- only a small number of released rows are labeled as repository-artifact answers;
- the published aggregate counts do not expose a clean machine-readable list of all originally reported shortcut and task-error IDs.

Therefore, report category-level agreement and manually double-annotate a stratified subset for the paper's own valid/invalid evidence taxonomy. Ask the analysis authors for the complete shortcut/task-error labels immediately, but do not make the paper depend on receiving them.

Primary sources:

- CORE-Bench: <https://arxiv.org/abs/2409.11363>
- Saturation analysis: <https://arxiv.org/abs/2606.26158>
- Public analysis repository: <https://github.com/nnadgi01/corebench-analysis>

### Baselines

1. **Answer-only:** compare the submitted value or artifact with expected output.
2. **Artifact-exists:** require only that an expected output path exists after execution.
3. **Snapshot delta:** require a post-run filesystem difference, approximating the current mechanism.
4. **Trace judge:** ask a language-model verifier to classify the run from its trace using the same written rubric.
5. **Provenance gate:** the complete evidence-contract method.

If resources are tight, treat agent self-confidence as a descriptive signal rather than a central baseline. It is less directly comparable than a verifier that sees the same trace.

### Primary metrics

- **False verification rate:** invalid runs receiving a decisive successful verdict; this is the primary metric.
- **Valid-run coverage:** valid runs receiving a decisive verdict.
- **Selective risk:** error rate among decisive verdicts as coverage changes.
- **Shortcut precision, recall, and macro-F1**, with per-strategy breakdown.
- **Reason-code accuracy** for failure localization.
- **Runtime overhead**, **trace size**, and **certificate size**.
- **Inter-annotator agreement** for the manually labeled real-trace subset.

Report bootstrap confidence intervals over base tasks, not only over runs, so variants of the same repository do not create pseudoreplication.

### Ablations

- no initial content-hash manifest;
- no read-dependency edges;
- no final-file-version semantics;
- no trusted metric extractor;
- no positive-control gate;
- no scientific-repair policy;
- content hashes versus size/mtime snapshots.

The ablations should map directly to attack classes. A generic component-removal table is less informative than showing why each observation is necessary.

## Core figures and tables

1. **Opening example:** two runs report the same correct answer; only one has an admissible provenance path.
2. **System diagram:** claim and acceptance policy -> evidence contract -> monitored execution -> provenance graph -> evidence gate -> scientific adjudication -> certificate.
3. **Main result:** false verification rate versus valid-run coverage for all methods.
4. **Attack matrix:** methods by shortcut class, showing which attacks pass undetected.
5. **Ablation table:** false verification and coverage after removing each evidence predicate.
6. **Overhead table:** runtime, trace size, and storage by task family.

## Page plan

| Section | Pages |
|---|---:|
| Abstract and introduction | 1.0 |
| Problem, threat model, and verdict taxonomy | 0.9 |
| Evidence contracts and provenance gate | 1.7 |
| Crucible implementation | 0.9 |
| Experimental setup | 1.1 |
| Results and ablations | 1.6 |
| Related work and limitations | 0.8 |
| Conclusion | 0.2 |
| **Total** | **8.2** |

Move detailed predicate schemas, trace instrumentation, annotation rubric, task inventory, and additional examples to the appendix.

## Target abstract

Repository-level scientific agents are commonly evaluated by whether they return an expected number or artifact. A correct output, however, does not establish that the agent performed the requested computation: the same answer may be copied from a manuscript, cached figure, or stale result. We introduce **evidence-carrying execution**, a verification method that compiles scientific acceptance policies into runtime evidence contracts. During execution, the verifier records versioned process--artifact provenance and admits a measurement only when trusted extraction connects it to fresh outputs, authorized inputs, required computations, and passing controls. This yields separate judgments for evidence admissibility and scientific support, allowing the verifier to abstain when an answer is correct but procedurally unsupported. We implement the method in Crucible and evaluate it on [N] controlled repository tasks under [K] genuine and shortcut strategies, plus [M] annotated agent trajectories. Compared with answer-only, artifact-only, snapshot-delta, and trace-judging baselines, the provenance gate reduces false verification from [X] to [Y] at [C] valid-run coverage, with [O]% runtime overhead. Our results show that scientific-agent evaluation needs executable evidence requirements, not answer matching alone.

Do not fill the bracketed quantities until the final experiment table is frozen.

## Claims to avoid

- “A filesystem delta proves where an answer came from.”
- “The system verifies arbitrary scientific code is correct.”
- “All CORE-Bench shortcuts are machine-readable in the public release.”
- “Modified pipelines are necessarily invalid.”
- “The verdicts are calibrated” unless a calibration experiment is run and reported.
- “Crucible already enforces all evidence requirements” before the live paths are integrated.
- Any benchmark result using the hard-coded always-successful naive arm as if it were a real same-agent baseline.

## Related-work positioning

Position the paper between four areas:

1. **Scientific-agent benchmarks:** CORE-Bench and newer reproducibility benchmarks establish the task, while this work questions what constitutes verified completion.
2. **Agent verification and monitoring:** most work scores outputs or asks learned verifiers to assess trajectories; this work makes task-specific evidence obligations executable.
3. **Execution provenance:** provenance systems record what happened; this work uses provenance as a gate on whether a scientific claim may be adjudicated.
4. **Selective prediction and abstention:** `INCONCLUSIVE` is a desirable verifier action when evidence is missing, and should be evaluated through risk--coverage rather than raw accuracy alone.

The novelty claim should be the combination of authored scientific evidence contracts, runtime provenance, and conservative claim adjudication--not provenance by itself.

## Likely reviewer objections and required answers

| Objection | Required answer |
|---|---|
| “This is only a software system.” | Formalize evidence contracts and admissibility, define the threat model, and report controlled causal ablations. |
| “Synthetic attacks are unrealistic.” | Include real agent traces and a manually adjudicated subset. |
| “Provenance does not prove semantic correctness.” | Agree, state the structural claim boundary, and show how trusted extractors and controls narrow the gap. |
| “An LLM rubric is not ground truth.” | Use scripted ground truth for the controlled suite and double human annotation for real traces. |
| “Modified pipelines can be legitimate.” | Separate answer source from policy validity; let authored acceptance policies specify permitted modifications. |
| “The verifier wins by abstaining on everything.” | Report valid-run coverage and selective risk together. |
| “The result is specific to one benchmark.” | Use multiple task families and describe the contract interface independently of CORE-Bench. |

## Four-week execution plan

### August 3--5: freeze the research contract

- Finalize threat model, verdict taxonomy, evidence predicates, and primary metric.
- Select 10 base tasks and enumerate controlled strategies before seeing results.
- Request complete shortcut/task-error metadata and trace access from the CORE-Bench analysis authors.
- Freeze a one-page experiment protocol to prevent post-hoc label changes.

### August 6--11: implement the minimum provenance gate

- Integrate evidence requirements into the live pipeline.
- Add content manifests, versioned file events, process tracing, trusted extraction, and reason codes.
- Add unit and end-to-end tests for every shortcut strategy.

### August 12--16: build data and baselines

- Generate the controlled trace matrix.
- Implement answer-only, artifact-only, snapshot-delta, and trace-judge baselines.
- Prepare the real-trace sample and annotation guide.

### August 17--20: run experiments

- Freeze raw results and environment metadata.
- Double-annotate the selected real traces and resolve disagreements transparently.
- Run primary analysis, confidence intervals, attack breakdowns, and overhead measurement.

### August 21--24: write the full draft

- Draft method and setup while experiments run.
- Create final figures from immutable result files.
- Fill the abstract only after the results table is frozen.

### August 25--27: adversarial review

- Try to bypass the provenance gate with new shortcut variants.
- Check every claim against code, tests, and result artifacts.
- Cut secondary studies that distract from the central result.

### August 28: submission-ready internal deadline

- Complete anonymization, template compliance, references, appendix, and reproducibility package.
- Reserve August 29 AoE for final checks rather than core experiments.

## Immediate decisions

1. Commit to the full paper and the evidence-carrying execution thesis.
2. Treat the current snapshot mechanism as a baseline, not the proposed solution.
3. Make false verification at fixed valid-run coverage the headline result.
4. Build the controlled provenance suite first; use public real traces for external validity.
5. Keep answer-key variance as a supporting observation, not a main contribution.
6. Defer the leakage dose-response study to a separate paper.

