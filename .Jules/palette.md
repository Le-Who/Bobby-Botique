## 2025-02-18 - Reusing Command Logic in Callbacks
**Learning:** The codebase uses a `DummyUpdate` wrapper class to reuse existing `CommandHandler` logic within `CallbackQueryHandler` functions. This prevents code duplication and ensures that clicking a button (e.g., "New Chat") performs exactly the same action as typing the command (`/newchat`).
**Action:** When adding new interactive buttons that mirror existing commands, wrap `query.message` and `query.from_user` in a `DummyUpdate` object and call the existing command function directly.

## 2025-02-18 - Live Menu Updates
**Learning:** Extracting the menu content generation (text + markup) into a helper function (e.g., `get_start_menu_content`) allows both the initial command handler (`/start`) and interaction callbacks (`toggle_search`) to share the exact same layout logic. This enables "live" updates of the menu state (like toggles) using `edit_message_text` without code duplication.
**Action:** For stateful menus, separate the "rendering" logic from the "sending" logic.
