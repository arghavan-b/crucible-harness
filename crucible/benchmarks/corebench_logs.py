"""Pinned CORE-Bench log retrieval and annotation-to-public-ID mapping.

The behavioral annotations published by ``nnadgi01/corebench-analysis`` were
produced from a re-ingested Docent collection.  Their ``agent_run_id`` values
therefore do *not* identify the public, full trajectories.  At the pinned
analysis revision, annotations map bijectively to public runs by the exact
``(capsule_id, scaffold)`` pair.

This module makes that bridge auditable:

* both upstream metadata files are pinned by commit and SHA-256;
* joins are exact and fail on duplicate or missing keys;
* annotation and public IDs remain separate in the generated CSV;
* logs are fetched only from the full public collection;
* saved JSON is canonicalized and covered by a resumable checksum manifest.

The public Docent REST route is used by the public dashboard but is not a
documented archival API.  The collection ID, source revision, response IDs, and
content hashes are consequently all recorded rather than treated as stable by
assumption.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crucible.certificate.manifest import sha256_file

ANALYSIS_REPOSITORY = "nnadgi01/corebench-analysis"
ANALYSIS_COMMIT = "167da1562809ee3ddf73816bffeddb738f4a0d82"
PUBLIC_COLLECTION_ID = "f739ce50-eec8-4d8e-86b3-2c3dd9f42ab7"
TRUNCATED_COLLECTION_IDS = frozenset(
    {
        "1d88d50a-7990-4528-aaf9-4b721d53b43d",
        "94497783-2245-4613-8d5f-73ab653079ec",
    }
)

RAW_GITHUB_BASE = f"https://raw.githubusercontent.com/{ANALYSIS_REPOSITORY}/{ANALYSIS_COMMIT}"
DOCENT_API_BASE = "https://api.docent.transluce.org/rest"
DOCENT_DASHBOARD_BASE = "https://docent.transluce.org/dashboard"

EXPECTED_ANNOTATION_RUNS = 390
EXPECTED_PUBLIC_RUNS = 780
EXPECTED_CAPSULES = 39
EXPECTED_ANNOTATED_SCAFFOLDS = 10
EXPECTED_SCORE_CHANGES = 3

_CAPSULE_RE = re.compile(r"^(?:capsule-)?([0-9]{7})$")


class CoreBenchLogError(RuntimeError):
    """Base error for pinned-source, mapping, and retrieval failures."""


class SourceIntegrityError(CoreBenchLogError):
    """A pinned source is absent, changed, or otherwise unverifiable."""


class MappingError(CoreBenchLogError):
    """Annotation and public-run metadata do not form the expected exact join."""


class LogValidationError(CoreBenchLogError):
    """A downloaded trajectory does not match its requested public run."""


class Fetcher(Protocol):
    """Minimal injectable HTTP transport used by source and log downloads."""

    def __call__(self, url: str, timeout: float) -> bytes: ...


@dataclass(frozen=True)
class SourceSpec:
    name: str
    repository_path: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{RAW_GITHUB_BASE}/{self.repository_path}"


PINNED_SOURCES = (
    SourceSpec(
        name="annotations",
        repository_path="data/rubric_v2_results.json",
        sha256="2d4fe00713e961ab19a6773ad14ba7594ff9f35b0fe95abd3039a9a9ba714cef",
    ),
    SourceSpec(
        name="public_runs",
        repository_path="acc_saturation/all_scaffolds_updated.csv",
        sha256="fb0ed81b9c0df20f786d334db9e0489dcd53b3eabdf528cd34ea65dd6aec048a",
    ),
)


@dataclass(frozen=True, order=True)
class RunKey:
    capsule_id: str
    scaffold: str


@dataclass(frozen=True)
class AnnotationRun:
    annotation_agent_run_id: str
    key: RunKey
    accuracy: float | None


@dataclass(frozen=True)
class PublicRun:
    public_agent_run_id: str
    key: RunKey
    accuracy: float | None


@dataclass(frozen=True)
class RunIdMap:
    key: RunKey
    annotation_agent_run_id: str
    public_agent_run_id: str
    annotation_accuracy: float | None
    public_accuracy: float | None

    @property
    def canonical_public_id(self) -> str:
        return f"docent:{PUBLIC_COLLECTION_ID}:{self.public_agent_run_id}"

    @property
    def public_url(self) -> str:
        return public_log_url(PUBLIC_COLLECTION_ID, self.public_agent_run_id)

    @property
    def api_url(self) -> str:
        return public_log_api_url(PUBLIC_COLLECTION_ID, self.public_agent_run_id)

    @property
    def score_changed(self) -> bool:
        return (
            self.annotation_accuracy is not None
            and self.public_accuracy is not None
            and self.annotation_accuracy != self.public_accuracy
        )


@dataclass(frozen=True)
class DownloadRecord:
    capsule_id: str
    scaffold: str
    annotation_agent_run_id: str
    public_agent_run_id: str
    canonical_public_id: str
    public_url: str
    api_url: str
    path: str
    status: str
    sha256: str | None = None
    byte_count: int | None = None
    http_response_sha256: str | None = None
    http_response_byte_count: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        row: dict[str, object] = {
            "capsule_id": self.capsule_id,
            "scaffold": self.scaffold,
            "annotation_agent_run_id": self.annotation_agent_run_id,
            "public_agent_run_id": self.public_agent_run_id,
            "canonical_public_id": self.canonical_public_id,
            "public_url": self.public_url,
            "api_url": self.api_url,
            "path": self.path,
            "status": self.status,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "http_response_sha256": self.http_response_sha256,
            "http_response_byte_count": self.http_response_byte_count,
        }
        if self.error is not None:
            row["error"] = self.error
        return row


@dataclass(frozen=True)
class DownloadManifest:
    generated_at: str
    analysis_repository: str
    analysis_commit: str
    collection_id: str
    mapping_sha256: str
    records: tuple[DownloadRecord, ...]

    @property
    def complete(self) -> bool:
        return all(record.status in {"downloaded", "cached"} for record in self.records)

    @property
    def completed_count(self) -> int:
        return sum(record.status in {"downloaded", "cached"} for record in self.records)

    def to_dict(self) -> dict[str, object]:
        failed = sum(record.status == "error" for record in self.records)
        pending = sum(record.status == "pending" for record in self.records)
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "analysis_repository": self.analysis_repository,
            "analysis_commit": self.analysis_commit,
            "collection_id": self.collection_id,
            "mapping_sha256": self.mapping_sha256,
            "expected_runs": len(self.records),
            "completed_runs": self.completed_count,
            "failed_runs": failed,
            "pending_runs": pending,
            "complete": self.complete,
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True)
class _ResumeInfo:
    sha256: str
    http_response_sha256: str | None
    http_response_byte_count: int | None


MAPPING_FIELDS = (
    "capsule_id",
    "scaffold",
    "annotation_agent_run_id",
    "public_agent_run_id",
    "canonical_public_id",
    "annotation_accuracy",
    "public_accuracy",
    "score_changed",
    "public_url",
    "api_url",
)


def normalize_capsule_id(value: object) -> str:
    """Normalize only the optional prefix; reject fuzzy capsule matching."""
    text = str(value).strip()
    match = _CAPSULE_RE.fullmatch(text)
    if match is None:
        raise MappingError(f"invalid CORE-Bench capsule ID: {value!r}")
    return f"capsule-{match.group(1)}"


def normalize_scaffold(value: object) -> str:
    """Trim boundary whitespace while retaining exact case and punctuation."""
    text = str(value).strip()
    if not text:
        raise MappingError("scaffold must be a non-empty string")
    return text


def _uuid(value: object, field: str) -> str:
    text = str(value).strip()
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as exc:
        raise MappingError(f"{field} is not a UUID: {value!r}") from exc
    return str(parsed)


def _accuracy(value: object, field: str) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise MappingError(f"{field} must be numeric, not boolean")
    if not isinstance(value, (int, float, str)):
        raise MappingError(f"{field} must be numeric: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MappingError(f"{field} must be numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise MappingError(f"{field} must be finite: {value!r}")
    return number


def _record(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MappingError(f"{context} must be a JSON object")
    return value


def _required(record: Mapping[str, object], field: str, context: str) -> object:
    if field not in record:
        raise MappingError(f"{context} is missing {field!r}")
    return record[field]


def _check_unique_runs(
    rows: Sequence[AnnotationRun] | Sequence[PublicRun],
    *,
    id_attribute: str,
    label: str,
) -> None:
    ids: dict[str, RunKey] = {}
    keys: dict[RunKey, str] = {}
    for row in rows:
        run_id = str(getattr(row, id_attribute))
        if run_id in ids:
            raise MappingError(f"duplicate {label} ID {run_id} for {ids[run_id]} and {row.key}")
        if row.key in keys:
            raise MappingError(f"duplicate {label} key {row.key}: IDs {keys[row.key]} and {run_id}")
        ids[run_id] = row.key
        keys[row.key] = run_id


def load_annotations(path: str | os.PathLike[str]) -> list[AnnotationRun]:
    """Load committed rubric annotations without treating their IDs as public."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MappingError(f"cannot read annotations from {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise MappingError("annotation source must contain a JSON array")

    rows: list[AnnotationRun] = []
    for index, value in enumerate(raw):
        context = f"annotation row {index}"
        record = _record(value, context)
        rows.append(
            AnnotationRun(
                annotation_agent_run_id=_uuid(
                    _required(record, "agent_run_id", context), "annotation agent_run_id"
                ),
                key=RunKey(
                    normalize_capsule_id(_required(record, "capsule_id", context)),
                    normalize_scaffold(_required(record, "scaffold", context)),
                ),
                accuracy=_accuracy(record.get("accuracy"), "annotation accuracy"),
            )
        )
    _check_unique_runs(rows, id_attribute="annotation_agent_run_id", label="annotation")
    return rows


def load_public_runs(path: str | os.PathLike[str]) -> list[PublicRun]:
    """Load the pinned full-collection ID table from the analysis repository."""
    try:
        handle = Path(path).open(newline="", encoding="utf-8")
    except OSError as exc:
        raise MappingError(f"cannot read public-run table from {path}: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        required = {
            "agent_run_id",
            "metadata.capsule_id",
            "metadata.scaffold",
            "metadata.scores.accuracy",
        }
        missing_columns = required - set(reader.fieldnames or ())
        if missing_columns:
            raise MappingError(
                "public-run table is missing columns: " + ", ".join(sorted(missing_columns))
            )
        rows = [
            PublicRun(
                public_agent_run_id=_uuid(row["agent_run_id"], "public agent_run_id"),
                key=RunKey(
                    normalize_capsule_id(row["metadata.capsule_id"]),
                    normalize_scaffold(row["metadata.scaffold"]),
                ),
                accuracy=_accuracy(row["metadata.scores.accuracy"], "public-run accuracy"),
            )
            for row in reader
        ]
    _check_unique_runs(rows, id_attribute="public_agent_run_id", label="public run")
    return rows


def map_public_ids(
    annotations: Sequence[AnnotationRun],
    public_runs: Sequence[PublicRun],
) -> list[RunIdMap]:
    """Map annotations to full public logs by an exact, unique natural key."""
    _check_unique_runs(annotations, id_attribute="annotation_agent_run_id", label="annotation")
    _check_unique_runs(public_runs, id_attribute="public_agent_run_id", label="public run")
    by_key = {run.key: run for run in public_runs}
    missing = [annotation.key for annotation in annotations if annotation.key not in by_key]
    if missing:
        examples = "; ".join(f"{key.capsule_id} / {key.scaffold}" for key in sorted(missing)[:5])
        raise MappingError(
            f"{len(missing)} annotation key(s) have no public full-log match: {examples}"
        )

    mapped = [
        RunIdMap(
            key=annotation.key,
            annotation_agent_run_id=annotation.annotation_agent_run_id,
            public_agent_run_id=by_key[annotation.key].public_agent_run_id,
            annotation_accuracy=annotation.accuracy,
            public_accuracy=by_key[annotation.key].accuracy,
        )
        for annotation in annotations
    ]
    return sorted(
        mapped,
        key=lambda row: (
            row.key.capsule_id,
            row.key.scaffold,
            row.annotation_agent_run_id,
        ),
    )


def validate_frozen_profile(
    annotations: Sequence[AnnotationRun],
    public_runs: Sequence[PublicRun],
    mapping: Sequence[RunIdMap],
) -> None:
    """Assert the dimensions observed at the pinned analysis commit."""
    checks = {
        "annotation rows": (len(annotations), EXPECTED_ANNOTATION_RUNS),
        "public rows": (len(public_runs), EXPECTED_PUBLIC_RUNS),
        "mapped rows": (len(mapping), EXPECTED_ANNOTATION_RUNS),
        "mapped capsules": (
            len({row.key.capsule_id for row in mapping}),
            EXPECTED_CAPSULES,
        ),
        "annotated scaffolds": (
            len({row.key.scaffold for row in mapping}),
            EXPECTED_ANNOTATED_SCAFFOLDS,
        ),
        "post-annotation score changes": (
            sum(row.score_changed for row in mapping),
            EXPECTED_SCORE_CHANGES,
        ),
        "annotation/public ID overlap": (
            len(
                {row.annotation_agent_run_id for row in mapping}
                & {row.public_agent_run_id for row in mapping}
            ),
            0,
        ),
    }
    failures = [
        f"{name}: got {got}, expected {want}" for name, (got, want) in checks.items() if got != want
    ]
    if failures:
        raise MappingError("pinned CORE-Bench profile mismatch (" + "; ".join(failures) + ")")


def public_log_url(collection_id: str, public_agent_run_id: str) -> str:
    return f"{DOCENT_DASHBOARD_BASE}/{collection_id}/agent_run/{public_agent_run_id}"


def public_log_api_url(collection_id: str, public_agent_run_id: str) -> str:
    query = urlencode({"agent_run_id": public_agent_run_id})
    return f"{DOCENT_API_BASE}/{collection_id}/agent_run?{query}"


def _format_accuracy(value: float | None) -> str:
    if value is None:
        return ""
    if value.is_integer():
        return str(int(value))
    return repr(value)


def mapping_csv_bytes(rows: Sequence[RunIdMap]) -> bytes:
    """Serialize the ID map in a stable order with platform-independent newlines."""
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=MAPPING_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in sorted(
        rows,
        key=lambda item: (
            item.key.capsule_id,
            item.key.scaffold,
            item.annotation_agent_run_id,
        ),
    ):
        writer.writerow(
            {
                "capsule_id": row.key.capsule_id,
                "scaffold": row.key.scaffold,
                "annotation_agent_run_id": row.annotation_agent_run_id,
                "public_agent_run_id": row.public_agent_run_id,
                "canonical_public_id": row.canonical_public_id,
                "annotation_accuracy": _format_accuracy(row.annotation_accuracy),
                "public_accuracy": _format_accuracy(row.public_accuracy),
                "score_changed": str(row.score_changed).lower(),
                "public_url": row.public_url,
                "api_url": row.api_url,
            }
        )
    return output.getvalue().encode("utf-8")


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _relative_path(path: Path, *, start: Path) -> str:
    """Return a portable path that resolves from a manifest's directory."""
    return Path(os.path.relpath(path.resolve(), start=start.resolve())).as_posix()


def write_mapping(rows: Sequence[RunIdMap], path: str | os.PathLike[str]) -> None:
    _atomic_write(Path(path), mapping_csv_bytes(rows))


def fetch_url(url: str, timeout: float) -> bytes:
    """Fetch one public resource without credentials or ambient auth state."""
    request = Request(
        url,
        headers={
            "Accept": "application/json, text/csv;q=0.9, */*;q=0.1",
            "User-Agent": "crucible-harness-corebench-log-retriever/1",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - pinned HTTPS URLs
        return bytes(response.read())


def fetch_pinned_sources(
    destination: str | os.PathLike[str],
    *,
    specs: Sequence[SourceSpec] = PINNED_SOURCES,
    fetcher: Fetcher = fetch_url,
    timeout: float = 60.0,
    force: bool = False,
) -> dict[str, Path]:
    """Retrieve or verify the exact metadata inputs used to construct the map."""
    root = Path(destination)
    paths: dict[str, Path] = {}
    for spec in specs:
        target = root / Path(spec.repository_path).name
        if target.exists():
            actual = sha256_file(str(target))
            if actual == spec.sha256:
                paths[spec.name] = target
                continue
            if not force:
                raise SourceIntegrityError(
                    f"cached source {target} has SHA-256 {actual}, expected {spec.sha256}; "
                    "refusing to overwrite it without --force"
                )

        content = fetcher(spec.url, timeout)
        actual = hashlib.sha256(content).hexdigest()
        if actual != spec.sha256:
            raise SourceIntegrityError(
                f"downloaded {spec.url} has SHA-256 {actual}, expected {spec.sha256}"
            )
        _atomic_write(target, content)
        paths[spec.name] = target
    return paths


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_log_payload(value: object, row: RunIdMap) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LogValidationError("public log response is not a JSON object")
    response_id = _uuid(value.get("id"), "downloaded public log id")
    if response_id != row.public_agent_run_id:
        raise LogValidationError(
            f"public log returned ID {response_id}, expected {row.public_agent_run_id}"
        )
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise LogValidationError("public log has no metadata object")
    if normalize_capsule_id(metadata.get("capsule_id")) != row.key.capsule_id:
        raise LogValidationError("public log capsule_id does not match the ID map")
    if normalize_scaffold(metadata.get("scaffold")) != row.key.scaffold:
        raise LogValidationError("public log scaffold does not match the ID map")
    scores = metadata.get("scores")
    if not isinstance(scores, Mapping):
        raise LogValidationError("public log metadata has no scores object")
    try:
        observed_accuracy = _accuracy(scores.get("accuracy"), "downloaded log accuracy")
    except MappingError as exc:
        raise LogValidationError(str(exc)) from exc
    if row.public_accuracy is not None and observed_accuracy != row.public_accuracy:
        raise LogValidationError(
            f"public log accuracy {observed_accuracy} does not match pinned table "
            f"accuracy {row.public_accuracy}"
        )
    transcripts = value.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        raise LogValidationError("public log contains no transcripts")
    actual_message_count = 0
    for index, transcript in enumerate(transcripts):
        if not isinstance(transcript, Mapping) or not isinstance(transcript.get("messages"), list):
            raise LogValidationError(f"transcript {index} has no message list")
        messages = transcript["messages"]
        assert isinstance(messages, list)
        actual_message_count += len(messages)
    declared_message_count = metadata.get("message_count")
    if isinstance(declared_message_count, bool) or not isinstance(declared_message_count, int):
        raise LogValidationError("public log metadata.message_count is not an integer")
    if declared_message_count != actual_message_count:
        raise LogValidationError(
            f"public log contains {actual_message_count} messages, but metadata declares "
            f"{declared_message_count}"
        )
    return value


def _load_resume_info(
    manifest_path: Path,
    *,
    collection_id: str,
    analysis_commit: str,
    mapping_sha256: str,
    require_complete_lock: bool = False,
) -> dict[str, _ResumeInfo]:
    if not manifest_path.exists():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceIntegrityError(f"cannot read prior download manifest: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SourceIntegrityError("prior download manifest is not a JSON object")
    pins = {
        "analysis_repository": ANALYSIS_REPOSITORY,
        "collection_id": collection_id,
        "analysis_commit": analysis_commit,
        "mapping_sha256": mapping_sha256,
    }
    for field, expected in pins.items():
        if raw.get(field) != expected:
            raise SourceIntegrityError(
                f"prior download manifest {field}={raw.get(field)!r}, expected {expected!r}"
            )
    records = raw.get("records")
    if not isinstance(records, list):
        raise SourceIntegrityError("prior download manifest has no records array")
    if require_complete_lock:
        lock_fields: dict[str, object] = {
            "schema_version": 1,
            "kind": "corebench-public-log-checksum-lock",
            "complete": True,
            "expected_runs": len(records),
        }
        for lock_field, expected_value in lock_fields.items():
            if raw.get(lock_field) != expected_value:
                raise SourceIntegrityError(
                    f"checksum lock {lock_field}={raw.get(lock_field)!r}, "
                    f"expected {expected_value!r}"
                )
    resume: dict[str, _ResumeInfo] = {}
    for index, value in enumerate(records):
        if not isinstance(value, Mapping):
            if require_complete_lock:
                raise SourceIntegrityError(f"checksum lock record {index} is not an object")
            continue
        run_id = value.get("public_agent_run_id")
        digest = value.get("sha256")
        response_digest = value.get("http_response_sha256")
        response_byte_count = value.get("http_response_byte_count")
        status = value.get("status")
        if isinstance(run_id, str) and run_id in resume:
            raise SourceIntegrityError(f"prior download manifest repeats public ID {run_id}")
        if (
            isinstance(run_id, str)
            and isinstance(digest, str)
            and status in {"downloaded", "cached", "pending", "locked"}
        ):
            resume[run_id] = _ResumeInfo(
                sha256=digest,
                http_response_sha256=(
                    response_digest if isinstance(response_digest, str) else None
                ),
                http_response_byte_count=(
                    response_byte_count if isinstance(response_byte_count, int) else None
                ),
            )
        elif require_complete_lock:
            raise SourceIntegrityError(
                f"checksum lock record {index} has no usable locked public ID and SHA-256"
            )
    return resume


def _manifest(
    *,
    generated_at: str,
    analysis_commit: str,
    collection_id: str,
    mapping_sha256: str,
    records: Sequence[DownloadRecord],
) -> DownloadManifest:
    return DownloadManifest(
        generated_at=generated_at,
        analysis_repository=ANALYSIS_REPOSITORY,
        analysis_commit=analysis_commit,
        collection_id=collection_id,
        mapping_sha256=mapping_sha256,
        records=tuple(records),
    )


def checksum_lock_bytes(manifest: DownloadManifest) -> bytes:
    """Create a portable content lock without local paths or raw trajectories."""
    if not manifest.complete:
        raise SourceIntegrityError("cannot create a checksum lock from an incomplete download")
    records = []
    for record in manifest.records:
        if record.sha256 is None:
            raise SourceIntegrityError(
                f"download record {record.public_agent_run_id} has no canonical SHA-256"
            )
        records.append(
            {
                "capsule_id": record.capsule_id,
                "scaffold": record.scaffold,
                "annotation_agent_run_id": record.annotation_agent_run_id,
                "public_agent_run_id": record.public_agent_run_id,
                "canonical_public_id": record.canonical_public_id,
                "public_url": record.public_url,
                "api_url": record.api_url,
                "status": "locked",
                "sha256": record.sha256,
                "byte_count": record.byte_count,
                "http_response_sha256": record.http_response_sha256,
                "http_response_byte_count": record.http_response_byte_count,
            }
        )
    payload = {
        "schema_version": 1,
        "kind": "corebench-public-log-checksum-lock",
        "generated_at": manifest.generated_at,
        "analysis_repository": manifest.analysis_repository,
        "analysis_commit": manifest.analysis_commit,
        "collection_id": manifest.collection_id,
        "mapping_sha256": manifest.mapping_sha256,
        "expected_runs": len(records),
        "complete": True,
        "records": records,
    }
    return _canonical_json_bytes(payload)


def write_checksum_lock(manifest: DownloadManifest, path: str | os.PathLike[str]) -> None:
    _atomic_write(Path(path), checksum_lock_bytes(manifest))


def download_logs(
    rows: Sequence[RunIdMap],
    destination: str | os.PathLike[str],
    *,
    manifest_path: str | os.PathLike[str],
    mapping_sha256: str,
    expected_manifest_path: str | os.PathLike[str] | None = None,
    collection_id: str = PUBLIC_COLLECTION_ID,
    analysis_commit: str = ANALYSIS_COMMIT,
    fetcher: Fetcher = fetch_url,
    timeout: float = 60.0,
    force: bool = False,
    generated_at: str | None = None,
    progress: Callable[[int, int, DownloadRecord], None] | None = None,
) -> DownloadManifest:
    """Download mapped full logs and atomically maintain a checksum manifest.

    An existing log is reusable only when the prior pinned manifest supplies its
    expected hash and the bytes still match.  Invalid cache entries are never
    silently overwritten; callers must explicitly pass ``force=True``.
    """
    if collection_id in TRUNCATED_COLLECTION_IDS:
        raise LogValidationError(
            f"collection {collection_id} is a truncated-log collection; full logs are required"
        )
    run_ids = [row.public_agent_run_id for row in rows]
    if len(set(run_ids)) != len(run_ids):
        raise MappingError("ID map contains duplicate public agent-run IDs")

    log_root = Path(destination)
    output_manifest = Path(manifest_path)
    expected_manifest = Path(expected_manifest_path) if expected_manifest_path else output_manifest
    enforce_lock = expected_manifest_path is not None
    resume_info = _load_resume_info(
        expected_manifest,
        collection_id=collection_id,
        analysis_commit=analysis_commit,
        mapping_sha256=mapping_sha256,
        require_complete_lock=enforce_lock,
    )
    if enforce_lock and set(resume_info) != set(run_ids):
        missing = len(set(run_ids) - set(resume_info))
        extra = len(set(resume_info) - set(run_ids))
        raise SourceIntegrityError(
            "expected manifest does not exactly cover the requested mapping "
            f"(missing {missing}, extra {extra})"
        )
    timestamp = generated_at or _utc_now()
    records: list[DownloadRecord] = []
    ordered_rows = sorted(
        rows,
        key=lambda row: (row.key.capsule_id, row.key.scaffold, row.public_agent_run_id),
    )

    for position, row in enumerate(ordered_rows, start=1):
        target = log_root / f"{row.public_agent_run_id}.json"
        api_url = public_log_api_url(collection_id, row.public_agent_run_id)
        public_url = public_log_url(collection_id, row.public_agent_run_id)
        relative_path = _relative_path(target, start=output_manifest.parent)
        try:
            if target.exists() and not force:
                prior = resume_info.get(row.public_agent_run_id)
                if prior is None:
                    raise SourceIntegrityError(
                        f"cached log {target} has no hash in the prior pinned manifest; "
                        "use --force to replace it"
                    )
                actual_hash = sha256_file(str(target))
                if actual_hash != prior.sha256:
                    raise SourceIntegrityError(
                        f"cached log {target} has SHA-256 {actual_hash}, expected {prior.sha256}; "
                        "use --force to replace it"
                    )
                cached = json.loads(target.read_text(encoding="utf-8"))
                _validate_log_payload(cached, row)
                record = DownloadRecord(
                    capsule_id=row.key.capsule_id,
                    scaffold=row.key.scaffold,
                    annotation_agent_run_id=row.annotation_agent_run_id,
                    public_agent_run_id=row.public_agent_run_id,
                    canonical_public_id=f"docent:{collection_id}:{row.public_agent_run_id}",
                    public_url=public_url,
                    api_url=api_url,
                    path=relative_path,
                    status="cached",
                    sha256=actual_hash,
                    byte_count=target.stat().st_size,
                    http_response_sha256=prior.http_response_sha256,
                    http_response_byte_count=prior.http_response_byte_count,
                )
            else:
                raw = fetcher(api_url, timeout)
                response_digest = hashlib.sha256(raw).hexdigest()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LogValidationError(f"public log is not valid UTF-8 JSON: {exc}") from exc
                validated = _validate_log_payload(payload, row)
                canonical = _canonical_json_bytes(validated)
                canonical_digest = hashlib.sha256(canonical).hexdigest()
                prior = resume_info.get(row.public_agent_run_id)
                if prior is not None and (enforce_lock or not force):
                    if canonical_digest != prior.sha256:
                        raise SourceIntegrityError(
                            f"fresh log {row.public_agent_run_id} has canonical SHA-256 "
                            f"{canonical_digest}, expected locked value {prior.sha256}"
                        )
                _atomic_write(target, canonical)
                record = DownloadRecord(
                    capsule_id=row.key.capsule_id,
                    scaffold=row.key.scaffold,
                    annotation_agent_run_id=row.annotation_agent_run_id,
                    public_agent_run_id=row.public_agent_run_id,
                    canonical_public_id=f"docent:{collection_id}:{row.public_agent_run_id}",
                    public_url=public_url,
                    api_url=api_url,
                    path=relative_path,
                    status="downloaded",
                    sha256=canonical_digest,
                    byte_count=len(canonical),
                    http_response_sha256=response_digest,
                    http_response_byte_count=len(raw),
                )
        except Exception as exc:  # keep an audit row for every requested run
            record = DownloadRecord(
                capsule_id=row.key.capsule_id,
                scaffold=row.key.scaffold,
                annotation_agent_run_id=row.annotation_agent_run_id,
                public_agent_run_id=row.public_agent_run_id,
                canonical_public_id=f"docent:{collection_id}:{row.public_agent_run_id}",
                public_url=public_url,
                api_url=api_url,
                path=relative_path,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        records.append(record)
        # Preserve every unprocessed row in the incremental manifest.  This is
        # what makes repeated interruption/resume cycles safe: hashes from a
        # prior run are not discarded merely because this invocation has not
        # reached those rows yet.
        pending = [
            DownloadRecord(
                capsule_id=item.key.capsule_id,
                scaffold=item.key.scaffold,
                annotation_agent_run_id=item.annotation_agent_run_id,
                public_agent_run_id=item.public_agent_run_id,
                canonical_public_id=f"docent:{collection_id}:{item.public_agent_run_id}",
                public_url=public_log_url(collection_id, item.public_agent_run_id),
                api_url=public_log_api_url(collection_id, item.public_agent_run_id),
                path=_relative_path(
                    log_root / f"{item.public_agent_run_id}.json",
                    start=output_manifest.parent,
                ),
                status="pending",
                sha256=(
                    resume_info[item.public_agent_run_id].sha256
                    if item.public_agent_run_id in resume_info
                    else None
                ),
                http_response_sha256=(
                    resume_info[item.public_agent_run_id].http_response_sha256
                    if item.public_agent_run_id in resume_info
                    else None
                ),
                http_response_byte_count=(
                    resume_info[item.public_agent_run_id].http_response_byte_count
                    if item.public_agent_run_id in resume_info
                    else None
                ),
            )
            for item in ordered_rows[position:]
        ]
        current = _manifest(
            generated_at=timestamp,
            analysis_commit=analysis_commit,
            collection_id=collection_id,
            mapping_sha256=mapping_sha256,
            records=[*records, *pending],
        )
        _atomic_write(output_manifest, _canonical_json_bytes(current.to_dict()))
        if progress is not None:
            progress(position, len(ordered_rows), record)

    final = _manifest(
        generated_at=timestamp,
        analysis_commit=analysis_commit,
        collection_id=collection_id,
        mapping_sha256=mapping_sha256,
        records=records,
    )
    _atomic_write(output_manifest, _canonical_json_bytes(final.to_dict()))
    return final


def _write_provenance_manifest(
    path: Path,
    *,
    sources: Mapping[str, Path],
    mapping_path: Path,
    mapping: Sequence[RunIdMap],
) -> None:
    source_rows = []
    for spec in PINNED_SOURCES:
        source_path = sources[spec.name]
        source_rows.append(
            {
                "name": spec.name,
                "repository_path": spec.repository_path,
                "url": spec.url,
                "sha256": sha256_file(str(source_path)),
            }
        )
    payload = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "analysis_repository": ANALYSIS_REPOSITORY,
        "analysis_commit": ANALYSIS_COMMIT,
        "public_collection_id": PUBLIC_COLLECTION_ID,
        "sources": source_rows,
        "mapping": {
            "file": _relative_path(mapping_path, start=path.parent),
            "sha256": sha256_file(str(mapping_path)),
            "rows": len(mapping),
            "capsules": len({row.key.capsule_id for row in mapping}),
            "scaffolds": len({row.key.scaffold for row in mapping}),
            "score_changes": sum(row.score_changed for row in mapping),
        },
    }
    _atomic_write(path, _canonical_json_bytes(payload))


def cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the pinned CORE-Bench annotation/public-ID map and fetch full logs."
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts/corebench_logs/mainline",
        help="Untracked source, log, and manifest directory.",
    )
    parser.add_argument(
        "--mapping-out",
        default=None,
        help="Mapping CSV path (default: <output-dir>/annotation_public_id_map.csv).",
    )
    parser.add_argument(
        "--mapping-only",
        action="store_true",
        help="Verify sources and write the ID map without fetching trajectories.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace cached files that do not match their pinned hashes.",
    )
    parser.add_argument(
        "--expected-manifest",
        default=None,
        help="Read-only checksum lockfile for verifying fresh downloads.",
    )
    parser.add_argument(
        "--lock-out",
        default=None,
        help="Write a portable checksum lock after a complete download.",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="Per-request timeout in seconds."
    )
    args = parser.parse_args(argv)
    if args.mapping_only and args.lock_out:
        parser.error("--lock-out requires trajectory download; remove --mapping-only")

    output = Path(args.output_dir)
    mapping_path = (
        Path(args.mapping_out) if args.mapping_out else output / "annotation_public_id_map.csv"
    )
    try:
        sources = fetch_pinned_sources(output / "sources", timeout=args.timeout, force=args.force)
        annotations = load_annotations(sources["annotations"])
        public_runs = load_public_runs(sources["public_runs"])
        mapping = map_public_ids(annotations, public_runs)
        validate_frozen_profile(annotations, public_runs, mapping)
        write_mapping(mapping, mapping_path)
        mapping_digest = sha256_file(str(mapping_path))
        _write_provenance_manifest(
            output / "provenance_manifest.json",
            sources=sources,
            mapping_path=mapping_path,
            mapping=mapping,
        )
        print(
            f"mapped {len(mapping)} annotations across "
            f"{len({row.key.capsule_id for row in mapping})} capsules -> {mapping_path}"
        )
        print(f"mapping SHA-256: {mapping_digest}")
        if args.mapping_only:
            return 0

        def show_progress(position: int, total: int, record: DownloadRecord) -> None:
            if record.status == "error" or position == total or position % 25 == 0:
                print(f"[{position}/{total}] {record.status}: {record.public_agent_run_id}")

        manifest = download_logs(
            mapping,
            output / "logs",
            manifest_path=output / "download_manifest.json",
            mapping_sha256=mapping_digest,
            expected_manifest_path=args.expected_manifest,
            timeout=args.timeout,
            force=args.force,
            progress=show_progress,
        )
        print(
            f"downloaded or verified {manifest.completed_count}/{len(manifest.records)} logs; "
            f"manifest -> {output / 'download_manifest.json'}"
        )
        if args.lock_out:
            write_checksum_lock(manifest, args.lock_out)
            print(f"checksum lock -> {args.lock_out}")
        return 0 if manifest.complete else 1
    except (CoreBenchLogError, OSError) as exc:
        print(f"corebench-log-download: {exc}")
        return 2


__all__ = [
    "ANALYSIS_COMMIT",
    "ANALYSIS_REPOSITORY",
    "AnnotationRun",
    "CoreBenchLogError",
    "DownloadManifest",
    "DownloadRecord",
    "LogValidationError",
    "MappingError",
    "PINNED_SOURCES",
    "PUBLIC_COLLECTION_ID",
    "PublicRun",
    "RunIdMap",
    "RunKey",
    "SourceIntegrityError",
    "SourceSpec",
    "cli",
    "checksum_lock_bytes",
    "download_logs",
    "fetch_pinned_sources",
    "fetch_url",
    "load_annotations",
    "load_public_runs",
    "map_public_ids",
    "mapping_csv_bytes",
    "normalize_capsule_id",
    "public_log_api_url",
    "public_log_url",
    "validate_frozen_profile",
    "write_checksum_lock",
    "write_mapping",
]
