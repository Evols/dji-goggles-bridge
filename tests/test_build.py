"""macOS single-file packaging and archive contents without a Mac toolchain."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build


class BuildTests(unittest.TestCase):
    def test_macos_embeds_libusb_and_archives_only_the_executable_and_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / "libusb-1.0.0.dylib"
            library.write_bytes(b"fake libusb")
            (root / "README.md").write_text("Readme", encoding="utf-8")
            (root / "LICENSE").write_text("License", encoding="utf-8")
            name = "dji-goggles-bridge-macos"
            executable = root / "dist/macos" / name
            commands = []

            def run(command):
                commands.append(command)
                if "PyInstaller" in command:
                    executable.parent.mkdir(parents=True)
                    executable.write_bytes(b"fake executable")
                    (executable.parent / "unrelated-file").write_bytes(b"keep out of archive")

            with (
                patch.object(build, "ROOT", root),
                patch.object(build.sys, "platform", "darwin"),
                patch.object(build.platform, "machine", return_value="arm64"),
                patch.object(build.ctypes.util, "find_library", return_value=str(library)),
                patch.object(build, "run", side_effect=run),
            ):
                self.assertEqual(build.main(["--skip-install"]), 0)

            package_command = next(command for command in commands if "PyInstaller" in command)
            self.assertIn("--onefile", package_command)
            binary = package_command[package_command.index("--add-binary") + 1]
            source, destination = binary.rsplit(os.pathsep, 1)
            self.assertTrue(Path(source).samefile(library))
            self.assertEqual(destination, ".")
            self.assertEqual(
                [command for command in commands if command[0] == str(executable)],
                [
                    [str(executable), "--help"],
                    [str(executable), "viewer", "--help"],
                    [str(executable), "server", "--help"],
                ],
            )
            with build.tarfile.open(root / "dist" / f"{name}-arm64.tar.gz") as archive:
                self.assertEqual(
                    set(archive.getnames()),
                    {f"{name}/{name}", f"{name}/README.md", f"{name}/LICENSE"},
                )
                self.assertEqual(archive.extractfile(f"{name}/{name}").read(), b"fake executable")
