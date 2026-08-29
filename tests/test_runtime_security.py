import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import config
import telegram_bot


class RuntimeSecurityTests(unittest.TestCase):
    def test_secret_file_permissions_are_restricted_to_its_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_file = Path(directory) / ".env"
            secret_file.write_text("TOKEN=secret\n", encoding="utf-8")
            secret_file.chmod(0o644)

            config.secure_secret_file(secret_file)

            self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)

    def test_telegram_polling_requires_a_sender_allowlist(self):
        previous_chats = config.TELEGRAM_ALLOWED_CHAT_IDS
        previous_users = config.TELEGRAM_ALLOWED_USER_IDS
        try:
            config.TELEGRAM_ALLOWED_CHAT_IDS = frozenset()
            config.TELEGRAM_ALLOWED_USER_IDS = frozenset()
            with self.assertRaisesRegex(ValueError, "requires"):
                config.validate_telegram_allowlist()
        finally:
            config.TELEGRAM_ALLOWED_CHAT_IDS = previous_chats
            config.TELEGRAM_ALLOWED_USER_IDS = previous_users

    def test_telegram_commands_require_configured_chat_and_user(self):
        previous_chats = config.TELEGRAM_ALLOWED_CHAT_IDS
        previous_users = config.TELEGRAM_ALLOWED_USER_IDS
        try:
            config.TELEGRAM_ALLOWED_CHAT_IDS = frozenset({"100"})
            config.TELEGRAM_ALLOWED_USER_IDS = frozenset({"200"})
            self.assertTrue(telegram_bot.is_authorized_sender(
                SimpleNamespace(effective_chat=SimpleNamespace(id=100), effective_user=SimpleNamespace(id=200))
            ))
            self.assertFalse(telegram_bot.is_authorized_sender(
                SimpleNamespace(effective_chat=SimpleNamespace(id=100), effective_user=SimpleNamespace(id=201))
            ))
        finally:
            config.TELEGRAM_ALLOWED_CHAT_IDS = previous_chats
            config.TELEGRAM_ALLOWED_USER_IDS = previous_users


if __name__ == "__main__":
    unittest.main()
