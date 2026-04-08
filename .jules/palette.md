## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.
## 2024-04-08 - ARIA attributes in custom UI components
**Learning:** Custom tab and error counter components in this application lack connective ARIA attributes (`aria-labelledby`, `aria-live`, `role="alert"`), impairing keyboard and screen reader accessibility.
**Action:** When creating or modifying dynamic components (like tabs or counters), always complete the WAI-ARIA pattern by ensuring links between controls and panels, and using live regions or alerts for dynamic feedback.
