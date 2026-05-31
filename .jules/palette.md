## 2024-05-31 - Tab Panel Accessibility & Dynamic Badges
**Learning:** Custom tab components often miss correct WAI-ARIA associations (tab `id` matching `aria-labelledby` on the panel). Additionally, error badges that change dynamically need `aria-live="polite"` and `aria-atomic="true"` to announce updates to screen readers.
**Action:** Add explicit IDs to tab buttons and link them to their panels using `aria-labelledby`. Ensure all dynamically updated alert counts have proper `aria-live` regions.
