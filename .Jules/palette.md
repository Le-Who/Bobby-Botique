## 2025-02-18 - Reusing Command Logic in Callbacks
**Learning:** The codebase uses a `DummyUpdate` wrapper class to reuse existing `CommandHandler` logic within `CallbackQueryHandler` functions. This prevents code duplication and ensures that clicking a button (e.g., "New Chat") performs exactly the same action as typing the command (`/newchat`).
**Action:** When adding new interactive buttons that mirror existing commands, wrap `query.message` and `query.from_user` in a `DummyUpdate` object and call the existing command function directly.

## 2025-02-18 - Live Menu Updates
**Learning:** Extracting the menu content generation (text + markup) into a helper function (e.g., `get_start_menu_content`) allows both the initial command handler (`/start`) and interaction callbacks (`toggle_search`) to share the exact same layout logic. This enables "live" updates of the menu state (like toggles) using `edit_message_text` without code duplication.
**Action:** For stateful menus, separate the "rendering" logic from the "sending" logic.

## 2025-02-18 - Selection Feedback
**Learning:** When a user selects an item from a list (like a model), updating the list in-place to show the new selection (e.g., moving the checkmark) is superior to sending a new confirmation message. It keeps the context and allows for quick corrections.
**Action:** When implementing selection menus, use `edit_message_text` with updated markup to show the new state instead of replacing the menu with a text confirmation.
## 2024-05-21 - [Preventing Accidental Data Loss in Command Interfaces]
**Learning:** Users often explore CLI/bot commands by running them without arguments to see help/status. Commands that perform destructive actions (like clearing settings) when run without arguments are a major UX trap.
**Action:** Always implement a "show status/help" behavior for no-argument invocations of configuration commands. Require explicit keywords (like `clear`, `reset`) for destructive actions.
