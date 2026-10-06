(() => {
  'use strict';

  const sections = [...document.querySelectorAll('.control-section')];
  const links = [...document.querySelectorAll('.sidebar nav a')];
  const picker = document.getElementById('section-select');
  const revision = document.getElementById('revision');
  const visibleRevision = document.getElementById('view-revision');
  const filters = [
    ['process-search', 'process'],
    ['process-group', 'group'],
    ['prompt-search', 'prompt'],
    ['command-search', 'command'],
    ['command-kind', 'command_kind'],
  ];

  function showSection({ focus = false } = {}) {
    const selected = location.hash.slice(1);
    const id = sections.some(section => section.id === selected) ? selected : 'processes';
    for (const section of sections) section.hidden = section.id !== id;
    for (const link of links) {
      if (link.hash === `#${id}`) link.setAttribute('aria-current', 'location');
      else link.removeAttribute('aria-current');
    }
    picker.value = id;
    if (focus) {
      const heading = document.getElementById(id).querySelector('h2');
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      heading.scrollIntoView({ block: 'start' });
    }
  }

  function navigate(id) {
    if (location.hash !== `#${id}`) history.pushState(null, '', `#${id}`);
    showSection({ focus: true });
  }

  for (const link of links) {
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      navigate(link.hash.slice(1));
    });
  }
  picker.addEventListener('change', () => navigate(picker.value));

  function restoreFilters() {
    const parameters = new URLSearchParams(location.search);
    for (const [id, key] of filters) {
      const input = document.getElementById(id);
      const value = parameters.get(key) || '';
      if (input.tagName === 'SELECT' && ![...input.options].some(option => option.value === value)) continue;
      if (input.value !== value) {
        input.value = value;
        input.dispatchEvent(new Event(input.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
      }
    }
  }

  for (const [id, key] of filters) {
    const input = document.getElementById(id);
    input.addEventListener(input.tagName === 'SELECT' ? 'change' : 'input', () => {
      const url = new URL(location.href);
      if (input.value) url.searchParams.set(key, input.value);
      else url.searchParams.delete(key);
      history.replaceState(null, '', url);
    });
  }

  // Groups arrive with the API response; restore deep links after their options exist.
  new MutationObserver(restoreFilters).observe(document.getElementById('process-group'), { childList: true });
  new MutationObserver(() => { visibleRevision.textContent = revision.textContent; }).observe(revision, { childList: true, characterData: true, subtree: true });
  window.addEventListener('hashchange', () => showSection());
  window.addEventListener('popstate', () => { showSection(); restoreFilters(); });
  visibleRevision.textContent = revision.textContent;
  restoreFilters();
  showSection();
})();
