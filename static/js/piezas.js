/* Modal "pieza por pieza" de Mesa (Chema 2026-10-22): decidir a dónde va cada
   pieza de una caja (regresa a bodega con posición y lote, cuarentena o no
   regresa). "Dividir" parte un renglón para decidir piezas iguales por
   separado; el destino muestra solo los campos que aplican; pegar el QR de
   la etiqueta (P|barras|lote) selecciona el lote. Sin JS el formulario sigue
   funcionando con un renglón por producto. */
(function () {
  function aplicarDestino(renglon) {
    var sel = renglon.querySelector('[data-destino]');
    if (!sel) return;
    var destino = sel.value;
    renglon.querySelectorAll('[data-solo]').forEach(function (el) {
      el.hidden = el.dataset.solo.split(' ').indexOf(destino) === -1;
    });
  }
  function renumerar(grupo) {
    grupo.querySelectorAll('.pieza-renglon').forEach(function (renglon, idx) {
      renglon.querySelectorAll('[name]').forEach(function (el) {
        el.name = el.name.replace(/^pieza-(\d+)-(\d+)-/, 'pieza-$1-' + idx + '-');
      });
    });
  }
  document.addEventListener('click', function (e) {
    var boton = e.target.closest('[data-dividir]');
    if (boton) {
      var renglon = boton.closest('.pieza-renglon');
      var grupo = renglon.closest('.pieza-grupo');
      var cantidad = renglon.querySelector('[name$="-cantidad"]');
      var n = parseInt(cantidad.value || '1', 10);
      var clon = renglon.cloneNode(true);
      var cantidadClon = clon.querySelector('[name$="-cantidad"]');
      if (n > 1) { cantidad.value = n - 1; cantidadClon.value = 1; } else { cantidadClon.value = 1; }
      renglon.after(clon);
      renumerar(grupo);
      aplicarDestino(clon);
      return;
    }
    var quitar = e.target.closest('[data-quitar]');
    if (quitar) {
      var fila = quitar.closest('.pieza-renglon');
      var contenedor = fila.closest('.pieza-grupo');
      if (contenedor.querySelectorAll('.pieza-renglon').length > 1) {
        fila.remove();
        renumerar(contenedor);
      }
    }
  });
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-destino]')) aplicarDestino(e.target.closest('.pieza-renglon'));
  });
  document.addEventListener('input', function (e) {
    if (!e.target.matches('[data-qr]')) return;
    var m = /^P[|\]]([^|\]]*)[|\]](.*)$/.exec(e.target.value.trim());
    if (!m) return;
    var sel = e.target.closest('.pieza-renglon').querySelector('[name$="-lote"]');
    var lote = m[2].trim();
    var opcion = Array.prototype.find.call(sel.options, function (o) { return o.value === lote; });
    if (opcion) { sel.value = opcion.value; e.target.value = ''; e.target.placeholder = 'lote ' + lote + ' seleccionado'; }
    else { e.target.setCustomValidity('Ese lote no existe para este producto'); e.target.reportValidity(); e.target.setCustomValidity(''); }
  });
  document.querySelectorAll('.pieza-renglon').forEach(aplicarDestino);
})();
