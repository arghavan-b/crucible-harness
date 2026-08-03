"""Procedure Compiler — locate the artifacts the validity checks need.

Domain design §3: unlike a generic reproducibility harness, this has to find
*domain-specific* artifacts — which file is the splitter, which function computes
the metric, where the molecule lists live, what the baseline is — and classify
each into the scientific vs infrastructure path.

The output is an ArtifactReport scoring every required artifact
Present / Reconstructible / Missing. That report is Phase A of the 10-paper
audit and produces the **auditability score**: the fraction of required
artifacts obtainable. The design's gating rule is encoded here — if the split
molecule lists AND the split code are both missing, the claim caps at
INCONCLUSIVE(artifacts_unavailable), because nothing downstream can check the
split, and the split checks are the highest-yield group.

Everything here is static analysis: no execution, no network. This is the T0
tier, the one a loop can afford to run on every experiment.
"""

from __future__ import annotations

import os
import re
from enum import Enum

from pydantic import BaseModel, Field

from crucible.schemas import PathClass

from .runconfig import RunConfig, extract_run_config

_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
_CODE_EXTS = (".py", ".ipynb", ".R", ".r")
_DATA_EXTS = (".csv", ".tsv", ".smi", ".sdf", ".parquet", ".json", ".txt", ".npy", ".pkl")


class Availability(str, Enum):
    PRESENT = "present"                 # the artifact is in the repo
    RECONSTRUCTIBLE = "reconstructible"  # not shipped, but derivable (e.g. split code + seed)
    MISSING = "missing"


class ArtifactKind(str, Enum):
    """The artifacts every domain check depends on (domain design §8b Phase A)."""

    REPO = "runnable_repo"
    SPLIT_CODE = "split_code"
    SPLIT_MOLECULE_LISTS = "split_molecule_lists"
    PREPROCESSING_CODE = "preprocessing_code"
    FEATURIZER_CODE = "featurizer_code"
    METRIC_CODE = "metric_code"
    MODEL_CONFIG = "model_config_seeds"
    BASELINE_CODE = "baseline_code"
    PREDICTIONS = "raw_predictions"
    DATASET = "dataset"


