## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-07-22 - Login Form Accessibility and Feedback
**Learning:** The login page's error messages lacked screen reader announcements (`role="alert"`, `aria-live="assertive"`) and the submit button lacked feedback upon submission, leaving users uncertain if the action was processing.
**Action:** Always add proper ARIA roles to dynamic error messages and include visual feedback (like changing text to "Signing In..." or adding opacity) and disabling the button to prevent multiple submissions.
