# Analog Leakage → Metric Inflation: a dose-response study

**Why this experiment exists.** `crucible/claims/policy.py` currently gates on
`analog_tanimoto = 0.7` and `max_analog_fraction = 0.05`. Both are guesses. A
verdict layer whose thresholds are folklore cannot claim to be calibrated, and
the 10-paper audit's own KILL criterion — *"the deciding checks are things a
domain expert already runs in an afternoon"* — is only survivable if the checks
carry numbers nobody has published.

**The question.** At what similarity threshold *t*, and what contaminated
fraction *f*, does analog leakage measurably inflate a reported metric, and by
how much?

**The payoff.** Replace a threshold with a **calibrated inflation budget**:
instead of "fail if >5% of test molecules exceed 0.7 similarity", the policy
becomes "expected inflation is Δ̂(t, f); fail if Δ̂ exceeds the claimed margin."
That converts an arbitrary constant into a decision-relevant quantity, and it
directly feeds `AcceptancePolicy` and the `NO_ANALOG_LEAKAGE` requirement.

---

## 1. Hypotheses

**H1 (dose-response).** Metric inflation increases monotonically with the leaked
fraction *f* and with threshold permissiveness *t*, and is approximately
separable: `Δ ≈ f · g(t)` with *g* sigmoid in *t*.

**H2 (differential inflation — the important one).** Inflation is *larger for
higher-capacity models*, because they memorise analogs. If true, leakage does not
merely inflate absolute numbers — it inflates **comparative** claims, and can
reverse a model ranking. "Model X beats baseline B" becomes an artefact of the
split rather than a property of the method.

H1 calibrates the policy. **H2 is the paper**, and it is the direct evidence for
the domain design's central thesis: the code runs perfectly and the claim is
still false.

**H3 (structure dependence).** Leakage risk is a property of dataset structure,
not just of split code. Congeneric/bioactivity sets carry far more latent
leakage than diversity-selected ADMET sets, so a single global threshold is
wrong.

---

## 2. Feasibility finding that shapes the design

Prototyped on RDKit's bundled `Data/NCI/first_5K.smi` (4,991 real molecules,
Bemis-Murcko scaffold split, 3,500 train / 1,491 holdout, Morgan r2 2048):

```
max-Tanimoto-to-train distribution of the holdout set
  [0.0,0.4)   789   52.9%
  [0.4,0.5)   328   22.0%
  [0.5,0.6)   233   15.6%
  [0.6,0.7)    92    6.2%
  [0.7,0.8)    36    2.4%
  [0.8,0.9)    13    0.9%
  [0.9,1.0)     0    0.0%
  median max-sim = 0.389 · >= 0.7 = 3.3% of holdout
```

Two consequences:

1. **You cannot construct high-*f* leaky test sets from natural analogs.** A
   correct scaffold split leaves ~49 molecules above 0.7. Sampling *from* the
   holdout caps *f* at roughly 0.15 for a 300-molecule test set. So the
   intervention must be **train-side**: inject analogs *into training*, which is
   also the more faithful model of the real failure (dedup that missed).
2. Whole pipeline is trivially cheap: fingerprints + 1,491×3,500 similarity in
   **0.2 s**. Compute is not the constraint; experimental design is.

Caveat: NCI first_5K is a *diversity* set, so 3.3% is a floor. Analog-rich
bioactivity data will sit far right of this — which is H3, and why the dataset
panel must span both.

---

## 3. Datasets

Six, spanning size, task type, class balance and — deliberately — molecular
diversity structure.

| Dataset | Source | n | Task | Why it's in the panel |
| --- | --- | --- | --- | --- |
| `Caco2_Wang` | TDC ADMET | ~910 | regression | small-n regime |
| `BBB_Martins` | TDC ADMET | ~2,000 | binary | classic, moderately balanced |
| `CYP2D6_Veith` | TDC ADMET | ~13,100 | binary | large, **imbalanced** (~20% pos) → AUROC-vs-AUPRC |
| `AMES` | TDC ADMET | ~7,250 | binary | balanced comparison to CYP2D6 |
| `Solubility_AqSolDB` | TDC ADMET | ~9,980 | regression | diversity-selected |
| ChEMBL single-target activity (e.g. CHEMBL217/CHEMBL240) | ChEMBL | 2–10k | binary/regression | **congeneric series → analog-rich**, tests H3 |