# (kind, filename-or-path regex, symbol regex or None, path class)
_SIGNATURES: list[tuple[ArtifactKind, re.Pattern[str], re.Pattern[str] | None, PathClass]] = [
    (
        ArtifactKind.SPLIT_CODE,
        re.compile(r"(^|/)(split|splits|splitter|data_split|partition)[\w-]*\.(py|ipynb|r)$", re.I),
        re.compile(
            r"def\s+\w*split\w*\(|scaffold_split|ScaffoldSplitter|RandomSplitter|"
            r"train_test_split|GroupShuffleSplit|KFold|create_fold|get_split|"
            # Ratio arithmetic is splitting too. Plenty of repos never call a
            # named splitter — they slice by `int(n * self.test_ratio)` — and
            # matching only library splitters reports "no split code" on code
            # that plainly partitions the data.
            r"train_ratio|val_ratio|test_ratio|(test|val|train)_num\s*=",
            re.I,
        ),
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.SPLIT_MOLECULE_LISTS,
        # The split token may sit anywhere in the stem, delimited by _ or -:
        # `train.csv`, `2004-04_train.csv`, `test_fold1.csv` all count. Anchoring
        # it to the start of the filename (the obvious first cut) misses the
        # extremely common date/fold-prefixed naming, while the delimiter
        # requirement still rejects `latest.csv` and `pretrain.csv`.
        re.compile(
            r"(^|/)([\w.-]*[_-])?(train|training|val|valid|validation|test|holdout)"
            r"([_-][\w.-]*)?\.(csv|tsv|smi|txt|json|parquet)$",
            re.I,
        ),
        None,
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.PREPROCESSING_CODE,
        re.compile(r"(^|/)(preprocess|prepare_data|standardi[sz]e|clean|curate)[\w-]*\.(py|ipynb)$",
                   re.I),
        re.compile(
            r"StandardScaler|MinMaxScaler|RobustScaler|SimpleImputer|\.fit_transform\(|"
            r"SelectKBest|VarianceThreshold|remove_salt|Standardizer|neutralize",
            re.I,
        ),
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.FEATURIZER_CODE,
        re.compile(r"(^|/)(featuri[sz]|descriptor|fingerprint|encode|represent)[\w-]*\.(py|ipynb)$",
                   re.I),
        re.compile(
            r"GetMorganFingerprint|MorganGenerator|rdMolDescriptors|MACCSkeys|"
            r"CalcMolDescriptors|Descriptors\.|mordred|featuri[sz]e",
            re.I,
        ),
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.METRIC_CODE,
        re.compile(r"(^|/)(metric|metrics|eval|evaluate|evaluation|score|scoring)[\w-]*\."
                   r"(py|ipynb|r)$", re.I),
        re.compile(
            r"roc_auc_score|average_precision_score|precision_recall_curve|f1_score|"
            r"matthews_corrcoef|mean_squared_error|mean_absolute_error|r2_score|"
            r"spearmanr|pearsonr|balanced_accuracy_score|cohen_kappa_score",
            re.I,
        ),
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.MODEL_CONFIG,
        re.compile(r"(^|/)(config|configs|conf|hparams|params|settings)[\w-]*\."
                   r"(ya?ml|json|toml|ini|py)$", re.I),
        re.compile(r"random_state|manual_seed|set_seed|np\.random\.seed|\bseed\b", re.I),
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.BASELINE_CODE,
        re.compile(r"(^|/)(baseline|baselines|compare|comparison|classical)[\w-]*\.(py|ipynb|r)$",
                   re.I),
        re.compile(
            r"RandomForest|SVC\(|SVR\(|XGB|LGBM|CatBoost|LogisticRegression|"
            r"GradientBoosting|KNeighbors|\bbaseline\b",
            re.I,
        ),
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.PREDICTIONS,
        # Two ways to be an evaluation output: named like one, or living in a
        # results/output directory. Research repos routinely do the latter with
        # names that mention neither "prediction" nor "output"
        # (`results/lp_res_0/CTGCN-C_auc_record.csv`), so filename-only matching
        # silently reports "no predictions shipped" on repos that ship plenty.
        re.compile(
            r"(^|/)(results?|outputs?|preds?|predictions?|eval|evaluation|scores?|metrics?)/"
            r".*\.(csv|tsv|json|npy|parquet)$"
            r"|(^|/)[\w.-]*(prediction|preds|y_pred|y_hat|output|result|record|score|metric)"
            r"[\w.-]*\.(csv|tsv|json|npy|parquet)$",
            re.I,
        ),
        None,
        PathClass.SCIENTIFIC,
    ),
    (
        ArtifactKind.DATASET,
        re.compile(r"(^|/)(data|datasets?)/.*\.(csv|tsv|smi|sdf|parquet)$", re.I),
        None,
        PathClass.SCIENTIFIC,
    ),
]

# Anything matching these is infrastructure: repairable without touching science.
_INFRA_RE = re.compile(
    r"(^|/)(dockerfile|requirements\.txt|pyproject\.toml|setup\.py|setup\.cfg|environment\.ya?ml|"
    r"makefile|\.github/|conda|install|env)[\w./-]*$",
    re.I,
)

# Reconstructibility: split lists are derivable if split code names a library
# splitter AND a seed is pinned somewhere.
_SEED_PIN_RE = re.compile(
    r"(random_state|manual_seed|set_seed|np\.random\.seed)\s*[=(]\s*\d+", re.I
)
_LIB_SPLITTER_RE = re.compile(
    r"scaffold_split|ScaffoldSplitter|RandomSplitter|train_test_split|create_fold|"
    r"deepchem|tdc\.|from tdc",
    re.I,
)


class ArtifactLocation(BaseModel):
    file: str
    symbol_hit: str | None = Field(default=None, description="matched symbol/pattern, if any")
    path_class: PathClass = PathClass.SCIENTIFIC


class ArtifactFinding(BaseModel):
    kind: ArtifactKind
    availability: Availability
    locations: list[ArtifactLocation] = Field(default_factory=list)
    detail: str | None = None


