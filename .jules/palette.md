## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2026-07-20 - Keyboard Accessibility on Div-based Tabs
**Learning:** Found custom tabs implemented as `<div>` elements lacking native keyboard support, which prevents users from navigating or activating tabs using 'Enter' or 'Space' keys.
**Action:** When building custom tabs with non-native elements like `<div>`, explicitly add `tabindex="0"` to make them focusable and attach an `onkeydown` event handler to process 'Enter' and 'Space' keypresses, including `event.preventDefault()` on Space to prevent unintentional page scrolling.
