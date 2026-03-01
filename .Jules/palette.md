## 2025-05-27 - [Add Back navigation buttons to menus]
**Learning:** Users often get stuck in deep menus (like Model selection or Role selection) without a clear path back to the main menu, forcing them to re-issue the start command. Adding explicit "Back" buttons significantly improves flow.
**Action:** Always include a "Back" or "Up" navigation option in sub-menus that replace the main menu content.

## 2025-05-28 - [Dead-end Action Screens]
**Learning:** Action screens that display static instructions (e.g., "Enter new name") via `edit_message_text` often create dead ends if they replace the previous menu without providing a way to cancel or go back. This forces the user to scroll up or restart the flow.
**Action:** Always include a "Back" or "Cancel" button in instruction screens that replace menu content, allowing users to abort the action and return to the previous context.

## 2025-05-29 - [Confirm Destructive Actions]
**Learning:** Destructive actions like "Clear All Documents" executed immediately on a single click can lead to significant user frustration and data loss. Users expect a safety net.
**Action:** Implement a two-step confirmation process for all bulk delete or irreversible actions, replacing the button with a clear "Confirm/Cancel" choice.

## 2025-06-03 - [Global Focus Visible for Keyboard Accessibility]
**Learning:** Custom components and embedded CSS styles often miss standard keyboard focus indicators, making the application inaccessible to users navigating via keyboard. A global `:focus-visible` utility alongside component-specific adjustments is required to ensure consistent focus rings without negatively impacting the mouse user experience.
**Action:** Always include a global `:focus-visible` utility in the base CSS and ensure any custom interactive elements (like tabs or topbar links) and embedded CSS (like on login pages) also define clear `:focus-visible` states.
