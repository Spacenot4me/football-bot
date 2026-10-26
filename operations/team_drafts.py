"""Persistent draft transitions. No Telegram calls; every mutation is transactional."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import random

from constants import MAX_PLAYERS
from operations.common import connect_db


def ensure_draft_tables():
    schema = Path(__file__).resolve().parents[1] / 'migrations' / 'add_team_drafts.sql'
    with closing(connect_db()) as conn:
        conn.executescript(schema.read_text(encoding='utf-8'))


@contextmanager
def transaction():
    ensure_draft_tables()
    with closing(connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        yield conn


def _draft(conn, draft_id):
    row = conn.execute('SELECT * FROM Team_Drafts WHERE draft_id = ?', (draft_id,)).fetchone()
    return dict(row) if row else None


def _match(conn, match_id):
    row = conn.execute('''
        SELECT m.match_id, m.datetime, m.chat_id, c.chat_id AS tg_chat_id
        FROM Matches m JOIN Chats c ON c.id = m.chat_id WHERE m.match_id = ?
    ''', (match_id,)).fetchone()
    return dict(row) if row else None


def _is_current(conn, match):
    return match and conn.execute(
        'SELECT MAX(match_id) FROM Matches WHERE chat_id = ?', (match['chat_id'],)
    ).fetchone()[0] == match['match_id']


def _players(conn, match_id):
    return [dict(row) for row in conn.execute('''
        SELECT mr.user_id, mr.registration_id, mr.confirmed, mr.priority, u.nickname, u.name
        FROM Match_Registration mr
        JOIN Users u ON u.user_id = mr.user_id
        JOIN Users inviter ON inviter.user_id = mr.registered_by_id
        WHERE mr.match_id = ? ORDER BY mr.priority, mr.registered_at, mr.registration_id
    ''', (match_id,))]


def _picks(conn, draft_id):
    return [dict(row) for row in conn.execute('''
        SELECT * FROM Team_Picks WHERE draft_id = ? AND state = 'active'
        ORDER BY pick_number, pick_id
    ''', (draft_id,))]


def _main(players):
    # A corrupt/legacy duplicate must never result in a player on both teams.
    unique = {p['user_id']: p for p in reversed(players[:MAX_PLAYERS])}
    return list(reversed(list(unique.values())))


def _view(conn, d, events=None, error=None):
    players = _players(conn, d['match_id'])
    main = _main(players)
    picks = _picks(conn, d['draft_id'])
    occupied = {p['user_id'] for p in picks} | {d['captain_a_id'], d['captain_b_id']}
    free = [p for p in main if p['user_id'] not in occupied]
    first = d['first_pick']
    turn = first if d['draft_pick_count'] % 2 == 0 else ('B' if first == 'A' else 'A')
    return {'draft': dict(d), 'match': _match(conn, d['match_id']), 'players': players,
            'main': main, 'picks': picks, 'free': free, 'turn': turn,
            'events': events or [], 'error': error}


def _save(conn, d):
    d['revision'] += 1
    columns = ('captain_a_id', 'captain_b_id', 'first_pick', 'status', 'revision',
               'draft_pick_count', 'replacement_a', 'replacement_b', 'roster_snapshot')
    conn.execute('UPDATE Team_Drafts SET ' + ', '.join(c + ' = ?' for c in columns)
                 + ' WHERE draft_id = ?', tuple(d[c] for c in columns) + (d['draft_id'],))
    for table in ('Team_Draft_Restart_Requests', 'Team_Draft_Cancel_Requests'):
        conn.execute(f'''UPDATE {table} SET status = 'stale'
                        WHERE draft_id = ? AND status = 'pending' AND draft_revision != ?''',
                     (d['draft_id'], d['revision']))


def _status(conn, d):
    if d['status'] == 'cancelled':
        return
    if d['captain_a_id'] is None or d['captain_b_id'] is None:
        d['status'] = 'waiting_for_players'
    elif d['replacement_a'] or d['replacement_b']:
        d['status'] = 'needs_replacement'
    else:
        d['status'] = 'drafting' if _view(conn, d)['free'] else 'done'


def _cancel(conn, d):
    d['status'] = 'cancelled'
    conn.execute('DELETE FROM Team_Picks WHERE draft_id = ?', (d['draft_id'],))


def _sync(conn, d):
    """Reconcile using an on-disk snapshot; catches changes while bot was offline."""
    if d['status'] == 'cancelled':
        return []
    players = _players(conn, d['match_id'])
    snapshot = json.dumps(players, sort_keys=True)
    if snapshot == d['roster_snapshot']:
        return []
    previous = {p['user_id']: p for p in json.loads(d['roster_snapshot'])}
    all_ids = {p['user_id'] for p in players}
    main = _main(players)
    ids = {p['user_id'] for p in main}
    events = [{'player': p, 'kind': 'left'} for uid, p in previous.items() if uid not in all_ids]
    d['roster_snapshot'] = snapshot
    if d['status'] == 'waiting_for_players':
        mode = d['captain_mode']
        candidates = json.loads(d['candidate_user_ids'])
        available = [p['user_id'] for p in main if mode == 'random_all' or p['user_id'] in candidates]
        if mode == 'manual':
            selected = candidates if all(uid in ids for uid in candidates) else []
        else:
            selected = random.sample(available, 2) if len(available) >= 2 else []
        if len(selected) == 2:
            d['captain_a_id'], d['captain_b_id'] = selected
    else:
        picks = _picks(conn, d['draft_id'])
        for pick in picks:
            if pick['user_id'] not in ids:
                conn.execute("UPDATE Team_Picks SET state = 'left' WHERE pick_id = ?", (pick['pick_id'],))
                d['replacement_' + pick['team'].lower()] += 1
                if pick['user_id'] in all_ids:
                    events.append({'player': previous.get(pick['user_id'], {'user_id': pick['user_id']}),
                                   'kind': 'roster'})
        for team in ('A', 'B'):
            key = 'captain_' + team.lower() + '_id'
            if d[key] not in ids:
                old = d[key]
                teammates = [p for p in _picks(conn, d['draft_id']) if p['team'] == team]
                if not teammates:
                    _cancel(conn, d)
                    events.append({'kind': 'cancelled', 'reason': 'No teammate is available to replace the captain.'})
                    break
                new = random.choice(teammates)
                d[key] = new['user_id']
                conn.execute('DELETE FROM Team_Picks WHERE pick_id = ?', (new['pick_id'],))
                d['replacement_' + team.lower()] += 1
                events.append({'kind': 'captain', 'old_id': old, 'new_id': d[key], 'team': team})
    _status(conn, d)
    _save(conn, d)
    return events


def _load_current(conn, draft_id):
    d = _draft(conn, draft_id)
    if not d or d['status'] == 'cancelled' or not _is_current(conn, _match(conn, d['match_id'])):
        return None
    return d


def create_draft(match_id, captain_mode='random_all', candidates=()):
    candidates = list(candidates)
    if captain_mode not in ('manual', 'random_all', 'random_list'):
        raise ValueError('Invalid captain mode')
    if captain_mode != 'random_all' and (len(set(candidates)) != len(candidates) or len(candidates) < 2):
        raise ValueError('Choose at least two different candidates.')
    if captain_mode == 'manual' and len(candidates) != 2:
        raise ValueError('Choose two different captains.')
    with transaction() as conn:
        if not _is_current(conn, _match(conn, match_id)):
            raise ValueError('There is no current match in this chat.')
        existing = conn.execute("SELECT draft_id FROM Team_Drafts WHERE match_id = ? AND status != 'cancelled'",
                                (match_id,)).fetchone()
        if existing:
            d = _draft(conn, existing['draft_id'])
            return _view(conn, d, _sync(conn, d), 'A draft already exists. Use Restart draft to reset it.')
        for uid in candidates:
            if not conn.execute('SELECT 1 FROM Users WHERE user_id = ?', (uid,)).fetchone():
                raise ValueError('A captain candidate is not registered in the bot.')
        cursor = conn.execute('''INSERT INTO Team_Drafts
            (match_id, captain_mode, candidate_user_ids, first_pick, status)
            VALUES (?, ?, ?, ?, 'waiting_for_players')''',
            (match_id, captain_mode, json.dumps(candidates), random.choice(('A', 'B'))))
        d = _draft(conn, cursor.lastrowid)
        _sync(conn, d)
        return _view(conn, d)


def get_active_draft(match_id):
    ensure_draft_tables()
    with closing(connect_db()) as conn:
        row = conn.execute("SELECT * FROM Team_Drafts WHERE match_id = ? AND status != 'cancelled'",
                           (match_id,)).fetchone()
        return dict(row) if row else None


def get_draft(draft_id):
    ensure_draft_tables()
    with closing(connect_db()) as conn:
        d = _draft(conn, draft_id)
        return {'draft': d, 'match': _match(conn, d['match_id'])} if d else None


def forget_message(tg_chat_id, message_id):
    with transaction() as conn:
        conn.execute('DELETE FROM Team_Draft_Messages WHERE tg_chat_id = ? AND message_id = ?',
                     (str(tg_chat_id), message_id))


def sync_draft(draft_id):
    with transaction() as conn:
        d = _load_current(conn, draft_id)
        return _view(conn, d, _sync(conn, d)) if d else None


def _add_pick(conn, d, user_id, team, kind):
    number = conn.execute('SELECT COALESCE(MAX(pick_number), 0) + 1 FROM Team_Picks WHERE draft_id = ?',
                          (d['draft_id'],)).fetchone()[0]
    conn.execute('''INSERT INTO Team_Picks (draft_id, user_id, team, kind, pick_number)
                    VALUES (?, ?, ?, ?, ?)''', (d['draft_id'], user_id, team, kind, number))
    if kind == 'draft':
        d['draft_pick_count'] += 1
    else:
        d['replacement_' + team.lower()] -= 1


def pick_player(draft_id, actor_id, user_id, revision):
    with transaction() as conn:
        d = _load_current(conn, draft_id)
        if not d:
            return None
        events = _sync(conn, d)
        view = _view(conn, d, events)
        team = 'A' if actor_id == d['captain_a_id'] else 'B' if actor_id == d['captain_b_id'] else None
        error = None
        if revision != d['revision']:
            error = 'This button is outdated. Please use the current draft message.'
        elif not team or d['status'] not in ('drafting', 'needs_replacement'):
            error = 'Only the captain whose turn it is can pick a player.'
        elif d['status'] == 'needs_replacement' and not d['replacement_' + team.lower()]:
            error = 'The other team is waiting for a replacement.'
        elif d['status'] == 'drafting' and view['turn'] != team:
            error = "It is the other captain's turn."
        elif user_id not in {p['user_id'] for p in view['free']}:
            error = 'This player is no longer available.'
        if error:
            view['error'] = error
            return view
        kind = 'replacement' if d['status'] == 'needs_replacement' else 'draft'
        _add_pick(conn, d, user_id, team, kind)
        _status(conn, d)
        after = _view(conn, d)
        # No timer: only finish the last ordinary pick when it is unambiguous.
        if d['status'] == 'drafting' and len(after['free']) == 1:
            counts = {t: 1 + sum(p['team'] == t for p in after['picks']) for t in ('A', 'B')}
            turn = after['turn']
            if counts[turn] <= counts['B' if turn == 'A' else 'A']:
                _add_pick(conn, d, after['free'][0]['user_id'], turn, 'draft')
                _status(conn, d)
        _save(conn, d)
        return _view(conn, d, events)


def _request_action(draft_id, actor_id, is_admin, action):
    table = _request_table(action)
    with transaction() as conn:
        d = _load_current(conn, draft_id)
        if not d:
            return None, None
        events = _sync(conn, d)
        view = _view(conn, d, events)
        if d['status'] == 'cancelled' or not (is_admin or actor_id in (d['captain_a_id'], d['captain_b_id'])):
            view['error'] = f'Only chat admins and current captains can {action} the draft.'
            return view, None
        cursor = conn.execute(f'''INSERT INTO {table}
            (draft_id, requested_by_id, draft_revision) VALUES (?, ?, ?)''',
            (draft_id, actor_id, d['revision']))
        return view, cursor.lastrowid


def _request_table(action):
    # Table names are fixed by code, never supplied by a callback or user input.
    return {'restart': 'Team_Draft_Restart_Requests', 'cancel': 'Team_Draft_Cancel_Requests'}[action]


def request_restart(draft_id, actor_id, is_admin):
    return _request_action(draft_id, actor_id, is_admin, 'restart')


def request_cancellation(draft_id, actor_id, is_admin):
    return _request_action(draft_id, actor_id, is_admin, 'cancel')


def _get_request(request_id, action):
    ensure_draft_tables()
    with closing(connect_db()) as conn:
        row = conn.execute(f'SELECT * FROM {_request_table(action)} WHERE request_id = ?', (request_id,)).fetchone()
        return dict(row) if row else None


def get_restart_request(request_id):
    return _get_request(request_id, 'restart')


def get_cancel_request(request_id):
    return _get_request(request_id, 'cancel')


def _finish_action(request_id, actor_id, is_admin, dismiss, action):
    table = _request_table(action)
    noun = 'restart' if action == 'restart' else 'cancellation'
    with transaction() as conn:
        row = conn.execute(f'SELECT * FROM {table} WHERE request_id = ?', (request_id,)).fetchone()
        if not row:
            return None, f'This {noun} request is no longer valid.'
        request = dict(row)
        if request['requested_by_id'] != actor_id:
            return None, f'Only the person who requested this {noun} can confirm or cancel it.'
        d = _load_current(conn, request['draft_id'])
        if not d:
            return None, f'This {noun} request is no longer valid.'
        events = _sync(conn, d)
        # Re-read: synchronization may already have invalidated the request.
        status = conn.execute(f'SELECT status FROM {table} WHERE request_id = ?', (request_id,)).fetchone()[0]
        view = _view(conn, d, events)
        if status != 'pending' or request['draft_revision'] != d['revision'] or d['status'] == 'cancelled':
            return view, f'This {noun} request is no longer valid. Please request a {noun} again.'
        if dismiss:
            conn.execute(f"UPDATE {table} SET status = 'cancelled' WHERE request_id = ?", (request_id,))
            return view, 'Restart cancelled.' if action == 'restart' else 'Cancellation dismissed. Draft kept.'
        if not (is_admin or actor_id in (d['captain_a_id'], d['captain_b_id'])):
            return view, f'Only chat admins and current captains can {action} the draft.'
        conn.execute(f"UPDATE {table} SET status = 'confirmed' WHERE request_id = ?", (request_id,))
        if action == 'cancel':
            _cancel(conn, d)
            _save(conn, d)
            return _view(conn, d, events), 'Draft cancelled.'
        conn.execute('DELETE FROM Team_Picks WHERE draft_id = ?', (d['draft_id'],))
        d['draft_pick_count'] = d['replacement_a'] = d['replacement_b'] = 0
        d['first_pick'] = random.choice(('A', 'B'))
        _status(conn, d)
        _save(conn, d)
        return _view(conn, d, events), 'Draft restarted.'


def finish_restart(request_id, actor_id, is_admin=False, cancel=False):
    return _finish_action(request_id, actor_id, is_admin, cancel, 'restart')


def finish_cancellation(request_id, actor_id, is_admin=False, dismiss=False):
    return _finish_action(request_id, actor_id, is_admin, dismiss, 'cancel')


def cancel_draft(draft_id):
    with transaction() as conn:
        d = _draft(conn, draft_id)
        if d:
            _cancel(conn, d)
            _save(conn, d)


def record_message(draft_id, tg_chat_id, message_id, main=False):
    with transaction() as conn:
        conn.execute('''INSERT OR IGNORE INTO Team_Draft_Messages (draft_id, tg_chat_id, message_id)
                        VALUES (?, ?, ?)''', (draft_id, str(tg_chat_id), message_id))
        if main:
            conn.execute('UPDATE Team_Drafts SET main_message_id = ? WHERE draft_id = ?', (message_id, draft_id))


def get_messages(draft_id):
    ensure_draft_tables()
    with closing(connect_db()) as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM Team_Draft_Messages WHERE draft_id = ?', (draft_id,))]
