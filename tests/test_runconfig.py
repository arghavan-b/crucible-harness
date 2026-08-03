"""Run-config extraction: commands, configs, declared split, and the split gate.

The fixture mirrors the shape of a real Code Ocean capsule (an executable `run`
script, a README with placeholder commands, per-method JSON configs), because
that shape is what exposed the two bugs these tests pin down:
README placeholders being mistaken for the config that ran, and a split declared
as config ratios being reported as "no split at all".
"""

from __future__ import annotations

import json
import os

from crucible.claims import compile_procedure, extract_run_config

RUN_SCRIPT = """\
#!/usr/bin/env bash
set -ex

# This is the master script for the capsule.
python -u main.py --config=config/uci.json --task=preprocessing --method=CTGCN-C

python -u main.py --config=config/uci.json --task=embedding --method=CTGCN-C

python3 main.py --config=config/uci.json --task=link_pred
"""

README = """\
# CTGCN

## Commands
Run the model with:
```
python main.py --config=config/xxx.json --task=embedding --method=CTGCN-S
```
"""

MAIN_PY = """\
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, default='config/uci.json')
parser.add_argument('--task', type=str, choices=['preprocessing', 'embedding', 'link_pred'])
parser.add_argument('--method', type=str, choices=['CTGCN-C', 'CTGCN-S', 'GCN'])

if __name__ == '__main__':
    args = parser.parse_args()
"""

UCI_CONFIG = {
    "embedding": {
        "CTGCN-C": {
            "embed_dim": 128,
            "epoch": 50,
            "lr": 1e-3,
            "batch_size": 2048,
            "train_ratio": 0.5,
            "val_ratio": 0.3,
            "test_ratio": 0.2,
        }
    }
}


def _capsule(root, *, seed=False, run_script=True):
    code = root / "code"
    (code / "config").mkdir(parents=True)
    if run_script:
        (code / "run").write_text(RUN_SCRIPT, encoding="utf-8")
    (code / "README.md").write_text(README, encoding="utf-8")
    (code / "main.py").write_text(MAIN_PY, encoding="utf-8")
    (code / "metrics.py").write_text(
        "from sklearn.metrics import roc_auc_score\n", encoding="utf-8"
    )
    config = json.loads(json.dumps(UCI_CONFIG))
    if seed:
        config["embedding"]["CTGCN-C"]["seed"] = 42
    (code / "config" / "uci.json").write_text(json.dumps(config), encoding="utf-8")
    (code / "config" / "xxx.json").write_text(json.dumps(UCI_CONFIG), encoding="utf-8")
    return root


# --- commands ------------------------------------------------------------------


def test_run_script_commands_are_found_in_order(tmp_path):
    _capsule(tmp_path)
    run = extract_run_config(str(tmp_path))
    cmds = run.reproduce_commands
    assert len(cmds) == 3
    assert "--task=preprocessing" in cmds[0].command
    assert "--task=embedding" in cmds[1].command
    assert "--task=link_pred" in cmds[2].command


def test_run_script_outranks_the_readme(tmp_path):
    _capsule(tmp_path)
    run = extract_run_config(str(tmp_path))
    assert all(c.kind == "run_script" for c in run.reproduce_commands)
    assert any(c.kind == "readme" for c in run.commands)


def test_readme_is_used_when_there_is_no_run_script(tmp_path):
    _capsule(tmp_path, run_script=False)
    run = extract_run_config(str(tmp_path))
    assert run.reproduce_commands
    assert all(c.kind == "readme" for c in run.reproduce_commands)


def test_command_args_and_entry_point_are_parsed(tmp_path):
    _capsule(tmp_path)
    cmd = extract_run_config(str(tmp_path)).reproduce_commands[0]
    assert cmd.args["config"] == "config/uci.json"
    assert cmd.args["method"] == "CTGCN-C"
    assert cmd.entry_point == "main.py"


def test_entry_points_are_discovered(tmp_path):
    _capsule(tmp_path)
    assert "main.py" in extract_run_config(str(tmp_path)).entry_points


