## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-06-10 - Full ARIA Support for Custom Tab Controls
**Learning:** Custom-built tabs lacking native button usage must provide keyboard access by explicitly setting `tabindex="0"` and adding keydown event listeners to handle the `Enter` or `Space` keys.
**Action:** When creating custom tabs with non-interactive elements like `<div>`, explicitly define keyboard events alongside ARIA states to ensure full WAI-ARIA compliance.
