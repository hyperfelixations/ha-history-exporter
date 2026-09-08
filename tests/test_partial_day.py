"""Exporting the running day on purpose.

``hhe export --date today`` (or --date with today's date) captures the day so
far. The result is recorded as ``status: partial``, which the planner already
treats as "not exported", so the next run that includes that day replaces it
with the complete day. Nothing else reaches into an unfinished day.

No prompt is involved anywhere: an export run never reads from stdin, so a
scheduled task or a double-clicked batch file can never block on one.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_history_exporter import cli, ha_client, planner
from ha_history_exporter.cli.commands import export as export_command
from ha_history_exporter.time_utils import partial_day_bounds, today_local
from tests.helpers import FakeCliClient, make_config, state_row

BERLIN = ZoneInfo("Europe/Berlin")


def today() -> date:
    return today_local(BERLIN)


def yesterday() -> date:
    return today() - timedelta(days=1)


def write_config(config_dir, tmp_path, formats: str = "[jsonl]"):
    path = config_dir / "config.yaml"
    path.write_text(
        f"""
export:
  output_dir: "{(tmp_path / "output").as_posix()}"
  timezone: Europe/Berlin
  formats: {formats}
requests:
  batch_size: 5
  sleep_between_requests: 0
  sleep_between_days: 0
  max_retries: 0
storage:
  temp_dir: "{(tmp_path / "temp").as_posix()}"
  locked_file_retries: 0
  locked_file_retry_sleep: 0
