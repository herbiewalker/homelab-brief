// Runs before the page paints, so a saved light, dark, or auto choice never flashes the other one.
// Dark is the default: with nothing saved, the console is dark.
(function () {
  var t = "dark";
  try {
    var saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark" || saved === "auto") t = saved;
  } catch (e) {}
  document.documentElement.setAttribute("data-theme", t);
})();
