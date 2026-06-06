#!/usr/bin/env python3
"""Development checks used by local Git hooks."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath


PRIVATE_DATA_DIRS = frozenset({"collection", "config", "export", "processed", "reports"})
TEXT_FILE_SUFFIXES = frozenset({".cfg", ".ini", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"})
TEXT_FILE_PATHS = frozenset({".githooks/pre-commit", ".gitignore", "AGENTS.md"})


@dataclass(frozen=True)
class Finding:
    path: str
    line_number: int | None
    message: str

    def format(self) -> str:
        if self.line_number is None:
            return f"{self.path}: {self.message}"
        return f"{self.path}:{self.line_number}: {self.message}"


def is_checkable_text_path(path: str) -> bool:
    path_parts = PurePosixPath(path).parts
    if not path_parts:
        return False
    if path_parts[0] in PRIVATE_DATA_DIRS:
        return False
    if "__pycache__" in path_parts:
        return False
    if path in TEXT_FILE_PATHS:
        return True
    return PurePosixPath(path).suffix.lower() in TEXT_FILE_SUFFIXES


def check_text_content(path: str, content: bytes) -> list[Finding]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding(path, None, "file is not valid UTF-8")]

    findings: list[Finding] = []
    if text and not text.endswith("\n"):
        findings.append(Finding(path, text.count("\n") + 1, "missing final newline"))

    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.rstrip(" \t") != line:
            findings.append(Finding(path, line_number, "trailing whitespace"))

    return findings


def staged_paths() -> list[str]:
    process = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace").strip())

    return [
        path_bytes.decode("utf-8", errors="surrogateescape")
        for path_bytes in process.stdout.split(b"\0")
        if path_bytes
    ]


def staged_file_content(path: str) -> bytes:
    process = subprocess.run(
        ["git", "show", f":{path}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace").strip())
    return process.stdout


def check_staged_formatting(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if not is_checkable_text_path(path):
            continue
        findings.extend(check_text_content(path, staged_file_content(path)))
    return findings


def run_staged_checks() -> int:
    findings = check_staged_formatting(staged_paths())
    if not findings:
        return 0

    print("Formatting check failed:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.format()}", file=sys.stderr)
    return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local development checks.")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="check staged project text files for whitespace formatting issues",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.staged:
        try:
            return run_staged_checks()
        except RuntimeError as error:
            print(f"dev-checks: {error}", file=sys.stderr)
            return 1

    print("dev-checks: choose a check, for example --staged", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
