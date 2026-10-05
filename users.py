import logging
import sqlite3

from telegram import Update
from telegram.ext import ContextTypes

from operations.users import create_user, delete_user, get_all_users_from_db, get_user
from utils import is_chat_admin

logger = logging.getLogger(__name__)

async def register_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id != chat_id:
        await context.bot.send_message(chat_id=chat_id, text=f"Please, register in DM of the bot.")
        return
        

    user = get_user(user_id)
    if user:
        nickname = user['nickname']
        name = user['name']
        await context.bot.send_message(chat_id=chat_id, text=f"You are already registered. Your nickname is {nickname} and name is {name}.")
        return

    nickname = update.effective_user.username or str(user_id)
    first_name = update.effective_user.first_name or ""
    last_name = update.effective_user.last_name or ""
    name = f"{first_name} {last_name}" if first_name or last_name else nickname

    try:
        create_user(user_id, nickname, name)
        await context.bot.send_message(chat_id=chat_id, text="Thank you for registering.")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"Oops! Something went wrong: {str(e)}")


async def get_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_chat_admin(update, context):
        return
    
    chat_id = update.effective_chat.id
    
    users = get_all_users_from_db()
    
    message = "Here are the users who have registered in the bot:\n"
    
    for idx, user in enumerate(users, start=1):
        message += f"{idx}. Name: {user['name']}, Nickname: @{user['nickname']}, ID: {user['user_id']}\n"
        
    await context.bot.send_message(chat_id=chat_id, text=message)

async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        user = get_user(user_id)
        if user:
            delete_user(user_id)
    except sqlite3.Error:
        logger.exception("Could not delete user account")
        await context.bot.send_message(chat_id=user_id, text="Could not delete your account. Please try again later.")
        return

    if user:
        text = "Your account has been deleted. To register for games again, send /start in my private chat."
    else:
        text = "You are not registered."
    await context.bot.send_message(chat_id=user_id, text=text)
