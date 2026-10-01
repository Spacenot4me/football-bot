from operations.chats import create_chat, delete_chat, get_chat_by_tg_id
from operations.matches import create_match, get_match_by_datetime
from utils import get_reply_markup, is_chat_admin
from date_utils import get_next_weekday, get_current_time
from constants import  DATETIME_FORMAT

from telegram import Update
from telegram.ext import CallbackContext

async def get_jobs(update: Update, context: CallbackContext):
    jobs = context.job_queue.jobs()

    job_info = ""
    for job in jobs:
        job_info += repr(job)
        job_info += job.next_t.strftime(DATETIME_FORMAT) + "\n"

    await update.message.reply_text("Current jobs: \n" + job_info)

async def start(context: CallbackContext):
    job_data = context.job.data
    tg_chat_id = job_data['chat_id']

    chat_data = get_chat_by_tg_id(tg_chat_id)

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
        next_reg_time = get_next_weekday(reg_week_day, reg_time)
        get_next_weekday(game_week_day, game_time)
    except (KeyError, ValueError):
        await update.message.reply_text(
            "Use English weekdays (monday, tuesday, wednesday, thursday, friday, saturday, sunday) "
            "and times in HH:MM format, for example 19:00."
        )
        return

    now = get_current_time()

    initial_delay = (next_reg_time - now).total_seconds()

    create_chat(tg_chat_id, chat_name, game_time, game_week_day, reg_time, reg_week_day)

    # Schedule job to run every week
    weekly_interval = 7 * 24 * 60 * 60  # 7 days in seconds

    job_data = {
        'chat_id': tg_chat_id,
    }

    job = context.job_queue.run_repeating(
        start,
        interval=weekly_interval,
        first=initial_delay,
        data=job_data,
        name=str(tg_chat_id)  # Using chat_id as job name
    )
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
