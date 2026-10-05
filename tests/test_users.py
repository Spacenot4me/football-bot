from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden

from operations import common
from operations.users import get_user
import users


class DeleteAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_db_path = common.DB_PATH
        common.DB_PATH = str(Path(self.directory.name) / "football.db")
        with closing(sqlite3.connect(common.DB_PATH)) as connection, connection:
            connection.executescript("""
                CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT, name TEXT);
                INSERT INTO Users VALUES (7, 'player_seven', 'Player Seven');
                INSERT INTO Users VALUES (8, 'player_eight', 'Player Eight');
            """)
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            effective_chat=SimpleNamespace(id=7),
        )
        self.context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        common.DB_PATH = self.previous_db_path
        self.directory.cleanup()

    async def test_deletes_only_callers_user_row_and_confirms_to_telegram_id(self):
        await users.delete_account(self.update, self.context)
        self.assertIsNone(get_user(7))
        self.assertIsNotNone(get_user(8))
        sent = self.context.bot.send_message.await_args.kwargs
        self.assertEqual(sent['chat_id'], 7)
        self.assertIn('has been deleted', sent['text'])
        self.assertIn('/start', sent['text'])

    async def test_repeat_deletion_returns_not_registered(self):
        await users.delete_account(self.update, self.context)
        await users.delete_account(self.update, self.context)
        self.context.bot.send_message.assert_awaited_with(chat_id=7, text="You are not registered.")

    async def test_command_in_group_still_deletes_only_sender_and_replies_in_private(self):
        self.update.effective_chat.id = -100
        await users.delete_account(self.update, self.context)
        self.assertIsNone(get_user(7))
        self.assertIsNotNone(get_user(8))
        self.assertEqual(self.context.bot.send_message.await_args.kwargs['chat_id'], 7)

    async def test_sqlite_failure_keeps_user_and_reports_error_without_missing_chat_id(self):
        with patch.object(users, 'delete_user', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertLogs(users.logger, level='ERROR'):
                await users.delete_account(self.update, self.context)
        self.assertIsNotNone(get_user(7))
        sent = self.context.bot.send_message.await_args.kwargs
        self.assertEqual(sent['chat_id'], 7)
        self.assertIn('Could not delete', sent['text'])
        self.assertNotIn('database is locked', sent['text'])

    async def test_notification_failure_is_not_misreported_as_database_failure(self):
        self.context.bot.send_message.side_effect = Forbidden('Bot was blocked')
        with self.assertRaises(Forbidden):
            await users.delete_account(self.update, self.context)
        self.assertIsNone(get_user(7))
        self.assertEqual(self.context.bot.send_message.await_count, 1)


if __name__ == '__main__':
    unittest.main()
