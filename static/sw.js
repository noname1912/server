// Service Worker — ESP32 Hi-Fi Music Hub PWA
const CACHE_NAME = 'esp32-hifi-v1';
const ASSETS_TO_CACHE = [
  '/',
  '/static/manifest.json',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Chỉ cache static/manifest, còn API player/search thì luôn lấy mạng mới nhất
  if (event.request.url.includes('/api/') || event.request.url.includes('/stream')) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
