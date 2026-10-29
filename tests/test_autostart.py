import ctypes
import importlib.util
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

    def test_frozen_launch_targets_the_executable_not_the_extraction_directory(self):
        startup = MagicMock(dwFlags=0)
        executable = str(Path(tempfile.gettempdir()) / "Program Folder" / "MayhemCompanion.exe")
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart.sys, "frozen", True, create=True), \
                patch.object(autostart.sys, "executable", executable), \
                patch.object(autostart.subprocess, "STARTUPINFO", return_value=startup, create=True), \
                patch.object(autostart.subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
                patch.object(autostart.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(autostart.subprocess, "Popen") as popen:
            autostart.launch_companion()
            self.assertEqual(autostart.watcher_command(), [executable, "--watch-league"])
        self.assertEqual(popen.call_args.args[0], [executable, "--auto-start"])
        self.assertEqual(Path(popen.call_args.kwargs["cwd"]), Path(executable).resolve().parent)
        self.assertFalse(popen.call_args.kwargs["shell"])

    def test_source_watcher_retains_python_module_entrypoint(self):
        with patch.object(autostart.sys, "frozen", False, create=True):
            self.assertEqual(autostart.watcher_command(), [sys.executable, "-m", "mayhem.autostart"])

    def test_source_configuration_preserves_vbs_enable_and_disable_contract(self):
        storage = Path(tempfile.gettempdir()) / "source installation"
        for enable in (True, False):
            with self.subTest(enable=enable), patch.object(autostart.sys, "platform", "win32"), \
                    patch.object(autostart.sys, "frozen", False, create=True), \
                    patch.object(autostart, "is_enabled", return_value=enable), \
                    patch.object(autostart, "_wait_for_watcher_exit", return_value=True) as wait, \
                    patch.object(autostart.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
                self.assertTrue(autostart.configure_startup(enable, storage))
            command = run.call_args.args[0]
            self.assertEqual(command[1:5], ["//B", "//NoLogo", str(storage / "Configure-Autostart.vbs"), str(storage)])
            self.assertEqual(command[5:], [] if enable else ["disable"])
            self.assertNotIn("-ExecutionPolicy", command)
            self.assertEqual(wait.call_count, 0 if enable else 1)

    def test_frozen_configuration_passes_paths_as_data_and_starts_only_an_enabled_watcher(self):
        storage = Path(tempfile.gettempdir()) / "storage ' $quoted"
        executable = str(storage / "MayhemCompanion.exe")
        shortcut = storage / autostart.STARTUP_SHORTCUT_NAME
        for enable in (True, False):
            with self.subTest(enable=enable), patch.object(autostart.sys, "platform", "win32"), \
                    patch.object(autostart.sys, "frozen", True, create=True), \
                    patch.object(autostart.sys, "executable", executable), \
                    patch.object(autostart, "startup_shortcut_path", return_value=shortcut), \
                    patch.object(autostart, "is_enabled", return_value=enable), \
                    patch.object(autostart.subprocess, "run", return_value=MagicMock(returncode=0)) as run, \
                    patch.object(autostart, "_wait_for_watcher_exit", return_value=True) as wait, \
                    patch.object(autostart, "_launch_hidden") as launch:
                self.assertTrue(autostart.configure_startup(enable, storage))
            environment = run.call_args.kwargs["env"]
            self.assertEqual(environment["MAYHEM_STARTUP_EXE"], str(Path(executable).resolve()))
            self.assertEqual(environment["MAYHEM_STARTUP_LINK"], str(shortcut))
            self.assertEqual(environment["MAYHEM_STARTUP_MODE"], "enable" if enable else "disable")
            self.assertNotIn(str(storage), run.call_args.args[0][-1])
            self.assertNotIn("-ExecutionPolicy", run.call_args.args[0])
            if enable:
                launch.assert_called_once_with([executable, "--watch-league"])
                wait.assert_not_called()
            else:
                launch.assert_not_called()
                wait.assert_called_once_with()

    def test_disabling_reports_failure_when_the_old_watcher_does_not_exit(self):
        for frozen in (True, False):
            with self.subTest(frozen=frozen), patch.object(autostart.sys, "platform", "win32"), \
                    patch.object(autostart.sys, "frozen", frozen, create=True), \
                    patch.object(autostart, "is_enabled", return_value=False), \
                    patch.object(autostart.subprocess, "run", return_value=MagicMock(returncode=0)), \
                    patch.object(autostart, "_wait_for_watcher_exit", return_value=False) as wait, \
                    patch.object(autostart, "_launch_hidden") as launch:
                self.assertFalse(autostart.configure_startup(False, Path("fixture")))
                wait.assert_called_once_with()
                launch.assert_not_called()

    def test_cleanup_waits_only_when_it_removed_its_own_shortcut(self):
        for result, success, waits in ((0, True, 1), (autostart._SHORTCUT_SKIPPED, True, 0),
                                       (3, False, 0)):
            with self.subTest(result=result), patch.object(autostart.sys, "platform", "win32"), \
                    patch.object(autostart.sys, "frozen", True, create=True), \
                    patch.object(autostart, "_configure_frozen_shortcut", return_value=result), \
                    patch.object(autostart, "_wait_for_watcher_exit", return_value=True) as wait:
                self.assertEqual(autostart.remove_owned_startup(), success)
                self.assertEqual(wait.call_count, waits)
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart.sys, "frozen", True, create=True), \
                patch.object(autostart, "_configure_frozen_shortcut", return_value=0), \
                patch.object(autostart, "_wait_for_watcher_exit", return_value=False):
            self.assertFalse(autostart.remove_owned_startup())

    def test_resume_starts_only_a_verified_owned_watcher(self):
        for result, success, launches in ((0, True, 1), (autostart._SHORTCUT_SKIPPED, True, 0),
                                          (3, False, 0)):
            with self.subTest(result=result), patch.object(autostart.sys, "platform", "win32"), \
                    patch.object(autostart.sys, "frozen", True, create=True), \
                    patch.object(autostart, "_configure_frozen_shortcut", return_value=result) as inspect, \
                    patch.object(autostart, "_launch_hidden") as launch:
                self.assertEqual(autostart.resume_owned_startup(), success)
                inspect.assert_called_once_with("inspect", Path.home() / "MayhemCompanion")
                self.assertEqual(launch.call_count, launches)
                if launches:
                    launch.assert_called_once_with([sys.executable, "--watch-league"])

    def test_resume_reports_inspection_or_launch_failure_without_changing_startup(self):
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart.sys, "frozen", True, create=True), \
                patch.object(autostart, "_configure_frozen_shortcut", side_effect=OSError("fixture")), \
                patch.object(autostart, "_launch_hidden") as launch:
            self.assertFalse(autostart.resume_owned_startup())
            launch.assert_not_called()
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart.sys, "frozen", True, create=True), \
                patch.object(autostart, "_configure_frozen_shortcut", return_value=0), \
                patch.object(autostart, "_launch_hidden", side_effect=OSError("fixture")):
            self.assertFalse(autostart.resume_owned_startup())

    def test_resume_is_not_available_to_source_or_non_windows_launchers(self):
        for platform, frozen in (("win32", False), ("test", True)):
            with self.subTest(platform=platform, frozen=frozen), \
                    patch.object(autostart.sys, "platform", platform), \
                    patch.object(autostart.sys, "frozen", frozen, create=True), \
                    patch.object(autostart, "_configure_frozen_shortcut") as inspect:
                self.assertFalse(autostart.resume_owned_startup())
                inspect.assert_not_called()

    def test_bundle_launcher_routes_resume_and_rejects_combined_actions(self):
        path = Path(__file__).resolve().parent.parent / "packaging" / "launcher.py"
        spec = importlib.util.spec_from_file_location("mayhem_bundle_launcher_test", path)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        for success in (True, False):
            with self.subTest(success=success), \
                    patch.object(autostart, "resume_owned_startup", return_value=success) as resume:
                self.assertEqual(launcher.main(["--resume-owned-startup"]), 0 if success else 1)
                resume.assert_called_once_with()
        with patch.object(autostart, "resume_owned_startup") as resume:
            for extra in ("--show", "--uninstall-cleanup", "--watch-league", "--resume-owned-startup"):
                self.assertEqual(launcher.main(["--resume-owned-startup", extra]), 2)
            resume.assert_not_called()

    def test_wait_observes_watcher_exit_then_releases_its_probe_handle(self):
        native = MagicMock()
        native.acquire_mutex.side_effect = [None, None, 123]
        with patch.object(autostart, "_NativeWindows", return_value=native), \
                patch.object(autostart.time, "monotonic", side_effect=[100, 100, 100.1]), \
                patch.object(autostart.time, "sleep") as sleep:
            self.assertTrue(autostart._wait_for_watcher_exit())
        self.assertEqual(sleep.call_count, 2)
        native.close_handle.assert_called_once_with(123)

    def test_wait_has_a_bounded_timeout_when_watcher_is_still_running(self):
        native = MagicMock()
        native.acquire_mutex.return_value = None
        with patch.object(autostart, "_NativeWindows", return_value=native), \
                patch.object(autostart.time, "monotonic", side_effect=[100, 100, 105]), \
                patch.object(autostart.time, "sleep") as sleep:
            self.assertFalse(autostart._wait_for_watcher_exit())
        sleep.assert_called_once_with(0.1)
        native.close_handle.assert_not_called()

    def test_configuration_failure_does_not_launch_a_watcher(self):
        with patch.object(autostart.sys, "platform", "win32"), \
                patch.object(autostart.sys, "frozen", True, create=True), \
                patch.object(autostart.subprocess, "run", side_effect=OSError("fixture")), \
                patch.object(autostart, "_launch_hidden") as launch:
            self.assertFalse(autostart.configure_startup(True, Path("fixture")))
            self.assertFalse(autostart.remove_owned_startup())
        launch.assert_not_called()

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
    def test_resume_inspects_exact_owner_and_arguments_without_modifying_shortcuts(self):
        # Exercise WScript.Shell against temporary links; fixture files are never executed.
        with tempfile.TemporaryDirectory(prefix="mayhem-resume-") as directory:
            storage = Path(directory)
            shortcut = storage / autostart.STARTUP_SHORTCUT_NAME
            first, second = storage / "first.exe", storage / "second.exe"
            legacy = storage / "MayhemCompanion" / ".venv" / "Scripts" / "pythonw.exe"
            legacy.parent.mkdir(parents=True)
            for executable in (first, second, legacy):
                executable.write_bytes(b"test fixture, never executed")
            powershell = (Path(os.environ.get("WINDIR", "C:/Windows")) / "System32" /
                          "WindowsPowerShell" / "v1.0" / "powershell.exe")
            fixture_script = (
                "$shell = New-Object -ComObject WScript.Shell; "
                "$link = $shell.CreateShortcut($env:MAYHEM_TEST_LINK); "
                "$link.TargetPath = $env:MAYHEM_TEST_TARGET; "
                "$link.Arguments = $env:MAYHEM_TEST_ARGS; $link.Save()"
            )
            with patch.object(autostart.sys, "frozen", True, create=True), \
                    patch.object(autostart.sys, "executable", str(first)), \
                    patch.object(autostart.Path, "home", return_value=storage), \
                    patch.object(autostart, "startup_shortcut_path", return_value=shortcut), \
                    patch.object(autostart, "_launch_hidden") as launch:
                self.assertTrue(autostart.resume_owned_startup())
                self.assertFalse(shortcut.exists())
                launch.assert_not_called()
                for target, arguments, owned in (
                    (first, "--watch-league", True), (second, "--watch-league", False),
                    (first, "--show", False), (legacy, "-m mayhem.autostart", False),
                ):
                    with self.subTest(target=target.name, arguments=arguments):
                        environment = dict(os.environ, MAYHEM_TEST_LINK=str(shortcut),
                                           MAYHEM_TEST_TARGET=str(target), MAYHEM_TEST_ARGS=arguments)
                        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command",
                                        fixture_script], env=environment, check=True, timeout=15,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                        before = shortcut.read_bytes()
                        launch.reset_mock()
                        self.assertTrue(autostart.resume_owned_startup())
                        self.assertEqual(shortcut.read_bytes(), before)
                        self.assertEqual(launch.call_count, int(owned))

    def test_frozen_shortcut_roundtrip_and_uninstall_leave_foreign_owners_untouched(self):
        # Only a temporary .lnk is touched, never the user's real Startup folder.
        with tempfile.TemporaryDirectory(prefix="mayhem-startup-") as directory:
            storage = Path(directory) / "quoted ' $ directory"
            storage.mkdir()
            first = storage / "first.exe"
            second = storage / "second.exe"
            first.write_bytes(b"test fixture, never executed")
            second.write_bytes(b"test fixture, never executed")
            shortcut = storage / autostart.STARTUP_SHORTCUT_NAME
            with patch.object(autostart.sys, "frozen", True, create=True), \
                    patch.object(autostart, "startup_shortcut_path", return_value=shortcut), \
                    patch.object(autostart, "_wait_for_watcher_exit", return_value=True) as wait, \
                    patch.object(autostart, "_launch_hidden") as launch:
                with patch.object(autostart.sys, "executable", str(first)):
                    self.assertTrue(autostart.configure_startup(True, storage))
                    self.assertTrue(shortcut.is_file())
                with patch.object(autostart.sys, "executable", str(second)):
                    self.assertFalse(autostart.configure_startup(False, storage))
                    self.assertTrue(autostart.remove_owned_startup())
                    self.assertTrue(shortcut.is_file())
                    wait.assert_not_called()
                with patch.object(autostart.sys, "executable", str(first)):
                    self.assertTrue(autostart.remove_owned_startup())
                    self.assertFalse(shortcut.exists())
                    wait.assert_called_once_with()
                    self.assertTrue(autostart.remove_owned_startup())
                    wait.assert_called_once_with()
                    self.assertTrue(autostart.configure_startup(False, storage))
                launch.assert_called_once_with([str(first), "--watch-league"])

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
