const providers = {
  'open.spotify.com': 'spotify-web', 'music.youtube.com': 'youtube-music',
  'music.yandex.ru': 'yandex-music', 'music.yandex.com': 'yandex-music',
  'music.yandex.kz': 'yandex-music', 'music.yandex.by': 'yandex-music'
};
const covers = new Map();
async function cover(url) {
  if (!url) return '';
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || !['scdn.co', 'yandex.net', 'ytimg.com', 'googleusercontent.com'].some(d => parsed.hostname === d || parsed.hostname.endsWith('.' + d))) return '';
    if (covers.has(url)) return covers.get(url);
    const response = await fetch(url, {signal: AbortSignal.timeout(3000), credentials: 'omit'});
    if (!response.ok) return '';
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.length > 1048576) return '';
    let binary = '';
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
    const result = btoa(binary);
    if (covers.size >= 8) covers.delete(covers.keys().next().value);
    covers.set(url, result);
    return result;
  } catch (_) { return ''; }
}
chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message.kind !== 'music' || !sender.tab || sender.frameId !== 0) return;
  (async () => {
    const provider = providers[new URL(sender.url).hostname];
    if (!provider || !message.track || typeof message.track.title !== 'string') return;
    const {endpoint} = await chrome.storage.local.get('endpoint');
    if (!endpoint) return;
    const target = new URL(endpoint);
    if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(target.hostname) || target.pathname !== '/api/music/browser') return;
    const track = message.track;
    const response = await fetch(endpoint, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, signal: AbortSignal.timeout(6000),
      body: JSON.stringify({provider, client: `${chrome.runtime.id}:${sender.tab.id}`,
        title: track.title.slice(0, 500), artist: String(track.artist || '').slice(0, 300),
        playing: track.playing === true, position: track.position || 0, duration: track.duration || 0,
        cover: await cover(track.artwork)})
    });
    await chrome.storage.local.set({status: response.ok ? 'Подключено: ' + provider : 'Ошибка панели: ' + response.status});
  })().catch(async () => { await chrome.storage.local.set({status: 'Нет связи с панелью. Проверьте её запуск и ссылку.'}); }).finally(() => respond({ok: true}));
  return true;
});
