self.addEventListener("push", (event) => {
  let body = "";
  if (event.data) {
    body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification("Tether", {
      body,
      tag: "tether-trigger",
    }),
  );
});
