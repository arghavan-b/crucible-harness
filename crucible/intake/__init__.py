"""Intake: paper + repo -> Experiment Spec (design §6.1)."""

from __future__ import annotations

from .extraction import (
    DatasetRef,
    ExtractedBaseline,
    ExtractedClaim,
    PaperExtraction,
    SourceRef,
)
from .intake import Intake
from .llm import AnthropicClient, FakeClient, LLMClient, OpenAIClient, default_client
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
    "OpenAIClient",
    "PaperExtraction",
    "ParsedPaper",
    "SourceRef",
    "Table",
    "default_client",
    "parse_pdf",
]
