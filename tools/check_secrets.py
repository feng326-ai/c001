#!/usr/bin/env python3
"""Scan tracked text without ever printing a matched credential value."""

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


MAX_FILE_SIZE = 1_000_000
PLACEHOLDER_MARKERS = (
    "${",
    "{{",
    "<",
    "replace-with-",
    "your_",
    "your-",
    "changeme",
    "change-me",
    "example",
    "placeholder",
    "dummy",
    "test-only",
    "qa-",
    "ci-",
    "disabled",
)
SAFE_EXPRESSIONS = (
    "none",
    "null",
    "true",
    "false",
    "os.",
    "env.",
    "getenv(",
    "settings.",
    "config.",
)
PLACEHOLDER_VALUES = {
    "pass",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "user",
}
UNQUOTED_CONFIG_SUFFIXES = {".env", ".yml", ".yaml", ".toml"}
FORBIDDEN_TRACKED_NAMES = {".env", "config.json", "secrets.json"}

TOKEN_RULES = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b")),
)
QUOTED_ASSIGNMENT = re.compile(
    r"(?i)(?:^|[^A-Za-z0-9])(?:password|passwd|secret|token|api[_-]?key)\b"
    r"\s*[:=]\s*([\"'])(?P<value>[^\"']*)\1"
)
UNQUOTED_ASSIGNMENT = re.compile(
    r"(?i)^\s*(?:[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY)"
    r"|password|passwd|secret|token|api[_-]?key)\s*[:=]\s*(?P<value>[^#\s]+)"
)
URL_CREDENTIAL = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://"
    r"(?:\$\{[^}\r\n]+\}|[^\s/:@]+):"
    r"(?P<value>(?:\$\{[^}\r\n]+\}|[^\s/@]+))@"
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").lower()
    if not normalized:
        return True
    if normalized in {"0", "1", "***", "xxxxx"} | PLACEHOLDER_VALUES:
        return True
    return normalized.startswith(PLACEHOLDER_MARKERS + SAFE_EXPRESSIONS)


def scan_file(path: Path) -> list[Finding]:
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\x00" in raw:
        return []
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []

    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in TOKEN_RULES:
            if pattern.search(line):
                findings.append(Finding(path, line_number, rule))
        patterns = [
            ("QUOTED_SECRET", QUOTED_ASSIGNMENT),
            ("URL_CREDENTIAL", URL_CREDENTIAL),
        ]
        if path.suffix.lower() in UNQUOTED_CONFIG_SUFFIXES or path.name.startswith(".env"):
            patterns.append(("CONFIG_SECRET", UNQUOTED_ASSIGNMENT))
        for rule, pattern in patterns:
            for match in pattern.finditer(line):
                if not _is_placeholder(match.group("value")):
                    findings.append(Finding(path, line_number, rule))
    return findings


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        root / item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def scan_paths(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        findings.extend(scan_file(path))
    return findings


def tracked_file_policy(paths: list[Path]) -> list[Finding]:
    findings = []
    for path in paths:
        name = path.name.lower()
        if name in FORBIDDEN_TRACKED_NAMES or path.suffix.lower() in {
            ".key",
            ".pem",
            ".p12",
            ".pfx",
        }:
            findings.append(Finding(path, 0, "TRACKED_SECRET_FILE"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="explicit files; default is every Git-tracked file",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    paths = args.paths or tracked_paths(root)
    findings = scan_paths(paths)
    if not args.paths:
        findings.extend(tracked_file_policy(paths))
    if findings:
        for finding in findings:
            try:
                display_path = finding.path.relative_to(root)
            except ValueError:
                display_path = finding.path
            print(
                f"[{finding.rule}] {display_path}:{finding.line} "
                "(value redacted)"
            )
        print(f"Secret scan failed: {len(findings)} finding(s); values redacted")
        return 1
    print(f"Secret scan OK: {len(paths)} tracked file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
