"""Offline tests for the pinned CORE-Bench public-log bridge."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from crucible.benchmarks.corebench_logs import (
    ANALYSIS_COMMIT,
    PUBLIC_COLLECTION_ID,
    AnnotationRun,
    LogValidationError,
    MappingError,
    PublicRun,
    RunIdMap,
    RunKey,
    SourceIntegrityError,
    SourceSpec,
    checksum_lock_bytes,
    download_logs,
    fetch_pinned_sources,
    load_annotations,
    load_public_runs,
    map_public_ids,
    mapping_csv_bytes,
    normalize_capsule_id,
    public_log_api_url,
    write_checksum_lock,
)

ANNOTATION_ID = "11111111-1111-4111-8111-111111111111"
PUBLIC_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"


def _write_annotations(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


def _write_public_runs(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "agent_run_id",
                "metadata.capsule_id",
                "metadata.scaffold",
                "metadata.scores.accuracy",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def _mapped_row() -> RunIdMap:
    return RunIdMap(
        key=RunKey("capsule-0000001", "Example Agent (model-x)"),
        annotation_agent_run_id=ANNOTATION_ID,
        public_agent_run_id=PUBLIC_ID,
        annotation_accuracy=0.0,
        public_accuracy=1.0,
    )


def _log_payload(row: RunIdMap) -> dict[str, object]:
    return {
        "id": row.public_agent_run_id,
        "name": None,
        "description": None,
        "metadata": {
            "capsule_id": row.key.capsule_id,
            "scaffold": row.key.scaffold,
            "scores": {"accuracy": row.public_accuracy},
            "message_count": 1,
        },
        "transcript_groups": [],
        "transcripts": [
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "messages": [{"role": "assistant", "content": "done"}],
                "metadata": {},
            }
        ],
    }


def test_load_and_map_uses_exact_key_not_accuracy(tmp_path: Path) -> None:
    annotations_path = tmp_path / "annotations.json"
    public_path = tmp_path / "public.csv"
    _write_annotations(
        annotations_path,
        [
            {
                "agent_run_id": ANNOTATION_ID,
                "capsule_id": "0000001",
                "scaffold": "  Example Agent (model-x)  ",
                "accuracy": 0,
            }
        ],
    )
    _write_public_runs(
        public_path,
        [
            {
                "agent_run_id": PUBLIC_ID,
                "metadata.capsule_id": "capsule-0000001",
                "metadata.scaffold": "Example Agent (model-x)",
                "metadata.scores.accuracy": 1,
            },
            {
                "agent_run_id": OTHER_ID,
                "metadata.capsule_id": "capsule-0000002",
                "metadata.scaffold": "Unannotated Agent",
                "metadata.scores.accuracy": 0,
            },
        ],
    )

    annotations = load_annotations(annotations_path)
    public_runs = load_public_runs(public_path)
    rows = map_public_ids(annotations, public_runs)

    assert len(rows) == 1  # unannotated public configurations are permitted
    assert rows[0].annotation_agent_run_id == ANNOTATION_ID
    assert rows[0].public_agent_run_id == PUBLIC_ID
    assert rows[0].key == RunKey("capsule-0000001", "Example Agent (model-x)")
    assert rows[0].score_changed  # accuracy is audited, never used as a join key


def test_mapping_fails_closed_on_duplicate_or_missing_key() -> None:
    key = RunKey("capsule-0000001", "Agent")
    annotation = AnnotationRun(ANNOTATION_ID, key, 0.0)
    duplicates = [
        PublicRun(PUBLIC_ID, key, 0.0),
        PublicRun(OTHER_ID, key, 0.0),
    ]
    with pytest.raises(MappingError, match="duplicate public run key"):
        map_public_ids([annotation], duplicates)

    with pytest.raises(MappingError, match="no public full-log match"):
        map_public_ids(
            [annotation],
            [PublicRun(PUBLIC_ID, RunKey("capsule-0000002", "Agent"), 0.0)],
        )


def test_capsule_normalization_is_narrow() -> None:
    assert normalize_capsule_id("0000001") == "capsule-0000001"
    assert normalize_capsule_id("capsule-0000001") == "capsule-0000001"
    with pytest.raises(MappingError):
        normalize_capsule_id("Capsule 1")
    with pytest.raises(MappingError):
        normalize_capsule_id("capsule-1")


def test_mapping_csv_is_sorted_and_preserves_both_ids() -> None:
    first = _mapped_row()
    second = RunIdMap(
        key=RunKey("capsule-0000002", "Agent B"),
        annotation_agent_run_id=OTHER_ID,
        public_agent_run_id="55555555-5555-4555-8555-555555555555",
        annotation_accuracy=1.0,
        public_accuracy=1.0,
    )
    forward = mapping_csv_bytes([second, first])
    reverse = mapping_csv_bytes([first, second])

    assert forward == reverse
    text = forward.decode("utf-8")
    assert text.index(ANNOTATION_ID) < text.index(OTHER_ID)
    assert f"docent:{PUBLIC_COLLECTION_ID}:{PUBLIC_ID}" in text
    assert public_log_api_url(PUBLIC_COLLECTION_ID, PUBLIC_ID) in text


def test_pinned_source_download_verifies_bytes_and_cache(tmp_path: Path) -> None:
    content = b"pinned source\n"
    spec = SourceSpec(
        name="fixture",
        repository_path="data/fixture.json",
        sha256=hashlib.sha256(content).hexdigest(),
    )
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bytes:
        calls.append(url)
        return content

    paths = fetch_pinned_sources(tmp_path, specs=[spec], fetcher=fetch)
    assert paths["fixture"].read_bytes() == content
    assert calls == [spec.url]

    def must_not_fetch(_url: str, _timeout: float) -> bytes:
        raise AssertionError("verified cached input should be reused")

    fetch_pinned_sources(tmp_path, specs=[spec], fetcher=must_not_fetch)
    paths["fixture"].write_bytes(b"changed")
    with pytest.raises(SourceIntegrityError, match="refusing to overwrite"):
        fetch_pinned_sources(tmp_path, specs=[spec], fetcher=fetch)
    fetch_pinned_sources(tmp_path, specs=[spec], fetcher=fetch, force=True)
    assert paths["fixture"].read_bytes() == content


def test_pinned_source_rejects_wrong_download_hash(tmp_path: Path) -> None:
    spec = SourceSpec(
        name="fixture",
        repository_path="data/fixture.json",
        sha256=hashlib.sha256(b"expected").hexdigest(),
    )
    with pytest.raises(SourceIntegrityError, match="downloaded .* SHA-256"):
        fetch_pinned_sources(
            tmp_path,
            specs=[spec],
            fetcher=lambda _url, _timeout: b"unexpected",
        )
    assert not (tmp_path / "fixture.json").exists()


def test_download_uses_public_id_canonicalizes_and_resumes(tmp_path: Path) -> None:
    row = _mapped_row()
    urls: list[str] = []

    def fetch(url: str, _timeout: float) -> bytes:
        urls.append(url)
        return json.dumps(_log_payload(row), indent=2).encode("utf-8")

    logs = tmp_path / "logs"
    manifest_path = tmp_path / "manifest.json"
    manifest = download_logs(
        [row],
        logs,
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=fetch,
        generated_at="2026-08-03T00:00:00Z",
    )

    assert manifest.complete
    assert urls == [public_log_api_url(PUBLIC_COLLECTION_ID, PUBLIC_ID)]
    assert ANNOTATION_ID not in urls[0]
    saved = logs / f"{PUBLIC_ID}.json"
    assert saved.read_bytes().endswith(b"\n")
    assert b"\n  " not in saved.read_bytes()  # canonical compact JSON
    disk_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert disk_manifest["complete"] is True
    assert disk_manifest["records"][0]["path"] == f"logs/{PUBLIC_ID}.json"
    assert disk_manifest["records"][0]["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()
    raw_response = json.dumps(_log_payload(row), indent=2).encode("utf-8")
    assert (
        disk_manifest["records"][0]["http_response_sha256"]
        == hashlib.sha256(raw_response).hexdigest()
    )
    assert disk_manifest["records"][0]["http_response_byte_count"] == len(raw_response)

    def offline(_url: str, _timeout: float) -> bytes:
        raise AssertionError("resume must not refetch verified logs")

    resumed = download_logs(
        [row],
        logs,
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=offline,
        generated_at="2026-08-04T00:00:00Z",
    )
    assert resumed.complete
    assert resumed.records[0].status == "cached"


def test_download_records_unavailable_or_tampered_logs(tmp_path: Path) -> None:
    row = _mapped_row()
    manifest_path = tmp_path / "manifest.json"
    failed = download_logs(
        [row],
        tmp_path / "logs",
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: b"not-json",
        generated_at="2026-08-03T00:00:00Z",
    )
    assert not failed.complete
    assert failed.records[0].status == "error"
    assert "valid UTF-8 JSON" in (failed.records[0].error or "")
    assert json.loads(manifest_path.read_text())["failed_runs"] == 1

    valid_payload = json.dumps(_log_payload(row)).encode()
    complete = download_logs(
        [row],
        tmp_path / "logs",
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: valid_payload,
        force=True,
        generated_at="2026-08-03T00:00:00Z",
    )
    assert complete.complete
    (tmp_path / "logs" / f"{PUBLIC_ID}.json").write_text("tampered")
    tampered = download_logs(
        [row],
        tmp_path / "logs",
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: valid_payload,
        generated_at="2026-08-03T00:00:00Z",
    )
    assert not tampered.complete
    assert "expected" in (tampered.records[0].error or "")


def test_repeated_resume_preserves_unprocessed_hashes(tmp_path: Path) -> None:
    first = _mapped_row()
    second = RunIdMap(
        key=RunKey("capsule-0000002", "Example Agent (model-y)"),
        annotation_agent_run_id=OTHER_ID,
        public_agent_run_id="55555555-5555-4555-8555-555555555555",
        annotation_accuracy=1.0,
        public_accuracy=1.0,
    )
    by_public_id = {
        first.public_agent_run_id: _log_payload(first),
        second.public_agent_run_id: _log_payload(second),
    }

    def fetch(url: str, _timeout: float) -> bytes:
        run_id = url.rsplit("=", 1)[-1]
        return json.dumps(by_public_id[run_id]).encode()

    logs = tmp_path / "logs"
    manifest_path = tmp_path / "manifest.json"
    initial = download_logs(
        [first, second],
        logs,
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=fetch,
    )
    assert initial.complete

    def interrupt(position: int, _total: int, _record: object) -> None:
        if position == 1:
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        download_logs(
            [first, second],
            logs,
            manifest_path=manifest_path,
            mapping_sha256="mapping-digest",
            fetcher=lambda _url, _timeout: pytest.fail("cache should be used"),
            progress=interrupt,
        )
    interrupted = json.loads(manifest_path.read_text())
    assert interrupted["expected_runs"] == 2
    assert interrupted["failed_runs"] == 0
    assert interrupted["pending_runs"] == 1
    assert interrupted["records"][1]["status"] == "pending"
    assert interrupted["records"][1]["sha256"]

    resumed = download_logs(
        [first, second],
        logs,
        manifest_path=manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: pytest.fail("cache should be used"),
    )
    assert resumed.complete
    assert [record.status for record in resumed.records] == ["cached", "cached"]


def test_download_rejects_truncated_collection(tmp_path: Path) -> None:
    with pytest.raises(LogValidationError, match="truncated-log collection"):
        download_logs(
            [_mapped_row()],
            tmp_path / "logs",
            manifest_path=tmp_path / "manifest.json",
            mapping_sha256="mapping-digest",
            collection_id="1d88d50a-7990-4528-aaf9-4b721d53b43d",
        )


def test_download_validates_log_identity_and_metadata(tmp_path: Path) -> None:
    row = _mapped_row()
    wrong = _log_payload(row)
    metadata = wrong["metadata"]
    assert isinstance(metadata, dict)
    metadata["scaffold"] = "Different Agent"
    manifest = download_logs(
        [row],
        tmp_path / "logs",
        manifest_path=tmp_path / "manifest.json",
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: json.dumps(wrong).encode(),
    )
    assert not manifest.complete
    assert "scaffold does not match" in (manifest.records[0].error or "")

    wrong_count = _log_payload(row)
    count_metadata = wrong_count["metadata"]
    assert isinstance(count_metadata, dict)
    count_metadata["message_count"] = 2
    count_manifest = download_logs(
        [row],
        tmp_path / "count-logs",
        manifest_path=tmp_path / "count-manifest.json",
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: json.dumps(wrong_count).encode(),
    )
    assert not count_manifest.complete
    assert "metadata declares 2" in (count_manifest.records[0].error or "")

    wrong_score = _log_payload(row)
    score_metadata = wrong_score["metadata"]
    assert isinstance(score_metadata, dict)
    scores = score_metadata["scores"]
    assert isinstance(scores, dict)
    scores["accuracy"] = 0
    score_manifest = download_logs(
        [row],
        tmp_path / "score-logs",
        manifest_path=tmp_path / "score-manifest.json",
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: json.dumps(wrong_score).encode(),
    )
    assert not score_manifest.complete
    assert "pinned table accuracy" in (score_manifest.records[0].error or "")


def test_expected_manifest_locks_fresh_download_content(tmp_path: Path) -> None:
    row = _mapped_row()
    original = _log_payload(row)
    download_manifest_path = tmp_path / "initial-manifest.json"
    initial = download_logs(
        [row],
        tmp_path / "initial-logs",
        manifest_path=download_manifest_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: json.dumps(original).encode(),
    )
    assert initial.complete
    lock_path = tmp_path / "lock.json"
    write_checksum_lock(initial, lock_path)
    lock = json.loads(lock_path.read_text())
    assert lock["kind"] == "corebench-public-log-checksum-lock"
    assert lock["records"][0]["status"] == "locked"
    assert "path" not in lock["records"][0]

    verified = download_logs(
        [row],
        tmp_path / "fresh-logs",
        manifest_path=tmp_path / "fresh-manifest.json",
        expected_manifest_path=lock_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: json.dumps(original, indent=2).encode(),
    )
    assert verified.complete  # lexical JSON changes are allowed; canonical content is locked

    changed = _log_payload(row)
    transcripts = changed["transcripts"]
    assert isinstance(transcripts, list)
    transcript = transcripts[0]
    assert isinstance(transcript, dict)
    messages = transcript["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    message["content"] = "changed"
    rejected = download_logs(
        [row],
        tmp_path / "changed-logs",
        manifest_path=tmp_path / "changed-manifest.json",
        expected_manifest_path=lock_path,
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: json.dumps(changed).encode(),
    )
    assert not rejected.complete
    assert "expected locked value" in (rejected.records[0].error or "")

    incomplete = download_logs(
        [row],
        tmp_path / "bad-logs",
        manifest_path=tmp_path / "bad-manifest.json",
        mapping_sha256="mapping-digest",
        fetcher=lambda _url, _timeout: b"not-json",
    )
    with pytest.raises(SourceIntegrityError, match="incomplete download"):
        checksum_lock_bytes(incomplete)

    invalid_lock = json.loads(lock_path.read_text())
    invalid_lock["kind"] = "ordinary-download-manifest"
    invalid_lock_path = tmp_path / "invalid-lock.json"
    invalid_lock_path.write_text(json.dumps(invalid_lock), encoding="utf-8")
    with pytest.raises(SourceIntegrityError, match="checksum lock kind"):
        download_logs(
            [row],
            tmp_path / "invalid-lock-logs",
            manifest_path=tmp_path / "invalid-lock-manifest.json",
            expected_manifest_path=invalid_lock_path,
            mapping_sha256="mapping-digest",
            fetcher=lambda _url, _timeout: pytest.fail("invalid lock must fail before fetch"),
        )


def test_committed_mapping_has_frozen_profile() -> None:
    path = Path(__file__).parents[1] / "data" / "corebench" / "annotation_public_id_map.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert ANALYSIS_COMMIT == "167da1562809ee3ddf73816bffeddb738f4a0d82"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "dad02d8479c46798b0d2a62db8904f4e16946b54697718ce7fcad201a1d5712c"
    )
    assert len(rows) == 390
    assert len({row["capsule_id"] for row in rows}) == 39
    assert len({row["scaffold"] for row in rows}) == 10
    assert len({row["annotation_agent_run_id"] for row in rows}) == 390
    assert len({row["public_agent_run_id"] for row in rows}) == 390
    assert not (
        {row["annotation_agent_run_id"] for row in rows}
        & {row["public_agent_run_id"] for row in rows}
    )
    assert sum(row["score_changed"] == "true" for row in rows) == 3
    assert all(
        row["canonical_public_id"].startswith(f"docent:{PUBLIC_COLLECTION_ID}:") for row in rows
    )


def test_committed_log_lock_has_frozen_profile() -> None:
    root = Path(__file__).parents[1]
    lock_path = root / "data" / "corebench" / "public_log_checksums.json"
    mapping_path = root / "data" / "corebench" / "annotation_public_id_map.csv"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    records = lock["records"]
    with mapping_path.open(newline="", encoding="utf-8") as handle:
        mapping = list(csv.DictReader(handle))

    assert hashlib.sha256(lock_path.read_bytes()).hexdigest() == (
        "e5fdcc7f310ae887d5a4f76c14e7d9e620b83da4eeb05d3d98d38e7e773a42c7"
    )
    assert lock["kind"] == "corebench-public-log-checksum-lock"
    assert lock["analysis_commit"] == ANALYSIS_COMMIT
    assert lock["collection_id"] == PUBLIC_COLLECTION_ID
    assert lock["mapping_sha256"] == hashlib.sha256(mapping_path.read_bytes()).hexdigest()
    assert lock["complete"] is True
    assert lock["expected_runs"] == len(records) == 390
    assert {record["status"] for record in records} == {"locked"}
    assert all("path" not in record for record in records)
    assert len({record["public_agent_run_id"] for record in records}) == 390
    assert len({record["sha256"] for record in records}) == 390
    assert all(len(record["sha256"]) == 64 and int(record["sha256"], 16) >= 0 for record in records)
    assert {
        (
            record["capsule_id"],
            record["scaffold"],
            record["annotation_agent_run_id"],
            record["public_agent_run_id"],
        )
        for record in records
    } == {
        (
            row["capsule_id"],
            row["scaffold"],
            row["annotation_agent_run_id"],
            row["public_agent_run_id"],
        )
        for row in mapping
    }
