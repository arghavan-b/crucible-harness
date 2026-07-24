"""Diagnoser — symptom -> ranked causes (design §9.2).

Rule-based mapping over the failure text, returning candidate causes with any
extracted parameters (e.g. the missing module name) the repair will need. The
LLM diagnoser is a seam: when rules are inconclusive, a model can propose a cause
from the symptom + environment — but the LLM never applies a repair itself
(design §9.4); it only proposes, and a playbook (checked, versioned) executes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from .symptom import Symptom
from .taxonomy import FailureCause


@dataclass
class Diagnosis:
    cause: FailureCause
    confidence: float
    params: dict[str, str] = field(default_factory=dict)


# (regex, cause, param-extractor) — first match wins per rule, all rules tried.
_RULES: list[tuple[re.Pattern[str], FailureCause, object]] = [
    (re.compile(r"No module named ['\"]?([\w\.]+)"), FailureCause.MISSING_DEPENDENCY,
     lambda m: {"module": m.group(1).split(".")[0]}),
    (re.compile(r"ModuleNotFoundError"), FailureCause.MISSING_DEPENDENCY, None),
    (re.compile(r"CUDA error|cudnn|libcuda|CUDA driver|no kernel image", re.I),
     FailureCause.CUDA_DRIVER_MISMATCH, None),
    (re.compile(r"CUDA out of memory|out of memory|MemoryError|Killed", re.I),
     FailureCause.OUT_OF_MEMORY, None),
    (re.compile(r"command not found|not found:|No such file or directory: ['\"]?[\w/.-]*bin",
     re.I), FailureCause.MISSING_SYSTEM_LIBRARY, None),
    (re.compile(r"No such file or directory", re.I), FailureCause.INVALID_PATH, None),
    (re.compile(r"Permission denied", re.I), FailureCause.INVALID_PATH, None),
    (re.compile(r"version .* required|VersionConflict|incompatible", re.I),
     FailureCause.INCOMPATIBLE_VERSION, None),
    (re.compile(r"cannot import name", re.I), FailureCause.API_CHANGED, None),
]


class Diagnoser(Protocol):
    def diagnose(self, symptom: Symptom) -> list[Diagnosis]: ...


class RuleDiagnoser:
    def diagnose(self, symptom: Symptom) -> list[Diagnosis]:
        if symptom.timed_out:
            return [Diagnosis(FailureCause.TIMEOUT, 0.9)]
        out: list[Diagnosis] = []
        for pattern, cause, extract in _RULES:
            m = pattern.search(symptom.text)
            if m:
                params = extract(m) if extract else {}
                out.append(Diagnosis(cause, 0.8, params))
        if not out:
            out.append(Diagnosis(FailureCause.UNKNOWN, 0.2))
        # De-dup by cause, keep highest confidence, richest params.
        best: dict[FailureCause, Diagnosis] = {}
        for d in out:
            cur = best.get(d.cause)
            if cur is None or d.confidence > cur.confidence or (d.params and not cur.params):
                best[d.cause] = d
        return sorted(best.values(), key=lambda d: d.confidence, reverse=True)


_DIAGNOSE_INSTRUCTIONS = """\
You are Crucible's failure diagnoser. Given the tail of a failed command's output,
classify the root cause using ONLY these labels:
{causes}

Return JSON: {{"diagnoses": [{{"cause": <label>, "confidence": 0.0-1.0,
"params": {{"module": <name>, "package": <name>}}}}]}}, most likely first.
Include a "module" param for missing Python imports and a "package" param for
missing system libraries when you can identify them. Do not invent a repair — only
name the cause. If you truly cannot tell, use "unknown".
"""


class LLMDiagnoser:
    """Proposes a cause with a model, constrained to the taxonomy. It never picks
    or runs a repair (design §9.4) — that stays with the playbook library."""

    def __init__(self, client: "object", max_causes: int = 3) -> None:
        self.client = client
        self.max_causes = max_causes

    def diagnose(self, symptom: Symptom) -> list[Diagnosis]:
        causes = ", ".join(c.value for c in FailureCause)
        prompt = (
            _DIAGNOSE_INSTRUCTIONS.format(causes=causes)
            + f"\n\nexit_code: {symptom.exit_code}\n--- output ---\n{symptom.text[-3000:]}"
        )
        try:
            raw = self.client.complete_json(prompt)  # type: ignore[attr-defined]
        except Exception:
            return [Diagnosis(FailureCause.UNKNOWN, 0.2)]
        out: list[Diagnosis] = []
        for d in (raw.get("diagnoses") or [])[: self.max_causes]:
            try:
                cause = FailureCause(str(d.get("cause")))
            except ValueError:
                continue
            params = {str(k): str(v) for k, v in (d.get("params") or {}).items() if v}
            out.append(Diagnosis(cause, float(d.get("confidence", 0.5)), params))
        return out or [Diagnosis(FailureCause.UNKNOWN, 0.2)]


class CascadingDiagnoser:
    """Cheap rules first; escalate to the LLM only when the rules are
    inconclusive (unknown cause or low confidence). Design §9.2: prefer cheap,
    non-destructive diagnostics before expensive ones."""

    def __init__(
        self, primary: Diagnoser, fallback: Diagnoser, min_confidence: float = 0.5
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.min_confidence = min_confidence

    def diagnose(self, symptom: Symptom) -> list[Diagnosis]:
        result = self.primary.diagnose(symptom)
        top = result[0] if result else None
        if top is not None and top.cause is not FailureCause.UNKNOWN \
                and top.confidence >= self.min_confidence:
            return result
        return self.fallback.diagnose(symptom)
