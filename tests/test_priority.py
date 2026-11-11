from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden

import draft_funcs
import register_funcs
from operations import common, team_drafts
from operations.match_registrations import get_current_match_registrations, set_match_registration_priority


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db_patch = patch.object(common, 'DB_PATH', str(Path(self.directory.name) / 'test.db'))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            conn.executescript('''
                CREATE TABLE Chats (id INTEGER PRIMARY KEY, chat_id TEXT, name TEXT);
                CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT, name TEXT);
                CREATE TABLE Matches (
                    match_id INTEGER PRIMARY KEY, chat_id INTEGER, datetime TEXT,
                    amount_per_person INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE Match_Registration (
                    registration_id INTEGER PRIMARY KEY, match_id INTEGER, user_id INTEGER,
                    registered_by_id INTEGER, is_plus INTEGER, confirmed INTEGER,
                    priority INTEGER, registered_at TEXT
                );
                CREATE TABLE Bans (ban_id INTEGER PRIMARY KEY, user_id INTEGER, until TEXT);
                INSERT INTO Chats VALUES (10, '-100', 'Football'), (20, '-200', 'Other chat');
                INSERT INTO Matches (match_id, chat_id, datetime) VALUES
                    (1, 10, '2020-01-01 19:00:00+0100'),
                    (2, 20, '2035-01-01 19:00:00+0100'),
                    (3, 10, '2035-01-01 19:00:00+0100');
                INSERT INTO Bans VALUES (1, 15, '2036-01-01');
            ''')
            conn.executemany('INSERT INTO Users VALUES (?, ?, ?)',
                             [(uid, f'player{uid}', f'Player {uid}') for uid in range(1, 18)])
            conn.executemany('''INSERT INTO Match_Registration
                (match_id, user_id, registered_by_id, is_plus, confirmed, priority, registered_at)
                VALUES (3, ?, ?, 0, 1, 2, ?)''',
                [(uid, uid, f'2026-11-01 10:00:{uid:02}') for uid in range(1, 17)])
            conn.executescript('''
                INSERT INTO Match_Registration
                    (match_id, user_id, registered_by_id, is_plus, confirmed, priority, registered_at)
                VALUES (1, 15, 15, 0, 1, 3, '2020-01-01'),
                       (2, 15, 15, 0, 1, 4, '2026-11-01');
            ''')
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100, type='supergroup'),
            effective_user=SimpleNamespace(id=99),
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
        )
        self.update.message = self.update.effective_message
        self.context = SimpleNamespace(
            args=['@PLAYER15', '1'],
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
                edit_message_text=AsyncMock(), pin_chat_message=AsyncMock(),
                delete_message=AsyncMock(),
            ),
        )
        admin_patch = patch.object(register_funcs, 'is_chat_admin', new=AsyncMock(return_value=True))
        self.admin = admin_patch.start()
        self.addCleanup(admin_patch.stop)
        self.real_sync = register_funcs.sync_registration
        sync_patch = patch.object(register_funcs, 'sync_registration', new=AsyncMock())
        self.sync = sync_patch.start()
        self.addCleanup(sync_patch.stop)

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            return conn.execute(statement, params).fetchall()

    async def run_command(self):
        await register_funcs.set_priority(self.update, self.context)

    async def test_changes_only_priority_in_current_chat_and_match_and_reorders_queue(self):
        before = self.sql('SELECT * FROM Match_Registration ORDER BY registration_id')
        await self.run_command()
        after = self.sql('SELECT * FROM Match_Registration ORDER BY registration_id')
        expected = [row[:6] + (1,) + row[7:] if row[1:3] == (3, 15) else row for row in before]
        self.assertEqual(after, expected)
        self.assertEqual(self.sql('SELECT * FROM Bans'), [(1, 15, '2036-01-01')])
        queue = get_current_match_registrations(10)
        self.assertEqual([row['user_id'] for row in queue], [15] + list(range(1, 15)) + [16])
        self.sync.assert_awaited_once_with(self.context, -100, 3)
        reply = self.update.message.reply_text.await_args.args[0]
        self.assertIn('@player15', reply)
        self.assertIn('P1', reply)
        self.assertIn('Refresh', reply)

    async def test_all_supported_priorities_and_repeating_same_value(self):
        for priority in ('1', '2', '3', '4', '4'):
            self.context.args[1] = priority
            await self.run_command()
            self.assertEqual(self.sql('SELECT priority FROM Match_Registration WHERE match_id=3 AND user_id=15'),
                             [(int(priority),)])
            self.assertIn(f'P{priority}', self.update.message.reply_text.await_args.args[0])

    async def test_invited_unconfirmed_player_keeps_invitation_details(self):
        self.sql('''UPDATE Match_Registration SET registered_by_id=1, confirmed=0, is_plus=1
                    WHERE match_id=3 AND user_id=15''')
        await self.run_command()
        self.assertEqual(self.sql('''SELECT registered_by_id, confirmed, is_plus, priority
                                    FROM Match_Registration WHERE match_id=3 AND user_id=15'''),
                         [(1, 0, 1, 1)])

    async def test_non_admin_cannot_change_priority(self):
        self.admin.return_value = False
        before = self.sql('SELECT * FROM Match_Registration')
        await self.run_command()
        self.assertEqual(self.sql('SELECT * FROM Match_Registration'), before)
        self.sync.assert_not_awaited()

    async def test_private_channel_and_anonymous_commands_do_not_change_priority(self):
        for kind in ('private', 'channel'):
            self.update.effective_chat.type = kind
            await self.run_command()
        self.update.effective_chat.type = 'group'
        self.update.effective_user = None
        await self.run_command()
        self.admin.assert_not_awaited()
        self.assertEqual(self.sql('SELECT priority FROM Match_Registration WHERE match_id=3 AND user_id=15'), [(2,)])
        self.sync.assert_not_awaited()

    async def test_invalid_arguments_return_usage_without_changing_database(self):
        before = self.sql('SELECT * FROM Match_Registration')
        for args in (None, [], ['@player15'], ['@player15', '1', 'extra'],
                     ['player15', '1'], ['@bad-name', '1'], ["@player15'", '1'],
                     ['@player15', '0'], ['@player15', '5'], ['@player15', '-1'],
                     ['@player15', '1.5'], ['@player15', 'one']):
            with self.subTest(args=args):
                self.context.args = args
                await self.run_command()
                self.assertIn('Usage: /set_priority', self.update.message.reply_text.await_args.args[0])
        self.assertEqual(self.sql('SELECT * FROM Match_Registration'), before)
        self.sync.assert_not_awaited()

    async def test_unknown_player_and_player_not_registered_for_match(self):
        for nickname, expected in (('@unknown', 'not registered in the bot'),
                                   ('@player17', 'not registered for the current match')):
            self.context.args[0] = nickname
            await self.run_command()
            self.assertIn(expected, self.update.message.reply_text.await_args.args[0])
        self.sync.assert_not_awaited()

    async def test_missing_chat_or_match_returns_explanation(self):
        self.update.effective_chat.id = -300
        await self.run_command()
        self.assertIn('not configured', self.update.message.reply_text.await_args.args[0])
        self.sql("INSERT INTO Chats VALUES (30, '-300', 'Empty chat')")
        await self.run_command()
        self.assertIn('no open registration', self.update.message.reply_text.await_args.args[0])
        self.sync.assert_not_awaited()

    async def test_started_match_cannot_be_changed(self):
        for hours in (0, -1):
            with patch.object(register_funcs, 'get_hours_until_match', return_value=hours):
                await self.run_command()
            self.assertIn('no open registration', self.update.message.reply_text.await_args.args[0])
        self.assertEqual(self.sql('SELECT priority FROM Match_Registration WHERE match_id=3 AND user_id=15'), [(2,)])
        self.sync.assert_not_awaited()

    async def test_sqlite_failure_does_not_report_success(self):
        with patch.object(register_funcs, 'set_match_registration_priority', side_effect=sqlite3.OperationalError('test')):
            with self.assertLogs(register_funcs.logger, level='ERROR'):
                await self.run_command()
        self.assertIn('Could not change', self.update.message.reply_text.await_args.args[0])
        self.assertEqual(self.sql('SELECT priority FROM Match_Registration WHERE match_id=3 AND user_id=15'), [(2,)])
        self.sync.assert_not_awaited()

    def test_sql_operation_rejects_other_chats_old_matches_and_out_of_range_priorities(self):
        self.assertFalse(set_match_registration_priority(20, 3, 15, 1))
        self.assertFalse(set_match_registration_priority(10, 1, 15, 1))
        for priority in (0, 5, -1):
            with self.assertRaises(ValueError):
                set_match_registration_priority(10, 3, 15, priority)
        self.assertEqual(self.sql('SELECT priority FROM Match_Registration WHERE user_id=15 ORDER BY match_id'),
                         [(3,), (4,), (2,)])

    async def test_priority_change_syncs_draft_replacements_and_invalidates_old_buttons(self):
        view = team_drafts.create_draft(3, 'manual', [1, 2])
        actor = view['draft'][f"captain_{view['turn'].lower()}_id"]
        view = team_drafts.pick_player(view['draft']['draft_id'], actor, 14, view['draft']['revision'])
        draft_id = view['draft']['draft_id']
        revision = view['draft']['revision']
        with patch.object(register_funcs, 'sync_registration', self.real_sync):
            await self.run_command()
        draft = team_drafts.get_active_draft(3)
        self.assertGreater(draft['revision'], revision)
        self.assertEqual(self.sql('SELECT state FROM Team_Picks WHERE draft_id=? AND user_id=14', (draft_id,)),
                         [('left',)])
        self.assertIsNotNone(team_drafts.pick_player(draft_id, actor, 3, revision)['error'])
        self.context.bot.send_message.assert_awaited()

    async def test_draft_delivery_error_does_not_undo_priority_or_prevent_confirmation(self):
        team_drafts.create_draft(3, 'manual', [1, 2])
        self.context.bot.send_message.side_effect = Forbidden('Cannot send draft')
        with patch.object(register_funcs, 'sync_registration', self.real_sync):
            with self.assertLogs(draft_funcs.logger, level='WARNING'):
                await self.run_command()
        self.assertEqual(self.sql('SELECT priority FROM Match_Registration WHERE match_id=3 AND user_id=15'), [(1,)])
        self.assertIn('P1', self.update.message.reply_text.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
