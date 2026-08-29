## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-24 - Missing ARIA Alert Roles on Error Messages
**Learning:** Found a pattern across forms (`login.html` and `natal_form.html`) where error messages are either conditionally rendered or dynamically unhidden, but they lack the `role="alert"` attribute. This prevents screen readers from proactively announcing the error to the user when it occurs, forcing them to manually discover the validation issue.
**Action:** Always add `role="alert"` (or `aria-live="assertive"`) to error message containers, and set `aria-invalid="true"` on the corresponding invalid inputs when validation fails.
