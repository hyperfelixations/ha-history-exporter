"""The installed command in a process of its own, against a local Home Assistant stand-in."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ha_history_exporter import __version__
from ha_history_exporter.archive.layout import ArchiveLayout
from ha_history_exporter.archive.lock import FileLock
from ha_history_exporter.settings.paths import ENV_CONFIG_DIR

TOKEN = "synthetic-test-token"

pytestmark = pytest.mark.enable_socket


class _HomeAssistant(BaseHTTPRequestHandler):
    """``/api/``, ``/api/config``, ``/api/states`` and an empty history."""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"message": "Unauthorized"})
        elif self.path == "/api/":
            self._send(200, {"message": "API running."})
        elif self.path == "/api/config":
            self._send(200, {"time_zone": "Europe/Berlin", "version": "2026.10.0"})
        elif self.path == "/api/states":
            self._send(200, [{"entity_id": "sensor.synthetic", "state": "1", "attributes": {}}])
        elif self.path.startswith("/api/history/period/"):
            self._send(200, [])
        else:
            self._send(404, {"message": "Not found"})

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def home_assistant() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HomeAssistant)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "process"
    return {
        **os.environ,
        ENV_CONFIG_DIR: str(root / "config"),
        "HHE_TOKEN": TOKEN,
        "HHE_EXPORT_OUTPUT_DIR": str(root / "archive"),
        "HHE_STORAGE_TEMP_DIR": str(root / "temp"),
        "HHE_RECORDER_PURGE_KEEP_DAYS": "14",
        "HHE_REQUESTS_MAX_RETRIES": "0",
        "HHE_REQUESTS_SLEEP_BETWEEN_REQUESTS": "0",
        "HHE_REQUESTS_SLEEP_BETWEEN_DAYS": "0",
        "NO_PROXY": "127.0.0.1,localhost",
    }


def hhe(environ: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ha_history_exporter", *argv],
        env=environ,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_module_names_itself_in_version_and_usage(environ: dict[str, str]) -> None:
    result = hhe(environ, "--version")
    assert (result.returncode, result.stdout.strip()) == (
        0,
        f"python -m ha_history_exporter {__version__}",
    )
    result = hhe(environ, "export", "--unknown")
    assert result.returncode == 2
    assert result.stderr.startswith("usage: python -m ha_history_exporter ")


def test_an_export_succeeds_and_a_second_one_has_nothing_to_do(
    environ: dict[str, str], home_assistant: str
) -> None:
    environ["HHE_URL"] = home_assistant
    result = hhe(environ, "export", "--last-days", "2")
    assert result.returncode == 0, result.stderr
    archive = ArchiveLayout(Path(environ["HHE_EXPORT_OUTPUT_DIR"]))
    assert len(list(archive.manifest_paths())) == 2
    assert archive.descriptor_file.is_file()
    assert len(list(archive.logs_dir.iterdir())) == 1
    again = hhe(environ, "export", "--last-days", "2")
    assert again.returncode == 0, again.stderr
    assert "Nothing to export" in again.stderr


def test_days_outside_the_retention_are_skipped_and_the_process_exits_with_one(
    environ: dict[str, str], home_assistant: str
) -> None:
    environ.update({"HHE_URL": home_assistant, "HHE_RECORDER_PURGE_KEEP_DAYS": "2"})
    result = hhe(environ, "export", "--last-days", "3")
    assert result.returncode == 1, result.stderr
    assert "Skipped 2 day(s) outside the 2-day Recorder retention" in result.stderr
    assert "skipped  outside the Recorder retention" in result.stdout
    archive = ArchiveLayout(Path(environ["HHE_EXPORT_OUTPUT_DIR"]))
    assert len(list(archive.manifest_paths())) == 1


def test_a_dry_run_leaves_no_trace(environ: dict[str, str], home_assistant: str) -> None:
    environ["HHE_URL"] = home_assistant
    result = hhe(environ, "export", "--last-days", "3", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Will export (3):" in result.stdout
    assert not Path(environ["HHE_EXPORT_OUTPUT_DIR"]).exists()
    assert not Path(environ["HHE_STORAGE_TEMP_DIR"]).exists()


def test_a_rejected_token_exits_with_three(environ: dict[str, str], home_assistant: str) -> None:
    environ.update({"HHE_URL": home_assistant, "HHE_TOKEN": "wrong-synthetic-token"})
    result = hhe(environ, "export")
    assert result.returncode == 3
    assert "rejected the access token (HTTP 401)" in result.stderr
    assert "wrong-synthetic-token" not in result.stderr + result.stdout


def test_an_unreachable_home_assistant_exits_with_four(environ: dict[str, str]) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    environ["HHE_URL"] = f"http://127.0.0.1:{port}"
    result = hhe(environ, "export", "--timeout", "5")
    assert result.returncode == 4
    assert "not available" in result.stderr


def test_a_wrong_value_exits_with_two(environ: dict[str, str]) -> None:
    environ["HHE_REQUESTS_BATCH_SIZE"] = "0"
    result = hhe(environ, "export")
    assert result.returncode == 2
    assert "requests.batch_size from HHE_REQUESTS_BATCH_SIZE" in result.stderr


def test_a_second_process_on_the_same_archive_is_refused(
    environ: dict[str, str], home_assistant: str
) -> None:
    environ["HHE_URL"] = home_assistant
    archive = ArchiveLayout(Path(environ["HHE_EXPORT_OUTPUT_DIR"]))
    with FileLock(archive.lock_file, "run_id: holding-test\n"):
        result = hhe(environ, "export")
    assert result.returncode == 1
    assert "Another export is already running" in result.stderr
    assert "run_id: holding-test" in " ".join(result.stderr.split())


def test_the_archive_commands_work_on_what_an_export_wrote(
    environ: dict[str, str], home_assistant: str
) -> None:
    environ["HHE_URL"] = home_assistant
    assert hhe(environ, "export", "--last-days", "2").returncode == 0
    del environ["HHE_URL"]  # none of them needs Home Assistant

    converted = hhe(environ, "convert", "--format", "csv", "--json")
    assert converted.returncode == 0, converted.stderr
    assert json.loads(converted.stdout)["summary"]["converted"] == 2
    verified = hhe(environ, "verify", "--deep")
    assert verified.returncode == 0, verified.stderr
    assert "2 day(s) decoded and checksummed: 4 ok." in verified.stdout
    status = hhe(environ, "status", "--json")
    assert status.returncode == 0, status.stderr
    assert len(json.loads(status.stdout)["months"]) >= 1


def test_setup_without_questions_takes_the_token_from_the_environment(
    environ: dict[str, str], home_assistant: str
) -> None:
    result = hhe(
        environ, "init", "--non-interactive", "--url", home_assistant, "--purge-keep-days", "14"
    )
    assert result.returncode == 0, result.stderr
    credentials = Path(environ[ENV_CONFIG_DIR]) / "credentials.toml"
    assert TOKEN in credentials.read_text(encoding="utf-8")
    assert TOKEN not in result.stdout + result.stderr
