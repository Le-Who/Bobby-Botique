## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.
## 2025-04-25 - ID tracking for tab buttons
**Learning:** Found that adding explicit `id` attributes to tab buttons is essential for the `aria-labelledby` attributes of their corresponding `tabpanel` elements to properly link up to them.
**Action:** When adding accessibility to WAI-ARIA tab components, ensure that not only do tabs have `role="tab"` and panels have `role="tabpanel"`, but there is a clear ID mapping so `aria-labelledby` connects the panel back to its originating tab button ID.
