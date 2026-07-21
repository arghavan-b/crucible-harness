"""Intake: paper + repo -> Experiment Spec (design §6.1)."""

from __future__ import annotations

from .extraction import (
    DatasetRef,
    ExtractedBaseline,
    ExtractedClaim,
    PaperExtraction,
    SourceRef,
)
from .grounding import (
    RepoBinding,
    RepoLocation,
    RepoSignals,
    gather_repo_signals,
    ground_claims,
)
from .intake import Intake
from .llm import (
    AnthropicClient,
    FakeClient,
    LLMClient,
    LoggingLLMClient,
    OpenAIClient,
    default_client,
)
from .paper import Figure, ParsedPaper, Table, parse_pdf

__all__ = [
    "AnthropicClient",
    "DatasetRef",
    "ExtractedBaseline",
    "ExtractedClaim",
    "FakeClient",
    "Figure",
    "Intake",
    "LLMClient",
    "LoggingLLMClient",
    "OpenAIClient",
    "PaperExtraction",
    "ParsedPaper",
    "RepoBinding",
    "RepoLocation",
    "RepoSignals",
    "SourceRef",
    "Table",
    "default_client",
    "gather_repo_signals",
    "ground_claims",
    "parse_pdf",
]
