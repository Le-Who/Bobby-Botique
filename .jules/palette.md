## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2024-05-30 - WAI-ARIA Tab Linkage & Dynamic Badges
**Learning:** Custom tab interfaces often use `aria-controls` but miss the reverse `aria-labelledby` linkage, leaving screen readers without context when navigating into the panel. Additionally, hidden dynamic badging elements (like error counters) fail to announce updates if missing `aria-live` and `aria-atomic` attributes.
**Action:** Always implement bidirectional WAI-ARIA linkage for tabs (buttons need `id` + `aria-controls`, panels need `aria-labelledby`) and apply `aria-live="polite"` with `aria-atomic="true"` to dynamic badges.
