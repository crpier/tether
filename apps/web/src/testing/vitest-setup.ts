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
