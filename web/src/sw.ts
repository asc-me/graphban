/// Service worker for Response Needed browser push (GRPH-1528, P75 D4). Built to /sw.js.
/// Receives a push, shows the question's first line and the ticket key, and opens the Triage
/// row on click. Nothing here runs unless a person clicked "Notify me in this browser".
const sw = self as unknown as {
  addEventListener: (type: string, fn: (e: any) => void) => void;
  registration: { showNotification: (title: string, o: object) => Promise<void> };
  clients: {
    matchAll: (o: object) => Promise<{ url: string; postMessage?: (m: unknown) => void; focus: () => Promise<unknown>; navigate?: (u: string) => Promise<unknown> }[]>;
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
  // Tell open pages so the bell refetches without a reload (GRPH-1654).
  const notifyClients = sw.clients
    .matchAll({ type: "window", includeUncontrolled: true })
    .then((wins) => wins.forEach((w) => w.postMessage?.({ type: "alerts-changed" })))
    .catch(() => undefined);
  event.waitUntil(
    Promise.all([
      sw.registration.showNotification(data.title ?? "A question needs a response", {
        body: data.body ?? "",
        tag: data.ticket,
        data: { url: data.url ?? "/triage" },
      }),
      notifyClients,
    ]),
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
