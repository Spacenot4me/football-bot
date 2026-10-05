from datetime import datetime
import re
import pytz
from date_utils import get_hours_until_match, get_current_time
from operations.bans import delete_ban, get_players_ban
from operations.chats import get_chat_by_tg_id
from operations.match_registrations import check_if_user_played_before, check_if_user_registered, confirm_user_registration, create_match_registration, delete_match_registration, get_current_match_registrations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackContext, ContextTypes
from constants import PRIORITY_HOURS, SQL_DATETIME_FORMAT
from operations.matches import get_current_match, get_match, was_in_last_match
from operations.users import get_all_users_from_db, get_user, get_user_by_nickname
from utils import get_reply_markup, is_user_in_chat, get_message

def is_player_banned(user_id):
    player_bans = get_players_ban(user_id)
    current_datetime = get_current_time()

    for ban in player_bans:
        user_date = datetime.fromisoformat(ban['until'])

        if current_datetime < user_date:
            return True

        delete_ban(ban['ban_id'])

    return False

async def check_for_confimation(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    tg_chat_id = job_data['chat_id']
    user_id = job_data['user_id']

    chat_data = get_chat_by_tg_id(tg_chat_id)

    if not chat_data:
        return
    if 'match_id' in job_data:
        curr_match = get_match(job_data['match_id'])
    else:
        curr_match = get_current_match(chat_data['id'])
    if not curr_match:
        return

    is_user_registered = check_if_user_registered(curr_match['match_id'], user_id)

    if is_user_registered and is_user_registered['confirmed'] == 0:
        delete_match_registration(curr_match['match_id'], user_id)
        await context.bot.send_message(chat_id=tg_chat_id, text=f"@{is_user_registered['nickname']} did not confirm their registration!")
        return

def get_priority(user_id, reg_time, game_time, was_in_last_match, is_plus):
    now = get_current_time()
    reg_time = pytz.timezone("Europe/Prague").localize(datetime.strptime(reg_time, SQL_DATETIME_FORMAT))

    if is_player_banned(user_id):
        return 4
    if (now - reg_time).total_seconds() / 7200 < PRIORITY_HOURS and was_in_last_match:
        return 1

    hours_difference = get_hours_until_match(game_time)
    if hours_difference < 26:
        return 3

    if not check_if_user_played_before(user_id):
        return 3

    if is_plus:
        return 3


    return 2

async def register_another_from_chat(update: Update, context: CallbackContext):
    user_id = update.callback_query.from_user.id
    tg_chat_id = update.effective_chat.id
    users = get_all_users_from_db()

    chat_data = get_chat_by_tg_id(tg_chat_id)
    current_match = get_current_match(chat_data['id']) if chat_data else None
    if not current_match or get_hours_until_match(current_match['datetime']) < 0:
        await context.bot.send_message(chat_id=user_id, text="There is no open registration for this chat.")
        return

    message = (
        f"Select a player for the match at {current_match['datetime']}.\n"
        "You can also send @username here, or reply to this message with @username.\n"
        "The player must first register with /start in my private chat."
    )

    keyboard = []

    for index in range(0, len(users), 2):
        keyboard.append([
            InlineKeyboardButton(user['name'], callback_data=f"registerplusone_{tg_chat_id}_{user['user_id']}")
            for user in users[index:index + 2]
        ])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    sent = await context.bot.send_message(chat_id=user_id, text=message, reply_markup=reply_markup)
    prompt = {'chat_id': tg_chat_id, 'match_id': current_match['match_id']}
    context.user_data['plus_one_prompt'] = prompt
    prompts = context.user_data.setdefault('plus_one_prompts', {})
    prompts[sent.message_id] = prompt
    while len(prompts) > 20:
        del prompts[next(iter(prompts))]


async def register_plus_one_by_username(update: Update, context: CallbackContext):
    """Accept @username only in private after opening the +1 player menu."""
    message = update.effective_message
    if not message or not update.effective_user or update.effective_chat.type != 'private':
        return
    match = re.fullmatch(r"@([A-Za-z][A-Za-z0-9_]{0,31})", (message.text or '').strip())
    if not match:
        await message.reply_text("Send one @username to invite a player from this +1 menu.")
        return

    reply = message.reply_to_message
    if reply:
        prompt = context.user_data.get('plus_one_prompts', {}).get(reply.message_id)
    else:
        prompt = context.user_data.get('plus_one_prompt')
    if not prompt:
        await message.reply_text("First click Register ➕ 1️⃣ in the game chat, then send @username here.")
        return
    tg_chat_id = prompt['chat_id']
    expected_match_id = prompt['match_id']

    nickname = match.group(1)
    player = get_user_by_nickname(nickname)
    if not player:
        await message.reply_text(
            f"@{nickname} is not registered in the bot. Ask them to send /start in my private chat, then try again."
        )
        return
    await register_plus_one(update, context, tg_chat_id, player['user_id'], expected_match_id)


def register_core(chat_id, user_id, registered_by_id, is_plus=False, confirmed=True):
    was_in_last_match_res = was_in_last_match(chat_id, user_id)

    current_match = get_current_match(chat_id)
    match_id = current_match['match_id']
    start_time = current_match['datetime']
    reg_time = current_match['created_at']

    is_user_registered = check_if_user_registered(match_id, user_id)

    if is_user_registered and is_user_registered['confirmed'] == 0:
        confirm_user_registration(match_id, user_id)
        return True

    if is_user_registered:
        return False

    hours_until_match = get_hours_until_match(current_match['datetime'])

    if hours_until_match < 0:
        return False

    priority = get_priority(user_id, reg_time, start_time, was_in_last_match_res, is_plus)
    create_result = create_match_registration(user_id, registered_by_id, is_plus, confirmed, priority, match_id)

    return create_result

async def register_himself(update: Update, context: CallbackContext):
    user_id = update.callback_query.from_user.id
    user_name = update.callback_query.from_user.username
    tg_chat_id = update.effective_chat.id

    chat_data = get_chat_by_tg_id(tg_chat_id)
    user = get_user(user_id)

    chat_id = chat_data['id']

    if not user:
        await context.bot.send_message(chat_id=tg_chat_id, text=f"@{user_name} To be able to register to the matches you need to activate me in the DM.")
        return

    res = register_core(chat_id, user_id, user_id)

    if res:
        query = update.callback_query
        await query.edit_message_text(text=get_message(chat_id), reply_markup=get_reply_markup(tg_chat_id), parse_mode=ParseMode.HTML)

    return

async def register_plus_one(update: Update, context: CallbackContext, tg_chat_id, user_id, expected_match_id=None):
    """Shared invitation path for menu buttons and username messages."""
    request_user_id = update.effective_user.id
    user_id = int(user_id)

    async def respond(text):
        if update.callback_query:
            await context.bot.send_message(chat_id=request_user_id, text=text)
        else:
            await update.effective_message.reply_text(text)

    if not get_user(request_user_id):
        await respond("Please register first: send /start in my private chat.")
        return
    player = get_user(user_id)
    if not player:
        await respond("This player is not registered. Ask them to send /start in my private chat.")
        return
    if user_id == request_user_id:
        await respond("Use Register to register yourself. The +1 option is for another player.")
        return

    chat_data = get_chat_by_tg_id(tg_chat_id)
    current_match = get_current_match(chat_data['id']) if chat_data else None
    if not current_match:
        await respond("There is no match open for registration in this chat.")
        return
    if expected_match_id is not None and current_match['match_id'] != expected_match_id:
        await respond("This +1 menu is for an older match. Click Register ➕ 1️⃣ again for the current match.")
        return
    if get_hours_until_match(current_match['datetime']) < 0:
        await respond("Registration for this match is closed.")
        return
    match_id = current_match['match_id']
    if check_if_user_registered(match_id, user_id):
        await respond("This player is already registered or awaiting their own confirmation.")
        return

    user_in_chat = await is_user_in_chat(update, context, tg_chat_id, user_id)
    res = register_core(chat_data['id'], user_id=user_id, registered_by_id=request_user_id, is_plus=not user_in_chat, confirmed=False)
    if not res:
        await respond("You cannot register more people for this match.")
        return

    keyboard = [
        [InlineKeyboardButton("Confirm", callback_data=f'confirm_{tg_chat_id}')],
        [InlineKeyboardButton("Quit", callback_data=f'removefromdm_{tg_chat_id}')],
    ]
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"You have been invited to the football match at {current_match['datetime']}. "
                "Confirm your registration within 4 hours, or click Quit. "
                "Cancelling close to the match may result in a ban."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except (Forbidden, BadRequest):
        delete_match_registration(match_id, user_id)
        await respond("I could not message this player. Ask them to unblock me and send /start, then try again.")
        return

    job_data = {'user_id': user_id, 'chat_id': tg_chat_id, 'match_id': match_id}
    context.job_queue.run_once(check_for_confimation, 14400, data=job_data)
    await respond(f"{player['name']} has been invited to the match at {current_match['datetime']}. Awaiting their confirmation.")

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_chat_id):
    user_id = update.callback_query.from_user.id
    chat_data = get_chat_by_tg_id(tg_chat_id)

    res = get_current_match(chat_data['id'])
    current_match = res
    match_id = current_match['match_id']

    confirm_user_registration(match_id, user_id)
