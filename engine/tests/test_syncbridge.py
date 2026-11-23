import importlib.util
import io
import json
import os
import shutil
import time
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
import unicodedata
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "syncbridge.py"
SPEC = importlib.util.spec_from_file_location("syncbridge_tested", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


REAL_NOTIFY = sync.notify


def datetime_text(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


class SyncSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.local = self.root / "local"
        self.cloud = self.root / "cloud"
        self.state = self.root / "state"
        self.local.mkdir()
        self.cloud.mkdir()
        self.state.mkdir()
        self.pair = sync.SyncPair("Test", self.local, self.cloud)
        sync.CONFIG = sync.AppConfig(
            config_path=self.root / "config.json",
            state_dir=self.state,
            state_file=self.state / "state.json",
            log_file=self.state / "sync.log",
            lock_file=self.state / "sync.lock",
            conflicts_file=self.state / "conflicts.json",
            summary_file=self.state / "summary.txt",
            exclusions_file=self.state / "activated-exclusions.json",
            pending_deletions_file=self.state / "pending-deletions.json",
            pairs=[self.pair],
            exclude_names=set(sync.DEFAULT_EXCLUDE_NAMES),
            exclude_prefixes=sync.DEFAULT_EXCLUDE_PREFIXES,
            exclude_suffixes=sync.DEFAULT_EXCLUDE_SUFFIXES,
            exclude_dirs=set(sync.DEFAULT_EXCLUDE_DIRS),
            exclude_path_patterns=sync.DEFAULT_EXCLUDE_PATH_PATTERNS,
            staged_exclusions=(),
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def write_both(self, path="file.docx", content=b"baseline"):
        for base in (self.local, self.cloud):
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def baseline(self):
        return sync.refresh_pair_state(self.pair, force_full=True)

    def test_bidirectional_copy_actions(self):
        actions, conflicts = sync.classify_changes(
            {"local.txt": "B", "cloud.txt": "A"},
            {"local.txt": "A", "cloud.txt": "C"},
            {"local.txt": "A", "cloud.txt": "A"},
            {"local.txt": "A", "cloud.txt": "A"},
        )
        self.assertEqual(
            actions,
            [
                ("copy_onedrive_to_local", "cloud.txt"),
                ("copy_local_to_onedrive", "local.txt"),
            ],
        )
        self.assertEqual(conflicts, [])

    def test_no_delete_really_preserves_destination_and_retries(self):
        self.write_both()
        previous = self.baseline()
        (self.local / "file.docx").unlink()

        first = sync.sync_pair(self.pair, previous, "test", no_delete=True)
        self.assertTrue((self.cloud / "file.docx").exists())
        self.assertEqual(first["deleted_onedrive_files"], [])
        self.assertEqual(first["skipped_delete_files"], ["file.docx"])
        self.assertEqual(
            first["skipped_delete_actions"],
            [("delete_onedrive", "file.docx")],
        )
        self.assertIn("file.docx", first["state"]["local"])

        second = sync.sync_pair(
            self.pair, first["state"], "test", no_delete=True, dry_run=True
        )
        self.assertEqual(second["skipped_delete_files"], ["file.docx"])

    def test_explicit_delete_propagation_still_works(self):
        self.write_both()
        previous = self.baseline()
        (self.local / "file.docx").unlink()
        result = sync.sync_pair(self.pair, previous, "test", no_delete=False)
        self.assertFalse((self.cloud / "file.docx").exists())
        self.assertEqual(result["deleted_onedrive_files"], ["file.docx"])

    def test_local_rename_is_propagated_despite_delete_protection(self):
        self.write_both("old-name.docx", b"content")
        previous = self.baseline()
        (self.local / "old-name.docx").rename(self.local / "new-name.docx")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertFalse((self.cloud / "old-name.docx").exists())
        self.assertEqual((self.cloud / "new-name.docx").read_bytes(), b"content")
        self.assertEqual(
            result["renamed_onedrive_files"],
            [("old-name.docx", "new-name.docx")],
        )
        self.assertEqual(result["skipped_delete_files"], [])

    def test_onedrive_rename_is_propagated_despite_delete_protection(self):
        self.write_both("old-name.docx", b"content")
        previous = self.baseline()
        (self.cloud / "old-name.docx").rename(self.cloud / "new-name.docx")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertFalse((self.local / "old-name.docx").exists())
        self.assertEqual((self.local / "new-name.docx").read_bytes(), b"content")
        self.assertEqual(
            result["renamed_local_files"],
            [("old-name.docx", "new-name.docx")],
        )
        self.assertEqual(result["skipped_delete_files"], [])

    def test_rename_with_simultaneous_edit_keeps_old_name_protected(self):
        self.write_both("old-name.docx", b"old")
        previous = self.baseline()
        (self.local / "old-name.docx").rename(self.local / "new-name.docx")
        (self.local / "new-name.docx").write_bytes(b"edited")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual((self.cloud / "old-name.docx").read_bytes(), b"old")
        self.assertEqual((self.cloud / "new-name.docx").read_bytes(), b"edited")
        self.assertEqual(result["renamed_onedrive_files"], [])
        self.assertEqual(result["skipped_delete_files"], ["old-name.docx"])

    def test_ambiguous_same_content_rename_keeps_old_name_protected(self):
        self.write_both("old-name.docx", b"same")
        previous = self.baseline()
        (self.local / "old-name.docx").rename(self.local / "new-name.docx")
        (self.local / "another-new-name.docx").write_bytes(b"same")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertTrue((self.cloud / "old-name.docx").exists())
        self.assertTrue((self.cloud / "new-name.docx").exists())
        self.assertTrue((self.cloud / "another-new-name.docx").exists())
        self.assertEqual(result["renamed_onedrive_files"], [])
        self.assertEqual(result["skipped_delete_files"], ["old-name.docx"])

    def test_preexisting_duplicate_does_not_bypass_delete_protection(self):
        self.write_both("old-name.docx", b"same")
        self.write_both("existing-copy.docx", b"same")
        previous = self.baseline()
        (self.local / "old-name.docx").unlink()

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertTrue((self.cloud / "old-name.docx").exists())
        self.assertEqual(result["renamed_onedrive_files"], [])
        self.assertEqual(result["skipped_delete_files"], ["old-name.docx"])

    def test_new_matching_destination_is_finished_as_a_safe_rename(self):
        self.write_both("old-name.docx", b"content")
        previous = self.baseline()
        (self.local / "old-name.docx").rename(self.local / "new-name.docx")
        (self.cloud / "new-name.docx").write_bytes(b"content")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertFalse((self.cloud / "old-name.docx").exists())
        self.assertEqual((self.cloud / "new-name.docx").read_bytes(), b"content")
        self.assertEqual(
            result["renamed_onedrive_files"],
            [("old-name.docx", "new-name.docx")],
        )

    def test_conflict_is_not_forgotten(self):
        self.write_both(content=b"old")
        previous = self.baseline()
        (self.local / "file.docx").write_bytes(b"local")
        (self.cloud / "file.docx").write_bytes(b"cloud")

        first = sync.sync_pair(self.pair, previous, "test", no_delete=True)
        self.assertEqual(len(first["conflicts"]), 1)
        second = sync.sync_pair(
            self.pair, first["state"], "test", no_delete=True, dry_run=True
        )
        self.assertEqual(len(second["conflicts"]), 1)

    def test_legacy_mismatched_baseline_never_triggers_overwrite(self):
        actions, conflicts = sync.classify_changes(
            {"file.docx": "LOCAL"},
            {"file.docx": "CLOUD-NEW"},
            {"file.docx": "LOCAL"},
            {"file.docx": "CLOUD-OLD"},
        )
        self.assertEqual(actions, [])
        self.assertEqual(
            conflicts[0]["reason"], "Previous baseline was not synchronized"
        )

    def test_case_and_unicode_name_differences_become_one_conflict(self):
        local_name = "TABLETOP-ÜBUNG.md"
        cloud_name = "Tabletop-U\u0308bung.md"
        conflicts, paths = sync.find_name_mismatch_conflicts(
            {local_name: "local"}, {cloud_name: "cloud"}
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(paths, {local_name, cloud_name})

    def test_identical_case_and_unicode_name_differences_are_equivalent(self):
        local_name = "TABLETOP-ÜBUNG.md"
        cloud_name = "Tabletop-U\u0308bung.md"
        conflicts, paths = sync.find_name_mismatch_conflicts(
            {local_name: "same"}, {cloud_name: "same"}
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(paths, {local_name, cloud_name})

    def test_failed_copy_is_retried(self):
        self.write_both(content=b"old")
        previous = self.baseline()
        (self.local / "file.docx").write_bytes(b"new")

        with mock.patch.object(sync, "copy_file", return_value=False):
            failed = sync.sync_pair(self.pair, previous, "test", no_delete=True)
        self.assertEqual(failed["errors"], 1)
        self.assertEqual((self.cloud / "file.docx").read_bytes(), b"old")

        retried = sync.sync_pair(self.pair, failed["state"], "test", no_delete=True)
        self.assertEqual(retried["errors"], 0)
        self.assertEqual((self.cloud / "file.docx").read_bytes(), b"new")

    def test_file_created_during_sync_is_not_accepted_as_baseline(self):
        self.write_both(content=b"old")
        previous = self.baseline()
        (self.local / "file.docx").write_bytes(b"new")
        original_copy = sync.copy_file

        def copy_and_create_late(*args, **kwargs):
            success = original_copy(*args, **kwargs)
            (self.local / "late.docx").write_bytes(b"late")
            return success

        with mock.patch.object(sync, "copy_file", side_effect=copy_and_create_late):
            first = sync.sync_pair(self.pair, previous, "test", no_delete=True)
        self.assertNotIn("late.docx", first["state"]["local"])
        preview = sync.sync_pair(
            self.pair, first["state"], "test", no_delete=True, dry_run=True
        )
        self.assertEqual(preview["to_onedrive_files"], ["late.docx"])

    def test_incomplete_scan_skips_pair_without_actions(self):
        self.write_both()
        previous = self.baseline()
        original_md5 = sync.md5_file

        def fail_cloud(path):
            if str(path).startswith(str(self.cloud)):
                raise PermissionError("offline placeholder")
            return original_md5(path)

        with mock.patch.object(sync, "md5_file", side_effect=fail_cloud):
            result = sync.sync_pair(
                self.pair, previous, "test", no_delete=False, force_full=True
            )
        self.assertTrue(result["skipped"])
        self.assertTrue((self.local / "file.docx").exists())
        self.assertTrue((self.cloud / "file.docx").exists())

    def test_unreadable_file_blocks_only_that_path_not_the_pair(self):
        for index in range(150):
            self.write_both(f"docs/file{index}.docx", f"content {index}".encode())
        self.write_both("bundle.bin", b"bundle")
        previous = self.baseline()
        (self.local / "docs/file0.docx").write_bytes(b"edited")
        original_md5 = sync.md5_file

        def fail_cloud_bundle(path):
            if Path(path) == self.cloud / "bundle.bin":
                raise OSError(45, "Operation not supported")
            return original_md5(path)

        with mock.patch.object(sync, "md5_file", side_effect=fail_cloud_bundle):
            result = sync.sync_pair(
                self.pair, previous, "test", no_delete=False, force_full=True
            )

        self.assertFalse(result["skipped"])
        self.assertEqual(result["to_onedrive_files"], ["docs/file0.docx"])
        self.assertEqual((self.cloud / "docs/file0.docx").read_bytes(), b"edited")
        self.assertEqual(result["unreadable_files"], ["bundle.bin"])
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["deleted_local_files"], [])
        self.assertEqual(result["skipped_delete_files"], [])
        self.assertTrue((self.local / "bundle.bin").exists())
        self.assertEqual(
            result["state"]["onedrive"]["bundle.bin"],
            previous["onedrive"]["bundle.bin"],
        )

    def test_unreadable_directory_still_skips_pair(self):
        self.write_both("private/file.docx")
        previous = self.baseline()
        locked = self.cloud / "private"
        locked.chmod(0)
        try:
            result = sync.sync_pair(self.pair, previous, "test", no_delete=False)
        finally:
            locked.chmod(0o755)
        self.assertTrue(result["skipped"])
        self.assertTrue((self.local / "private/file.docx").exists())

    def test_case_only_directory_rename_is_applied_once_and_not_repeated(self):
        probe = self.cloud / "CaseProbe"
        probe.mkdir()
        case_insensitive = (self.cloud / "caseprobe").exists()
        probe.rmdir()
        if not case_insensitive:
            self.skipTest("Requires a case-insensitive file system like APFS")

        self.write_both("internal Audits/report.pdf", b"report")
        previous = self.baseline()
        os.rename(self.local / "internal Audits", self.local / "Internal Audits")

        first = sync.sync_pair(self.pair, previous, "test", no_delete=True)
        self.assertEqual(
            first["renamed_onedrive_files"],
            [("internal Audits/report.pdf", "Internal Audits/report.pdf")],
        )
        self.assertEqual(os.listdir(self.cloud), ["Internal Audits"])

        second = sync.sync_pair(self.pair, first["state"], "test", no_delete=True)
        self.assertEqual(second["renamed_onedrive_files"], [])
        self.assertEqual(second["renamed_local_files"], [])
        self.assertEqual(second["to_onedrive_files"], [])
        self.assertEqual(second["to_local_files"], [])
        self.assertEqual(second["skipped_delete_files"], [])
        self.assertEqual(second["conflicts"], [])

    def test_dialogs_and_notifications_use_the_app_icon(self):
        icon = self.root / "AppIcon.icns"
        icon.write_bytes(b"icns")
        with mock.patch.object(sync, "APP_ICON", icon), mock.patch.object(
            sync.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(stdout="OK\n")
            sync._osascript_with_tempfile("Hello", "Title", ["OK"], "OK")
            script = run.call_args.args[0][2]
            self.assertIn(f'with icon (POSIX file "{icon}")', script)

        with mock.patch.object(sync.shutil, "which", return_value="/bin/notifier"), \
                mock.patch.object(sync.subprocess, "run") as run:
            sync.notify("Title", "Message")
            command = run.call_args.args[0]
            self.assertIn("-sender", command)
            self.assertEqual(command[command.index("-sender") + 1], sync.APP_BUNDLE_ID)
            self.assertNotIn("-execute", command)

    def test_conflict_dialog_shows_dates_sizes_and_newer_version(self):
        (self.local / "plan.xlsx").write_bytes(b"x" * 2048)
        (self.cloud / "plan.xlsx").write_bytes(b"y" * 10)
        os.utime(self.local / "plan.xlsx", (1700000000, 1700000000))
        os.utime(self.cloud / "plan.xlsx", (1700090000, 1700090000))

        text = sync.conflict_versions_text(self.pair, "plan.xlsx")

        local_line, cloud_line = text.splitlines()
        self.assertIn(datetime_text(1700000000), local_line)
        self.assertIn("2.0 KB", local_line)
        self.assertNotIn("newer", local_line)
        self.assertIn(datetime_text(1700090000), cloud_line)
        self.assertIn("10 B", cloud_line)
        self.assertIn("newer", cloud_line)

        (self.cloud / "plan.xlsx").unlink()
        self.assertIn("deleted", sync.conflict_versions_text(self.pair, "plan.xlsx"))

    def test_conflict_diff_preview_is_short_with_change_summary(self):
        (self.cloud / "app.tsx").write_text(
            "".join(f"line {i}\n" for i in range(10)), encoding="utf-8"
        )
        (self.local / "app.tsx").write_text(
            "".join(f"line {i}\n" for i in range(10))
            + "".join(f"added {i}\n" for i in range(40)),
            encoding="utf-8",
        )

        preview = sync.conflict_diff_preview(self.pair, "app.tsx", max_lines=8)

        lines = preview.splitlines()
        self.assertEqual(lines[0], "Local has 40 added and 0 removed line(s) vs. OneDrive.")
        self.assertNotIn("+++", preview)
        self.assertNotIn("---", preview)
        self.assertLessEqual(len(lines), 1 + 8 + 1)
        self.assertIn("more line(s)", lines[-1])

    def test_missing_root_is_never_treated_as_mass_deletion(self):
        self.write_both()
        previous = self.baseline()
        self.local.rename(self.root / "local-away")
        result = sync.sync_pair(self.pair, previous, "test", no_delete=False)
        self.assertTrue(result["skipped"])
        self.assertTrue((self.cloud / "file.docx").exists())

    def test_signature_detects_same_mtime_content_replacement(self):
        self.write_both(content=b"AAAA")
        previous = self.baseline()
        old_stat = (self.cloud / "file.docx").stat()
        (self.cloud / "file.docx").write_bytes(b"BBBB")
        os.utime(
            self.cloud / "file.docx",
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns),
        )

        result = sync.sync_pair(
            self.pair, previous, "test", no_delete=True, dry_run=True
        )
        self.assertEqual(result["to_local_files"], ["file.docx"])

    def test_dry_run_does_not_change_state_conflicts_or_summary(self):
        self.write_both(content=b"old")
        pair_state = self.baseline()
        sync.save_state({"Test": pair_state})
        sync.save_conflicts(
            [{"pair": "Test", "file": "existing", "reason": "existing"}]
        )
        sync.atomic_write_text(sync.CONFIG.summary_file, "existing summary")
        before = {
            path: path.read_bytes()
            for path in (
                sync.CONFIG.state_file,
                sync.CONFIG.conflicts_file,
                sync.CONFIG.summary_file,
            )
        }
        (self.local / "file.docx").write_bytes(b"new")

        sync.sync_all(dry_run=True, no_delete=True)
        after = {path: path.read_bytes() for path in before}
        self.assertEqual(before, after)

    def test_summary_caps_large_pending_delete_lists(self):
        pending = [f"folder/file-{index}.jpg" for index in range(250)]
        result = {
            "pair": self.pair,
            "skipped": False,
            "skip_reason": None,
            "to_local_files": [],
            "to_onedrive_files": [],
            "deleted_onedrive_files": [],
            "deleted_local_files": [],
            "skipped_delete_files": pending,
            "conflicts": [],
            "errors": 0,
        }
        sync.write_summary("test", [result])
        summary = sync.CONFIG.summary_file.read_text(encoding="utf-8")
        self.assertIn("Skipped deletes (250 files):", summary)
        self.assertIn("... and 150 more", summary)
        self.assertNotIn("folder/file-249.jpg", summary)

    def test_pending_deletions_are_grouped_with_staged_outputs_first(self):
        sync.CONFIG.staged_exclusions = (
            sync.StagedExclusion(
                pair="Test",
                label="Temporary generated outputs",
                pattern="generated/**",
            ),
        )
        pending = [
            sync.PendingDeletion("Test", "delete_onedrive", "other/file.txt"),
            sync.PendingDeletion("Test", "delete_onedrive", "generated/a.jpg"),
            sync.PendingDeletion("Test", "delete_onedrive", "generated/b.jpg"),
        ]

        groups = sync.group_pending_deletions(pending)

        self.assertEqual(groups[0].path, "generated")
        self.assertTrue(groups[0].recommended)
        self.assertEqual(len(groups[0].items), 2)
        self.assertEqual(groups[1].path, "other")
        self.assertFalse(groups[1].recommended)

    def test_deletion_application_requires_explicit_confirmation(self):
        with self.assertRaises(PermissionError):
            sync.apply_approved_deletions(
                [sync.PendingDeletion("Test", "delete_onedrive", "file.docx")]
            )

    def test_selected_generated_cleanup_deletes_only_selection_and_activates_exclusion(self):
        sync.CONFIG.staged_exclusions = (
            sync.StagedExclusion(
                pair="Test",
                label="Temporary generated outputs",
                pattern="generated/**",
            ),
        )
        self.write_both("generated/a.jpg", b"a")
        self.write_both("generated/b.jpg", b"b")
        self.write_both("other/keep-cloud.txt", b"keep")
        sync.save_state({"Test": self.baseline()})
        (self.local / "generated/a.jpg").unlink()
        (self.local / "generated/b.jpg").unlink()
        (self.local / "other/keep-cloud.txt").unlink()
        pending, issues = sync.plan_pending_deletions()
        sync.save_pending_deletion_snapshot(pending)
        selected = [item for item in pending if item.file.startswith("generated/")]

        deleted, failed, activated = sync.apply_approved_deletions(
            selected,
            confirmed=True,
        )

        self.assertEqual(len(deleted), 2)
        self.assertEqual(failed, [])
        self.assertFalse((self.cloud / "generated/a.jpg").exists())
        self.assertFalse((self.cloud / "generated/b.jpg").exists())
        self.assertTrue((self.cloud / "other/keep-cloud.txt").exists())
        self.assertEqual([item.label for item in activated], ["Temporary generated outputs"])
        self.assertTrue(sync.should_exclude_rel_path("generated/future.jpg"))
        saved = json.loads(sync.CONFIG.exclusions_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["patterns"], ["generated/**"])
        self.assertEqual(issues, [])

    def test_changed_deletion_plan_aborts_without_deleting(self):
        self.write_both("folder/file.docx", b"content")
        sync.save_state({"Test": self.baseline()})
        (self.local / "folder/file.docx").unlink()
        pending, issues = sync.plan_pending_deletions()
        sync.save_pending_deletion_snapshot(pending)
        self.assertEqual(issues, [])
        (self.local / "folder/file.docx").write_bytes(b"content")

        with self.assertRaisesRegex(RuntimeError, "plan changed"):
            sync.apply_approved_deletions(pending, confirmed=True)

        self.assertTrue((self.cloud / "folder/file.docx").exists())

    def test_deletion_moves_local_file_to_trash_not_unlink(self):
        self.write_both("folder/file.docx", b"keep me")
        previous = self.baseline()
        (self.cloud / "folder/file.docx").unlink()

        result = sync.sync_pair(self.pair, previous, "test", no_delete=False)

        self.assertEqual(result["deleted_local_files"], ["folder/file.docx"])
        self.assertFalse((self.local / "folder/file.docx").exists())
        trashed = list(sync.trash_root().glob("*/Test/local/folder/file.docx"))
        self.assertEqual(len(trashed), 1)
        self.assertEqual(trashed[0].read_bytes(), b"keep me")

    def test_old_trash_batches_are_pruned(self):
        old_batch = sync.trash_root() / "2000-01-01_000000"
        new_batch = sync.trash_root() / "2999-01-01_000000"
        for batch in (old_batch, new_batch):
            (batch / "Test").mkdir(parents=True)
            (batch / "Test" / "file.docx").write_bytes(b"x")
        old_time = 946684800
        os.utime(old_batch, (old_time, old_time))

        sync.prune_trash(retention_days=30)

        self.assertFalse(old_batch.exists())
        self.assertTrue(new_batch.exists())

    def test_auto_delete_held_for_review_above_threshold(self):
        for index in range(3):
            self.write_both(f"bulk/file{index}.docx", f"c{index}".encode())
        previous = self.baseline()
        for index in range(3):
            (self.local / f"bulk/file{index}.docx").unlink()

        held = sync.sync_pair(
            self.pair, previous, "test", no_delete=False, delete_limit=2
        )
        self.assertEqual(held["deleted_onedrive_files"], [])
        self.assertEqual(len(held["skipped_delete_files"]), 3)
        for index in range(3):
            self.assertTrue((self.cloud / f"bulk/file{index}.docx").exists())

        within = sync.sync_pair(
            self.pair, previous, "test", no_delete=False, delete_limit=3
        )
        self.assertEqual(len(within["deleted_onedrive_files"]), 3)
        self.assertEqual(within["skipped_delete_files"], [])

    def test_auto_delete_ignores_backlog_already_held_for_review(self):
        self.write_both("old.docx", b"old")
        self.write_both("new.docx", b"new")
        previous = self.baseline()
        (self.local / "old.docx").unlink()
        (self.local / "new.docx").unlink()

        result = sync.sync_pair(
            self.pair,
            previous,
            "test",
            no_delete=False,
            delete_limit=1,
            held_deletions={("delete_onedrive", "old.docx")},
        )

        self.assertEqual(result["deleted_onedrive_files"], ["new.docx"])
        self.assertEqual(result["skipped_delete_files"], ["old.docx"])
        self.assertTrue((self.cloud / "old.docx").exists())

    def test_restore_pending_deletion_copies_file_back(self):
        self.write_both("folder/file.docx", b"content")
        sync.save_state({"Test": self.baseline()})
        (self.local / "folder/file.docx").unlink()
        pending, issues = sync.plan_pending_deletions()
        sync.save_pending_deletion_snapshot(pending)
        self.assertEqual(issues, [])

        restored, failed = sync.restore_pending_deletions(pending)

        self.assertEqual([item.file for item in restored], ["folder/file.docx"])
        self.assertEqual(failed, [])
        self.assertEqual((self.local / "folder/file.docx").read_bytes(), b"content")
        self.assertEqual(sync.load_pending_deletion_snapshot(), [])
        state = sync.load_state()["pairs"]["Test"]
        after = sync.sync_pair(self.pair, state, "test", no_delete=True, dry_run=True)
        self.assertEqual(after["skipped_delete_files"], [])
        self.assertEqual(after["to_onedrive_files"], [])
        self.assertEqual(after["to_local_files"], [])

    def test_deleting_last_file_removes_empty_parent_dirs(self):
        self.write_both("a/b/only.docx", b"x")
        self.write_both("a/keep.docx", b"keep")
        previous = self.baseline()
        (self.cloud / "a/b/.DS_Store").write_bytes(b"finder")
        shutil.rmtree(self.local / "a/b")

        sync.sync_pair(self.pair, previous, "test", no_delete=False)

        self.assertFalse((self.cloud / "a/b").exists())
        self.assertTrue((self.cloud / "a/keep.docx").exists())
        self.assertTrue(self.cloud.exists())

    def test_deleting_last_file_keeps_folder_that_still_exists_on_other_side(self):
        self.write_both("a/b/only.docx", b"x")
        previous = self.baseline()
        (self.local / "a/b/only.docx").unlink()

        result = sync.sync_pair(self.pair, previous, "test", no_delete=False)
        sync.sync_pair(self.pair, result["state"], "test", no_delete=False)

        self.assertTrue((self.cloud / "a/b").is_dir())
        self.assertTrue((self.local / "a/b").is_dir())

    def test_rename_removes_emptied_source_folder(self):
        self.write_both("old folder/file.docx", b"content")
        previous = self.baseline()
        (self.local / "old folder").rename(self.local / "new folder")

        sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual(os.listdir(self.cloud), ["new folder"])

    def test_sensitive_and_volatile_files_are_excluded(self):
        files = [
            ".env",
            "project/.env.local",
            "project/.claude/settings.local.json",
            "project/cache.db-shm",
            "project/build.tsbuildinfo",
            "project/.wrangler/tmp/cache.json",
        ]
        for rel_path in files:
            target = self.local / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("secret", encoding="utf-8")
        included = self.local / "customer.docx"
        included.write_bytes(b"document")

        checksums, _, _ = sync.scan_directory(self.local)
        self.assertEqual(set(checksums), {"customer.docx"})

    def test_build_artifacts_are_excluded(self):
        excluded = [
            "web/.next-qa-qms/server/page.js",
            "proto/.local-test/computer-test.txt",
            "proto/clang-cache/module.pcm",
            "proto/dist/mac/Evidence Agent.app/Contents/Info.plist",
            "Frameworks/Electron Framework.framework/Versions/A/Resources/x.pak",
        ]
        included = [
            "web/src/next-steps.md",
            "web/.nextrc",
            "Kunde/App-Konzept.docx",
            "notes/application.docx",
        ]
        for rel_path in excluded + included:
            target = self.local / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("data", encoding="utf-8")
        # OneDrive can expose a bundle as a single opaque file.
        (self.local / "Frameworks/Mantle.framework").write_text("x", encoding="utf-8")

        checksums, _, _ = sync.scan_directory(self.local)
        self.assertEqual(set(checksums), set(included))

    def test_reconcile_missing_only_copies_and_never_overwrites(self):
        self.write_both("parallel-change.docx", b"old")
        self.write_both("pending-delete.docx", b"old")
        (self.local / "local-only.docx").write_bytes(b"local")
        (self.cloud / "cloud-only.docx").write_bytes(b"cloud")
        (self.local / "different.docx").write_bytes(b"local version")
        (self.cloud / "different.docx").write_bytes(b"cloud version")
        pair_state = self.baseline()
        sync.save_state({"Test": pair_state})
        (self.local / "parallel-change.docx").write_bytes(b"new local edit")
        (self.local / "pending-delete.docx").unlink()
        sync.save_conflicts(
            [
                {
                    "pair": "Test",
                    "file": "local-only.docx",
                    "reason": "old drift",
                    "local_hash": sync.md5_file(self.local / "local-only.docx"),
                    "onedrive_hash": None,
                },
                {
                    "pair": "Test",
                    "file": "cloud-only.docx",
                    "reason": "old drift",
                    "local_hash": None,
                    "onedrive_hash": sync.md5_file(self.cloud / "cloud-only.docx"),
                },
                {
                    "pair": "Test",
                    "file": "different.docx",
                    "reason": "old drift",
                    "local_hash": sync.md5_file(self.local / "different.docx"),
                    "onedrive_hash": sync.md5_file(self.cloud / "different.docx"),
                },
            ]
        )

        resolved, remaining = sync.reconcile_missing_conflicts()
        self.assertEqual(len(resolved), 2)
        self.assertEqual(len(remaining), 1)
        self.assertEqual((self.cloud / "local-only.docx").read_bytes(), b"local")
        self.assertEqual((self.local / "cloud-only.docx").read_bytes(), b"cloud")
        self.assertEqual(
            (self.cloud / "different.docx").read_bytes(), b"cloud version"
        )
        saved = sync.load_state()["pairs"]["Test"]
        old_hash = sync.hashlib.md5(b"old").hexdigest()
        self.assertEqual(saved["local"]["parallel-change.docx"], old_hash)
        self.assertEqual(saved["onedrive"]["parallel-change.docx"], old_hash)
        self.assertEqual(saved["local"]["pending-delete.docx"], old_hash)
        self.assertEqual(saved["onedrive"]["pending-delete.docx"], old_hash)


class ConflictRuleTests(unittest.TestCase):
    """Automatic conflict rules configured in the app."""

    write_both = SyncSafetyTests.write_both
    baseline = SyncSafetyTests.baseline
    tearDown = SyncSafetyTests.tearDown

    def setUp(self):
        SyncSafetyTests.setUp(self)

    def edit_both(self, name, local_time, cloud_time):
        self.write_both(name, b"base")
        previous = self.baseline()
        (self.local / name).write_bytes(b"local version")
        (self.cloud / name).write_bytes(b"cloud version")
        os.utime(self.local / name, (local_time, local_time))
        os.utime(self.cloud / name, (cloud_time, cloud_time))
        return previous

    def backups(self, side, name):
        return list(sync.trash_root().glob(f"*/Test/{side}/{name}"))

    def test_rule_newer_keeps_newer_version_and_backs_up_other(self):
        sync.CONFIG.conflict_rule = "newer"
        previous = self.edit_both("report.docx", local_time=1700090000, cloud_time=1700000000)

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["to_onedrive_files"], ["report.docx"])
        self.assertEqual((self.cloud / "report.docx").read_bytes(), b"local version")
        backup = self.backups("onedrive", "report.docx")
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_bytes(), b"cloud version")

    def test_rule_ask_keeps_conflict_open(self):
        previous = self.edit_both("report.docx", local_time=1700090000, cloud_time=1700000000)

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual(len(result["conflicts"]), 1)
        self.assertEqual((self.cloud / "report.docx").read_bytes(), b"cloud version")

    def test_rule_local_always_keeps_local(self):
        sync.CONFIG.conflict_rule = "local"
        previous = self.edit_both("report.docx", local_time=1700000000, cloud_time=1700090000)

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.cloud / "report.docx").read_bytes(), b"local version")
        self.assertEqual(len(self.backups("onedrive", "report.docx")), 1)

    def test_rule_newer_keeps_edited_file_when_other_side_deleted(self):
        sync.CONFIG.conflict_rule = "newer"
        self.write_both("plan.xlsx", b"base")
        previous = self.baseline()
        (self.local / "plan.xlsx").unlink()
        (self.cloud / "plan.xlsx").write_bytes(b"edited in cloud")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=False)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.local / "plan.xlsx").read_bytes(), b"edited in cloud")
        self.assertEqual((self.cloud / "plan.xlsx").read_bytes(), b"edited in cloud")

    def test_rule_never_deletes_when_winning_side_was_deleted(self):
        sync.CONFIG.conflict_rule = "local"
        self.write_both("plan.xlsx", b"base")
        previous = self.baseline()
        (self.local / "plan.xlsx").unlink()
        (self.cloud / "plan.xlsx").write_bytes(b"edited in cloud")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=False)

        self.assertEqual(len(result["conflicts"]), 1)
        self.assertTrue((self.cloud / "plan.xlsx").exists())

    def test_rule_newer_leaves_same_time_and_name_conflicts_open(self):
        sync.CONFIG.conflict_rule = "newer"
        previous = self.edit_both("same.docx", local_time=1700000000, cloud_time=1700000000)

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual(len(result["conflicts"]), 1)
        name_conflict = {"pair": "Test", "file": "A.md", "related_file": "a.md", "reason": "x"}
        self.assertIsNone(sync.rule_decision(self.pair, name_conflict, "newer"))

    def test_conflict_rule_is_read_and_validated_from_config(self):
        path = self.root / "config.json"
        pairs = [{"name": "T", "local": str(self.local), "onedrive": str(self.cloud)}]
        path.write_text(json.dumps({"pairs": pairs, "conflict_rule": "newer"}), encoding="utf-8")
        self.assertEqual(sync.load_config(str(path)).conflict_rule, "newer")
        errors = sync.validate_config_payload({"pairs": pairs, "conflict_rule": "random"})
        self.assertTrue(any("conflict_rule" in error for error in errors))


