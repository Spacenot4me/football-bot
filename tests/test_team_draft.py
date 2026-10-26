from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('MISTRAL_API_KEY', 'test-key')

from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CallbackQueryHandler

import draft_funcs
import main
import register_funcs
import remove_funcs
from operations import common, team_drafts as drafts


class DraftFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_db_path = common.DB_PATH
        common.DB_PATH = str(Path(self.directory.name) / 'test.db')
        self.sql('''
            CREATE TABLE Users (user_id INTEGER PRIMARY KEY, nickname TEXT UNIQUE, name TEXT);
            CREATE TABLE Chats (id INTEGER PRIMARY KEY, chat_id TEXT, name TEXT);
            CREATE TABLE Matches (match_id INTEGER PRIMARY KEY, chat_id INTEGER, datetime TEXT,
                amount_per_person INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE Match_Registration (registration_id INTEGER PRIMARY KEY,
                match_id INTEGER, user_id INTEGER, registered_by_id INTEGER,
                confirmed INTEGER, is_plus INTEGER, priority INTEGER,
                registered_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE Bans (ban_id INTEGER PRIMARY KEY, user_id INTEGER, until TEXT);
            INSERT INTO Chats VALUES (10, '-100', 'Football');
            INSERT INTO Chats VALUES (11, '-200', 'Other chat');
            INSERT INTO Matches (match_id, chat_id, datetime) VALUES
                (1,10,'2020-01-01 19:00:00+0100'), (2,11,'2030-01-01 19:00:00+0100');
        ''', script=True)
        for uid in range(1, 31):
            self.sql('INSERT INTO Users VALUES (?, ?, ?)', (uid, f'player{uid}', f'Player {uid}'))
        self.sql("INSERT INTO Users VALUES (99, 'admin', 'Admin')")

    def tearDown(self):
        common.DB_PATH = self.previous_db_path
        self.directory.cleanup()

    def sql(self, statement, params=(), script=False):
        with closing(sqlite3.connect(common.DB_PATH)) as conn, conn:
            if script:
                conn.executescript(statement)
                return []
            return conn.execute(statement, params).fetchall()

    def register(self, *ids, confirmed=1, match_id=1):
        for uid in ids:
            self.sql('''INSERT INTO Match_Registration
                (match_id,user_id,registered_by_id,confirmed,is_plus,priority)
                VALUES (?,?,?, ?,0,2)''', (match_id, uid, uid, confirmed))

    def create(self, count=0, mode='manual', candidates=(1, 2)):
        self.register(*range(1, count + 1))
        with patch.object(drafts.random, 'choice', return_value='A'):
            return drafts.create_draft(1, mode, candidates if mode != 'random_all' else ())

    def pick(self, view, actor=None, uid=None):
        d = view['draft']
        actor = actor or d['captain_' + view['turn'].lower() + '_id']
        uid = uid or view['free'][0]['user_id']
        return drafts.pick_player(d['draft_id'], actor, uid, d['revision'])

    def finish(self, view):
        while view['draft']['status'] == 'drafting':
            view = self.pick(view)
            self.assertIsNone(view['error'])
        return view


