# El gate de Human-in-the-Loop (`graph/edges.py`)

Este documento explica, con la misma analogía del estudio de arquitectura e ingeniería que usan [`agents/AGENTS.md`](../agents/AGENTS.md) y [`tests/TESTS.md`](../tests/TESTS.md), la segunda pausa de intervención humana que tiene el sistema: la aprobación del plan **antes** de que alguien empiece a construir de verdad.

## La analogía: el permiso de obra antes de que el maestro de obra toque nada

En el estudio, hasta ahora el flujo era: el analista de producto toma el pedido, el arquitecto dibuja el plano — y en cuanto el plano está listo, el maestro de obra (**Developer Agent**) entra con la maza y empieza a construir. Nadie del lado del cliente lo vio antes.

Eso es exactamente lo que le faltaba al sistema: `developer_agent` es el **único** agente autorizado a tocar el repositorio real (vía MCP) — crea archivos, edita código, dentro de `_sandbox/Solicitud_de_Vacaciones`. Hasta ahora lo hacía sin que ningún humano confirmara nada antes. Es como dejar que el maestro de obra empiece a demoler paredes en cuanto el arquitecto termina el dibujo, sin que el dueño del edificio firme el permiso de obra.

Este gate agrega exactamente esa firma: **antes** de que el maestro de obra entre a construir, el socio del estudio (vos, desde la terminal) recibe el plano completo — qué stack, qué componentes, qué pasos va a seguir, qué riesgos técnicos ya se identificaron — y tiene que decir explícitamente "sí, adelante" o "no, pará acá".

## Dónde vive y cómo se activa

`graph/edges.py` — junto a `advance_iteration`/`escalate_to_human` (el otro punto de intervención humana del sistema, que actúa cuando se agotan los reintentos del ciclo de revisión). Este es un punto distinto: no reacciona a un fallo repetido, actúa **preventivamente**, siempre, antes de la primera escritura real sobre el repo.

Tres piezas nuevas:

| Pieza | Rol | Analogía |
|---|---|---|
| `request_plan_approval(state)` | Imprime el plano completo y hace `input()` — se queda esperando de verdad hasta que alguien tipee algo | El socio abriendo el plano sobre el escritorio y esperando con el bolígrafo en la mano |
| `route_after_plan_approval(state)` | Decide: `"developer_agent"` si se aprobó, `"cancelled_by_human"` si no | El portero que deja pasar al maestro de obra solo si ve la firma en el permiso |
| `cancelled_by_human(state)` | Nodo terminal: deja `review.status = "CANCELLED"` y `human_review_required = True` | El expediente que vuelve al archivo sin construirse — no es un rechazo técnico (nadie hizo nada mal), es una decisión del dueño |

## Por qué `CANCELLED` y no `REJECTED`

Un `REJECTED` del Reviewer Agent significa "algo está mal, y le toca corregirlo a un agente puntual" (`return_to`). Un `CANCELLED` en este gate significa otra cosa completamente distinta: **nadie hizo nada mal** — el plan puede ser perfecto — el usuario simplemente decidió no autorizar la ejecución en este momento. Mezclar ambos estados haría que un "no, todavía no" se viera igual que un "esto tiene un error", y el resto del sistema (o quien lea el resultado final) perdería esa distinción.

## Cómo se ve al correrlo (el progreso en vivo)

Antes, `app.py` invocaba el grafo entero y recién mostraba algo cuando terminaba todo — como si el estudio te devolviera el expediente completo al final del día, sin enterarte de nada mientras tanto. Ahora `app.py` (y el smoke test de `graph/workflow.py`) usan `grafo.stream(..., stream_mode="updates")` en vez de `.invoke()`: cada vez que un agente termina su parte, se imprime una línea al toque —

```
   >> [Product Agent] completado.
   >> [Architect Agent] completado.

======================================================================
HUMAN-IN-THE-LOOP: aprobación requerida antes de tocar el repo real
======================================================================
Resumen: ...
Plan de alto nivel (lo que developer_agent va a ejecutar):
  - Implementar entidad SolicitudVacaciones
  - Implementar endpoints de creación y aprobación

¿Autorizas a Developer Agent a ejecutar este plan sobre el repo real? [s/N]:
```

— y ahí el proceso se queda esperando de verdad, en pantalla, hasta que respondés. Es la misma idea que ver pasar el expediente de escritorio en escritorio en tiempo real, en vez de que te lo entreguen recién armado al final: podés ver exactamente en qué momento el sistema se detuvo a esperarte, no solo que "en algún punto" pidió permiso.

## Qué NO hace (todavía)

No usa el mecanismo nativo de pausa/reanudación de LangGraph (`interrupt()` + checkpointer), que permitiría cortar el proceso por completo y retomarlo más tarde, incluso en otra ejecución. Acá la pausa es un `input()` bloqueante dentro del mismo proceso — alcanza y sobra para una interfaz CLI de un solo proceso, que es todo lo que este proyecto necesita; agregar checkpointers sería infraestructura para un caso de uso (retomar una corrida horas después, desde otro proceso) que nadie pidió todavía.
