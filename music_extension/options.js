const input = document.querySelector('#endpoint'), status = document.querySelector('#status');
chrome.storage.local.get(['endpoint', 'status']).then(saved => { input.value = saved.endpoint || ''; status.textContent = saved.status || 'Ожидание музыки'; });
document.querySelector('#settings').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const url = new URL(input.value.trim());
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.pathname !== '/api/music/browser' || !url.searchParams.get('token') || url.username || url.password) throw new Error();
    await chrome.storage.local.set({endpoint: url.href, status: 'Сохранено. Запустите музыку на поддерживаемом сайте.'});
    status.textContent = 'Сохранено. Запустите музыку.';
  } catch (_) { status.textContent = 'Вставьте ссылку подключения расширения из панели Immortal.'; }
});
