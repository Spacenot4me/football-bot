from contextlib import closing
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("MISTRAL_API_KEY", "test-key")

import pytz
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.error import Forbidden
from telegram.ext import Application

import main
from operations import common
from operations.match_registrations import confirm_user_registration
import register_funcs


class PlusOneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_db_path = common.DB_PATH
        common.DB_PATH = str(Path(self.directory.name) / "football.db")
        self.match_time = datetime.now(pytz.timezone("Europe/Prague")) + timedelta(days=2)
        with closing(sqlite3.connect(common.DB_PATH)) as connection, connection:
            connection.executescript("""
                CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT, name TEXT);
                CREATE TABLE Chats (id INTEGER PRIMARY KEY, chat_id TEXT, name TEXT);
                CREATE TABLE Matches (
                    match_id INTEGER PRIMARY KEY, chat_id INTEGER,
                    datetime TEXT, created_at TEXT
                );
                CREATE TABLE Match_Registration (
                    registration_id INTEGER PRIMARY KEY, match_id INTEGER,
                    user_id INTEGER, registered_by_id INTEGER, is_plus INTEGER,
                    confirmed INTEGER, priority INTEGER,
                    registered_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE Bans (ban_id INTEGER PRIMARY KEY, user_id INTEGER, until TEXT);
                INSERT INTO Users VALUES (1, 'inviter', 'Inviter');
                INSERT INTO Users VALUES (2, 'Player_Two', 'Player Two');
                INSERT INTO Users VALUES (3, 'player_three', 'Player Three');
                INSERT INTO Users VALUES (4, 'player_four', 'Player Four');
                INSERT INTO Chats VALUES (10, '-100', 'Football');
            """)
            connection.execute("INSERT INTO Matches VALUES (1, 10, ?, ?)", (
                self.match_time.strftime('%Y-%m-%d %H:%M:%S%z'),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            ))
        self.message = SimpleNamespace(
            text="@player_two", reply_to_message=None, reply_text=AsyncMock(),
        )
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=1, type="private"),
            effective_message=self.message,
            callback_query=None,
        )
        self.context = SimpleNamespace(
            user_data={'plus_one_prompt': {'chat_id': -100, 'match_id': 1}}, job_queue=Mock(),
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=500)),
                get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")),
            ),
        )

    def tearDown(self):
        common.DB_PATH = self.previous_db_path
        self.directory.cleanup()

    def registrations(self):
        with closing(sqlite3.connect(common.DB_PATH)) as connection:
            return connection.execute(
                "SELECT user_id, registered_by_id, is_plus, confirmed, match_id FROM Match_Registration ORDER BY user_id"
            ).fetchall()

    async def invite(self):
        await register_funcs.register_plus_one_by_username(self.update, self.context)

    async def open_menu(self):
        previous_chat = self.update.effective_chat
        self.update.effective_chat = SimpleNamespace(id=-100, type="supergroup")
        self.update.callback_query = SimpleNamespace(from_user=SimpleNamespace(id=1))
        await register_funcs.register_another_from_chat(self.update, self.context)
        self.update.callback_query = None
        self.update.effective_chat = previous_chat
        self.context.bot.send_message.reset_mock()

    async def test_private_username_invites_case_insensitively_and_requires_confirmation(self):
        await self.invite()
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 1)])
        sent = self.context.bot.send_message.await_args.kwargs
        self.assertEqual(sent['chat_id'], 2)
        self.assertEqual(sent['reply_markup'].inline_keyboard[0][0].text, "Confirm")
        job = self.context.job_queue.run_once.call_args
        self.assertEqual(job.args[1], 14400)
        self.assertEqual(job.kwargs['data'], {'chat_id': -100, 'user_id': 2, 'match_id': 1})

    async def test_chat_member_is_registered_without_external_plus_flag(self):
        self.context.bot.get_chat_member.return_value.status = "member"
        await self.invite()
        self.assertEqual(self.registrations(), [(2, 1, 0, 0, 1)])

    async def test_unknown_user_is_not_created(self):
        self.message.text = "@unknown"
        await self.invite()
        self.assertEqual(self.registrations(), [])
        self.assertIn("/start", self.message.reply_text.await_args.args[0])
        self.context.bot.send_message.assert_not_awaited()

    async def test_invalid_multiple_mentions_are_rejected(self):
        self.message.text = "@player_two @player_three"
        await self.invite()
        self.assertEqual(self.registrations(), [])
        self.context.bot.send_message.assert_not_awaited()

    async def test_inviter_must_be_registered_and_cannot_invite_self(self):
        self.update.effective_user.id = 999
        await self.invite()
        self.assertEqual(self.registrations(), [])
        self.update.effective_user.id = 1
        self.message.text = "@inviter"
        await self.invite()
        self.assertEqual(self.registrations(), [])

    async def test_reinviting_pending_player_does_not_confirm_them(self):
        await self.invite()
        await self.invite()
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 1)])
        self.assertEqual(self.context.bot.send_message.await_count, 1)
        self.assertEqual(self.context.job_queue.run_once.call_count, 1)

    async def test_existing_external_plus_limit_applies(self):
        await self.invite()
        self.message.text = "@player_three"
        await self.invite()
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 1)])

    async def test_closed_or_unconfigured_chat_does_not_register(self):
        self.context.user_data['plus_one_prompt']['chat_id'] = -404
        await self.invite()
        self.assertEqual(self.registrations(), [])
        self.context.user_data['plus_one_prompt']['chat_id'] = -100
        with closing(sqlite3.connect(common.DB_PATH)) as connection, connection:
            connection.execute("UPDATE Matches SET datetime = ?", (
                (self.match_time - timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S%z'),
            ))
        await self.invite()
        self.assertEqual(self.registrations(), [])

    async def test_failed_private_delivery_removes_new_registration(self):
        self.context.bot.send_message.side_effect = Forbidden("Bot was blocked")
        await self.invite()
        self.assertEqual(self.registrations(), [])
        self.context.job_queue.run_once.assert_not_called()

    async def test_menu_keeps_buttons_and_private_username_uses_menu_chat(self):
        self.update.effective_chat = SimpleNamespace(id=-100, type="supergroup")
        self.update.callback_query = SimpleNamespace(from_user=SimpleNamespace(id=1))
        await register_funcs.register_another_from_chat(self.update, self.context)
        markup = self.context.bot.send_message.await_args.kwargs['reply_markup']
        self.assertEqual(sum(len(row) for row in markup.inline_keyboard), 4)
        self.assertTrue(all(len(row) <= 2 for row in markup.inline_keyboard))
        self.update.callback_query = None
        self.update.effective_chat = SimpleNamespace(id=1, type="private")
        self.message.text = "@player_two"
        self.message.reply_to_message = SimpleNamespace(message_id=500)
        await self.invite()
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 1)])

    async def test_private_message_without_reply_uses_latest_menu(self):
        await self.open_menu()
        self.update.effective_chat = SimpleNamespace(id=1, type="private")
        self.message.text = "@player_two"
        await self.invite()
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 1)])

    async def test_private_message_without_menu_cannot_choose_arbitrary_match(self):
        self.context.user_data.clear()
        self.update.effective_chat = SimpleNamespace(id=1, type="private")
        self.message.text = "@player_two"
        await self.invite()
        self.assertEqual(self.registrations(), [])

    async def test_stale_menu_does_not_register_for_new_match(self):
        await self.open_menu()
        with closing(sqlite3.connect(common.DB_PATH)) as connection, connection:
            connection.execute("INSERT INTO Matches SELECT 2, chat_id, datetime, created_at FROM Matches WHERE match_id=1")
        self.update.effective_chat = SimpleNamespace(id=1, type="private")
        self.message.text = "@player_two"
        await self.invite()
        self.assertEqual(self.registrations(), [])
        self.assertIn("older match", self.message.reply_text.await_args.args[0])

    async def test_existing_button_uses_same_invitation_path(self):
        self.update.callback_query = SimpleNamespace(from_user=SimpleNamespace(id=1))
        await register_funcs.register_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 1)])

    async def test_confirmation_only_confirms_selected_player(self):
        await self.invite()
        self.context.bot.get_chat_member.return_value.status = "member"
        self.message.text = "@player_three"
        await self.invite()
        confirm_user_registration(1, 2)
        self.assertEqual(self.registrations(), [(2, 1, 1, 1, 1), (3, 1, 0, 0, 1)])

    async def test_confirmation_timeout_targets_original_match_and_handles_removed_player(self):
        await self.invite()
        with closing(sqlite3.connect(common.DB_PATH)) as connection, connection:
            connection.execute("INSERT INTO Matches SELECT 2, chat_id, datetime, created_at FROM Matches WHERE match_id=1")
            connection.execute("INSERT INTO Match_Registration (match_id,user_id,registered_by_id,is_plus,confirmed,priority) VALUES (2,2,1,1,0,3)")
        self.context.job = SimpleNamespace(data={'chat_id': -100, 'user_id': 2, 'match_id': 1})
        await register_funcs.check_for_confimation(self.context)
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 2)])
        await register_funcs.check_for_confimation(self.context)
        self.assertEqual(self.registrations(), [(2, 1, 1, 0, 2)])

    async def test_handler_routing_only_accepts_private_username(self):
        application = Application.builder().token("123456:test-token").build()
        builder = Mock()
        builder.token.return_value.build.return_value = application
        with patch.object(Application, "builder", return_value=builder):
            with patch.object(Application, "run_polling"), patch.object(main, "initiate"):
                main.main()
        for chat_type, text in [
            ('private', '@player_two'), ('private', '  @player_two  '),
        ]:
            entities = []
            if text.startswith('/'):
                entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len('/plus_one'))]
            message = Message(10, datetime.now(), Chat(-100 if chat_type != 'private' else 1, chat_type),
                              text=text, from_user=User(1, 'Inviter', False), entities=entities)
            message.set_bot(SimpleNamespace(username='cuefa_bot'))
            update = Update(10, message=message)
            handler = next(handler for handler in application.handlers[0] if handler.check_update(update))
            self.assertIs(handler.callback, register_funcs.register_plus_one_by_username)
        message = Message(11, datetime.now(), Chat(-100, 'supergroup'), text='@player_two')
        self.assertFalse(any(handler.check_update(Update(11, message=message)) for handler in application.handlers[0]))
        for chat_type, text in [
            ('supergroup', '+1 @player_two'), ('private', '+1 @player_two'),
            ('supergroup', '/plus_one @player_two'), ('private', '/plus_one @player_two'),
        ]:
            entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len('/plus_one'))] if text.startswith('/') else []
            message = Message(12, datetime.now(), Chat(-100 if chat_type != 'private' else 1, chat_type),
                              text=text, from_user=User(1, 'Inviter', False), entities=entities)
            message.set_bot(SimpleNamespace(username='cuefa_bot'))
            matches = [handler for handler in application.handlers[0] if handler.check_update(Update(12, message=message))]
            self.assertFalse(any(handler.callback is register_funcs.register_plus_one_by_username for handler in matches))

    async def test_group_message_cannot_register_even_with_private_menu_context(self):
        self.update.effective_chat = SimpleNamespace(id=-100, type="supergroup")
        for text in ['@player_two', '+1 @player_two', '/plus_one @player_two']:
            self.message.text = text
            await self.invite()
        self.assertEqual(self.registrations(), [])
        self.context.bot.send_message.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