class AppInterfaceTests(unittest.TestCase):
    """JSON interface used by the SyncBridge menu bar app."""

    write_both = SyncSafetyTests.write_both
    baseline = SyncSafetyTests.baseline
    tearDown = SyncSafetyTests.tearDown

    def setUp(self):
        SyncSafetyTests.setUp(self)
        notify = mock.patch.object(sync, "notify")
        notify.start()
        self.addCleanup(notify.stop)

    def write_config(self, **extra):
        payload = {
            "state_dir": str(self.state),
            "pairs": [
                {"name": "Test", "local": str(self.local), "onedrive": str(self.cloud)}
            ],
        }
        payload.update(extra)
        path = self.root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_status_json_reports_pairs_counts_and_last_sync(self):
        self.write_both("a.docx", b"a")
        self.write_both("b.docx", b"b")
        sync.save_state({"Test": self.baseline()})
        (self.local / "a.docx").write_bytes(b"local edit")
        (self.cloud / "a.docx").write_bytes(b"cloud edit")
        (self.local / "b.docx").unlink()
        sync.sync_all(no_delete=True)

        status = sync.status_payload()

        self.assertFalse(status["running"])
        self.assertIsNotNone(status["last_sync"])
        self.assertEqual(status["conflicts"], 1)
        self.assertEqual(status["pending_deletions"], 1)
        self.assertEqual(status["pairs"][0]["name"], "Test")
        self.assertEqual(status["pairs"][0]["local"], str(self.local))
        self.assertIn("conflict", status["last_result"])
        json.dumps(status)

    def test_list_conflicts_json_includes_version_dates_and_sizes(self):
        self.write_both("notes.md", b"base\n")
        sync.save_state({"Test": self.baseline()})
        (self.local / "notes.md").write_bytes(b"base\nlocal line\n")
        (self.cloud / "notes.md").write_bytes(b"cloud\n")
        os.utime(self.local / "notes.md", (1700090000, 1700090000))
        os.utime(self.cloud / "notes.md", (1700000000, 1700000000))
        sync.sync_all(no_delete=True)

        conflicts = sync.conflicts_payload()

        self.assertEqual(len(conflicts), 1)
        item = conflicts[0]
        self.assertEqual(item["pair"], "Test")
        self.assertEqual(item["file"], "notes.md")
        self.assertEqual(item["local"]["size"], len(b"base\nlocal line\n"))
        self.assertEqual(item["local"]["modified"], 1700090000)
        self.assertEqual(item["onedrive"]["modified"], 1700000000)
        self.assertEqual(item["newer"], "local")
        self.assertIn("added", item["diff_preview"])
        json.dumps(conflicts)

    def test_list_deletions_json_groups_items_like_review(self):
        self.write_both("folder/a.docx", b"a")
        self.write_both("folder/b.docx", b"b")
        sync.save_state({"Test": self.baseline()})
        (self.local / "folder/a.docx").unlink()
        (self.local / "folder/b.docx").unlink()
        sync.sync_all(no_delete=True)

        groups = sync.deletions_payload()

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["pair"], "Test")
        self.assertEqual(group["remaining_on"], "OneDrive")
        self.assertEqual(group["path"], "folder")
        self.assertEqual(group["count"], 2)
        self.assertEqual(
            [item["file"] for item in group["items"]], ["folder/a.docx", "folder/b.docx"]
        )
        json.dumps(groups)

    def test_apply_and_restore_deletions_from_json_selection(self):
        self.write_both("gone.docx", b"gone")
        self.write_both("back.docx", b"back")
        sync.save_state({"Test": self.baseline()})
        (self.local / "gone.docx").unlink()
        (self.local / "back.docx").unlink()
        sync.sync_all(no_delete=True)
        items = {item["file"]: item for group in sync.deletions_payload() for item in group["items"]}

        with self.assertRaises(PermissionError):
            sync.apply_deletion_selection([items["gone.docx"]], "delete", confirmed=False)
        deleted = sync.apply_deletion_selection([items["gone.docx"]], "delete", confirmed=True)
        restored = sync.apply_deletion_selection([items["back.docx"]], "restore", confirmed=True)

        self.assertEqual(deleted["done"], ["gone.docx"])
        self.assertEqual(restored["done"], ["back.docx"])
        self.assertFalse((self.cloud / "gone.docx").exists())
        self.assertEqual((self.local / "back.docx").read_bytes(), b"back")
        self.assertEqual(sync.deletions_payload(), [])

    def test_resolve_batch_applies_decisions_without_full_rescan(self):
        self.write_both("keep-local.md", b"base")
        self.write_both("keep-cloud.md", b"base")
        sync.save_state({"Test": self.baseline()})
        for name in ("keep-local.md", "keep-cloud.md"):
            (self.local / name).write_bytes(b"local " + name.encode())
            (self.cloud / name).write_bytes(b"cloud " + name.encode())
        sync.sync_all(no_delete=True)
        self.assertEqual(len(sync.conflicts_payload()), 2)

        with mock.patch.object(sync, "refresh_pair_state") as full_rescan:
            result = sync.resolve_conflicts_batch(
                [
                    {"pair": "Test", "file": "keep-local.md", "keep": "local"},
                    {"pair": "Test", "file": "keep-cloud.md", "keep": "onedrive"},
                ]
            )
        full_rescan.assert_not_called()

        self.assertEqual(sorted(result["done"]), ["keep-cloud.md", "keep-local.md"])
        self.assertEqual((self.cloud / "keep-local.md").read_bytes(), b"local keep-local.md")
        self.assertEqual((self.local / "keep-cloud.md").read_bytes(), b"cloud keep-cloud.md")
        self.assertEqual(sync.conflicts_payload(), [])
        state = sync.load_state()["pairs"]["Test"]
        after = sync.sync_pair(self.pair, state, "test", no_delete=True, dry_run=True)
        self.assertEqual(after["to_onedrive_files"] + after["to_local_files"], [])
        self.assertEqual(after["conflicts"], [])

    def test_config_roundtrip_preserves_unknown_fields_and_validates_pairs(self):
        path = self.write_config(custom_field={"keep": True})
        config = sync.read_config_payload(path)
        config["pairs"].append(
            {"name": "Docs", "local": str(self.root / "docs"), "onedrive": str(self.root / "cloud-docs")}
        )
        config.pop("custom_field")

        self.assertEqual(sync.save_config_payload(config, path), [])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["custom_field"], {"keep": True})
        self.assertEqual([pair["name"] for pair in saved["pairs"]], ["Test", "Docs"])

        before = path.read_text(encoding="utf-8")
        invalid = {
            "pairs": [
                {"name": "A", "local": str(self.local), "onedrive": str(self.cloud)},
                {"name": "A", "local": str(self.local / "sub"), "onedrive": str(self.local)},
                {"name": "", "local": "", "onedrive": str(self.cloud)},
            ],
            "auto_delete_limit": -1,
        }
        errors = sync.save_config_payload(invalid, path)
        self.assertTrue(any("duplicate" in error.lower() for error in errors))
        self.assertTrue(any("overlap" in error.lower() for error in errors))
        self.assertTrue(any("required" in error.lower() for error in errors))
        self.assertTrue(any("auto_delete_limit" in error for error in errors))
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_default_config_location_is_application_support(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SYNCBRIDGE_CONFIG", None)
            self.assertEqual(
                sync.default_config_path(),
                Path.home() / "Library/Application Support/SyncBridge/config.json",
            )
        with mock.patch.dict(os.environ, {"SYNCBRIDGE_CONFIG": str(self.root / "c.json")}):
            self.assertEqual(sync.default_config_path(), self.root / "c.json")
        path = self.root / "minimal.json"
        path.write_text(
            json.dumps({"pairs": [{"name": "T", "local": str(self.local), "onedrive": str(self.cloud)}]}),
            encoding="utf-8",
        )
        self.assertEqual(
            sync.load_config(str(path)).state_dir,
            Path.home() / "Library/Application Support/SyncBridge/state",
        )

    def test_trash_retention_and_interval_come_from_config(self):
        config = sync.load_config(
            str(self.write_config(trash_retention_days=7, sync_interval_minutes=30))
        )
        self.assertEqual(config.trash_retention_days, 7)
        self.assertEqual(config.sync_interval_minutes, 30)
        sync.CONFIG = config
        batch = sync.trash_root() / "batch"
        batch.mkdir(parents=True)
        eight_days_ago = sync.time.time() - 8 * 86400
        os.utime(batch, (eight_days_ago, eight_days_ago))
        sync.prune_trash()
        self.assertFalse(batch.exists())



def core_xml(author="", revision=1):
    """The save metadata of an Office file."""

    return (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties">'
        f"<cp:lastModifiedBy>{author}</cp:lastModifiedBy><cp:revision>{revision}</cp:revision>"
        "</cp:coreProperties>"
    )


def office_file(paragraphs, author="", revision=1):
    """A minimal Word file with the given paragraphs and save metadata."""

    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return zip_file({
        "docProps/core.xml": core_xml(author, revision),
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>",
    })


class ConflictAnalysisTests(unittest.TestCase):
    """Suggestions for open conflicts, and the engine side of AI help and merges."""

    write_both = SyncSafetyTests.write_both
    baseline = SyncSafetyTests.baseline
    tearDown = SyncSafetyTests.tearDown
    setUp = AppInterfaceTests.setUp

    def make_conflict(self, name, local, cloud, local_time=1700090000, cloud_time=1700000000):
        self.write_both(name, b"base")
        sync.save_state({"Test": self.baseline()})
        for base, content, stamp in ((self.local, local, local_time), (self.cloud, cloud, cloud_time)):
            path = base / name
            if content is None:
                path.unlink()
                continue
            path.write_bytes(content)
            os.utime(path, (stamp, stamp))
        sync.sync_all(no_delete=True)
        self.assertEqual(len(sync.load_conflicts()), 1)

    def analysis(self, name):
        return sync.analyze_conflict(self.pair, name)

    def test_office_files_report_author_revision_and_text(self):
        path = self.root / "plan.docx"
        path.write_bytes(office_file(["Scope", "Timeline"], author="Anna", revision=14))

        info = sync.office_info(path)

        self.assertEqual(info["author"], "Anna")
        self.assertEqual(info["revision"], 14)
        self.assertEqual(info["paragraphs"], ["Scope", "Timeline"])

    def test_version_that_contains_the_other_is_suggested_with_high_confidence(self):
        self.make_conflict(
            "plan.docx",
            local=office_file(["Scope", "Timeline"], author="Me", revision=12),
            cloud=office_file(["Scope", "Timeline", "New risk section"], author="Anna", revision=14),
            local_time=1700090000,
            cloud_time=1700000000,
        )

        result = self.analysis("plan.docx")

        # OneDrive is older by time but holds everything the laptop has, plus more.
        self.assertEqual(result["suggestion"], "onedrive")
        self.assertEqual(result["confidence"], "high")
        self.assertIn("contains everything", result["reason"])
        self.assertEqual(result["onedrive_info"]["author"], "Anna")
        self.assertEqual(result["local_info"]["revision"], 12)

    def test_both_sides_with_own_changes_need_a_merge(self):
        self.make_conflict(
            "notes.md",
            local=b"intro\nlocal idea\n",
            cloud=b"intro\ncloud idea\nsecond cloud idea\n",
        )

        result = self.analysis("notes.md")

        self.assertEqual(result["suggestion"], "merge")
        self.assertIn("1", result["reason"])
        self.assertIn("2", result["reason"])
        self.assertTrue(result["mergeable"])

    def test_same_text_suggests_the_newer_file_with_medium_confidence(self):
        self.make_conflict(
            "plan.docx",
            local=office_file(["Scope"], revision=3),
            cloud=office_file(["Scope"], revision=4),
            local_time=1700000000,
            cloud_time=1700090000,
        )

        result = self.analysis("plan.docx")

        self.assertEqual(result["suggestion"], "onedrive")
        self.assertEqual(result["confidence"], "medium")
        self.assertIn("same text", result["reason"])
        self.assertFalse(result["mergeable"])

    def test_files_without_readable_text_fall_back_to_newer_with_low_confidence(self):
        self.make_conflict("logo.png", local=b"\x89PNG local", cloud=b"\x89PNG cloud")

        result = self.analysis("logo.png")

        self.assertEqual(result["suggestion"], "local")
        self.assertEqual(result["confidence"], "low")

    def test_damaged_office_file_does_not_break_the_analysis(self):
        self.make_conflict("plan.docx", local=b"not a zip", cloud=office_file(["Scope"]))

        result = self.analysis("plan.docx")

        # The damaged laptop file is newer, but the intact OneDrive file is the one to keep.
        self.assertEqual(result["suggestion"], "onedrive")
        self.assertEqual(result["confidence"], "high")
        self.assertIn("damaged", result["reason"])

    def test_same_text_with_other_pictures_names_the_pictures(self):
        self.make_conflict(
            "plan.docx",
            local=word_file(["Scope"], image=b"picture one"),
            cloud=word_file(["Scope"], image=b"picture two"),
        )

        result = self.analysis("plan.docx")

        self.assertIn("pictures", result["reason"])
        self.assertNotIn("only formatting", result["reason"])

    def test_same_text_with_other_links_does_not_say_only_formatting(self):
        links = "word/_rels/document.xml.rels"
        self.make_conflict(
            "plan.docx",
            local=word_file(["Scope"], parts={links: "https://a.example"}),
            cloud=word_file(["Scope"], parts={links: "https://b.example"}),
        )

        result = self.analysis("plan.docx")

        self.assertEqual(result["confidence"], "low")
        self.assertIn("links", result["reason"])
        self.assertNotIn("only formatting", result["reason"])

    def test_date_in_the_future_is_named_and_decides_nothing(self):
        self.make_conflict(
            "plan.docx",
            local=office_file(["Scope"], revision=3),
            cloud=office_file(["Scope"], revision=4),
            cloud_time=int(time.time()) + 3600,
        )

        result = self.analysis("plan.docx")

        self.assertIsNone(result["suggestion"])
        self.assertIn("future", result["reason"])

    def test_text_that_stops_in_the_middle_of_a_line_looks_cut_off(self):
        self.make_conflict(
            "notes.md",
            local=b"intro\nthe plan",
            cloud=b"intro\nthe plan is to grow\nend\n",
        )

        result = self.analysis("notes.md")

        self.assertEqual(result["suggestion"], "onedrive")
        self.assertEqual(result["confidence"], "medium")
        self.assertIn("cut off", result["reason"])

    def test_excel_analysis_sees_changed_numbers(self):
        self.make_conflict(
            "plan.xlsx",
            local=excel_file({"A1": ("s", 0), "B1": 10}, ("Total",)),
            cloud=excel_file({"A1": ("s", 0), "B1": 99}, ("Total",)),
        )

        result = self.analysis("plan.xlsx")

        self.assertEqual(result["suggestion"], "merge")
        self.assertIn("sheet1!B1: 10", sync.document_paragraphs(self.local / "plan.xlsx"))
        self.assertIn("sheet1!A1: Total", sync.document_paragraphs(self.local / "plan.xlsx"))

    def test_edit_against_deletion_gets_no_suggestion(self):
        self.make_conflict("plan.md", local=b"edited\n", cloud=None)

        result = self.analysis("plan.md")

        self.assertIsNone(result["suggestion"])
        self.assertIn("deleted", result["reason"])

    def test_conflict_list_includes_the_analysis(self):
        self.make_conflict("notes.md", local=b"intro\nlocal\n", cloud=b"intro\ncloud\n")

        item = sync.conflicts_payload()[0]

        self.assertEqual(item["analysis"]["suggestion"], "merge")
        json.dumps(item)

    def test_texts_for_ai_contain_both_versions_and_are_shortened(self):
        self.make_conflict("notes.md", local=b"L" * 500, cloud=b"C" * 500)

        payload = sync.conflict_texts_payload("Test", "notes.md", max_chars=100)

        self.assertEqual(payload["file"], "notes.md")
        self.assertTrue(payload["local_text"].startswith("L" * 100))
        self.assertLess(len(payload["local_text"]), 200)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["mergeable"])
        self.assertIn("C", payload["onedrive_text"])

    def test_texts_for_ai_read_office_documents(self):
        self.make_conflict(
            "plan.docx", local=office_file(["Scope"]), cloud=office_file(["Scope", "Risks"])
        )

        payload = sync.conflict_texts_payload("Test", "plan.docx")

        self.assertEqual(payload["onedrive_text"], "Scope\nRisks")
        self.assertFalse(payload["mergeable"])

    def test_approved_merge_is_written_to_both_sides_and_old_versions_are_kept(self):
        self.make_conflict("notes.md", local=b"intro\nlocal\n", cloud=b"intro\ncloud\n")

        result = sync.resolve_conflicts_batch(
            [{"pair": "Test", "file": "notes.md", "keep": "merged", "merged_text": "intro\nlocal\ncloud\n"}]
        )

        self.assertEqual(result["done"], ["notes.md"])
        for base in (self.local, self.cloud):
            self.assertEqual((base / "notes.md").read_text(), "intro\nlocal\ncloud\n")
        kept = sorted(p.read_bytes() for p in sync.trash_root().rglob("notes*.md"))
        self.assertEqual(kept, [b"intro\ncloud\n", b"intro\nlocal\n"])
        self.assertEqual(sync.conflicts_payload(), [])
        state = sync.load_state()["pairs"]["Test"]
        after = sync.sync_pair(self.pair, state, "test", no_delete=True, dry_run=True)
        self.assertEqual(after["to_onedrive_files"] + after["to_local_files"], [])

    def test_merge_is_refused_for_office_files(self):
        self.make_conflict("plan.docx", local=office_file(["A"]), cloud=office_file(["B"]))

        result = sync.resolve_conflicts_batch(
            [{"pair": "Test", "file": "plan.docx", "keep": "merged", "merged_text": "A\nB"}]
        )

        self.assertEqual(result["done"], [])
        self.assertIn("merge", result["failed"][0]["error"])
        self.assertEqual(len(sync.conflicts_payload()), 1)


