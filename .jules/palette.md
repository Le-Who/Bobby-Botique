## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-18 - Missing ARIA attributes for dynamic state changes and error messages
**Learning:** Found instances where error messages (`.error-msg` in login template) lacked proper ARIA roles to alert screen readers, and dynamic notification counters (e.g., `#error-badge` in dashboard) lacked `aria-live` attributes, preventing screen readers from announcing updates automatically.
**Action:** Always add `role="alert"` to containers displaying form/system errors. For dynamic visual indicators like unread counts or error badges that update asynchronously, ensure `aria-live="polite"` (or `assertive` if critical) is present so changes are announced properly.
