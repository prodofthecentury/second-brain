"use strict";

const CACHE_NAME = "second-brain-mobile-vault-v1";
const APP_SHELL_ASSETS = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./manifest.webmanifest",
  "./icon.svg",
  "./lib/vault-crypto.js",
  "./lib/vault-epistemic-engine.js",
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(APP_SHELL_ASSETS);
    }).then(function () {
      return self.skipWaiting();
    })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys
          .filter(function (k) { return k !== CACHE_NAME; })
          .map(function (k) { return caches.delete(k); })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (event) {
  const req = event.request;
  if (req.method !== "GET") {
    return;
  }
  const url = new URL(req.url);
  // Never cache encrypted remote sync endpoints or external origins
  if (url.origin !== self.location.origin) {
    return;
  }
  if (
    url.pathname.endsWith("/vault-header.json") ||
    url.pathname.endsWith("/head.json") ||
    url.pathname.indexOf("/manifests/") !== -1 ||
    url.pathname.indexOf("/objects/") !== -1 ||
    url.pathname.indexOf("/inbox-drops/") !== -1
  ) {
    return;
  }
  event.respondWith(
    caches.match(req, { ignoreSearch: true }).then(function (cached) {
      if (cached) {
        return cached;
      }
      return fetch(req);
    })
  );
});
