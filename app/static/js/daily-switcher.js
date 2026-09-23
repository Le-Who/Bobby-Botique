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

  function render(game) {
    if (!games[game]) return;
    selected = game;
    current.textContent = games[game].name;
    for (const choice of choices) {
      choice.setAttribute('aria-pressed', String(choice.dataset.dailyGame === game));
    }
  }

  async function request(method, game) {
    const initData = window.Telegram?.WebApp?.initData || '';
    const response = await fetch('/webapp/api/daily-game', {
      method,
      headers: { 'X-TG-INIT-DATA': initData, ...(game ? { 'Content-Type': 'application/json' } : {}) },
      ...(game ? { body: JSON.stringify({ game }) } : {}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(response.status === 401
      ? 'Откройте игру в Telegram, чтобы сохранить выбор.'
      : 'Не удалось сохранить выбор. Проверьте соединение и попробуйте ещё раз.');
    return body;
  }

  async function load() {
    try {
      const saved = await request('GET');
      if (busy) return;
      render(saved.game);
      status.textContent = saved.subscribed
        ? 'Вы будете получать выбранную игру каждый день.'
        : 'Ежедневная рассылка сейчас выключена.';
    } catch {
      if (busy) return;
      status.textContent = 'Откройте игру в Telegram, чтобы сохранить выбор.';
    }
  }

  trigger.addEventListener('click', () => {
    dialog.showModal();
    const active = choices.find(choice => choice.dataset.dailyGame === selected);
    (active || choices[0]).focus();
    load();
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => trigger.focus());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });

  for (const choice of choices) {
    choice.addEventListener('click', async () => {
      if (busy) return;
      const game = choice.dataset.dailyGame;
      if (!games[game]) return;
      busy = true;
      status.textContent = 'Сохраняем выбор…';
      choices.forEach(button => { button.disabled = true; });
      try {
        const saved = await request('PATCH', game);
        render(saved.game);
        if (game === root.dataset.pageGame) {
          dialog.close();
          busy = false;
          choices.forEach(button => { button.disabled = false; });
        } else {
          window.location.assign(games[game].path);
        }
      } catch (error) {
        status.textContent = error.message || 'Не удалось сохранить выбор.';
        busy = false;
        choices.forEach(button => { button.disabled = false; });
      }
    });
  }

  render(selected);
  load();
})();
