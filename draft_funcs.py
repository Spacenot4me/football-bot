"""Telegram interface for persistent captains and team drafts."""

import asyncio
from collections import defaultdict
from html import escape
import logging
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

from operations import common
from operations import team_drafts as drafts
from operations.chats import get_chat_by_tg_id
from operations.matches import get_current_match
from operations.users import get_user_by_nickname

logger = logging.getLogger(__name__)
_locks = defaultdict(asyncio.Lock)


def _lock(tg_chat_id):
    return _locks[(common.DB_PATH, str(tg_chat_id))]


def _label(player):
    if not player:
        return 'Unknown player'
    return '@' + str(player['nickname']) if player.get('nickname') else str(player['user_id'])


def _actor(update):
    query = getattr(update, 'callback_query', None)
    return query.from_user.id if query else update.effective_user.id


async def _admin(context, tg_chat_id, user_id):
    try:
        member = await context.bot.get_chat_member(tg_chat_id, user_id)
        return member.status in ('administrator', 'creator')
    except TelegramError:
        logger.warning('Could not check draft administrator permissions', exc_info=True)
        return False


async def _send(context, tg_chat_id, text, draft_id=None, markup=None, quiet=True):
    try:
        sent = await context.bot.send_message(
            chat_id=tg_chat_id, text=text, parse_mode='HTML',
            reply_markup=markup, disable_notification=quiet,
        )
        if draft_id is not None:
            drafts.record_message(draft_id, tg_chat_id, sent.message_id)
        return sent
    except TelegramError:
        logger.warning('Could not send draft message', exc_info=True)
        return None


def render_draft(view):
    d = view['draft']
    players = {p['user_id']: p for p in view['players']}
    text = [f"⚽ Team draft — match {escape(str(view['match']['datetime']))}", '']
    if d['status'] == 'waiting_for_players':
        text += [f"Registered players: {len(view['players'])}",
                 'Waiting for players to assign two different captains.',
                 'The draft will continue when the required players register.']
        if d['captain_mode'] != 'random_all':
            import json
            from operations.users import get_user
            text.append('Captain candidates: ' + ', '.join(
                escape(_label(get_user(uid) or {'user_id': uid}))
                for uid in json.loads(d['candidate_user_ids'])))
    else:
        for team in ('A', 'B'):
            captain_id = d['captain_' + team.lower() + '_id']
            captain = players.get(captain_id, {'user_id': captain_id})
            members = [captain] + [players[p['user_id']] for p in view['picks']
                                   if p['team'] == team and p['user_id'] in players]
            text.append(f"<b>Team {escape(_label(captain))} ({len(members)})</b>")
            for index, player in enumerate(members, 1):
                text.append(f"{index}. {escape(_label(player))} - {escape(str(player.get('name') or ''))}"
                            + (' (C)' if index == 1 else ''))
            text.append('')
        if d['status'] == 'needs_replacement':
            for team in ('A', 'B'):
                missing = d['replacement_' + team.lower()]
                if missing:
                    captain = players.get(d['captain_' + team.lower() + '_id'])
                    text.append(f"⚠️ {escape(_label(captain))}, pick a replacement ({missing} needed).")
            if not view['free']:
                text.append('Waiting for an available player or a draft restart.')
        elif d['status'] == 'drafting':
            captain = players.get(d['captain_' + view['turn'].lower() + '_id'])
            text.append(f"🎯 {escape(_label(captain))}'s turn to pick:")
        else:
            text.append('✅ All available players have been assigned.')
    keyboard = []
    if d['status'] in ('drafting', 'needs_replacement'):
        for offset in range(0, len(view['free']), 2):
            keyboard.append([InlineKeyboardButton(
                ('Pick ' if d['status'] == 'needs_replacement' else '')
                + _label(player) + ' - ' + str(player.get('name') or ''),
                callback_data=f"draftpick_{d['draft_id']}_{player['user_id']}_{d['revision']}",
            ) for player in view['free'][offset:offset + 2]])
    keyboard.append([
        InlineKeyboardButton('🔁 Restart draft', callback_data=f"draftrestart_{d['draft_id']}"),
        InlineKeyboardButton('Refresh 🔄', callback_data=f"draftrefresh_{d['draft_id']}"),
    ])
    keyboard.append([InlineKeyboardButton('❌ Cancel draft', callback_data=f"draftcancel_{d['draft_id']}")])
    return '\n'.join(text), InlineKeyboardMarkup(keyboard)


