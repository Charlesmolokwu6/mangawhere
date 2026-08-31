/* Service worker. This is the piece that lets a notification arrive when
   Manga Where isn't open — the browser keeps it alive in the background.
   It must be served from the site root to control the whole site. */

self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) {}

  var title = data.title || "Manga Where";
  var options = {
    body: data.body || "A new chapter is out.",
    icon: data.icon || "/static/icon-192.png",
    badge: "/static/badge.png",
    // Same tag replaces an earlier alert for the same series instead of
    // stacking three notifications for one manga.
    tag: data.tag || "mangawhere",
    renotify: true,
    data: { url: data.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      // Focus an open tab if there is one, rather than opening a duplicate.
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(self.location.origin) === 0 && "focus" in list[i]) {
          list[i].navigate(url);
          return list[i].focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
