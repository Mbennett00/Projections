const CACHE = 'projex-v7';
const SHELL = ['/', '/how/', '/picks/', '/picks.js', '/theme.css', '/manifest.webmanifest'];

// Cache each shell URL on its own. caches.addAll() is all-or-nothing: a single
// 404 anywhere in SHELL rejects the whole thing, which fails the install event,
// which means the new worker never activates -- and the previous version keeps
// serving its old cache indefinitely. A stale entry in this list would silently
// freeze the app on old code, so one missing file is skipped instead.
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => Promise.all(SHELL.map(u =>
        c.add(u).catch(err => console.warn('sw: skipped ' + u, err))
      )))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;

  // Data: network-first so boards always show the freshest model run
  if (url.pathname.startsWith('/data/')) {
    e.respondWith(
      fetch(e.request)
        .then(r => {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(url.pathname, copy));
          return r;
        })
        .catch(() => caches.match(url.pathname))
    );
    return;
  }

  // Shell: cache-first, refresh in background
  e.respondWith(
    caches.match(e.request).then(cached => {
      const net = fetch(e.request).then(r => {
        caches.open(CACHE).then(c => c.put(e.request, r.clone()));
        return r;
      }).catch(() => cached);
      return cached || net;
    })
  );
});
