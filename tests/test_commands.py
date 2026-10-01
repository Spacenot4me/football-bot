import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("MISTRAL_API_KEY", "test-key")

from telegram import Chat, ChatMemberMember, Message, MessageEntity, Update, User
from telegram.ext import Application

import bans
import jobs_funcs
import main
import pidor
import utils
from operations import common


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = common.DB_PATH
        common.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        with sqlite3.connect(common.DB_PATH) as connection:
            connection.executescript("""
                CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT, name TEXT);
                CREATE TABLE Bans (ban_id INTEGER PRIMARY KEY, user_id INTEGER, until TEXT);
                CREATE TABLE Chats (
                    id INTEGER PRIMARY KEY, chat_id TEXT, name TEXT, game_time TEXT,
                    game_week_day TEXT, reg_time TEXT, reg_week_day TEXT
                );
                INSERT INTO Users VALUES (7, 'vlad', '<Vlad &>');
            """)
        self.update = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=7), reply_text=AsyncMock()),
            effective_chat=SimpleNamespace(id=7),
        )
        self.context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        common.DB_PATH = self.previous_db_path
        self.temp_dir.cleanup()

    async def test_bans_query_uses_user_id_and_escapes_name(self):
        with sqlite3.connect(common.DB_PATH) as connection:
            connection.executemany(
                "INSERT INTO Bans (user_id, until) VALUES (?, ?)",
                [(7, "2030-01-01"), (8, "2040-01-01")],
            )
        await bans.get_my_bans(self.update, self.context)
        text = self.context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("&lt;Vlad &amp;&gt;", text)
        self.assertIn("2030-01-01", text)
        self.assertNotIn("2040-01-01", text)

    async def test_bans_without_records(self):
        await bans.get_my_bans(self.update, self.context)
        self.assertIn("no bans", self.context.bot.send_message.await_args.kwargs["text"])

    async def test_unregistered_user_gets_instructions(self):
        self.update.message.from_user.id = 8
        await bans.get_my_bans(self.update, self.context)
        self.assertIn("/start", self.update.message.reply_text.await_args.args[0])
        self.context.bot.send_message.assert_not_awaited()

    async def test_random_selection_awaits_mistral_with_timeout(self):
        self.context.bot.get_chat_member = AsyncMock(
            return_value=ChatMemberMember(User(7, "Vlad", False, username="vlad"))
        )
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Generated poem"))])
        client = Mock()
        client.chat.complete_async = AsyncMock(return_value=response)
        with patch.object(pidor, "mistral", client):
            await pidor.pick_random_user(self.update, self.context)
        client.chat.complete_async.assert_awaited_once()
        self.assertEqual(client.chat.complete_async.await_args.kwargs["timeout_ms"], 30000)
        client.chat.complete.assert_not_called()
        self.update.message.reply_text.assert_awaited_once_with("Generated poem")
        with sqlite3.connect(common.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT user_id FROM Random_Selections").fetchone()[0], 7)

    async def test_error_handler_only_logs(self):
        message = Message(1, datetime.now(timezone.utc), Chat(7, "private"), text="/get_my_bans")
        update = Update(1, message=message)
        context = SimpleNamespace(error=ValueError("private diagnostic details"))
        with patch.object(Message, "reply_text", new_callable=AsyncMock) as reply:
            with self.assertLogs(main.logger, level="ERROR"):
                await main.handle_error(update, context)
            reply.assert_not_awaited()

    async def test_schedule_rejects_invalid_arguments_without_saving(self):
        self.context.job_queue = Mock()
        self.context.job_queue.jobs.return_value = []
        self.context.chat_data = {}
        for args in (
            [], ["Football"],
            ["Football", "invalid", "10:00", "wednesday", "19:00"],
            ["Football", "monday", "10:00", "wednesday", "25:00"],
        ):
            with self.subTest(args=args):
                self.context.args = args
                self.update.message.reply_text.reset_mock()
                with patch.object(jobs_funcs, "is_chat_admin", new=AsyncMock(return_value=True)):
                    await jobs_funcs.start_repeating_job(self.update, self.context)
                self.update.message.reply_text.assert_awaited_once()
                self.context.job_queue.run_repeating.assert_not_called()
                with sqlite3.connect(common.DB_PATH) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM Chats").fetchone()[0], 0)

    async def test_schedule_saves_valid_arguments_and_creates_job(self):
        self.context.args = ["Football", "monday", "10:00", "wednesday", "19:00"]
        self.context.chat_data = {}
        self.context.job_queue = Mock()
        self.context.job_queue.jobs.return_value = []
        with patch.object(jobs_funcs, "is_chat_admin", new=AsyncMock(return_value=True)):
            await jobs_funcs.start_repeating_job(self.update, self.context)
        self.context.job_queue.run_repeating.assert_called_once()
        job_args = self.context.job_queue.run_repeating.call_args.kwargs
        self.assertEqual(job_args["data"], {"chat_id": 7})
        self.assertEqual(job_args["interval"], 7 * 24 * 60 * 60)
        self.assertGreater(job_args["first"], 0)
        with sqlite3.connect(common.DB_PATH) as connection:
            row = connection.execute("SELECT name, reg_week_day, reg_time, game_week_day, game_time FROM Chats").fetchone()
            self.assertEqual(row, tuple(self.context.args))

    async def test_match_history_in_unconfigured_chat(self):
        for callback in (utils.last_match, utils.last_5_matches_players):
            with self.subTest(callback=callback.__name__):
                self.update.message.reply_text.reset_mock()
                with patch.object(utils, "get_last_match") as last:
                    with patch.object(utils, "get_last_5_matches_with_players") as last_five:
                        await callback(self.update, self.context)
                        last.assert_not_called()
                        last_five.assert_not_called()
                self.assertIn("not configured", self.update.message.reply_text.await_args.args[0])

    async def test_registered_commands_precede_unknown_command_handler(self):
        application = Application.builder().token("123456:test-token").build()
        builder = Mock()
        builder.token.return_value.build.return_value = application
        with patch.object(Application, "builder", return_value=builder):
            with patch.object(Application, "run_polling"), patch.object(main, "initiate"):
                main.main()
        self.assertIn(main.handle_error, application.error_handlers)
        for text, callback in (
            ("/get_my_bans", bans.get_my_bans),
            ("/help", main.unknown_command),
            ("/register_another_from_chat", main.unknown_command),
        ):
            message = Message(
                1, datetime.now(timezone.utc), Chat(7, "private"), text=text,
                entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text))],
            )
            message.set_bot(SimpleNamespace(username="cuefa_bot"))
            update = Update(1, message=message)
            handler = next(h for h in application.handlers[0] if h.check_update(update))
            self.assertIs(handler.callback, callback)


if __name__ == "__main__":
    unittest.main()
