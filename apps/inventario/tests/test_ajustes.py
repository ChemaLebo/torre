"""Ajustes: doble firma obligatoria — dos PINs, dos personas, rol piso/mesa."""
from apps.core.models import PerfilUsuario
from apps.inventario import services
from apps.inventario.models import Ajuste, Movimiento, Saldo

from .base import InventarioTestCase


class AjusteDobleFirmaTests(InventarioTestCase):
    def setUp(self):
        super().setUp()
        self.piso1 = self.crear_firmante("piso1", PerfilUsuario.ROL_PISO, "1111")
        self.jefe = self.crear_firmante("jefe", PerfilUsuario.ROL_PISO, "2222")
        self.mesa1 = self.crear_firmante("mesa1", PerfilUsuario.ROL_MESA, "3333")
        self.poner_vendible(10)

    def test_ajuste_valido_con_dos_firmas(self):
        ajuste = services.aplicar_ajuste(
            self.sku, -2, Ajuste.MOTIVO_DANO, "piso1", "1111", "jefe", "2222",
        )
        self.assertEqual(ajuste.autorizo_1, "piso1")
        self.assertEqual(ajuste.autorizo_2, "jefe")
        self.assertTrue(ajuste.folio.startswith("AJU-"))
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 8)

        mov = Movimiento.objects.get(sku=self.sku, tipo=Movimiento.AJUSTE)
        self.assertEqual(mov.delta, -2)
        self.assertEqual(mov.referencia, ajuste.folio)

    def test_ajuste_positivo_suma_stock(self):
        services.aplicar_ajuste(
            self.sku, 4, Ajuste.MOTIVO_ERROR_RECEPCION, "piso1", "1111", "mesa1", "3333",
        )
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 14)

    def test_exige_dos_usuarios_distintos(self):
        """El mismo PIN no firma dos veces, aunque sea correcto."""
        with self.assertRaisesMessage(ValueError, "dos personas distintas"):
            services.aplicar_ajuste(
                self.sku, -1, Ajuste.MOTIVO_DANO, "piso1", "1111", "piso1", "1111",
            )
        self.assertEqual(Ajuste.objects.count(), 0)
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 10)  # nada se movió

    def test_pin_incorrecto_rechaza(self):
        with self.assertRaisesMessage(ValueError, "PIN incorrecto"):
            services.aplicar_ajuste(
                self.sku, -1, Ajuste.MOTIVO_DANO, "piso1", "9999", "jefe", "2222",
            )
        with self.assertRaisesMessage(ValueError, "PIN incorrecto"):
            services.aplicar_ajuste(
                self.sku, -1, Ajuste.MOTIVO_DANO, "piso1", "1111", "jefe", "0000",
            )
        self.assertEqual(Ajuste.objects.count(), 0)

    def test_rol_portal_no_puede_firmar(self):
        karina = self.crear_firmante("karina", PerfilUsuario.ROL_PORTAL, "4444")
        karina.perfil.cliente = self.cliente
        karina.perfil.save()
        with self.assertRaisesMessage(ValueError, "no puede firmar"):
            services.aplicar_ajuste(
                self.sku, -1, Ajuste.MOTIVO_DANO, "karina", "4444", "jefe", "2222",
            )

    def test_usuario_inexistente_o_sin_pin(self):
        with self.assertRaises(ValueError):
            services.aplicar_ajuste(
                self.sku, -1, Ajuste.MOTIVO_DANO, "fantasma", "1111", "jefe", "2222",
            )
        sin_pin = self.crear_firmante("sinpin", PerfilUsuario.ROL_PISO, "")
        with self.assertRaisesMessage(ValueError, "no tiene PIN"):
            services.aplicar_ajuste(
                self.sku, -1, Ajuste.MOTIVO_DANO, "sinpin", "", "jefe", "2222",
            )

    def test_motivo_fuera_de_catalogo(self):
        with self.assertRaisesMessage(ValueError, "fuera del catálogo"):
            services.aplicar_ajuste(
                self.sku, -1, "porque_si", "piso1", "1111", "jefe", "2222",
            )

    def test_no_deja_saldo_negativo(self):
        with self.assertRaisesMessage(ValueError, "saldo negativo"):
            services.aplicar_ajuste(
                self.sku, -20, Ajuste.MOTIVO_ROBO, "piso1", "1111", "jefe", "2222",
            )
        self.assertEqual(self.suma(Saldo.UBICADO_VENDIBLE), 10)

    def test_ajuste_cero_no_tiene_sentido(self):
        with self.assertRaises(ValueError):
            services.aplicar_ajuste(
                self.sku, 0, Ajuste.MOTIVO_CONTEO, "piso1", "1111", "jefe", "2222",
            )
