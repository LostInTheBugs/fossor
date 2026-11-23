/* Navigation unique de l'application (§2f / §4.19).
 *
 * Une seule source pour la barre de navigation : chaque page pose
 * <nav id="nav"></nav> et ce fichier y injecte les liens, en marquant
 * l'onglet actif d'après l'URL. Aucune page ne recopie la liste.
 */
(function () {
  "use strict";
  var SECTIONS = [
    { key: "pilotage", label: "Pilotage", items: [
      ["/dashboard", "📊", "Tableau de bord"],
      ["/machines", "🖥️", "Machines"],
      ["/profiles", "⛏️", "Profils"],
      ["/campaigns", "🎯", "Campagnes"],
      ["/benchmark", "🌡️", "Benchmark"]
    ]},
    { key: "suivi", label: "Suivi", items: [
      ["/followup", "📈", "Suivi"],
      ["/history", "🕰️", "Historique"],
      ["/mined", "🪙", "Tokens minés"],
      ["/audit", "📋", "Log"]
    ]},
    { key: "analyse", label: "Analyse", items: [
      ["/analysis", "🔎", "Analyse des tokens"],
      ["/help", "📖", "Comment ça marche"]
    ]},
    { key: "reglages", label: "Réglages", items: [
      ["/settings", "⚙️", "Configuration"],
      ["/vault", "🔐", "Coffre"]
    ]}
  ];
  var mount = document.getElementById("nav");
  if (!mount) { return; }
  var path = window.location.pathname.replace(/\/+$/, "") || "/";

  /* Feuille de style unique : elle gagne sur les .nav-tabs recopiés dans les
   * pages (sélecteur #nav, plus spécifique). Même rendu partout, 56 px de
   * haut, aucune ligne qui passe à la ligne. */
  if (!document.getElementById("navStyles")) {
    var style = document.createElement("style");
    style.id = "navStyles";
    style.textContent = [
      "#nav.nav-tabs{display:block;margin:0 auto 20px;padding:0;border-bottom:1px solid var(--border);",
      "font:600 13.5px/1 'Segoe UI',system-ui,sans-serif;position:sticky;top:0;z-index:140;",
      "background:var(--bg,#0d1117);width:100%;max-width:100%;box-sizing:border-box}",
      "#nav .nav-inner{display:flex;align-items:center;gap:10px;height:56px;width:100%;min-width:0}",
      "#nav .nav-brand{display:flex;align-items:center;gap:7px;color:var(--txt,#e6edf3);",
      "text-decoration:none;font-weight:800;letter-spacing:.02em;padding:0 6px;flex:0 0 auto}",
      "#nav .nav-brand span{color:var(--muted,#8b949e);font-weight:600;font-size:11px}",
      "#nav .nav-groups{display:flex;align-items:stretch;gap:2px;height:56px;min-width:0;flex:1 1 auto;overflow-x:auto;scrollbar-width:none}",
      "#nav .nav-groups::-webkit-scrollbar{display:none}",
      "#nav .nav-group{position:relative;display:flex;align-items:stretch;flex:0 0 auto}",
      "#nav .nav-group-btn{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted,#8b949e);",
      "font:inherit;font-weight:600;padding:0 13px;cursor:pointer;display:flex;align-items:center;gap:5px;white-space:nowrap}",
      "#nav .nav-group-btn:hover{color:var(--txt,#e6edf3)}",
      "#nav .nav-group.active .nav-group-btn{color:var(--txt,#e6edf3);border-bottom-color:#3fb950}",
      "#nav .nav-group-btn:after{content:'▾';font-size:9px;opacity:.7}",
      "#nav .nav-drop{display:none;position:absolute;top:56px;left:0;min-width:220px;padding:6px;",
      "background:var(--card,#161b22);border:1px solid var(--border,#30363d);border-radius:0 0 10px 10px;",
      "box-shadow:0 10px 26px rgba(0,0,0,.45);flex-direction:column;gap:1px}",
      "#nav .nav-group.open .nav-drop{display:flex}",
      "#nav .nav-drop a{display:flex;align-items:center;gap:8px;color:var(--muted,#8b949e);text-decoration:none;",
      "padding:9px 11px;border-radius:6px;white-space:nowrap;border:0}",
      "#nav .nav-drop a:hover{color:var(--txt,#e6edf3);background:rgba(110,118,129,.15)}",
      "#nav .nav-drop a.active{color:#fff;background:rgba(63,185,80,.18);box-shadow:inset 2px 0 0 #3fb950}",
      "#nav .nav-current{display:none;color:var(--muted,#8b949e);font-weight:600;white-space:nowrap;",
      "overflow:hidden;text-overflow:ellipsis;flex:1 1 auto;text-align:right;padding-right:4px}",
      "@media(max-width:760px){",
      "#nav .nav-brand span{display:none}",
      "#nav .nav-group-btn{padding:0 9px;font-size:12.5px}",
      "#nav .nav-drop{position:fixed;top:56px;left:8px;right:8px;min-width:0;max-height:70vh;overflow:auto}",
      "}",
      "@media(max-width:430px){",
      "#nav .nav-brand{display:none}",
      "#nav .nav-current{display:block;font-size:12px;max-width:38%}",
      "#nav .nav-group-btn{padding:0 8px;font-size:12px}",
      "}"
    ].join("");
    document.head.appendChild(style);
  }

  mount.className = "nav-tabs";
  mount.innerHTML = "";
  var inner = document.createElement("div");
  inner.className = "nav-inner";
  var brand = document.createElement("a");
  brand.className = "nav-brand";
  brand.href = "/dashboard";
  brand.innerHTML = "Fossor <span>minage piloté</span>";
  inner.appendChild(brand);
  var groups = document.createElement("div");
  groups.className = "nav-groups";
  var current = "";
  SECTIONS.forEach(function (section) {
    var active = section.items.some(function (item) { return item[0] === path; });
    var group = document.createElement("div");
    group.className = "nav-group" + (active ? " active" : "");
    group.setAttribute("data-section", section.key);
    var button = document.createElement("button");
    button.type = "button";
    button.className = "nav-group-btn";
    button.setAttribute("aria-expanded", "false");
    button.textContent = section.label;
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      var open = group.classList.toggle("open");
      button.setAttribute("aria-expanded", open ? "true" : "false");
      Array.prototype.forEach.call(
        document.querySelectorAll("#nav .nav-group"),
        function (other) {
          if (other !== group) {
            other.classList.remove("open");
            var b = other.querySelector(".nav-group-btn");
            if (b) { b.setAttribute("aria-expanded", "false"); }
          }
        }
      );
    });
    var drop = document.createElement("div");
    drop.className = "nav-drop";
    section.items.forEach(function (item) {
      var link = document.createElement("a");
      link.href = item[0];
      link.textContent = item[1] + " " + item[2];
      if (item[0] === path) {
        link.className = "active";
        link.setAttribute("aria-current", "page");
        current = section.label + " · " + item[2];
      }
      drop.appendChild(link);
    });
    group.appendChild(button);
    group.appendChild(drop);
    groups.appendChild(group);
  });
  inner.appendChild(groups);
  var here = document.createElement("span");
  here.className = "nav-current";
  here.textContent = current;
  inner.appendChild(here);
  mount.appendChild(inner);
  document.addEventListener("click", function () {
    Array.prototype.forEach.call(
      document.querySelectorAll("#nav .nav-group"),
      function (g) {
        g.classList.remove("open");
        var b = g.querySelector(".nav-group-btn");
        if (b) { b.setAttribute("aria-expanded", "false"); }
      }
    );
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      Array.prototype.forEach.call(
        document.querySelectorAll("#nav .nav-group"),
        function (g) { g.classList.remove("open"); }
      );
    }
  });

  /* §5.7 — bandeau : il dit la vérité du parc, machine armée comprise. */
  function banner(text, real) {
    var existing = document.getElementById("simBanner");
    if (existing) {
      existing.textContent = text;
      existing.style.background = real ? "#1a3a2a" : "#3a2f1a";
      existing.style.color = real ? "#9fe0b4" : "#f0c674";
      existing.style.borderBottomColor = real ? "#3fb950" : "#d29922";
      return;
    }
    var bar = document.createElement("div");
    bar.id = "simBanner";
    bar.setAttribute("role", "status");
    bar.style.cssText =
      "position:sticky;top:0;z-index:150;padding:7px 16px;font:600 12.5px/1.4 " +
      "'Segoe UI',system-ui,sans-serif;text-align:center";
    bar.textContent = text;
    bar.style.background = real ? "#1a3a2a" : "#3a2f1a";
    bar.style.color = real ? "#9fe0b4" : "#f0c674";
    bar.style.borderBottom = "1px solid " + (real ? "#3fb950" : "#d29922");
    document.body.insertBefore(bar, document.body.firstChild);
  }

  function pilotageBanner(data) {
    var pilotage = (data && data.pilotage) || {};
    var simulated = pilotage.simulated || [];
    var warning = pilotage.warning || pilotage.warnings || "";
    var existing = document.getElementById("simBanner");
    // 2026.11.021 : le bandeau ne parle que d'un reste de simulation (ou d'un
    // avertissement). Toutes les machines pilotables en réel = aucun bandeau ;
    // les ASIC, suivis en lecture seule, ne comptent pas.
    if (!warning && !simulated.length) {
      if (existing) { existing.remove(); }
      return;
    }
    if (warning) { banner(String(warning)); return; }
    banner(
      simulated.length + (simulated.length > 1 ? " machines restent" : " machine reste") +
      " en simulation : aucune commande n'est exécutée sur " +
      (simulated.length > 1 ? "elles" : "elle") + " — " + simulated.join(" · ")
    );
  }

  /* Préprod — bandeau discret quand l'authentification est coupée. */
  function authBanner(ip) {
    if (document.getElementById("authBanner")) { return; }
    var bar = document.createElement("div");
    bar.id = "authBanner";
    bar.setAttribute("role", "status");
    bar.style.cssText =
      "position:sticky;top:0;z-index:149;background:#1a2a3a;color:#8ab4f8;" +
      "border-bottom:1px solid #2f5d8a;padding:6px 16px;font:600 12px/1.4 " +
      "'Segoe UI',system-ui,sans-serif;text-align:center";
    bar.textContent = "Authentification désactivée (réseau de confiance)" + (ip ? " — " + ip : "");
    document.body.insertBefore(bar, document.body.firstChild);
  }

  fetch("/api/miner/settings", { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (data && data.pilotage) { pilotageBanner(data); }
      else if (data && data.simulation === true) {
        banner("Simulation sur toutes les machines — aucune commande n'est exécutée.");
      }
    })
    .catch(function () { /* page sans API : pas de bandeau */ });

  fetch("/api/auth/state", { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (data && data.bypass === true) { authBanner(data.client_ip); }
    })
    .catch(function () { /* page sans API : pas de bandeau */ });
})();