async def _delete_message(context, tg_chat_id, message_id):
    try:
        await context.bot.delete_message(chat_id=tg_chat_id, message_id=message_id)
    except BadRequest as error:
        if 'message to delete not found' not in str(error).lower():
            logger.warning('Could not delete draft message', exc_info=True)
            return False
    except TelegramError:
        logger.warning('Could not delete draft message', exc_info=True)
        return False
    drafts.forget_message(tg_chat_id, message_id)
    return True


async def _cleanup(context, draft_id):
    for message in drafts.get_messages(draft_id):
        await _delete_message(context, message['tg_chat_id'], message['message_id'])


async def _notify_events(context, view):
    if not view['events']:
        return
    lines = []
    for event in view['events']:
        if event['kind'] in ('left', 'roster'):
            suffix = 'left the match registration.' if event['kind'] == 'left' else 'left the main roster.'
            lines.append(f"⚠️ {escape(_label(event['player']))} {suffix}")
        elif event['kind'] == 'captain':
            player = next((p for p in view['players'] if p['user_id'] == event['new_id']), None)
            lines.append(f"{escape(_label(player))} is the new captain of Team {event['team']}.")
        elif event['kind'] == 'cancelled':
            lines.append('Draft cancelled. ' + escape(event['reason']))
    d = view['draft']
    if d['status'] == 'needs_replacement':
        for team in ('A', 'B'):
            if d['replacement_' + team.lower()]:
                player = next((p for p in view['players']
                               if p['user_id'] == d['captain_' + team.lower() + '_id']), None)
                lines.append(f"{escape(_label(player))}, pick a replacement or request a draft restart.")
    if lines:
        await _send(context, view['match']['tg_chat_id'], '\n'.join(lines), d['draft_id'], quiet=False)


async def _publish(context, view, resend=False):
    if not view or not view['match']:
        return
    d = view['draft']
    if d['status'] == 'cancelled':
        await _cleanup(context, d['draft_id'])
        await _notify_events(context, view)
        return
    text, markup = render_draft(view)
    tg_chat_id = view['match']['tg_chat_id']
    old_id = d['main_message_id']
    create = resend or old_id is None
    if not create:
        try:
            await context.bot.edit_message_text(chat_id=tg_chat_id, message_id=old_id,
                                                text=text, reply_markup=markup, parse_mode='HTML')
        except BadRequest as error:
            if 'message to edit not found' in str(error).lower():
                create = True
            elif 'message is not modified' not in str(error).lower():
                logger.warning('Could not update draft message', exc_info=True)
        except TelegramError:
            logger.warning('Could not update draft message', exc_info=True)
    if create:
        sent = await _send(context, tg_chat_id, text, d['draft_id'], markup)
        if sent:
            drafts.record_message(d['draft_id'], tg_chat_id, sent.message_id, main=True)
            try:
                await context.bot.pin_chat_message(chat_id=tg_chat_id, message_id=sent.message_id,
                                                   disable_notification=False)
            except TelegramError:
                logger.warning('Could not pin draft message; draft remains available', exc_info=True)
            if old_id and old_id != sent.message_id:
                if not await _delete_message(context, tg_chat_id, old_id):
                    try:
                        await context.bot.edit_message_reply_markup(chat_id=tg_chat_id, message_id=old_id,
                                                                    reply_markup=None)
                    except TelegramError:
                        logger.warning('Could not disable old draft keyboard', exc_info=True)
    await _notify_events(context, view)


def _current(tg_chat_id):
    chat = get_chat_by_tg_id(tg_chat_id)
    return get_current_match(chat['id']) if chat else None


