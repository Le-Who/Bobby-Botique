## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-18 - Dynamic Badging Requires Live Regions
**Learning:** Found that dynamically updated badge counters (like the `error-badge` which updates via JS polling or SSE) remain invisible to screen reader users because changes to hidden or non-live elements aren't announced natively.
**Action:** When adding or maintaining dynamic badging elements (e.g., counters or live status indicators), ensure they are equipped with WAI-ARIA live region attributes such as `aria-live="polite"` and `aria-atomic="true"` so that their dynamic text changes are correctly queued and announced to assistive technologies without interrupting the user.
