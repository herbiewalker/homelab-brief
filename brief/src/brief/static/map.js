// The interactive network map (docs/guide/07-network-map.md). Runs on /map.html (read-only) and on the
// admin Network map page (which adds "Save as default"). Cytoscape.js is vendored; nothing here makes a
// request to another origin, and no text from the data is ever put into the page as HTML.
(function () {
  "use strict";
  var root = document.getElementById("map");
  if (!root) return;
  var api = root.getAttribute("data-api");
  var saveUrl = root.getAttribute("data-save");
  var csrf = root.getAttribute("data-csrf") || "";
  var STORE = "brief-map-layout-v1";
  var COLORS = { green: "#2f9e44", yellow: "#f59f00", red: "#e03131", unknown: "#868e96" };
  var EDGE = { uplink: "#495057", depends: "#1c7ed6", power: "#c2255c", hosts: "#adb5bd" };
  var cy = null, doc = null, timer = null;
  var layers = { network: true, services: true, power: true };

  function el(id) { return document.getElementById(id); }
  function say(text) { el("map-note").textContent = text; }

  function readLocal() {
    try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { return {}; }
  }
  function writeLocal(layout) {
    try { localStorage.setItem(STORE, JSON.stringify(layout)); } catch (e) {}
  }
  function currentLayout() {
    var out = {};
    cy.nodes().forEach(function (n) { var p = n.position(); out[n.id()] = { x: Math.round(p.x), y: Math.round(p.y) }; });
    return out;
  }
  function labelOf(n) {
    var bits = [];
    (n.badges || []).forEach(function (b) {
      if (b === "new") bits.push("NEW");
      else if (b === "update") bits.push("UPDATE");
      else if (b.indexOf("findings:") === 0) bits.push("! " + b.slice(9));
    });
    return bits.length ? n.label + "\n" + bits.join(" ") : n.label;
  }
  function visibleLayer(layer) { return layers[layer] !== false; }

  function elements(d) {
    var list = [];
    d.nodes.forEach(function (n) {
      list.push({ group: "nodes", data: {
        id: n.id, parent: n.parent || undefined, label: labelOf(n), name: n.label, kind: n.kind,
        state: n.state, layer: n.layer, raw: n } });
    });
    d.edges.forEach(function (e, i) {
      if (e.kind === "hosts") return;  // containment is drawn as the compound box; an edge from a box to its own child breaks layouts
      list.push({ group: "edges", data: { id: "e" + i + ":" + e.src + ">" + e.dst, source: e.src, target: e.dst,
        kind: e.kind, layer: e.layer, label: e.label || "" } });
    });
    return list;
  }

  function build(d) {
    var saved = d.layout || {}, mine = readLocal();
    var positions = {};
    d.nodes.forEach(function (n) { positions[n.id] = mine[n.id] || saved[n.id] || null; });
    var anySaved = d.nodes.some(function (n) { return positions[n.id]; });
    // Nothing arranged yet: use the server's automatic placement (the same one map.svg uses).
    if (!anySaved) d.nodes.forEach(function (n) { positions[n.id] = (d.auto && d.auto[n.id]) || null; });
    cy = cytoscape({
      container: root, elements: elements(d), wheelSensitivity: 0.3, minZoom: 0.2, maxZoom: 3,
      style: [
        { selector: "node", style: { label: "data(label)", "text-wrap": "wrap", "text-valign": "center",
          "text-halign": "center", color: "#fff", "font-size": 12, shape: "round-rectangle",
          "background-color": function (n) { return COLORS[n.data("state")] || COLORS.unknown; },
          width: 130, height: 40, "text-max-width": 120 } },
        { selector: "node:parent", style: { "background-opacity": 0.08, "border-width": 3,
          "border-color": function (n) { return COLORS[n.data("state")] || COLORS.unknown; },
          "text-valign": "top", color: "#555", padding: 14, "text-margin-y": -4 } },
        { selector: "node[kind = 'client_group']", style: { shape: "ellipse", width: 120 } },
        { selector: "node[kind = 'internet']", style: { shape: "diamond", width: 90, height: 60 } },
        { selector: "edge", style: { width: 2, "line-color": function (e) { return EDGE[e.data("kind")] || "#495057"; },
          "curve-style": "bezier", label: "data(label)", "font-size": 10 } },
        { selector: "edge[kind = 'depends']", style: { "line-style": "dashed" } },
        { selector: "edge[kind = 'power']", style: { "line-style": "dotted" } },
        { selector: ".gone, .off", style: { display: "none" } },
        { selector: ":selected", style: { "border-width": 4, "border-color": "#1c7ed6" } }
      ]
    });
    placeNodes(positions);
    cy.on("tap", "node", function (ev) { detail(ev.target.data("raw")); });
    cy.on("dbltap", "node:parent", function (ev) { ev.target.children().toggleClass("gone"); });
    cy.on("dragfree", "node", function () { writeLocal(currentLayout()); });
    applyLayers();
  }

  // Nodes with a saved position stay exactly where they were put. With nothing saved, the whole graph is
  // laid out automatically; otherwise only the new nodes are placed, in a grid in free space beside the rest.
  function placeNodes(positions) {
    var fresh = [], placed = 0;
    cy.nodes().forEach(function (n) {
      if (n.isParent()) return;
      var p = positions[n.id()];
      if (p) { n.position({ x: p.x, y: p.y }); placed++; } else fresh.push(n);
    });
    if (!placed) {
      var name = (doc && doc.auto_layout) || "cose";
      if (["cose", "grid", "circle", "concentric", "breadthfirst"].indexOf(name) < 0) name = "cose";
      cy.layout({ name: name, animate: false, fit: false, padding: 30 }).run();
    } else if (fresh.length) {
      var box = cy.nodes().filter(function (n) { return !n.isParent() && positions[n.id()]; }).boundingBox();
      var cols = Math.ceil(Math.sqrt(fresh.length));
      fresh.forEach(function (n, i) {
        n.position({ x: box.x2 + 120 + (i % cols) * 150, y: box.y1 + Math.floor(i / cols) * 70 });
      });
    }
    cy.fit(undefined, 40);
  }

  function applyLayers() {
    cy.batch(function () {
      cy.elements().forEach(function (x) {
        var layer = x.data("layer");
        var hide = layer && !visibleLayer(layer) && !(x.isNode() && x.data("raw") && x.data("raw").parent);
        x.toggleClass("off", !!hide);
      });
    });
  }

  function detail(n) {
    var box = el("map-detail");
    box.textContent = "";
    function row(k, v) {
      if (v === undefined || v === null || v === "") return;
      var p = document.createElement("p");
      var b = document.createElement("strong");
      b.textContent = k + ": ";
      p.appendChild(b);
      p.appendChild(document.createTextNode(String(v)));
      box.appendChild(p);
    }
    var h = document.createElement("h3");
    h.textContent = n.label;
    box.appendChild(h);
    row("Kind", n.kind);
    row("Status", n.state);
    row("Source", n.source);
    var a = n.attrs || {};
    Object.keys(a).sort().forEach(function (k) { if (typeof a[k] !== "object") row(k, a[k]); });
    row("Badges", (n.badges || []).join(", "));
    row("First seen", n.first_seen);
    row("Last seen", n.last_seen);
  }

  // Refresh: update status and badges, add and remove nodes, never move one that is already placed.
  function refresh(d) {
    var seen = {};
    d.nodes.forEach(function (n) {
      seen[n.id] = true;
      var x = cy.getElementById(n.id);
      if (x.length) x.data({ label: labelOf(n), state: n.state, raw: n });
    });
    var fresh = d.nodes.filter(function (n) { return !cy.getElementById(n.id).length; });
    var gone = cy.nodes().filter(function (n) { return !seen[n.id()]; });
    if (fresh.length || gone.length) {
      cy.destroy();
      doc = d;
      build(d);
    }
    doc = d;
  }

  function load(first) {
    return fetch(api, { headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error(r.status === 404 ? "The network map is switched off (map.yaml: enabled)." : "HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        if (first || !cy) { doc = d; build(d); } else { refresh(d); }
        say((d.run_ok ? "" : "No data today: colours are not current. ") + "Updated " + d.generated_at);
        if (!timer) timer = setInterval(function () { load(false).catch(function () {}); }, Math.max(30, d.refresh_s || 300) * 1000);
      })
      .catch(function (e) { say(String(e.message || e)); });
  }

  function exportLayout() {
    var blob = new Blob([JSON.stringify(currentLayout(), null, 1)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "map_layout.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  function importLayout(file) {
    var reader = new FileReader();
    reader.onload = function () {
      var data;
      try { data = JSON.parse(String(reader.result)); } catch (e) { say("That file is not valid JSON."); return; }
      var ok = {};
      Object.keys(data || {}).forEach(function (k) {
        var p = data[k];
        if (p && typeof p.x === "number" && typeof p.y === "number" && isFinite(p.x) && isFinite(p.y)) ok[k] = { x: p.x, y: p.y };
      });
      writeLocal(ok);
      cy.nodes().forEach(function (n) { if (ok[n.id()] && !n.isParent()) n.position(ok[n.id()]); });
      cy.fit(undefined, 40);
      say("Layout imported.");
    };
    reader.readAsText(file);
  }
  function saveDefault() {
    fetch(saveUrl, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(currentLayout()) })
      .then(function (r) { return r.text().then(function (t) { say(r.ok ? "Saved as the default layout." : t); }); })
      .catch(function () { say("Could not save the layout."); });
  }

  ["network", "services", "power"].forEach(function (name) {
    var box = el("layer-" + name);
    if (box) box.addEventListener("change", function () { layers[name] = box.checked; if (cy) applyLayers(); });
  });
  el("map-export").addEventListener("click", function () { if (cy) exportLayout(); });
  el("map-import").addEventListener("change", function (ev) { if (cy && ev.target.files[0]) importLayout(ev.target.files[0]); ev.target.value = ""; });
  el("map-reset").addEventListener("click", function () { writeLocal({}); if (cy) { cy.destroy(); cy = null; load(true); } });
  if (saveUrl && el("map-save")) el("map-save").addEventListener("click", function () { if (cy) saveDefault(); });
  load(true);
})();
