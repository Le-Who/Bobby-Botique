## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.
## 2024-06-11 - Tab Navigation Accessibility in miniapp.html
**Learning:** Custom tab navigations using plain `div` elements require semantic ARIA attributes (`role="tablist"`, `role="tab"`, `aria-selected`, `aria-controls`) and keyboard interaction (`tabindex="0"`, handling 'Enter' and 'Space') to be accessible to screen reader users and keyboard-only users.
**Action:** Adding full WAI-ARIA compliance to the tabbed interface in `miniapp.html`.
