## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-19 - Missing ARIA Relationships and Live Regions in Custom Navigations
**Learning:** Found a recurring pattern where dynamic badging and custom tab panels lack critical ARIA attributes (like `aria-labelledby`, `aria-live`, and `aria-atomic`), preventing screen readers from accurately reading panel context and failing to announce dynamic changes to badges.
**Action:** Always verify ARIA patterns are complete when building or modifying dynamic custom components. Use `aria-labelledby` on `tabpanel` to associate it with its `tab`, and apply `aria-live="polite"` and `aria-atomic="true"` to dynamic counters/badges so updates are announced reliably.
