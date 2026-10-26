from datetime import datetime, timedelta
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('MISTRAL_API_KEY', 'test-key')

import pytz
from telegram.ext import JobQueue

import date_utils
from date_utils import BOT_TIMEZONE, get_current_time, get_hours_until_match, get_next_weekday
import jobs_funcs
import main


def local(year, month, day, hour=0, minute=0, is_dst=False):
    return BOT_TIMEZONE.localize(datetime(year, month, day, hour, minute), is_dst=is_dst)


class CalendarDateTests(unittest.TestCase):
    def test_clock_uses_prague_timezone_and_the_current_instant(self):
        before = datetime.now(pytz.UTC)
        current = get_current_time()
        after = datetime.now(pytz.UTC)
        self.assertEqual(current.tzinfo.zone, 'Europe/Prague')
        self.assertLessEqual(before, current)
        self.assertLessEqual(current, after)

    def test_next_match_is_localized_for_its_own_date(self):
        cases = [
            (local(2026, 3, 25, 20), '2026-04-01T19:00:00+02:00'),
            (local(2026, 10, 21, 20), '2026-10-28T19:00:00+01:00'),
        ]
        for now, expected in cases:
            with self.subTest(expected=expected), patch.object(date_utils, 'get_current_time', return_value=now):
                self.assertEqual(get_next_weekday('wednesday', '19:00').isoformat(), expected)

    def test_same_day_before_and_at_time_and_calendar_rollover(self):
        for now, expected in [
            (local(2026, 10, 19, 9), '2026-10-19T10:00:00+02:00'),
            (local(2026, 10, 19, 10), '2026-10-26T10:00:00+01:00'),
            (local(2026, 12, 31, 20), '2027-01-07T19:00:00+01:00'),
        ]:
            weekday = 'Thursday' if now.month == 12 else 'Monday'
            clock = '19:00' if now.month == 12 else '10:00'
            with self.subTest(now=now), patch.object(date_utils, 'get_current_time', return_value=now):
                self.assertEqual(get_next_weekday(weekday, clock).isoformat(), expected)

    def test_hours_until_match_measure_elapsed_time_across_both_clock_changes(self):
        for now, match, expected in [
            (local(2026, 3, 28, 19), '2026-03-29 19:00:00+0200', 23),
            (local(2026, 10, 24, 19), '2026-10-25 19:00:00+0100', 25),
            (local(2026, 10, 25, 1, 30), '2026-10-25 03:30:00+0100', 3),
        ]:
            with self.subTest(match=match), patch.object(date_utils, 'get_current_time', return_value=now):
                self.assertEqual(get_hours_until_match(match), expected)

    def test_missing_spring_time_moves_forward_to_valid_local_time(self):
        with patch.object(date_utils, 'get_current_time', return_value=local(2026, 3, 28, 12)):
            self.assertEqual(get_next_weekday('sunday', '02:30').isoformat(), '2026-03-29T03:30:00+02:00')

    def test_repeated_autumn_time_uses_second_occurrence(self):
        # Even after 02:30 in the first occurrence, the second 02:30 is still ahead.
        now = local(2026, 10, 25, 2, 45, is_dst=True)
        with patch.object(date_utils, 'get_current_time', return_value=now):
            result = get_next_weekday('sunday', '02:30')
        self.assertEqual(result.isoformat(), '2026-10-25T02:30:00+01:00')
        self.assertEqual((result - now).total_seconds(), 45 * 60)


