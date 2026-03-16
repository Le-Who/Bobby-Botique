## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2026-03-16 - Importance of ARIA Live Regions and Alerts for Dynamic States
**Learning:** Found that dynamic error states (like the `error-badge` in the dashboard or login error messages) are visually conveyed but not announced to screen readers by default. Missing WAI-ARIA properties mean users are unaware when a login fails or when new background errors are polled.
**Action:** Use `role="alert"` for important error messages that require immediate attention (like form validations), and use `aria-live="polite"` for dynamic visual badges (like error counters) so they are announced by screen readers without aggressively interrupting the user.
