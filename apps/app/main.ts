// Entry point. The app is grown from here — index.html loads this file as a
// module and every other source file must be a plain import off this graph,
// because the typecheck and build gates bundle exactly this entry.

export function appRoot(): HTMLElement {
  const el = document.getElementById("app");
  if (!el) throw new Error("index.html must provide #app");
  return el;
}

if (typeof document !== "undefined" && document.getElementById("app")) {
  appRoot().textContent = "empty shell — build me";
}
