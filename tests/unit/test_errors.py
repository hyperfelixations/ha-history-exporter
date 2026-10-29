from __future__ import annotations

import io
from pathlib import Path

import pytest

from ha_history_exporter.cli import output as console
from ha_history_exporter.errors import (
    AuthError,
    ConfigError,
    CredentialsError,
    ErrorCode,
    ErrorRecord,
    ExitCode,
    ExportError,
    HAAPIError,
    HAConnectionError,
    HHEError,
    InternalError,
    Remedy,
    TLSError,
    UsageError,
    ValidationError,
    env_assignment,
    safe_error_record,
)
from ha_history_exporter.settings import resolve
from ha_history_exporter.settings.paths import ENV_CONFIG_DIR


class FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        (HHEError, ExitCode.INTERNAL),
        (InternalError, ExitCode.INTERNAL),
        (UsageError, ExitCode.USAGE),
        (ConfigError, ExitCode.USAGE),
        (CredentialsError, ExitCode.USAGE),
        (AuthError, ExitCode.AUTHENTICATION),
        (HAConnectionError, ExitCode.HOME_ASSISTANT),
        (HAAPIError, ExitCode.HOME_ASSISTANT),
        (TLSError, ExitCode.HOME_ASSISTANT),
        (ValidationError, ExitCode.FAILED),
        (ExportError, ExitCode.FAILED),
    ],
)
def test_exit_codes_are_declared_per_error_type(
    error_type: type[HHEError], expected_code: ExitCode
) -> None:
    assert error_type("synthetic").exit_code is expected_code


def test_exit_codes_are_the_documented_numbers() -> None:
    assert [int(code) for code in ExitCode] == [0, 1, 2, 3, 4, 70, 130]


def test_persisted_error_codes_keep_their_values() -> None:
    assert {code.value for code in ErrorCode} == {
        "usage_error",
        "configuration_error",
        "credentials_error",
        "authentication_error",
        "home_assistant_connection_error",
        "home_assistant_api_error",
        "tls_error",
        "validation_error",
        "export_error",
        "unexpected_error",
        "interrupted",
    }
    assert ConfigError("synthetic").code is ErrorCode.CONFIGURATION


def test_records_never_carry_arbitrary_exception_text() -> None:
    assert safe_error_record(ValueError("synthetic-private-payload")) == ErrorRecord(
        ErrorCode.UNEXPECTED, "Unexpected internal error."
    )
    assert safe_error_record(KeyboardInterrupt()).code is ErrorCode.INTERRUPTED
    assert safe_error_record(AuthError("Rejected.")) == ErrorRecord(
        ErrorCode.AUTHENTICATION, "Rejected."
    )


def test_credentials_error_stays_a_config_error() -> None:
    """Callers that catch ConfigError must keep catching missing credentials."""
    assert issubclass(CredentialsError, ConfigError)


def test_str_returns_the_summary_so_manifests_stay_readable() -> None:
    exc = HHEError("something broke", details="long details", remedies=(Remedy("fix"),))
    assert str(exc) == "something broke"


def test_render_error_formats_summary_details_remedies_and_context() -> None:
    stream = io.StringIO()
    exc = ConfigError(
        "Config file not found: <path>/absent.toml",
        details="HHE was asked to read this file, but it does not exist.",
        remedies=(
            Remedy("Copy the bundled example:", "cp a.toml b.toml"),
            Remedy("Or pass an existing file."),
        ),
        context={"config_file": "<path>/absent.toml"},
    )

    console.render_error(exc, stream=stream)

    assert stream.getvalue() == (
        "\n"
        "error: Config file not found: <path>/absent.toml\n"
        "\n"
        "  HHE was asked to read this file, but it does not exist.\n"
        "\n"
        "  Copy the bundled example:\n"
        "      cp a.toml b.toml\n"
        "\n"
        "  Or pass an existing file.\n"
        "\n"
        "  Configuration file: <path>/absent.toml\n"
        "\n"
    )


def test_render_error_without_details_remedies_or_context() -> None:
    stream = io.StringIO()
    console.render_error(HHEError("plain failure"), stream=stream)
    assert stream.getvalue() == "\nerror: plain failure\n\n"


def test_render_error_wraps_long_details() -> None:
    stream = io.StringIO()
    console.render_error(HHEError("short", details="word " * 60), stream=stream)
    body = [line for line in stream.getvalue().splitlines() if line.startswith("  ")]
    assert len(body) > 1
    assert all(len(line) <= console.WIDTH for line in body)


def test_render_error_shows_the_safe_url_and_drops_unregistered_context_keys() -> None:
    """A validated base URL is useful context; arbitrary values stay hidden."""
    stream = io.StringIO()
    console.render_error(
        HHEError(
            "boom",
            context={
                "token": "synthetic-secret-value",
                "raw_payload": "synthetic-private-payload",
                "url": "http://home-assistant.invalid",
            },
        ),
        stream=stream,
    )
    rendered = stream.getvalue()
    assert "synthetic-secret-value" not in rendered
    assert "synthetic-private-payload" not in rendered
    assert "http://home-assistant.invalid" in rendered


def test_color_is_used_only_on_a_tty_without_no_color() -> None:
    tty = FakeTty()
    console.render_error(HHEError("boom"), stream=tty, environ={})
    assert "\033[" in tty.getvalue()

    plain = io.StringIO()
    console.render_error(HHEError("boom"), stream=plain, environ={})
    assert "\033[" not in plain.getvalue()


@pytest.mark.parametrize(
    ("variable", "value", "colored"),
    [
        ("NO_COLOR", "1", False),
        ("HHE_NO_COLOR", "1", False),
        ("NO_COLOR", "", True),
    ],
)
def test_color_is_suppressed_by_a_non_empty_variable(
    variable: str, value: str, colored: bool
) -> None:
    tty = FakeTty()
    console.render_error(HHEError("boom"), stream=tty, environ={variable: value})
    assert ("\033[" in tty.getvalue()) is colored


def test_missing_credentials_carry_platform_commands_with_placeholders(tmp_path: Path) -> None:
    environ = {ENV_CONFIG_DIR: str(tmp_path / "config")}
    with pytest.raises(CredentialsError) as missing_url:
        resolve(environ=environ, cwd=tmp_path).connection()
    assert any(remedy.command for remedy in missing_url.value.remedies)

    environ["HHE_URL"] = "http://home-assistant.invalid"
    with pytest.raises(CredentialsError) as missing_token:
        resolve(environ=environ, cwd=tmp_path).connection()
    stream = io.StringIO()
    console.render_error(missing_token.value, stream=stream, environ={})
    assert env_assignment("HHE_TOKEN", "<token>") in stream.getvalue()
