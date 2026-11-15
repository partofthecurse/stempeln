try {
  const theme = localStorage.getItem('stempeln-theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
} catch (error) { /* Speicher nicht verfügbar: Systemeinstellung gilt */ }
