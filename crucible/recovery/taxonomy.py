"""Failure taxonomy — research-code edition (design §9.1).

Hierarchical causes a step failure can be attributed to. Kept domain-specific and
small; it expands as the playbook library grows. `cuda_driver_mismatch` is called
out in the design as the #1 real-world killer.
"""

from __future__ import annotations

from enum import Enum

from crucible.schemas import FailureCategory


class FailureCause(str, Enum):
    # environment
    PYTHON_VERSION_MISMATCH = "python_version_mismatch"
    CUDA_DRIVER_MISMATCH = "cuda_driver_mismatch"
    MISSING_SYSTEM_LIBRARY = "missing_system_library"
    DEPENDENCY_RESOLUTION_CONFLICT = "dependency_resolution_conflict"
    MISSING_DEPENDENCY = "missing_dependency"
    WRONG_RUNTIME = "wrong_runtime"
    INCOMPATIBLE_VERSION = "incompatible_version"
    # resource
    OUT_OF_MEMORY = "out_of_memory"
    DISK_FULL = "disk_full"
    TIMEOUT = "timeout"
    # configuration
    MISSING_CREDENTIAL = "missing_credential"
    INVALID_PATH = "invalid_path"
    MALFORMED_CONFIG = "malformed_config"
    # input
    MISSING_INPUT = "missing_input"
    INVALID_FORMAT = "invalid_format"
    # implementation
    API_CHANGED = "api_changed"
    COMMAND_DEPRECATED = "command_deprecated"
    CODE_BUG = "code_bug"
    # fallback
    UNKNOWN = "unknown"


CATEGORY_OF: dict[FailureCause, FailureCategory] = {
    FailureCause.PYTHON_VERSION_MISMATCH: FailureCategory.ENVIRONMENT,
    FailureCause.CUDA_DRIVER_MISMATCH: FailureCategory.ENVIRONMENT,
    FailureCause.MISSING_SYSTEM_LIBRARY: FailureCategory.ENVIRONMENT,
    FailureCause.DEPENDENCY_RESOLUTION_CONFLICT: FailureCategory.DEPENDENCY,
    FailureCause.MISSING_DEPENDENCY: FailureCategory.DEPENDENCY,
    FailureCause.WRONG_RUNTIME: FailureCategory.ENVIRONMENT,
    FailureCause.INCOMPATIBLE_VERSION: FailureCategory.DEPENDENCY,
    FailureCause.OUT_OF_MEMORY: FailureCategory.RESOURCE,
    FailureCause.DISK_FULL: FailureCategory.RESOURCE,
    FailureCause.TIMEOUT: FailureCategory.RESOURCE,
    FailureCause.MISSING_CREDENTIAL: FailureCategory.CONFIGURATION,
    FailureCause.INVALID_PATH: FailureCategory.CONFIGURATION,
    FailureCause.MALFORMED_CONFIG: FailureCategory.CONFIGURATION,
    FailureCause.MISSING_INPUT: FailureCategory.INPUT,
    FailureCause.INVALID_FORMAT: FailureCategory.INPUT,
    FailureCause.API_CHANGED: FailureCategory.IMPLEMENTATION,
    FailureCause.COMMAND_DEPRECATED: FailureCategory.IMPLEMENTATION,
    FailureCause.CODE_BUG: FailureCategory.IMPLEMENTATION,
}
