// Behaviour for the application shell: menu drawer, theme, toasts, confirm dialogs, copy buttons, and htmx
// feedback. Plain script (no inline code anywhere, so the Content-Security-Policy stays strict); everything is wired
// by data attributes and event delegation, so content swapped in by htmx works without re-binding.
(function () {
  "use strict";
  var root = document.documentElement;

  // ---------------------------------------------------------------- theme: auto -> light -> dark
  var ORDER = ["auto", "light", "dark"], ICON = { auto: "monitor", light: "sun", dark: "moon" };
  function theme() { try { return localStorage.getItem("theme") || "auto"; } catch (e) { return "auto"; } }
  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t); else root.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-icon]").forEach(function (use) {
      use.setAttribute("href", "#i-" + ICON[t]);
    });
    document.querySelectorAll("[data-theme-label]").forEach(function (el) { el.textContent = "Theme: " + t; });
  }
  applyTheme(theme());

  // ---------------------------------------------------------------- toasts
  function toasts() {
    var box = document.getElementById("toasts");
    if (!box) { box = document.createElement("div"); box.id = "toasts"; box.className = "toasts"; box.setAttribute("aria-live", "polite"); document.body.appendChild(box); }
    return box;
  }
  var GLYPH = { ok: "check-circle", bad: "alert-circle", warn: "alert", info: "info" };
  function icon(name) {
    var ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"), use = document.createElementNS(ns, "use");
    svg.setAttribute("class", "icon"); svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#i-" + name); svg.appendChild(use); return svg;
  }
  // window.toast("ok", "Saved", {undo: "/admin/...", undoLabel: "Undo", csrf: "..."})
  function toast(kind, text, opts) {
    opts = opts || {};
    var el = document.createElement("div"), body = document.createElement("div"), actions = document.createElement("div");
    el.className = "toast toast--" + (kind || "info"); el.setAttribute("role", kind === "bad" ? "alert" : "status");
    body.className = "toast-text"; body.textContent = text;
    actions.className = "toast-actions";
    if (opts.undo) {
      var form = document.createElement("form"), btn = document.createElement("button");
      form.method = "post"; form.action = opts.undo; form.className = "inline";
      var tok = document.createElement("input"); tok.type = "hidden"; tok.name = "csrf_token"; tok.value = opts.csrf || csrfToken();
      btn.type = "submit"; btn.className = "btn btn--ghost btn--sm"; btn.textContent = opts.undoLabel || "Undo";
      Object.keys(opts.fields || {}).forEach(function (k) {
        var h = document.createElement("input"); h.type = "hidden"; h.name = k; h.value = opts.fields[k]; form.appendChild(h);
      });
      form.appendChild(tok); form.appendChild(btn); actions.appendChild(form);
    }
    var close = document.createElement("button");
    close.type = "button"; close.className = "btn btn--ghost btn--sm btn--icon"; close.setAttribute("aria-label", "Dismiss");
    close.appendChild(icon("x")); close.addEventListener("click", function () { el.remove(); });
    actions.appendChild(close);
    el.appendChild(icon(GLYPH[kind] || "info")); el.appendChild(body); el.appendChild(actions);
    toasts().appendChild(el);
    var ms = opts.sticky ? 0 : (kind === "bad" ? 9000 : 4500);
    if (ms) setTimeout(function () { el.remove(); }, ms);
  }
  window.toast = toast;
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf"]'); return m ? m.getAttribute("content") : "";
  }

  // The server asks for a toast with the HX-Trigger header: {"toast": {"kind": "ok", "text": "Saved"}}.
  document.body.addEventListener("toast", function (ev) {
    var d = ev.detail || {}; if (d.text) toast(d.kind || "info", d.text, d);
    // Several cards can edit one file: after a save, every card of that file takes the new version stamp, so the
    // next save from another card is not refused as "changed while you were editing".
    if (d.file && d.digest) document.querySelectorAll("form[data-file]").forEach(function (f) {
      if (f.getAttribute("data-file") === d.file) { var b = f.querySelector('input[name="base"]'); if (b) b.value = d.digest; }
    });
  });
  document.body.addEventListener("htmx:responseError", function (ev) {
    var x = ev.detail.xhr, msg = (x && x.responseText || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 220);
    toast("bad", msg || ("The server answered " + (x && x.status) + "."));
  });
  // A form the server refused (422) answers with the same form, marked up with its errors: show it.
  document.body.addEventListener("htmx:beforeSwap", function (ev) {
    if (ev.detail.xhr && ev.detail.xhr.status === 422) { ev.detail.shouldSwap = true; ev.detail.isError = false; }
  });
  document.body.addEventListener("htmx:sendError", function () { toast("bad", "Could not reach the server. Check the connection and try again."); });
  document.body.addEventListener("htmx:timeout", function () { toast("warn", "That is taking longer than expected."); });

  // ---------------------------------------------------------------- confirm dialog
  function confirmDialog(message, okLabel, danger) {
    return new Promise(function (resolve) {
      var dlg = document.getElementById("confirm-dialog");
      if (!dlg || typeof dlg.showModal !== "function") { resolve(window.confirm(message)); return; }
      dlg.querySelector("[data-confirm-text]").textContent = message;
      var ok = dlg.querySelector("[data-confirm-ok]");
      ok.textContent = okLabel || "Continue"; ok.className = "btn " + (danger ? "btn--danger" : "");
      function done(v) { dlg.close(); ok.onclick = null; cancel.onclick = null; resolve(v); }
      var cancel = dlg.querySelector("[data-confirm-cancel]");
      ok.onclick = function () { done(true); }; cancel.onclick = function () { done(false); };
      dlg.addEventListener("cancel", function once() { dlg.removeEventListener("cancel", once); resolve(false); });
      dlg.showModal(); cancel.focus();
    });
  }
  // data-confirm="Revoke this token?" on a form or a button; data-confirm-ok="Revoke" names the action.
  document.addEventListener("submit", function (ev) {
    var form = ev.target, src = ev.submitter, text = (src && src.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (!text || form.__confirmed) { form.__confirmed = false; return; }
    ev.preventDefault();
    var label = (src && src.getAttribute("data-confirm-ok")) || form.getAttribute("data-confirm-ok");
    var danger = (src && src.hasAttribute("data-danger")) || form.hasAttribute("data-danger");
    confirmDialog(text, label, danger).then(function (yes) {
      if (yes) { form.__confirmed = true; if (form.requestSubmit) form.requestSubmit(src || undefined); else form.submit(); }
    });
  }, true);
  document.body.addEventListener("htmx:confirm", function (ev) {
    var q = ev.detail.question; if (!q) return;
    ev.preventDefault();
    confirmDialog(q, ev.detail.target && ev.detail.target.getAttribute("data-confirm-ok"), ev.detail.target && ev.detail.target.hasAttribute("data-danger"))
      .then(function (yes) { if (yes) ev.detail.issueRequest(true); });
  });

  // ---------------------------------------------------------------- clicks: menu, theme, copy
  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-action]"); if (!t) return;
    var a = t.getAttribute("data-action");
    if (a === "menu") { document.body.classList.toggle("menu-open"); t.setAttribute("aria-expanded", document.body.classList.contains("menu-open")); }
    else if (a === "menu-close") { document.body.classList.remove("menu-open"); }
    else if (a === "theme") {
      var next = ORDER[(ORDER.indexOf(theme()) + 1) % ORDER.length];
      try { localStorage.setItem("theme", next); } catch (e) {}
      applyTheme(next);
    } else if (a === "copy") {
      var text = t.getAttribute("data-copy") || (document.querySelector(t.getAttribute("data-copy-from")) || {}).textContent || "";
      if (navigator.clipboard) navigator.clipboard.writeText(text.trim()).then(function () { toast("ok", "Copied."); }, function () { toast("warn", "Could not copy; select it and copy by hand."); });
    }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") document.body.classList.remove("menu-open");
  });
  document.addEventListener("click", function (ev) {
    if (ev.target.closest(".sidebar a") && window.matchMedia("(max-width: 960px)").matches) document.body.classList.remove("menu-open");
  });

  // ---------------------------------------------------------------- forms: show the first server error next to its field
  document.body.addEventListener("htmx:afterSwap", function () { applyTheme(theme()); });
  document.addEventListener("invalid", function (ev) {
    var f = ev.target.closest && ev.target.closest(".field"); if (f) f.classList.add("invalid");
  }, true);
  document.addEventListener("input", function (ev) {
    var f = ev.target.closest && ev.target.closest(".field"); if (f) f.classList.remove("invalid");
  });
})();

