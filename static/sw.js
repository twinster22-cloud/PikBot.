self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith('pikbot-')).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});
