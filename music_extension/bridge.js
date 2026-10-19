(() => {
  let lastSent = 0;
  window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.kind !== 'immortal-music') return;
    if (Date.now() - lastSent < 1500) return;
    lastSent = Date.now();
    chrome.runtime.sendMessage({kind: 'music', track: event.data.track}).catch(() => {});
  });
})();
