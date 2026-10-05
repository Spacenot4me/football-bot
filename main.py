import os
import datetime
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, CallbackContext, MessageHandler, filters
from bans import ban, ban_forever, get_all_bans_command, get_my_bans, unban
from date_utils import get_next_weekday, get_current_time
from matches import cancel_match
from operations.chats import get_all_chats, get_chat_by_tg_id, update_chat
from users import delete_account, get_all_users, register_user
from utils import refresh_message, show_registration_message, last_match, last_5_matches_players
from register_funcs import register_himself, register_another_from_chat, register_plus_one, register_plus_one_by_username, confirm
from remove_funcs import remove_from_dm, remove_other_plus_one, remove_plus_one, remove_other, start_remove_plus_one
from jobs_funcs import get_jobs, start_repeating_job, stop_repeating_job, start, manual_start_registration
from pidor import pick_random_user, get_random_stats, clear_random_stats

TOKEN = os.getenv("TG_TOKEN")
logger = logging.getLogger(__name__)

async def unknown_command(update: Update, context: CallbackContext):
    logger.warning("Unknown command received")

async def handle_error(update: object, context: CallbackContext):
    error = context.error
    logger.error("Failed to handle update", exc_info=(type(error), error, error.__traceback__))

def initiate(application):
    chats = get_all_chats()

    for chat in chats:
        now = get_current_time()
        next_weekday = get_next_weekday(chat['reg_week_day'], chat['reg_time'])
        if next_weekday < now:
            next_weekday += datetime.timedelta(days=7)

        initial_delay = (next_weekday - now).total_seconds()

        weekly_interval = 7 * 24 * 60 * 60  # 7 days in seconds

        job_data = {
        'chat_id': chat['chat_id'],
        }

        application.job_queue.run_repeating(
            start,
            interval=weekly_interval,
            first=initial_delay,
            data=job_data,
            name=chat['chat_id']
        )

def migchat(bot, update):
    oldchatid = update.message.migrate_from_chat_id
    newchatid = update.message.chat.id
    old_chat = get_chat_by_tg_id(oldchatid)
    update_chat(old_chat['id'], newchatid, old_chat['name'], old_chat['game_time'], old_chat['game_week_day'], old_chat['reg_time'], old_chat['reg_week_day'])

# Main function
def main():
    application = Application.builder().token(TOKEN).build()
    application.add_error_handler(handle_error)

    application.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, migchat))

    application.add_handler(CommandHandler("start_repeating_job", start_repeating_job))
    application.add_handler(CommandHandler("stop", stop_repeating_job))
    application.add_handler(CommandHandler("start_registration", manual_start_registration))
    application.add_handler(CommandHandler("ban", ban))
    application.add_handler(CommandHandler("unban", unban))
    application.add_handler(CommandHandler("ban_forever", ban_forever))
    application.add_handler(CommandHandler("get_my_bans", get_my_bans))
    application.add_handler(CommandHandler("get_all_bans", get_all_bans_command))
    application.add_handler(CommandHandler("cancel_match", cancel_match))

    application.add_handler(CommandHandler(
        "show_registration_message", show_registration_message))

    application.add_handler(CommandHandler("last_match", last_match))
    application.add_handler(CommandHandler("last_5_matches_players", last_5_matches_players))
    application.add_handler(CommandHandler("remove_other", remove_other))
    application.add_handler(CommandHandler("remove_other_plus_one", remove_other_plus_one))

    application.add_handler(CommandHandler("confirm", confirm))
    application.add_handler(CommandHandler("get_jobs", get_jobs))

    application.add_handler(CommandHandler("start", register_user))
    application.add_handler(CommandHandler("register_me", register_user))
    application.add_handler(CommandHandler("get_all_users", get_all_users))
    application.add_handler(CommandHandler("delete_my_account", delete_account))

    # Add new command handlers for random user selection
    application.add_handler(CommandHandler("pidor", pick_random_user))
    application.add_handler(CommandHandler("pidor_stats", get_random_stats))
    application.add_handler(CommandHandler("clear_random_stats", clear_random_stats))

    callback_mapping = {
        'register': register_himself,
        'registeranother': register_another_from_chat,
        'refreshmessage': refresh_message,
    }

    def parse_callback_data(data):
        parts = data.split('_')
        if len(parts) == 3:
            action, chat_id, user_id = parts
            return action, chat_id, user_id
        elif len(parts) == 2:
            action, chat_id = parts
            return action, chat_id
        elif len(parts) == 1:
            action = parts[0]
            return action,
        else:
            raise ValueError("Invalid data format")


    async def callback_query_handler(update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        await query.answer()
        parsed_callback_data = parse_callback_data(query.data)
        action = parsed_callback_data[0]

        if action == 'quit':
            chat_id = parsed_callback_data[1]
            user_id = update.callback_query.from_user.id

            print(action, chat_id, user_id)

            keyboard = [
                [InlineKeyboardButton(
                    "Yes, confirm quit", callback_data=f'removefromdm_{chat_id}')],
            ]
            confirm_reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=user_id, text="Do you want to quit?", reply_markup=confirm_reply_markup)

        if action == 'removeplusone':
            chat_id = parsed_callback_data[1]
            await start_remove_plus_one(update, context, chat_id)

        if action == 'removeplusoneconfirm':
            chat_id = parsed_callback_data[1]
            registration_id = parsed_callback_data[2] if len(parsed_callback_data) == 3 else None
            await remove_plus_one(update, context, chat_id, registration_id)

        if action == 'confirm':
            chat_id = parsed_callback_data[1]
            await confirm(update, context, chat_id)

        if action == 'removefromdm':
            chat_id = parsed_callback_data[1]
            await remove_from_dm(update, context, chat_id)

        if action == 'registerplusone':
            chat_id = parsed_callback_data[1]
            user_id = parsed_callback_data[2]

            await register_plus_one(update, context, chat_id, user_id)

        # Call the corresponding function
        if action in callback_mapping:
            await callback_mapping[action](update, context)

    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & filters.Regex(r"^\s*@"),
        register_plus_one_by_username,
    ))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    initiate(application)

    application.run_polling()


if __name__ == '__main__':
    main()
