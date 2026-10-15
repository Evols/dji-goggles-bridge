"""Tests for the one-time Linux setup: udev rule install and link address assignment."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from apps import app_launcher


class SetupLinuxTests(unittest.TestCase):
    def test_udev_rules_cover_v2_access_and_g3_auto_ip(self):
        rules = app_launcher.LINUX_UDEV_RULES
        self.assertIn('ATTR{idProduct}=="001f", MODE="0666"', rules)
        self.assertIn("NM_UNMANAGED", rules)
        self.assertIn("192.168.60.1/24", rules)

    def test_install_linux_udev_rules_writes_file_and_reloads(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "99-dji-goggles.rules"
            with patch.object(
                app_launcher.subprocess, "run", return_value=Mock(returncode=0)
            ) as run:
                self.assertEqual(app_launcher.install_linux_udev_rules(path), 0)
            self.assertEqual(path.read_text(), app_launcher.LINUX_UDEV_RULES)
            run.assert_called_once_with(
                ["udevadm", "control", "--reload-rules"], check=False, capture_output=True
            )

    def test_install_linux_udev_rules_skips_reload_when_up_to_date(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "99-dji-goggles.rules"
            path.write_text(app_launcher.LINUX_UDEV_RULES)
            with patch.object(app_launcher.subprocess, "run") as run:
                self.assertEqual(app_launcher.install_linux_udev_rules(path), 0)
            run.assert_not_called()

    def test_setup_linux_goggles_link_installs_rules_and_assigns_address(self):
        with (
            patch.object(app_launcher, "install_linux_udev_rules", return_value=0) as udev,
            patch("apps.diagnostics.goggles_usb_interface", return_value="usb0"),
            patch.object(app_launcher, "_linux_local_ip_configured", return_value=False),
            patch.object(app_launcher.subprocess, "run", return_value=Mock(returncode=0)) as run,
            patch("builtins.input", return_value=""),
        ):
            self.assertEqual(app_launcher.setup_linux_goggles_link(), 0)
        udev.assert_called_once()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["ip", "link", "set", "usb0", "up"], commands)
        self.assertIn(["ip", "addr", "add", "192.168.60.1/24", "dev", "usb0"], commands)

    def test_setup_linux_goggles_link_fails_when_udev_install_fails(self):
        with (
            patch.object(app_launcher, "install_linux_udev_rules", return_value=1),
            patch.object(app_launcher.subprocess, "run") as run,
        ):
            self.assertEqual(app_launcher.setup_linux_goggles_link(), 1)
        run.assert_not_called()

    def test_setup_linux_goggles_link_succeeds_without_headset(self):
        with (
            patch.object(app_launcher, "install_linux_udev_rules", return_value=0),
            patch("apps.diagnostics.goggles_usb_interface", return_value=None),
            patch.object(app_launcher.subprocess, "run") as run,
        ):
            self.assertEqual(app_launcher.setup_linux_goggles_link(), 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
