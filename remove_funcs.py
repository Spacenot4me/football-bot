from datetime import datetime, timedelta
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from constants import DATETIME_FORMAT, MAX_PLAYERS
from date_utils import get_hours_until_match
from operations.bans import create_ban
from operations.chats import get_chat_by_tg_id
from operations.match_registrations import delete_match_plus_one_registration, delete_match_registration, get_current_match_registrations, get_invited_match_registrations
from operations.matches import get_current_match
from operations.users import get_user, get_user_by_nickname
from utils import is_chat_admin

logger = logging.getLogger(__name__)

async def check_waiting_list_and_notify(context, tg_chat_id, match_id, match_datetime, quitting_player_position=None):
    """Check if there are players in waiting list and notify the first one that they can play"""
    chat_data = get_chat_by_tg_id(tg_chat_id)
    current_match_registrations = get_current_match_registrations(chat_data['id'])
    
    # If the quitting player was already in the waiting list, don't notify anyone
    if quitting_player_position is not None and quitting_player_position >= MAX_PLAYERS:
        return False  # Player was already in waiting list, no need to promote anyone
    
    # If quitting player wasn't in the main roster (or we don't know their position), don't notify
    if quitting_player_position is None or quitting_player_position >= MAX_PLAYERS:
        return False
    
    # After deletion, 14 remaining players means there was a reserve to promote.
    if len(current_match_registrations) >= MAX_PLAYERS:
        # The former first reserve is now the last player in the main roster.
        promoted_player = current_match_registrations[MAX_PLAYERS - 1]
        
        # Create a notification message for the promoted player
        message = f"Good news! A spot has opened up for the match at {match_datetime}. You've been moved from the waiting list to the main roster. See you at the game!"
        
        notifications = [
            (promoted_player['user_id'], message),
            (tg_chat_id, f"@{promoted_player['nickname']} has been moved from the waiting list to the main roster."),
        ]
        for destination, text in notifications:
            try:
                await context.bot.send_message(chat_id=destination, text=text)
            except TelegramError:
                logger.warning("Could not send waiting list promotion notification", exc_info=True)
        # The player moves into the roster even if a notification fails.
        return True
    
    return False  # No waiting list

async def remove_from_dm(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_chat_id):
    user_id = update.callback_query.from_user.id
    query = update.callback_query

    chat_data = get_chat_by_tg_id(tg_chat_id)

    current_match = get_current_match(chat_data['id'])
    
    # Get current registrations to find player's position before deleting
    current_match_registrations = get_current_match_registrations(chat_data['id'])
    
    # Find the position of the quitting player
    player_position = None
    for i, reg in enumerate(current_match_registrations):
        if reg['user_id'] == user_id:
            player_position = i
            break

    datetime_parsed = datetime.strptime(current_match['datetime'], DATETIME_FORMAT)
    hours_difference = get_hours_until_match(current_match['datetime'])
    
    # First delete the registration
    delete_match_registration(current_match['match_id'], user_id)
    
    # Then check if there's a waiting list player who can replace
    has_waiting_list_player = await check_waiting_list_and_notify(
        context, 
        tg_chat_id, 
        current_match['match_id'], 
        current_match['datetime'],
        player_position
    )
    
    # Only ban if:
    # 1. It's too close to the match
    # 2. There's no replacement from waiting list
    # 3. The player was in the main roster (not in waiting list)
    if hours_difference < 20 and not has_waiting_list_player and player_position is not None and player_position < MAX_PLAYERS:
        ten_days = timedelta(days=10)
        banned_until = datetime_parsed + ten_days
        create_ban(user_id, banned_until)
        user = get_user(user_id)
        await context.bot.send_message(chat_id=tg_chat_id, text=f"@{user['nickname']} - {user['name']}, you've been banned until {banned_until} for cancelling your registration too close to the match.")
    elif hours_difference < 20 and has_waiting_list_player and player_position is not None and player_position < MAX_PLAYERS:
        # Player cancelled with short notice but someone from waiting list can play
        user = get_user(user_id)
        await context.bot.send_message(chat_id=tg_chat_id, text=f"@{user['nickname']} - {user['name']} cancelled with short notice, but a player from the waiting list has been promoted to replace them.")

    try:
        await query.edit_message_text(text="Thank you for the confirmation.")
    except Exception as error:
        print("Error!", error)
        return

def invited_player_name(registration):
    return str(registration['name'] or registration['nickname'] or registration['user_id'])


async def start_remove_plus_one(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_chat_id):
    """Ask which owned invitation to remove and bind the button to its record."""
    user_id = update.callback_query.from_user.id
    chat_data = get_chat_by_tg_id(tg_chat_id)
    current_match = get_current_match(chat_data['id']) if chat_data else None
    if not current_match or get_hours_until_match(current_match['datetime']) < 0:
        await context.bot.send_message(chat_id=user_id, text="There is no open match in this chat.")
        return
    invitations = get_invited_match_registrations(current_match['match_id'], user_id)
    if not invitations:
        await context.bot.send_message(chat_id=user_id, text="You have no invited players to remove from this match.")
        return
    keyboard = [
        [InlineKeyboardButton(
            f"Remove {invited_player_name(invitation)}",
            callback_data=f"removeplusoneconfirm_{tg_chat_id}_{invitation['registration_id']}",
        )]
        for invitation in invitations
    ]
    text = (
        f"Remove {invited_player_name(invitations[0])} from the match?"
        if len(invitations) == 1 else "Choose the invited player you want to remove from the match."
    )
    await context.bot.send_message(chat_id=user_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))


