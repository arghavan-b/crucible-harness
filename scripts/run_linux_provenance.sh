#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run a repository through Crucible's Linux strace provenance collector.

Usage:
  scripts/run_linux_provenance.sh [--rebuild] [--image IMAGE] TASK_REPO [SUBMIT_OPTIONS...]

Examples:
  scripts/run_linux_provenance.sh \
    benchmarks/provenance/pilot/tasks/pilot_weighted_mean/repo \
    --out weighted-mean-linux.certificate.json

  scripts/run_linux_provenance.sh --rebuild ./controlled-task \
    --out certificate.json

The task repository must be inside this checkout. The launcher builds the
Linux image when needed, mounts the checkout at /workspace, adds SYS_PTRACE,
disables Docker's ptrace-blocking seccomp profile, disables container network
access, and selects --runner linux-strace automatically.

Environment:
  CRUCIBLE_PROVENANCE_IMAGE  Override the default image tag.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
DOCKERFILE="${REPO_ROOT}/docker/provenance.Dockerfile"
IMAGE="${CRUCIBLE_PROVENANCE_IMAGE:-crucible-provenance-linux:local}"
REBUILD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --rebuild)
      REBUILD=1
      shift
      ;;
    --image)
      if [[ $# -lt 2 ]]; then
        echo "error: --image requires a value" >&2
        exit 2
      fi
      IMAGE="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or is not on PATH" >&2
  exit 127
fi

TASK_REPO="$1"
shift

if [[ ! -d "${TASK_REPO}" ]]; then
  echo "error: task repository is not a directory: ${TASK_REPO}" >&2
  exit 2
fi

TASK_REPO_ABS="$(cd "${TASK_REPO}" && pwd -P)"
case "${TASK_REPO_ABS}" in
  "${REPO_ROOT}")
    CONTAINER_TASK_REPO=/workspace
    ;;
  "${REPO_ROOT}"/*)
    CONTAINER_TASK_REPO="/workspace/${TASK_REPO_ABS#"${REPO_ROOT}"/}"
    ;;
  *)
    echo "error: task repository must be inside ${REPO_ROOT}" >&2
    exit 2
    ;;
esac

if [[ ${REBUILD} -eq 1 ]] || ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "Building Linux provenance image: ${IMAGE}"
  docker build \
    --file "${DOCKERFILE}" \
    --tag "${IMAGE}" \
    "${REPO_ROOT}"
fi

echo "Running Linux provenance collector for ${CONTAINER_TASK_REPO}"
exec docker run \
  --rm \
  --init \
  --user "$(id -u):$(id -g)" \
  --cap-add SYS_PTRACE \
  --security-opt seccomp=unconfined \
  --network none \
  --env HOME=/tmp \
  --env CRUCIBLE_NETWORK_POLICY=none \
  --mount "type=bind,source=${REPO_ROOT},target=/workspace" \
  --workdir /workspace \
  "${IMAGE}" \
  submit "${CONTAINER_TASK_REPO}" \
  --runner linux-strace \
  "$@"
