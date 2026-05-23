/* ===========================================================================
   artifact.js — reusable widget library for ai-infra-portfolio artifacts
   Each artifact links this file, then calls the widget(s) it needs.
   Page-specific interactive math stays inline in the artifact's own <script>.
   =========================================================================== */
(function (global) {
  "use strict";

  // ---- helpers -----------------------------------------------------------
  function el(tag, cls, html) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }
  function pct(x, total) { return Math.max(0, Math.min(100, (x / total) * 100)); }

  /* -------------------------------------------------------------------------
     Artifact.topology(rootSelector, config)

     A clickable node topology with an optional Before/After mode switch.
     Click a node → detail panel (specs, role, workloads, why / why-not).
     Toggle mode → each node's role + workloads swap between states.

     config = {
       mesh: "Tailscale mesh · 100.x.x.x/8",
       modes: { current: "現況 · docker-compose", planned: "目標 · K3s" }, // optional
       defaultMode: "current",
       defaultNode: "node-a",
       nodes: [{
         id, name, tag, accent: "green|blue|amber|red|purple",
         excluded: false,                 // dashed border (not in cluster)
         specs: ["8C/16T", "16 GB ⚠️", ...],
         why: "...", whyNot: "...",
         // per-mode role + workloads. If no modes, use a single `default` key.
         byMode: { current: { role, workloads:[], note }, planned: {...} }
       }]
     }
  ------------------------------------------------------------------------- */
  function topology(rootSelector, cfg) {
    const root = typeof rootSelector === "string"
      ? document.querySelector(rootSelector) : rootSelector;
    if (!root) return;

    const hasModes = !!cfg.modes;
    let mode = cfg.defaultMode || (hasModes ? Object.keys(cfg.modes)[0] : "default");
    let selectedId = cfg.defaultNode || cfg.nodes[0].id;

    root.classList.add("topo");
    root.innerHTML = "";

    // mode switch
    let segmented = null;
    if (hasModes) {
      segmented = el("div", "segmented");
      segmented.style.marginBottom = "18px";
      Object.keys(cfg.modes).forEach(function (m) {
        const b = el("button", m === mode ? "active" : "", cfg.modes[m]);
        b.addEventListener("click", function () {
          mode = m;
          Array.from(segmented.children).forEach(function (c) { c.classList.remove("active"); });
          b.classList.add("active");
          renderNodes();
          renderDetail();
        });
        segmented.appendChild(b);
      });
      root.appendChild(segmented);
    }

    // mesh + stem
    if (cfg.mesh) {
      root.appendChild(el("div", "topo-mesh", cfg.mesh));
      root.appendChild(el("div", "topo-stem"));
    }

    const nodesWrap = el("div", "topo-nodes");
    root.appendChild(nodesWrap);

    const detail = el("div", "topo-detail");
    root.appendChild(detail);
    root.appendChild(el("div", "topo-hint", "▲ 點任一節點看規格、角色與分派理據"));

    function modeData(node) {
      return (node.byMode && (node.byMode[mode] || node.byMode.default)) || {};
    }

    function renderNodes() {
      nodesWrap.innerHTML = "";
      cfg.nodes.forEach(function (node) {
        const d = modeData(node);
        const card = el("button",
          "node accent-" + (node.accent || "blue") +
          (node.excluded ? " excluded" : "") +
          (node.id === selectedId ? " selected" : ""));
        card.appendChild(el("span", "ntag", node.tag || ""));
        card.appendChild(el("div", "nname", node.name));
        card.appendChild(el("div", "nrole", d.role || ""));
        card.addEventListener("click", function () {
          selectedId = node.id;
          renderNodes();
          renderDetail();
        });
        nodesWrap.appendChild(card);
      });
    }

    function renderDetail() {
      const node = cfg.nodes.find(function (n) { return n.id === selectedId; }) || cfg.nodes[0];
      const d = modeData(node);
      const accentVar = {
        green: "var(--green)", blue: "var(--blue)", amber: "var(--amber)",
        red: "var(--red)", purple: "var(--purple)"
      }[node.accent || "blue"];
      detail.style.borderLeftColor = accentVar;

      const specs = (node.specs || []).map(function (s) {
        return '<span class="spec">' + s + "</span>";
      }).join("");
      const wls = (d.workloads || []).map(function (w) {
        return '<span class="wl">' + w + "</span>";
      }).join("");

      detail.innerHTML =
        '<div class="dhead"><span class="dname" style="color:' + accentVar + '">' + node.name +
          '</span><span class="drole">' + (d.role || "") + "</span></div>" +
        '<div class="specs">' + specs + "</div>" +
        (wls ? '<div class="workloads">' + wls + "</div>" : "") +
        '<div class="yn">' +
          '<div class="why"><b>為什麼是它</b>' + (node.why || "—") + "</div>" +
          '<div class="whynot"><b>取捨 / 為什麼不</b>' + (node.whyNot || "—") + "</div>" +
        "</div>" +
        (d.note ? '<div class="dnote">' + d.note + "</div>" : "");
    }

    renderNodes();
    renderDetail();
  }

  global.Artifact = { el: el, pct: pct, topology: topology };
})(window);
