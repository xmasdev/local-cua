// Minimal service worker: lets the page be installed as an app. No offline caching
// (the app is useless without a live connection to the PC anyway).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
