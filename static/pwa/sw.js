/* Service worker de Torre — shell offline mínimo + notificaciones Web Push.
   Se sirve desde /sw.js (view de core) para que su scope cubra todo el sitio;
   la view estampa la versión real en la constante CACHE (mtime de static/). */
"use strict";

var CACHE = "torre-shell-v1";
/* Solo estáticos del shell: NADA de páginas con CSRF/sesión (cachear la
   página de login congelaba su token y el login moría al volver la red).
   El fallback offline de navegación lo atiende el handler de fetch. */
var SHELL = [
  "/static/css/wop.css",
  "/static/js/escaner.js",
  "/static/js/push.js",
  "/static/fonts/questrial-v19-latin.woff2",
  "/static/pwa/icono-192.png",
  "/static/pwa/manifest.webmanifest"
];

var TIMEOUT_NAVEGACION_MS = 4000;

self.addEventListener("install", function (ev) {
  ev.waitUntil(
    caches.open(CACHE)
      .then(function (cache) { return cache.addAll(SHELL); })
      .catch(function () { /* sin shell cacheado también se vive */ })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (ev) {
  ev.waitUntil(
    caches.keys().then(function (claves) {
      return Promise.all(
        claves.filter(function (c) { return c !== CACHE; })
              .map(function (c) { return caches.delete(c); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

function respuestaOffline() {
  return new Response(
    "<!doctype html><html lang='es'><meta charset='utf-8'>" +
    "<meta name='viewport' content='width=device-width, initial-scale=1'>" +
    "<title>Sin conexión</title>" +
    "<body style='margin:0;display:grid;place-items:center;min-height:100vh;" +
    "background:#F7F6F3;color:#050505;font-family:system-ui,sans-serif'>" +
    "<div style='text-align:center;padding:24px'>" +
    "<h1 style='margin:0 0 8px'>Sin conexión</h1>" +
    "<p style='margin:0'>Sin conexión — reintenta cuando vuelva el WiFi.</p>" +
    "</div></body></html>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

function conTimeout(promesa, ms) {
  return new Promise(function (resolve, reject) {
    var t = setTimeout(function () { reject(new Error("timeout")); }, ms);
    promesa.then(
      function (r) { clearTimeout(t); resolve(r); },
      function (e) { clearTimeout(t); reject(e); }
    );
  });
}

/* Estrategia:
   - /static/: caché primero (inmutable entre versiones del SW), red de respaldo.
   - Navegaciones: red con timeout ~4 s → caché → aviso 503 (SOLO navigate).
   - Todo lo demás (AJAX, media): directo a la red, sin tocar. */
self.addEventListener("fetch", function (ev) {
  if (ev.request.method !== "GET") return;
  var url = new URL(ev.request.url);

  if (url.origin === self.location.origin && url.pathname.indexOf("/static/") === 0) {
    ev.respondWith(
      caches.match(ev.request).then(function (respuesta) {
        if (respuesta) return respuesta;
        return fetch(ev.request).then(function (red) {
          if (red && red.ok) {
            var copia = red.clone();
            caches.open(CACHE).then(function (cache) { cache.put(ev.request, copia); });
          }
          return red;
        });
      })
    );
    return;
  }

  if (ev.request.mode === "navigate") {
    ev.respondWith(
      conTimeout(fetch(ev.request), TIMEOUT_NAVEGACION_MS).catch(function () {
        return caches.match(ev.request).then(function (respuesta) {
          return respuesta || respuestaOffline();
        });
      })
    );
  }
});

/* Push del servidor → notificación del sistema (app cerrada incluida). */
self.addEventListener("push", function (ev) {
  var datos = {};
  try {
    datos = ev.data ? ev.data.json() : {};
  } catch (err) {
    datos = { title: "Torre", body: ev.data ? ev.data.text() : "" };
  }
  ev.waitUntil(
    self.registration.showNotification(datos.title || "Torre", {
      body: datos.body || "",
      icon: "/static/pwa/icono-192.png",
      badge: "/static/pwa/icono-192.png",
      data: { url: datos.url || "/" }
    })
  );
});

/* Tap en la notificación → enfocar una ventana que YA esté en esa URL, o
   abrir una nueva. JAMÁS navegar una ventana existente: el operador puede
   traer un empaque a medias y el aviso no debe tirárselo. */
self.addEventListener("notificationclick", function (ev) {
  ev.notification.close();
  var url = (ev.notification.data && ev.notification.data.url) || "/";
  ev.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true })
      .then(function (ventanas) {
        for (var i = 0; i < ventanas.length; i++) {
          var ventana = ventanas[i];
          if ("focus" in ventana && ventana.url && ventana.url.indexOf(url) !== -1) {
            return ventana.focus();
          }
        }
        return self.clients.openWindow(url);
      })
  );
});
