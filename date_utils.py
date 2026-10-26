from datetime import datetime, timedelta
import pytz

from constants import DATETIME_FORMAT

BOT_TIMEZONE = pytz.timezone('Europe/Prague')
WEEKDAYS = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}


def parse_weekday_time(weekday_name, time_str):
    """Return a Monday-based weekday and a wall-clock time, without a UTC offset."""
    return WEEKDAYS[weekday_name.lower()], datetime.strptime(time_str, '%H:%M').time()


def get_current_time():
    """Use the bot's time zone even when the server itself runs in UTC."""
    return datetime.now(BOT_TIMEZONE)


def get_next_weekday(weekday_name, time_str):
    """Next future occurrence, localized for the target date, not today's offset.

    Match APScheduler/pytz's calendar policy for the transition hour: a missing
    spring time moves forward by the clock change; a repeated autumn time uses
    the standard-time (second) occurrence.
    """
    weekday, target_time = parse_weekday_time(weekday_name, time_str)
    now = get_current_time()
    target_date = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
    candidate = BOT_TIMEZONE.normalize(BOT_TIMEZONE.localize(
        datetime.combine(target_date, target_time), is_dst=False))
    if candidate <= now:
        target_date += timedelta(days=7)
        candidate = BOT_TIMEZONE.normalize(BOT_TIMEZONE.localize(
            datetime.combine(target_date, target_time), is_dst=False))
    return candidate


def get_hours_until_match(game_time):
    datetime_parsed = datetime.strptime(game_time, DATETIME_FORMAT)
    return (datetime_parsed - get_current_time()).total_seconds() / 3600
