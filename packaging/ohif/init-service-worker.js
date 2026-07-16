// DcmGet PDI is an immutable, offline directory. Never register a service
// worker or fetch Workbox/CDN resources; remove registrations left by an older
// viewer opened on the same local origin.
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.getRegistrations().then(function (registrations) {
    registrations.forEach(function (registration) {
      registration.unregister();
    });
  });
}