if __name__ == "__main__":
    unittest.main()


class OptionTestBase(unittest.TestCase):
    """Shared setup for the FreeFileSync-style options."""

    write_both = SyncSafetyTests.write_both
    baseline = SyncSafetyTests.baseline
    tearDown = SyncSafetyTests.tearDown

    def setUp(self):
        SyncSafetyTests.setUp(self)
        notify = mock.patch.object(sync, "notify")
        notify.start()
        self.addCleanup(notify.stop)

    def use_pair(self, **options):
        self.pair = sync.SyncPair("Test", self.local, self.cloud, **options)
        sync.CONFIG.pairs = [self.pair]
        return self.pair

    def run_pair(self, previous):
        return sync.sync_pair(self.pair, previous, "test", no_delete=False)


class SyncVariantTests(OptionTestBase):
    """Two way, Mirror, Update and Custom, as in FreeFileSync."""

    def test_variant_presets_map_each_change_to_a_direction(self):
        two_way = sync.effective_directions(self.use_pair())
        self.assertEqual(two_way["local_new"], "to_onedrive")
        self.assertEqual(two_way["local_deleted"], "to_onedrive")
        self.assertEqual(two_way["onedrive_changed"], "to_local")

        mirror = sync.effective_directions(self.use_pair(variant="mirror", direction="to_onedrive"))
        self.assertEqual(set(mirror.values()), {"to_onedrive"})

        update = sync.effective_directions(self.use_pair(variant="update", direction="to_local"))
        self.assertEqual(update["onedrive_new"], "to_local")
        self.assertEqual(update["onedrive_changed"], "to_local")
        self.assertEqual(update["onedrive_deleted"], "none")
        self.assertEqual(update["local_new"], "none")

    def test_mirror_makes_onedrive_an_exact_copy_of_local(self):
        self.use_pair(variant="mirror", direction="to_onedrive")
        for name in ("edited.docx", "deleted.docx", "both.docx"):
            self.write_both(name, b"base")
        previous = self.baseline()
        (self.cloud / "edited.docx").write_bytes(b"cloud edit")
        (self.cloud / "deleted.docx").unlink()
        (self.cloud / "cloud-only.docx").write_bytes(b"new in cloud")
        (self.local / "both.docx").write_bytes(b"local edit")
        (self.cloud / "both.docx").write_bytes(b"cloud edit")

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.cloud / "edited.docx").read_bytes(), b"base")
        self.assertEqual((self.cloud / "deleted.docx").read_bytes(), b"base")
        self.assertEqual((self.cloud / "both.docx").read_bytes(), b"local edit")
        self.assertFalse((self.cloud / "cloud-only.docx").exists())
        self.assertFalse((self.local / "cloud-only.docx").exists())

    def test_update_copies_new_and_changed_files_but_never_deletes(self):
        self.use_pair(variant="update", direction="to_onedrive")
        self.write_both("kept.docx", b"base")
        self.write_both("changed.docx", b"base")
        previous = self.baseline()
        (self.local / "kept.docx").unlink()
        (self.local / "changed.docx").write_bytes(b"local edit")
        (self.local / "new.docx").write_bytes(b"new")
        (self.cloud / "cloud-only.docx").write_bytes(b"stays in cloud")

        first = self.run_pair(previous)
        second = self.run_pair(first["state"])

        self.assertEqual((self.cloud / "kept.docx").read_bytes(), b"base")
        self.assertEqual((self.cloud / "changed.docx").read_bytes(), b"local edit")
        self.assertEqual((self.cloud / "new.docx").read_bytes(), b"new")
        self.assertFalse((self.local / "cloud-only.docx").exists())
        self.assertFalse((self.local / "kept.docx").exists())
        for result in (first, second):
            self.assertEqual(result["conflicts"], [])
        self.assertEqual(second["to_onedrive_files"], [])
        self.assertEqual(second["deleted_onedrive_files"], [])

    def test_custom_directions_per_change_category(self):
        self.use_pair(
            variant="custom",
            directions={
                "local_new": "to_onedrive",
                "local_changed": "to_onedrive",
                "local_deleted": "none",
                "onedrive_new": "none",
                "onedrive_changed": "to_local",
                "onedrive_deleted": "to_local",
            },
        )
        self.write_both("removed-locally.docx", b"base")
        self.write_both("removed-in-cloud.docx", b"base")
        previous = self.baseline()
        (self.local / "removed-locally.docx").unlink()
        (self.cloud / "removed-in-cloud.docx").unlink()
        (self.cloud / "cloud-new.docx").write_bytes(b"x")

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertTrue((self.cloud / "removed-locally.docx").exists())
        self.assertFalse((self.local / "removed-in-cloud.docx").exists())
        self.assertFalse((self.local / "cloud-new.docx").exists())

    def test_opposite_directions_for_a_file_changed_on_both_sides_is_a_conflict(self):
        self.use_pair(variant="custom", directions=dict(sync.effective_directions(self.use_pair())))
        self.write_both("report.docx", b"base")
        previous = self.baseline()
        (self.local / "report.docx").write_bytes(b"local")
        (self.cloud / "report.docx").write_bytes(b"cloud")

        result = self.run_pair(previous)

        self.assertEqual(len(result["conflicts"]), 1)


