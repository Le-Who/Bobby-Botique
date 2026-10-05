(() => {
  const root = document.querySelector('.daily-choice');
  if (!root) return;

  const dialog = document.getElementById('daily-choice-dialog');
  const trigger = document.getElementById('daily-choice-trigger');
  const close = document.getElementById('daily-choice-close');
  const current = document.getElementById('daily-choice-current');
  const status = document.getElementById('daily-choice-status');
  const choices = [...dialog.querySelectorAll('[data-daily-game]')];
  const games = {
    crocodile: { name: '🐊 Крокодил', path: '/webapp/game?game_id=daily' },
    '2048': { name: '◈ 2048 Sprint', path: '/webapp/daily2048' },
    trivia: { name: '✦ Викторина', path: '/webapp/dailytrivia' },
  };
  let selected = root.dataset.pageGame;
  let busy = false;
  let readEpoch = 0;

  function setStatus(message, tone = 'info') {
    status.textContent = message;
    status.dataset.tone = tone;
  }

  function render(game) {
    if (!games[game]) return;
    selected = game;
    for (const choice of choices) {
      choice.setAttribute('aria-pressed', String(choice.dataset.dailyGame === game));
    }
  }

  async function request(method, game) {
    const initData = window.Telegram?.WebApp?.initData || '';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch('/webapp/api/daily-game', {
        method,
        signal: controller.signal,
        headers: { 'X-TG-INIT-DATA': initData, ...(game ? { 'Content-Type': 'application/json' } : {}) },
        ...(game ? { body: JSON.stringify({ game }) } : {}),
      });
      if (response.status === 401 || response.status === 403) {
        throw new Error('Откройте игру в Telegram, чтобы сохранить выбор.');
      }
      if (!response.ok) throw new Error('Не удалось обновить выбор. Попробуйте ещё раз.');
      const body = await response.json();
      if (!games[body.game]) throw new Error('Не удалось загрузить выбор. Попробуйте ещё раз.');
      return body;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function load() {
    const epoch = ++readEpoch;
    try {
      const saved = await request('GET');
      if (busy || epoch !== readEpoch) return;
      render(saved.game);
      setStatus(saved.subscribed
        ? `В рассылке: ${games[saved.game].name}. Выбор можно изменить.`
        : 'Ежедневная рассылка выключена. Здесь можно сменить игру.');
    } catch (error) {
      if (busy || epoch !== readEpoch) return;
      setStatus(error instanceof TypeError || error.name === 'AbortError'
        ? 'Не удалось загрузить выбор. Проверьте соединение и откройте меню ещё раз.'
        : error.message, 'error');
    }
  }

  trigger.addEventListener('click', () => {
    if (dialog.open) return;
    dialog.showModal();
    trigger.setAttribute('aria-expanded', 'true');
    const active = choices.find(choice => choice.dataset.dailyGame === selected);
    (active || choices[0]).focus();
    load();
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    trigger.setAttribute('aria-expanded', 'false');
    trigger.focus({ preventScroll: true });
  });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });

  for (const choice of choices) {
    choice.addEventListener('click', async () => {
      if (busy) return;
      const game = choice.dataset.dailyGame;
      if (!games[game]) return;
      busy = true;
      ++readEpoch;
      dialog.setAttribute('aria-busy', 'true');
      setStatus('Сохраняем выбор…');
      choices.forEach(button => { button.disabled = true; });
      try {
        const saved = await request('PATCH', game);
        render(saved.game);
        setStatus('Выбор сохранён.', 'success');
        if (game === root.dataset.pageGame) {
          dialog.close();
          busy = false;
          choices.forEach(button => { button.disabled = false; });
        } else {
          window.location.assign(games[game].path);
        }
      } catch (error) {
        setStatus(error instanceof TypeError || error.name === 'AbortError'
          ? 'Не удалось подтвердить сохранение. Проверьте соединение и попробуйте ещё раз.'
          : error.message || 'Не удалось сохранить выбор.', 'error');
        busy = false;
        choices.forEach(button => { button.disabled = false; });
      } finally {
        dialog.removeAttribute('aria-busy');
      }
    });
  }

  current.textContent = games[selected]?.name || 'Выбрать игру';
  render(selected);
  load();
})();
