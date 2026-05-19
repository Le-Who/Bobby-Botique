## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-19 - Accessible Dynamic Badging
**Learning:** Dynamic badging elements, like hidden-by-default error counters, require `aria-live="polite"` and `aria-atomic="true"` attributes so screen readers announce dynamic changes correctly.
**Action:** Ensure dynamic elements that change content dynamically use proper ARIA live regions to be announced by screen readers.
