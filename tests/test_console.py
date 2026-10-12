"""Console updates preserve guidance, redirected output, and useful support logs."""

import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps import console


def screen(text):
    """Render just the cursor-up/erase commands emitted by our console panel."""
    rows = [""]
    row = column = 0
    for token in re.findall(r"\x1b\[\d+[AK]|[^\x1b]", text):
        if token.startswith("\x1b["):
            if token.endswith("A"):
                row = max(0, row - int(token[2:-1]))
            else:
                rows[row] = ""
        elif token == "\r":
            column = 0
        elif token == "\n":
            row += 1
            column = 0
            while len(rows) <= row:
                rows.append("")
        else:
            rows[row] = rows[row].ljust(column)[:column] + token + rows[row][column + 1 :]
            column += 1
    return "\n".join(rows).strip()


class ConsoleTests(unittest.TestCase):
    def panel(self, dynamic):
        output = io.StringIO()
        with patch.object(console, "terminal_updates", return_value=dynamic):
            panel = console.StatusPanel(output)
        return panel, output

    def test_live_status_updates_replace_previous_rows(self):
        panel, output = self.panel(True)
        panel.status("USB", "plugged in", ready=True)
        panel.status("Goggles", "connected", ready=True)
        panel.status("Live view", "playing - 29 fps", ready=True)
        panel.status("Live view", "playing - 30 fps", ready=True)
        final = screen(output.getvalue())
        self.assertEqual(len(final.splitlines()), 4)
        self.assertIn("Live view: playing - 30 fps", final)
        self.assertNotIn("29 fps", final)
        self.assertIn("Ready to watch!", final)

    def test_help_and_player_instructions_survive_future_updates(self):
        panel, output = self.panel(True)
        panel.status("USB", "plugged in", ready=True)
        token = console.active_panel.set(panel)
        try:
            console.note("Turn on sharing in the goggles.\nThen try again.")
        finally:
            console.active_panel.reset(token)
        panel.status("Goggles", "connected", ready=True)
        final = screen(output.getvalue())
        self.assertTrue(final.startswith("Turn on sharing in the goggles.\nThen try again."))
        self.assertEqual(final.count("USB: plugged in"), 1)
        self.assertIn("Goggles: connected", final)

    def test_redirected_output_has_no_terminal_control_codes(self):
        panel, output = self.panel(False)
        panel.status("USB", "plugged in", ready=True)
        panel.status("USB", "plugged in", ready=True)
        panel.status("Goggles", "connected", ready=True)
        panel.status("Live view", "playing", ready=True)
        panel.close()
        self.assertNotIn("\x1b", output.getvalue())
        self.assertEqual(output.getvalue().count("USB: plugged in"), 1)

    def test_stall_removes_ready_state_and_recovery_restores_it(self):
        panel, output = self.panel(True)
        for name in panel.values:
            panel.status(name, "ready", ready=True)
        panel.status("Live view", "paused")
        self.assertNotIn("Ready to watch!", screen(output.getvalue()))
        self.assertIn("2/3", screen(output.getvalue()))
        panel.status("Live view", "playing", ready=True)
        self.assertIn("Ready to watch!", screen(output.getvalue()))

    def test_error_details_are_saved_without_printing_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(console.tempfile, "gettempdir", return_value=directory):
                path = console.save_error(OSError("synthetic connection detail"))
            self.assertEqual(path, Path(directory) / "dji-goggles-bridge.log")
            self.assertIn("synthetic connection detail", path.read_text(encoding="utf-8"))

    def test_unwritable_support_log_does_not_break_the_app(self):
        with patch.object(console, "RotatingFileHandler", side_effect=PermissionError):
            self.assertIsNone(console.save_error(OSError("synthetic error")))
