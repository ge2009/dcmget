(() => {
  "use strict";

  try {
    const stored = window.localStorage.getItem("dcmget-theme");
    const theme = stored === "light" || stored === "dark"
      ? stored
      : (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    meta?.setAttribute("content", theme === "dark" ? "#0e1518" : "#087481");
  } catch {
    document.documentElement.dataset.theme = "light";
  }
})();
