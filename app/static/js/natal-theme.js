(() => {
  'use strict';

  const root = document.documentElement;
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const telegram = window.Telegram?.WebApp;
  // The SDK also exists in ordinary browser tabs, with a default light scheme.
  const isTelegram = () => Boolean(telegram && (
    telegram.initData || Object.keys(telegram.themeParams || {}).length
  ));

  const applyTheme = () => {
    const scheme = isTelegram() && ['light', 'dark'].includes(telegram.colorScheme)
      ? telegram.colorScheme
      : media.matches ? 'dark' : 'light';
    root.dataset.natalTheme = scheme;
    root.style.colorScheme = scheme;
  };

  applyTheme();
  telegram?.onEvent?.('themeChanged', applyTheme);
  if (media.addEventListener) {
    media.addEventListener('change', applyTheme);
  } else {
    media.addListener(applyTheme);
  }
})();