Pin the TDC version and record the dataset hash — the harness already requires
`DATASET_HASH_PINNED`, so the study should satisfy its own policy.

---

## 4. Design

Three arms. Arm B is the causal core; A is nearly free; C measures what actually
happens in the wild.

### Arm A — observational similarity gradient (1 training run per config)

Scaffold-split, train once, then bucket the *test* set by max-Tanimoto-to-train
and report the metric per bucket.

- Answers: does performance rise with proximity to training data?
- Cost: essentially free — reuses one fitted model.
- Weakness: **confounded**. High-similarity molecules may be intrinsically
  easier (common scaffolds, better-represented chemotypes). Report it as
  motivation, never as the causal estimate.

### Arm B — train-side analog injection (the dose-response)

Hold the **test set completely fixed**. Vary only the training set.

```
1. Scaffold-split into T (train) and S (test). Freeze S.
2. Pick a target similarity band t ∈ {0.5, 0.6, 0.7, 0.8, 0.9, 1.0}.
3. Pick a leaked fraction f ∈ {0, 0.02, 0.05, 0.10, 0.20, 0.40}.
4. Sample f·|S| molecules from S. For each, add ONE analog with
   Tanimoto ∈ [t, t+0.05] to the training set:
     - t = 1.0        -> the test molecule itself (exact-duplicate leakage)
     - t = 0.95-0.99  -> salt/stereo/tautomer variant (the `stereo_salt_dup` case)
     - t < 0.95       -> nearest neighbour from a held-out background library
                         (ChEMBL) filtered to the band; fall back to an MMPA /
                         BRICS single-fragment swap of the test molecule
5. Retrain on T ∪ injected. Evaluate on the frozen S.
6. Δ(t, f) = metric(T ∪ injected) − metric(T)
```

Why this and not test-set construction: the test set never changes, so the
comparison is not confounded by test difficulty, class balance, or size. The
*only* varying quantity is what the model was allowed to see. Section 2 also
shows test-side construction cannot reach high *f* anyway.

**Control arm (essential).** Inject the same *number* of molecules drawn at
random from the background library — molecules with no analog relationship to
S. This separates "leakage inflated the metric" from "more training data
inflated the metric". Without it the whole result is confounded by training-set
size, and a reviewer will say so immediately.

### Arm C — split-parity mismatch (realistic magnitude)

Same data, same model, random split vs scaffold split. This is the single most
common real invalidator (`SPLIT_PARITY`, `CLAIMED_SPLIT_IS_ACTUAL`) and it
produces leakage *emergently* rather than by injection. Reports the magnitude a
reader should expect in the wild, and calibrates `split_parity` severity.

---

## 5. Models

Capacity ladder — H2 needs at least three rungs:

| Model | Featurizer | Capacity | Cost |
| --- | --- | --- | --- |
| Logistic regression / Ridge | ECFP4 (Morgan r2, 2048) | low | seconds |
| Random Forest | ECFP4 | medium | seconds |
| LightGBM | ECFP4 | medium-high | seconds |
| Chemprop D-MPNN | learned graph | high | minutes, GPU optional |

Everything except Chemprop is CPU-only. Run the first three for the full grid;
add Chemprop for the two or three (dataset, t, f) cells where H1 shows the
largest effect.

---

## 6. Metrics and statistics

- Classification: **AUROC and AUPRC** (both — the divergence under imbalance is
  itself a finding that feeds `METRIC_APPROPRIATE_FOR_BALANCE`).
- Regression: **RMSE and Spearman ρ**.
- 5 seeds per cell — matching the policy's own `min_seeds = 5`; seeds vary the
  injection sample and model initialisation, not the test set.
- Report mean with a **bootstrap 95% CI over test molecules**, and a paired test
  across seeds (the same frozen test set makes pairing valid).
- Effect size is Δ metric in the metric's own units. Do not report percentage
  improvements — they hide the comparison against a claimed margin.

