import os
import stat
import tempfile
import unittest
from pathlib import Path

import config


class RuntimeSecurityTests(unittest.TestCase):
    def test_secret_file_permissions_are_restricted_to_its_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / ".env"
            secret_file.write_text("TOKEN=secret\n", encoding="utf-8")
            secret_file.chmod(0o644)

            config.secure_secret_file(secret_file)

            self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)

if __name__ == "__main__":
    unittest.main()
