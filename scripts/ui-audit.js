// A browser audit of one admin page: run it in the page (the browser tool, or the console) after loading it. It reports
//   - horizontal overflow, in the light and the dark theme
//   - text whose measured contrast is below WCAG AA (4.5:1, or 3:1 for large text), in both themes
//   - controls with no accessible name, images with no alt, duplicate ids, heading problems, tables with no caption
//   - tap targets smaller than 24 by 24 CSS pixels
// Usage: put this file where the page can load it (a dev copy under the static folder), add it with a script tag or
// paste it into the console, then call window.__audit(). It changes nothing on the server.
(function () {
  "use strict";
  var canvas = document.createElement("canvas"); canvas.width = canvas.height = 1;
  var ctx = canvas.getContext("2d", { willReadFrequently: true });

  function rgba(css) {
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = "#000"; ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1);
    var d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255];
  }
  function over(top, bottom) {
    var a = top[3] + bottom[3] * (1 - top[3]);
    if (a === 0) return [0, 0, 0, 0];
    return [0, 1, 2].map(function (i) { return (top[i] * top[3] + bottom[i] * bottom[3] * (1 - top[3])) / a; }).concat([a]);
  }
  function lum(c) {
    var v = c.slice(0, 3).map(function (x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
  }
  function ratio(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function background(el) {
    var layers = [];
    for (var e = el; e; e = e.parentElement) {
      var c = rgba(getComputedStyle(e).backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    var base = rgba(getComputedStyle(document.body).backgroundColor); if (base[3] === 0) base = [255, 255, 255, 1];
    var out = base; for (var i = layers.length - 1; i >= 0; i--) out = over(layers[i], out);
    return out;
  }
  function visible(el) {
    var r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && parseFloat(s.opacity) > 0;
  }
  function label(el) {
    var id = el.id ? "#" + el.id : "", cls = typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/)[0] : "";
    return el.tagName.toLowerCase() + id + cls;
  }

  function lowContrast() {
    var seen = {}, found = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      var node = walker.currentNode, text = node.nodeValue.trim(); if (!text) continue;
      var el = node.parentElement;
      if (!el || /^(SCRIPT|STYLE|NOSCRIPT|OPTION)$/.test(el.tagName) || el.closest(".sr-only, [hidden], dialog:not([open]), svg, [disabled], .sprite")) continue;
      if (!visible(el)) continue;
      var s = getComputedStyle(el), fg = rgba(s.color), bg = background(el);
      fg = over(fg, bg);
      var size = parseFloat(s.fontSize), bold = parseInt(s.fontWeight, 10) >= 700;
      var need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5, got = ratio(fg, bg);
      if (got < need) {
        var key = label(el) + "|" + s.color + "|" + got.toFixed(1);
        if (!seen[key]) { seen[key] = 1; found.push({ el: label(el), text: text.slice(0, 30), ratio: +got.toFixed(2), need: need }); }
      }
    }
    return found.slice(0, 8);
  }

  function names() {
    var bad = [];
    document.querySelectorAll("input:not([type=hidden]), select, textarea").forEach(function (el) {
      if (!visible(el) && el.type !== "checkbox") return;
      var named = (el.labels && el.labels.length) || el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.getAttribute("title");
      if (!named) bad.push("no label: " + label(el) + (el.name ? "[name=" + el.name + "]" : ""));
    });
    document.querySelectorAll("button, a[href], [role=button]").forEach(function (el) {
      if (!visible(el) && !el.closest(".sidebar")) return;
      var t = (el.textContent || "").trim();
      if (!t && !el.getAttribute("aria-label") && !el.getAttribute("aria-labelledby") && !el.getAttribute("title") && !el.querySelector("img[alt]"))
        bad.push("no name: " + label(el));
    });
    document.querySelectorAll("img:not([alt])").forEach(function (el) { bad.push("img without alt: " + label(el)); });
    return bad.slice(0, 10);
  }
  function structure() {
    var out = [], ids = {}, dup = [];
    document.querySelectorAll("[id]").forEach(function (el) { if (ids[el.id]) dup.push(el.id); ids[el.id] = 1; });
    if (dup.length) out.push("duplicate ids: " + dup.slice(0, 4).join(", "));
    var h1 = document.querySelectorAll("h1").length; if (h1 !== 1) out.push(h1 + " h1 elements");
    var last = 0;
    document.querySelectorAll("main h1, main h2, main h3, main h4").forEach(function (h) {
      var n = +h.tagName[1]; if (last && n > last + 1) out.push("heading jumps h" + last + " to h" + n + ": " + h.textContent.trim().slice(0, 30)); last = n;
    });
    document.querySelectorAll("main table").forEach(function (t) { if (!t.querySelector("caption") && !t.getAttribute("aria-label")) out.push("table without caption"); });
    if (document.querySelectorAll("main").length !== 1) out.push("expected one main landmark");
    if (!document.title) out.push("no title");
    if (document.querySelector("[tabindex]:not([tabindex='0']):not([tabindex='-1'])")) out.push("positive tabindex");
    return out.slice(0, 8);
  }
  function small() {
    var bad = [];
    document.querySelectorAll("button, input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, .btn, .tab, .nav-link").forEach(function (el) {
      if (!visible(el)) return;
      var r = el.getBoundingClientRect(); if (r.width < 24 || r.height < 24) bad.push(label(el) + " " + Math.round(r.width) + "x" + Math.round(r.height));
    });
    return bad.slice(0, 6);
  }

  window.__audit = function () {
    var root = document.documentElement, saved = root.getAttribute("data-theme"), out = { path: location.pathname, width: innerWidth, themes: {} };
    var still = document.createElement("style"); still.textContent = "*, *::before, *::after { transition: none !important; animation: none !important; }"; document.head.appendChild(still);
    ["light", "dark"].forEach(function (theme) {
      root.setAttribute("data-theme", theme);
      document.getAnimations().forEach(function (a) { try { a.finish(); } catch (e) {} }); // a hidden tab never renders, so settle transitions by hand
      out.themes[theme] = { overflow: root.scrollWidth > root.clientWidth + 1 ? root.scrollWidth + ">" + root.clientWidth : false, contrast: lowContrast() };
    });
    if (saved === null) root.removeAttribute("data-theme"); else root.setAttribute("data-theme", saved);
    still.remove();
    out.names = names(); out.structure = structure(); out.small = small();
    return out;
  };
})();