async def remove_plus_one(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_chat_id, registration_id=None):
    user_id = update.callback_query.from_user.id
    query = update.callback_query

    async def finish(text):
        try:
            await query.edit_message_text(text=text)
        except TelegramError:
            await context.bot.send_message(chat_id=user_id, text=text)

    chat_data = get_chat_by_tg_id(tg_chat_id)
    current_match = get_current_match(chat_data['id']) if chat_data else None
    if not current_match or get_hours_until_match(current_match['datetime']) < 0:
        await finish("There is no open match in this chat.")
        return
    invitations = get_invited_match_registrations(current_match['match_id'], user_id)
    if registration_id is None:
        # Old confirmation messages remain usable, but cannot remove two people.
        if len(invitations) > 1:
            await start_remove_plus_one(update, context, tg_chat_id)
            return
        invitation = invitations[0] if invitations else None
    else:
        invitation = next((item for item in invitations if item['registration_id'] == int(registration_id)), None)
    if invitation is None:
        await finish("This invitation is no longer available for removal from the current match.")
        return

    current_match_registrations = get_current_match_registrations(chat_data['id'])
    plus_one_position = next((
        index for index, reg in enumerate(current_match_registrations)
        if reg['registration_id'] == invitation['registration_id']
    ), None)
    hours_difference = get_hours_until_match(current_match['datetime'])
    removed_user_id = delete_match_plus_one_registration(
        current_match['match_id'], user_id, invitation['registration_id'],
    )
    if removed_user_id is None:
        await finish("This player has already been removed from the match.")
        return
    name = invited_player_name(invitation)
    await finish(f"Removed {name} from the match.")

    has_waiting_list_player = await check_waiting_list_and_notify(
        context, tg_chat_id, current_match['match_id'], current_match['datetime'], plus_one_position,
    )
    if hours_difference < 22 and not has_waiting_list_player and plus_one_position is not None and plus_one_position < MAX_PLAYERS:
        banned_until = datetime.strptime(current_match['datetime'], DATETIME_FORMAT) + timedelta(days=10)
        create_ban(removed_user_id, banned_until)
        try:
            await context.bot.send_message(
                chat_id=tg_chat_id,
                text=f"{name} has been banned until {banned_until} for cancelling less than 22 hours before the match without a waiting list replacement.",
            )
        except TelegramError:
            logger.warning("Could not send cancellation ban notification", exc_info=True)

    inviter = get_user(user_id)
    inviter_name = (inviter['name'] or inviter['nickname']) if inviter else str(user_id)
    try:
        await context.bot.send_message(chat_id=removed_user_id, text=f"You have been removed from the match registration by {inviter_name}.")
    except TelegramError:
        logger.warning("Could not notify removed invited player", exc_info=True)

async def remove_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id
    chat = get_chat_by_tg_id(tg_chat_id)

    current_match = get_current_match(chat['id'])
    message_id = update.message.id

    if len(context.args) == 1 and context.args[0].startswith('@'):
        user_nickname = context.args[0][1:]
        user = get_user_by_nickname(user_nickname)
        if not user:
            await context.bot.send_message(chat_id=tg_chat_id, text="This user is not registered in bot.")
            return
    else:
        await update.message.reply_text("No user was provided.")
        return
        
    # Get current registrations to find player's position before deleting
    current_match_registrations = get_current_match_registrations(chat['id'])
    
    # Find the position of the player to be removed
    player_position = None
    for i, reg in enumerate(current_match_registrations):
        if reg['user_id'] == user['user_id']:
            player_position = i
            break

    # Delete the registration first
    delete_match_registration(current_match['match_id'], user['user_id'])
    
    # Check if someone from waiting list can be promoted
    await check_waiting_list_and_notify(
        context, 
        tg_chat_id, 
        current_match['match_id'], 
        current_match['datetime'],
        player_position
    )

    await context.bot.set_message_reaction(chat_id=tg_chat_id, message_id=message_id, reaction="👌")

async def remove_other_plus_one(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id
    chat = get_chat_by_tg_id(tg_chat_id)

    current_match = get_current_match(chat['id'])
    message_id = update.message.id

    if len(context.args) == 1 and context.args[0].startswith('@'):
        user_nickname = context.args[0][1:]
        user = get_user_by_nickname(user_nickname)
        if not user:
            await context.bot.send_message(chat_id=tg_chat_id, text="This user is not registered in bot.")
            return
    else:
        await update.message.reply_text("No user was provided.")
        return

    # Delete the plus one registration first
    delete_match_plus_one_registration(current_match['match_id'], user['user_id'])
    
    # Check if someone from waiting list can be promoted
    await check_waiting_list_and_notify(
        context, 
        tg_chat_id, 
        current_match['match_id'], 
        current_match['datetime']
    )

    await context.bot.set_message_reaction(chat_id=tg_chat_id, message_id=message_id, reaction="👌")
