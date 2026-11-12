import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile
import factorio_backup as app


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.save = self.root / 'world.zip'
        with zipfile.ZipFile(self.save, 'w') as archive:
            archive.writestr('world/level.dat', b'world data')

    def test_snapshot_does_not_modify_source(self):
        before = self.save.read_bytes()
        digest = app.snapshot(self.save, self.root / 'copy.tmp', 0)
        self.assertEqual(len(digest), 64)
        self.assertEqual(self.save.read_bytes(), before)

    def test_incomplete_zip_never_uploads(self):
        self.save.write_bytes(b'partial zip')
        with patch.object(app.subprocess, 'run') as run:
            self.assertEqual(app.scan(self.root, 'gdrive:Backups', {}, 0, 20), 1)
            run.assert_not_called()

    def test_changing_source_is_rejected(self):
        with patch.object(app, 'signature', side_effect=[(10, 1, 1), (11, 2, 2)]):
            with self.assertRaisesRegex(ValueError, 'записывает'):
                app.snapshot(self.save, self.root / 'copy.tmp', 0)

    def test_success_skips_unchanged_but_reuploads_changes(self):
        uploaded = {}
        with patch.object(app.subprocess, 'run') as run:
            self.assertEqual(app.scan(self.root, 'gdrive:Backups', uploaded, 0, 20), 0)
            self.assertEqual(app.scan(self.root, 'gdrive:Backups', uploaded, 0, 20), 0)
            self.assertEqual(run.call_count, 1)
            with zipfile.ZipFile(self.save, 'w') as archive:
                archive.writestr('world/level.dat', b'new world data')
            self.assertEqual(app.scan(self.root, 'gdrive:Backups', uploaded, 0, 20), 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][3], 'gdrive:Backups/world.zip')

    def test_failure_is_retried(self):
        uploaded = {}
        with patch.object(app.subprocess, 'run', side_effect=[subprocess.CalledProcessError(1, 'rclone'), None]) as run:
            self.assertEqual(app.scan(self.root, 'gdrive:Backups', uploaded, 0, 20), 1)
            self.assertEqual(uploaded, {})
            self.assertEqual(app.scan(self.root, 'gdrive:Backups', uploaded, 0, 20), 0)
            self.assertEqual(run.call_count, 2)
            self.assertIn('world.zip', uploaded)


class WatchTests(unittest.TestCase):
    def args(self, once=False):
        return SimpleNamespace(process_name='factorio.exe', interval=2, saves=Path('.'),
                               remote='gdrive:Backups', settle=0, timeout=20, once=once)

    def test_idle_and_running_never_upload_until_exit(self):
        with patch.object(app, 'game_running', side_effect=[False, True, True, False]), \
             patch.object(app.time, 'sleep'), \
             patch.object(app.subprocess, 'run'), \
             patch.object(app, 'scan', return_value=0) as scan:
            self.assertEqual(app.watch(self.args(once=True)), 0)
            scan.assert_called_once()

    def test_process_check_failure_is_not_exit(self):
        with patch.object(app, 'game_running', side_effect=[True, OSError('tasklist failed'), True, False]), \
             patch.object(app.time, 'sleep'), \
             patch.object(app.subprocess, 'run'), \
             patch.object(app, 'scan', return_value=0) as scan:
            self.assertEqual(app.watch(self.args(once=True)), 0)
            scan.assert_called_once()

    def test_retry_waits_while_game_reopened(self):
        with patch.object(app, 'game_running', side_effect=[True, False, True, False, False, KeyboardInterrupt]), \
             patch.object(app.time, 'sleep'), \
             patch.object(app.subprocess, 'run'), \
             patch.object(app, 'scan', side_effect=[1, 0]) as scan:
            with self.assertRaises(KeyboardInterrupt):
                app.watch(self.args())
            self.assertEqual(scan.call_count, 2)

    def test_windows_matches_executable_not_window_title(self):
        with patch.object(app.sys, 'platform', 'win32'), \
             patch.object(app.subprocess, 'run', return_value=SimpleNamespace(
                 stdout=b'"factorio.exe","123","Console","1","100 K"\r\n')):
            self.assertTrue(app.game_running('factorio.exe'))
            self.assertFalse(app.game_running('other.exe'))


if __name__ == '__main__':
    unittest.main()