class FilterTests(OptionTestBase):
    """Per-pair include/exclude, size and time span filters."""

    def test_include_and_exclude_patterns_per_pair(self):
        self.use_pair(filter=sync.PairFilter(include=("Projects/**",), exclude=("*.mov",)))
        previous = self.baseline()
        for name in ("Projects/plan.docx", "Projects/clip.mov", "Private/notes.docx"):
            (self.local / name).parent.mkdir(parents=True, exist_ok=True)
            (self.local / name).write_bytes(b"x")

        result = self.run_pair(previous)

        self.assertEqual(result["to_onedrive_files"], ["Projects/plan.docx"])
        self.assertEqual(result["conflicts"], [])

    def test_size_filter_skips_large_files_without_deleting_the_other_copy(self):
        self.use_pair(filter=sync.PairFilter(max_size_bytes=10))
        self.write_both("video.bin", b"small")
        previous = sync.refresh_pair_state(self.pair, force_full=True)
        (self.local / "video.bin").write_bytes(b"x" * 100)
        (self.local / "large-new.bin").write_bytes(b"x" * 100)

        result = self.run_pair(previous)

        self.assertEqual((self.cloud / "video.bin").read_bytes(), b"small")
        self.assertFalse((self.cloud / "large-new.bin").exists())
        self.assertEqual(result["deleted_onedrive_files"], [])
        self.assertEqual(result["conflicts"], [])

    def test_time_span_filter_syncs_only_recently_changed_files(self):
        self.use_pair(filter=sync.PairFilter(max_age_days=30))
        previous = self.baseline()
        old = datetime.now().timestamp() - 100 * 86400
        (self.local / "old.docx").write_bytes(b"old")
        os.utime(self.local / "old.docx", (old, old))
        (self.local / "recent.docx").write_bytes(b"recent")

        result = self.run_pair(previous)

        self.assertEqual(result["to_onedrive_files"], ["recent.docx"])

    def test_adding_a_filter_never_deletes_files_that_are_now_excluded(self):
        self.write_both("archive/old.docx", b"keep")
        previous = self.baseline()
        self.use_pair(filter=sync.PairFilter(exclude=("archive/**",)))

        result = self.run_pair(previous)

        self.assertTrue((self.local / "archive/old.docx").exists())
        self.assertTrue((self.cloud / "archive/old.docx").exists())
        self.assertEqual(result["deleted_local_files"] + result["deleted_onedrive_files"], [])
        self.assertNotIn("archive/old.docx", result["state"]["local"])


