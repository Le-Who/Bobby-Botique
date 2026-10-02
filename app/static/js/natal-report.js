(() => {
  'use strict';

  function revealSection(hash) {
    if (!hash || hash === '#') return false;
    let id;
    try {
      id = decodeURIComponent(hash.slice(1));
    } catch {
      return false;
    }
    let target = document.getElementById(id);
    if (!target) return false;
    if (target.dataset.sectionTarget) {
      target = document.getElementById(target.dataset.sectionTarget);
      if (!target) return false;
    }

    let disclosure = target.closest('details');
    while (disclosure) {
      disclosure.open = true;
      disclosure = disclosure.parentElement?.closest('details');
    }
    const focusTarget = target.matches('details') ? target.querySelector('summary') : target;
    if (focusTarget) {
      if (!focusTarget.matches('summary, a, button, input, select, textarea, [tabindex]')) {
        focusTarget.setAttribute('tabindex', '-1');
      }
      focusTarget.focus({ preventScroll: true });
      focusTarget.scrollIntoView({ block: 'start' });
    }
    return true;
  }

  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const link = event.target.closest('a[href]');
    const hash = link?.getAttribute('href');
    if (!hash?.startsWith('#') || !revealSection(hash)) return;
    event.preventDefault();
    if (window.location.hash !== hash) window.history.pushState(null, '', hash);
  });
  window.addEventListener('hashchange', () => revealSection(window.location.hash));
  revealSection(window.location.hash);
  window.addEventListener('load', () => revealSection(window.location.hash), { once: true });

  document.querySelectorAll('.visual-zoom-toggle').forEach((button) => {
    const diagram = document.getElementById(button.getAttribute('aria-controls'));
    if (!diagram) return;
    button.hidden = false;
    button.addEventListener('click', () => {
      const zoomed = diagram.classList.toggle('is-zoomed');
      button.setAttribute('aria-pressed', String(zoomed));
      button.textContent = zoomed ? 'Уменьшить схему' : 'Увеличить схему';
      if (zoomed) diagram.focus({ preventScroll: true });
    });
  });
})();
