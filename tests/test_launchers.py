"""Console selection, read-only preflight and source/frozen dependency handling."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apps import app_launcher, mac_app, server, viewer, windows_app


class LauncherTests(unittest.TestCase):
    def test_windows_network_failure_stops_before_dependency_setup_and_receiver(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(app_launcher, "check_windows_network", return_value=1),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch.object(server, "main") as receiver,
        ):
            self.assertEqual(windows_app.main(["server"]), 1)
        dependencies.assert_not_called()
        receiver.assert_not_called()

    def test_windows_server_passes_all_destination_and_connection_options(self):
        options = ["--host", "127.0.0.1", "--port", "5601", "--local-port", "0"]
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(app_launcher, "check_windows_network", return_value=0) as network,
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(server, "main", return_value=0) as receiver,
        ):
            self.assertEqual(windows_app.main(["server", *options]), 0)
        network.assert_called_once()
        receiver.assert_called_once_with(options)

    def test_mac_viewer_skips_windows_check_and_preserves_transport_options(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(app_launcher, "check_windows_network") as network,
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", return_value=0) as receiver,
        ):
            self.assertEqual(mac_app.main(["viewer", "--transport", "usb"]), 0)
        network.assert_not_called()
        receiver.assert_called_once_with(["--transport", "usb"])

    def test_menu_defaults_to_viewer_and_can_quit_without_preflight(self):
        with patch("builtins.input", return_value=""):
            self.assertEqual(app_launcher.choose_mode(), "viewer")
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="q"),
            patch.object(app_launcher, "check_windows_network") as network,
        ):
            self.assertEqual(windows_app.main([]), 0)
        network.assert_not_called()

    def test_both_mode_help_commands_need_no_hardware_or_dependencies(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(app_launcher, "check_windows_network") as network,
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
        ):
            for mode in ("server", "viewer"):
                with self.assertRaises(SystemExit) as result:
                    windows_app.main([mode, "--help"])
                self.assertEqual(result.exception.code, 0)
        network.assert_not_called()
        dependencies.assert_not_called()

    def test_frozen_launcher_never_bootstraps_python(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(app_launcher.subprocess, "run") as run,
            patch.object(app_launcher.importlib.util, "find_spec") as lookup,
        ):
            self.assertIsNone(app_launcher.prepare_dependencies("server", "win32", []))
        run.assert_not_called()
        lookup.assert_not_called()

    def test_source_restart_keeps_selected_menu_mode_and_arguments(self):
        with (
            patch.object(app_launcher.importlib.util, "find_spec", return_value=None),
            patch.object(Path, "is_file", return_value=True),
            patch.object(sys, "executable", str(Path(".local/other-python").resolve())),
            patch.object(sys, "prefix", str(Path(".local/other-prefix").resolve())),
            patch.object(app_launcher.subprocess, "run", return_value=Mock(returncode=7)) as run,
        ):
            self.assertEqual(
                app_launcher.prepare_dependencies("server", "win32", ["--port", "5601"]), 7
            )
        self.assertEqual(run.call_args.args[0][-3:], ["server", "--port", "5601"])

    def test_missing_dependencies_install_inside_current_project_venv(self):
        root = Path(app_launcher.__file__).resolve().parents[2]
        with (
            patch.object(app_launcher.importlib.util, "find_spec", return_value=None),
            patch.object(Path, "is_file", return_value=True),
            patch.object(sys, "executable", str(root / ".venv/Scripts/python.exe")),
            patch.object(sys, "prefix", str(root / ".venv")),
            patch.object(app_launcher.subprocess, "run") as run,
        ):
            self.assertIsNone(app_launcher.prepare_dependencies("server", "win32", []))
        self.assertEqual(run.call_args.args[0][-4:], ["-m", "pip", "install", ".[apps]"])
        self.assertEqual(run.call_args.kwargs["cwd"], root)

    def test_network_checker_is_available_as_an_installed_resource(self):
        with patch.object(app_launcher.subprocess, "run", return_value=Mock(returncode=0)) as run:
            self.assertEqual(app_launcher.check_windows_network(), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["powershell.exe", "-NoProfile"])
        self.assertTrue(Path(command[-1]).is_file())
        self.assertIn("Confirm-GogglesNetwork", Path(command[-1]).read_text())

    def test_direct_source_help_works_without_site_packages(self):
        source = Path(__file__).resolve().parents[1] / "src" / "apps"
        for entry in ("windows_app.py", "mac_app.py", "viewer.py", "server.py"):
            result = subprocess.run(
                [sys.executable, "-S", str(source / entry), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--help", result.stdout)
