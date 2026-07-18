## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.
## 2026-07-18 - Full WAI-ARIA support for miniapp tabs
**Learning:** Custom div-based tabs in miniapp.html lack semantic ARIA roles and keyboard interaction logic, hindering screen reader users.
**Action:** Always add role="tablist", role="tab", aria-controls, role="tabpanel", aria-labelledby, tabindex="0", onkeydown handling, and sync aria-selected state via JS when building custom tabs using divs.