class DeletionHandlingTests(OptionTestBase):
    """Versioning, macOS Trash or permanent delete, for deleted and overwritten files."""

    def overwrite_in_cloud(self, name="report.docx"):
        self.write_both(name, b"old")
        previous = self.baseline()
        (self.local / name).write_bytes(b"new")
        return self.run_pair(previous)

    def test_versioning_keeps_overwritten_files_not_only_deleted_ones(self):
        self.overwrite_in_cloud()

        version = sync.trash_root() / sync.RUN_ID / "Test" / "onedrive" / "report.docx"
        self.assertEqual(version.read_bytes(), b"old")

    def test_versioning_style_timestamp_file_appends_the_time_to_the_name(self):
        sync.CONFIG.versioning_style = "timestamp_file"
        self.overwrite_in_cloud()

        version = sync.trash_root() / "Test" / "onedrive" / f"report {sync.RUN_ID}.docx"
        self.assertEqual(version.read_bytes(), b"old")

    def test_versioning_style_replace_keeps_only_the_last_version(self):
        sync.CONFIG.versioning_style = "replace"
        self.overwrite_in_cloud()

        version = sync.trash_root() / "Test" / "onedrive" / "report.docx"
        self.assertEqual(version.read_bytes(), b"old")

    def test_version_limits_keep_max_count_and_at_least_min_count(self):
        sync.CONFIG.versioning_style = "timestamp_file"
        folder = sync.trash_root() / "Test" / "local"
        folder.mkdir(parents=True)
        for day in range(1, 6):
            (folder / f"plan 2000-01-0{day}_000000.docx").write_bytes(b"x")

        sync.CONFIG.versioning_max_count = 3
        sync.prune_trash(retention_days=0)
        sync.CONFIG.versioning_min_count = 2
        sync.CONFIG.versioning_max_count = 0
        sync.prune_trash(retention_days=30)

        self.assertEqual(
            sorted(path.name for path in folder.iterdir()),
            ["plan 2000-01-04_000000.docx", "plan 2000-01-05_000000.docx"],
        )

    def test_macos_trash_moves_deleted_files_to_the_finder_trash(self):
        sync.CONFIG.deletion_handling = "trash"
        finder_trash = self.root / "Finder Trash"
        finder_trash.mkdir()
        self.write_both("file.docx", b"content")
        previous = self.baseline()
        (self.cloud / "file.docx").unlink()

        with mock.patch.object(sync, "macos_trash_dir", return_value=finder_trash):
            self.run_pair(previous)

        self.assertEqual((finder_trash / "file.docx").read_bytes(), b"content")
        self.assertFalse(sync.trash_root().exists())

    def test_permanent_deletion_keeps_no_copy(self):
        sync.CONFIG.deletion_handling = "permanent"
        self.write_both("file.docx", b"content")
        previous = self.baseline()
        (self.cloud / "file.docx").unlink()

        self.run_pair(previous)

        self.assertFalse((self.local / "file.docx").exists())
        self.assertFalse(sync.trash_root().exists())


class FailSafeCopyTests(OptionTestBase):
    def test_interrupted_copy_leaves_the_old_file_intact(self):
        self.write_both("report.docx", b"old")
        previous = self.baseline()
        (self.local / "report.docx").write_bytes(b"new")

        def broken_copy(src, dst, *args, **kwargs):
            Path(dst).write_bytes(b"partial")
            raise OSError("disk full")

        with mock.patch.object(sync.shutil, "copyfile", side_effect=broken_copy):
            result = self.run_pair(previous)

        self.assertEqual(result["errors"], 1)
        self.assertEqual((self.cloud / "report.docx").read_bytes(), b"old")
        self.assertEqual(sorted(path.name for path in self.cloud.iterdir()), ["report.docx"])


class PreviewAndRunOptionsTests(OptionTestBase):
    """Compare/preview with per-file overrides, single-pair runs, command after sync."""

    def setUp(self):
        super().setUp()
        self.write_both("cloud-edit.docx", b"base")
        sync.save_state({"Test": self.baseline()})
        (self.local / "new.docx").write_bytes(b"new")
        (self.cloud / "cloud-edit.docx").write_bytes(b"cloud edit")

    def test_preview_lists_planned_actions_without_changing_anything(self):
        preview = sync.preview_payload()

        self.assertEqual(
            [(item["pair"], item["file"], item["action"]) for item in preview["actions"]],
            [
                ("Test", "cloud-edit.docx", "copy_onedrive_to_local"),
                ("Test", "new.docx", "copy_local_to_onedrive"),
            ],
        )
        self.assertFalse((self.cloud / "new.docx").exists())
        self.assertEqual((self.local / "cloud-edit.docx").read_bytes(), b"base")
        json.dumps(preview)

    def test_preview_prints_nothing_and_never_notifies(self):
        import contextlib
        import io

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sync.preview_payload()

        self.assertEqual(output.getvalue(), "")
        sync.notify.assert_not_called()

    def test_overrides_skip_or_reverse_single_files(self):
        sync.sync_all(
            no_delete=True,
            overrides=[
                {"pair": "Test", "file": "new.docx", "action": "skip"},
                {"pair": "Test", "file": "cloud-edit.docx", "action": "copy_local_to_onedrive"},
            ],
        )

        self.assertFalse((self.cloud / "new.docx").exists())
        self.assertEqual((self.cloud / "cloud-edit.docx").read_bytes(), b"base")
        remaining = [item["file"] for item in sync.preview_payload()["actions"]]
        self.assertEqual(remaining, ["new.docx"])

    def test_sync_can_run_for_selected_pairs_only(self):
        other_local, other_cloud = self.root / "other-local", self.root / "other-cloud"
        other_local.mkdir()
        other_cloud.mkdir()
        (other_local / "x.docx").write_bytes(b"x")
        sync.CONFIG.pairs = [self.pair, sync.SyncPair("Other", other_local, other_cloud)]

        sync.sync_all(no_delete=True, pair_names=["Other"])

        self.assertTrue((other_cloud / "x.docx").exists())
        self.assertFalse((self.cloud / "new.docx").exists())
        self.assertIn("Test", sync.load_state()["pairs"])

    def test_command_after_sync_runs_with_status_file(self):
        sync.CONFIG.run_after_sync = "say done"
        with mock.patch.object(sync.subprocess, "run") as run:
            sync.sync_all(no_delete=True)
            sync.sync_all(dry_run=True)

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], "say done")
        self.assertTrue(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["env"]["SYNCBRIDGE_STATUS"], str(sync.status_file()))