class DraftStorageTests(DraftFixture, unittest.TestCase):
    def test_start_zero_one_and_resume_after_database_reopen(self):
        view = self.create(0, mode='random_all')
        draft_id = view['draft']['draft_id']
        self.assertEqual(view['draft']['status'], 'waiting_for_players')
        self.register(1, confirmed=0)
        view = drafts.sync_draft(draft_id)
        self.assertEqual(view['draft']['status'], 'waiting_for_players')
        self.assertIsNone(view['draft']['captain_a_id'])
        self.register(2)
        view = drafts.sync_draft(draft_id)
        self.assertEqual(view['draft']['status'], 'done')
        self.assertEqual({view['draft']['captain_a_id'], view['draft']['captain_b_id']}, {1, 2})

    def test_manual_candidates_can_wait_outside_match(self):
        view = self.create(1, candidates=(1, 3))
        self.assertEqual(view['draft']['status'], 'waiting_for_players')
        self.register(2)
        self.assertEqual(drafts.sync_draft(view['draft']['draft_id'])['draft']['status'], 'waiting_for_players')
        self.register(3)
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual((view['draft']['captain_a_id'], view['draft']['captain_b_id']), (1, 3))

    def test_random_list_uses_only_available_candidates(self):
        view = self.create(3, mode='random_list', candidates=(2, 4, 5))
        self.assertEqual(view['draft']['status'], 'waiting_for_players')
        self.register(4)
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual({view['draft']['captain_a_id'], view['draft']['captain_b_id']}, {2, 4})

    def test_even_odd_and_more_than_fourteen(self):
        for count in (2, 3, 5, 10, 14, 17):
            with self.subTest(count=count):
                self.sql('DELETE FROM Match_Registration')
                view = self.finish(self.create(count))
                self.assertEqual(view['draft']['status'], 'done')
                ids = {p['user_id'] for p in view['picks']} | {1, 2}
                self.assertEqual(ids, set(range(1, min(count, 14) + 1)))
                sizes = [1 + sum(p['team'] == t for p in view['picks']) for t in ('A', 'B')]
                self.assertLessEqual(abs(sizes[0] - sizes[1]), 1)
                drafts.cancel_draft(view['draft']['draft_id'])

    def test_wrong_captain_and_duplicate_stale_picks_do_not_change_teams(self):
        view = self.create(8)
        rejected = self.pick(view, actor=2)
        self.assertIsNotNone(rejected['error'])
        self.assertEqual(rejected['picks'], [])
        after = self.pick(view)
        stale = self.pick(view)
        self.assertIn('outdated', stale['error'])
        self.assertEqual(stale['picks'], after['picks'])
        outsider = drafts.pick_player(view['draft']['draft_id'], 99, 4, after['draft']['revision'])
        self.assertIsNotNone(outsider['error'])

    def test_last_ordinary_player_is_picked_automatically(self):
        view = self.pick(self.create(4))
        self.assertEqual(view['draft']['status'], 'done')
        self.assertEqual({p['user_id'] for p in view['picks']}, {3, 4})
        self.assertEqual(view['draft']['draft_pick_count'], 2)

    def test_additional_registration_continues_completed_draft(self):
        view = self.finish(self.create(10))
        self.register(11)
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(view['draft']['status'], 'drafting')
        self.assertEqual([p['user_id'] for p in view['free']], [11])
        view = self.pick(view)
        self.assertEqual(view['draft']['status'], 'done')

    def test_only_affected_captain_can_pick_replacement(self):
        view = self.finish(self.create(15))
        departed = next(p for p in view['picks'] if p['team'] == 'B')
        self.sql('DELETE FROM Match_Registration WHERE user_id = ?', (departed['user_id'],))
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(view['draft']['status'], 'needs_replacement')
        self.assertEqual([p['user_id'] for p in view['free']], [15])
        bad = self.pick(view, actor=1)
        self.assertIsNotNone(bad['error'])
        count = view['draft']['draft_pick_count']
        view = self.pick(view, actor=2)
        self.assertEqual(view['draft']['status'], 'done')
        self.assertEqual(view['draft']['draft_pick_count'], count)
        self.assertEqual(next(p['kind'] for p in view['picks'] if p['user_id'] == 15), 'replacement')

    def test_captain_replaced_by_own_team_without_resetting_turn_or_other_team(self):
        view = self.finish(self.create(15))
        teammate_ids = {p['user_id'] for p in view['picks'] if p['team'] == 'A'}
        other = [p for p in view['picks'] if p['team'] == 'B']
        count = view['draft']['draft_pick_count']
        self.sql('DELETE FROM Match_Registration WHERE user_id = 1')
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertIn(view['draft']['captain_a_id'], teammate_ids)
        self.assertEqual(view['draft']['captain_b_id'], 2)
        self.assertEqual([p for p in view['picks'] if p['team'] == 'B'], other)
        self.assertEqual(view['draft']['draft_pick_count'], count)
        self.assertEqual(view['draft']['replacement_a'], 1)
        self.assertNotIn(view['draft']['captain_a_id'], {p['user_id'] for p in view['picks']})

    def test_one_teammate_becomes_captain_and_empty_team_cancels(self):
        view = self.finish(self.create(4))
        teammate = next(p['user_id'] for p in view['picks'] if p['team'] == 'A')
        self.sql('DELETE FROM Match_Registration WHERE user_id = 1')
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(view['draft']['captain_a_id'], teammate)
        self.sql('DELETE FROM Match_Registration WHERE user_id = ?', (teammate,))
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(view['draft']['status'], 'cancelled')
        self.assertEqual(view['picks'], [])

    def test_departure_from_reserve_invalidates_request_without_changing_picks(self):
        view = self.finish(self.create(16))
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 1, False)
        self.sql('DELETE FROM Match_Registration WHERE user_id = 16')
        synced = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(view['picks'], synced['picks'])
        self.assertEqual(synced['events'][0]['player']['user_id'], 16)
        self.assertEqual(drafts.get_restart_request(request_id)['status'], 'stale')

    def test_restart_requires_owner_confirmation_and_rechecks_role(self):
        view = self.pick(self.create(8))
        denied, request_id = drafts.request_restart(view['draft']['draft_id'], 3, False)
        self.assertIsNone(request_id)
        self.assertIsNotNone(denied['error'])
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 99, True)
        _, result = drafts.finish_restart(request_id, 1, False)
        self.assertIn('person who requested', result)
        current, result = drafts.finish_restart(request_id, 99, False)
        self.assertIn('Only chat admins', result)
        self.assertEqual(current['picks'], view['picks'])
        current, result = drafts.finish_restart(request_id, 99, True)
        self.assertEqual(result, 'Draft restarted.')
        self.assertEqual(current['picks'], [])
        self.assertEqual((current['draft']['captain_a_id'], current['draft']['captain_b_id']), (1, 2))
        revision = current['draft']['revision']
        current, result = drafts.finish_restart(request_id, 99, True)
        self.assertIn('no longer valid', result)
        self.assertEqual(current['draft']['revision'], revision)

    def test_cancel_request_preserves_teams_and_turn(self):
        view = self.pick(self.create(8))
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 2, False)
        after, result = drafts.finish_restart(request_id, 2, cancel=True)
        self.assertEqual(result, 'Restart cancelled.')
        self.assertEqual(after['picks'], view['picks'])
        self.assertEqual(after['draft']['revision'], view['draft']['revision'])

    def test_stale_restart_after_pick_and_old_pick_after_restart(self):
        view = self.create(8)
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 1, False)
        after = self.pick(view)
        current, result = drafts.finish_restart(request_id, 1)
        self.assertIn('no longer valid', result)
        self.assertEqual(current['picks'], after['picks'])
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 1, False)
        current, result = drafts.finish_restart(request_id, 1)
        self.assertEqual(result, 'Draft restarted.')
        stale = self.pick(view)
        self.assertIn('outdated', stale['error'])
        self.assertEqual(stale['picks'], [])

    def test_parallel_confirms_execute_only_once(self):
        view = self.pick(self.create(8))
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 1, False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: drafts.finish_restart(request_id, 1), range(2)))
        self.assertEqual(sum(result == 'Draft restarted.' for _, result in results), 1)

    def test_waiting_restart_and_cancelled_or_old_match_callbacks(self):
        view = self.create(0, mode='random_all')
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 99, True)
        view, result = drafts.finish_restart(request_id, 99, True)
        self.assertEqual(view['draft']['status'], 'waiting_for_players')
        self.assertEqual(result, 'Draft restarted.')
        self.sql("INSERT INTO Matches (match_id,chat_id,datetime) VALUES (3,10,'2030-01-01')")
        self.assertIsNone(drafts.sync_draft(view['draft']['draft_id']))
        self.assertIsNone(drafts.pick_player(view['draft']['draft_id'], 1, 3, view['draft']['revision']))

    def test_second_start_does_not_reset_existing_draft(self):
        view = self.pick(self.create(8))
        other = drafts.create_draft(1)
        self.assertEqual(other['picks'], view['picks'])
        self.assertIsNotNone(other['error'])

    def test_both_teams_can_replace_their_own_missing_players(self):
        view = self.finish(self.create(16))
        departed = {team: next(p['user_id'] for p in view['picks'] if p['team'] == team)
                    for team in ('A', 'B')}
        self.sql('DELETE FROM Match_Registration WHERE user_id IN (?, ?)', tuple(departed.values()))
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual((view['draft']['replacement_a'], view['draft']['replacement_b']), (1, 1))
        view = self.pick(view, actor=2)
        self.assertEqual(view['draft']['status'], 'needs_replacement')
        view = self.pick(view, actor=1)
        self.assertEqual(view['draft']['status'], 'done')
        self.assertEqual(len(view['picks']), 12)

    def test_main_roster_change_cannot_leave_a_reserve_in_a_team(self):
        view = self.finish(self.create(15))
        self.sql('UPDATE Match_Registration SET priority=1 WHERE user_id=15')
        view = drafts.sync_draft(view['draft']['draft_id'])
        self.assertEqual(view['draft']['status'], 'needs_replacement')
        assigned = {p['user_id'] for p in view['picks']} | {1, 2}
        self.assertNotIn(14, assigned)
        self.assertIn(15, {p['user_id'] for p in view['free']})


