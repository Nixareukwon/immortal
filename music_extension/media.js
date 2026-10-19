(() => {
  function report() {
    const metadata = navigator.mediaSession?.metadata;
    if (!metadata?.title) return;
    const players = [...document.querySelectorAll('audio, video')];
    const player = players.find(p => !p.paused && !p.ended) || players[0];
    const playing = player ? !player.paused && !player.ended : navigator.mediaSession.playbackState === 'playing';
    window.postMessage({kind: 'immortal-music', track: {
      title: metadata.title.slice(0, 500), artist: (metadata.artist || '').slice(0, 300), playing,
      position: Number.isFinite(player?.currentTime) ? Math.max(0, player.currentTime) : 0,
      duration: Number.isFinite(player?.duration) ? Math.max(0, player.duration) : 0,
      artwork: metadata.artwork?.[0]?.src || ''
    }}, location.origin);
  }
  setInterval(report, 2000);
  report();
})();
