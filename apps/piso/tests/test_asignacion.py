"""Asignación de pedidos en piso: dueño, ocultamiento y transferencia aceptada."""
from django.contrib.auth.models import User
from django.urls import reverse

from apps.core.models import PerfilUsuario
from apps.pedidos.models import Pedido
from apps.pedidos import services

from .base import PisoTestCase


class AsignacionTests(PisoTestCase):
    def setUp(self):
        self.login_piso()
        self.crear_stock(cantidad=50)
        self.pedido = self.crear_pedido(cantidad=2)
        self.otro = User.objects.create_user("piso2", password="x12345")
        PerfilUsuario.objects.create(usuario=self.otro, rol="piso")

    def _iniciar(self):
        services.iniciar_picking(self.pedido, self.operador)
        self.pedido.refresh_from_db()

    def test_iniciar_asigna_al_operador(self):
        self._iniciar()
        self.assertEqual(self.pedido.asignado_a, self.operador)

    def test_ajeno_lo_ve_sin_boton_y_no_lo_abre(self):
        self._iniciar()
        self.client.force_login(self.otro)
        # En la lista de picking se ve, con quién lo tiene y sin botón de escanear:
        lista = self.client.get(reverse("piso:picking"))
        self.assertContains(lista, self.pedido.folio)
        self.assertContains(lista, f"lo tiene <b>{self.operador.username}</b>")
        self.assertNotContains(lista, 'href="' + reverse("piso:picking_pedido", args=[self.pedido.pk]) + '"')
        # El detalle lo rechaza con el nombre del dueño:
        detalle = self.client.get(
            reverse("piso:picking_pedido", args=[self.pedido.pk]), follow=True,
        )
        self.assertContains(detalle, "lo tiene")
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)

    def test_transferencia_requiere_aceptacion_y_reinicia_el_avance(self):
        self._iniciar()
        from apps.pedidos.services import confirmar_linea_pick
        confirmar_linea_pick(self.pedido.lineas.get(), 1, self.operador)

        services.transferir_pedido(self.pedido, self.operador, self.otro)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)  # aún del dueño
        self.assertEqual(self.pedido.transferencia_a, self.otro)

        # El destinatario la ve en su home y acepta:
        self.client.force_login(self.otro)
        home = self.client.get(reverse("piso:home"))
        self.assertContains(home, "Te mandaron pedidos")
        self.assertContains(home, self.pedido.folio)
        self.client.post(reverse("piso:home"), {
            "accion": "aceptar_transferencia", "pedido_id": self.pedido.pk,
        })
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.otro)
        self.assertIsNone(self.pedido.transferencia_a)
        # Chema 2026-09-22: quien recibe re-escanea desde el carrito.
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 0)
        from apps.core.models import EventoAuditoria
        evento = EventoAuditoria.objects.get(
            entidad="pedido", entidad_id=str(self.pedido.pk), accion="picking_reiniciado",
        )
        self.assertEqual(evento.delta["avance_anterior"], [{"sku": self.sku.codigo, "pickeada": 1}])

    def test_transferencia_con_caja_ya_empacada_no_reinicia(self):
        from decimal import Decimal

        from apps.envios.models import Paquete, PaqueteLinea
        from apps.pedidos.services import confirmar_linea_pick, empacar_caja

        self._iniciar()
        linea = self.pedido.lineas.get()
        confirmar_linea_pick(linea, 2, self.operador)
        caja = Paquete.objects.create(
            pedido=self.pedido, numero=1, peso_kg=Decimal("2.1"), carrier="estafeta", servicio="ground",
        )
        c2 = Paquete.objects.create(
            pedido=self.pedido, numero=2, peso_kg=Decimal("2.1"), carrier="estafeta", servicio="ground",
        )
        PaqueteLinea.objects.create(paquete=caja, linea_pedido=linea, cantidad=1)
        PaqueteLinea.objects.create(paquete=c2, linea_pedido=linea, cantidad=1)
        empacar_caja(caja, self.operador, 2100, self.foto())  # la caja 1 ya está pesada y con foto
        services.transferir_pedido(self.pedido, self.operador, self.otro)
        services.aceptar_transferencia(self.pedido, self.otro)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 2)  # no se deshace lo empacado

    def test_solo_el_dueno_puede_enviar(self):
        self._iniciar()
        with self.assertRaises(ValueError):
            services.transferir_pedido(self.pedido, self.otro, self.operador)

    def test_rechazo_lo_regresa_limpio(self):
        self._iniciar()
        services.transferir_pedido(self.pedido, self.operador, self.otro)
        services.rechazar_transferencia(self.pedido, self.otro)
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)
        self.assertIsNone(self.pedido.transferencia_a)

    def test_pedido_libre_se_adopta_al_trabajarlo(self):
        # Compat con lo vivo: un EN_PICKING sin dueño (pre-deploy) lo adopta
        # el primer operador que le haga un POST.
        self._iniciar()
        Pedido.objects.filter(pk=self.pedido.pk).update(asignado_a=None)
        self.client.force_login(self.otro)
        self.client.post(
            reverse("piso:picking_pedido", args=[self.pedido.pk]),
            {"codigo": self.sku.codigo_barras or self.sku.codigo, "cantidad": 1},
        )
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.otro)

    def _soltado_con_avance(self, por):
        """Pedido en picking con 1 de 2 escaneada, soltado por `por`."""
        services.iniciar_picking(self.pedido, por)
        services.confirmar_linea_pick(self.pedido.lineas.get(), 1, por)
        services.soltar_pedido(self.pedido, por)
        self.pedido.refresh_from_db()
        url = reverse("piso:picking_pedido", args=[self.pedido.pk])
        return url, {"codigo": self.sku.codigo_barras, "cantidad": 1}

    def test_tomar_lo_que_solto_otro_reinicia_y_recarga(self):
        url, datos = self._soltado_con_avance(self.operador)
        self.client.force_login(self.otro)
        pagina = self.client.get(url)
        self.assertContains(pagina, "el avance se reinicia")
        self.assertContains(pagina, "piso1")
        respuesta = self.client.post(url, datos, HTTP_ACCEPT="application/json")
        self.assertTrue(respuesta.json()["reiniciado"])
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.otro)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 1)  # 0 + la pieza recién escaneada
        from apps.core.models import EventoAuditoria
        self.assertTrue(EventoAuditoria.objects.filter(
            entidad="pedido", entidad_id=str(self.pedido.pk), accion="picking_reiniciado",
        ).exists())

    def test_quien_lo_solto_lo_retoma_con_su_avance(self):
        url, datos = self._soltado_con_avance(self.operador)
        pagina = self.client.get(url)  # sigo logueado como piso1
        self.assertNotContains(pagina, "el avance se reinicia")
        respuesta = self.client.post(url, datos, HTTP_ACCEPT="application/json")
        self.assertFalse(respuesta.json()["reiniciado"])
        self.pedido.refresh_from_db()
        self.assertEqual(self.pedido.asignado_a, self.operador)
        self.assertEqual(self.pedido.lineas.get().cantidad_pickeada, 2)  # 1 + 1
