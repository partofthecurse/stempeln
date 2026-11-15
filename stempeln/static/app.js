const root = document.documentElement;
const themeButton = document.querySelector('.theme-toggle');
const english = root.lang === 'en';
const themeLabels = english ? { system: 'System', light: 'Light', dark: 'Dark' } : { system: 'System', light: 'Hell', dark: 'Dunkel' };

function applyTheme(theme) {
  if (theme === 'system') delete root.dataset.theme; else root.dataset.theme = theme;
  try { if (theme === 'system') localStorage.removeItem('stempeln-theme'); else localStorage.setItem('stempeln-theme', theme); } catch (error) { /* ignorieren */ }
  if (themeButton) {
    themeButton.title = `${english ? 'Theme' : 'Darstellung'}: ${themeLabels[theme]}`;
    themeButton.textContent = theme === 'dark' ? '☾' : theme === 'light' ? '☀' : '◐';
  }
}
applyTheme(root.dataset.theme || 'system');
themeButton?.addEventListener('click', () => {
  const order = ['system', 'dark', 'light'];
  applyTheme(order[(order.indexOf(root.dataset.theme || 'system') + 1) % order.length]);
});

document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => { if (!confirm(form.dataset.confirm)) event.preventDefault(); });
});

document.querySelectorAll('[data-width]').forEach(bar => { bar.style.width = `${Math.min(100, bar.dataset.width)}%`; });
document.querySelectorAll('[data-left]').forEach(mark => { mark.style.left = `${Math.min(100, mark.dataset.left)}%`; });

const timer = document.querySelector('.timer[data-running]');
if (timer) {
  const net = Number(timer.dataset.net);
  const renderedAt = Number(timer.dataset.now);
  const offset = Date.now() / 1000 - renderedAt;
  const tick = () => {
    const seconds = Math.max(0, Math.floor(net + Date.now() / 1000 - offset - renderedAt));
    const hours = Math.floor(seconds / 3600);
    const minutes = String(Math.floor(seconds / 60) % 60).padStart(2, '0');
    const secs = String(seconds % 60).padStart(2, '0');
    timer.innerHTML = `${hours}:${minutes}<small>:${secs}</small>`;
  };
  tick();
  setInterval(tick, 1000);
  // Pausenabzug und Automatik-Status serverseitig auffrischen
  setTimeout(() => { if (document.visibilityState === 'visible') location.reload(); }, 5 * 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && Date.now() / 1000 - offset - renderedAt > 60) location.reload();
  });
}

document.querySelectorAll('select[data-toggle-day]').forEach(select => {
  const update = () => select.form.querySelectorAll('.day-only').forEach(row => { row.hidden = select.value !== 'day'; });
  select.addEventListener('change', update);
});

document.querySelectorAll('select[data-autosubmit]').forEach(select => {
  select.addEventListener('change', () => select.form.submit());
});

// Rechnungslayout: Live-Vorschau bei jeder Änderung (verzögert), ohne zu speichern
const layoutForm = document.getElementById('layout-form');
const previewButton = document.getElementById('preview-button');
if (layoutForm && previewButton) {
  let timer;
  const refresh = () => { clearTimeout(timer); timer = setTimeout(() => layoutForm.requestSubmit(previewButton), 500); };
  layoutForm.addEventListener('input', event => {
    if (event.target.type === 'color') event.target.nextElementSibling.textContent = event.target.value;
    refresh();
  });
  layoutForm.addEventListener('change', refresh);
}
