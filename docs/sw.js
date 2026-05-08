// Service Worker — 오프라인 캐시
const CACHE_NAME = 'repeat-player-v7';
const ASSETS = [
  './',
  './index.html',
  './app.js',
  './dict.js',
  './manifest.json',
  // tesseract는 큰 용량이므로 첫 사용 시 fetch
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  // GET 요청만 캐시 (POST/PUT 등은 통과)
  if (e.request.method !== 'GET') return;
  // OpenRouter API 호출은 캐시 안 함
  if (e.request.url.includes('openrouter.ai')) return;
  e.respondWith(
    caches.match(e.request).then(resp => resp || fetch(e.request).then(r => {
      // 동적 리소스는 런타임 캐시
      if (r.ok && r.type === 'basic') {
        const clone = r.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      }
      return r;
    }).catch(() => caches.match('./index.html')))
  );
});
