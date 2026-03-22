## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-24 - Missing WAI-ARIA Linkage on Custom Tab Panels
**Learning:** Found that custom tab navigations (like in `dashboard.html`) often lack explicit linkage between the tab controls and the tab panels using `aria-labelledby`. Additionally, dynamic status counters, like error badges, lack `aria-live="polite"` which prevents screen readers from announcing real-time updates.
**Action:** When working with dynamic UI elements or custom tabs, always ensure complete WAI-ARIA implementation: add `id` on tabs, link them using `aria-labelledby` on panels, and add `aria-live` to dynamically updating text like error counters.
