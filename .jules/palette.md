## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-18 - Missing ARIA Labels on Tab Panels and Badges
**Learning:** Found custom tab panels lacking `aria-labelledby` linking back to their tab buttons, and dynamic error badges lacking `aria-live="polite"` and `aria-atomic="true"`, preventing screen readers from announcing dynamic changes and tab content correctly.
**Action:** Ensure every `role="tab"` button has an explicit `id` that strictly maps to the `aria-labelledby` attribute of its corresponding `tabpanel`. Ensure dynamic badging elements have both `aria-live="polite"` and `aria-atomic="true"` attributes.
