"""Regression scenarios from the whole-bot review; temporary databases and mock API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_team_draft import DraftFixture
from date_utils import BOT_TIMEZONE
import bans
import jobs_funcs
import main
import matches
import pidor
import register_funcs
import remove_funcs
import users
import utils
from operations import common, team_drafts
from operations.chats import get_chat_by_tg_id, get_all_chats
from operations.match_registrations import (
    check_if_user_played_before, create_match_registration, delete_match_registration,
    get_current_match_registrations, set_match_registration_priority,
)
from operations.matches import (
    create_match_if_missing, delete_match, get_current_match, get_last_match,
    get_last_5_matches_with_players, was_in_last_match,
)
from operations.registration_safety import initialize_registration_ids
from operations.users import ActiveRegistrationsError, delete_user, get_user
from telegram.ext import Application, CallbackQueryHandler


class SafetyFixture(DraftFixture):
    def setUp(self):
        super().setUp()
        self.future = (datetime.now(BOT_TIMEZONE) + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S%z')
        self.sql('UPDATE Matches SET datetime=? WHERE match_id=1', (self.future,))

    def add_current(self):
        self.sql('INSERT INTO Matches (match_id,chat_id,datetime) VALUES (3,10,?)', (self.future,))

    def invitation(self, uid=3, inviter=1):
        return create_match_registration(uid, inviter, False, False, 2, 1)


class SafetyStorageTests(SafetyFixture, unittest.TestCase):
    def test_invitation_ids_survive_deleting_highest_row_and_database_reopen(self):
        old = self.invitation()
        delete_match_registration(1, 3)
        new = self.invitation(4)
        self.assertGreater(new, old)
        delete_match_registration(1, 4)
        initialize_registration_ids()
        self.assertGreater(self.invitation(), new)

    def test_legacy_ids_are_reserved_before_a_button_can_reference_them(self):
        self.sql('''INSERT INTO Match_Registration
            (registration_id,match_id,user_id,registered_by_id,confirmed,is_plus,priority)
            VALUES (400,1,3,1,0,0,2)''')
        get_current_match_registrations(10)
        delete_match_registration(1, 3)
        self.assertGreater(self.invitation(), 400)

    def test_concurrent_invitations_cannot_reuse_id_or_exceed_inviter_limit(self):
        initialize_registration_ids()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda uid: self.invitation(uid), (3, 4)))
        self.assertEqual(sum(bool(result) for result in results), 1)
        self.assertEqual(len(get_current_match_registrations(10)), 1)

    def test_additive_migration_on_copy_keeps_existing_rows_and_is_idempotent(self):
        self.register(1, 2, 3)
        tables = ('Users', 'Chats', 'Matches', 'Match_Registration')
        before = {table: self.sql(f'SELECT * FROM {table}') for table in tables}
        original = common.DB_PATH
        copied = str(Path(self.directory.name) / 'copy.db')
        shutil.copyfile(original, copied)
        with patch.object(common, 'DB_PATH', copied):
            initialize_registration_ids()
            initialize_registration_ids()
            self.assertEqual(before, {table: self.sql(f'SELECT * FROM {table}') for table in tables})
        self.assertEqual(before, {table: self.sql(f'SELECT * FROM {table}') for table in tables})

    def test_delete_account_requires_quitting_future_self_registration(self):
        self.register(1)
        with self.assertRaises(ActiveRegistrationsError):
            delete_user(1)
        self.assertIsNotNone(get_user(1))
        self.assertEqual(len(get_current_match_registrations(10)), 1)

    def test_delete_account_requires_removing_guests_even_without_self_registration(self):
        self.invitation()
        with self.assertRaises(ActiveRegistrationsError):
            delete_user(1)
        with self.assertRaises(ActiveRegistrationsError):
            delete_user(3)
        self.assertEqual([row['user_id'] for row in get_current_match_registrations(10)], [3])

    def test_deleted_historical_inviter_does_not_hide_guests_or_change_draft(self):
        self.register(1, 2, 3, 4)
        self.sql('UPDATE Match_Registration SET registered_by_id=1 WHERE user_id IN (3,4)')
        view = team_drafts.create_draft(1, 'manual', [2, 3])
        self.sql("UPDATE Matches SET datetime='2020-01-01 19:00:00+0100' WHERE match_id=1")
        delete_user(1)
        roster = get_current_match_registrations(10)
        self.assertEqual([row['user_id'] for row in roster], [1, 2, 3, 4])
        self.assertEqual(roster[0]['name'], 'Deleted account')
        synced = team_drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(len(synced['players']), 4)
        self.assertNotEqual(synced['draft']['status'], 'cancelled')
        self.add_current()
        self.assertEqual(len(get_last_match(10)), 4)

    def test_cancelled_match_is_not_current_and_does_not_resurrect_previous(self):
        self.register(1)
        self.add_current()
        self.register(2, match_id=3)
        delete_match(3)
        self.assertIsNone(get_current_match(10))
        self.assertEqual(get_current_match_registrations(10), [])
        self.assertEqual([row['user_id'] for row in get_last_match(10)], [1])
        self.assertEqual(self.sql('SELECT user_id FROM Match_Registration ORDER BY user_id'), [(1,), (2,)])
        self.assertFalse(set_match_registration_priority(10, 3, 2, 1))
        self.assertFalse(check_if_user_played_before(2))

    def test_cancelled_match_can_be_explicitly_reopened_with_a_new_id(self):
        self.register(1)
        delete_match(1)
        self.assertIsNone(create_match_if_missing(self.future, 130, 10))
        new_id = create_match_if_missing(self.future, 130, 10, reopen_cancelled=True)
        self.assertGreater(new_id, 1)
        self.assertEqual(get_current_match(10)['match_id'], new_id)
        self.assertEqual(get_current_match_registrations(10), [])
        self.assertIsNone(create_match_if_missing(self.future, 130, 10))

    def test_cancelled_match_draft_buttons_are_inert_and_account_can_be_deleted(self):
        self.register(1, 2, 3, 4)
        view = team_drafts.create_draft(1, 'manual', [1, 2])
        delete_match(1)
        self.assertIsNone(team_drafts.sync_draft(view['draft']['draft_id']))
        self.assertIsNone(team_drafts.pick_player(view['draft']['draft_id'], 1, 3, view['draft']['revision']))
        delete_user(1)
        self.assertIsNone(get_user(1))

    def test_history_and_priority_use_the_same_roster_order_and_exclude_reserve(self):
        self.register(*range(1, 17))
        # Registration IDs intentionally disagree with chronological registration order.
        for uid in range(1, 17):
            self.sql('UPDATE Match_Registration SET registered_at=? WHERE user_id=?',
                     (f'2020-01-01 10:00:{17-uid:02d}', uid))
        expected = [row['user_id'] for row in get_current_match_registrations(10)[:14]]
        self.add_current()
        self.assertEqual([row['user_id'] for row in get_last_match(10)], expected)
        for uid in range(1, 17):
            self.assertEqual(was_in_last_match(10, uid), uid in expected)
        self.assertEqual([row['user_id'] for row in get_last_5_matches_with_players(10)], expected)

    def test_history_keeps_only_five_non_cancelled_matches_with_fourteen_each(self):
        self.register(*range(1, 17))
        for mid in range(3, 9):
            self.sql('INSERT INTO Matches (match_id,chat_id,datetime) VALUES (?,10,?)',
                     (mid, f'2025-01-{mid:02d} 19:00:00+0100'))
            self.register(*range(1, 17), match_id=mid)
        delete_match(8)
        rows = get_last_5_matches_with_players(10)
        self.assertEqual(len(rows), 70)
        self.assertNotIn(8, {row['match_id'] for row in rows})
        self.assertEqual(len({row['match_id'] for row in rows}), 5)


class SafetyHandlerTests(SafetyFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.context = NS(bot=NS(send_message=AsyncMock(return_value=NS(message_id=50)),
            get_chat_member=AsyncMock(return_value=NS(status='member')),
            set_message_reaction=AsyncMock()), user_data={}, chat_data={}, args=[], job_queue=Mock())
        self.context.job_queue.jobs.return_value = []
        self.query = NS(from_user=NS(id=1, username='player1'), data='', answer=AsyncMock(), edit_message_text=AsyncMock())
        self.update = NS(callback_query=self.query, effective_chat=NS(id=-100, type='supergroup'),
            effective_user=NS(id=1), message=NS(id=100, reply_text=AsyncMock()), effective_message=NS(reply_text=AsyncMock()))

    def router(self):
        app = Application.builder().token('123456:test-token').build()
        builder = Mock()
        builder.token.return_value.build.return_value = app
        with patch.object(Application, 'builder', return_value=builder), patch.object(Application, 'run_polling'), patch.object(main, 'initiate'):
            main.main()
        return next(h.callback for h in app.handlers[0] if isinstance(h, CallbackQueryHandler))

    async def test_legacy_buttons_cannot_mutate_current_registration(self):
        self.register(1)
        self.invitation()
        router = self.router()
        for payload in ('register', 'registeranother', 'quit_-100', 'removefromdm_-100',
                        'removeplusone_-100', 'removeplusoneconfirm_-100_2', 'confirm_-100_1_2',
                        'registerplusone_-100_4'):
            before = self.sql('SELECT * FROM Match_Registration')
            self.query.data = payload
            await router(self.update, self.context)
            self.assertEqual(self.sql('SELECT * FROM Match_Registration'), before, payload)
        self.assertIn('old button', self.context.bot.send_message.await_args.kwargs['text'])

    async def test_old_group_buttons_and_private_menu_cannot_target_new_match(self):
        self.register(1)
        self.add_current()
        self.register(1, match_id=3)
        router = self.router()
        for payload in ('register_1', 'registeranother_1', 'quit_-100_1',
                        'removeplusone_-100_1', 'registerplusone_-100_4_1', 'leave_-100_1_1'):
            before = self.sql('SELECT * FROM Match_Registration')
            self.query.data = payload
            await router(self.update, self.context)
            self.assertEqual(self.sql('SELECT * FROM Match_Registration'), before, payload)

    async def test_replaced_invitation_rejects_old_confirm_and_remove_buttons(self):
        old = self.invitation()
        delete_match_registration(1, 3)
        new = self.invitation()
        self.query.from_user.id = 3
        await register_funcs.confirm(self.update, self.context, -100, 1, old)
        self.assertEqual(self.sql('SELECT confirmed FROM Match_Registration'), [(0,)])
        self.query.from_user.id = 1
        await remove_funcs.remove_plus_one(self.update, self.context, -100, old, 1)
        self.assertEqual(self.sql('SELECT registration_id FROM Match_Registration'), [(new,)])
        self.query.from_user.id = 3
        await register_funcs.confirm(self.update, self.context, -100, 1, new)
        self.assertEqual(self.sql('SELECT confirmed FROM Match_Registration'), [(1,)])

    async def test_new_private_invitation_buttons_route_confirm_and_quit(self):
        router = self.router()
        self.query.data = 'registerplusone_-100_3_1'
        await router(self.update, self.context)
        invitation = next(call.kwargs for call in self.context.bot.send_message.await_args_list
                          if call.kwargs['chat_id'] == 3)
        confirm_button, quit_button = [row[0] for row in invitation['reply_markup'].inline_keyboard]
        self.query.from_user.id = 3
        self.query.data = confirm_button.callback_data
        await router(self.update, self.context)
        self.assertEqual(self.sql('SELECT confirmed FROM Match_Registration WHERE user_id=3'), [(1,)])
        self.query.data = quit_button.callback_data
        await router(self.update, self.context)
        self.assertEqual(self.sql('SELECT * FROM Match_Registration'), [])

    async def test_schedule_opening_during_membership_check_does_not_invite_to_new_match(self):
        async def membership(*args):
            self.add_current()
            return NS(status='member')
        self.context.bot.get_chat_member.side_effect = membership
        self.query.data = 'registerplusone_-100_3_1'
        await self.router()(self.update, self.context)
        self.assertEqual(self.sql('SELECT * FROM Match_Registration'), [])

    async def test_new_group_register_and_invitation_removal_buttons_work(self):
        router = self.router()
        self.query.data = 'register_1'
        await router(self.update, self.context)
        self.assertEqual(self.sql('SELECT user_id FROM Match_Registration'), [(1,)])
        self.invitation()
        self.query.data = 'removeplusone_-100_1'
        await router(self.update, self.context)
        self.query.data = self.context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        await router(self.update, self.context)
        self.assertEqual(self.sql('SELECT user_id FROM Match_Registration'), [(1,)])

    async def test_quit_confirmation_binds_exact_registration_and_does_not_remove_replacement(self):
        old = create_match_registration(1, 1, False, True, 2, 1)
        await remove_funcs.start_quit(self.update, self.context, -100, 1)
        button = self.context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(button.callback_data, f'leave_-100_1_{old}')
        delete_match_registration(1, 1)
        new = create_match_registration(1, 1, False, True, 2, 1)
        self.query.data = button.callback_data
        await self.router()(self.update, self.context)
        self.assertEqual(self.sql('SELECT registration_id FROM Match_Registration'), [(new,)])

    async def test_refresh_upgrades_old_group_message_to_bound_buttons(self):
        self.query.data = 'refreshmessage'
        await self.router()(self.update, self.context)
        markup = self.query.edit_message_text.await_args.kwargs['reply_markup']
        payloads = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn('register_1', payloads)
        self.assertIn('quit_-100_1', payloads)
        self.assertTrue(all(len(payload.encode()) <= 64 for payload in payloads))

    async def test_delete_account_reports_active_registrations_without_hiding_guests(self):
        self.invitation()
        await users.delete_account(self.update, self.context)
        self.assertIsNotNone(get_user(1))
        self.assertEqual(len(get_current_match_registrations(10)), 1)
        self.assertIn('Use Quit', self.context.bot.send_message.await_args.kwargs['text'])

    async def test_pause_restart_and_resume_preserve_chat_match_and_draft(self):
        for col in ('game_time', 'game_week_day', 'reg_time', 'reg_week_day'):
            self.sql(f'ALTER TABLE Chats ADD COLUMN {col} TEXT')
        self.sql("UPDATE Chats SET reg_week_day='monday',reg_time='10:00'")
        self.register(1, 2, 3)
        draft_id = team_drafts.create_draft(1, 'manual', [1, 2])['draft']['draft_id']
        job = NS(name='-100', schedule_removal=Mock())
        self.context.job_queue.jobs.return_value = [job]
        with patch.object(jobs_funcs, 'is_chat_admin', AsyncMock(return_value=True)):
            await jobs_funcs.stop_repeating_job(self.update, self.context)
        job.schedule_removal.assert_called_once()
        queue = Mock()
        main.initiate(NS(job_queue=queue))
        self.assertFalse(any(call.args[0] == jobs_funcs.start and call.kwargs['data']['chat_id'] == '-100'
                             for call in queue.run_daily.call_args_list))
        self.assertEqual(get_chat_by_tg_id(-100)['id'], 10)
        self.assertEqual(team_drafts.get_active_draft(1)['draft_id'], draft_id)
        self.context.job_queue.jobs.return_value = []
        self.context.args = ['Football', 'monday', '10:00', 'wednesday', '19:00']
        with patch.object(jobs_funcs, 'is_chat_admin', AsyncMock(return_value=True)):
            await jobs_funcs.start_repeating_job(self.update, self.context)
        self.assertEqual(get_chat_by_tg_id(-100)['id'], 10)
        self.assertEqual(len(get_current_match_registrations(10)), 3)
        self.assertTrue(next(chat for chat in get_all_chats() if chat['id'] == 10)['schedule_enabled'])

    async def test_repeated_cancel_does_not_cancel_previous_match_and_closes_draft(self):
        self.register(1, 2, 3)
        self.add_current()
        self.register(1, 2, 3, match_id=3)
        did = team_drafts.create_draft(3, 'manual', [1, 2])['draft']['draft_id']
        self.context.bot.delete_message = AsyncMock()
        with patch.object(matches, 'is_chat_admin', AsyncMock(return_value=True)):
            await matches.cancel_match(self.update, self.context)
            await matches.cancel_match(self.update, self.context)
        self.assertIsNone(get_current_match(10))
        self.assertIsNone(team_drafts.get_active_draft(1))
        self.assertEqual(team_drafts.get_draft(did)['draft']['status'], 'cancelled')
        self.assertEqual(self.context.bot.set_message_reaction.await_count, 1)
        self.assertEqual(self.sql('SELECT match_id FROM Cancelled_Matches'), [(3,)])

    async def test_schedule_does_not_reopen_cancelled_game_but_manual_open_does(self):
        match_time = datetime.fromisoformat(self.future)
        self.sql('UPDATE Matches SET datetime=? WHERE match_id=1', (str(match_time),))
        delete_match(1)
        chat = {'id': 10, 'game_week_day': 'wednesday', 'game_time': '19:00'}
        self.context.bot.pin_chat_message = AsyncMock()
        with patch.object(jobs_funcs, 'get_next_weekday', return_value=match_time):
            self.assertFalse(await jobs_funcs._open_registration(self.context, -100, chat))
            self.context.bot.send_message.assert_not_awaited()
            self.assertTrue(await jobs_funcs._open_registration(self.context, -100, chat, reopen_cancelled=True))
        self.assertNotEqual(get_current_match(10)['match_id'], 1)

    async def test_html_escapes_names_and_nicknames_in_registration_history_bans_and_stats(self):
        self.sql("UPDATE Users SET nickname='a<b>&',name='Player <striker> &' WHERE user_id=1")
        self.register(1, 2)
        self.sql('UPDATE Match_Registration SET is_plus=1,registered_by_id=1 WHERE user_id=2')
        self.assertIn('Player &lt;striker&gt; &amp;', utils.registered(get_current_match_registrations(10)))
        self.assertIn('from a&lt;b&gt;&amp;', utils.registered(get_current_match_registrations(10)))
        self.add_current()
        await utils.last_match(self.update, self.context)
        self.assertIn('Player &lt;striker&gt; &amp;', self.update.message.reply_text.await_args.args[0])
        await utils.last_5_matches_players(self.update, self.context)
        self.assertIn('a&lt;b&gt;&amp;', self.update.message.reply_text.await_args.args[0])
        self.sql("INSERT INTO Bans VALUES (1,1,'2099-01-01')")
        await bans.get_all_bans_command(self.update, self.context)
        self.assertIn('Player &lt;striker&gt; &amp;', self.context.bot.send_message.await_args.kwargs['text'])
        pidor.create_random_selection_table()
        pidor.record_selection(1, -100)
        await pidor.get_random_stats(self.update, self.context)
        self.assertIn('Player &lt;striker&gt; &amp;', self.update.message.reply_text.await_args.args[0])
        with patch.object(pidor, 'get_last_selection', return_value={'user_id': 1}):
            await pidor.pick_random_user(self.update, self.context)
        self.assertIn('a&lt;b&gt;&amp;', self.update.message.reply_text.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