class DraftTelegramTests(DraftFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        sequence = iter(range(500, 10000))

        async def send(**kwargs):
            return SimpleNamespace(message_id=next(sequence))

        self.context = SimpleNamespace(args=[], bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=send),
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
            delete_message=AsyncMock(), pin_chat_message=AsyncMock(),
            get_chat_member=AsyncMock(side_effect=lambda chat, uid: SimpleNamespace(
                status='administrator' if uid == 99 else 'member')),
            set_message_reaction=AsyncMock(),
        ))
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=-100),
            effective_user=SimpleNamespace(id=99), effective_message=SimpleNamespace(message_id=100),
            callback_query=None)

    async def callback(self, data, uid=1, chat_id=-100):
        self.update.effective_chat.id = chat_id
        self.update.callback_query = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid),
            answer=AsyncMock(), edit_message_text=AsyncMock())
        await draft_funcs.draft_callback(self.update, self.context)

    async def test_start_zero_pins_with_sound_and_render_is_persistent(self):
        await draft_funcs.captains(self.update, self.context)
        d = drafts.get_active_draft(1)
        self.assertEqual(d['status'], 'waiting_for_players')
        self.assertIsNotNone(d['main_message_id'])
        self.assertFalse(self.context.bot.pin_chat_message.await_args.kwargs['disable_notification'])
        text = self.context.bot.send_message.await_args.kwargs['text']
        self.assertIn('Registered players: 0', text)
        view = drafts.sync_draft(d['draft_id'])
        _, markup = draft_funcs.render_draft(view)
        self.assertEqual(markup.inline_keyboard[-1][0].text, '🔁 Restart draft')

    async def test_non_admin_cannot_start_and_candidates_validated(self):
        self.update.effective_user.id = 3
        await draft_funcs.captains(self.update, self.context)
        self.assertIsNone(drafts.get_active_draft(1))
        self.update.effective_user.id = 99
        for args in (['@player1'], ['@unknown', '@player2'], ['@player1', '@PLAYER1']):
            self.context.args = args
            await draft_funcs.captains(self.update, self.context)
            self.assertIsNone(drafts.get_active_draft(1))

    async def test_permission_callback_and_request_confirmation_are_separate(self):
        view = self.create(8)
        did = view['draft']['draft_id']
        await draft_funcs.teams(self.update, self.context)
        await self.callback(f'draftrestart_{did}', uid=3)
        self.assertIn('Only chat admins', self.context.bot.send_message.await_args.kwargs['text'])
        await self.callback(f'draftrestart_{did}', uid=1)
        message = self.context.bot.send_message.await_args.kwargs
        self.assertTrue(message['disable_notification'])
        buttons = message['reply_markup'].inline_keyboard[0]
        self.assertEqual([b.text for b in buttons], ['✅ Confirm restart', 'Cancel'])
        request_id = int(buttons[0].callback_data.split('_')[1])
        await self.callback(buttons[0].callback_data, uid=2)
        self.update.callback_query.edit_message_text.assert_not_awaited()
        self.assertEqual(drafts.get_restart_request(request_id)['status'], 'pending')
        await self.callback(buttons[0].callback_data, uid=1)
        self.update.callback_query.edit_message_text.assert_awaited_once_with(text='Draft restarted.', reply_markup=None)

    async def test_cross_chat_and_current_match_validation(self):
        view = self.create(8)
        await self.callback(f"draftpick_{view['draft']['draft_id']}_3_{view['draft']['revision']}", chat_id=-200)
        self.assertEqual(drafts.sync_draft(view['draft']['draft_id'])['picks'], [])
        self.assertIn('does not belong', self.context.bot.send_message.await_args.kwargs['text'])

    async def test_exit_notification_goes_to_group_with_sound_once(self):
        view = self.finish(self.create(15))
        did = view['draft']['draft_id']
        self.sql('DELETE FROM Match_Registration WHERE user_id = 15')
        notified = await draft_funcs.on_player_removed(self.context, -100, 1, 15)
        self.assertTrue(notified)
        sent = self.context.bot.send_message.await_args.kwargs
        self.assertEqual(str(sent['chat_id']), '-100')
        self.assertFalse(sent['disable_notification'])
        self.assertIn('@player15', sent['text'])
        count = self.context.bot.send_message.await_count
        await draft_funcs.on_player_removed(self.context, -100, 1, 15)
        self.assertEqual(self.context.bot.send_message.await_count, count)
        self.assertEqual(drafts.get_active_draft(1)['draft_id'], did)

    async def test_pin_failure_keeps_draft_and_message(self):
        self.context.bot.pin_chat_message.side_effect = Forbidden('No pin rights')
        with self.assertLogs(draft_funcs.logger, level='WARNING'):
            await draft_funcs.captains(self.update, self.context)
        self.assertIsNotNone(drafts.get_active_draft(1)['main_message_id'])

    async def test_teams_replaces_old_message_and_cancel_cleans_every_message(self):
        await draft_funcs.captains(self.update, self.context)
        old = drafts.get_active_draft(1)['main_message_id']
        await draft_funcs.teams(self.update, self.context)
        d = drafts.get_active_draft(1)
        self.assertNotEqual(old, d['main_message_id'])
        self.assertIn(old, [call.kwargs['message_id'] for call in self.context.bot.delete_message.await_args_list])
        await self.callback(f"draftrestart_{d['draft_id']}", uid=99)
        self.update.callback_query = None
        await draft_funcs.cancel_team_draft(self.update, self.context)
        self.assertIsNone(drafts.get_active_draft(1))
        self.assertEqual(drafts.get_messages(d['draft_id']), [])

    async def test_deleted_main_message_is_recreated_but_unchanged_is_not(self):
        await draft_funcs.captains(self.update, self.context)
        d = drafts.get_active_draft(1)
        self.context.bot.edit_message_text.side_effect = BadRequest('Message is not modified')
        count = self.context.bot.send_message.await_count
        await self.callback(f"draftrefresh_{d['draft_id']}")
        self.assertEqual(self.context.bot.send_message.await_count, count)
        self.context.bot.edit_message_text.side_effect = BadRequest('Message to edit not found')
        await self.callback(f"draftrefresh_{d['draft_id']}")
        self.assertEqual(self.context.bot.send_message.await_count, count + 1)

    async def test_render_escapes_user_fields_and_callback_has_revision(self):
        view = self.create(8)
        self.sql("UPDATE Users SET name='<script>&' WHERE user_id=1")
        view = drafts.sync_draft(view['draft']['draft_id'])
        text, markup = draft_funcs.render_draft(view)
        self.assertIn('&lt;script&gt;&amp;', text)
        self.assertNotIn('<script>', text)
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)
        self.assertEqual(len(markup.inline_keyboard[0][0].callback_data.split('_')), 4)

    async def test_timeout_updates_draft_and_notifies_group(self):
        view = self.create(4)
        self.sql('UPDATE Match_Registration SET confirmed=0 WHERE user_id=3')
        drafts.sync_draft(view['draft']['draft_id'])
        self.context.job = SimpleNamespace(data={'chat_id': -100, 'match_id': 1, 'user_id': 3})
        await register_funcs.check_for_confimation(self.context)
        self.assertEqual(self.sql('SELECT user_id FROM Match_Registration WHERE user_id=3'), [])
        self.assertEqual(self.context.bot.send_message.await_count, 2)  # main message + one notification
        self.assertFalse(self.context.bot.send_message.await_args.kwargs['disable_notification'])

    async def test_main_routes_new_commands_and_pick_callback(self):
        view = self.create(8)
        app = Application.builder().token('123456:test-token').build()
        builder = Mock()
        builder.token.return_value.build.return_value = app
        with patch.object(Application, 'builder', return_value=builder), patch.object(Application, 'run_polling'), patch.object(main, 'initiate'):
            main.main()
        commands = {command for handler in app.handlers[0] for command in getattr(handler, 'commands', [])}
        self.assertTrue({'captains', 'capitans', 'teams', 'cancel_draft'} <= commands)
        handler = next(h for h in app.handlers[0] if isinstance(h, CallbackQueryHandler))
        self.update.callback_query = SimpleNamespace(
            from_user=SimpleNamespace(id=1), answer=AsyncMock(),
            data=f"draftpick_{view['draft']['draft_id']}_3_{view['draft']['revision']}")
        await handler.callback(self.update, self.context)
        self.assertEqual(drafts.sync_draft(view['draft']['draft_id'])['picks'][0]['user_id'], 3)
        self.update.callback_query.answer.assert_awaited_once()

    async def test_self_registration_resumes_waiting_session(self):
        view = self.create(0, mode='random_all')
        for uid in (1, 2):
            self.update.callback_query = SimpleNamespace(
                from_user=SimpleNamespace(id=uid, username=f'player{uid}'), edit_message_text=AsyncMock())
            with patch.object(register_funcs, 'get_hours_until_match', return_value=48), \
                    patch.object(register_funcs, 'get_priority', return_value=2):
                await register_funcs.register_himself(self.update, self.context)
            expected = 'waiting_for_players' if uid == 1 else 'done'
            self.assertEqual(drafts.sync_draft(view['draft']['draft_id'])['draft']['status'], expected)

    async def test_quit_hook_updates_teams_and_notifies_only_the_group(self):
        view = self.finish(self.create(8))
        self.update.callback_query = SimpleNamespace(from_user=SimpleNamespace(id=3), edit_message_text=AsyncMock())
        with patch.object(remove_funcs, 'get_hours_until_match', return_value=48):
            await remove_funcs.remove_from_dm(self.update, self.context, -100)
        after = drafts.sync_draft(view['draft']['draft_id'])
        self.assertNotIn(3, {p['user_id'] for p in after['picks']})
        self.assertEqual(after['draft']['status'], 'needs_replacement')
        notices = [c.kwargs for c in self.context.bot.send_message.await_args_list
                   if c.kwargs.get('disable_notification') is False]
        self.assertEqual(len(notices), 1)
        self.assertEqual(str(notices[0]['chat_id']), '-100')

    async def test_inviter_and_admin_removal_hooks_use_the_removed_player(self):
        for method in ('remove_plus_one', 'remove_other', 'remove_other_plus_one'):
            with self.subTest(method=method):
                self.sql('DELETE FROM Match_Registration')
                view = self.finish(self.create(8))
                did = view['draft']['draft_id']
                self.sql('UPDATE Match_Registration SET registered_by_id=1,is_plus=1 WHERE user_id=3')
                drafts.sync_draft(did)
                self.context.bot.send_message.reset_mock()
                self.update.callback_query = SimpleNamespace(from_user=SimpleNamespace(id=1), edit_message_text=AsyncMock())
                self.update.message = SimpleNamespace(id=100, reply_text=AsyncMock())
                self.context.args = ['@player3'] if method == 'remove_other' else ['@player1']
                with patch.object(remove_funcs, 'get_hours_until_match', return_value=48), \
                        patch.object(remove_funcs, 'is_chat_admin', new=AsyncMock(return_value=True)):
                    if method == 'remove_plus_one':
                        rid = self.sql('SELECT registration_id FROM Match_Registration WHERE user_id=3')[0][0]
                        await remove_funcs.remove_plus_one(self.update, self.context, -100, rid)
                    else:
                        await getattr(remove_funcs, method)(self.update, self.context)
                after = drafts.sync_draft(did)
                self.assertNotIn(3, {p['user_id'] for p in after['picks']})
                notices = [c.kwargs for c in self.context.bot.send_message.await_args_list
                           if c.kwargs.get('disable_notification') is False]
                self.assertEqual(len(notices), 1)
                self.assertIn('@player3', notices[0]['text'])
                drafts.cancel_draft(did)

    async def test_delivery_failure_does_not_undo_captain_replacement(self):
        view = self.finish(self.create(8))
        self.sql('DELETE FROM Match_Registration WHERE user_id=1')
        self.context.bot.send_message.side_effect = Forbidden('No message permission')
        with self.assertLogs(draft_funcs.logger, level='WARNING'):
            await draft_funcs.on_player_removed(self.context, -100, 1, 1)
        after = drafts.sync_draft(view['draft']['draft_id'])
        self.assertNotEqual(after['draft']['captain_a_id'], 1)
        self.assertEqual(after['draft']['status'], 'needs_replacement')

    async def test_match_close_cleans_keyboard_and_invalidates_confirmation(self):
        view = self.create(8)
        _, request_id = drafts.request_restart(view['draft']['draft_id'], 1, False)
        await draft_funcs.teams(self.update, self.context)
        await draft_funcs.close_match_draft(self.context, -100, 1)
        self.assertIsNone(drafts.get_active_draft(1))
        self.assertEqual(drafts.get_restart_request(request_id)['status'], 'stale')
        self.assertEqual(drafts.get_messages(view['draft']['draft_id']), [])


if __name__ == '__main__':
    unittest.main()