async def captains(update, context):
    tg_chat_id = update.effective_chat.id
    if not await _admin(context, tg_chat_id, _actor(update)):
        await _send(context, tg_chat_id, 'Only chat admins can start a draft.')
        return
    async with _lock(tg_chat_id):
        match = _current(tg_chat_id)
        if not match:
            await _send(context, tg_chat_id, 'Open match registration in this chat first.')
            return
        args = context.args or []
        mode = 'random_all'
        names = []
        if args:
            if args[0].lower() == 'random':
                names = args[1:]
                mode = 'random_list' if names else 'random_all'
            else:
                names, mode = args, 'manual'
        if (mode == 'manual' and len(names) != 2) or (mode == 'random_list' and len(names) < 2):
            await _send(context, tg_chat_id, 'Usage: /captains [@a @b | random [@a @b ...]]')
            return
        candidates = []
        for name in names:
            user = get_user_by_nickname(name[1:]) if name.startswith('@') else None
            if not user or user['user_id'] in candidates:
                await _send(context, tg_chat_id, 'Use different @usernames registered in the bot.')
                return
            candidates.append(user['user_id'])
        view = drafts.create_draft(match['match_id'], mode, candidates)
        if getattr(update, 'effective_message', None):
            drafts.record_message(view['draft']['draft_id'], tg_chat_id, update.effective_message.message_id)
        if view['error']:
            await _send(context, tg_chat_id, view['error'], view['draft']['draft_id'])
        await _publish(context, view)


async def teams(update, context):
    tg_chat_id = update.effective_chat.id
    async with _lock(tg_chat_id):
        match = _current(tg_chat_id)
        draft = drafts.get_active_draft(match['match_id']) if match else None
        if not draft:
            await _send(context, tg_chat_id, 'No active draft. Ask an admin to use /captains.')
            return
        if getattr(update, 'effective_message', None):
            drafts.record_message(draft['draft_id'], tg_chat_id, update.effective_message.message_id)
        await _publish(context, drafts.sync_draft(draft['draft_id']), resend=True)


async def cancel_team_draft(update, context):
    tg_chat_id = update.effective_chat.id
    async with _lock(tg_chat_id):
        match = _current(tg_chat_id)
        draft = drafts.get_active_draft(match['match_id']) if match else None
        if not draft:
            await _send(context, tg_chat_id, 'No active draft in this chat.')
            return
        drafts.record_message(draft['draft_id'], tg_chat_id, update.effective_message.message_id)
        actor = _actor(update)
        view, request_id = drafts.request_cancellation(draft['draft_id'], actor, await _admin(context, tg_chat_id, actor))
        if request_id:
            await _confirmation(context, tg_chat_id, draft['draft_id'], request_id, actor, cancellation=True)
        elif view and view['error']:
            await _send(context, tg_chat_id, view['error'], draft['draft_id'])
        await _publish(context, view)


async def _confirmation(context, tg_chat_id, draft_id, request_id, actor, cancellation=False):
    if cancellation:
        text = 'cancel the draft? All teams and draft messages will be removed.'
        buttons = [
            InlineKeyboardButton('✅ Confirm cancellation', callback_data=f'draftcancelconfirm_{request_id}'),
            InlineKeyboardButton('Keep draft', callback_data=f'draftcancelreject_{request_id}'),
        ]
    else:
        text = 'restart the draft? All current picks will be reset.'
        buttons = [
            InlineKeyboardButton('✅ Confirm restart', callback_data=f'draftrestartconfirm_{request_id}'),
            InlineKeyboardButton('Cancel', callback_data=f'draftrestartcancel_{request_id}'),
        ]
    await _send(context, tg_chat_id, f'<a href="tg://user?id={actor}">Requester</a>, {text}',
                draft_id, InlineKeyboardMarkup([buttons]))


async def draft_help(update, context):
    await _send(context, update.effective_chat.id,
                '<b>Captains and team draft</b>\n'
                '<code>/captains</code> or <code>/captains random</code> — random captains.\n'
                '<code>/captains @alice @bob</code> — choose two captains.\n'
                '<code>/captains random @alice @bob @charlie</code> — choose two from a list.\n'
                '<code>/capitans</code> is an alias for <code>/captains</code>.\n'
                'Only chat admins can start a draft. Players must register with the bot first.\n'
                'You can start with any number of players; missing captain candidates can join later.\n'
                '<code>/teams</code> — show and pin the teams.\n'
                '<code>/cancel_draft</code> or <b>Cancel draft</b> — request cancellation.\n'
                '<b>Restart draft</b> and <b>Cancel draft</b> are for admins and current captains. '
                'The requester must confirm; choosing Keep draft leaves the teams unchanged.')


