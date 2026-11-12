/// Service worker for Response Needed browser push (GRPH-1528, P75 D4). Built to /sw.js.
/// Receives a push, shows the question's first line and the ticket key, and opens the Triage
/// row on click. Nothing here runs unless a person clicked "Notify me in this browser".
const sw = self as unknown as {
  addEventListener: (type: string, fn: (e: any) => void) => void;
  registration: { showNotification: (title: string, o: object) => Promise<void> };
  clients: {
    matchAll: (o: object) => Promise<{ url: string; focus: () => Promise<unknown>; navigate?: (u: string) => Promise<unknown> }[]>;
    openWindow: (url: string) => Promise<unknown>;
  };
};

sw.addEventListener("push", (event) => {
  let data: { title?: string; body?: string; url?: string; ticket?: string } = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    sw.registration.showNotification(data.title ?? "A question needs a response", {
      body: data.body ?? "",
      tag: data.ticket,
      data: { url: data.url ?? "/triage" },
    }),
  );
});

sw.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url: string = event.notification.data?.url ?? "/triage";
  event.waitUntil(
    sw.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      const open = wins[0];
      if (open && open.navigate) return open.navigate(url).then(() => open.focus());
      return sw.clients.openWindow(url);
    }),
  );
});
