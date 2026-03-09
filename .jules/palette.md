## 2024-05-26 - Missing Keyboard Focus States
**Learning:** The entire application dashboard and administrative UI lacked visible focus indicators for keyboard navigation, making it functionally unusable for users relying on keyboard input or screen readers.
**Action:** Always include a global `:focus-visible` CSS rule for interactive elements, mapping it to the primary accent color or standard outline behavior. Unlike `:focus`, `:focus-visible` provides accessibility without compromising mouse/touch click styles.
