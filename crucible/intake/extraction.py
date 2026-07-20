"""Extraction schema — the structured output intake asks the model to produce.

Every extracted item carries provenance (where in the paper) and a confidence,
so the draft spec is auditable and a human can review low-confidence claims
before anything runs (design §6.1). These are the fields the LLM prompt asks for
and that map into an ExperimentSpec.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    location: str = Field(..., description="e.g. 'Table 2, p.5' or 'Abstract'")
    quote: str | None = Field(default=None, description="short supporting snippet")


class ExtractedBaseline(BaseModel):
    name: str = Field(..., description="the comparison method, e.g. 'ResNet-50 baseline'")
    metric: str
    dataset: str | None = None
    reported_value: float
    source: SourceRef
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class ExtractedClaim(BaseModel):
    claim_id: str
    statement: str
    metric: str
    dataset: str | None = None
    method: str
    baseline: str | None = Field(default=None, description="name of the baseline it is compared to")
    comparison: str = Field(..., description="e.g. 'method_x > baseline_b' or 'accuracy >= 0.84'")
    reported_value: float | None = None
    baseline_value: float | None = None
    tolerance: float = Field(0.005, description="absolute tolerance for 'reproduced'")
    hypothesis_type: str = Field("comparative", description="comparative | reproduction | ablation | exploratory")
    source: SourceRef
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class DatasetRef(BaseModel):
    name: str
    url: str | None = None
    checksum: str | None = None


class PaperExtraction(BaseModel):
    title: str | None = None
    claims: list[ExtractedClaim] = Field(default_factory=list)
    baselines: list[ExtractedBaseline] = Field(default_factory=list)
    datasets: list[DatasetRef] = Field(default_factory=list)
    notes: str | None = None