# --- configs -------------------------------------------------------------------


def test_readme_placeholder_config_is_not_treated_as_the_one_that_ran(tmp_path):
    _capsule(tmp_path)
    referenced = extract_run_config(str(tmp_path)).configs_referenced()
    assert referenced == ["config/uci.json"]
    assert "config/xxx.json" not in referenced


def test_declared_split_reports_what_the_code_ran_with(tmp_path):
    _capsule(tmp_path)
    split = extract_run_config(str(tmp_path)).declared_split()
    values = set(split.values())
    assert {"0.5", "0.3", "0.2"} <= values
    assert all("uci.json" in key for key in split)


def test_decision_relevant_params_are_kept_and_noise_dropped(tmp_path):
    _capsule(tmp_path)
    cfg = extract_run_config(str(tmp_path)).config_for("config/uci.json")
    leaves = {k.split(".")[-1] for k in cfg.params}
    assert {"train_ratio", "epoch", "lr", "embed_dim"} <= leaves
    assert "base_path" not in leaves


def test_config_sections_expose_method_names(tmp_path):
    _capsule(tmp_path)
    cfg = extract_run_config(str(tmp_path)).config_for("config/uci.json")
    assert cfg.sections == ["embedding"]


def test_malformed_config_is_recorded_not_swallowed(tmp_path):
    _capsule(tmp_path)
    (tmp_path / "code" / "config" / "broken.json").write_text("{not json", encoding="utf-8")
    run = extract_run_config(str(tmp_path))
    broken = run.config_for("config/broken.json")
    assert broken.parse_error is not None


def test_argparse_choices_are_captured(tmp_path):
    _capsule(tmp_path)
    run = extract_run_config(str(tmp_path))
    assert "preprocessing" in run.cli_choices["--task"]
    assert "CTGCN-C" in run.cli_choices["--method"]
    assert "--config" in run.cli_options["code/main.py"]


# --- the split gate -------------------------------------------------------------


def test_config_declared_split_without_seed_is_not_regenerable(tmp_path):
    _capsule(tmp_path, seed=False)
    report = compile_procedure(str(tmp_path))
    assert report.blocking_reason() == "split_not_regenerable"
    assert "no seed pinned" in report.summary()


def test_config_declared_split_with_seed_clears_the_gate(tmp_path):
    _capsule(tmp_path, seed=True)
    report = compile_procedure(str(tmp_path))
    assert report.blocking_reason() is None


def test_no_split_information_at_all_is_artifacts_unavailable(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "main.py").write_text("print('hi')\n", encoding="utf-8")
    report = compile_procedure(str(tmp_path))
    assert report.blocking_reason() == "artifacts_unavailable"


def test_run_config_is_attached_to_the_artifact_report(tmp_path):
    _capsule(tmp_path)
    report = compile_procedure(str(tmp_path))
    assert report.run_config is not None
    assert report.run_config.reproduce_commands


def test_repo_summary_carries_commands_and_split_to_the_extractor(tmp_path):
    from crucible.claims import repo_summary

    _capsule(tmp_path)
    summary = repo_summary(compile_procedure(str(tmp_path)))
    assert "--task=preprocessing" in summary
    assert "declared_split" in summary
    assert "train_ratio" in summary


# --- against the real capsule in the repo, when present -------------------------

_CAPSULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "capsule-7038571", "capsule-7038571",
)


def test_real_ctgcn_capsule_run_config(tmp_path):
    """Ground truth: the capsule's `run` invokes main.py three times against
    config/uci.json with tasks preprocessing, embedding, link_pred."""
    if not os.path.isdir(_CAPSULE):
        import pytest

        pytest.skip("CORE-Bench capsule not present")
    run = extract_run_config(_CAPSULE)
    cmds = run.reproduce_commands
    assert [c.args.get("task") for c in cmds] == ["preprocessing", "embedding", "link_pred"]
    assert run.configs_referenced() == ["config/uci.json"]
    assert run.declared_split()
    assert not run.declared_seeds()
