from operations.chats import create_chat, delete_chat, get_chat_by_tg_id
from operations.matches import create_match, get_match_by_datetime, get_current_match
from draft_funcs import close_match_draft
from utils import get_reply_markup, is_chat_admin
from date_utils import BOT_TIMEZONE, get_next_weekday, parse_weekday_time
from constants import  DATETIME_FORMAT

from telegram import Update
from telegram.ext import CallbackContext

async def get_jobs(update: Update, context: CallbackContext):
    jobs = context.job_queue.jobs()

    job_info = ""
    for job in jobs:
        job_info += repr(job)
        job_info += job.next_t.astimezone(BOT_TIMEZONE).strftime(DATETIME_FORMAT) + " Europe/Prague\n"

    await update.message.reply_text("Current jobs: \n" + job_info)


def schedule_weekly_registration(job_queue, tg_chat_id, weekday_name, time_str):
    """Calendar-based weekly registration, shared by setup and startup restore."""
    weekday, clock_time = parse_weekday_time(weekday_name, time_str)
    return job_queue.run_daily(
        start,
        time=clock_time.replace(tzinfo=BOT_TIMEZONE),
        # PTB 21 uses Sunday=0, while datetime.weekday() uses Monday=0.
        days=((weekday + 1) % 7,),
        data={'chat_id': tg_chat_id},
        name=str(tg_chat_id),
        job_kwargs={'coalesce': True, 'max_instances': 1},
    )

async def start(context: CallbackContext):
    job_data = context.job.data
    tg_chat_id = job_data['chat_id']

    chat_data = get_chat_by_tg_id(tg_chat_id)

    previous_match = get_current_match(chat_data['id'])
    if previous_match:
        await close_match_draft(context, tg_chat_id, previous_match['match_id'])

    next_match_datetime = get_next_weekday(chat_data['game_week_day'], chat_data['game_time'])
    create_match(next_match_datetime, 130, chat_data['id'])

    formatted_game_time = next_match_datetime.strftime("%d.%m.%Y %H:%M")

    sent_message = await context.bot.send_message(chat_id=tg_chat_id, text=f"Registration is now open! \nGame time: {formatted_game_time}", reply_markup=get_reply_markup(tg_chat_id))
    await context.bot.pin_chat_message(chat_id=tg_chat_id, message_id=sent_message.message_id)

async def start_repeating_job(update: Update, context: CallbackContext):
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id

    jobs = context.job_queue.jobs()

    job_exists = any(str(job.name) == str(tg_chat_id) for job in jobs)

    if job_exists:
        await update.message.reply_text('The bot is already running in this chat.')
        return

    if len(context.args or []) != 5:
        await update.message.reply_text(
            "Usage: /start_repeating_job NAME REG_DAY REG_TIME GAME_DAY GAME_TIME\n"
            "Example: /start_repeating_job Football monday 10:00 wednesday 19:00"
        )
        return

    chat_name, reg_week_day, reg_time, game_week_day, game_time = context.args

    try:
        parse_weekday_time(reg_week_day, reg_time)
        parse_weekday_time(game_week_day, game_time)
    except (KeyError, ValueError):
        await update.message.reply_text(
            "Use English weekdays (monday, tuesday, wednesday, thursday, friday, saturday, sunday) "
            "and times in HH:MM format, for example 19:00."
        )
        return

    create_chat(tg_chat_id, chat_name, game_time, game_week_day, reg_time, reg_week_day)
    job = schedule_weekly_registration(context.job_queue, tg_chat_id, reg_week_day, reg_time)
    context.chat_data[str(tg_chat_id)] = job
    await update.message.reply_text("The bot has started successfully.")

async def stop_repeating_job(update: Update, context: CallbackContext):
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id

    jobs = context.job_queue.jobs()
    for job in jobs:
        if str(job.name) == str(tg_chat_id):
            job.schedule_removal()
            chat_data = get_chat_by_tg_id(tg_chat_id)
            current_match = get_current_match(chat_data['id']) if chat_data else None
            if current_match:
                await close_match_draft(context, tg_chat_id, current_match['match_id'])
            delete_chat(tg_chat_id)
            await update.message.reply_text("The bot has been stopped.")
            return

    await update.message.reply_text("No recurrent messages are currently scheduled for this chat.")

async def manual_start_registration(update: Update, context: CallbackContext):
    """Manually start registration for the current chat"""
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id
    
    # Check if chat exists in the database
    chat_data = get_chat_by_tg_id(tg_chat_id)
    if not chat_data:
        await update.message.reply_text("This chat is not configured. Please use /start_repeating_job first.")
        return
    
    # Calculate the next match datetime (same as start function does)
    next_match_datetime = get_next_weekday(chat_data['game_week_day'], chat_data['game_time'])
    
    # Check if a match already exists for this datetime
    existing_match = get_match_by_datetime(chat_data['id'], next_match_datetime)
    
    if existing_match:
        # Match already exists, just send the registration message
        formatted_game_time = next_match_datetime.strftime("%d.%m.%Y %H:%M")
        sent_message = await context.bot.send_message(chat_id=tg_chat_id, text=f"Registration is now open! \nGame time: {formatted_game_time}", reply_markup=get_reply_markup(tg_chat_id))
        await context.bot.pin_chat_message(chat_id=tg_chat_id, message_id=sent_message.message_id)
        await update.message.reply_text("Registration has been manually started! (Using existing match)")
    else:
        # No match exists, create one by calling start
        job_data = {'chat_id': tg_chat_id}
        
        # Create a mock context with the job data
        class MockJob:
            def __init__(self, data):
                self.data = data
        
        mock_context = context
        mock_context.job = MockJob(job_data)
        
        try:
            # Call the start function with the mock context
            await start(mock_context)
            await update.message.reply_text("Registration has been manually started!")
        except Exception as e:
            await update.message.reply_text(f"Error starting registration: {str(e)}")
