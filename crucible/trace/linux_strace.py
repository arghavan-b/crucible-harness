"""Normalize per-PID ``strace -ff`` logs into typed causal event evidence.

The parser intentionally keeps only successful events. It records every parser
ambiguity in ``LinuxEventTrace.issues`` so the envelope cannot claim complete
causal facets when the raw trace could not be interpreted losslessly.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .capture import (
    LinuxEventTrace,
    LinuxFileEvent,
    LinuxFileOperation,
    LinuxProcessEvent,
    LinuxProcessOperation,
)


STRACE_SYSCALLS = (
    "execve",
    "execveat",
    "clone",
    "clone3",
    "fork",
    "vfork",
    "exit",
    "exit_group",
    "chdir",
    "fchdir",
    "open",
    "openat",
    "openat2",
    "creat",
    "close",
    "dup",
    "dup2",
    "dup3",
    "fcntl",
    "read",
    "pread64",
    "readv",
    "preadv",
    "preadv2",
    "write",
    "pwrite64",
    "writev",
    "pwritev",
    "pwritev2",
    "mmap",
    "mmap2",
    "stat",
    "lstat",
    "fstat",
    "newfstatat",
    "statx",
    "access",
    "faccessat",
    "faccessat2",
    "readlink",
    "readlinkat",
    "getdents",
    "getdents64",
    "rename",
    "renameat",
    "renameat2",
    "unlink",
    "unlinkat",
    "mkdir",
    "mkdirat",
    "rmdir",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "mknod",
    "mknodat",
    "truncate",
    "ftruncate",
    "fallocate",
    "chmod",
    "fchmod",
    "fchmodat",
    "chown",
    "fchown",
    "lchown",
    "fchownat",
    "utime",
    "utimes",
    "utimensat",
    "setxattr",
    "lsetxattr",
    "fsetxattr",
    "removexattr",
    "lremovexattr",
    "fremovexattr",
    "sendfile",
    "sendfile64",
    "copy_file_range",
    "splice",
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
)

_TIMESTAMPED_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(.*)$")
_SYSCALL_LINE = re.compile(r"^([a-zA-Z0-9_]+)\((.*)\)\s+=\s+(.+)$")
_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')
_FD_PATH = re.compile(r"(?:\b\d+|AT_FDCWD)<([^>]*)>")
_RESUMED = re.compile(r"^<\.\.\. ([a-zA-Z0-9_]+) resumed>(.*)$")
_EXITED = re.compile(r"^\+\+\+ exited with (\d+) \+\+\+$")
_KILLED = re.compile(r"^\+\+\+ killed by ([A-Z0-9]+)(?: \([^)]*\))? \+\+\+$")
_INTEGER_RESULT = re.compile(r"^(-?\d+)")

_READ_CALLS = frozenset({"read", "pread64", "readv", "preadv", "preadv2"})
_WRITE_CALLS = frozenset({"write", "pwrite64", "writev", "pwritev", "pwritev2"})
_METADATA_CALLS = frozenset(
    {
        "stat",
        "lstat",
        "fstat",
        "newfstatat",
        "statx",
        "access",
        "faccessat",
        "faccessat2",
        "readlink",
        "readlinkat",
    }
)
_IGNORED_CALLS = frozenset({"close", "dup", "dup2", "dup3", "fcntl", "exit", "exit_group"})
_METADATA_WRITE_CALLS = frozenset(
    {
        "chmod",
        "fchmod",
        "fchmodat",
        "chown",
        "fchown",
        "lchown",
        "fchownat",
        "utime",
        "utimes",
        "utimensat",
        "setxattr",
        "lsetxattr",
        "fsetxattr",
        "removexattr",
        "lremovexattr",
        "fremovexattr",
    }
)
_METADATA_FD_WRITE_CALLS = frozenset({"fchmod", "fchown", "fsetxattr", "fremovexattr"})


class LinuxTraceParseError(RuntimeError):
    """The collector did not produce any parseable per-PID trace files."""


@dataclass(frozen=True)
class _RawLine:
    timestamp_s: float
    pid: int
    line_number: int
    body: str


def _decode_c_string(token: str) -> str:
    value = ast.literal_eval(token)
    if not isinstance(value, str):
        raise ValueError("strace string token did not decode to text")
    return value


def _quoted_arguments(arguments: str) -> list[str]:
    return [_decode_c_string(match.group(0)) for match in _QUOTED.finditer(arguments)]


def _integer_result(result: str) -> int | None:
    match = _INTEGER_RESULT.match(result.strip())
    return int(match.group(1)) if match else None


def _all_fd_paths(text: str) -> list[str]:
    return [match.group(1) for match in _FD_PATH.finditer(text)]


def _file_fd_paths(text: str) -> list[str]:
    return [path for path in _all_fd_paths(text) if path.startswith("/")]


def _normalize_path(path: str, cwd: str) -> str:
    if path.startswith("/"):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))


def _workspace_path(path: str, working_dir: str) -> str | None:
    comparable = path.removesuffix(" (deleted)")
    try:
        relative = os.path.relpath(comparable, working_dir)
    except ValueError:
        return None
    if relative == "." or relative == ".." or relative.startswith(f"..{os.sep}"):
        return None
    return Path(relative).as_posix()


def _path_argument(arguments: str, cwd: str, index: int = 0) -> str | None:
    quoted = _quoted_arguments(arguments)
    if len(quoted) <= index:
        return None
    return _normalize_path(quoted[index], cwd)


def _fd_or_path(arguments: str, result: str, cwd: str, path_index: int = 0) -> str | None:
    annotated = _file_fd_paths(result)
    if annotated:
        return annotated[-1]
    annotated = _file_fd_paths(arguments)
    if annotated:
        return annotated[-1]
    return _path_argument(arguments, cwd, path_index)


def _at_path(arguments: str, cwd: str, *, path_index: int = 0, dir_index: int = 0) -> str | None:
    quoted = _quoted_arguments(arguments)
    if len(quoted) <= path_index:
        return None
    directory_paths = _file_fd_paths(arguments)
    base = directory_paths[dir_index] if len(directory_paths) > dir_index else cwd
    return _normalize_path(quoted[path_index], base)


def _read_trace_files(prefix: Path) -> tuple[list[_RawLine], dict[str, str], set[int], list[str]]:
    lines: list[_RawLine] = []
    digests: dict[str, str] = {}
    pids: set[int] = set()
    issues: list[str] = []
    for path in sorted(prefix.parent.glob(f"{prefix.name}.*")):
        suffix = path.name.removeprefix(f"{prefix.name}.")
        if not suffix.isdigit() or not path.is_file():
            continue
        pid = int(suffix)
        pids.add(pid)
        payload = path.read_bytes()
        digests[f"pid:{pid}"] = hashlib.sha256(payload).hexdigest()
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = payload.decode("utf-8", errors="replace")
            issues.append(f"pid {pid}: raw trace was not valid UTF-8")
        unfinished: tuple[str, str, float, int] | None = None
        for line_number, text_line in enumerate(text.splitlines(), start=1):
            match = _TIMESTAMPED_LINE.match(text_line)
            if not match:
                issues.append(f"pid {pid} line {line_number}: missing absolute timestamp")
                continue
            timestamp_s = float(match.group(1))
            body = match.group(2)
            resumed = _RESUMED.match(body)
            if resumed:
                if unfinished is None or unfinished[0] != resumed.group(1):
                    issues.append(
                        f"pid {pid} line {line_number}: resumed syscall has no matching start"
                    )
                    continue
                body = unfinished[1] + resumed.group(2)
                if unfinished[0] in {"clone", "clone3", "fork", "vfork"}:
                    timestamp_s = unfinished[2]
                    line_number = unfinished[3]
                unfinished = None
            elif body.endswith("<unfinished ...>"):
                syscall = body.split("(", 1)[0]
                unfinished = (
                    syscall,
                    body.removesuffix("<unfinished ...>"),
                    timestamp_s,
                    line_number,
                )
                continue
            elif unfinished is not None and not body.startswith("--- "):
                issues.append(
                    f"pid {pid} line {line_number}: unfinished {unfinished[0]} was not resumed"
                )
                unfinished = None
            lines.append(
                _RawLine(
                    timestamp_s=timestamp_s,
                    pid=pid,
                    line_number=line_number,
                    body=body,
                )
            )
        if unfinished is not None:
            issues.append(f"pid {pid}: unfinished {unfinished[0]} at end of trace")
    if not pids:
        raise LinuxTraceParseError("strace produced no per-PID trace files")
    lines.sort(key=lambda line: (line.timestamp_s, line.pid, line.line_number))
    return lines, digests, pids, issues


def parse_strace_trace(
    prefix: str | os.PathLike[str],
    *,
    working_dir: str,
    strace_version: str,
    collection_issue: str | None = None,
) -> LinuxEventTrace:
    """Parse ``strace -ff -ttt -yy`` output rooted at ``prefix``."""
    raw_lines, digests, process_ids, issues = _read_trace_files(Path(prefix))
    if collection_issue:
        issues.append(collection_issue)

    cwd_by_pid: dict[int, str] = {pid: os.path.abspath(working_dir) for pid in process_ids}
    child_pids: set[int] = set()
    process_events: list[LinuxProcessEvent] = []
    file_events: list[LinuxFileEvent] = []
    sequence = 0

    def process_event(
        line: _RawLine,
        operation: LinuxProcessOperation,
        *,
        child_pid: int | None = None,
        executable: str | None = None,
        exit_code: int | None = None,
        signal: str | None = None,
    ) -> None:
        nonlocal sequence
        process_events.append(
            LinuxProcessEvent(
                sequence=sequence,
                timestamp_s=line.timestamp_s,
                pid=line.pid,
                operation=operation,
                child_pid=child_pid,
                executable=executable,
                exit_code=exit_code,
                signal=signal,
            )
        )
        sequence += 1

    def file_event(
        line: _RawLine,
        operation: LinuxFileOperation,
        path: str,
        *,
        target_path: str | None = None,
        bytes_transferred: int | None = None,
    ) -> None:
        nonlocal sequence
        file_events.append(
            LinuxFileEvent(
                sequence=sequence,
                timestamp_s=line.timestamp_s,
                pid=line.pid,
                operation=operation,
                path=path,
                target_path=target_path,
                workspace_path=_workspace_path(path, working_dir),
                target_workspace_path=(
                    _workspace_path(target_path, working_dir) if target_path is not None else None
                ),
                bytes_transferred=bytes_transferred,
            )
        )
        sequence += 1

    for line in raw_lines:
        cwd = cwd_by_pid.setdefault(line.pid, os.path.abspath(working_dir))
        body = line.body
        exited = _EXITED.match(body)
        if exited:
            process_event(line, operation="exit", exit_code=int(exited.group(1)))
            continue
        killed = _KILLED.match(body)
        if killed:
            process_event(line, operation="exit", signal=killed.group(1))
            continue
        if body.startswith("--- "):
            continue
        if "<unfinished ...>" in body or body.startswith("<... "):
            issues.append(
                f"pid {line.pid} line {line.line_number}: unfinished/resumed syscall unsupported"
            )
            continue

        parsed = _SYSCALL_LINE.match(body)
        if not parsed:
            issues.append(f"pid {line.pid} line {line.line_number}: unrecognized trace line")
            continue
        syscall, arguments, result_text = parsed.groups()
        result = _integer_result(result_text)
        succeeded = result is None or result >= 0
        if not succeeded:
            continue

        try:
            if syscall in {"execve", "execveat"}:
                executable = (
                    _at_path(arguments, cwd)
                    if syscall == "execveat"
                    else _path_argument(arguments, cwd)
                )
                if executable is None:
                    raise ValueError("missing executable path")
                process_event(line, operation="exec", executable=executable)
                file_event(line, "open_read", executable)
            elif syscall in {"clone", "clone3", "fork", "vfork"}:
                if result is None or result <= 0:
                    raise ValueError("missing spawned PID")
                child_pids.add(result)
                process_ids.add(result)
                cwd_by_pid[result] = cwd
                process_event(line, operation="spawn", child_pid=result)
            elif syscall == "chdir":
                destination = _path_argument(arguments, cwd)
                if destination is None:
                    raise ValueError("missing chdir path")
                cwd_by_pid[line.pid] = destination
            elif syscall == "fchdir":
                paths = _file_fd_paths(arguments)
                if not paths:
                    raise ValueError("fchdir file descriptor had no decoded path")
                cwd_by_pid[line.pid] = paths[0]
            elif syscall in {"open", "openat", "openat2", "creat"}:
                returned_paths = _file_fd_paths(result_text)
                if returned_paths:
                    opened_path: str | None = returned_paths[-1]
                elif syscall in {"openat", "openat2"}:
                    opened_path = _at_path(arguments, cwd)
                else:
                    opened_path = _path_argument(arguments, cwd)
                if opened_path is None:
                    raise ValueError("open result had no decoded path")
                write_intent = syscall == "creat" or any(
                    flag in arguments
                    for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")
                )
                operation: LinuxFileOperation = "open_write" if write_intent else "open_read"
                file_event(line, operation, opened_path)
            elif syscall in _READ_CALLS | _WRITE_CALLS:
                paths = _file_fd_paths(arguments)
                if not paths:
                    if _all_fd_paths(arguments):
                        continue  # pipe, socket, or other non-file descriptor
                    raise ValueError("I/O file descriptor had no decoded path")
                assert result is not None
                file_event(
                    line,
                    "read" if syscall in _READ_CALLS else "write",
                    paths[0],
                    bytes_transferred=result,
                )
            elif syscall in {"mmap", "mmap2"}:
                paths = _file_fd_paths(arguments)
                if not paths:
                    continue  # anonymous mapping
                if "PROT_READ" in arguments:
                    file_event(line, "mmap_read", paths[-1])
                if "PROT_WRITE" in arguments and "MAP_SHARED" in arguments:
                    file_event(line, "mmap_write", paths[-1])
            elif syscall in _METADATA_CALLS:
                quoted = _quoted_arguments(arguments)
                if quoted:
                    metadata_path: str | None = _at_path(arguments, cwd)
                else:
                    paths = _file_fd_paths(arguments)
                    metadata_path = paths[0] if paths else None
                if metadata_path is None:
                    raise ValueError("metadata syscall had no decoded path")
                file_event(line, "metadata_read", metadata_path, bytes_transferred=result)
            elif syscall in {"getdents", "getdents64"}:
                paths = _file_fd_paths(arguments)
                if not paths:
                    if _all_fd_paths(arguments):
                        continue
                    raise ValueError("directory descriptor had no decoded path")
                assert result is not None
                file_event(line, "directory_read", paths[0], bytes_transferred=result)
            elif syscall in {"rename", "renameat", "renameat2"}:
                quoted = _quoted_arguments(arguments)
                if len(quoted) < 2:
                    raise ValueError("rename syscall had fewer than two paths")
                directory_paths = _file_fd_paths(arguments)
                source_base = directory_paths[0] if directory_paths else cwd
                target_base = directory_paths[1] if len(directory_paths) > 1 else cwd
                source = _normalize_path(quoted[0], source_base)
                target = _normalize_path(quoted[1], target_base)
                file_event(line, "rename", source, target_path=target)
            elif syscall in {"unlink", "unlinkat"}:
                unlinked_path = (
                    _at_path(arguments, cwd)
                    if syscall == "unlinkat"
                    else _path_argument(arguments, cwd)
                )
                if unlinked_path is None:
                    raise ValueError("unlink syscall had no path")
                file_event(line, "unlink", unlinked_path)
            elif syscall in {"mkdir", "mkdirat", "rmdir", "mknod", "mknodat"}:
                namespace_path = (
                    _at_path(arguments, cwd)
                    if syscall in {"mkdirat", "mknodat"}
                    else _path_argument(arguments, cwd)
                )
                if namespace_path is None:
                    raise ValueError("namespace syscall had no path")
                file_event(line, "namespace_write", namespace_path)
            elif syscall in {"symlink", "symlinkat"}:
                link_path = (
                    _at_path(arguments, cwd, path_index=1)
                    if syscall == "symlinkat"
                    else _path_argument(arguments, cwd, index=1)
                )
                if link_path is None:
                    raise ValueError("symlink syscall had no destination path")
                file_event(line, "namespace_write", link_path)
            elif syscall in {"link", "linkat"}:
                quoted = _quoted_arguments(arguments)
                if len(quoted) < 2:
                    raise ValueError("link syscall had fewer than two paths")
                directory_paths = _file_fd_paths(arguments)
                source_base = directory_paths[0] if directory_paths else cwd
                target_base = directory_paths[1] if len(directory_paths) > 1 else cwd
                source = _normalize_path(quoted[0], source_base)
                target = _normalize_path(quoted[1], target_base)
                file_event(line, "metadata_read", source)
                file_event(line, "namespace_write", target)
            elif syscall in _METADATA_WRITE_CALLS:
                if syscall in _METADATA_FD_WRITE_CALLS:
                    paths = _file_fd_paths(arguments)
                    metadata_write_path = paths[0] if paths else None
                else:
                    metadata_write_path = _at_path(arguments, cwd)
                if metadata_write_path is None:
                    raise ValueError("metadata mutation had no decoded path")
                file_event(line, "metadata_write", metadata_write_path)
            elif syscall in {"truncate", "ftruncate", "fallocate"}:
                truncated_path = _fd_or_path(arguments, result_text, cwd)
                if truncated_path is None:
                    raise ValueError("truncate syscall had no decoded path")
                file_event(line, "truncate", truncated_path)
            elif syscall in {"sendfile", "sendfile64", "copy_file_range", "splice"}:
                paths = _all_fd_paths(arguments)
                if len(paths) < 2:
                    raise ValueError("file transfer descriptors lacked decoded paths")
                assert result is not None
                if syscall in {"sendfile", "sendfile64"}:
                    output_path, input_path = paths[0], paths[1]
                else:
                    input_path, output_path = paths[0], paths[1]
                if input_path.startswith("/"):
                    file_event(line, "read", input_path, bytes_transferred=result)
                if output_path.startswith("/"):
                    file_event(line, "write", output_path, bytes_transferred=result)
            elif syscall.startswith("io_uring_"):
                issues.append(
                    f"pid {line.pid} line {line.line_number}: io_uring I/O is outside v1 decoding"
                )
            elif syscall not in _IGNORED_CALLS:
                issues.append(f"pid {line.pid} line {line.line_number}: unsupported {syscall}")
        except (SyntaxError, ValueError) as exc:
            issues.append(f"pid {line.pid} line {line.line_number}: {syscall}: {exc}")

    roots = sorted(process_ids - child_pids)
    if len(roots) == 1:
        root_pid = roots[0]
    else:
        root_pid = min(process_ids)
        issues.append(f"ambiguous traced root process set: {roots}")
    if not any(event.operation == "exec" for event in process_events):
        issues.append("trace contains no successful exec event")
    exited_pids = {event.pid for event in process_events if event.operation == "exit"}
    missing_exits = sorted(process_ids - exited_pids)
    if missing_exits:
        issues.append(f"trace contains no terminal event for PID(s): {missing_exits}")
    missing_raw_traces = sorted(process_ids - {int(name[4:]) for name in digests})
    if missing_raw_traces:
        issues.append(f"spawned PID(s) have no raw trace file: {missing_raw_traces}")

    return LinuxEventTrace(
        strace_version=strace_version,
        syscall_filter=STRACE_SYSCALLS,
        root_pid=root_pid,
        process_ids=tuple(sorted(process_ids)),
        process_events=tuple(process_events),
        file_events=tuple(file_events),
        raw_trace_sha256=digests,
        collection_complete=not issues,
        issues=tuple(issues),
    )


__all__ = ["LinuxTraceParseError", "STRACE_SYSCALLS", "parse_strace_trace"]
