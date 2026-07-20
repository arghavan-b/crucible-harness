"""The fixed step-type ontology (design §4.2).

Every step in an Execution Plan must instantiate one of these types. Free-form
steps are rejected at plan validation. The ontology is versioned and extensible,
but extension is a deliberate act, not something the planner may do inline.
"""

from __future__ import annotations

from enum import Enum


class StepType(str, Enum):
    ACQUIRE_SOURCE = "acquire_source"                # clone/pin repo, fetch paper assets
    INSPECT_PROJECT = "inspect_project"              # detect language, manifests, entry points, CUDA reqs
    BUILD_ENVIRONMENT = "build_environment"          # container/venv construction
    PROVISION_DEPENDENCIES = "provision_dependencies"  # install per manifest
    ACQUIRE_DATA = "acquire_data"                    # download/verify datasets
    CONFIGURE = "configure"                          # write configs, env vars, credentials (from vault)
    SMOKE_RUN = "smoke_run"                          # reduced-scale execution
    POSITIVE_CONTROL_RUN = "positive_control_run"    # reproduce a known-good number
    FULL_RUN = "full_run"                            # the actual experiment, per seed
    COLLECT_ARTIFACTS = "collect_artifacts"          # gather outputs, logs, metrics
    EVALUATE_CLAIMS = "evaluate_claims"              # compare metrics to spec tolerances


ONTOLOGY_VERSION = "v1"
