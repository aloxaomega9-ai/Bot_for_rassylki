# Multi-user migration (phase 1)

## Scope
- The existing `main.py` remains the single-owner application until the replacement is tested.
- First release: one connected Telegram account per service user. The account table can support multiple accounts later, but the repository enforces one active account per user now.
- User-owned records are always queried with the Telegram owner ID.
- No deployment or production environment changes are part of this phase.

## Messaging safety
- Only send to chats where the account owner has permission and messages are expected/explicitly opted into.
- A chat appearing in the connected account's dialog list is not consent.
- No scraping recipients, unsolicited direct messages, or bypassing Telegram limits.
- Jobs snapshot only explicitly authorized target chats. Re-check authorization immediately before sending each message.
- The first prototype limits each job to 20 authorized chats and one active job per user.
- Provide visible pause/stop controls and cancellation audit records.
- Respect Telegram FloodWait and stop rather than attempting to evade platform limits.

## Session security
- Telegram session strings are account credentials. Never send them to users in bot messages, write them to logs, or commit them to Git.
- Encrypt sessions at rest with a Fernet key stored separately as a Render secret (`SESSION_ENCRYPTION_KEY`).
- Encryption at rest does not prevent the running service from accessing the session. Explain this clearly before account connection.
- Provide disconnect/revoke controls; disconnect clears the encrypted credential from the row.
- Do not request one-time codes or cloud passwords in ordinary support chats.

## Data model and service layer
- `User`: Telegram identity and account status.
- `ConnectedAccount`: encrypted session, owner and revocation state.
- `AuthorizedChat`: possible destination; it is ineligible until explicitly authorized.
- `BroadcastJob` / `BroadcastJobTarget`: owner-scoped jobs and a snapshot of approved target destinations.
- `multiuser.service.MultiUserService`: transaction-managed operations for registration, account connection/disconnection, chat authorization and job state.

## Database transactions and setup
Repository functions flush but do not commit. The service layer wraps each operation in its own transaction and session.
For a **new, empty database only**, initialize tables with:
```bash
ALLOW_SCHEMA_CREATE=1 python -m multiuser.schema
```
This bootstrap does not alter existing tables. Use Alembic migrations for future schema changes. Never run schema setup against a database containing valuable data without a backup and review.

## Tests
Install `dev-requirements.txt` and run `pytest`. Tests use an in-memory SQLite database; production runtime remains PostgreSQL/asyncpg.

## Rollout
1. Add isolated database/security primitives.
2. Add owner-scoped repository, service layer and tests.
3. Build a new multi-user bot entry point; do not replace `main.py` yet.
4. Verify tenant isolation, encryption, stop/cancel behavior, and authorization checks.
5. Only after explicit approval, configure Render and deploy the new entry point.
