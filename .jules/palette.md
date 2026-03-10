# Palette's UX/A11y Learnings

## 2024-05-18 - Missing ARIA Tab Roles
**Learning:** Adding `class="tab active"` to a `button` is visually sufficient for sighted users, but completely opaque to screen readers navigating Custom Tabs. Without ARIA roles (`role="tablist"`, `role="tab"`, `role="tabpanel"`) and explicitly defined linkages (`aria-selected`, `aria-controls`, `aria-labelledby`), screen reader users won't know they're inside a tabbed interface or which tab is currently active.
**Action:** Always implement the full WAI-ARIA pattern for custom tabs: `role="tablist"` on the container, `role="tab"` and dynamic `aria-selected="true|false"` toggling via JS on the buttons, and `role="tabpanel"` on the content areas, explicitly linking them using IDs (`aria-controls` on the tab -> `id` on the panel, and `aria-labelledby` on the panel -> `id` on the tab).
