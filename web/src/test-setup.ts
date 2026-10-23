import "@testing-library/jest-dom/vitest";

// CodeGraphView reads layout, sel, depth, iso, and cell once on mount and
// writes them back with replaceState. A test that opens Matrix or selects a
// node would otherwise hand that link to the next test in the same file.
beforeEach(() => {
  if (!window.location.search) return;
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.hash}`,
  );
});

// jsdom lacks these; Radix (dropdowns, dialogs) calls them.
Element.prototype.hasPointerCapture = () => false;
Element.prototype.setPointerCapture = () => {};
Element.prototype.releasePointerCapture = () => {};
Element.prototype.scrollIntoView = () => {};

// jsdom logs and throws on canvas.getContext. A 2D context that no-ops lets the
// canvas view render its DOM overlay. WebGL2 stays absent here, so unit tests
// take the Canvas 2D fallback unless a test installs its own context. Neither
// context (the stub returns null) is the reduced view.
function noop2d(): CanvasRenderingContext2D {
  return new Proxy({} as CanvasRenderingContext2D, {
    get(target, prop) {
      if (prop in target) return target[prop as keyof CanvasRenderingContext2D];
      // Gradient and pattern factories return an object the caller then paints on.
      // A bare noop would hand back undefined and throw on the first addColorStop.
      if (prop === "createRadialGradient" || prop === "createLinearGradient" || prop === "createPattern") {
        return () => noop2d();
      }
      return () => {};
    },
    set(target, prop, value) {
      (target as unknown as Record<string, unknown>)[prop as string] = value;
      return true;
    },
  });
}
HTMLCanvasElement.prototype.getContext = function (type: string) {
  if (type === "2d") return noop2d();
  return null;
} as HTMLCanvasElement["getContext"];
