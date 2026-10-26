from telegram.ext import ContextTypes
from telegram import Update
from operations.chats import get_chat_by_tg_id
from operations.matches import delete_match, get_current_match
from utils import is_chat_admin
from draft_funcs import close_match_draft

async def cancel_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_chat_admin(update, context):
        return

    tg_chat_id = update.effective_chat.id
    message_id = update.message.id
    
    chat_data = get_chat_by_tg_id(tg_chat_id)
    
    current_match = get_current_match(chat_data['id'])
    await close_match_draft(context, tg_chat_id, current_match['match_id'])
    delete_match(current_match['match_id'])
    
    await context.bot.set_message_reaction(chat_id=tg_chat_id, message_id=message_id, reaction="👌")
