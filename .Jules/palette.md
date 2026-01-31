## 2025-02-18 - Reusing Command Logic in Callbacks
**Learning:** The codebase uses a `DummyUpdate` wrapper class to reuse existing `CommandHandler` logic within `CallbackQueryHandler` functions. This prevents code duplication and ensures that clicking a button (e.g., "New Chat") performs exactly the same action as typing the command (`/newchat`).
**Action:** When adding new interactive buttons that mirror existing commands, wrap `query.message` and `query.from_user` in a `DummyUpdate` object and call the existing command function directly.
