from __future__ import annotations

from pathlib import Path

import pytest

from ha_history_exporter.runtime import transaction
from ha_history_exporter.runtime.transaction import DayTransaction, recover_incomplete
from ha_history_exporter.runtime.workspace import open_workspace
from tests.helpers import make_config


def prepare_files(workspace, day="2026-07-28"):
    artifact = workspace.work_dir / f"{day}.jsonl"
    manifest = workspace.work_dir / f"{day}.manifest.json"
    artifact.write_bytes(b"new artifact")
    manifest.write_bytes(b"new manifest")
    artifact_target = workspace.output_dir / "exports" / f"{day}.jsonl"
    manifest_target = workspace.output_dir / "exports" / f"{day}.manifest.json"
    return artifact, artifact_target, manifest, manifest_target


def test_transaction_promotes_the_manifest_last_and_cleans_its_journal(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    artifact, artifact_target, manifest, manifest_target = prepare_files(workspace)
    replacements: list[Path] = []
    original = transaction._replace_file

    def record(src, dst, retries, retry_sleep):
        replacements.append(dst)
        return original(src, dst, retries, retry_sleep)

    monkeypatch.setattr(transaction, "_replace_file", record)
    try:
        DayTransaction(
            workspace=workspace,
            day="2026-07-28",
            artifacts=[(artifact, artifact_target)],
            manifest=(manifest, manifest_target),
        ).commit()

        assert artifact_target.read_bytes() == b"new artifact"
        assert manifest_target.read_bytes() == b"new manifest"
        visible = [path for path in replacements if path in {artifact_target, manifest_target}]
        assert visible == [artifact_target, manifest_target]
        assert not transaction.journal_root(workspace.output_dir).exists()
    finally:
        workspace.close()


def test_failed_force_transaction_restores_the_previous_generation_byte_exactly(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    artifact, artifact_target, manifest, manifest_target = prepare_files(workspace)
    artifact_target.parent.mkdir(parents=True)
    artifact_target.write_bytes(b"old artifact")
    manifest_target.write_bytes(b"old manifest")
    original = transaction._replace_file

    def fail_manifest(src, dst, retries, retry_sleep):
        if dst == manifest_target and ".hhe-staging" in src.parts:
            raise PermissionError("synthetic promotion failure")
        return original(src, dst, retries, retry_sleep)

    monkeypatch.setattr(transaction, "_replace_file", fail_manifest)
    try:
        with pytest.raises(PermissionError, match="synthetic"):
            DayTransaction(
                workspace=workspace,
                day="2026-07-28",
                artifacts=[(artifact, artifact_target)],
                manifest=(manifest, manifest_target),
            ).commit()

        assert artifact_target.read_bytes() == b"old artifact"
        assert manifest_target.read_bytes() == b"old manifest"
        assert not transaction.journal_root(workspace.output_dir).exists()
    finally:
        workspace.close()


def test_recovery_rolls_back_an_interrupted_commit(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    artifact, artifact_target, manifest, manifest_target = prepare_files(workspace)
    artifact_target.parent.mkdir(parents=True)
    artifact_target.write_bytes(b"old artifact")
    manifest_target.write_bytes(b"old manifest")
    original = transaction._replace_file

    def interrupt_artifact(src, dst, retries, retry_sleep):
        if dst == artifact_target and ".hhe-staging" in src.parts:
            raise KeyboardInterrupt
        return original(src, dst, retries, retry_sleep)

    monkeypatch.setattr(transaction, "_replace_file", interrupt_artifact)
    try:
        with pytest.raises(KeyboardInterrupt):
            DayTransaction(
                workspace=workspace,
                day="2026-07-28",
                artifacts=[(artifact, artifact_target)],
                manifest=(manifest, manifest_target),
            ).commit()

        assert list(transaction.journal_root(workspace.output_dir).glob("*.json"))
        monkeypatch.setattr(transaction, "_replace_file", original)
        recover_incomplete(workspace.output_dir)

        assert artifact_target.read_bytes() == b"old artifact"
        assert manifest_target.read_bytes() == b"old manifest"
        assert not transaction.journal_root(workspace.output_dir).exists()
    finally:
        workspace.close()


def test_failed_first_generation_leaves_no_visible_partial_artifact(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    artifact, artifact_target, manifest, manifest_target = prepare_files(workspace)
    original = transaction._replace_file

    def fail_manifest(src, dst, retries, retry_sleep):
        if dst == manifest_target and ".hhe-staging" in src.parts:
            raise PermissionError("synthetic promotion failure")
        return original(src, dst, retries, retry_sleep)

    monkeypatch.setattr(transaction, "_replace_file", fail_manifest)
    try:
        with pytest.raises(PermissionError):
            DayTransaction(
                workspace=workspace,
                day="2026-07-28",
                artifacts=[(artifact, artifact_target)],
                manifest=(manifest, manifest_target),
            ).commit()

        assert not artifact_target.exists()
        assert not manifest_target.exists()
    finally:
        workspace.close()


def test_post_commit_cleanup_failure_does_not_reclassify_a_committed_generation(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    artifact, artifact_target, manifest, manifest_target = prepare_files(workspace)

    def fail_cleanup(*args, **kwargs):
        raise PermissionError("synthetic cleanup failure")

    monkeypatch.setattr(transaction, "_cleanup", fail_cleanup)
    try:
        DayTransaction(
            workspace=workspace,
            day="2026-07-28",
            artifacts=[(artifact, artifact_target)],
            manifest=(manifest, manifest_target),
        ).commit()

        assert artifact_target.read_bytes() == b"new artifact"
        assert manifest_target.read_bytes() == b"new manifest"
    finally:
        workspace.close()


def test_failure_during_multi_file_backup_restores_every_original(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    first, first_target, manifest, manifest_target = prepare_files(workspace)
    second = workspace.work_dir / "2026-07-28.csv"
    second_target = workspace.output_dir / "exports" / "2026-07-28.csv"
    second.write_bytes(b"new second")
    first_target.parent.mkdir(parents=True)
    first_target.write_bytes(b"old first")
    second_target.write_bytes(b"old second")
    manifest_target.write_bytes(b"old manifest")
    original = transaction._replace_file

    def fail_second_backup(src, dst, retries, retry_sleep):
        if src == second_target and transaction.JOURNAL_DIRNAME in dst.parts:
            raise PermissionError("synthetic backup failure")
        return original(src, dst, retries, retry_sleep)

    monkeypatch.setattr(transaction, "_replace_file", fail_second_backup)
    try:
        with pytest.raises(PermissionError, match="synthetic"):
            DayTransaction(
                workspace=workspace,
                day="2026-07-28",
                artifacts=[(first, first_target), (second, second_target)],
                manifest=(manifest, manifest_target),
            ).commit()

        assert first_target.read_bytes() == b"old first"
        assert second_target.read_bytes() == b"old second"
        assert manifest_target.read_bytes() == b"old manifest"
        assert not transaction.journal_root(workspace.output_dir).exists()
    finally:
        workspace.close()


def test_recovery_restores_old_generation_after_one_of_two_artifacts_was_promoted(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    first, first_target, manifest, manifest_target = prepare_files(workspace)
    second = workspace.work_dir / "2026-07-28.csv"
    second_target = workspace.output_dir / "exports" / "2026-07-28.csv"
    second.write_bytes(b"new second")
    first_target.parent.mkdir(parents=True)
    first_target.write_bytes(b"old first")
    second_target.write_bytes(b"old second")
    manifest_target.write_bytes(b"old manifest")
    original = transaction._replace_file

    def interrupt_second_artifact(src, dst, retries, retry_sleep):
        if dst == second_target and ".hhe-staging" in src.parts:
            raise KeyboardInterrupt
        return original(src, dst, retries, retry_sleep)

    monkeypatch.setattr(transaction, "_replace_file", interrupt_second_artifact)
    try:
        with pytest.raises(KeyboardInterrupt):
            DayTransaction(
                workspace=workspace,
                day="2026-07-28",
                artifacts=[(first, first_target), (second, second_target)],
                manifest=(manifest, manifest_target),
            ).commit()

        assert first_target.read_bytes() == b"new artifact"
        monkeypatch.setattr(transaction, "_replace_file", original)
        recover_incomplete(workspace.output_dir)

        assert first_target.read_bytes() == b"old first"
        assert second_target.read_bytes() == b"old second"
        assert manifest_target.read_bytes() == b"old manifest"
        assert not transaction.journal_root(workspace.output_dir).exists()
    finally:
        workspace.close()


def test_recovery_keeps_new_generation_after_committed_journal_was_persisted(
    tmp_path, monkeypatch
):
    cfg = make_config(tmp_path)
    workspace = open_workspace(cfg)
    artifact, artifact_target, manifest, manifest_target = prepare_files(workspace)
    artifact_target.parent.mkdir(parents=True)
    artifact_target.write_bytes(b"old artifact")
    manifest_target.write_bytes(b"old manifest")
    original_write_journal = transaction._write_journal

    def interrupt_after_journal(path, data):
        original_write_journal(path, data)
        if data["phase"] == "committed":
            raise KeyboardInterrupt

    monkeypatch.setattr(transaction, "_write_journal", interrupt_after_journal)
    try:
        with pytest.raises(KeyboardInterrupt):
            DayTransaction(
                workspace=workspace,
                day="2026-07-28",
                artifacts=[(artifact, artifact_target)],
                manifest=(manifest, manifest_target),
            ).commit()

        assert artifact_target.read_bytes() == b"new artifact"
        assert manifest_target.read_bytes() == b"new manifest"
        recover_incomplete(workspace.output_dir)

        assert artifact_target.read_bytes() == b"new artifact"
        assert manifest_target.read_bytes() == b"new manifest"
        assert not transaction.journal_root(workspace.output_dir).exists()
    finally:
        workspace.close()
