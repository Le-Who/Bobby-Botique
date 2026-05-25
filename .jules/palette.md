## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2025-02-28 - Complete WAI-ARIA properties for tabs and dynamic badges
**Learning:** Custom tab components in this app lacked complete WAI-ARIA patterns, missing the explicit `id` mapping to `aria-labelledby` on tab panels. Also, dynamic badging elements like hidden-by-default error counters require `aria-live="polite"` and `aria-atomic="true"` so screen readers announce dynamic changes correctly.
**Action:** Ensure each tab button has an explicit `id` attribute that strictly maps to the `aria-labelledby` attribute of its corresponding `tabpanel`, and add `aria-live` and `aria-atomic` attributes to dynamically updated badges.
