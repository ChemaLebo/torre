// Campo predictivo de pedido (Chema 2026-09-28): un input de texto que filtra
// una lista de pedidos (folio, # de Shopify, comprador, cliente) al escribir y
// fija el elegido en un input oculto. Marcado esperado:
//   <input type="hidden" name="pedido" id="X">
//   <input type="search" data-buscador-pedido="X" data-opciones="ID_DEL_JSON">
//   <script type="application/json" id="ID_DEL_JSON">[{"id": 1, "texto": "PED-… · #… · …"}]</script>
(function () {
  "use strict";
  var MAX = 12;

  function normal(s) {
    return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  }

  function armar(input) {
    var oculto = document.getElementById(input.getAttribute("data-buscador-pedido"));
    var fuente = document.getElementById(input.getAttribute("data-opciones"));
    if (!oculto || !fuente) return;
    var opciones = JSON.parse(fuente.textContent || "[]");
    var lista = document.createElement("div");
    lista.className = "sugerencias";
    lista.hidden = true;
    input.insertAdjacentElement("afterend", lista);
    var activa = -1;

    function elegir(op) {
      oculto.value = op.id;
      input.value = op.texto;
      cerrar();
    }
    function cerrar() { lista.hidden = true; lista.innerHTML = ""; activa = -1; }
    function pintar(items) {
      lista.innerHTML = "";
      items.forEach(function (op) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "sugerencia";
        b.textContent = op.texto;
        b.addEventListener("mousedown", function (ev) { ev.preventDefault(); elegir(op); });
        lista.appendChild(b);
      });
      lista.hidden = items.length === 0;
      activa = -1;
    }
    function filtrar() {
      var q = normal(input.value.trim());
      if (oculto.value && input.value !== (opciones.filter(function (o) { return String(o.id) === oculto.value; })[0] || {}).texto) {
        oculto.value = "";  // se editó el texto: lo elegido ya no vale
      }
      if (!q) { cerrar(); return; }
      var partes = q.split(/\s+/);
      pintar(opciones.filter(function (op) {
        var t = normal(op.texto);
        return partes.every(function (p) { return t.indexOf(p) !== -1; });
      }).slice(0, MAX));
    }
    function mover(delta) {
      var botones = lista.querySelectorAll(".sugerencia");
      if (!botones.length) return;
      activa = (activa + delta + botones.length) % botones.length;
      botones.forEach(function (b, i) { b.classList.toggle("activa", i === activa); });
      botones[activa].scrollIntoView({ block: "nearest" });
    }

    input.addEventListener("input", filtrar);
    input.addEventListener("focus", function () { if (!oculto.value) filtrar(); });
    input.addEventListener("blur", function () { setTimeout(cerrar, 150); });
    input.addEventListener("keydown", function (ev) {
      if (lista.hidden) return;
      if (ev.key === "ArrowDown") { ev.preventDefault(); mover(1); }
      else if (ev.key === "ArrowUp") { ev.preventDefault(); mover(-1); }
      else if (ev.key === "Enter") {
        var botones = lista.querySelectorAll(".sugerencia");
        var b = botones[activa >= 0 ? activa : 0];
        if (b) { ev.preventDefault(); b.dispatchEvent(new MouseEvent("mousedown")); }
      } else if (ev.key === "Escape") { cerrar(); }
    });
    // Sin elegir nada, el formulario no se manda: evita el "no encuentro el pedido".
    input.form && input.form.addEventListener("submit", function (ev) {
      if (!oculto.value) {
        ev.preventDefault();
        input.focus();
        filtrar();
        input.setCustomValidity("Elige el pedido de la lista.");
        input.reportValidity();
        input.setCustomValidity("");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-buscador-pedido]"), armar);
  });
})();
