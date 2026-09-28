// Tablas de Torre (Chema 2026-09-28): toda tabla con encabezado se ordena al
// hacer clic en la columna y, con 8 renglones o más, trae un buscador que
// filtra sus renglones al escribir. Sin marcado extra: se aplica sola a cada
// pantalla. Se salta las tablas con renglones de detalle o totales (celdas con
// colspan en el cuerpo, como Salida) y las marcadas con data-sin-tabla-js.
(function () {
  "use strict";
  var MIN_BUSCADOR = 8;

  function normal(s) {
    return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  }

  function textoCelda(celda) {
    if (!celda) return "";
    return (celda.getAttribute("data-orden") || celda.textContent || "").replace(/\s+/g, " ").trim();
  }

  // Fecha ISO → número; "$1,234.50", "12 kg", "3 pzas" → número; lo demás texto.
  function valor(texto) {
    var fecha = texto.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/);
    if (fecha) {
      return { n: Date.UTC(+fecha[1], +fecha[2] - 1, +fecha[3], +(fecha[4] || 0), +(fecha[5] || 0)), s: "" };
    }
    var num = texto.replace(/[$,%\s]/g, "").replace(/(kg|g|pzas?|piezas?|h|d[ií]as?|cajas?)$/i, "");
    if (num !== "" && /^-?\d/.test(num) && !isNaN(num)) return { n: parseFloat(num), s: "" };
    return { n: null, s: normal(texto) };
  }

  function comparar(a, b) {
    if (a.n !== null && b.n !== null) return a.n - b.n;
    if (a.n !== null) return -1;
    if (b.n !== null) return 1;
    return a.s.localeCompare(b.s, "es");
  }

  function aplica(tabla) {
    if (!tabla.tHead || !tabla.tHead.rows.length || tabla.hasAttribute("data-sin-tabla-js")) return false;
    var cuerpos = tabla.tBodies;
    for (var i = 0; i < cuerpos.length; i++) {
      for (var j = 0; j < cuerpos[i].rows.length; j++) {
        var celdas = cuerpos[i].rows[j].cells;
        for (var k = 0; k < celdas.length; k++) {
          if (celdas[k].colSpan > 1) return false;
        }
      }
    }
    return true;
  }

  function ordenar(tabla, indice, asc) {
    Array.prototype.forEach.call(tabla.tBodies, function (cuerpo) {
      var claves = Array.prototype.map.call(cuerpo.rows, function (r, i) {
        return { r: r, i: i, v: valor(textoCelda(r.cells[indice])) };
      });
      claves.sort(function (a, b) {
        var c = comparar(a.v, b.v) || (a.i - b.i);
        return asc ? c : -c;
      });
      claves.forEach(function (k) { cuerpo.appendChild(k.r); });
    });
    Array.prototype.forEach.call(tabla.tHead.rows[0].cells, function (th, i) {
      if (i === indice) th.setAttribute("aria-sort", asc ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
  }

  function activarOrden(tabla) {
    Array.prototype.forEach.call(tabla.tHead.rows[0].cells, function (th, indice) {
      if (!th.textContent.trim()) return;
      th.classList.add("ordenable");
      th.title = "Ordenar por " + th.textContent.trim();
      th.addEventListener("click", function (ev) {
        if (ev.target.closest("a, button, input, select, label")) return;
        ordenar(tabla, indice, th.getAttribute("aria-sort") !== "ascending");
      });
    });
  }

  var contador = 0;

  // Misma pinta que el "Buscar" de los filtros de Mesa: etiqueta arriba e
  // input de la casa. Si la página ya trae un buscador del servidor (input
  // name="q"), no se duplica.
  function activarBuscador(tabla) {
    if (document.querySelector('form input[name="q"]')) return;
    var total = 0;
    Array.prototype.forEach.call(tabla.tBodies, function (c) { total += c.rows.length; });
    if (total < MIN_BUSCADOR) return;
    var id = "tabla-buscador-" + (++contador);
    var caja = document.createElement("div");
    caja.className = "tabla-buscador";
    var etiqueta = document.createElement("label");
    etiqueta.htmlFor = id;
    etiqueta.textContent = "Buscar";
    var input = document.createElement("input");
    input.type = "search";
    input.id = id;
    input.placeholder = "Lo que sea de esta tabla: folio, comprador, guía…";
    var cuenta = document.createElement("span");
    cuenta.className = "small muted";
    caja.appendChild(etiqueta);
    caja.appendChild(input);
    caja.appendChild(cuenta);
    tabla.parentNode.insertBefore(caja, tabla);
    input.addEventListener("input", function () {
      var q = normal(input.value.trim());
      var visibles = 0;
      Array.prototype.forEach.call(tabla.tBodies, function (c) {
        Array.prototype.forEach.call(c.rows, function (r) {
          var ok = !q || normal(r.textContent).indexOf(q) !== -1;
          r.hidden = !ok;
          if (ok) visibles++;
        });
      });
      cuenta.textContent = q ? visibles + " de " + total : "";
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("table"), function (tabla) {
      if (!aplica(tabla)) return;
      activarOrden(tabla);
      activarBuscador(tabla);
    });
  });
})();
