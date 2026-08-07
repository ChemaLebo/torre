/* Opt-in / opt-out de notificaciones Web Push (PWA de Torre).

   Muestra los botones .btn-push SOLO si el navegador soporta push. Sin
   suscripción: "Activar" → pide permiso, se suscribe con la llave VAPID
   pública (data-vapid) y guarda la suscripción en POST /push/suscribir/
   verificando la respuesta REAL del servidor (redirect al login o status
   raro jamás se celebra como éxito). Ya suscrito: el mismo botón se vuelve
   "Desactivar" → subscription.unsubscribe() + POST /push/baja/. Todo con
   guardas: en un origen sin contexto seguro el botón no aparece. */
(function () {
  "use strict";

  function base64UrlAUint8Array(base64url) {
    var relleno = "=".repeat((4 - (base64url.length % 4)) % 4);
    var b64 = (base64url + relleno).replace(/-/g, "+").replace(/_/g, "/");
    var crudo = atob(b64);
    var bytes = new Uint8Array(crudo.length);
    for (var i = 0; i < crudo.length; i++) bytes[i] = crudo.charCodeAt(i);
    return bytes;
  }

  function leerCookie(nombre) {
    var partes = document.cookie ? document.cookie.split("; ") : [];
    for (var i = 0; i < partes.length; i++) {
      var par = partes[i].split("=");
      if (par[0] === nombre) return decodeURIComponent(par.slice(1).join("="));
    }
    return "";
  }

  function soportado() {
    return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  }

  function zonaDe(boton) {
    return document.getElementById(boton.getAttribute("data-zona") || "") || boton;
  }

  function llamarServidor(url, cuerpo) {
    /* POST verificado: el fetch sigue redirects (sesión expirada → página de
       login con 200) — eso NO es éxito. Solo un JSON {ok:true} directo lo es. */
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRFToken": leerCookie("csrftoken"),
      },
      credentials: "same-origin",
      body: JSON.stringify(cuerpo),
    }).then(function (respuesta) {
      if (respuesta.redirected) throw new Error("sesión expirada");
      if (!respuesta.ok) throw new Error("el servidor respondió " + respuesta.status);
      return respuesta.json().then(function (datos) {
        if (!datos || datos.ok !== true) {
          throw new Error((datos && datos.error) || "el servidor no confirmó");
        }
        return datos;
      });
    });
  }

  function suscribir(boton) {
    return Notification.requestPermission().then(function (permiso) {
      if (permiso !== "granted") throw new Error("permiso no concedido");
      return navigator.serviceWorker.ready;
    }).then(function (reg) {
      return reg.pushManager.getSubscription().then(function (sub) {
        if (sub) return sub;
        return reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: base64UrlAUint8Array(boton.getAttribute("data-vapid") || ""),
        });
      });
    }).then(function (sub) {
      return llamarServidor("/push/suscribir/", sub.toJSON());
    });
  }

  function darDeBaja() {
    return navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription();
    }).then(function (sub) {
      if (!sub) return llamarServidor("/push/baja/", {});
      var endpoint = sub.endpoint;
      return sub.unsubscribe().then(function () {
        return llamarServidor("/push/baja/", { endpoint: endpoint });
      });
    });
  }

  function modoActivar(boton) {
    boton.textContent = "🔔 Activar notificaciones";
    boton.dataset.modo = "activar";
  }

  function modoDesactivar(boton) {
    boton.textContent = "🔕 Desactivar notificaciones";
    boton.dataset.modo = "desactivar";
  }

  function activarBoton(boton, suscrito) {
    var zona = zonaDe(boton);
    zona.hidden = false;
    if (suscrito) modoDesactivar(boton); else modoActivar(boton);
    boton.addEventListener("click", function () {
      boton.disabled = true;
      var accion = boton.dataset.modo === "desactivar" ? darDeBaja() : suscribir(boton);
      var eraBaja = boton.dataset.modo === "desactivar";
      accion.then(function () {
        boton.disabled = false;
        if (eraBaja) {
          modoActivar(boton);
        } else {
          boton.textContent = "🔔 Notificaciones activadas";
          setTimeout(function () { modoDesactivar(boton); boton.disabled = false; }, 1800);
        }
      }).catch(function () {
        boton.disabled = false;
        boton.textContent = "🔔 No se pudo — reintenta";
      });
    });
  }

  function iniciar() {
    var botones = Array.prototype.slice.call(document.querySelectorAll(".btn-push"));
    if (!botones.length || !soportado()) return;
    // .ready solo resuelve con un SW activo (origen seguro o con el flag de
    // Chrome): sin eso, los botones se quedan escondidos a propósito.
    navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.getSubscription();
    }).then(function (sub) {
      var suscrito = !!sub && Notification.permission === "granted";
      botones.forEach(function (boton) { activarBoton(boton, suscrito); });
    }).catch(function () { /* sin push no hay botón, y no pasa nada */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();
