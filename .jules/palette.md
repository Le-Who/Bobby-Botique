## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.

## 2026-07-12 - Keyboard Accessibility for Div-based Tabs
**Learning:** Div-based interactive elements (like tabs in miniapp.html) lack native keyboard accessibility, making them unusable for keyboard and screen reader users compared to native <button> elements.
**Action:** When building custom tabs with <div>, always ensure keyboard accessibility by adding tabindex="0" and an onkeydown handler to process 'Enter' and 'Space' key events, strictly including event.preventDefault() to prevent default page scrolling on Space.