**Decision-relevance link.** For each Δ, report the fraction of the 10 audited
papers whose claimed margin is *smaller than Δ*. That single number connects the
curve to willingness-to-pay: "leakage at f = 0.10, t = 0.7 inflates AUROC by
Δ = 0.02, which exceeds the reported margin in N of 10 audited papers." That is
the Decision-impact-Y column of the audit, derived rather than asserted.

---

## 7. Confounds to control explicitly

| Confound | Control |
| --- | --- |
| Training-set size grows with *f* | random-injection control arm (§4 Arm B) |
| Test difficulty varies | test set frozen across all conditions |
| Class balance drifts | stratify injection by label; report balance per cell |
| Scaffold split is itself stochastic | 5 seeds; report split-to-split variance |
| Similarity metric choice | primary Morgan r2 2048 bits; robustness check with MACCS and RDKit descriptors |
| Duplicate molecules already present | run exact + stereo/salt dedup *before* the study; the baseline must be genuinely clean |

That last row matters: if the "clean" baseline already contains leakage, every
Δ is understated. Measure and report baseline contamination per dataset first.

---

## 8. Cost

Similarity and fingerprinting are free (0.2 s at 5k molecules). The grid is
6 datasets × 6 *t* × 6 *f* × 3 models × 5 seeds ≈ 3,240 fits, each seconds on
CPU for ECFP+sklearn/LightGBM. **Days on a laptop**, plus optional GPU hours for
the Chemprop cells. The expense objection does not apply.

---

## 9. What the curve should look like (falsifiable predictions)

- **Flat below t ≈ 0.5.** Tanimoto 0.4–0.5 on ECFP4 is barely "similar"; if Δ is
  non-zero here, the fingerprint or the control arm is wrong.
- **Inflection at t ≈ 0.6–0.75**, near the medicinal-chemistry folk threshold —
  which would be the first empirical justification for a number the field has
  used by convention for two decades.
- **Steep above t ≈ 0.85**, saturating at t = 1.0 (exact duplicates).
- **Δ roughly linear in f** at fixed t, giving `Δ ≈ f · g(t)`.
- **H2:** slope of Δ in f is ordered `LR < RF < LightGBM < D-MPNN`.

If H1 holds but H2 fails — leakage inflates every model equally — the finding is
weaker but still useful: leakage corrupts absolute claims, not comparative ones,
which would *narrow* the product's scope to reproduction rather than comparative
claims. That is a real, publishable negative result and it should be reported as
one.

If Δ is negligible everywhere at realistic *f*, the `NO_ANALOG_LEAKAGE`
requirement should be demoted from gating to advisory. The study is allowed to
kill its own check.

---

## 10. How results feed back into the harness

1. **`DedupPolicy.analog_tanimoto`** ← the *t* where Δ becomes detectable at
   f = 0.05, per dataset structure class (H3 → possibly two defaults: one for
   diversity sets, one for congeneric).
2. **`DedupPolicy.max_analog_fraction`** ← the *f* at which Δ crosses a
   reference margin.
3. **New: an inflation-budget check.** `NO_ANALOG_LEAKAGE` stops being a
   threshold and becomes `Δ̂(t̂, f̂) < claimed_margin`, where t̂ and f̂ are measured
   from the submission. This is the calibrated form, and it is what makes the
   certificate defensible.
4. **Verifier calibration record** — Δ̂ with its CI is exactly the
   `TP/FP/TN/FN`-style evidence §7.4 requires before a verifier may gate.
5. **Severity assignment** — Arm C's magnitude sets whether `SPLIT_PARITY`
   failures are Severity 2 or 3 in the audit rubric.

---

## 11. Threats to the result

- **Background-library dependence.** Analogs pulled from ChEMBL may differ
  systematically from analogs an author's own dedup would have missed. Mitigate
  by reporting the MMPA-generated variant alongside the library-retrieved one
  and checking they agree.
- **Fingerprint monoculture.** Tanimoto on ECFP4 is one similarity notion;
  scaffold-level and pharmacophoric leakage are not captured. State the scope.
- **Single split family.** Everything is conditioned on Bemis-Murcko scaffolds.
  Cluster and temporal splits are separate studies.
- **Generalisation beyond small molecules.** None claimed.