class OptionConfigTests(OptionTestBase):
    def pairs(self, **options):
        return [{"name": "T", "local": str(self.local), "onedrive": str(self.cloud), **options}]

    def test_pair_options_are_loaded_from_config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "pairs": self.pairs(
                variant="update",
                direction="to_onedrive",
                filter={"exclude": ["*.mov"], "max_size_bytes": 1000, "max_age_days": 7},
            ),
            "deletion_handling": "versioning",
            "versioning_style": "timestamp_file",
            "versioning_max_count": 10,
            "versioning_min_count": 1,
            "run_after_sync": "echo ok",
        }), encoding="utf-8")

        config = sync.load_config(str(path))

        pair = config.pairs[0]
        self.assertEqual((pair.variant, pair.direction), ("update", "to_onedrive"))
        self.assertEqual(pair.filter, sync.PairFilter(exclude=("*.mov",), max_size_bytes=1000, max_age_days=7))
        self.assertEqual(config.versioning_style, "timestamp_file")
        self.assertEqual((config.versioning_max_count, config.versioning_min_count), (10, 1))
        self.assertEqual(config.run_after_sync, "echo ok")

    def test_invalid_options_are_rejected(self):
        cases = [
            ({"pairs": self.pairs(variant="sideways")}, "variant"),
            ({"pairs": self.pairs(variant="mirror")}, "direction"),
            ({"pairs": self.pairs(variant="custom", directions={"local_new": "up"})}, "directions"),
            ({"pairs": self.pairs(filter={"max_size_bytes": -1})}, "max_size_bytes"),
            ({"pairs": self.pairs(), "deletion_handling": "shred"}, "deletion_handling"),
            ({"pairs": self.pairs(), "versioning_style": "zip"}, "versioning_style"),
        ]
        for payload, field in cases:
            with self.subTest(field=field):
                errors = sync.validate_config_payload(payload)
                self.assertTrue(any(field in error for error in errors), errors)


class AppIntegrationTests(OptionTestBase):
    def test_status_lists_counts_and_options_per_pair(self):
        self.use_pair(variant="update", direction="to_onedrive")
        sync.save_conflicts([{"pair": "Test", "file": "a.docx", "reason": "x"}])
        sync.save_pending_deletion_snapshot(
            [sync.PendingDeletion("Test", "delete_onedrive", "b.docx")]
        )

        pair = sync.status_payload()["pairs"][0]

        self.assertEqual((pair["conflicts"], pair["pending_deletions"]), (1, 1))
        self.assertEqual((pair["variant"], pair["direction"]), ("update", "to_onedrive"))

    def test_notifications_are_handed_to_the_app_so_they_show_its_icon(self):
        with mock.patch.dict(os.environ, {"SYNCBRIDGE_APP_NOTIFICATIONS": "1"}), \
                mock.patch.object(sync.subprocess, "run") as run, \
                mock.patch("builtins.print") as printed:
            REAL_NOTIFY("Sync Complete", "1 synced")

        run.assert_not_called()
        line = printed.call_args.args[0]
        self.assertTrue(line.startswith(sync.APP_NOTIFICATION_PREFIX))
        self.assertEqual(
            json.loads(line[len(sync.APP_NOTIFICATION_PREFIX):]),
            {"title": "Sync Complete", "message": "1 synced", "open": "summary"},
        )

    def test_new_conflict_notification_opens_the_conflicts_window_when_clicked(self):
        self.write_both("both.docx", b"base")
        sync.save_state({"Test": self.baseline()})
        (self.local / "both.docx").write_bytes(b"local edit")
        (self.cloud / "both.docx").write_bytes(b"cloud edit")
        sync.CONFIG.conflict_rule = "ask"

        sync.sync_all(no_delete=True)

        title, message = sync.notify.call_args.args[:2]
        self.assertIn("conflict", title.lower())
        self.assertEqual(sync.notify.call_args.kwargs.get("open"), "conflicts")


class EmptyFolderTests(OptionTestBase):
    """Empty folders are synced like files: created, removed and filtered."""

    def setUp(self):
        super().setUp()
        self.use_pair()
        self.write_both("Project/notes.docx", b"base")

    def test_empty_folder_created_on_one_side_is_created_on_the_other(self):
        previous = self.baseline()
        (self.local / "Project" / "01_Audits").mkdir()
        (self.cloud / "Cloud Only" / "Inbox").mkdir(parents=True)

        result = self.run_pair(previous)

        self.assertTrue((self.cloud / "Project" / "01_Audits").is_dir())
        self.assertTrue((self.local / "Cloud Only" / "Inbox").is_dir())
        self.assertEqual(result["created_onedrive_dirs"], ["Project/01_Audits"])
        self.assertEqual(result["created_local_dirs"], ["Cloud Only", "Cloud Only/Inbox"])
        self.assertIn("Project/01_Audits", result["state"]["onedrive_dirs"])

    def test_brand_new_pair_creates_empty_folders_both_ways(self):
        (self.local / "Local Empty").mkdir()
        (self.cloud / "Cloud Empty").mkdir()

        self.run_pair(None)

        self.assertTrue((self.cloud / "Local Empty").is_dir())
        self.assertTrue((self.local / "Cloud Empty").is_dir())

    def test_first_run_after_update_only_creates_folders(self):
        previous = self.baseline()
        for key in ("local_dirs", "onedrive_dirs"):
            previous.pop(key, None)  # state written by an older version
        (self.local / "Template" / "02_Norm_Gap").mkdir(parents=True)
        (self.cloud / "Old Empty").mkdir()

        result = self.run_pair(previous)
        second = self.run_pair(result["state"])

        # Laptop folders go to OneDrive; old empty OneDrive leftovers stay
        # where they are and are not copied to the laptop.
        self.assertTrue((self.cloud / "Template" / "02_Norm_Gap").is_dir())
        self.assertTrue((self.cloud / "Old Empty").is_dir())
        self.assertFalse((self.local / "Old Empty").exists())
        self.assertEqual(result["deleted_onedrive_dirs"] + result["deleted_local_dirs"], [])
        self.assertEqual(second["created_local_dirs"] + second["created_onedrive_dirs"], [])

    def test_deleted_empty_folder_is_removed_on_the_other_side(self):
        for base in (self.local, self.cloud):
            (base / "Project" / "Drafts").mkdir()
            (base / "Project" / "Drafts" / ".DS_Store").write_bytes(b"finder")
        previous = self.baseline()
        shutil.rmtree(self.local / "Project" / "Drafts")

        result = self.run_pair(previous)

        self.assertFalse((self.cloud / "Project" / "Drafts").exists())
        self.assertEqual(result["deleted_onedrive_dirs"], ["Project/Drafts"])
        second = self.run_pair(result["state"])
        self.assertFalse((self.local / "Project" / "Drafts").exists())
        self.assertEqual(second["created_local_dirs"], [])

    def test_folder_with_files_on_the_other_side_is_never_removed(self):
        for base in (self.local, self.cloud):
            (base / "Project" / "Shared").mkdir()
        previous = self.baseline()
        (self.local / "Project" / "Shared").rmdir()
        (self.cloud / "Project" / "Shared" / "new.docx").write_bytes(b"new in cloud")

        result = self.run_pair(previous)

        self.assertTrue((self.cloud / "Project" / "Shared" / "new.docx").exists())
        self.assertEqual(result["deleted_onedrive_dirs"], [])

    def test_case_only_folder_rename_never_deletes_the_folder(self):
        for base in (self.local, self.cloud):
            (base / "Project" / "Archive").mkdir()
        previous = self.baseline()
        (self.local / "Project" / "Archive").rename(self.local / "Project" / "archive")

        result = self.run_pair(previous)

        self.assertEqual(result["deleted_onedrive_dirs"], [])
        self.assertTrue(any(
            name.lower() == "archive" for name in os.listdir(self.cloud / "Project")
        ))

    def test_excluded_and_filtered_folders_are_not_created(self):
        self.use_pair(filter=sync.PairFilter(exclude=["Project/private/**"]))
        previous = self.baseline()
        (self.local / "node_modules").mkdir()
        (self.local / "Project" / "private").mkdir()

        result = self.run_pair(previous)

        self.assertFalse((self.cloud / "node_modules").exists())
        self.assertFalse((self.cloud / "Project" / "private").exists())
        self.assertEqual(result["created_onedrive_dirs"], [])

    def test_one_way_variants_create_folders_only_in_their_direction(self):
        self.use_pair(variant="update", direction="to_onedrive")
        for base in (self.local, self.cloud):
            (base / "Kept").mkdir()
        previous = self.baseline()
        (self.local / "Kept").rmdir()
        (self.local / "New Local").mkdir()
        (self.cloud / "New Cloud").mkdir()

        result = self.run_pair(previous)

        self.assertTrue((self.cloud / "New Local").is_dir())
        self.assertFalse((self.local / "New Cloud").exists())
        self.assertTrue((self.cloud / "Kept").is_dir())
        self.assertEqual(result["deleted_onedrive_dirs"], [])

    def test_dry_run_and_preview_list_folder_actions_without_changes(self):
        sync.save_state({"Test": self.baseline()})
        (self.local / "Planned").mkdir()

        dry = sync.sync_pair(self.pair, sync.load_state()["pairs"]["Test"], "test", dry_run=True)
        preview = sync.preview_payload()

        self.assertEqual(dry["created_onedrive_dirs"], ["Planned"])
        self.assertIn(
            ("Test", "Planned", "create_folder_onedrive"),
            [(item["pair"], item["file"], item["action"]) for item in preview["actions"]],
        )
        self.assertFalse((self.cloud / "Planned").exists())

    def test_folder_holding_files_is_left_to_the_file_sync(self):
        """A folder whose files wait for review is not recreated empty on the other side."""
        self.write_both("Old/report.docx", b"base")
        previous = self.baseline()
        for key in ("local_dirs", "onedrive_dirs"):
            previous.pop(key)  # first run after the update
        shutil.rmtree(self.local / "Old")

        result = sync.sync_pair(self.pair, previous, "test", no_delete=True)

        self.assertEqual(result["skipped_delete_files"], ["Old/report.docx"])
        self.assertFalse((self.local / "Old").exists())
        self.assertEqual(result["created_local_dirs"], [])


class LockWaitTests(OptionTestBase):
    """App commands (resolve, apply deletions) wait for a running sync."""

    def test_app_command_waits_for_a_running_sync_instead_of_failing(self):
        import threading

        running_sync = sync.acquire_lock()
        threading.Timer(0.3, sync.release_lock, args=[running_sync]).start()

        with mock.patch("builtins.print") as printed:
            code = sync.run_locked(lambda: {"done": ["a.docx"]}, wait_seconds=5)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed.call_args.args[0]), {"done": ["a.docx"]})

    def test_app_command_reports_busy_after_waiting_too_long(self):
        running_sync = sync.acquire_lock()
        self.addCleanup(sync.release_lock, running_sync)

        with mock.patch("builtins.print") as printed:
            code = sync.run_locked(lambda: {"done": []}, wait_seconds=0.2)

        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(printed.call_args.args[0]))


