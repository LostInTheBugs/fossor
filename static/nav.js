/* Navigation unique de l'application (défaut 2f).
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
    ["/mined", "🪙 Tokens minés"],
    ["/settings", "⚙️ Configuration"],
    ["/audit", "📋 Audit Log"]
  ];
  var mount = document.getElementById("nav");
  if (!mount) {
    return;
  }
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
})();
