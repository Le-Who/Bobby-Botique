## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-20 - Ensure tab components have strict ID and aria-labelledby mapping
**Learning:** Custom tab components in this codebase often lack complete WAI-ARIA patterns, such as mapping tab button `id` attributes to `aria-labelledby` attributes on tabpanels, and using `aria-live` for dynamic badging elements.
**Action:** Ensure each tab button has an explicit `id` attribute that maps to the `aria-labelledby` attribute of its corresponding `tabpanel`, and add `aria-live="polite"` and `aria-atomic="true"` to dynamic badges.
