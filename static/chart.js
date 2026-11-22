/* Fossor — mini bibliothèque de graphiques (§10.7).
 *
 * SVG pur, aucune dépendance externe : les séries horodatées sont rendues
 * avec axes, grille et légende. Même esprit que le sparkline de Suivi,
 * en plus complet (plusieurs séries, échelle Y et dates).
 */
(function () {
  "use strict";

  var W = 820;

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function time(value) {
    var stamp = typeof value === "number" ? value : Date.parse(value);
    return isNaN(stamp) ? 0 : stamp;
  }

  function fmtValue(value, unit) {
    if (value == null || !isFinite(value)) { return "—"; }
    var abs = Math.abs(value);
    var text;
    if (abs >= 1e12) { text = (value / 1e12).toFixed(2) + " T"; }
    else if (abs >= 1e9) { text = (value / 1e9).toFixed(2) + " G"; }
    else if (abs >= 1e6) { text = (value / 1e6).toFixed(2) + " M"; }
    else if (abs >= 1e3) { text = (value / 1e3).toFixed(2) + " k"; }
    else if (abs >= 10) { text = value.toFixed(1); }
    else if (abs >= 1) { text = value.toFixed(2); }
    else { text = value.toPrecision(3); }
    return text + (unit ? " " + unit : "");
  }

  function fmtDate(stamp, withTime) {
    var date = new Date(stamp);
    if (isNaN(date.getTime())) { return ""; }
    var opts = withTime
      ? { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { day: "2-digit", month: "2-digit", year: "2-digit" };
    return date.toLocaleString("fr-FR", opts);
  }

  function niceMax(value) {
    if (!isFinite(value) || value <= 0) { return 1; }
    var step = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var scaled = value / step;
    var factor = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return factor * step;
  }

  function render(el, options) {
    options = options || {};
    var series = (options.series || []).filter(function (item) {
      return item && item.points && item.points.length;
    });
    if (!el) { return; }
    if (!series.length) {
      el.innerHTML = '<div class="empty">' + esc(options.empty || "Aucune donnée sur la période.") + "</div>";
      return;
    }
    var height = options.height || 170;
    var padL = 62, padR = 14, padT = 12, padB = 26;
    var xs = [], ys = [];
    series.forEach(function (item) {
      item.points.forEach(function (point) {
        var t = time(point[0]);
        var v = point[1];
        if (!t || v == null || !isFinite(v)) { return; }
        xs.push(t);
        ys.push(Number(v));
      });
    });
    if (!xs.length) {
      el.innerHTML = '<div class="empty">' + esc(options.empty || "Aucune donnée sur la période.") + "</div>";
      return;
    }
    var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
    if (maxX === minX) { maxX = minX + 3600000; }
    var maxY = Math.max.apply(null, ys);
    var minY = Math.min.apply(null, ys);
    if (minY > 0) { minY = 0; }
    var top = niceMax(maxY <= 0 ? 1 : maxY * 1.08);
    var bottom = minY < 0 ? -niceMax(Math.abs(minY) * 1.08) : 0;
    if (top === bottom) { top = bottom + 1; }

    function X(t) { return padL + ((t - minX) / (maxX - minX)) * (W - padL - padR); }
    function Y(v) { return height - padB - ((v - bottom) / (top - bottom)) * (height - padT - padB); }

    var grid = "";
    for (var i = 0; i <= 4; i++) {
      var value = bottom + ((top - bottom) * i) / 4;
      var y = Y(value);
      grid += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y.toFixed(1) + '" y2="' + y.toFixed(1) +
        '" stroke="#2c3544" stroke-width="1"></line>' +
        '<text x="' + (padL - 6) + '" y="' + (y + 3).toFixed(1) +
        '" text-anchor="end" fill="#8b98a9" font-size="9">' + esc(fmtValue(value, options.unit)) + "</text>";
    }
    var ticks = 4;
    for (var j = 0; j <= ticks; j++) {
      var t = minX + ((maxX - minX) * j) / ticks;
      grid += '<text x="' + X(t).toFixed(1) + '" y="' + (height - 8) +
        '" text-anchor="middle" fill="#8b98a9" font-size="9">' +
        esc(fmtDate(t, maxX - minX < 3 * 86400000)) + "</text>";
    }

    var paths = "";
    series.forEach(function (item) {
      var d = "";
      var started = false;
      item.points.forEach(function (point) {
        var v = point[1];
        var t = time(point[0]);
        if (v == null || !isFinite(v) || !t) { started = false; return; }
        d += (started ? "L" : "M") + X(t).toFixed(1) + " " + Y(Number(v)).toFixed(1) + " ";
        started = true;
      });
      paths += '<path d="' + d.trim() + '" fill="none" stroke="' + esc(item.color || "#4f8cff") +
        '" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"></path>';
    });

    var legend = series.map(function (item) {
      return '<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px">' +
        '<i style="width:10px;height:10px;border-radius:2px;display:inline-block;background:' +
        esc(item.color || "#4f8cff") + '"></i>' + esc(item.label || "") + "</span>";
    }).join("");

    el.innerHTML =
      '<svg viewBox="0 0 ' + W + " " + height + '" preserveAspectRatio="none" role="img" ' +
      'aria-label="' + esc(options.title || "graphique") + '" style="width:100%;height:' + height + 'px;display:block">' +
      grid + paths + "</svg>" +
      '<div class="legend">' + legend + "</div>";
  }

  window.FossorChart = { render: render };
})();
