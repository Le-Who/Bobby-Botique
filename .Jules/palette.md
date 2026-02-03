## 2024-05-21 - [Preventing Accidental Data Loss in Command Interfaces]
**Learning:** Users often explore CLI/bot commands by running them without arguments to see help/status. Commands that perform destructive actions (like clearing settings) when run without arguments are a major UX trap.
**Action:** Always implement a "show status/help" behavior for no-argument invocations of configuration commands. Require explicit keywords (like `clear`, `reset`) for destructive actions.
