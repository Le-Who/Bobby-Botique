## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-19 - Missing ARIA Attributes on Tabs and Dynamic Badges
**Learning:** Custom tab panels often lack `aria-labelledby` linking back to the tab control's `id`, preventing screen readers from understanding their relationship. Furthermore, hidden-by-default dynamic elements (like error counters) fail to announce updates without `aria-live="polite"` and `aria-atomic="true"`.
**Action:** Always map tab buttons with explicit `id` attributes to `aria-labelledby` on their corresponding `tabpanel` elements. Ensure dynamic badging elements include both `aria-live="polite"` and `aria-atomic="true"` so state changes are properly announced.
