if (typeof globalThis.ResizeObserver === "undefined") {
  class TestResizeObserver implements ResizeObserver {
    disconnect(): void {
      // jsdom has no layout to observe.
    }
    observe(): void {
      // jsdom has no layout to observe.
    }
    unobserve(): void {
      // jsdom has no layout to observe.
    }
  }

  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true,
    writable: true,
    value: TestResizeObserver,
  });
}

if (typeof window !== "undefined") {
  const noOp = () => {
    return undefined;
  };

  Object.defineProperty(window, "scrollTo", {
    configurable: true,
    writable: true,
    value: noOp,
  });

  Object.defineProperty(window, "scroll", {
    configurable: true,
    writable: true,
    value: noOp,
  });
}
