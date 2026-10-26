# CUEFA Football registration telegram bot

This is my small project to automate registration for our amateur football matches.

Adding another player:

- Click **Register ➕ 1️⃣** in the game chat. The bot still sends the player buttons
  in a private message; you can also send `@username` there after opening the menu.
  A reply to a particular menu uses that menu's game chat. Without a reply, the
  latest menu is used. Open the menu again after a bot restart or for a new match.
- Username entry is available only in the bot's private chat after opening the
  **Register ➕ 1️⃣** menu. Messages in the game chat do not register another player.
- Both players must already be registered with `/start` in the bot's private chat.
  Username lookup uses the stored nickname, ignoring letter case. A removed or
  unknown user must register again; Bot API cannot resolve a private user ID from
  a username alone.
- The invited player receives **Confirm** and **Quit** buttons. They have four
  hours to confirm. Existing invitation limits and the distinction between a
  chat member and an external +1 still apply.
  On confirmation, the private message shows the confirmed status and match
  date. The **Confirm** button disappears while **Quit** remains available.
  Expired, cancelled, or outdated invitations show an explanation instead of
  a success message.
- To cancel an invitation, click **Remove ➕ 1️⃣** in the game chat and confirm
  the removal in the bot's private message. If you invited both a chat member
  and an external guest, select which player to remove. Your own registration
  and other users' invitations are preserved.

Captains and team draft:

- A chat admin can start with `/captains` (alias `/capitans`) or
  `/captains random`. Use `/captains @alice @bob` to name captains, or
  `/captains random @alice @bob @charlie` to choose from specific candidates.
- Start at any time for the current match, even with zero or one registered
  player. The draft waits until two eligible captains are available. Named
  candidates must be registered with the bot; they can join the match later.
- Captains alternate picks; the first turn is random. The first 14 registered
  players are eligible, including pending invitations. Others remain reserves.
  The last ordinary pick can be assigned automatically, without a timer.
- The draft has one pinned message. Pinning and player departure notifications
  use ordinary notifications; restart confirmation requests are silent.
- **Restart draft** is always visible. Only chat admins and current captains
  can use it. The requester must confirm before picks are reset. Old buttons
  and outdated confirmations cannot reset or change the current draft.
- **Cancel draft** is also always visible and restricted to chat admins and
  current captains. It asks for **Confirm cancellation / Keep draft** in the
  group. Only the requester can confirm; permissions are checked again before
  the draft and its tracked messages are removed. Old confirmations are rejected.
- A departing captain is replaced randomly by a remaining teammate. If their
  team is empty, the draft is cancelled. The affected captain can pick a
  replacement when a free player becomes available; announcements go to the group.
- `/teams` reposts and pins the current draft, replacing the old message.
  `/cancel_draft` opens the same confirmation for chat admins and current captains.
  The bot needs permission to pin and delete messages in the group.
- Draft state is stored in SQLite and continues after a bot restart on the next
  registration change or refresh. Its tables are created automatically using
  `migrations/add_team_drafts.sql`; no manual migration is needed for this feature.
- The Telegram command menu updates automatically when the bot starts, including
  captain selection examples. `/draft_help` explains manual selection, random
  selection, candidates, and restart/cancellation permissions.

Run offline tests (Telegram and Mistral calls are mocked):

```text
pipenv run python -m unittest discover -s tests -v
```

Registration schedule:

- `/start_repeating_job Football monday 10:00 wednesday 19:00` opens registration
  every Monday at 10:00 for the next Wednesday game at 19:00, using `Europe/Prague`.
  The calendar schedule keeps the same local time through summer/winter clock changes.
- Schedules are restored from `Chats` when the bot restarts. Deploy and restart
  the updated bot once to replace the old interval jobs; no database migration or
  reconfiguration of chats is required. Existing matches and registrations are not rewritten.
- `/get_jobs` displays upcoming runs in Prague time, including the UTC offset.
- For a schedule inside the clock-change hour, a missing spring 02:30 runs at
  03:30 that day; an autumn 02:30 runs on its second occurrence (standard time).
- Invitation confirmation still expires after four elapsed hours, rather than
  a calendar interval. These one-off invitation timers are not restored after a restart.
