import "@testing-library/jest-dom/vitest";

// jsdom lacks these; Radix (dropdowns, dialogs) calls them.
Element.prototype.hasPointerCapture = () => false;
Element.prototype.setPointerCapture = () => {};
Element.prototype.releasePointerCapture = () => {};
Element.prototype.scrollIntoView = () => {};

// jsdom logs and throws on canvas.getContext. A missing 2D context is the
// reduced view, so the stub returns null instead of failing the render.
HTMLCanvasElement.prototype.getContext = (() => null) as HTMLCanvasElement["getContext"];
