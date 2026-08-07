/* ══════════════════════════════════════════════════════════════════
   TORRE · Escáner de cámara del piso (sin librerías) + Feedback global.

   Escaner.montar(contenedor, alLeer) — abre la cámara trasera y corre
   BarcodeDetector nativa en un loop (~200 ms); llama alLeer(valor) con
   re-armado por salida de cuadro: la MISMA lectura solo se re-acepta
   cuando el código salió del visor (~3 ciclos sin verlo) — dejar el
   producto bajo la cámara NO confirma piezas solas. Un valor DISTINTO
   respeta una ventana de 2 s (dos códigos visibles a la vez no
   double-firean). Regresa true si el visor quedó vivo; false si el
   navegador no trae lector (el contenedor lo avisa y la UI cae al input
   manual, que SIEMPRE está visible como fallback).
   Escaner.detener() — apaga cámara y loop (llamar al salir de la vista).

   Feedback.ok() / Feedback.error(msg) — beep + vibración + banda de
   color. Al cargar cualquier página con .flash: scroll al flash + beep
   + banda visual según su clase (quick win global de toda la app).
   ══════════════════════════════════════════════════════════════════ */

window.Escaner = (function () {
  "use strict";
  var FORMATOS = ["ean_13", "ean_8", "code_128", "upc_a", "qr_code"];
  var CICLOS_REARME = 3;    // ciclos consecutivos SIN ver el código para re-armarlo
  var MAX_FALLOS_DETECT = 30; // detect() reventando seguido = sin backend real
  var video = null, stream = null, timer = null, activo = false;
  var ultimo = "", ultimoTs = 0, ciclosSinUltimo = 0, rearmado = true;
  var fallosDetect = 0, contenedorActivo = null, huboVisor = false;

  function soporta() {
    return (
      "BarcodeDetector" in window &&
      navigator.mediaDevices && navigator.mediaDevices.getUserMedia
    );
  }

  function aviso(contenedor, texto) {
    contenedor.classList.remove("escaner-vivo");
    contenedor.innerHTML =
      '<div class="escaner-sin-lector">' + texto + "</div>";
  }

  function montar(contenedor, alLeer) {
    detener();
    if (typeof contenedor === "string") {
      contenedor = document.querySelector(contenedor);
    }
    if (!contenedor) return false;
    if (!soporta()) {
      // Distinguir la causa: en http:// (origen inseguro) el navegador ESCONDE
      // el lector y la cámara — el fix es el flag de Chrome, no otro teléfono.
      if (!window.isSecureContext) {
        aviso(contenedor,
          "El lector necesita el modo seguro de Chrome. Activa el flag " +
          "«insecure-origin-as-secure» con esta dirección (guía de instalación, " +
          "paso 1) y reinicia Chrome. Mientras, teclea el código.");
      } else {
        aviso(contenedor, "Este navegador no trae lector de códigos — teclea el código (en el celular Android sí hay cámara).");
      }
      return false;
    }

    activo = true;
    huboVisor = true;
    contenedorActivo = contenedor;
    video = document.createElement("video");
    video.setAttribute("playsinline", "");
    video.muted = true;
    video.autoplay = true;
    contenedor.innerHTML = "";
    contenedor.appendChild(video);
    contenedor.classList.add("escaner-vivo");

    var detector;
    try {
      detector = new BarcodeDetector({ formats: FORMATOS });
    } catch (err) {
      aviso(contenedor, "Tu navegador no trae lector — teclea el código");
      activo = false;
      return false;
    }

    // BarcodeDetector puede existir SIN backend real (detecta cero formatos):
    // exigir intersección de formatos soportados antes de confiar en él.
    try {
      if (window.BarcodeDetector.getSupportedFormats) {
        window.BarcodeDetector.getSupportedFormats().then(function (fmts) {
          var alguno = FORMATOS.some(function (f) { return fmts.indexOf(f) !== -1; });
          if (!alguno && activo) {
            aviso(contenedor, "Tu navegador no trae lector — teclea el código");
            detener();
          }
        }).catch(function () {});
      }
    } catch (err) {}

    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then(function (s) {
        if (!activo) {
          s.getTracks().forEach(function (t) { t.stop(); });
          return;
        }
        stream = s;
        video.srcObject = s;
        var p = video.play();
        if (p && p.catch) p.catch(function () {});
        ciclo(detector, alLeer);
      })
      .catch(function () {
        if (activo) aviso(contenedor, "Sin permiso de cámara — teclea el código");
      });
    return true;
  }

  function procesa(codigos, alLeer) {
    var valores = [];
    for (var i = 0; i < codigos.length; i++) {
      var v = String(codigos[i].rawValue || "").trim();
      if (v) valores.push(v);
    }
    // Re-armado: solo cuando el último código aceptado SALIÓ del cuadro.
    if (ultimo && valores.indexOf(ultimo) === -1) {
      ciclosSinUltimo++;
      if (ciclosSinUltimo >= CICLOS_REARME) rearmado = true;
    } else if (ultimo) {
      ciclosSinUltimo = 0;
    }
    if (!valores.length) return;
    var valor = valores[0];
    var ahora = Date.now();
    var acepta;
    if (valor === ultimo) {
      // Mismo código: SOLO tras salir del cuadro (jamás por puro tiempo —
      // dejar el producto bajo la cámara no confirma piezas solas).
      acepta = rearmado;
    } else {
      // Código distinto: ventana de 2 s (dos códigos visibles alternando
      // en el cuadro no deben double-firear).
      acepta = ahora - ultimoTs > 2000;
    }
    if (acepta) {
      ultimo = valor;
      ultimoTs = ahora;
      rearmado = false;
      ciclosSinUltimo = 0;
      try { alLeer(valor); } catch (err) {}
    }
  }

  function ciclo(detector, alLeer) {
    if (!activo) return;
    timer = setTimeout(function () {
      requestAnimationFrame(function () {
        if (!activo) return;
        if (!video || video.readyState < 2) { ciclo(detector, alLeer); return; }
        detector.detect(video).then(function (codigos) {
          fallosDetect = 0;
          if (activo) procesa(codigos || [], alLeer);
          ciclo(detector, alLeer);
        }).catch(function () {
          fallosDetect++;
          if (fallosDetect >= MAX_FALLOS_DETECT) {
            // El lector truena en cada frame: no hay backend real detrás.
            if (contenedorActivo) {
              aviso(contenedorActivo, "El lector no responde — teclea el código");
            }
            detener();
            return;
          }
          ciclo(detector, alLeer);
        });
      });
    }, 200);
  }

  function detener() {
    activo = false;
    if (timer) { clearTimeout(timer); timer = null; }
    if (stream) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    video = null;
    contenedorActivo = null;
    ultimo = "";
    ultimoTs = 0;
    ciclosSinUltimo = 0;
    rearmado = true;
    fallosDetect = 0;
  }

  window.addEventListener("pagehide", detener);
  // bfcache: al regresar con el botón atrás la página revive congelada (el
  // stream de cámara ya murió) — recargar la deja operable.
  window.addEventListener("pageshow", function (ev) {
    if (ev.persisted && huboVisor) window.location.reload();
  });
  return { montar: montar, detener: detener, soporta: soporta };
})();


