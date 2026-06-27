## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.
## 2026-06-27 - Missing ARIA States on Miniapp Custom Navigation Tabs
**Learning:** Found a similar accessibility pattern in `miniapp.html` where custom tab buttons lacked `role="tab"` and key handling, while the pages lacked `role="tabpanel"`.
**Action:** Always ensure any div-based custom tabs have keyboard support with Enter/Space (including `e.preventDefault()`), `tabindex="0"`, and synchronize `aria-selected` exactly with visual `.active` classes.
