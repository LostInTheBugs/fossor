/* Navigation unique de l'application (§2f / §4.19).
 *
 * Une seule source pour la barre de navigation : chaque page pose
 * <nav id="nav"></nav> et ce fichier y injecte les liens, en marquant
 * l'onglet actif d'après l'URL. Aucune page ne recopie la liste.
 */
(function () {
  "use strict";
  var LINKS = [
    ["/dashboard", "📊 Tableau de bord"],
    ["/machines", "🖥️ Machines"],
    ["/profiles", "⛏️ Profils"],
    ["/campaigns", "🎯 Campagnes"],
    ["/analysis", "🔎 Analyse des tokens"],
    ["/mined", "🪙 Tokens minés"],
    ["/followup", "📈 Suivi"],
    ["/help", "📖 Comment ça marche"],
    ["/settings", "⚙️ Configuration"],
    ["/audit", "📋 Log"]
  ];
  var mount = document.getElementById("nav");
  if (!mount) { return; }
  var path = window.location.pathname.replace(/\/+$/, "") || "/";
  mount.className = "nav-tabs";
  mount.innerHTML = "";
  LINKS.forEach(function (entry) {
    var link = document.createElement("a");
    link.setAttribute("href", entry[0]);
    link.textContent = entry[1];
    if (path === entry[0]) {
      link.className = "active";
      link.setAttribute("aria-current", "page");
    }
    mount.appendChild(link);
  });

  /* §5.7 — bandeau permanent tant que la simulation est active. */
  function banner() {
    if (document.getElementById("simBanner")) { return; }
    var bar = document.createElement("div");
    bar.id = "simBanner";
    bar.setAttribute("role", "status");
    bar.style.cssText =
      "position:sticky;top:0;z-index:150;background:#3a2f1a;color:#f0c674;" +
      "border-bottom:1px solid #d29922;padding:7px 16px;font:600 12.5px/1.4 " +
      "'Segoe UI',system-ui,sans-serif;text-align:center";
    bar.textContent =
      "Simulation : aucune commande n'est exécutée — les actions de pilotage renvoient la liste des commandes.";
    document.body.insertBefore(bar, document.body.firstChild);
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
      if (data && data.simulation === true) { banner(); }
    })
    .catch(function () { /* page sans API : pas de bandeau */ });

  fetch("/api/auth/state", { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (data) {
      if (data && data.bypass === true) { authBanner(data.client_ip); }
    })
    .catch(function () { /* page sans API : pas de bandeau */ });
})();
