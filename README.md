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
- A departing captain is replaced randomly by a remaining teammate. If their
  team is empty, the draft is cancelled. The affected captain can pick a
  replacement when a free player becomes available; announcements go to the group.
- `/teams` reposts and pins the current draft, replacing the old message.
  Admin-only `/cancel_draft` cancels it and attempts to delete all tracked draft
  messages. The bot needs permission to pin and delete messages in the group.
- Draft state is stored in SQLite and continues after a bot restart on the next
  registration change or refresh. Its tables are created automatically using
  `migrations/add_team_drafts.sql`; no manual migration is needed for this feature.

Run offline tests (Telegram and Mistral calls are mocked):

```text
pipenv run python -m unittest discover -s tests -v
```
