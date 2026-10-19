(() => {
  'use strict';
  const player = document.querySelector('#player');
  const token = new URLSearchParams(location.search).get('token') || '';
  const cover = document.querySelector('#track-cover');
  let coverSource = null;
  cover.addEventListener('load', () => { cover.hidden = false; });
  cover.addEventListener('error', () => { cover.hidden = true; });
  function updateCover(source) {
    if (source === coverSource) return;
    coverSource = source;
    cover.hidden = true;
    if (source) cover.src = source;
    else cover.removeAttribute('src');
  }
  let track = null, received = 0;
  const stamp = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  function progress() {
    if (!track || !track.visible || !track.duration) return;
    const position = Math.min(track.duration, track.position + (track.playing ? (performance.now() - received) / 1000 : 0));
    document.querySelector('#progress').style.width = `${100 * position / track.duration}%`;
    document.querySelector('#track-time').textContent = `${stamp(position)} / ${stamp(track.duration)}`;
  }
  async function poll() {
    try {
      const response = await fetch(`/overlay/music/state?token=${encodeURIComponent(token)}`, { cache: 'no-store', signal: AbortSignal.timeout(7000) });
      if (!response.ok) throw new Error('unavailable');
      track = await response.json();
      received = performance.now();
      player.hidden = !track.visible;
      updateCover(track.visible ? track.cover || null : null);
      if (track.visible) {
        document.querySelector('#track-title').textContent = track.title;
        document.querySelector('#track-artist').textContent = track.artist;
        document.querySelector('#play-label').textContent = track.playing ? 'СЕЙЧАС ИГРАЕТ' : 'НА ПАУЗЕ';
        player.classList.toggle('is-paused', !track.playing);
        document.querySelector('#timeline').hidden = !(track.duration > 0);
        progress();
      }
    } catch (_) { player.hidden = true; track = null; updateCover(null); }
    finally { setTimeout(poll, 2000); }
  }
  setInterval(progress, 500);
  poll();
})();
