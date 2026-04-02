## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2026-04-02 - Missing Connective ARIA Attributes for Dynamic Elements
**Learning:** Dynamic UI elements like counters, badges, and validation error messages frequently lack the ARIA attributes needed for screen readers to announce changes. This includes `aria-labelledby` linking panels to tabs, `aria-live="polite"` for dynamic content updates, and `role="alert"` for form feedback.
**Action:** Whenever introducing or modifying dynamic UI components, ensure that custom tabs are explicitly linked with `aria-labelledby`, visual updates use `aria-live`, and critical errors rely on `role="alert"`.
