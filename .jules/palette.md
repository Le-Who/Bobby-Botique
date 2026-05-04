## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-18 - Missing ARIA Live Regions on Dynamic Badging Elements
**Learning:** Found that dynamic UI elements (like hidden-by-default error counters/badges) that update asynchronously do not announce their changes to screen readers because they lack ARIA live region attributes.
**Action:** When building or enhancing dynamic badging elements, ensure they have both `aria-live="polite"` and `aria-atomic="true"` attributes so screen readers announce dynamic changes correctly. Also, ensure tab buttons and tabpanels are properly linked via `id` and `aria-labelledby` attributes.