class CalendarTriggerTests(unittest.TestCase):
    def make_trigger(self, weekday='monday', clock='10:00'):
        queue = JobQueue()
        job = jobs_funcs.schedule_weekly_registration(queue, -100, weekday, clock)
        self.assertEqual(job.name, '-100')
        self.assertEqual(job.data, {'chat_id': -100})
        self.assertIs(job.callback, jobs_funcs.start)
        return job.job.trigger

    def test_real_job_queue_keeps_ten_am_across_spring_and_autumn(self):
        for before, expected, hours in [
            (local(2026, 3, 23, 10), '2026-03-30T10:00:00+02:00', 167),
            (local(2026, 10, 19, 10), '2026-10-26T10:00:00+01:00', 169),
        ]:
            with self.subTest(expected=expected):
                trigger = self.make_trigger()
                first = trigger.get_next_fire_time(None, before)
                next_fire = trigger.get_next_fire_time(first, first + timedelta(microseconds=1))
                self.assertEqual(next_fire.isoformat(), expected)
                self.assertEqual((next_fire - first).total_seconds(), hours * 3600)
                following = trigger.get_next_fire_time(next_fire, next_fire + timedelta(microseconds=1))
                self.assertEqual((following.hour, following.minute), (10, 0))
                self.assertEqual(following - next_fire, timedelta(days=7))

    def test_all_weekdays_map_correctly_to_telegram_job_queue(self):
        monday = local(2026, 10, 19)
        for weekday, name in enumerate(date_utils.WEEKDAYS):
            with self.subTest(day=name):
                fire = self.make_trigger(name, '19:15').get_next_fire_time(None, monday)
                self.assertEqual(fire.weekday(), weekday)
                self.assertEqual((fire.hour, fire.minute), (19, 15))
                self.assertEqual(fire.date(), (monday + timedelta(days=weekday)).date())

    def test_transition_hour_matches_next_match_policy_without_double_run(self):
        for before in (local(2026, 3, 28, 12), local(2026, 10, 24, 12)):
            with self.subTest(before=before):
                trigger = self.make_trigger('sunday', '02:30')
                fire = trigger.get_next_fire_time(None, before)
                with patch.object(date_utils, 'get_current_time', return_value=before):
                    expected = get_next_weekday('sunday', '02:30')
                self.assertEqual(fire.astimezone(BOT_TIMEZONE), expected)
                after = trigger.get_next_fire_time(fire, fire + timedelta(microseconds=1))
                self.assertEqual((after.date() - fire.date()).days, 7)

    def test_startup_restores_calendar_jobs_instead_of_intervals(self):
        queue = JobQueue()
        chats = [
            {'chat_id': '-100', 'reg_week_day': 'monday', 'reg_time': '10:00'},
            {'chat_id': '-200', 'reg_week_day': 'sunday', 'reg_time': '19:15'},
        ]
        with patch.object(main, 'get_all_chats', return_value=chats):
            main.initiate(SimpleNamespace(job_queue=queue))
        self.assertEqual({job.name for job in queue.jobs()}, {'-100', '-200'})
        now = local(2026, 10, 26)
        fire = queue.get_jobs_by_name('-100')[0].job.trigger.get_next_fire_time(None, now)
        self.assertEqual(fire.isoformat(), '2026-10-26T10:00:00+01:00')
        self.assertTrue(all(job.job.trigger.timezone.zone == 'Europe/Prague' for job in queue.jobs()))

    def test_invalid_schedule_does_not_add_any_job(self):
        queue = JobQueue()
        for weekday, clock in [('wrong', '10:00'), ('monday', '25:00'), ('sunday', '12:99')]:
            with self.subTest(day=weekday, clock=clock), self.assertRaises((KeyError, ValueError)):
                jobs_funcs.schedule_weekly_registration(queue, -100, weekday, clock)
        self.assertEqual(queue.jobs(), ())


class CalendarJobDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_jobs_displays_prague_time_even_when_scheduler_reports_utc(self):
        job = SimpleNamespace(next_t=pytz.UTC.localize(datetime(2026, 10, 26, 9)))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(job_queue=SimpleNamespace(jobs=lambda: [job]))
        await jobs_funcs.get_jobs(update, context)
        self.assertIn('2026-10-26 10:00:00+0100 Europe/Prague', update.message.reply_text.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