class ArtifactReport(BaseModel):
    """Phase-A output: what exists, what is derivable, what is gone."""

    repo_root: str
    findings: list[ArtifactFinding] = Field(default_factory=list)
    scientific_path: list[str] = Field(default_factory=list)
    infrastructure_path: list[str] = Field(default_factory=list)
    run_config: RunConfig | None = Field(
        default=None, description="how the repo is run, and with which config/parameters"
    )

    def by_kind(self, kind: ArtifactKind) -> ArtifactFinding | None:
        return next((f for f in self.findings if f.kind is kind), None)

    def availability_of(self, kind: ArtifactKind) -> Availability:
        finding = self.by_kind(kind)
        return finding.availability if finding else Availability.MISSING

    @property
    def auditability_score(self) -> float:
        """Fraction of required artifacts obtainable (present or reconstructible).
        Reported up front so a submitter sees the wall before any work happens."""
        if not self.findings:
            return 0.0
        obtainable = sum(
            1 for f in self.findings if f.availability is not Availability.MISSING
        )
        return round(obtainable / len(self.findings), 3)

    def blocking_reason(self) -> str | None:
        """The design's hard gate: no split code AND no molecule lists means the
        split can never be checked, and the split group is the highest-yield set
        of checks — so the claim caps at INCONCLUSIVE(artifacts_unavailable).

        Refined by what the run config declares. Plenty of real repos have no
        file named like a splitter because the split is a *ratio in a config*
        (`train_ratio: 0.5`). That is not "unavailable" — the split is stated,
        just not necessarily regenerable. The two cases get different reasons
        because the remedy differs: one needs the artifacts published, the other
        needs a seed.
        """
        code = self.availability_of(ArtifactKind.SPLIT_CODE)
        lists = self.availability_of(ArtifactKind.SPLIT_MOLECULE_LISTS)
        if code is not Availability.MISSING or lists is not Availability.MISSING:
            return None
        run = self.run_config
        if run is not None and run.declared_split():
            # Split parameters are declared. Regenerable only with a pinned seed.
            return None if run.declared_seeds() else "split_not_regenerable"
        return "artifacts_unavailable"

    def missing(self) -> list[ArtifactKind]:
        return [f.kind for f in self.findings if f.availability is Availability.MISSING]

    def summary(self) -> str:
        blocked = self.blocking_reason()
        head = f"auditability {self.auditability_score:.0%}"
        if blocked == "split_not_regenerable":
            return f"{head} — BLOCKED ({blocked}): split ratios declared but no seed pinned"
        if blocked:
            return f"{head} — BLOCKED ({blocked}): no split code and no molecule lists"
        missing = self.missing()
        if not missing:
            return f"{head} — all required artifacts located"
        return f"{head} — missing: {', '.join(k.value for k in missing)}"


# --- compilation ---------------------------------------------------------------


def _walk(root: str, limit: int = 20000) -> list[str]:
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            out.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(out) >= limit:
                return out
    return out


