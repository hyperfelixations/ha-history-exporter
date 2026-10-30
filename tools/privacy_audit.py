"""Audit publishable files, distributions, and Git history for private data.

Exit 1 lists every finding by rule and location without its text; exit 2 rejects the
acknowledged history findings. See internal dev doc § "Privacy audit".
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Iterator
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
KNOWN_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|pypi-[A-Za-z0-9_-]{40,})\b")
SECRET_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "auth", "password", "secret", "signature", "token"}
)
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "users.noreply.github.com"}
)
#: GitHub commits merges and Dependabot updates, and Dependabot signs them off, under these.
ALLOWED_EMAILS = frozenset({"noreply@github.com", "support@github.com"})
ALLOWED_HOME_NAMES = frozenset({"you", "user", "runner"})
DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
#: Every rule except the numbered ones of the external denylist.
RULES = (
    "absolute_user_path",
    "personal_email",
    "url_userinfo",
    "secret_url_query",
    "private_network_endpoint",
    "jwt",
    "bearer_token",
    "known_token_shape",
)
ACKNOWLEDGED_FILE = ".github/privacy-audit-acknowledged.txt"
ACKNOWLEDGED_LINE = re.compile(r"([0-9a-f]{40}):([a-z0-9_]+)\s+#\s*\S.*")
#: Fields per commit in the ``git log`` output of scan_git_history().
COMMIT_FIELDS = 6


@dataclass(frozen=True, order=True)
class Finding:
    location: str
    rule: str


@dataclass(frozen=True)
class Acknowledged:
    """A reviewed history finding: a blob outside HEAD or a commit, and one rule."""

    object_id: str
    rule: str
    line: int

    def covers(self, finding: Finding) -> bool:
        return finding.rule == self.rule and (
            finding.location.startswith(f"git-blob:{self.object_id}:")
            or finding.location == f"git-commit:{self.object_id}"
        )


class AcknowledgedError(ValueError):
    """The acknowledged findings are malformed, unsafe or out of date."""

    def __init__(self, line: int, problem: str) -> None:
        super().__init__(f"{ACKNOWLEDGED_FILE}, line {line}: {problem}")


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
        email = match.group(0).lower()
        if email.rsplit("@", 1)[1] not in ALLOWED_EMAIL_DOMAINS and email not in ALLOWED_EMAILS:
            findings.add(Finding(location, "personal_email"))
    for match in URL.finditer(text):
        try:
            parsed = urlsplit(match.group(0))
            host = parsed.hostname
        except ValueError:
            continue
        if parsed.username is not None or parsed.password is not None:
            findings.add(Finding(location, "url_userinfo"))
        query_keys = {item.split("=", 1)[0].lower() for item in parsed.query.split("&") if item}
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
        if not path.is_file() or any(part in EXCLUDED_PARTS for part in relative.parts):
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


def _git(root: Path, *args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for repository privacy auditing.")
    return subprocess.run(  # noqa: S603 - fixed executable and internal arguments
        [executable, *args],
        cwd=root,
        input=stdin,
        check=check,
        capture_output=True,
    ).stdout


def _objects(output: bytes) -> Iterator[tuple[str, str, bytes]]:
    """Name, type and content of each object in ``git cat-file --batch`` output."""
    position = 0
    while position < len(output):
        header_end = output.index(b"\n", position)
        name, kind, size = output[position:header_end].decode("ascii").split()
        start = header_end + 1
        yield name, kind, output[start : start + int(size)]
        position = start + int(size) + 1


def scan_git_history(root: Path, denylist: tuple[str, ...] = ()) -> set[Finding]:
    """Identities and message of every commit and every blob any reference reaches."""
    findings: set[Finding] = set()
    metadata = _git(
        root,
        "log",
        "--all",
        "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x00",
    )
    # Git refuses NUL in names and messages, so it separates the fields unambiguously.
    fields = metadata.decode("utf-8", errors="replace").split("\x00")
    if (len(fields) - 1) % COMMIT_FIELDS:
        raise RuntimeError("Unexpected git log output.")
    for start in range(0, len(fields) - 1, COMMIT_FIELDS):
        sha, *details = fields[start : start + COMMIT_FIELDS]
        findings.update(scan_text("\n".join(details), f"git-commit:{sha.strip()}", denylist))
    paths: dict[str, str] = {}
    for line in _git(root, "rev-list", "--objects", "--all").decode("utf-8").splitlines():
        name, _, path = line.partition(" ")
        if path:
            paths.setdefault(name, path)
    request = "".join(f"{name}\n" for name in paths).encode("ascii")
    kinds = _git(root, "cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=request)
    blobs = [
        name
        for name, kind in (line.split() for line in kinds.decode("ascii").splitlines())
        if kind == "blob"
    ]
    contents = _git(root, "cat-file", "--batch", stdin="".join(f"{b}\n" for b in blobs).encode())
    for name, _, data in _objects(contents):
        findings.update(_scan_bytes(data, f"git-blob:{name}:{paths[name]}", denylist))
    return findings


def _head_blobs(root: Path) -> set[str]:
    if not _git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip():
        return set()
    listing = _git(root, "ls-tree", "-r", "-z", "HEAD").decode("utf-8")
    return {entry.split("\t", 1)[0].split()[2] for entry in listing.split("\x00") if entry}


def _object_types(root: Path, object_ids: list[str]) -> dict[str, str]:
    """``blob``, ``commit``, another type or ``missing`` for each id."""
    if not object_ids:
        return {}
    request = "".join(f"{object_id}\n" for object_id in object_ids).encode("ascii")
    output = _git(root, "cat-file", "--batch-check", stdin=request).decode("ascii")
    return {
        object_id: line.split()[1]
        for object_id, line in zip(object_ids, output.splitlines(), strict=True)
    }


def load_acknowledged(root: Path) -> list[Acknowledged]:
    """The entries of ACKNOWLEDGED_FILE: ``<40-hex id>:<rule>  # <reason>`` per line."""
    path = root / ACKNOWLEDGED_FILE
    if not path.is_file():
        return []
    entries: list[Acknowledged] = []
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = text.strip()
        if not line or line.startswith("#"):
            continue
        match = ACKNOWLEDGED_LINE.fullmatch(line)
        if match is None:
            raise AcknowledgedError(number, 'expected "<40-hex id>:<rule>  # <reason>"')
        object_id, rule = match.groups()
        if rule.startswith("external_denylist_"):
            raise AcknowledgedError(number, "the private denylist is never acknowledged")
        if rule not in RULES:
            raise AcknowledgedError(number, f"unknown rule {rule!r}")
        if any(entry.object_id == object_id and entry.rule == rule for entry in entries):
            raise AcknowledgedError(number, f"{object_id}:{rule} is listed twice")
        entries.append(Acknowledged(object_id, rule, number))
    types = _object_types(root, [entry.object_id for entry in entries])
    head = _head_blobs(root)
    for entry in entries:
        if types[entry.object_id] not in {"blob", "commit"}:
            raise AcknowledgedError(entry.line, f"{entry.object_id} is neither a blob nor a commit")
        if entry.object_id in head:
            raise AcknowledgedError(
                entry.line, f"{entry.object_id} is part of HEAD; fix the file instead"
            )
    return entries


def apply_acknowledged(
    findings: set[Finding], entries: list[Acknowledged]
) -> tuple[set[Finding], int]:
    """The history findings no entry covers, and how many were covered."""
    used: set[Acknowledged] = set()
    remaining: set[Finding] = set()
    for finding in findings:
        covering = [entry for entry in entries if entry.covers(finding)]
        used.update(covering)
        if not covering:
            remaining.add(finding)
    for entry in entries:
        if entry not in used:
            raise AcknowledgedError(
                entry.line, f"{entry.object_id}:{entry.rule} suppresses no finding; remove it"
            )
    return remaining, len(findings) - len(remaining)


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
        try:
            history, _ = apply_acknowledged(
                scan_git_history(root, denylist), load_acknowledged(root)
            )
        except AcknowledgedError as error:
            print(error)
            return 2
        findings.update(history)
    for artifact in args.artifact:
        findings.update(scan_archive(artifact, denylist))
    for finding in sorted(findings):
        print(f"{finding.rule}: {finding.location}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