""",
        encoding="utf-8",
    )
    return path


def synthetic_env(monkeypatch):
    monkeypatch.setenv("HA_URL", "http://home-assistant.invalid")
    monkeypatch.setenv("HA_TOKEN", "synthetic-test-token")


def day_file(tmp_path, day: date, suffix: str):
    return (
        tmp_path
        / "output"
        / "exports"
        / "daily"
        / f"{day.year}"
        / f"{day.month:02d}"
        / f"{day}.{suffix}"
    )


def manifest_of(tmp_path, day: date) -> dict:
    return json.loads(
        day_file(tmp_path, day, "manifest.json").read_text(encoding="utf-8")
    )


# ── the time window ───────────────────────────────────────────────────────────

def test_partial_bounds_run_from_local_midnight_to_now():
    now = datetime(2026, 6, 15, 14, 30, 45, 123456, tzinfo=BERLIN)
    start, end = partial_day_bounds(date(2026, 6, 15), BERLIN, now)

    assert start == datetime(2026, 6, 15, 0, 0, 0, tzinfo=BERLIN)
    assert end == datetime(2026, 6, 15, 14, 30, 45, tzinfo=BERLIN)
    assert end.microsecond == 0


def test_partial_bounds_on_a_dst_transition_day():
    """The 25-hour day still starts at its own local midnight."""
    now = datetime(2026, 10, 25, 5, 0, 0, tzinfo=BERLIN)
    start, end = partial_day_bounds(date(2026, 10, 25), BERLIN, now)

    assert start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert end.isoformat() == "2026-10-25T05:00:00+01:00"


def test_partial_bounds_refuse_any_day_but_today():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=BERLIN)
    with pytest.raises(ValueError, match="is not today"):
        partial_day_bounds(date(2026, 6, 14), BERLIN, now)


# ── what triggers it ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("spelling", ["today", "iso"])
def test_a_single_day_selection_of_today_triggers_the_partial_export(spelling):
    value = "today" if spelling == "today" else today().isoformat()
    args = cli.parse_args(["export", "--date", value])
    assert export_command.requested_partial_day(args, BERLIN) == today()


@pytest.mark.parametrize(
    "argv",
    [
        ["export"],
        ["export", "--date", "yesterday"],
        ["export", "--last-days", "3"],
        ["export", "--start-date", "2026-01-01", "--end-date", "today"],
    ],
)
def test_nothing_else_reaches_into_the_running_day(argv):
    """A range or --last-days that spans today still skips it."""
    args = cli.parse_args(argv)
    assert export_command.requested_partial_day(args, BERLIN) is None


def test_a_range_ending_today_still_skips_today(tmp_path):
    cfg = make_config(tmp_path)
    plan = planner.build_plan(
        yesterday(), today(), BERLIN, cfg, force=False, partial_day=None
    )
    assert plan.days_skipped_incomplete == [today()]
    assert plan.partial_days == []


# ── the plan ──────────────────────────────────────────────────────────────────

def test_the_planner_schedules_the_named_day_as_partial(tmp_path):
    cfg = make_config(tmp_path)
    plan = planner.build_plan(
        today(), today(), BERLIN, cfg, force=False, partial_day=today()
    )

    assert plan.days_to_export == [today()]
    assert plan.partial_days == [today()]
    assert plan.days_skipped_incomplete == []


def test_only_the_named_day_becomes_partial_never_its_neighbours(tmp_path):
    """Future days in the same range stay skipped, not partially exported.

    The command line cannot currently produce this combination, but the planner
    is the place where the rule lives, so it is pinned here.
    """
    cfg = make_config(tmp_path)
    plan = planner.build_plan(
        today(),
        today() + timedelta(days=2),
        BERLIN,
        cfg,
        force=False,
        partial_day=today(),
    )

    assert plan.partial_days == [today()]
    assert plan.days_skipped_incomplete == [
        today() + timedelta(days=1),
        today() + timedelta(days=2),
    ]


def test_a_complete_day_beside_the_partial_one_is_exported_in_full(tmp_path):
    cfg = make_config(tmp_path)
    plan = planner.build_plan(
        yesterday(), today(), BERLIN, cfg, force=False, partial_day=today()
    )

    assert plan.days_to_export == [yesterday(), today()]
    assert plan.partial_days == [today()]


def test_the_plan_summary_marks_the_partial_day(tmp_path, capsys):
    cfg = make_config(tmp_path)
    plan = planner.build_plan(
        today(), today(), BERLIN, cfg, force=False, partial_day=today()
    )
    plan.print_summary(
        entity_count=1, unknown_count=0, unavailable_count=0, batch_size=5
    )
    assert "partial - today is not over yet" in capsys.readouterr().out


# ── the run ───────────────────────────────────────────────────────────────────

def test_the_partial_day_is_written_and_marked_partial(
    isolated_user_environment, tmp_path, monkeypatch
):
    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    FakeCliClient.history_payload = [
        [state_row("sensor.synthetic", "1", f"{today()}T06:00:00+00:00")]
    ]
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--date", "today"]) == 0

    manifest = manifest_of(tmp_path, today())
    assert manifest["status"] == "partial"
    assert manifest["state_object_count"] == 1
    assert day_file(tmp_path, today(), "jsonl").is_file()


def test_the_manifest_records_how_far_the_partial_day_reaches(
    isolated_user_environment, tmp_path, monkeypatch
):
    """end_utc is the cut-off, so no extra field is needed to document it."""
    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--date", "today"]) == 0

    manifest = manifest_of(tmp_path, today())
    end = datetime.fromisoformat(manifest["end_utc"])
    start = datetime.fromisoformat(manifest["start_utc"])
    assert start < end < datetime.now(BERLIN) + timedelta(seconds=5)
    assert (end - start) < timedelta(hours=25)


def test_the_run_warns_before_and_after(
    isolated_user_environment, tmp_path, monkeypatch, caplog
):
    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    with caplog.at_level("WARNING"):
        assert cli.main(["export", "--date", "today"]) == 0

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("is not over yet" in message for message in warnings)
    assert any("status=partial" in message for message in warnings)


def test_a_partial_day_never_reads_from_standard_input(
    isolated_user_environment, tmp_path, monkeypatch
):
    """An export run must never block on a prompt; a scheduled task cannot answer."""

    class ForbiddenStdin:
        def read(self, *_args):
            raise AssertionError("an export run must not read stdin")

        def readline(self, *_args):
            raise AssertionError("an export run must not read stdin")

        def isatty(self):
            return True

    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)
    monkeypatch.setattr("sys.stdin", ForbiddenStdin())

    assert cli.main(["export", "--date", "today"]) == 0


def test_a_dry_run_shows_the_partial_day_and_fetches_nothing(
    isolated_user_environment, tmp_path, monkeypatch, capsys
):
    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--date", "today", "--dry-run"]) == 0

    assert FakeCliClient.instances[0].history_calls == []
    assert "partial - today is not over yet" in capsys.readouterr().out


# ── and the day heals itself ──────────────────────────────────────────────────

def test_a_partial_day_is_exported_again_in_full_later(
    isolated_user_environment, tmp_path, monkeypatch
):
    """The whole point: partial is not done, so a later run replaces it."""
    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--date", "today"]) == 0
    assert manifest_of(tmp_path, today())["status"] == "partial"
    partial_bytes = day_file(tmp_path, today(), "jsonl").read_bytes()

    # The next day, the same day is complete and carries more data.
    FakeCliClient.reset()
    FakeCliClient.history_payload = [
        [
            state_row("sensor.synthetic", "1", f"{today()}T06:00:00+00:00"),
            state_row("sensor.synthetic", "2", f"{today()}T20:00:00+00:00"),
        ]
    ]
    cfg = make_config(tmp_path)
    plan = planner.build_plan(
        today(), today(), BERLIN, cfg, force=False, partial_day=today()
    )
    assert plan.days_to_export == [today()], "a partial day counts as not exported"

    assert cli.main(["export", "--date", "today"]) == 0
    assert day_file(tmp_path, today(), "jsonl").read_bytes() != partial_bytes


def test_a_successful_day_is_still_skipped(
    isolated_user_environment, tmp_path, monkeypatch
):
    """The partial path must not weaken the ordinary resume decision."""
    write_config(isolated_user_environment, tmp_path)
    synthetic_env(monkeypatch)
    monkeypatch.setattr(ha_client, "HomeAssistantClient", FakeCliClient)

    assert cli.main(["export", "--date", "yesterday"]) == 0
    assert manifest_of(tmp_path, yesterday())["status"] == "ok"

    calls_before = len(FakeCliClient.instances)
    assert cli.main(["export", "--date", "yesterday"]) == 0
    assert len(FakeCliClient.instances) == calls_before