def _read(root: str, rel: str, limit: int = 200_000) -> str:
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def compile_procedure(repo_root: str) -> ArtifactReport:
    """Locate every required artifact in a repo and classify each file's path.

    Matching is two-signal: a filename pattern OR a symbol pattern inside a code
    file. Symbol matching is what finds a splitter defined in `utils.py` — the
    common case, since research repos rarely name files helpfully.
    """
    repo_root = os.path.abspath(repo_root)
    files = _walk(repo_root)
    code_files = [f for f in files if f.endswith(_CODE_EXTS)]
    contents = {rel: _read(repo_root, rel) for rel in code_files}

    scientific: set[str] = set()
    infrastructure: set[str] = set()
    hits: dict[ArtifactKind, list[ArtifactLocation]] = {}

    for kind, name_re, symbol_re, path_class in _SIGNATURES:
        found: list[ArtifactLocation] = []
        for rel in files:
            norm = rel.replace(os.sep, "/")
            matched_name = bool(name_re.search(norm))
            symbol_hit: str | None = None
            if symbol_re is not None and rel in contents:
                m = symbol_re.search(contents[rel])
                if m:
                    symbol_hit = m.group(0)
            if matched_name or symbol_hit:
                found.append(
                    ArtifactLocation(file=norm, symbol_hit=symbol_hit, path_class=path_class)
                )
        if found:
            hits[kind] = found[:25]

    # Disambiguate: the results-directory rule for PREDICTIONS also sweeps up
    # split files that live under results/ (`results/lp_data_0/2004-04_train.csv`).
    # A file is a split list first — reporting it as a raw prediction would make
    # `metric_implementation_correct` look satisfiable when no predictions exist.
    split_files = {loc.file for loc in hits.get(ArtifactKind.SPLIT_MOLECULE_LISTS, [])}
    if split_files and ArtifactKind.PREDICTIONS in hits:
        remaining = [
            loc for loc in hits[ArtifactKind.PREDICTIONS] if loc.file not in split_files
        ]
        if remaining:
            hits[ArtifactKind.PREDICTIONS] = remaining
        else:
            del hits[ArtifactKind.PREDICTIONS]

    for rel in files:
        norm = rel.replace(os.sep, "/")
        if _INFRA_RE.search(norm):
            infrastructure.add(norm)
    for locations in hits.values():
        for loc in locations:
            if loc.path_class is PathClass.SCIENTIFIC and loc.file not in infrastructure:
                scientific.add(loc.file)

    findings: list[ArtifactFinding] = []

    # The repo itself: present if there is any code at all.
    findings.append(
        ArtifactFinding(
            kind=ArtifactKind.REPO,
            availability=Availability.PRESENT if code_files else Availability.MISSING,
            locations=[ArtifactLocation(file=f, path_class=PathClass.INFRASTRUCTURE)
                       for f in code_files[:5]],
            detail=f"{len(code_files)} code file(s)",
        )
    )

    # Reconstructibility of the split depends on a seed pinned *in the split
    # code* — a random_state on a baseline classifier says nothing about how the
    # molecules were partitioned, so a repo-wide search would over-credit.
    split_sources = "\n".join(
        contents.get(loc.file, "") for loc in hits.get(ArtifactKind.SPLIT_CODE, [])
    )
    seed_pinned = bool(_SEED_PIN_RE.search(split_sources))
    lib_splitter = bool(_LIB_SPLITTER_RE.search("\n".join(contents.values())))

    for kind, _n, _s, _p in _SIGNATURES:
        locations = hits.get(kind, [])
        if locations:
            findings.append(
                ArtifactFinding(kind=kind, availability=Availability.PRESENT, locations=locations)
            )
            continue

        # Not shipped — is it derivable?
        availability = Availability.MISSING
        detail: str | None = None
        if kind is ArtifactKind.SPLIT_MOLECULE_LISTS:
            has_split_code = bool(hits.get(ArtifactKind.SPLIT_CODE))
            if has_split_code and lib_splitter and seed_pinned:
                availability = Availability.RECONSTRUCTIBLE
                detail = "regenerable: library splitter with a pinned seed"
            elif has_split_code and not seed_pinned:
                detail = "split code present but no pinned seed — split is not regenerable"
        elif kind is ArtifactKind.PREDICTIONS:
            detail = "no raw predictions shipped — metric cannot be recomputed statically"
        elif kind is ArtifactKind.DATASET:
            if lib_splitter:
                availability = Availability.RECONSTRUCTIBLE
                detail = "fetchable from a benchmark library (TDC/DeepChem/MoleculeNet)"
        findings.append(ArtifactFinding(kind=kind, availability=availability, detail=detail))

    return ArtifactReport(
        repo_root=repo_root,
        findings=findings,
        scientific_path=sorted(scientific),
        infrastructure_path=sorted(infrastructure),
        run_config=extract_run_config(repo_root),
    )


def repo_summary(report: ArtifactReport) -> str:
    """Compact repo description handed to the LLM extractor as context.

    Includes the reproduce commands and declared split parameters, because a
    model that can see `--config=config/uci.json` and `train_ratio: 0.5` will
    ground the claim's split against what the code actually ran instead of
    against what the paper says.
    """
    parts = [f"root={os.path.basename(report.repo_root)}", report.summary()]
    for finding in report.findings:
        if finding.availability is Availability.PRESENT and finding.locations:
            files = ", ".join(loc.file for loc in finding.locations[:3])
            parts.append(f"{finding.kind.value}: {files}")

    run = report.run_config
    if run is not None:
        if run.entry_points:
            parts.append(f"entry_points: {', '.join(run.entry_points[:5])}")
        for cmd in run.reproduce_commands[:6]:
            parts.append(f"run[{cmd.kind}]: {cmd.command}")
        split = run.declared_split()
        if split:
            parts.append(
                "declared_split: " + ", ".join(f"{k}={v}" for k, v in list(split.items())[:8])
            )
        seeds = run.declared_seeds()
        if seeds:
            parts.append("declared_seeds: " + ", ".join(f"{k}={v}" for k, v in seeds.items()))
        for flag, values in list(run.cli_choices.items())[:6]:
            parts.append(f"cli_choices {flag}: {', '.join(values)}")
    return "\n".join(parts)
