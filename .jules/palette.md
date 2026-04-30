## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2026-04-30 - Incomplete WAI-ARIA Patterns on Dynamic Badges
**Learning:** Found a pattern where dynamic badging elements, like the hidden-by-default error counter in `dashboard.html`, lack both the `aria-live` and `aria-atomic` attributes, meaning screen readers are not alerted when these dynamic changes occur.
**Action:** When building or enhancing dynamic badging elements, ensure they include `aria-live="polite"` and `aria-atomic="true"` attributes so that screen readers announce the dynamic updates correctly.
