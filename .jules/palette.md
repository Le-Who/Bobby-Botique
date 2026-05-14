## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2025-02-23 - Dynamic Badges Accessibility
**Learning:** Hidden-by-default dynamic badging elements (like error counters) were not correctly announced by screen readers when their content or visibility changed dynamically.
**Action:** When building or enhancing dynamic badging elements, always include `aria-live="polite"` and `aria-atomic="true"` attributes so that screen readers announce dynamic changes correctly.
