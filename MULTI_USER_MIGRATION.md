# Multi-user migration (phase 1)

## Scope
- The existing `main.py` remains the single-owner application until the replacement is tested.
- First release: one connected Telegram account per service user. The database schema uses a separate account table so multiple accounts per user can be added later.
- Each user's settings, connected account, authorized destinations, and jobs must be isolated by `telegram_user_id` / account ownership.
- No deployment or production environment changes are part of this phase.

## Messaging safety requirements
- Only send to chats where the account owner has permission and where messages are expected/explicitly opted into.
- No importing arbitrary members, scraping recipients, unsolicited direct messages, or bypassing Telegram rate limits.
- Provide visible pause/stop controls, per-user quotas, and an audit trail for job creation and cancellation.
- The sender must re-check authorization immediately before each send. A chat being present in the account's dialog list is not consent.
- Respect Telegram FloodWait and stop rather than trying to evade platform limits.

## Session security
- Telegram session strings are account credentials. Never send them back in bot messages, write them to logs, or commit them to Git.
- Encrypt sessions at rest with a Fernet key stored separately as a Render secret (for example `SESSION_ENCRYPTION_KEY`).
- Encryption at rest does not prevent the running service from accessing the session. Explain this clearly before a user connects an account.
- Do not collect one-time login codes or cloud passwords in ordinary support conversations. Prefer a safer authorization flow and provide disconnect/revoke controls.

## Data model
- `User`: Telegram user identity and account status.
- `ConnectedAccount`: encrypted session and owner; schema permits future multiple accounts.
- `AuthorizedChat`: destinations explicitly approved by the account owner, with authorization state.
- `BroadcastJob`: owner/account-scoped job metadata and lifecycle state.

## Rollout
1. Add isolated database/security primitives (this phase).
2. Add repository/service layer and tests.
3. Build a new multi-user bot entry point; do not replace `main.py` yet.
4. Verify tenant isolation, encryption, stop/cancel behavior, and authorization checks.
5. Only after explicit approval, configure Render and deploy the new entry point.
