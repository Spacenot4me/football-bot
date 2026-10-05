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
from telegram.error import Forbidden
from telegram.ext import Application, CallbackQueryHandler

import main
from operations import common
from operations.match_registrations import delete_match_plus_one_registration, get_current_match_registrations
import remove_funcs


class RemovePlusOneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_db_path = common.DB_PATH
        common.DB_PATH = str(Path(self.directory.name) / "football.db")
        match_time = datetime.now(pytz.timezone("Europe/Prague")) + timedelta(days=2)
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            conn.executescript("""
                CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT, name TEXT);
                CREATE TABLE Chats (id INTEGER PRIMARY KEY, chat_id TEXT, name TEXT);
                CREATE TABLE Matches (
                    match_id INTEGER PRIMARY KEY, chat_id INTEGER, datetime TEXT,
                    created_at TEXT, amount_per_person INTEGER
                );
                CREATE TABLE Match_Registration (
                    registration_id INTEGER PRIMARY KEY, match_id INTEGER,
                    user_id INTEGER, registered_by_id INTEGER, is_plus INTEGER,
                    confirmed INTEGER, priority INTEGER,
                    registered_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE Bans (ban_id INTEGER PRIMARY KEY, user_id INTEGER, until TEXT);
                INSERT INTO Chats VALUES (10, '-100', 'Football');
            """)
            conn.execute("INSERT INTO Matches VALUES (1,10,?,CURRENT_TIMESTAMP,130)",
                         (match_time.strftime('%Y-%m-%d %H:%M:%S%z'),))
            conn.executemany("INSERT INTO Users VALUES (?, ?, ?)",
                             [(number, f"player{number}", f"Player {number}") for number in range(1, 31)])
            conn.executemany("""
                INSERT INTO Match_Registration
                    (registration_id,match_id,user_id,registered_by_id,is_plus,confirmed,priority)
                VALUES (?,1,?,?,?,1,2)
            """, [(1,1,1,0), (2,2,1,1), (3,3,1,0), (4,4,4,0), (5,5,4,1)])
        self.query = SimpleNamespace(
            from_user=SimpleNamespace(id=1), data='removeplusone_-100',
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        self.update = SimpleNamespace(callback_query=self.query)
        self.context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        common.DB_PATH = self.previous_db_path
        self.directory.cleanup()

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            return conn.execute(sql, params).fetchall()

    def registered_ids(self):
        return [row[0] for row in self.execute("SELECT user_id FROM Match_Registration ORDER BY user_id")]

    async def test_external_guest_removal_uses_numeric_chat_id_and_preserves_other_players(self):
        self.assertIn('registered_by_id', get_current_match_registrations(10)[0])
        await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.registered_ids(), [1,3,4,5])
        self.assertEqual(self.context.bot.send_message.await_args.kwargs['chat_id'], 2)
        self.query.edit_message_text.assert_awaited_once_with(text="Removed Player 2 from the match.")
        self.assertEqual(self.execute("SELECT user_id FROM Bans"), [])

    async def test_invited_chat_member_can_be_removed(self):
        await remove_funcs.remove_plus_one(self.update, self.context, '-100', '3')
        self.assertEqual(self.registered_ids(), [1,2,4,5])
        self.assertEqual(self.context.bot.send_message.await_args.kwargs['chat_id'], 3)

    async def test_remove_menu_lists_only_owned_invitations(self):
        await remove_funcs.start_remove_plus_one(self.update, self.context, '-100')
        message = self.context.bot.send_message.await_args.kwargs
        buttons = [button for row in message['reply_markup'].inline_keyboard for button in row]
        self.assertEqual(message['chat_id'], 1)
        self.assertEqual([button.callback_data for button in buttons],
                         ['removeplusoneconfirm_-100_2', 'removeplusoneconfirm_-100_3'])
        self.assertEqual(self.registered_ids(), [1,2,3,4,5])

    async def test_legacy_confirmation_with_two_invites_requires_selection(self):
        await remove_funcs.remove_plus_one(self.update, self.context, '-100')
        self.assertEqual(self.registered_ids(), [1,2,3,4,5])
        self.assertIn('Choose', self.context.bot.send_message.await_args.kwargs['text'])

    async def test_legacy_confirmation_with_one_invite_still_works(self):
        self.execute("DELETE FROM Match_Registration WHERE registration_id=3")
        await remove_funcs.remove_plus_one(self.update, self.context, '-100')
        self.assertEqual(self.registered_ids(), [1,4,5])

    async def test_empty_invites_produce_explanation_without_claiming_removal(self):
        self.execute("DELETE FROM Match_Registration WHERE registration_id IN (2,3)")
        await remove_funcs.start_remove_plus_one(self.update, self.context, '-100')
        self.assertIn('no invited players', self.context.bot.send_message.await_args.kwargs['text'])
        await remove_funcs.remove_plus_one(self.update, self.context, '-100')
        self.assertIn('no longer available', self.query.edit_message_text.await_args.kwargs['text'])
        self.assertEqual(self.registered_ids(), [1,4,5])

    async def test_foreign_and_self_registration_cannot_be_deleted(self):
        for registration_id in [1,4,5]:
            self.assertIsNone(delete_match_plus_one_registration(1,1,registration_id))
            await remove_funcs.remove_plus_one(self.update, self.context, '-100', registration_id)
        self.assertEqual(self.registered_ids(), [1,2,3,4,5])

    async def test_stale_confirmation_does_not_delete_replacement_invitation(self):
        self.execute("DELETE FROM Match_Registration WHERE registration_id=2")
        self.execute("""
            INSERT INTO Match_Registration
                (registration_id,match_id,user_id,registered_by_id,is_plus,confirmed,priority)
            VALUES (20,1,2,1,1,1,2)
        """)
        await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.registered_ids(), [1,2,3,4,5])

    async def test_old_match_confirmation_does_not_remove_from_current_match(self):
        self.execute("INSERT INTO Matches SELECT 2,chat_id,datetime,created_at,amount_per_person FROM Matches WHERE match_id=1")
        self.execute("""
            INSERT INTO Match_Registration
                (registration_id,match_id,user_id,registered_by_id,is_plus,confirmed,priority)
            VALUES (20,2,2,1,1,1,2)
        """)
        await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.execute("SELECT registration_id FROM Match_Registration WHERE user_id=2 ORDER BY registration_id"),
                         [(2,), (20,)])

    async def test_repeat_removal_does_not_report_success_twice(self):
        await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.context.bot.send_message.await_count, 1)
        self.assertIn('no longer available', self.query.edit_message_text.await_args.kwargs['text'])

    async def test_unconfigured_or_closed_chat_does_not_modify_registrations(self):
        await remove_funcs.remove_plus_one(self.update, self.context, '-404', '2')
        with patch.object(remove_funcs, 'get_hours_until_match', return_value=-1):
            await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.registered_ids(), [1,2,3,4,5])

    async def test_unreachable_guest_does_not_undo_removal(self):
        self.context.bot.send_message.side_effect = Forbidden('Bot was blocked')
        with self.assertLogs(remove_funcs.logger, level='WARNING'):
            await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.registered_ids(), [1,3,4,5])
        self.query.edit_message_text.assert_awaited_once_with(text="Removed Player 2 from the match.")

    async def test_late_cancellation_bans_removed_player_instead_of_inviter(self):
        with patch.object(remove_funcs, 'get_hours_until_match', return_value=10):
            await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.execute("SELECT user_id FROM Bans"), [(2,)])

    async def test_one_waiting_player_prevents_ban_even_when_promotion_dm_fails(self):
        for number in range(6,16):
            self.execute("""
                INSERT INTO Match_Registration
                    (registration_id,match_id,user_id,registered_by_id,is_plus,confirmed,priority)
                VALUES (?,1,?,?,0,1,2)
            """, (number,number,number))

        async def send_message(**kwargs):
            if kwargs['chat_id'] == 15:
                raise Forbidden('Waiting player blocked the bot')

        self.context.bot.send_message.side_effect = send_message
        with patch.object(remove_funcs, 'get_hours_until_match', return_value=10):
            with self.assertLogs(remove_funcs.logger, level='WARNING'):
                await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(len(self.registered_ids()), 14)
        self.assertEqual(self.execute("SELECT user_id FROM Bans"), [])
        self.assertTrue(any(call.kwargs['chat_id'] == 15 for call in self.context.bot.send_message.await_args_list))

    async def test_waiting_guest_cancellation_does_not_ban_or_promote_another_player(self):
        self.execute("UPDATE Match_Registration SET priority=4 WHERE registration_id=2")
        for number in range(6,16):
            self.execute("""
                INSERT INTO Match_Registration
                    (registration_id,match_id,user_id,registered_by_id,is_plus,confirmed,priority)
                VALUES (?,1,?,?,0,1,2)
            """, (number,number,number))
        with patch.object(remove_funcs, 'get_hours_until_match', return_value=10):
            await remove_funcs.remove_plus_one(self.update, self.context, '-100', '2')
        self.assertEqual(self.execute("SELECT user_id FROM Bans"), [])
        self.assertEqual(self.context.bot.send_message.await_count, 1)

    async def test_callback_button_and_confirmation_use_bound_registration(self):
        application = Application.builder().token("123456:test-token").build()
        builder = Mock()
        builder.token.return_value.build.return_value = application
        with patch.object(Application, 'builder', return_value=builder):
            with patch.object(Application, 'run_polling'), patch.object(main, 'initiate'):
                main.main()
        handler = next(handler for handler in application.handlers[0] if isinstance(handler, CallbackQueryHandler))
        await handler.callback(self.update, self.context)
        message = self.context.bot.send_message.await_args.kwargs
        self.query.data = message['reply_markup'].inline_keyboard[0][0].callback_data
        self.context.bot.send_message.reset_mock()
        await handler.callback(self.update, self.context)
        self.assertEqual(self.query.answer.await_count, 2)
        self.assertEqual(self.registered_ids(), [1,3,4,5])
        self.assertEqual(self.context.bot.send_message.await_args.kwargs['chat_id'], 2)


if __name__ == '__main__':
    unittest.main()
