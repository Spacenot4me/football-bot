import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from date_utils import BOT_TIMEZONE
import date_utils
import jobs_funcs
from operations import common, team_drafts
from operations.matches import create_match, create_match_if_missing, get_current_match


def local(year, month, day, hour=0):
    return BOT_TIMEZONE.localize(datetime(year, month, day, hour))


class RegistrationStartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        db_patch = patch.object(common, 'DB_PATH', str(Path(self.directory.name) / 'test.db'))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            conn.executescript('''
                CREATE TABLE Chats (
                    id INTEGER PRIMARY KEY, chat_id TEXT, name TEXT,
                    game_week_day TEXT, game_time TEXT
                );
                CREATE TABLE Matches (
                    match_id INTEGER PRIMARY KEY, chat_id INTEGER, datetime TEXT,
                    amount_per_person INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT, name TEXT);
                CREATE TABLE Match_Registration (
                    registration_id INTEGER PRIMARY KEY, match_id INTEGER,
                    user_id INTEGER, registered_by_id INTEGER, confirmed INTEGER,
                    priority INTEGER, registered_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO Chats VALUES (10, '-100', 'Football', 'wednesday', '19:00');
                INSERT INTO Chats VALUES (20, '-200', 'Other football', 'wednesday', '19:00');
            ''')
        self.context = self.make_context()
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100),
            message=SimpleNamespace(reply_text=AsyncMock()),
        )
        admin_patch = patch.object(jobs_funcs, 'is_chat_admin', new=AsyncMock(return_value=True))
        self.admin = admin_patch.start()
        self.addCleanup(admin_patch.stop)

    def make_context(self, tg_chat_id=-100):
        return SimpleNamespace(
            job=SimpleNamespace(data={'chat_id': tg_chat_id}),
            bot=SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                pin_chat_message=AsyncMock(),
            ),
        )

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            return conn.execute(sql, params).fetchall()

    async def test_early_manual_start_then_schedule_preserves_roster_and_draft(self):
        # Sunday and Monday refer to the same Wednesday, across calendar weeks.
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 18, 12)):
            await jobs_funcs.manual_start_registration(self.update, self.context)
        match = get_current_match(10)
        self.rows("INSERT INTO Users VALUES (1, 'one', 'One'), (2, 'two', 'Two'), (3, 'three', 'Three')")
        self.rows('''INSERT INTO Match_Registration
            (match_id, user_id, registered_by_id, confirmed, priority)
            VALUES (?, 1, 1, 1, 2), (?, 2, 2, 1, 2), (?, 3, 3, 0, 2)''',
            (match['match_id'],) * 3)
        team_drafts.create_draft(match['match_id'], 'manual', [1, 2])
        before = {table: self.rows(f'SELECT * FROM {table}')
                  for table in ('Matches', 'Match_Registration', 'Team_Drafts', 'Team_Picks')}
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            await jobs_funcs.start(self.context)
        for table, rows in before.items():
            self.assertEqual(self.rows(f'SELECT * FROM {table}'), rows)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.pin_chat_message.assert_awaited_once_with(chat_id=-100, message_id=55)

    async def test_repeated_manual_start_does_not_publish_or_change_context_job(self):
        original_job = self.context.job
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 9)):
            await jobs_funcs.manual_start_registration(self.update, self.context)
            await jobs_funcs.manual_start_registration(self.update, self.context)
        self.assertIs(self.context.job, original_job)
        self.assertEqual(len(self.rows('SELECT * FROM Matches')), 1)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.pin_chat_message.assert_awaited_once()
        reply = self.update.message.reply_text.await_args.args[0]
        self.assertIn('already open', reply)
        self.assertIn('/show_registration_message', reply)

    async def test_manual_start_after_scheduled_start_does_not_republish(self):
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            await jobs_funcs.start(self.context)
            await jobs_funcs.manual_start_registration(self.update, self.context)
        self.assertEqual(len(self.rows('SELECT * FROM Matches')), 1)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.pin_chat_message.assert_awaited_once()
        self.assertIn('already open', self.update.message.reply_text.await_args.args[0])

    async def test_new_context_after_restart_still_skips_existing_match(self):
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            await jobs_funcs.start(self.context)
            restarted_context = self.make_context()
            await jobs_funcs.start(restarted_context)
        self.assertEqual(len(self.rows('SELECT * FROM Matches')), 1)
        restarted_context.bot.send_message.assert_not_awaited()
        restarted_context.bot.pin_chat_message.assert_not_awaited()

    async def test_following_week_opens_new_match_and_closes_old_draft_across_dst(self):
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            await jobs_funcs.start(self.context)
        old_match = get_current_match(10)
        team_drafts.create_draft(old_match['match_id'])
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 26, 10)):
            await jobs_funcs.start(self.context)
        self.assertEqual(self.rows('SELECT datetime, amount_per_person FROM Matches ORDER BY match_id'), [
            ('2026-10-21 19:00:00+02:00', 130), ('2026-10-28 19:00:00+01:00', 130),
        ])
        self.assertIsNone(team_drafts.get_active_draft(old_match['match_id']))
        self.assertEqual(self.context.bot.send_message.await_count, 2)
        self.assertEqual(self.context.bot.pin_chat_message.await_count, 2)

    async def test_same_match_time_in_another_chat_is_allowed(self):
        other_context = self.make_context(-200)
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            await jobs_funcs.start(self.context)
            await jobs_funcs.start(other_context)
        self.assertEqual(self.rows('SELECT chat_id FROM Matches ORDER BY chat_id'), [(10,), (20,)])
        other_context.bot.send_message.assert_awaited_once()
        other_context.bot.pin_chat_message.assert_awaited_once_with(chat_id=-200, message_id=55)

    async def test_existing_match_is_found_even_if_it_is_not_the_latest_row(self):
        create_match(local(2026, 10, 21, 19), 130, 10)
        create_match(local(2026, 10, 14, 19), 130, 10)
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            with patch.object(jobs_funcs, 'close_match_draft', new=AsyncMock()) as close:
                await jobs_funcs.start(self.context)
        self.assertEqual(len(self.rows('SELECT * FROM Matches')), 2)
        self.context.bot.send_message.assert_not_awaited()
        self.context.bot.pin_chat_message.assert_not_awaited()
        close.assert_not_awaited()

    async def test_simultaneous_manual_and_scheduled_start_publish_once(self):
        async def send_message(**kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(message_id=55)

        self.context.bot.send_message.side_effect = send_message
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 10, 19, 10)):
            await asyncio.gather(
                jobs_funcs.manual_start_registration(self.update, self.context),
                jobs_funcs.start(self.context),
            )
        self.assertEqual(len(self.rows('SELECT * FROM Matches')), 1)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.pin_chat_message.assert_awaited_once()

    def test_concurrent_database_connections_create_only_one_match(self):
        barrier = Barrier(2)

        def create():
            barrier.wait(timeout=5)
            return create_match_if_missing(local(2026, 10, 21, 19), 130, 10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(results.count(None), 1)
        self.assertEqual(len(self.rows('SELECT * FROM Matches')), 1)

    async def test_manual_start_still_requires_admin_and_configured_chat(self):
        self.admin.return_value = False
        await jobs_funcs.manual_start_registration(self.update, self.context)
        self.update.message.reply_text.assert_not_awaited()
        self.admin.return_value = True
        self.update.effective_chat.id = -300
        await jobs_funcs.manual_start_registration(self.update, self.context)
        self.assertIn('not configured', self.update.message.reply_text.await_args.args[0])
        self.assertEqual(self.rows('SELECT * FROM Matches'), [])
        self.context.bot.send_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
