# CORE-Bench public log bridge

This directory contains a derived mapping from the 390 behavioral annotations
published by `nnadgi01/corebench-analysis` to the corresponding full public
Docent trajectories.

The annotation file's `agent_run_id` values are IDs from a separate ingestion;
none of them are public full-log IDs. The pinned snapshot supports one exact
bridge:

```text
(annotation.capsule_id, annotation.scaffold)
    ==
(public_table.metadata.capsule_id, public_table.metadata.scaffold)
```

The join deliberately excludes accuracy. Three public scores were corrected
after the annotations were generated, so including accuracy would lose valid
matches.

## Frozen inputs

- Analysis repository: `nnadgi01/corebench-analysis`
- Commit: `167da1562809ee3ddf73816bffeddb738f4a0d82`
- Annotation source: `data/rubric_v2_results.json`
- Annotation SHA-256:
  `2d4fe00713e961ab19a6773ad14ba7594ff9f35b0fe95abd3039a9a9ba714cef`
- Public-ID source: `acc_saturation/all_scaffolds_updated.csv`
- Public-ID SHA-256:
  `fb0ed81b9c0df20f786d334db9e0489dcd53b3eabdf528cd34ea65dd6aec048a`
- Full Docent collection: `f739ce50-eec8-4d8e-86b3-2c3dd9f42ab7`
- Canonical public-log checksum lock: `public_log_checksums.json`
- Checksum-lock SHA-256:
  `e5fdcc7f310ae887d5a4f76c14e7d9e620b83da4eeb05d3d98d38e7e773a42c7`

`annotation_public_id_map.csv` has 390 unique mappings across 39 capsules and
10 annotated scaffolds. It preserves `annotation_agent_run_id` and
`public_agent_run_id` as separate fields and gives both the public dashboard URL
and raw JSON URL. Its SHA-256 is
`dad02d8479c46798b0d2a62db8904f4e16946b54697718ce7fcad201a1d5712c`.

## Reproduce the mapping

From the repository root:

```bash
python scripts/download_corebench_logs.py \
  --mapping-only \
  --output-dir artifacts/corebench_logs/mainline \
  --mapping-out data/corebench/annotation_public_id_map.csv
```

The command downloads the two files at the frozen commit, verifies their
SHA-256 values, checks the expected 390/780-row profile, performs the exact
join, and writes a deterministic CSV.

## Download all mapped full logs

```bash
python scripts/download_corebench_logs.py \
  --output-dir artifacts/corebench_logs/mainline \
  --expected-manifest data/corebench/public_log_checksums.json
```

Logs are stored as canonical JSON under `artifacts/`, which is ignored by Git.
`download_manifest.json` records every requested public ID, source URL, local
path, canonical byte count and SHA-256, and raw HTTP response byte count and
SHA-256. The committed checksum lock makes a fresh download fail if its parsed,
canonical JSON content has drifted. Lexically different but semantically
identical JSON is accepted; the raw-response hash remains an audit signal.

A cached log is reused only when its hash matches the lock or prior download
manifest. Missing, invalid, or changed logs remain explicit error rows and make
the command exit nonzero. `--force` refetches a mismatched cache but does not
bypass `--expected-manifest`.

To create a candidate replacement lock during an intentional, separately
reviewed snapshot update, omit `--expected-manifest` and add, for example,
`--lock-out /tmp/corebench-public-log-checksums.json`. Do not overwrite the
committed lock merely because the live endpoint changed.

The anonymous Docent route is used by the public dashboard but is not a
documented archival API. The manifests therefore record content hashes instead
of assuming the endpoint is immutable. Public availability alone should not be
interpreted as permission to redistribute raw trajectories.
