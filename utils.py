from datetime import datetime
from date_utils import get_hours_until_match
from operations.chats import get_chat_by_tg_id
from operations.match_registrations import get_current_match_registrations
from operations.matches import get_current_match, get_last_match, get_last_5_matches_with_players
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ChatMember
from telegram.ext import ContextTypes, CallbackContext
from telegram.error import BadRequest

from constants import DATETIME_FORMAT
from telegram.constants import ParseMode

def get_reply_markup(tg_chat_id):
    current_match = get_current_match(tg_chat_id)

    if current_match:
        hours_until_match = get_hours_until_match(current_match['datetime'])

        if hours_until_match < 0:
            keyboard = [
                [InlineKeyboardButton("Refresh 🔄", callback_data='refreshmessage')],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            return reply_markup

    keyboard = [
        [InlineKeyboardButton("Register", callback_data='register')],
        [InlineKeyboardButton("Register ➕ 1️⃣", callback_data='registeranother')],
        [InlineKeyboardButton("Quit", callback_data=f'quit_{tg_chat_id}')],
        [InlineKeyboardButton("Remove ➕ 1️⃣", callback_data=f'removeplusone_{tg_chat_id}')],
        [InlineKeyboardButton("Refresh 🔄", callback_data='refreshmessage')],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    return reply_markup


def get_message(chat_id):
    current_match = get_current_match(chat_id)
    current_match_registrations = get_current_match_registrations(chat_id)

    game_time_frmt = datetime.strptime(current_match['datetime'], DATETIME_FORMAT).strftime("%d.%m.%Y %H:%M")

    message = f"Registration is now open! \nMatch time: {game_time_frmt}\n\n"

    hours_until_match = get_hours_until_match(current_match['datetime'])

    if hours_until_match < 0:
        message = f"Registration is <b>CLOSED!</b> \nMatch time: {game_time_frmt}\n\n"

    message += registered(current_match_registrations)
    return message

def registered(current_match):
    if len(current_match) == 0:
        message = "<b>No registrations yet!</b>"

        return message

    message = "<b>Registered Players:</b>\n"
    for idx, match_registration in enumerate(current_match):
        if idx == 14:
            message += "\n<b>Waiting List:</b>\n"

        if match_registration == None:
            message += f"{idx + 1}. Этот чубзик так и не удосужился запустить бота \n"
            continue

        message += f"{idx + 1}. @{match_registration['nickname']} - {match_registration['name']} <i>(P{match_registration['priority']})</i>"

        if match_registration['is_plus']:
            message += f" ➕ 1️⃣ from {match_registration['registered_by_nickname']}"

        if not match_registration['confirmed']:
            message += "<i> Not confirmed</i>"
        message += "\n"

    return message


async def is_chat_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check if the user is an admin
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member["status"] == ChatMember.ADMINISTRATOR or chat_member["status"] == ChatMember.OWNER:
        return True
    else:
        await update.message.reply_text('Вася, гуляй! Only admins of the chat can call this function.')
        return False


async def is_user_in_chat(update, context, chat_id, user_id):
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            return True
        else:
            return False
    except BadRequest as e:
        if e.message == "Member not found":
            return False
        else:
            raise e


async def refresh_message(update: Update, context: CallbackContext):
    query = update.callback_query

    tg_chat_id = update.effective_chat.id

    chat_data = get_chat_by_tg_id(tg_chat_id)

    try:
        await query.edit_message_text(text=get_message(chat_data['id']), reply_markup=get_reply_markup(tg_chat_id), parse_mode=ParseMode.HTML)
        return
    except Exception as error:
        print(error)
        print("No update")
        return


async def show_registration_message(update: Update, context: CallbackContext):
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id
    chat_data = get_chat_by_tg_id(tg_chat_id)

    await context.bot.send_message(chat_id=tg_chat_id, text=get_message(chat_data['id']), reply_markup=get_reply_markup(tg_chat_id), parse_mode=ParseMode.HTML)


async def last_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_chat_id = update.effective_chat.id

    chat_data = get_chat_by_tg_id(tg_chat_id)

    if not chat_data:
        await update.message.reply_text("This chat is not configured. Please use /start_repeating_job first.")
        return

    last_match = get_last_match(chat_data['id'])

    if len(last_match) == 0:
        return await update.message.reply_text("No last match available", parse_mode=ParseMode.HTML)

    message = f"<b>Last Match Players for the game {last_match[0]['datetime']}:</b>\n"
    for idx, player in enumerate(last_match):
        if idx == 14:
            break

        message += f"{idx + 1}. @{player['nickname']} {player['name']}\n"

    await update.message.reply_text(message, parse_mode=ParseMode.HTML)


async def last_5_matches_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_chat_id = update.effective_chat.id
    
    chat_data = get_chat_by_tg_id(tg_chat_id)

    if not chat_data:
        await update.message.reply_text("This chat is not configured. Please use /start_repeating_job first.")
        return
    
    # Get players from last 5 matches
    last_5_matches_data = get_last_5_matches_with_players(chat_data['id'])
    
    if len(last_5_matches_data) == 0:
        return await update.message.reply_text("No matches found", parse_mode=ParseMode.HTML)
    
    # Get unique players from all matches and filter by current chat members
    unique_players = {}
    matches_by_date = {}
    
    for match_data in last_5_matches_data:
        match_date = match_data['datetime']
        user_id = match_data['user_id']
        
        # Group matches by date
        if match_date not in matches_by_date:
            matches_by_date[match_date] = []
        matches_by_date[match_date].append(match_data)
        
        # Store unique players
        if user_id not in unique_players:
            unique_players[user_id] = {
                'nickname': match_data['nickname'],
                'name': match_data['name'],
                'user_id': user_id
            }
    
    # Filter players who are still in the chat
    current_chat_players = []
    for user_id, player_data in unique_players.items():
        try:
            # Check if user is still in the chat
            member = await context.bot.get_chat_member(tg_chat_id, user_id)
            if member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                current_chat_players.append(player_data)
        except Exception:
            # User is no longer in the chat, skip them
            continue
    
    if len(current_chat_players) == 0:
        return await update.message.reply_text("No players from the last 5 matches are currently in this chat", parse_mode=ParseMode.HTML)
    
    # Create response message
    message = f"<b>Players from last 5 matches (currently in chat):</b>\n\n"
    
    # Add match summary
    match_dates = sorted(matches_by_date.keys(), reverse=True)
    message += f"<b>Matches included:</b>\n"
    for i, match_date in enumerate(match_dates[:5], 1):
        message += f"{i}. {match_date}\n"
    message += "\n"
    
    # Add players list
    message += f"<b>Players ({len(current_chat_players)} total):</b>\n"
    for idx, player in enumerate(sorted(current_chat_players, key=lambda x: x['nickname']), 1):
        message += f"{idx}. @{player['nickname']} {player['name']}\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)
