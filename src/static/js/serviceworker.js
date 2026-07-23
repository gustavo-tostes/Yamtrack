const CACHE_NAME = 'flexihub-v1';

const STATIC_CACHE_URLS = [
  '/static/css/main.css',
  '/static/img/flexihub/android-chrome-192x192.png',
  '/static/img/flexihub/android-chrome-512x512.png',
  '/static/img/flexihub/favicon-16x16.png',
  '/static/img/flexihub/favicon-32x32.png',
  '/static/img/flexihub/apple-touch-icon.png',
  '/static/img/flexihub/favicon.ico',
  '/static/fonts/roboto-flex.woff2',
];

// Install event
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_CACHE_URLS))
      .then(() => self.skipWaiting())
      .catch((error) => {
        console.warn('[FlexiHub SW] Failed to cache static assets:', error);
      }),
  );
});

// Activate event
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((cacheNames) =>
        Promise.all(
          cacheNames.map((cacheName) => {
            if (cacheName !== CACHE_NAME) {
              return caches.delete(cacheName);
            }

            return null;
          }),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

// Fetch event
self.addEventListener('fetch', (event) => {
  const request = event.request;

  if (request.method !== 'GET') {
    return;
  }

  const requestUrl = new URL(request.url);

  if (requestUrl.origin !== self.location.origin) {
    return;
  }

  // Static files: cache first
  if (requestUrl.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((cachedResponse) => {
        if (cachedResponse) {
          return cachedResponse;
        }

        return fetch(request).then((networkResponse) => {
          if (!networkResponse || networkResponse.status !== 200) {
            return networkResponse;
          }

          const responseToCache = networkResponse.clone();

          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseToCache);
          });

          return networkResponse;
        });
      }),
    );

    return;
  }

  // Pages/navigation: network first
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/')),
    );
  }
});