// A polled region (data-poll) is not refreshed while the tab is hidden or while someone is using a control inside it,
// so the page never changes under a click.
document.body.addEventListener("htmx:beforeRequest", function (ev) {
  var el = ev.detail.elt;
  if (!el || !el.hasAttribute || !el.hasAttribute("data-poll")) return;
  var active = document.activeElement;
  if (document.hidden || (active && active !== document.body && el.contains(active) && /^(BUTTON|INPUT|SELECT|TEXTAREA|A)$/.test(active.tagName))) ev.preventDefault();
});
// A time of day typed in UTC (data-local-time points at the input) is also shown in the viewer's own time zone.
(function () {
  function show(el) {
    var input = document.getElementById(el.getAttribute("data-local-time")), m = input && /^(\d\d):(\d\d)$/.exec(input.value.trim());
    if (!m) { el.textContent = ""; return; }
    var d = new Date(Date.UTC(2000, 0, 1, +m[1], +m[2]));
    el.textContent = "UTC. In your time zone: " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + ".";
  }
  function all() { document.querySelectorAll("[data-local-time]").forEach(show); }
  document.addEventListener("input", all);
  document.body.addEventListener("htmx:afterSwap", all);
  all();
})();

// List pages: bulk selection and saved views. Plain script, wired by data attributes and event delegation.
(function () {
  "use strict";

  // ---------------------------------------------------------------- bulk selection
  // A container with data-bulk holds checkboxes [data-bulk-item], one [data-bulk-all], a [data-bulk-bar] that is shown when
  // something is ticked, and a [data-bulk-count] for "3 selected".
  function bulkUpdate(form) {
    var items = form.querySelectorAll("[data-bulk-item]"), on = 0;
    items.forEach(function (i) { if (i.checked) on++; });
    var bar = form.querySelector("[data-bulk-bar]"), count = form.querySelector("[data-bulk-count]"), all = form.querySelector("[data-bulk-all]");
    if (bar) bar.hidden = on === 0;
    if (count) count.textContent = on + " selected";
    if (all) { all.checked = on > 0 && on === items.length; all.indeterminate = on > 0 && on < items.length; }
  }
  document.addEventListener("change", function (ev) {
    var t = ev.target, form = t.closest && t.closest("[data-bulk]");
    if (!form) return;
    if (t.hasAttribute("data-bulk-all")) form.querySelectorAll("[data-bulk-item]").forEach(function (i) { i.checked = t.checked; });
    if (t.hasAttribute("data-bulk-all") || t.hasAttribute("data-bulk-item")) bulkUpdate(form);
  });
  document.body.addEventListener("htmx:afterSwap", function () { document.querySelectorAll("[data-bulk]").forEach(bulkUpdate); });
  document.querySelectorAll("[data-bulk]").forEach(bulkUpdate);

  // ---------------------------------------------------------------- saved views (per browser)
  // <div data-saved-views="findings"> shows the views saved for that page and a small form to save the current filters.
  function store(key) { try { return JSON.parse(localStorage.getItem("views:" + key) || "[]"); } catch (e) { return []; } }
  function keep(key, list) { try { localStorage.setItem("views:" + key, JSON.stringify(list.slice(0, 12))); } catch (e) {} }
  function renderViews(box) {
    var key = box.getAttribute("data-saved-views"), list = store(key), holder = box.querySelector("[data-views-list]");
    if (!holder) return;
    holder.textContent = "";
    list.forEach(function (v, i) {
      var chip = document.createElement("span"), a = document.createElement("a"), x = document.createElement("button");
      chip.className = "view-chip"; a.href = location.pathname + (v.q ? "?" + v.q : ""); a.textContent = v.name;
      x.type = "button"; x.setAttribute("aria-label", "Remove the saved view " + v.name); x.setAttribute("data-view-remove", String(i)); x.textContent = "×";
      chip.appendChild(a); chip.appendChild(x); holder.appendChild(chip);
    });
    var none = box.querySelector("[data-views-none]"); if (none) none.hidden = list.length > 0;
  }
  document.addEventListener("click", function (ev) {
    var box = ev.target.closest && ev.target.closest("[data-saved-views]");
    if (!box) return;
    var key = box.getAttribute("data-saved-views");
    if (ev.target.closest("[data-view-save]")) {
      var input = box.querySelector("[data-view-name]"), name = (input && input.value || "").trim();
      if (!name) { if (input) input.focus(); return; }
      var list = store(key).filter(function (v) { return v.name !== name; });
      list.unshift({ name: name.slice(0, 40), q: location.search.replace(/^\?/, "") });
      keep(key, list); if (input) input.value = ""; renderViews(box);
    }
    var rm = ev.target.closest("[data-view-remove]");
    if (rm) { var l = store(key); l.splice(+rm.getAttribute("data-view-remove"), 1); keep(key, l); renderViews(box); }
  });
  document.querySelectorAll("[data-saved-views]").forEach(renderViews);
})();
