import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllGroupChats
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, CallbackContext, MessageHandler, filters
from bans import ban, ban_forever, get_all_bans_command, get_my_bans, unban
from matches import cancel_match
from operations.chats import get_all_chats, get_chat_by_tg_id, update_chat
from users import delete_account, get_all_users, register_user
from utils import refresh_message, show_registration_message, last_match, last_5_matches_players
from register_funcs import register_himself, register_another_from_chat, register_plus_one, register_plus_one_by_username, confirm, confirm_command
from remove_funcs import remove_from_dm, remove_other_plus_one, remove_plus_one, remove_other, start_remove_plus_one
from jobs_funcs import get_jobs, start_repeating_job, stop_repeating_job, manual_start_registration, schedule_weekly_registration
from pidor import pick_random_user, get_random_stats, clear_random_stats
from draft_funcs import captains, cancel_team_draft, teams, draft_callback, draft_help

TOKEN = os.getenv("TG_TOKEN")
logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand('start', 'Activate the bot and register your profile (in private chat)'),
    BotCommand('register_me', 'Register your profile in private chat (alias for /start)'),
    BotCommand('delete_my_account', 'Delete your registered profile'),
    BotCommand('register_another_from_chat', 'Invite another player: open their selection menu from the game chat'),
    BotCommand('confirm', 'How to confirm an invitation using its Confirm button'),
    BotCommand('get_my_bans', 'Show your bans'),
    BotCommand('get_all_bans', 'Show all bans'),
    BotCommand('last_match', 'Show players from the previous match'),
    BotCommand('last_5_matches_players', 'Show players from the last five matches'),
    BotCommand('captains', 'Choose captains: @a @b | random | random @a @b @c (admin)'),
    BotCommand('capitans', 'Alias for /captains: @a @b | random | random @a @b @c (admin)'),
    BotCommand('teams', 'Show and pin the current teams'),
    BotCommand('cancel_draft', 'Cancel draft with confirmation (admin or captain)'),
    BotCommand('draft_help', 'Examples and help for captain selection and team draft'),
    BotCommand('pidor', 'Select a random registered chat member for today'),
    BotCommand('pidor_stats', 'Show daily selection statistics'),
    BotCommand('start_repeating_job', 'Schedule: NAME REG_DAY REG_TIME GAME_DAY GAME_TIME (admin)'),
    BotCommand('stop', 'Stop registration schedule in this chat (admin)'),
    BotCommand('start_registration', 'Open match registration now (admin)'),
    BotCommand('show_registration_message', 'Show the current registration list and buttons (admin)'),
    BotCommand('get_jobs', 'Show scheduled tasks and their next Prague time'),
    BotCommand('get_all_users', 'Show all registered bot users (admin)'),
    BotCommand('ban', 'Ban a player: /ban @username (admin)'),
    BotCommand('unban', 'Remove player bans: /unban @username (admin)'),
    BotCommand('ban_forever', 'Ban a player indefinitely: /ban_forever @username (admin)'),
    BotCommand('cancel_match', 'Cancel the current match (admin)'),
    BotCommand('remove_other', 'Remove a player: /remove_other @username (admin)'),
    BotCommand('remove_other_plus_one', 'Remove the external guest invited by @username (admin)'),
    BotCommand('clear_random_stats', 'Clear daily selection statistics for this chat (admin)'),
]


async def configure_command_menu(application):
    # Run on startup rather than during import/build; no token is used by tests.
    for scope in (None, BotCommandScopeAllGroupChats()):
        try:
            await application.bot.set_my_commands(USER_COMMANDS, scope=scope)
        except TelegramError:
            logger.warning('Could not update Telegram command menu', exc_info=True)

async def unknown_command(update: Update, context: CallbackContext):
    logger.warning("Unknown command received")

async def handle_error(update: object, context: CallbackContext):
    error = context.error
    logger.error("Failed to handle update", exc_info=(type(error), error, error.__traceback__))

def initiate(application):
    chats = get_all_chats()

    for chat in chats:
        schedule_weekly_registration(application.job_queue, chat['chat_id'], chat['reg_week_day'], chat['reg_time'])

def migchat(bot, update):
    oldchatid = update.message.migrate_from_chat_id
    newchatid = update.message.chat.id
    old_chat = get_chat_by_tg_id(oldchatid)
    update_chat(old_chat['id'], newchatid, old_chat['name'], old_chat['game_time'], old_chat['game_week_day'], old_chat['reg_time'], old_chat['reg_week_day'])

# Main function
def main():
    application = Application.builder().token(TOKEN).build()
    application.post_init = configure_command_menu
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
    application.add_handler(CommandHandler(["captains", "capitans"], captains))
    application.add_handler(CommandHandler("cancel_draft", cancel_team_draft))
    application.add_handler(CommandHandler("teams", teams))
    application.add_handler(CommandHandler("draft_help", draft_help))

    application.add_handler(CommandHandler(
        "show_registration_message", show_registration_message))

    application.add_handler(CommandHandler("last_match", last_match))
    application.add_handler(CommandHandler("last_5_matches_players", last_5_matches_players))
    application.add_handler(CommandHandler("remove_other", remove_other))
    application.add_handler(CommandHandler("remove_other_plus_one", remove_other_plus_one))

    application.add_handler(CommandHandler("confirm", confirm_command))
    application.add_handler(CommandHandler("register_another_from_chat", register_another_from_chat))
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
        if 1 <= len(parts) <= 4:
            return tuple(parts)
        raise ValueError("Invalid data format")


    async def callback_query_handler(update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        await query.answer()
        parsed_callback_data = parse_callback_data(query.data)
        action = parsed_callback_data[0]

        if action in ('draftpick', 'draftrestart', 'draftrestartconfirm', 'draftrestartcancel', 'draftrefresh',
                      'draftcancel', 'draftcancelconfirm', 'draftcancelreject'):
            await draft_callback(update, context)
            return

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
            match_id = parsed_callback_data[2] if len(parsed_callback_data) >= 3 else None
            registration_id = parsed_callback_data[3] if len(parsed_callback_data) == 4 else None
            await confirm(update, context, chat_id, match_id, registration_id)

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