window.Feedback = (function () {
  "use strict";
  var ctx = null;

  function audio() {
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === "suspended") ctx.resume();
      return ctx;
    } catch (err) { return null; }
  }

  // Autoplay policy: el AudioContext nace suspendido hasta un gesto del
  // usuario — el primer touch/click lo desbloquea para que el beep del
  // primer escaneo sí suene.
  function desbloquear() { audio(); }
  document.addEventListener("touchstart", desbloquear, { once: true, passive: true });
  document.addEventListener("click", desbloquear, { once: true });

  function tono(freq, dur, espera, tipo) {
    var c = audio();
    if (!c) return;
    try {
      var osc = c.createOscillator(), gain = c.createGain();
      osc.type = tipo || "sine";
      osc.frequency.value = freq;
      gain.gain.value = 0.15;
      osc.connect(gain);
      gain.connect(c.destination);
      var t = c.currentTime + (espera || 0);
      osc.start(t);
      osc.stop(t + dur);
    } catch (err) {}
  }

  function banda(clase, ms, texto) {
    var el = document.createElement("div");
    el.className = "feedback-banda " + clase;
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    if (texto) el.textContent = texto;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, ms);
  }

  function beepOk() {
    tono(1175, 0.12);
    if (navigator.vibrate) navigator.vibrate(60);
  }

  function beepError() {
    // Doble buzz grave: inconfundible con el beep de ok.
    tono(196, 0.16, 0, "square");
    tono(147, 0.2, 0.18, "square");
    if (navigator.vibrate) navigator.vibrate([80, 60, 80]);
  }

  function ok() { beepOk(); banda("ok", 450); }

  function error(mensaje) {
    beepError();
    // Un error CON texto necesita tiempo de lectura (3.5 s); el flash verde
    // de 450 ms es solo para el ok.
    banda("error", mensaje ? 3500 : 1200, mensaje || "");
  }

  // Quick win global: toda página con .flash hace scroll al flash, suena y
  // pinta banda visual según su clase (el beep puede venir bloqueado por la
  // autoplay policy — la banda se ve siempre).
  document.addEventListener("DOMContentLoaded", function () {
    var flash = document.querySelector(".flash");
    if (!flash) return;
    try { flash.scrollIntoView({ block: "nearest" }); } catch (err) {}
    if (flash.classList.contains("error")) { beepError(); banda("error", 1200); }
    else { beepOk(); banda("ok", 450); }
  });

  return { ok: ok, error: error };
})();


/* ── Guarda global anti doble-submit ─────────────────────────────────
   Un doble tap en EMPEZAR ▶ o en el manifiesto dispara DOS POSTs (dos
   pedidos reclamados, doble firma). Al enviar cualquier form se apagan
   sus botones de submit; pageshow (incluye regreso por bfcache) los
   re-enciende. Los forms manejados por fetch (preventDefault) no pasan
   por aquí. ── */
(function () {
  "use strict";
  document.addEventListener("submit", function (ev) {
    if (ev.defaultPrevented) return;
    var form = ev.target;
    if (!form || !form.querySelectorAll) return;
    var botones = form.querySelectorAll(
      'button[type="submit"], button:not([type]), input[type="submit"]'
    );
    // Apagar DESPUÉS de que el submit serialice (name/value del botón).
    setTimeout(function () {
      for (var i = 0; i < botones.length; i++) {
        botones[i].disabled = true;
        botones[i].setAttribute("data-doble-submit", "1");
      }
    }, 0);
  });
  window.addEventListener("pageshow", function () {
    var marcados = document.querySelectorAll("[data-doble-submit]");
    for (var i = 0; i < marcados.length; i++) {
      marcados[i].disabled = false;
      marcados[i].removeAttribute("data-doble-submit");
    }
  });
})();
