// Small Alpine components for the admin pages. The CSP build of Alpine runs no inline expressions, so
// everything the templates use is a named property or method defined here.
document.addEventListener("alpine:init", function () {
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
  }
  Alpine.data("shell", function () {
    return {
      open: false,
      theme: "auto",
      init: function () {
        try { this.theme = localStorage.getItem("theme") || "auto"; } catch (e) {}
      },
      toggleMenu: function () { this.open = !this.open; },
      get menuClass() { return this.open ? "side open" : "side"; },
      cycleTheme: function () {
        var order = ["auto", "light", "dark"];
        this.theme = order[(order.indexOf(this.theme) + 1) % 3];
        try { localStorage.setItem("theme", this.theme); } catch (e) {}
        applyTheme(this.theme);
      },
      get themeLabel() { return "Theme: " + this.theme; },
    };
  });
  // A form that asks before doing something that cannot be undone.
  Alpine.data("confirmForm", function () {
    return {
      ask: function (event) {
        var text = event.currentTarget.getAttribute("data-confirm");
        if (text && !window.confirm(text)) event.preventDefault();
      },
    };
  });
});
