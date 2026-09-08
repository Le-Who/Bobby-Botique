## 2024-05-18 - Missing ARIA States on Custom Navigation Tabs

> Historical agent journal, reviewed as archival material on 2026-09-08. Statements
> about timings, tests, vulnerabilities and implementation describe earlier work,
> not current guarantees or mandatory coding rules. Revalidate against current
> code and AGENTS.md; do not apply blanket lock/regex/escaping advice automatically.
**Learning:** Found a recurring pattern where custom-built tab navigations (like the one in `dashboard.html`) only toggle visual states (e.g., CSS `.active` classes) without signaling state changes to screen readers via `aria-selected` and `aria-controls` properties, preventing users from tracking active tabs correctly.
**Action:** When building custom tab components or enhancing existing ones, ensure the parent element has `role="tablist"`, each child button has `role="tab"`, and Javascript click handlers toggle `aria-selected` attributes identically to the visual classes. Add `role="tabpanel"` to the corresponding panel elements.
