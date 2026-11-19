"""Credential refresh uses a private temporary file and cleans it after failure."""

import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cursor_plugin.credentials import read_browser_credentials, write_credentials


class CredentialWriteTests(unittest.TestCase):
    def test_atomic_refresh_keeps_private_file_and_cleans_failed_write(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = directory / "auth.json"
        old = {"accessToken": "fixture-before"}
        write_credentials(path, old)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(read_browser_credentials(path), old)
        with patch("cursor_plugin.credentials.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                write_credentials(path, {"accessToken": "fixture-after"})
        self.assertEqual(read_browser_credentials(path), old)
        self.assertEqual(list(directory.iterdir()), [path])
