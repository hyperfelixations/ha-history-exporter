"""Audit publishable files, distributions, and Git history for private data."""

from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL = re.compile(r"https?://[^\s\"'<>`)]+", re.IGNORECASE)
WINDOWS_USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+", re.IGNORECASE)
POSIX_USER_PATH = re.compile(r"/(?:home|Users)/([^/\s]+)")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b")
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{20,}\b", re.IGNORECASE)
KNOWN_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{40,})\b"
)
SECRET_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "auth", "password", "secret", "signature", "token"}
)
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "users.noreply.github.com"}
)
ALLOWED_HOME_NAMES = frozenset({"you", "user", "runner"})
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    rule: str


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


def scan_text(text: str, location: str, denylist: tuple[str, ...] = ()) -> set[Finding]:
    findings: set[Finding] = set()
    if WINDOWS_USER_PATH.search(text):
        findings.add(Finding(location, "absolute_user_path"))
    for match in POSIX_USER_PATH.finditer(text):
        if match.group(1).lower() not in ALLOWED_HOME_NAMES:
            findings.add(Finding(location, "absolute_user_path"))
    for match in EMAIL.finditer(text):
        domain = match.group(0).rsplit("@", 1)[1].lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            findings.add(Finding(location, "personal_email"))
    for match in URL.finditer(text):
        try:
            parsed = urlsplit(match.group(0))
            host = parsed.hostname
        except ValueError:
            continue
        if parsed.username is not None or parsed.password is not None:
            findings.add(Finding(location, "url_userinfo"))
        query_keys = {
            item.split("=", 1)[0].lower()
            for item in parsed.query.split("&")
            if item
        }
        if query_keys & SECRET_QUERY_KEYS:
            findings.add(Finding(location, "secret_url_query"))
        if host:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                documented = any(address in network for network in DOCUMENTATION_NETWORKS)
                if address.is_private and not documented and not address.is_loopback:
                    findings.add(Finding(location, "private_network_endpoint"))
    for pattern, rule in (
        (JWT, "jwt"),
        (BEARER, "bearer_token"),
        (KNOWN_TOKEN, "known_token_shape"),
    ):
        if pattern.search(text):
            findings.add(Finding(location, rule))
    lowered = text.casefold()
    for index, literal in enumerate(denylist, start=1):
        if literal.casefold() in lowered:
            findings.add(Finding(location, f"external_denylist_{index}"))
    return findings


def _scan_bytes(data: bytes, location: str, denylist: tuple[str, ...]) -> set[Finding]:
    if not _is_text(data):
        return set()
    return scan_text(data.decode("utf-8", errors="replace"), location, denylist)


def scan_paths(root: Path, denylist: tuple[str, ...] = ()) -> set[Finding]:
    findings: set[Finding] = set()
    if (root / ".git").exists():
        listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
        candidates = [root / value for value in listed.decode("utf-8").splitlines()]
    else:
        candidates = list(root.rglob("*"))
    for path in sorted(candidates):
        relative = path.relative_to(root)
        if not path.is_file() or any(
            part in EXCLUDED_PARTS for part in relative.parts
        ):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        findings.update(_scan_bytes(path.read_bytes(), relative.as_posix(), denylist))
    return findings


def scan_archive(path: Path, denylist: tuple[str, ...] = ()) -> set[Finding]:
    findings: set[Finding] = set()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.endswith("/"):
                    findings.update(_scan_bytes(archive.read(name), name, denylist))
        return findings
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            for member in archive.getmembers():
                handle = archive.extractfile(member) if member.isfile() else None
                if handle is not None:
                    findings.update(_scan_bytes(handle.read(), member.name, denylist))
        return findings
    raise ValueError(f"Unsupported distribution type: {path.name}")


def _git(root: Path, *args: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for repository privacy auditing.")
    return subprocess.run(  # noqa: S603 - fixed executable and internal arguments
        [executable, *args],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def scan_git_history(root: Path, denylist: tuple[str, ...] = ()) -> set[Finding]:
    findings: set[Finding] = set()
    metadata = _git(
        root,
        "log",
        "--all",
        "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00",
    )
    findings.update(_scan_bytes(metadata, "git-metadata", denylist))
    revisions = _git(root, "rev-list", "--all").decode("ascii").splitlines()
    seen_blobs: set[str] = set()
    for revision in revisions:
        tree = _git(root, "ls-tree", "-r", "-z", revision)
        for record in tree.split(b"\x00"):
            if not record:
                continue
            metadata_bytes, name = record.split(b"\t", 1)
            blob = metadata_bytes.split()[2].decode("ascii")
            if blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            data = _git(root, "cat-file", "blob", blob)
            location = f"git-blob:{blob}:{name.decode('utf-8', errors='replace')}"
            findings.update(_scan_bytes(data, location, denylist))
    return findings


def load_external_denylist(path: Path | None, repository: Path) -> tuple[str, ...]:
    if path is None:
        return ()
    resolved = path.resolve()
    if resolved.is_relative_to(repository.resolve()):
        raise ValueError("The private denylist must be stored outside the repository.")
    return tuple(
        line.strip()
        for line in resolved.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--denylist-file", type=Path)
    args = parser.parse_args()
    root = args.repository.resolve()
    denylist = load_external_denylist(args.denylist_file, root)
    findings = scan_paths(root, denylist)
    if args.history:
        findings.update(scan_git_history(root, denylist))
    for artifact in args.artifact:
        findings.update(scan_archive(artifact, denylist))
    for finding in sorted(findings):
        print(f"{finding.rule}: {finding.location}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
