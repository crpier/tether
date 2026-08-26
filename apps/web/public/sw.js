self.addEventListener("push", (event) => {
  let notification = { body: "", title: "Tether", url: "/" };
  if (event.data) {
    try {
      notification = { ...notification, ...event.data.json() };
    } catch {
      notification.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(notification.title, {
      body: notification.body,
      data: { url: notification.url },
      tag: "tether-trigger",
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow(event.notification.data?.url ?? "/"));
});
