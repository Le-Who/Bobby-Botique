## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-24 - WAI-ARIA on Dynamic Badges
**Learning:** Dynamic badging elements like hidden-by-default error counters need explicit ARIA live attributes to ensure screen readers announce dynamic changes.
**Action:** Add aria-live="polite" and aria-atomic="true" to dynamic badges and ensure tab navigation elements map ids correctly via aria-labelledby.
