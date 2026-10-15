"""Console selection, read-only preflight and source/frozen dependency handling."""

import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apps import app_launcher, linux_app, mac_app, server, viewer, webrtc, windows_app


class LauncherTests(unittest.TestCase):
    def test_third_menu_option_runs_webrtc_with_detected_v2(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", return_value=5),
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(webrtc, "main", return_value=0) as receiver,
            patch("builtins.input", side_effect=["3", "q"]),
        ):
            self.assertEqual(windows_app.main([]), 0)
        receiver.assert_called_once_with(["--model", "v2"])

    def test_direct_webrtc_passes_listen_and_model_options_on_both_platforms(self):
        options = ["--host", "0.0.0.0", "--port", "8081", "--model", "v2"]
        for platform, launcher in (("win32", windows_app), ("darwin", mac_app)):
            with (
                patch.object(sys, "platform", platform),
                patch.object(app_launcher, "prepare_dependencies", return_value=None),
                patch.object(webrtc, "main", return_value=0) as receiver,
            ):
                self.assertEqual(launcher.main(["webrtc", *options]), 0)
            receiver.assert_called_once_with(options)

    def test_source_webrtc_checks_its_optional_dependencies(self):
        required = []
        with patch.object(
            app_launcher.importlib.util,
            "find_spec",
            side_effect=lambda name: required.append(name) or True,
        ):
            self.assertIsNone(app_launcher.prepare_dependencies("webrtc", "win32", []))
        self.assertIn("aiohttp", required)
        self.assertIn("aiortc", required)

    def test_windows_detected_v2_skips_network_setup_and_passes_model_to_receiver(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(app_launcher, "check_windows_network", return_value=5),
            patch.object(app_launcher, "setup_windows_network") as setup,
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(server, "main", return_value=0) as receiver,
        ):
            self.assertEqual(windows_app.main(["server", "--port", "5601"]), 0)
        setup.assert_not_called()
        receiver.assert_called_once_with(["--port", "5601", "--model", "v2"])

    def test_explicit_v2_never_runs_windows_rndis_preflight(self):
        for options in (["--model", "v2"], ["--model=v2", "--transport", "usb"]):
            with (
                patch.object(sys, "platform", "win32"),
                patch.object(app_launcher, "check_windows_network") as network,
                patch.object(app_launcher, "prepare_dependencies", return_value=None),
                patch.object(viewer, "main", return_value=0) as receiver,
            ):
                self.assertEqual(windows_app.main(["viewer", *options]), 0)
            network.assert_not_called()
            receiver.assert_called_once_with(options)

    def test_mac_menu_detects_v2_and_preserves_explicit_model(self):
        options = ["--port", "5601"]
        with patch("apps.diagnostics.usb_device_count", side_effect=[1, 1]):
            self.assertEqual(app_launcher.ensure_mac_connection(options), 0)
        self.assertEqual(options, ["--port", "5601", "--model", "v2"])
        options = ["--model", "goggles3"]
        with patch("apps.diagnostics.usb_device_count", return_value=1) as count:
            self.assertEqual(app_launcher.ensure_mac_connection(options), 0)
        count.assert_called_once_with("darwin", model="all")
        self.assertEqual(options, ["--model", "goggles3"])

    def test_mac_model_inventory_failure_still_allows_connection(self):
        options = []
        with patch(
            "apps.diagnostics.usb_device_count", side_effect=[1, OSError("registry unavailable")]
        ):
            self.assertEqual(app_launcher.ensure_mac_connection(options), 0)
        self.assertEqual(options, [])

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

    def test_menu_defaults_to_viewer_and_checks_network_before_quitting(self):
        with patch("builtins.input", return_value=""):
            self.assertEqual(app_launcher.choose_mode(), "viewer")
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="q"),
            patch.object(app_launcher, "check_windows_network", return_value=0) as network,
        ):
            self.assertEqual(windows_app.main([]), 0)
        network.assert_called_once()

    def test_setup_warning_confirmation_and_recheck_precede_the_menu(self):
        calls = []
        output = io.StringIO()

        def check():
            calls.append("check")
            return 2 if calls.count("check") == 1 else 0

        def answer(prompt):
            if "Set up" in prompt:
                self.assertIn("system (UAC) prompt", output.getvalue())
                calls.append("confirm")
                return "yes"
            self.assertEqual(calls, ["check", "confirm", "setup", "check"])
            calls.append("menu")
            return "q"

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(sys, "stdout", output),
            patch.object(app_launcher, "check_windows_network", side_effect=check),
            patch.object(
                app_launcher,
                "setup_windows_network",
                side_effect=lambda: calls.append("setup") or 0,
            ),
            patch("builtins.input", side_effect=answer),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
        ):
            self.assertEqual(windows_app.main([]), 0)
        self.assertEqual(calls, ["check", "confirm", "setup", "check", "menu"])
        dependencies.assert_not_called()

    def test_declined_setup_does_not_elevate(self):
        with (
            patch.object(app_launcher, "check_windows_network", return_value=3),
            patch.object(app_launcher, "setup_windows_network") as setup,
            patch("builtins.input", return_value="n"),
        ):
            self.assertEqual(app_launcher.ensure_windows_network(True), 3)
        setup.assert_not_called()

    def test_noninteractive_setup_does_not_prompt_or_elevate(self):
        with (
            patch.object(app_launcher, "check_windows_network", return_value=2),
            patch.object(app_launcher, "setup_windows_network") as setup,
            patch("builtins.input") as question,
        ):
            self.assertEqual(app_launcher.ensure_windows_network(False), 2)
        setup.assert_not_called()
        question.assert_not_called()

    def test_failed_or_cancelled_uac_setup_stops_before_receiver(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", return_value=3),
            patch.object(app_launcher, "setup_windows_network", return_value=1),
            patch("builtins.input", return_value="y"),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch.object(server, "main") as receiver,
        ):
            self.assertEqual(windows_app.main(["server"]), 1)
        dependencies.assert_not_called()
        receiver.assert_not_called()

    def test_reboot_required_is_reported_and_does_not_continue(self):
        with (
            patch.object(app_launcher, "check_windows_network", return_value=3) as check,
            patch.object(app_launcher, "setup_windows_network", return_value=3010),
            patch("builtins.input", return_value="yes"),
        ):
            self.assertEqual(app_launcher.ensure_windows_network(True), 1)
        check.assert_called_once()

    def test_missing_headset_can_quit_before_the_menu_or_setup_prompt(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", return_value=1),
            patch.object(app_launcher, "setup_windows_network") as setup,
            patch.object(app_launcher, "choose_mode") as menu,
            patch("builtins.input", return_value="q"),
        ):
            self.assertEqual(windows_app.main([]), 0)
        setup.assert_not_called()
        menu.assert_not_called()

    def test_reconnect_checks_again_before_showing_the_menu(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", side_effect=[1, 0]) as network,
            patch("builtins.input", side_effect=["", "q"]),
        ):
            self.assertEqual(windows_app.main([]), 0)
        self.assertEqual(network.call_count, 2)

    def test_closing_video_returns_to_the_menu(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", return_value=0),
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", return_value=0) as receiver,
            patch("builtins.input", side_effect=["1", "q"]),
        ):
            self.assertEqual(windows_app.main([]), 0)
        receiver.assert_called_once_with([])

    def test_failed_video_can_retry_then_return_to_menu(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", return_value=0) as network,
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", side_effect=[1, 0]) as receiver,
            patch("builtins.input", side_effect=["1", "", "q"]),
        ):
            self.assertEqual(windows_app.main([]), 0)
        self.assertEqual(receiver.call_count, 2)
        self.assertEqual(network.call_count, 2)

    def test_failed_video_can_go_back_and_choose_the_other_mode(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(app_launcher, "check_windows_network", return_value=0),
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", return_value=1) as watch,
            patch.object(server, "main", return_value=0) as share,
            patch("builtins.input", side_effect=["1", "b", "2", "q"]),
        ):
            self.assertEqual(windows_app.main([]), 0)
        watch.assert_called_once_with([])
        share.assert_called_once_with([])

    def test_mac_menu_waits_for_a_single_headset_without_opening_usb(self):
        with (
            patch.object(sys, "platform", "darwin"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", side_effect=[0, 2, 1, 0]) as usb,
            patch("builtins.input", side_effect=["", "", "q"]),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
        ):
            self.assertEqual(mac_app.main([]), 0)
        self.assertEqual(usb.call_count, 4)
        dependencies.assert_not_called()

    def test_linux_launcher_rejects_other_platforms(self):
        with patch.object(sys, "platform", "win32"):
            self.assertEqual(linux_app.main(["viewer"]), 1)

    def test_linux_direct_v2_skips_the_link_preflight(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(app_launcher, "check_linux_goggles_link") as link_check,
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", return_value=0) as receiver,
        ):
            self.assertEqual(linux_app.main(["viewer", "--model", "v2"]), 0)
        link_check.assert_not_called()
        receiver.assert_called_once_with(["--model", "v2"])

    def test_linux_direct_goggles3_blocks_when_the_link_is_not_ready(self):
        output = io.StringIO()
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=False),
            patch.object(app_launcher, "check_linux_goggles_link", return_value=3),
            patch.object(sys, "stdout", output),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch.object(viewer, "main") as receiver,
        ):
            self.assertEqual(linux_app.main(["viewer"]), 3)
        dependencies.assert_not_called()
        receiver.assert_not_called()
        self.assertIn("isn't ready", output.getvalue())

    def test_linux_menu_detects_v2_and_passes_it_to_the_receiver(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", side_effect=[1, 1]),
            patch.object(app_launcher, "_is_root", return_value=False),
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", return_value=0) as receiver,
            patch("builtins.input", side_effect=["1", "q"]),
        ):
            self.assertEqual(linux_app.main(["viewer", "--port", "5601"]), 0)
        receiver.assert_called_once_with(["--port", "5601", "--model", "v2"])

    def test_linux_menu_v2_as_root_installs_udev_rules(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", side_effect=[1, 1]),
            patch.object(app_launcher, "_is_root", return_value=True),
            patch.object(app_launcher, "install_linux_udev_rules", return_value=0) as udev,
            patch.object(app_launcher, "prepare_dependencies", return_value=None),
            patch.object(viewer, "main", return_value=0),
            patch("builtins.input", side_effect=["1", "q"]),
        ):
            self.assertEqual(linux_app.main([]), 0)
        udev.assert_called_once()

    def test_linux_menu_goggles3_ready_skips_setup_and_reaches_the_menu(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", side_effect=[1, 0]),
            patch.object(app_launcher, "_linux_local_ip_configured", return_value=True),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch("builtins.input", side_effect=["q"]),
        ):
            self.assertEqual(linux_app.main([]), 0)
        dependencies.assert_not_called()

    def test_linux_menu_root_offers_one_time_setup_and_rechecks(self):
        output = io.StringIO()
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", side_effect=[1, 0, 1, 0]),
            patch.object(app_launcher, "_linux_local_ip_configured", side_effect=[False, True]),
            patch.object(app_launcher, "_is_root", return_value=True),
            patch.object(app_launcher, "setup_linux_goggles_link", return_value=0) as setup,
            patch.object(sys, "stdout", output),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch("builtins.input", side_effect=["y", "q"]),
        ):
            self.assertEqual(linux_app.main([]), 0)
        dependencies.assert_not_called()
        setup.assert_called_once()
        self.assertIn("one-time setup", output.getvalue())
        self.assertIn("Checking your goggles again...", output.getvalue())

    def test_linux_menu_nonroot_shows_instructions_and_retries_until_ready(self):
        output = io.StringIO()
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", side_effect=[1, 0, 1, 0]),
            patch.object(app_launcher, "_linux_local_ip_configured", side_effect=[False, True]),
            patch.object(app_launcher, "_is_root", return_value=False),
            patch.object(app_launcher, "setup_linux_goggles_link") as setup,
            patch.object(sys, "stdout", output),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch("builtins.input", side_effect=["", "q"]),
        ):
            self.assertEqual(linux_app.main([]), 0)
        dependencies.assert_not_called()
        setup.assert_not_called()
        self.assertIn("sudo ip addr add 192.168.60.1/24", output.getvalue())

    def test_linux_menu_without_headset_can_quit(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("apps.diagnostics.usb_device_count", return_value=0),
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
            patch("builtins.input", side_effect=["q"]),
        ):
            self.assertEqual(linux_app.main([]), 0)
        dependencies.assert_not_called()

    def test_linux_custom_local_ip_skips_the_link_check(self):
        with patch("apps.diagnostics.usb_device_count") as count:
            self.assertEqual(app_launcher.check_linux_goggles_link(["--local-ip", "127.0.0.1"]), 0)
        count.assert_not_called()

    def test_source_restart_relaunches_the_linux_launcher_on_linux(self):
        with (
            patch.object(app_launcher.importlib.util, "find_spec", return_value=None),
            patch.object(Path, "is_file", return_value=True),
            patch.object(sys, "executable", str(Path(".local/other-python").resolve())),
            patch.object(sys, "prefix", str(Path(".local/other-prefix").resolve())),
            patch.object(app_launcher.subprocess, "run", return_value=Mock(returncode=7)) as run,
        ):
            self.assertEqual(
                app_launcher.prepare_dependencies("server", "linux", ["--port", "5601"]), 7
            )
        command = run.call_args.args[0]
        self.assertTrue(command[1].endswith("linux_app.py"))
        self.assertEqual(command[2:], ["server", "--port", "5601"])

    def test_setup_help_is_available_without_starting_a_receiver(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["h", "q"]), patch.object(sys, "stdout", output):
            self.assertIsNone(app_launcher.choose_mode())
        self.assertIn("Set up your goggles", output.getvalue())
        self.assertIn("Share Liveview", output.getvalue())

    def test_all_mode_help_commands_need_no_hardware_or_dependencies(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(app_launcher, "check_windows_network") as network,
            patch.object(app_launcher, "prepare_dependencies") as dependencies,
        ):
            for mode in ("server", "viewer", "webrtc"):
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
        for entry in (
            "windows_app.py",
            "mac_app.py",
            "linux_app.py",
            "viewer.py",
            "server.py",
            "webrtc.py",
        ):
            result = subprocess.run(
                [sys.executable, "-S", str(source / entry), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--help", result.stdout)