async def draft_callback(update, context):
    query = update.callback_query
    parts = query.data.split('_')
    tg_chat_id = update.effective_chat.id
    try:
        action, identifier = parts[0], int(parts[1])
        request = None
        if action in ('draftrestartconfirm', 'draftrestartcancel'):
            request = drafts.get_restart_request(identifier)
        elif action in ('draftcancelconfirm', 'draftcancelreject'):
            request = drafts.get_cancel_request(identifier)
        draft_id = request['draft_id'] if request else identifier
        identity = drafts.get_draft(draft_id)
        if not identity or not identity['match'] or str(identity['match']['tg_chat_id']) != str(tg_chat_id):
            await _send(context, tg_chat_id, 'This draft button does not belong to this chat.')
            return
        async with _lock(tg_chat_id):
            actor = _actor(update)
            view = None
            error = None
            if action == 'draftpick' and len(parts) == 4:
                view = drafts.pick_player(draft_id, actor, int(parts[2]), int(parts[3]))
                error = view['error'] if view else None
            elif action == 'draftrefresh' and len(parts) == 2:
                view = drafts.sync_draft(draft_id)
            elif action in ('draftrestart', 'draftcancel') and len(parts) == 2:
                cancellation = action == 'draftcancel'
                create_request = drafts.request_cancellation if cancellation else drafts.request_restart
                view, request_id = create_request(draft_id, actor, await _admin(context, tg_chat_id, actor))
                error = view['error'] if view else None
                if request_id:
                    await _confirmation(context, tg_chat_id, draft_id, request_id, actor, cancellation)
            elif action in ('draftrestartconfirm', 'draftrestartcancel', 'draftcancelconfirm', 'draftcancelreject') and request and len(parts) == 2:
                is_admin = await _admin(context, tg_chat_id, actor)
                if action.startswith('draftcancel'):
                    view, result = drafts.finish_cancellation(identifier, actor, is_admin,
                                                              dismiss=action == 'draftcancelreject')
                else:
                    view, result = drafts.finish_restart(identifier, actor, is_admin,
                                                         cancel=action == 'draftrestartcancel')
                own = request['requested_by_id'] == actor
                # Another user's click must not remove the owner's confirmation buttons.
                if own and ('Only chat admins' not in result):
                    try:
                        await query.edit_message_text(text=result, reply_markup=None)
                    except TelegramError:
                        await _send(context, tg_chat_id, result, draft_id)
                else:
                    await _send(context, tg_chat_id, result, draft_id)
            else:
                await _send(context, tg_chat_id, 'This draft button is no longer valid.')
                return
            if error:
                await _send(context, tg_chat_id, error, draft_id)
            elif view is None and action not in ('draftrestartconfirm', 'draftrestartcancel', 'draftcancelconfirm', 'draftcancelreject'):
                await _send(context, tg_chat_id, 'This draft is no longer active for the current match.')
            await _publish(context, view)
    except (ValueError, IndexError):
        await _send(context, tg_chat_id, 'Invalid draft button. Please use /teams.')


async def sync_registration(context, tg_chat_id, match_id, removed_user_id=None):
    """Best-effort UI hook; delivery errors must not undo a registration/removal."""
    try:
        async with _lock(tg_chat_id):
            draft = drafts.get_active_draft(match_id)
            if not draft:
                return False
            view = drafts.sync_draft(draft['draft_id'])
            if not view:
                return False
            await _publish(context, view)
            return any(event['kind'] == 'left' and event['player']['user_id'] == removed_user_id
                       for event in view['events'])
    except (sqlite3.Error, TelegramError):
        logger.exception('Could not synchronize team draft with registration')
        return False


async def on_player_removed(context, tg_chat_id, match_id, removed_user_id):
    return await sync_registration(context, tg_chat_id, match_id, removed_user_id)


async def close_match_draft(context, tg_chat_id, match_id):
    """Close old keyboards when a match is cancelled or replaced by a new one."""
    async with _lock(tg_chat_id):
        draft = drafts.get_active_draft(match_id)
        if draft:
            drafts.cancel_draft(draft['draft_id'])
            await _cleanup(context, draft['draft_id'])
