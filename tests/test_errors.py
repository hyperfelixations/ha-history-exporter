from __future__ import annotations

import io

import pytest

from ha_history_exporter import console
from ha_history_exporter.errors import (
    AuthError,
    ConfigError,
    CredentialsError,
    ExportError,
    HAAPIError,
    HAConnectionError,
    HHEError,
    Remedy,
    UsageError,
    ValidationError,
)


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        (HHEError, 1),
        (UsageError, 2),
        (ConfigError, 2),
        (CredentialsError, 2),
        (AuthError, 1),
        (HAConnectionError, 1),
        (HAAPIError, 1),
        (ValidationError, 1),
        (ExportError, 1),
    ],
)
def test_exit_codes_are_declared_per_error_type(error_type, expected_code):
    assert error_type("synthetic").exit_code == expected_code


def test_credentials_error_stays_a_config_error():
    """Callers that catch ConfigError must keep catching missing credentials."""
    assert issubclass(CredentialsError, ConfigError)


def test_str_returns_the_summary_so_manifests_stay_readable():
    exc = HHEError("something broke", details="long details", remedies=(Remedy("fix"),))
    assert str(exc) == "something broke"


def test_render_error_formats_summary_details_remedies_and_context():
    stream = io.StringIO()
    exc = ConfigError(
        "Config file not found: /tmp/absent.yaml",
        details="HHE was asked to read this file, but it does not exist.",
        remedies=(
            Remedy("Copy the bundled example:", "cp a.yaml b.yaml"),
            Remedy("Or pass an existing file."),
        ),
        context={"config_file": "/tmp/absent.yaml"},
    )

    console.render_error(exc, stream=stream)

    assert stream.getvalue() == (
        "\n"
        "error: Config file not found: /tmp/absent.yaml\n"
        "\n"
        "  HHE was asked to read this file, but it does not exist.\n"
        "\n"
        "  Copy the bundled example:\n"
        "      cp a.yaml b.yaml\n"
        "\n"
        "  Or pass an existing file.\n"
        "\n"
        "  Configuration file: /tmp/absent.yaml\n"
        "\n"
    )


def test_render_error_without_details_remedies_or_context():
    stream = io.StringIO()
    console.render_error(HHEError("plain failure"), stream=stream)
    assert stream.getvalue() == "\nerror: plain failure\n\n"


def test_render_error_wraps_long_details():
    stream = io.StringIO()
    console.render_error(
        HHEError("short", details="word " * 60), stream=stream
    )
    body = [line for line in stream.getvalue().splitlines() if line.startswith("  ")]
    assert len(body) > 1
    assert all(len(line) <= console.WIDTH for line in body)


def test_render_error_drops_unregistered_context_keys():
    """An unexpected context key must never reach the terminal."""
    stream = io.StringIO()
    console.render_error(
        HHEError(
            "boom",
            context={"token": "synthetic-secret-value", "url": "http://ha.invalid"},
        ),
        stream=stream,
    )
    rendered = stream.getvalue()
    assert "synthetic-secret-value" not in rendered
    assert "http://ha.invalid" in rendered


def test_color_is_used_only_on_a_tty_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HHE_NO_COLOR", raising=False)

    tty = FakeTty()
    console.render_error(HHEError("boom"), stream=tty)
    assert "\033[" in tty.getvalue()

    plain = io.StringIO()
    console.render_error(HHEError("boom"), stream=plain)
    assert "\033[" not in plain.getvalue()


@pytest.mark.parametrize("variable", ["NO_COLOR", "HHE_NO_COLOR"])
def test_color_is_suppressed_by_environment(monkeypatch, variable):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("HHE_NO_COLOR", raising=False)
    monkeypatch.setenv(variable, "1")

    tty = FakeTty()
    console.render_error(HHEError("boom"), stream=tty)
    assert "\033[" not in tty.getvalue()


def test_configuration_errors_carry_actionable_remedies(tmp_path, monkeypatch):
    """Every error a first-time user can hit must offer a way forward."""
    from ha_history_exporter.config import load_config

    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)

    with pytest.raises(ConfigError) as missing_file:
        load_config(tmp_path / "absent.yaml")
    assert missing_file.value.remedies

    path = tmp_path / "config.yaml"
    path.write_text("home_assistant:\n  timezone: Europe/Berlin\n", encoding="utf-8")

    with pytest.raises(CredentialsError) as missing_url:
        load_config(path)
    assert missing_url.value.remedies
    assert any(remedy.command for remedy in missing_url.value.remedies)

    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    with pytest.raises(CredentialsError) as missing_token:
        load_config(path)
    assert missing_token.value.remedies
    assert "synthetic" not in str(missing_token.value)


def test_no_error_remedy_ever_embeds_a_real_token(monkeypatch, tmp_path):
    """Remedies show placeholders, never a configured secret."""
    from ha_history_exporter.config import load_config

    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.delenv("HA_TOKEN", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text("home_assistant:\n  timezone: Europe/Berlin\n", encoding="utf-8")

    with pytest.raises(CredentialsError) as exc:
        load_config(path)

    stream = io.StringIO()
    console.render_error(exc.value, stream=stream)
    assert "<your-token>" in stream.getvalue()
