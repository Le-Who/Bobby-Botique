## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-18 - Missing Dynamic ARIA States on Alerts and Live Regions
**Learning:** Found that dynamic elements like visual badges (e.g., error counters) and form validation feedback lacked semantic announcements for screen readers. They only functioned visually but omitted `aria-live` and `role="alert"`, causing screen reader users to miss critical real-time information or error changes.
**Action:** When working on dynamic visual badges (like error counters), always add `aria-live="polite"` so screen readers can announce changes as they happen. For form validation feedback or general error messages, add `role="alert"` so they are read out immediately to the user upon appearance.
