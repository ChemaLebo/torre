# Pick & pack por caja, sin olas — plan conceptual

**Estado:** idea acordada con Chema el 2026-10-20. **ALTA PRIORIDAD: pendiente número 1**
después del lote de etiquetas 51×51 y empaque por tanda. Sin go de construcción todavía.
Este documento es la idea, no un plan de implementación: no toca código ni nombra archivos.

## La idea en una frase

La caja es la unidad de trabajo del piso; el pedido es solo el resumen de sus cajas.

## Cómo funciona hoy (el modelo de olas)

Hoy el pedido tiene un solo carril de estado: pendiente, en picking, empacado, con guía,
recolectado. Todo lo que el piso hace de un pedido en una pasada es una "ola": se planean
cajas solo para lo que tiene stock, se pickea y se empaca todo eso, la guía de todas las
cajas se compra al pesar la última, y hasta que esa ola sale del corral no puede empezar
otra. Lo que no tenía stock espera sin caja; cuando llega stock, nace una segunda ola con
cajas nuevas, pero solo después de que la primera ya se fue. Mientras la primera ola está
en manos del piso, el stock que entra ni siquiera se reserva para ese pedido.

## Cómo funcionaría sin olas

1. **Llega el pedido y se planea completo.** Cada caja sabe qué lleva. Si a una caja le
   falta stock, queda marcada en espera. (La visibilidad de esa caja importa poco: una
   reposición nunca se va a planear de antemano.)
2. **El piso trabaja cajas, no pedidos.** Picking y empaque leen el plan: se pickea y se
   arma solo lo que existe, caja por caja. La guía de cada caja se compra al cerrarla, no al
   cerrar la última del pedido.
3. **Cualquier caja que llega a Salida ya cuenta.** En cuanto una caja sale, el pedido está
   parcialmente fulfilled. "Parcial" deja de ser un carril por el que pasa el pedido y pasa
   a ser un resumen: hay cajas fuera y cajas dentro.
4. **El stock que llega para una caja en espera la pone al frente de la cola.** Esa caja ya
   lleva tiempo corriendo contra el tiempo de fulfillment del pedido, así que se arma antes
   que cualquier pedido nuevo. Si las otras cajas del pedido siguen en bodega, viaja con
   ellas; si ya se fueron, sale sola.
5. **Una reposición es una caja nueva del mismo pedido**, con el mismo carril que cualquier
   caja. Nunca una línea nueva en el pedido, nunca planeada de antemano.
6. **Cancelación, retorno y reingreso son por caja.** Ya lo son en parte.
7. **El estado del pedido se deriva de sus cajas.** Mesa, portal, rastreo, Shopify,
   finanzas y reportes leen ese resumen. Detener un pedido sigue siendo por pedido.

## Qué cambia en el sistema, en palabras

- La cola de Mi turno entrega la siguiente caja trabajable, no el siguiente pedido entero.
- El picking cuenta por caja y línea, no por línea del pedido.
- El kardex se mueve por caja: a empaque al cerrar la caja, a salida al salir la caja.
  Hoy se mueve una sola vez al pesar la última caja del pedido, y ahí nacieron los
  descuadres del PED-00151 y del PED-00606.
- La guía se compra por caja al pesarla.
- Etiquetas, evidencia y confirmación en salida ya son por caja o por tanda con el lote de
  etiquetas 51; Salida y manifiesto ya son por caja con ese mismo lote.
- Shopify ya es por caja.
- El pedido pierde su carril: sus estados pasan a ser un resumen calculado, y
  "parcialmente despachado" desaparece como estado por el que se transita.

## Por qué vale la pena

- Un pedido con una caja en espera sale completo si el stock llega a tiempo, en vez de
  salir en dos entregas al comprador.
- La caja que esperaba stock no vuelve al final de la cola.
- Desaparecen los huecos de "unidades sin caja" y de kardex a nivel pedido.
- El piso deja de depender del estado del pedido para saber qué hacer: solo lee cajas.

## Riesgos y cuidados

- Es un refactor del corazón: estado del pedido, picking, kardex, guías, cola, Salida,
  Mesa, portal, rastreo, cancelaciones, finanzas e incidencias leen el carril actual.
- El kardex por caja es la pieza con más riesgo; conviene construirla primero y sola.
- Varios cientos de pruebas cambian. Se necesita una migración para pedidos vivos a mitad
  de camino el día del cambio.
- Estimación honesta: dos a tres semanas de trabajo con pruebas.

## Fases sugeridas

0. **Este lote (etiquetas 51 y empaque por tanda):** sellos por caja, Salida y manifiesto
   por caja sin importar el carril del pedido.
1. **Guía y kardex por caja** al cerrar cada caja.
2. **Cola y picking por caja**, con la caja en espera al frente cuando llega su stock.
3. **Pedido como resumen:** estado derivado, fuera el carril de parcial, fuera las olas.

## Decisiones de Chema ya tomadas (2026-10-20)

- La visibilidad de cajas en espera desde el plan no importa: una reposición nunca se
  planea de antemano.
- La caja que esperaba stock sale primero, para no alargar el fulfillment del pedido.
- El estatus es por caja; Salida saca la caja lista aunque el pedido esté replaneando o
  reponiendo otra.
