// A minimal service worker - its whole job is to make the page installable (Chrome's
// criteria require one registered with a fetch handler), not to run this app offline.
// Everything that talks to the server (/api/*) always goes straight to the network,
// untouched: caching a chat answer or a fact lookup would risk ever serving a stale or
// wrong one, which this app can never afford. Only the static app shell is cached, so a
// flaky connection gets the page itself back instead of a blank tab.
const SHELL_CACHE = "jay-shell-v1";
const SHELL_FILES = ["/", "/theme.css", "/manifest.json"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return cache.addAll(SHELL_FILES);
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(
        names.filter(function (name) { return name !== SHELL_CACHE; })
          .map(function (name) { return caches.delete(name); })
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) {
    return;  // never intercept a write, and never cache anything server-driven
  }
  if (!SHELL_FILES.includes(url.pathname)) {
    return;  // static assets outside the shell (icons, etc.) just hit the network
  }
  event.respondWith(
    fetch(event.request)
      .then(function (response) {
        var copy = response.clone();
        caches.open(SHELL_CACHE).then(function (cache) { cache.put(event.request, copy); });
        return response;
      })
      .catch(function () { return caches.match(event.request); })
  );
});