class FolderRuleTests(OptionTestBase):
    """Fixed rules: protected folders, per-folder conflict rules and author priority."""

    def rules(self, *rules, authors=()):
        sync.CONFIG.folder_rules = tuple(sync.FolderRule(pattern, rule) for pattern, rule in rules)
        sync.CONFIG.author_priority = tuple(authors)

    def edit_both(self, name, local, cloud, local_time=1700000000, cloud_time=1700090000):
        self.write_both(name, b"base")
        previous = self.baseline()
        (self.local / name).write_bytes(local)
        (self.cloud / name).write_bytes(cloud)
        os.utime(self.local / name, (local_time, local_time))
        os.utime(self.cloud / name, (cloud_time, cloud_time))
        return previous

    def edit_both_office(self, name, local_author, cloud_author):
        return self.edit_both(
            name,
            office_file(["base", "local"], author=local_author),
            office_file(["base", "cloud"], author=cloud_author),
        )

    def backups(self, side, name):
        return list(sync.trash_root().glob(f"*/Test/{side}/{name}"))

    # Protected folders

    def test_protected_file_deleted_on_onedrive_is_restored_there(self):
        self.rules(("Clients/**", "protect"))
        self.write_both("Clients/plan.docx", b"keep me")
        previous = self.baseline()
        (self.cloud / "Clients/plan.docx").unlink()

        result = self.run_pair(previous)

        self.assertEqual((self.local / "Clients/plan.docx").read_bytes(), b"keep me")
        self.assertEqual((self.cloud / "Clients/plan.docx").read_bytes(), b"keep me")
        self.assertEqual(result["to_onedrive_files"], ["Clients/plan.docx"])
        self.assertEqual(result["deleted_local_files"], [])

    def test_protected_file_deleted_locally_is_restored_locally(self):
        self.rules(("Clients/**", "protect"))
        self.write_both("Clients/plan.docx", b"keep me")
        previous = self.baseline()
        (self.local / "Clients/plan.docx").unlink()

        result = self.run_pair(previous)

        self.assertEqual((self.local / "Clients/plan.docx").read_bytes(), b"keep me")
        self.assertEqual(result["to_local_files"], ["Clients/plan.docx"])
        self.assertEqual(result["deleted_onedrive_files"], [])

    def test_protected_file_deleted_and_edited_keeps_the_edit_without_conflict(self):
        self.rules(("Clients/**", "protect"))
        self.write_both("Clients/plan.docx", b"base")
        previous = self.baseline()
        (self.cloud / "Clients/plan.docx").unlink()
        (self.local / "Clients/plan.docx").write_bytes(b"edited")

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.cloud / "Clients/plan.docx").read_bytes(), b"edited")
        self.assertEqual((self.local / "Clients/plan.docx").read_bytes(), b"edited")

    def test_deletions_outside_protected_folders_work_as_before(self):
        self.rules(("Clients/**", "protect"))
        self.write_both("Notes/old.txt", b"old")
        previous = self.baseline()
        (self.cloud / "Notes/old.txt").unlink()

        result = self.run_pair(previous)

        self.assertFalse((self.local / "Notes/old.txt").exists())
        self.assertEqual(result["deleted_local_files"], ["Notes/old.txt"])

    def test_large_deletion_batch_in_protected_folder_is_restored_not_held(self):
        self.rules(("Clients/**", "protect"))
        names = [f"Clients/file{index}.docx" for index in range(3)]
        for name in names:
            self.write_both(name, b"keep")
        previous = self.baseline()
        for name in names:
            (self.cloud / name).unlink()

        result = sync.sync_pair(self.pair, previous, "test", no_delete=False, delete_limit=1)

        self.assertEqual(result["skipped_delete_files"], [])
        self.assertEqual(sorted(result["to_onedrive_files"]), names)
        for name in names:
            self.assertTrue((self.cloud / name).exists())

    # Author priority (Office files)

    def test_author_priority_keeps_the_higher_ranked_authors_version(self):
        self.rules(authors=("Alice Example", "Bob Example"))
        previous = self.edit_both_office("report.docx", "Bob Example", "Alice Example")

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["to_local_files"], ["report.docx"])
        self.assertIn("cloud", sync.office_info(self.local / "report.docx")["paragraphs"])
        self.assertEqual(len(self.backups("local", "report.docx")), 1)

    def test_author_priority_reads_only_the_save_metadata(self):
        self.rules(authors=("Alice Example", "Bob Example"))
        broken_sheet = {"xl/worksheets/sheet1.xml": "<worksheet><unclosed>"}
        local, cloud = (
            zip_file({"docProps/core.xml": core_xml(author), **broken_sheet})
            for author in ("Bob Example", "Alice Example")
        )
        previous = self.edit_both("budget.xlsx", local, cloud)

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.local / "budget.xlsx").read_bytes(), cloud)

    def test_author_priority_with_the_same_author_leaves_the_conflict_open(self):
        self.rules(authors=("Alice Example", "Bob Example"))
        previous = self.edit_both_office("report.docx", "Alice Example", "Alice Example")

        result = self.run_pair(previous)

        self.assertEqual(len(result["conflicts"]), 1)

    def test_author_priority_ignores_unknown_authors_and_non_office_files(self):
        self.rules(authors=("Alice Example", "Bob Example"))

        def open_conflicts(previous):
            return [conflict["file"] for conflict in self.run_pair(previous)["conflicts"]]

        one_unknown = self.edit_both_office("report.docx", "Carol Example", "Alice Example")
        self.assertIn("report.docx", open_conflicts(one_unknown))
        both_unknown = self.edit_both_office("memo.docx", "Carol Example", "Dave Example")
        self.assertIn("memo.docx", open_conflicts(both_unknown))
        text = self.edit_both("notes.txt", b"local", b"cloud")
        self.assertIn("notes.txt", open_conflicts(text))

    def test_author_priority_does_not_decide_delete_versus_edit(self):
        self.rules(authors=("Alice Example", "Bob Example"))
        self.write_both("report.docx", office_file(["base"], author="Bob Example"))
        previous = self.baseline()
        (self.local / "report.docx").unlink()
        (self.cloud / "report.docx").write_bytes(office_file(["edited"], author="Alice Example"))

        result = self.run_pair(previous)

        self.assertEqual(len(result["conflicts"]), 1)
        self.assertFalse((self.local / "report.docx").exists())

    # Per-folder conflict rules

    def test_folder_rule_keep_local_wins_inside_its_folder_only_and_before_authors(self):
        self.rules(("Drafts/**", "keep_local"), authors=("Alice Example", "Bob Example"))
        inside = self.edit_both_office("Drafts/a.docx", "Bob Example", "Alice Example")
        result = self.run_pair(inside)
        self.assertEqual(result["conflicts"], [])
        self.assertIn("local", sync.office_info(self.cloud / "Drafts/a.docx")["paragraphs"])

        outside = self.edit_both("Other/b.txt", b"local", b"cloud")
        self.assertEqual(len(self.run_pair(outside)["conflicts"]), 1)

    def test_folder_rule_keep_onedrive_never_deletes(self):
        self.rules(("Client/**", "keep_onedrive"))
        edited = self.edit_both("Client/a.txt", b"local", b"cloud")
        self.assertEqual(self.run_pair(edited)["conflicts"], [])
        self.assertEqual((self.local / "Client/a.txt").read_bytes(), b"cloud")

        self.write_both("Client/b.txt", b"base")
        previous = self.baseline()
        (self.cloud / "Client/b.txt").unlink()
        (self.local / "Client/b.txt").write_bytes(b"edited")
        self.assertEqual(len(self.run_pair(previous)["conflicts"]), 1)
        self.assertTrue((self.local / "Client/b.txt").exists())

    def test_folder_rule_keep_newer(self):
        self.rules(("Work/**", "keep_newer"))
        previous = self.edit_both("Work/a.txt", b"local", b"cloud", local_time=1700090000, cloud_time=1700000000)

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.cloud / "Work/a.txt").read_bytes(), b"local")

    def test_folder_rule_keep_older(self):
        self.rules(("Archive/**", "keep_older"))
        previous = self.edit_both("Archive/a.txt", b"local", b"cloud", local_time=1700090000, cloud_time=1700000000)

        result = self.run_pair(previous)

        self.assertEqual(result["conflicts"], [])
        self.assertEqual((self.local / "Archive/a.txt").read_bytes(), b"cloud")

    # Config

    def test_rules_are_read_and_validated_from_config(self):
        path = self.root / "config.json"
        pairs = [{"name": "T", "local": str(self.local), "onedrive": str(self.cloud)}]
        rules = [{"pattern": "Clients/**", "rule": "protect"}, {"pattern": "Archive/**", "rule": "keep_older"}]
        path.write_text(
            json.dumps({"pairs": pairs, "folder_rules": rules, "author_priority": ["Alice Example"]}),
            encoding="utf-8",
        )
        config = sync.load_config(str(path))
        self.assertEqual(
            config.folder_rules,
            (sync.FolderRule("Clients/**", "protect"), sync.FolderRule("Archive/**", "keep_older")),
        )
        self.assertEqual(config.author_priority, ("Alice Example",))

        for payload, field in (
            ({"folder_rules": [{"pattern": "X/**", "rule": "random"}]}, "folder_rules"),
            ({"folder_rules": [{"pattern": " ", "rule": "protect"}]}, "folder_rules"),
            ({"author_priority": ["Alice", " "]}, "author_priority"),
        ):
            errors = sync.validate_config_payload({"pairs": pairs, **payload})
            self.assertTrue(any(field in error for error in errors), (payload, errors))


