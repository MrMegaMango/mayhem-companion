import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import uuid

from mayhem import autostart


class WatcherTests(unittest.TestCase):
    def run_states(self, states, launch=None):
        source = MagicMock(side_effect=states)
        launch = launch or MagicMock()
        sleep = MagicMock()
        enabled = MagicMock(side_effect=[True] * len(states) + [False])
        autostart.run_watcher(process_source=source, launch=launch, sleep=sleep, enabled=enabled)
        return launch, source, sleep

    def test_launches_once_when_league_opens_and_again_only_after_it_closes(self):
        launch, source, sleep = self.run_states([False, True, True, True, False, True, True])
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(source.call_count, 7)
        self.assertEqual(sleep.call_count, 7)
        self.assertTrue(all(call.args == (2.0,) for call in sleep.call_args_list))

    def test_initially_running_league_launches_once(self):
        launch, _, _ = self.run_states([True, True, True])
        self.assertEqual(launch.call_count, 1)

    def test_tray_exit_does_not_relaunch_while_league_stays_open(self):
        # No companion-process check is made. Its exit cannot rearm the watcher.
        launch, _, _ = self.run_states([True] * 10)
        self.assertEqual(launch.call_count, 1)

    def test_snapshot_failure_does_not_rearm_a_handled_session(self):
        launch, _, _ = self.run_states([True, OSError("transient failure"), True])
        self.assertEqual(launch.call_count, 1)

    def test_failed_launch_is_retried_then_session_is_marked_handled(self):
        launch = MagicMock(side_effect=[OSError("transient failure"), None])
        self.run_states([True, True, True], launch)
        self.assertEqual(launch.call_count, 2)

    def test_disabling_exits_without_process_query_or_launch(self):
        source, launch, sleep = MagicMock(), MagicMock(), MagicMock()
        autostart.run_watcher(process_source=source, launch=launch, sleep=sleep, enabled=lambda: False)
        source.assert_not_called()
        launch.assert_not_called()
        sleep.assert_not_called()

    def test_only_exact_league_client_and_game_names_are_supported(self):
        self.assertEqual(autostart._LEAGUE_PROCESS_NAMES,
                         {"leagueclient.exe", "leagueclientux.exe", "league of legends.exe"})


class StartupAndLaunchTests(unittest.TestCase):
    def test_startup_environment_fallback_preserves_exact_shortcut_name(self):
        with patch.object(autostart.sys, "platform", "test"), patch.dict(os.environ, {"APPDATA": "roaming-root"}):
            path = autostart.startup_shortcut_path()
        self.assertEqual(path, Path("roaming-root") / "Microsoft" / "Windows" / "Start Menu" /
                         "Programs" / "Startup" / "Mayhem Companion - Start with League.lnk")

    def test_is_enabled_tracks_shortcut_removal_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            shortcut = Path(directory) / autostart.STARTUP_SHORTCUT_NAME
            with patch.object(autostart, "startup_shortcut_path", return_value=shortcut):
                self.assertFalse(autostart.is_enabled())
                shortcut.write_bytes(b"fixture")
                self.assertTrue(autostart.is_enabled())
                shortcut.unlink()
                self.assertFalse(autostart.is_enabled())

    def test_launch_is_hidden_uses_current_python_and_auto_start_flag(self):
        startup = MagicMock(dwFlags=0)
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart.subprocess, "STARTUPINFO", return_value=startup, create=True), \
                patch.object(autostart.subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
                patch.object(autostart.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(autostart.subprocess, "Popen") as popen:
            autostart.launch_companion()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "mayhem", "--auto-start"])
        options = popen.call_args.kwargs
        self.assertEqual(Path(options["cwd"]), Path(autostart.__file__).resolve().parent.parent)
        self.assertEqual(options["creationflags"], 0x08000000)
        self.assertEqual(startup.wShowWindow, 0)
        self.assertFalse(options["shell"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(options[stream], subprocess.DEVNULL)

    def test_entrypoint_duplicate_watcher_does_not_start_another_loop(self):
        native = MagicMock()
        native.acquire_mutex.return_value = None
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart, "is_enabled", return_value=True), \
                patch.object(autostart, "_NativeWindows", return_value=native), \
                patch.object(autostart, "run_watcher") as run:
            self.assertEqual(autostart.main(), 0)
        run.assert_not_called()

    def test_entrypoint_closes_mutex_when_watcher_stops(self):
        native = MagicMock()
        native.acquire_mutex.return_value = 123
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart, "is_enabled", return_value=True), \
                patch.object(autostart, "_NativeWindows", return_value=native), \
                patch.object(autostart, "run_watcher") as run:
            self.assertEqual(autostart.main(), 0)
        run.assert_called_once_with(process_source=native.league_running)
        native.close_handle.assert_called_once_with(123)

    def test_entrypoint_is_noop_off_windows_or_when_disabled(self):
        with patch.object(autostart.sys, "platform", "test"), patch.object(autostart, "_NativeWindows") as native:
            self.assertEqual(autostart.main(), 0)
            native.assert_not_called()
        with patch.object(autostart.sys, "platform", "win32"), patch.object(autostart, "is_enabled", return_value=False), \
                patch.object(autostart, "_NativeWindows") as native:
            self.assertEqual(autostart.main(), 0)
            native.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows native APIs")
class NativeWindowsTests(unittest.TestCase):
    def test_process_snapshot_is_readable_and_returns_only_a_boolean(self):
        native = autostart._NativeWindows()
        self.assertIsInstance(native.league_running(), bool)

    def test_independent_mutex_blocks_duplicate_until_first_handle_closes(self):
        native = autostart._NativeWindows()
        name = "Local\\MayhemCompanion.AutostartTest." + uuid.uuid4().hex
        first = native.acquire_mutex(name)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(native.acquire_mutex(name))
        finally:
            native.close_handle(first)
        next_handle = native.acquire_mutex(name)
        self.assertIsNotNone(next_handle)
        native.close_handle(next_handle)

    def test_user_startup_folder_resolves_without_mutation(self):
        path = autostart.startup_shortcut_path()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.name, autostart.STARTUP_SHORTCUT_NAME)


if __name__ == "__main__":
    unittest.main()
