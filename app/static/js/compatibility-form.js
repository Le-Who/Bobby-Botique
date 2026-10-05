(() => {
  'use strict';

  const options = JSON.parse(document.getElementById('compatibility-options').textContent);
  const copy = options.copy;
  const telegram = window.Telegram?.WebApp;
  const form = document.getElementById('compatibility-form');
  const formError = document.getElementById('form-error');
  const back = document.getElementById('back-button');
  const next = document.getElementById('next-button');
  const submit = document.getElementById('submit-button');
  const names = ['first', 'second'];
  const partners = {};
  let step = 0;
  let pending = false;
  let accepted = false;

  const byId = id => document.getElementById(id);
  const setError = (element, message) => {
    element.textContent = message || '';
    element.hidden = !message;
  };
  const authHeaders = () => {
    if (!telegram?.initData) throw new Error(copy.auth_error);
    return { Authorization: `tma ${telegram.initData}` };
  };
  const timePrecision = name => form.querySelector(`input[name="${name}-time-precision"]:checked`).value;

  function clearFieldError(name, field) {
    setError(byId(`${name}-${field}-error`), '');
    const ids = field === 'date' ? ['day', 'month', 'year']
      : field === 'time' ? ['time', 'time-start', 'time-end'] : [field];
    ids.forEach(id => byId(`${name}-${id}`).removeAttribute('aria-invalid'));
  }

  function fieldError(name, field, message, focusId) {
    setError(byId(`${name}-${field}-error`), message);
    const input = byId(`${name}-${focusId || field}`);
    input.setAttribute('aria-invalid', 'true');
    input.focus();
    return false;
  }

  function fillDates(name) {
    const add = (select, value, label) => {
      const option = document.createElement('option');
      option.value = String(value);
      option.textContent = label;
      select.appendChild(option);
    };
    for (let day = 1; day <= 31; day += 1) add(byId(`${name}-day`), day, String(day));
    const monthFormat = new Intl.DateTimeFormat(options.lang === 'en' ? 'en' : 'ru', { month: 'long', timeZone: 'UTC' });
    for (let month = 1; month <= 12; month += 1) {
      add(byId(`${name}-month`), month, monthFormat.format(new Date(Date.UTC(2000, month - 1, 1))));
    }
    for (let year = new Date().getFullYear(); year >= 1900; year -= 1) add(byId(`${name}-year`), year, String(year));
    ['day', 'month', 'year'].forEach(part => byId(`${name}-${part}`).addEventListener('change', () => clearFieldError(name, 'date')));
  }

  function toggleTime(name) {
    const precision = timePrecision(name);
    const unknown = precision === 'unknown';
    byId(`${name}-single-time`).hidden = !['exact', 'approximate'].includes(precision);
    byId(`${name}-range-time`).hidden = precision !== 'range';
    const placeUnknown = byId(`${name}-place-unknown`);
    placeUnknown.disabled = !unknown;
    if (!unknown) placeUnknown.checked = false;
    byId(`${name}-place-fields`).hidden = placeUnknown.checked;
    clearFieldError(name, 'time');
  }

  function autocomplete(name, kind) {
    const input = byId(`${name}-${kind}`);
    const results = byId(`${name}-${kind}-results`);
    let timer;
    let controller;
    let revision = 0;
    let active = -1;
    const close = () => {
      results.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    };
    const reset = () => {
      revision += 1;
      clearTimeout(timer);
      controller?.abort();
      close();
      results.replaceChildren();
    };
    const select = item => {
      partners[name][kind] = item;
      input.value = item.label;
      clearFieldError(name, kind);
      reset();
      if (kind === 'country') {
        partners[name].city = null;
        partners[name].citySearch.reset();
        byId(`${name}-city`).value = '';
        byId(`${name}-city`).disabled = false;
        byId(`${name}-city`).focus();
      } else input.focus();
    };
    async function search(version) {
      const query = input.value.trim();
      if (query.length < 2 || (kind === 'city' && !partners[name].country)) return;
      controller = new AbortController();
      try {
        const parameters = new URLSearchParams({ q: query });
        if (kind === 'city') parameters.set('country', partners[name].country.id);
        const response = await fetch(`/webapp/api/compatibility/${kind === 'country' ? 'countries' : 'cities'}?${parameters}`, {
          headers: authHeaders(), signal: controller.signal, credentials: 'same-origin', cache: 'no-store',
        });
        if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? copy.auth_error : copy.form_error);
        const data = await response.json();
        if (version !== revision || input.value.trim() !== query) return;
        results.replaceChildren();
        const items = Array.isArray(data.items) ? data.items.slice(0, 12).filter(item => (
          item && typeof item.id === 'string' && typeof item.label === 'string'
          && item.id.length <= 100 && item.label.length <= 200
        )) : [];
        items.forEach((item, index) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.id = `${name}-${kind}-option-${index}`;
          button.setAttribute('role', 'option');
          button.setAttribute('aria-selected', 'false');
          button.textContent = item.label;
          button.addEventListener('click', () => select(item));
          results.appendChild(button);
        });
        if (!items.length) {
          const note = document.createElement('p');
          note.textContent = copy.no_results;
          note.setAttribute('role', 'status');
          results.appendChild(note);
        }
        results.hidden = false;
        input.setAttribute('aria-expanded', 'true');
      } catch (error) {
        if (error.name === 'AbortError' || version !== revision) return;
        setError(byId(`${name}-${kind}-error`), error.message === copy.auth_error ? copy.auth_error : copy.form_error);
      }
    }
    input.addEventListener('input', () => {
      reset();
      partners[name][kind] = null;
      clearFieldError(name, kind);
      if (kind === 'country') {
        partners[name].city = null;
        partners[name].citySearch.reset();
        byId(`${name}-city`).value = '';
        byId(`${name}-city`).disabled = true;
        clearFieldError(name, 'city');
      }
      const version = revision;
      timer = setTimeout(() => search(version), 180);
    });
    input.addEventListener('keydown', event => {
      const buttons = [...results.querySelectorAll('button')];
      if (event.key === 'Escape') { reset(); return; }
      if (results.hidden || !buttons.length) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        active = event.key === 'ArrowDown' ? (active + 1) % buttons.length
          : active < 0 ? buttons.length - 1 : (active + buttons.length - 1) % buttons.length;
        buttons.forEach((button, index) => button.setAttribute('aria-selected', String(index === active)));
        input.setAttribute('aria-activedescendant', buttons[active].id);
        buttons[active].scrollIntoView({ block: 'nearest' });
      } else if (event.key === 'Enter' && active >= 0) {
        event.preventDefault();
        buttons[active].click();
      }
    });
    input.addEventListener('focus', () => {
      if (results.childElementCount && !partners[name][kind]) {
        results.hidden = false;
        input.setAttribute('aria-expanded', 'true');
      }
    });
    input.addEventListener('blur', () => {
      // Let a result's click select it before closing the dropdown.
      setTimeout(() => { if (!results.contains(document.activeElement)) close(); }, 150);
    });
    return { reset };
  }

  function validate(name) {
    const year = Number(byId(`${name}-year`).value);
    const month = Number(byId(`${name}-month`).value);
    const day = Number(byId(`${name}-day`).value);
    const date = new Date(year, month - 1, day);
    const today = new Date();
    today.setHours(23, 59, 59, 999);
    if (!year || !month || !day || year < 1900 || date.getFullYear() !== year
      || date.getMonth() !== month - 1 || date.getDate() !== day || date > today) {
      return fieldError(name, 'date', copy.date_error, !day ? 'day' : !month ? 'month' : 'year');
    }
    const precision = timePrecision(name);
    const time = byId(`${name}-time`).value;
    const start = byId(`${name}-time-start`).value;
    const end = byId(`${name}-time-end`).value;
    const validTime = value => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
    if (['exact', 'approximate'].includes(precision) && !validTime(time)) {
      return fieldError(name, 'time', copy.time_error, 'time');
    }
    if (precision === 'range' && (!validTime(start) || !validTime(end) || end <= start)) {
      return fieldError(name, 'time', copy.time_error, !validTime(start) ? 'time-start' : 'time-end');
    }
    if (!byId(`${name}-place-unknown`).checked) {
      if (!partners[name].country) return fieldError(name, 'country', copy.country_error);
      if (!partners[name].city) return fieldError(name, 'city', copy.place_error);
    }
    return true;
  }

  function partnerPayload(name) {
    const pad = value => value.padStart(2, '0');
    const precision = timePrecision(name);
    const payload = {
      birth_date: `${byId(`${name}-year`).value}-${pad(byId(`${name}-month`).value)}-${pad(byId(`${name}-day`).value)}`,
      time_precision: precision,
    };
    if (['exact', 'approximate'].includes(precision)) payload.birth_time = byId(`${name}-time`).value;
    if (precision === 'range') {
      payload.birth_time_range_start = byId(`${name}-time-start`).value;
      payload.birth_time_range_end = byId(`${name}-time-end`).value;
    }
    if (!byId(`${name}-place-unknown`).checked) {
      payload.country_code = partners[name].country.id;
      payload.city_geoname_id = partners[name].city.id;
    }
    return payload;
  }

  function renderReview() {
    names.forEach(name => {
      const payload = partnerPayload(name);
      const date = new Date(`${payload.birth_date}T12:00:00Z`);
      const precision = payload.time_precision;
      const time = precision === 'range' ? `${payload.birth_time_range_start}–${payload.birth_time_range_end}`
        : payload.birth_time || '';
      const rows = [
        [copy.date, new Intl.DateTimeFormat(options.lang === 'en' ? 'en' : 'ru', { day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }).format(date)],
        [copy.time, time ? `${copy[precision]} · ${time}` : copy.unknown],
        [copy.place, byId(`${name}-place-unknown`).checked ? copy.place_unknown
          : `${partners[name].city.label}, ${partners[name].country.label}`],
      ];
      const list = byId(`review-${name}`).querySelector('dl');
      list.replaceChildren();
      rows.forEach(([label, value]) => {
        const term = document.createElement('dt');
        const description = document.createElement('dd');
        term.textContent = label;
        description.textContent = value;
        list.append(term, description);
      });
    });
  }

  function showStep(index, focus = true) {
    step = index;
    names.forEach((name, partnerIndex) => { byId(`partner-${name}`).hidden = index !== partnerIndex; });
    byId('compatibility-review').hidden = index !== 2;
    if (index === 2) renderReview();
    back.hidden = index === 0;
    next.hidden = index === 2;
    submit.hidden = index !== 2;
    setError(formError, '');
    document.querySelectorAll('.rail-step').forEach((item, itemIndex) => {
      if (itemIndex === index) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
      item.dataset.complete = String(itemIndex < index);
    });
    byId('step-status').textContent = copy.step.includes('{current}')
      ? copy.step.replace('{current}', String(index + 1)).replace('{total}', '3')
      : `${copy.step} ${index + 1} / 3`;
    if (index > 0) telegram?.BackButton?.show?.();
    else telegram?.BackButton?.hide?.();
    if (focus) {
      byId(index === 2 ? 'review-title' : `${names[index]}-title`).focus();
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
  }

  function setPending(value) {
    pending = value;
    form.setAttribute('aria-busy', String(value));
    submit.disabled = value;
    back.disabled = value;
    next.disabled = value;
    document.querySelectorAll('[data-edit]').forEach(button => { button.disabled = value; });
  }

  names.forEach(name => {
    partners[name] = { country: null, city: null };
    fillDates(name);
    partners[name].countrySearch = autocomplete(name, 'country');
    partners[name].citySearch = autocomplete(name, 'city');
    form.querySelectorAll(`input[name="${name}-time-precision"]`).forEach(input => {
      input.addEventListener('change', () => toggleTime(name));
    });
    ['time', 'time-start', 'time-end'].forEach(id => byId(`${name}-${id}`).addEventListener('input', () => clearFieldError(name, 'time')));
    byId(`${name}-place-unknown`).addEventListener('change', event => {
      byId(`${name}-place-fields`).hidden = event.target.checked;
      partners[name].countrySearch.reset();
      partners[name].citySearch.reset();
      clearFieldError(name, 'country');
      clearFieldError(name, 'city');
    });
  });
  next.addEventListener('click', () => { if (!pending && step < 2 && validate(names[step])) showStep(step + 1); });
  const goBack = () => { if (!pending && !accepted && step > 0) showStep(step - 1); };
  back.addEventListener('click', goBack);
  telegram?.BackButton?.onClick?.(goBack);
  document.querySelectorAll('[data-edit]').forEach(button => {
    button.addEventListener('click', () => { if (!pending) showStep(names.indexOf(button.dataset.edit)); });
  });
  byId('cancel-button').addEventListener('click', () => telegram?.close?.());
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (pending || accepted) return;
    if (step < 2) { next.click(); return; }
    for (const name of names) {
      if (!validate(name)) { showStep(names.indexOf(name), false); validate(name); return; }
    }
    setError(formError, '');
    let failure = copy.form_error;
    try {
      const headers = { ...authHeaders(), 'Content-Type': 'application/json' };
      setPending(true);
      const response = await fetch('/webapp/api/compatibility/submit', {
        method: 'POST', headers, credentials: 'same-origin', cache: 'no-store',
        body: JSON.stringify({ pair: options.pair, first: partnerPayload('first'), second: partnerPayload('second') }),
      });
      const data = await response.json();
      if (response.status !== 202 || data.ok !== true || data.status !== 'accepted') {
        const detail = typeof data.detail === 'string' ? data.detail.slice(0, 500) : '';
        failure = response.status === 401 || response.status === 403 ? copy.auth_error : detail || copy.form_error;
        throw new Error(failure);
      }
      accepted = true;
      form.hidden = true;
      document.querySelector('.step-progress').hidden = true;
      document.querySelector('.intro').hidden = true;
      const status = byId('submission-status');
      status.hidden = false;
      status.focus();
      telegram?.BackButton?.hide?.();
    } catch (error) {
      setError(formError, error instanceof Error && error.message === copy.auth_error ? copy.auth_error : failure);
      formError.focus();
    } finally {
      setPending(false);
    }
  });

  telegram?.ready?.();
  telegram?.expand?.();
  showStep(0, false);
})();