def zip_file(parts):
    """An Office-style package with the given parts (name -> text or bytes)."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def excel_file(cells, strings=()):
    """A minimal Excel file: cells maps a reference to a number, "=formula" or (cell type, value),
    e.g. ("s", string index) or ("b", 1) for TRUE."""

    def cell(reference, value):
        if isinstance(value, tuple):
            return f'<c r="{reference}" t="{value[0]}"><v>{value[1]}</v></c>'
        if str(value).startswith("="):
            return f'<c r="{reference}"><f>{value[1:]}</f><v>0</v></c>'
        return f'<c r="{reference}"><v>{value}</v></c>'

    namespace = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    return zip_file({
        "xl/sharedStrings.xml": f"<sst {namespace}>" + "".join(f"<si><t>{text}</t></si>" for text in strings) + "</sst>",
        "xl/worksheets/sheet1.xml": f"<worksheet {namespace}><sheetData><row>"
        + "".join(cell(reference, value) for reference, value in cells.items())
        + "</row></sheetData></worksheet>",
    })


def word_file(paragraphs, header="", image=b"", revision=1, parts=None):
    """A Word file with a header and an embedded image next to the body text, plus any other parts."""

    extra = parts or {}
    namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    parts = {
        "docProps/core.xml": core_xml(revision=revision),
        "word/document.xml": f"<w:document {namespace}><w:body>{body}</w:body></w:document>",
        "word/header1.xml": f"<w:hdr {namespace}><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>",
    }
    if image:
        parts["word/media/image1.png"] = image
    return zip_file({**parts, **extra})


class SafeRuleTests(OptionTestBase):
    """The conflict rule "safe": decide only where no content can be lost, leave the rest open."""

    edit_both = FolderRuleTests.edit_both
    backups = FolderRuleTests.backups
    OLD, NEW = 1700000000, 1700090000
    PNG = b"\x89PNG\r\n\x1a\n picture data \x00\x00\x00\x00IEND\xaeB`\x82"

    def setUp(self):
        super().setUp()
        sync.CONFIG.conflict_rule = "safe"

    def resolve(self, name, local, cloud, local_time=OLD, cloud_time=NEW):
        """Runs a sync with this conflict; returns the kept side, or None if it stays open."""

        previous = self.edit_both(name, local, cloud, local_time, cloud_time)
        result = self.run_pair(previous)
        if any(conflict["file"] == name for conflict in result["conflicts"]):
            self.assertEqual((self.local / name).read_bytes(), local)
            self.assertEqual((self.cloud / name).read_bytes(), cloud)
            return None
        kept = (self.local / name).read_bytes()
        self.assertEqual(kept, (self.cloud / name).read_bytes())
        side = "local" if kept == local else "onedrive"
        # The overwritten version is kept, as for every rule.
        self.assertEqual(len(self.backups("onedrive" if side == "local" else "local", name)), 1)
        return side

    def test_same_text_with_other_formatting_keeps_the_newer_version(self):
        local = office_file(["Scope", "Timeline"], revision=3)
        cloud = office_file(["Scope", "Timeline"], revision=4)
        self.assertEqual(self.resolve("plan.docx", local, cloud), "onedrive")

    def test_same_content_saved_in_the_same_second_stays_open(self):
        local = office_file(["Scope"], revision=3)
        cloud = office_file(["Scope"], revision=4)
        self.assertIsNone(self.resolve("plan.docx", local, cloud, local_time=self.OLD, cloud_time=self.OLD))

    def test_newer_version_that_only_adds_content_is_kept(self):
        local = b"intro\nplan\nnew risk\nnew owner\n"
        self.assertEqual(self.resolve("notes.md", local, b"intro\nplan\n", self.NEW, self.OLD), "local")

    def test_older_version_with_more_content_stays_open(self):
        # The newer side may have removed the lines on purpose.
        self.assertIsNone(self.resolve("notes.md", b"intro\nplan\nrisk\n", b"intro\nplan\n"))

    def test_own_changes_on_both_sides_stay_open(self):
        self.assertIsNone(self.resolve("notes.md", b"intro\nlocal idea\n", b"intro\ncloud idea\n"))

    def test_reordered_or_repeated_lines_stay_open(self):
        self.assertIsNone(self.resolve("a.md", b"one\ntwo\nthree\n", b"two\none\nthree\nfour\n"))
        self.assertIsNone(self.resolve("b.md", b"one\none\ntwo\n", b"one\ntwo\n"))

    def test_line_endings_bom_and_accent_form_do_not_count(self):
        local = ("﻿Title\r\n\r\n" + unicodedata.normalize("NFD", "Café text") + "\r\n").encode()
        cloud = "Title\n\nCafé text\n".encode()
        self.assertEqual(self.resolve("notes.md", local, cloud), "onedrive")

    def test_trailing_spaces_blank_lines_and_unreadable_characters_count(self):
        # Two trailing spaces are a line break in Markdown, a blank line separates paragraphs.
        self.assertIsNone(self.resolve("a.md", b"Title  \nText\n", b"Title\nText\n"))
        # The newer version removed the blank line: not the same, and not only an addition.
        self.assertIsNone(self.resolve("b.md", b"Title\n\nText\n", b"Title\nText\n"))
        self.assertIsNone(self.resolve("c.md", b"Title\n\xff\n", b"Title\n\xfe\n"))
        self.assertIsNone(self.resolve("d.md", b"Title\n", b"Title\n\xff\n"))
        # Only CRLF and LF end a line; a lone CR inside a quoted CSV field is a value.
        self.assertIsNone(self.resolve("e.csv", b'"A\rB",1\n', b'"A\nB",1\n'))

    def test_changed_indentation_stays_open(self):
        self.assertIsNone(self.resolve("job.yml", b"steps:\n  run: test\n", b"steps:\nrun: test\n"))

    def test_json_with_other_layout_but_the_same_data_keeps_the_newer_version(self):
        local = b'{"name": "x", "tags": [1, 2]}'
        cloud = b'{\n  "tags": [1, 2],\n  "name": "x"\n}\n'
        self.assertEqual(self.resolve("data.json", local, cloud), "onedrive")
        self.assertIsNone(self.resolve("other.json", b'{"tags": [1, 2]}', b'{"tags": [2, 1]}'))

    def test_json_types_duplicate_keys_and_deep_nesting_stay_open(self):
        self.assertIsNone(self.resolve("a.json", b'{"on": true}', b'{"on": 1}'))
        # An added line with a known key changes the value read, it does not only add.
        self.assertIsNone(self.resolve("b.json", b'{\n"on": true\n}\n', b'{\n"on": true,\n"on": false\n}\n'))
        deep = b"[" * 1500 + b"]" * 1500
        self.assertIsNone(self.resolve("c.json", deep, b"[ " * 1500 + b"]" * 1500))
        self.assertIsNone(self.resolve("d.json", b'{"x": 1.00000000000000001}', b'{"x": 1.00000000000000002}'))
        nfc, nfd = (unicodedata.normalize(form, "\u00e9") for form in ("NFC", "NFD"))
        two_keys = json.dumps({nfc: 1, nfd: 2}, ensure_ascii=False).encode()
        repeated = ('{"%s": 1, "%s": 2}' % (nfc, nfc)).encode()
        self.assertIsNone(self.resolve("e.json", two_keys, repeated))

    def test_empty_or_damaged_file_loses_even_when_it_is_newer(self):
        pdf = b"%PDF-1.7\ncontent\n%%EOF\n"
        cases = {
            "broken.docx": (office_file(["Scope"]), b"not a zip"),
            "empty.xlsx": (excel_file({"A1": 1}), b""),
            "empty.jpg": (b"\xff\xd8 picture \xff\xd9", b""),
            "cut.pdf": (pdf, pdf[:14]),
            "cut.png": (self.PNG, self.PNG[:9]),
            # The end marker of an earlier image, then the copy stopped.
            "middle.png": (self.PNG, self.PNG + b" cut off image dat"),
            # The zip directory is intact, but a part's bytes do not match its checksum.
            "checksum.docx": (office_file(["Scope"]), office_file(["Scope"]).replace(b"Scope", b"Scopf")),
        }
        for name, (intact, damaged) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.resolve(name, intact, damaged), "local")

    def test_two_damaged_files_stay_open(self):
        self.assertIsNone(self.resolve("plan.docx", b"not a zip", b"also broken"))

    def test_files_that_only_look_damaged_stay_open(self):
        # A password-protected Office file is not a zip file, but it is not damaged.
        encrypted = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600
        self.assertIsNone(self.resolve("plan.docx", office_file(["Scope"]), encrypted))
        # Files that may be empty or shorter on purpose: a marker file, a trimmed log.
        self.assertIsNone(self.resolve("COMPLETE", b"done", b""))
        lines = b"".join(b'{"n": %d}\n' % n for n in range(5000))
        self.assertIsNone(self.resolve("events.jsonl", lines, lines[:20_000]))
        # A song cut at a frame boundary is the beginning of the full one, and still playable.
        song = bytes(range(256)) * 400
        self.assertIsNone(self.resolve("song.mp3", song, song[:50_000]))
        # Neither zip file opens, so the longer one is no complete copy either.
        self.assertIsNone(self.resolve("files.zip", b"abcdef", b"abc"))

    def test_zip_files_with_unreadable_names_count_as_damaged_without_stopping_the_sync(self):
        broken = zip_file({"aé.txt": "x"}).replace("é".encode(), b"\xff\xfe")
        self.assertIsNone(self.resolve("files.zip", broken, broken + b"\x00"))
        self.assertEqual(self.resolve("plan.docx", office_file(["Scope"]), broken), "local")

    def test_saves_less_than_a_second_apart_count_as_the_same_time(self):
        local, cloud = office_file(["Scope"], revision=3), office_file(["Scope"], revision=4)
        self.assertIsNone(self.resolve("plan.docx", local, cloud, self.OLD + 0.97, self.OLD + 1.03))

    def test_emptied_text_file_stays_open_unless_the_full_one_is_newer(self):
        self.assertIsNone(self.resolve("notes.md", b"intro\nplan\n", b""))
        self.assertEqual(self.resolve("later.md", b"", b"intro\nplan\n"), "onedrive")

    def test_incomplete_copy_of_a_file_without_text_loses_even_when_it_is_newer(self):
        video = bytes(range(256)) * 600
        self.assertEqual(self.resolve("clip.mp4", video, video[:70_000]), "local")
        self.assertIsNone(self.resolve("other.mp4", video, video[:70_000] + b"different end"))

    def test_excel_numbers_and_formulas_count_as_content(self):
        strings = ("Total",)
        base = excel_file({"A1": ("s", 0), "B1": 10, "C1": "=B1*2"}, strings)
        self.assertIsNone(self.resolve("a.xlsx", base, excel_file({"A1": ("s", 0), "B1": 99, "C1": "=B1*2"}, strings)))
        self.assertIsNone(self.resolve("b.xlsx", base, excel_file({"A1": ("s", 0), "B1": 10, "C1": "=B1*3"}, strings)))
        added = excel_file({"A1": ("s", 0), "B1": 10, "C1": "=B1*2", "D1": 5}, strings)
        self.assertIsNone(self.resolve("c.xlsx", base, added))
        self.assertIsNone(self.resolve("d.xlsx", excel_file({"A1": ("b", 1)}), excel_file({"A1": 1})))

    def test_word_headers_and_images_count_as_content(self):
        base = word_file(["Scope"], header="Draft", image=b"picture one")
        self.assertIsNone(self.resolve("a.docx", base, word_file(["Scope"], header="Final", image=b"picture one")))
        self.assertIsNone(self.resolve("b.docx", base, word_file(["Scope"], header="Draft", image=b"picture two")))
        same = word_file(["Scope"], header="Draft", image=b"picture one", revision=2)
        self.assertEqual(self.resolve("c.docx", base, same), "onedrive")

    def test_office_files_count_as_the_same_only_when_just_the_metadata_differs(self):
        links = "word/_rels/document.xml.rels"
        old = word_file(["Scope"], parts={links: '<Relationships><Relationship Target="https://a.example"/></Relationships>'})
        new = word_file(["Scope"], parts={links: '<Relationships><Relationship Target="https://b.example"/></Relationships>'})
        self.assertIsNone(self.resolve("link.docx", old, new))
        # The same picture moved to another slide: same text, same media, other relations.
        slide = "ppt/slides/_rels/slide{}.xml.rels"
        picture = {"ppt/media/image1.png": "picture"}
        first = zip_file({**picture, slide.format(1): "image1.png", slide.format(2): "none"})
        second = zip_file({**picture, slide.format(1): "none", slide.format(2): "image1.png"})
        self.assertIsNone(self.resolve("deck.pptx", first, second))
        # Custom properties (a retention period, a classification) are content.
        custom = "docProps/custom.xml"
        self.assertIsNone(self.resolve("kept.docx", word_file(["Scope"], parts={custom: "1 year"}),
                                       word_file(["Scope"], parts={custom: "10 years"})))

    def test_deletions_and_name_conflicts_stay_open(self):
        self.write_both("plan.md", b"base")
        previous = self.baseline()
        (self.local / "plan.md").unlink()
        (self.cloud / "plan.md").write_bytes(b"base\nedited\n")
        self.assertEqual(len(self.run_pair(previous)["conflicts"]), 1)
        name_conflict = {"pair": "Test", "file": "A.md", "related_file": "a.md", "reason": "x"}
        self.assertIsNone(sync.rule_decision(self.pair, name_conflict, "safe"))

    def test_folder_rules_still_come_first(self):
        sync.CONFIG.folder_rules = (sync.FolderRule("Drafts/**", "keep_older"),)
        local = office_file(["Scope"], revision=3)
        cloud = office_file(["Scope"], revision=4)
        self.assertEqual(self.resolve("Drafts/plan.docx", local, cloud), "local")

    # Names that differ only by capitalisation or the Unicode form of "ä"

    def test_name_conflicts_stay_open_with_every_rule(self):
        local, cloud = office_file(["Scope"], revision=3), office_file(["Scope"], revision=4)
        for index, rule in enumerate(("safe", "newer", "local", "onedrive")):
            names = (
                (f"r{index}.docx", f"R{index}.docx"),
                tuple(unicodedata.normalize(form, f"Qualität{index}.docx") for form in ("NFC", "NFD")),
            )
            for local_name, cloud_name in names:
                with self.subTest(rule=rule, name=cloud_name):
                    sync.CONFIG.conflict_rule = rule
                    previous = self.baseline()
                    for base, name, content, stamp in (
                        (self.local, local_name, local, self.OLD), (self.cloud, cloud_name, cloud, self.NEW)
                    ):
                        (base / name).write_bytes(content)
                        os.utime(base / name, (stamp, stamp))
                    result = self.run_pair(previous)
                    self.assertTrue(any(c["file"] == local_name for c in result["conflicts"]))
                    self.assertEqual((self.local / local_name).read_bytes(), local)
                    self.assertEqual((self.cloud / cloud_name).read_bytes(), cloud)

    # A clock that is wrong makes "newer" meaningless

    def test_version_dated_in_the_future_is_not_treated_as_newer(self):
        future = int(time.time()) + 3600
        local, cloud = office_file(["Scope"], revision=3), office_file(["Scope"], revision=4)
        self.assertIsNone(self.resolve("plan.docx", local, cloud, cloud_time=future))
        sync.CONFIG.conflict_rule = "newer"
        self.assertIsNone(self.resolve("other.docx", local, cloud, cloud_time=future))

    def test_damaged_file_is_replaced_even_with_a_date_in_the_future(self):
        future = int(time.time()) + 3600
        self.assertEqual(self.resolve("plan.docx", office_file(["Scope"]), b"not a zip", cloud_time=future), "local")

    def test_text_file_that_stops_in_the_middle_of_a_line_stays_open(self):
        self.assertIsNone(self.resolve("notes.md", b"intro\nthe plan is to grow\nend\n", b"intro\nthe plan"))

    def test_safe_is_a_valid_conflict_rule(self):
        self.assertIn("safe", sync.CONFLICT_RULES)
        self.assertEqual(sync.config_choice_errors({"conflict_rule": "safe"}), [])


class NameConflictResolutionTests(unittest.TestCase):
    """Keep Local / Keep OneDrive also settle conflicts whose names differ only
    invisibly (Unicode form of "ä") or by capitalisation: the kept side's
    content and spelling end up on both sides."""

    write_both = SyncSafetyTests.write_both
    baseline = SyncSafetyTests.baseline
    tearDown = SyncSafetyTests.tearDown
    setUp = AppInterfaceTests.setUp

    def make_name_conflict(self, local_name, cloud_name):
        (self.local / local_name).write_bytes(b"local content")
        (self.cloud / cloud_name).write_bytes(b"cloud content")
        sync.save_state({"Test": self.baseline()})
        sync.sync_all(no_delete=True)
        conflicts = sync.load_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertTrue(conflicts[0].get("related_file"))
        return conflicts[0]

    def assert_settled(self, name, content):
        for base in (self.local, self.cloud):
            self.assertEqual(os.listdir(base), [name])
            self.assertEqual((base / name).read_bytes(), content)
        self.assertEqual(sync.conflicts_payload(), [])
        sync.sync_all(no_delete=True)
        self.assertEqual(sync.load_conflicts(), [])

    def test_keep_local_settles_invisible_unicode_name_difference(self):
        nfc = unicodedata.normalize("NFC", "Qualität.docx")
        nfd = unicodedata.normalize("NFD", "Qualität.docx")
        self.make_name_conflict(nfc, nfd)

        result = sync.resolve_conflicts_batch([{"pair": "Test", "file": nfc, "keep": "local"}])

        self.assertEqual(result["done"], [nfc])
        self.assert_settled(nfc, b"local content")
        kept = [p.read_bytes() for p in sync.trash_root().rglob("*.docx")]
        self.assertEqual(kept, [b"cloud content"])

    def test_keep_onedrive_settles_capitalisation_difference(self):
        self.make_name_conflict("report.md", "Report.md")

        result = sync.resolve_conflicts_batch([{"pair": "Test", "file": "report.md", "keep": "onedrive"}])

        self.assertEqual(result["done"], ["report.md"])
        self.assert_settled("Report.md", b"cloud content")
