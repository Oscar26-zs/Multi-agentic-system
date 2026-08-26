# Cómo funciona el motor de orquestación (`graph/`)

## La analogía, continuando la del estudio

`agents/AGENTS.md` describe el sistema como un **estudio de arquitectura e ingeniería**: el Product Agent es el analista de requerimientos, el Architect Agent es el arquitecto, y así hasta el Reviewer Agent, el tech lead que da el veredicto final. Cada uno de esos seis agentes ya sabe hacer su trabajo — fueron construidos y probados de forma aislada en la Fase 6 de `Guia_Construccion.md`.

`graph/` es el **gerente de proyecto que coordina la fila de producción** de ese estudio. No hace el trabajo técnico de nadie — no analiza requerimientos, no diseña, no programa, no audita seguridad, no prueba, no da veredictos — pero decide en qué orden pasa el expediente de escritorio en escritorio, qué hacer cuando alguien lo devuelve con correcciones, y cuándo dejar de insistir y llamar a un humano.

Tres archivos, tres responsabilidades de ese gerente:

- **`graph/nodes.py`** = las casillas de entrada de cada escritorio. Cada agente ya sabe hacer su trabajo (Fase 6); el nodo solo se asegura de que, si algo sale mal en un escritorio puntual, quede clarísimo cuál fue — no un error genérico y anónimo en medio de una fila de seis personas.
- **`graph/edges.py`** = las reglas de tránsito del expediente. Quién lo recibe después de quién, y — la parte nueva de esta fase — qué hacer con un expediente rechazado: a quién se lo devuelven, cuántas vueltas como máximo antes de escalarlo a un humano en vez de seguir dando vueltas indefinidamente.
- **`graph/workflow.py`** = el plano completo de la oficina, ya ensamblado: la fila de producción de punta a punta, lista para recibir un requerimiento real y devolver un veredicto.

## `graph/nodes.py` — un nodo por agente, sin reimplementar nada

Cada uno de los 6 agentes ya tiene, desde que se construyó en la Fase 6, exactamente la firma que un nodo de LangGraph necesita: `state -> dict` de update parcial. Por eso `nodes.py` no reimplementa nada — envuelve:

```python
def _wrap(agent_fn, node_name):
    def _node(state):
        try:
            return agent_fn(state)
        except Exception as exc:
            raise NodeExecutionError(node_name, exc) from exc
    return _node
```

Es como ponerle una placa con el nombre a cada escritorio: si el arquitecto comete un error, el reporte de incidente dice "escritorio del arquitecto", no "alguien, en algún lugar de la oficina, algo salió mal". La excepción **no se traga ni se sigue de largo** — cada uno de los 6 agentes ya corta con `raise ValueError` cuando le falta una precondición (ej. `architect_agent` sin `specification`); dejar que el nodo la absorba y el grafo avanzara igual dejaría al siguiente escritorio trabajando sobre un expediente incompleto, exactamente lo que esa validación temprana quería evitar. `NodeExecutionError` guarda tanto el nombre del nodo como la excepción original (`.node_name`, `.original`), y se probó con dos casos reales: una precondición rota (dispara antes de tocar el LLM, sin gastar cuota) y un error real de LLM (durante la construcción, un `RateLimitError` de OpenRouter llegó envuelto como `[product_agent] Error code: 429 ...` — confirmando que el wrapper también atrapa fallos que no son de precondición).

Un detalle que no es casualidad: cada nodo se registra con el mismo nombre que la función del agente (`"product_agent"`, `"architect_agent"`, etc.). Eso es lo que permite que `edges.py` use `review["return_to"]` directamente como destino de la conditional edge, sin una tabla de traducción — el "nombre del escritorio" en el expediente y el "nombre del escritorio" en el mapa de la oficina son el mismo string.

## `graph/edges.py` — a dónde va el expediente, y hasta cuándo insiste

El Reviewer (Fase 6) ya decide `APPROVED`/`REJECTED` y, si rechaza, a qué agente devolver el trabajo (`return_to`) — pero deliberadamente **no** toca el contador de vueltas (`state["iteration"]`): esa cuenta es del gerente de proyecto, no del tech lead que revisa.

Acá aparece una restricción propia de LangGraph que vale la pena explicar con la analogía: la función que decide "a qué escritorio va ahora el expediente" (`route_after_review`) solo puede **mirar** el expediente y señalar una dirección — no puede escribir nada en él. Es como un cartel de señalización: indica el camino, pero no puede anotar nada en los papeles que pasan por debajo. Por eso, anotar "van 2 vueltas" es trabajo de un empleado real (un nodo, `advance_iteration`), parado justo antes del cartel:

```python
def advance_iteration(state):
    if state.get("review", {}).get("status") == "REJECTED":
        return {"iteration": state.get("iteration", 0) + 1}
    return {}
```

Con el contador ya actualizado, `route_after_review` decide:

- `APPROVED` → `END` (el expediente sale aprobado del estudio).
- `REJECTED` con `iteration` ya en `MAX_ITERATIONS = 3` → `escalate_to_human` (tres vueltas rechazado es la señal de que el estudio, por su cuenta, no está convergiendo — se llama a un humano en vez de seguir insistiendo).
- `REJECTED` con `return_to` válido → ese mismo nombre de nodo, reingresando la cadena lineal de siempre (el ciclo de revisión no necesita "edges de vuelta" separadas: como los nodos ya se llaman igual que los agentes, volver a `"architect_agent"` simplemente reengancha el camino normal desde ahí).
- Cualquier otro caso (defensivo — `_coerce_verdict` de `reviewer_agent.py` ya garantiza que esto no debería pasar) → también `escalate_to_human`, para que una futura ruptura de ese contrato falle de forma visible y no en silencio.

`escalate_to_human` es el escritorio final de esa rama: deja `human_review_required = True` (el campo que `graph/state.py` reserva exactamente para esto desde la Fase 1) y de ahí una edge fija va a `END`.

## `graph/workflow.py` — la oficina ensamblada

Arma el `StateGraph(EngineeringState)`, registra los 8 nodos (6 agentes + `advance_iteration` + `escalate_to_human`), cablea la cadena lineal `START → product_agent → ... → reviewer_agent → advance_iteration`, y de ahí la conditional edge de `edges.py`. Se compila una sola vez a nivel de módulo (`graph_app`), listo para que `app.py` (Fase 9) lo importe y lo invoque.

Un dato verificado durante la construcción, no asumido: la versión de `langgraph` instalada en este proyecto (1.2.11) trae un límite de recursión por defecto de 10007 pasos — muy por encima del peor caso real de este grafo (~22 pasos con `MAX_ITERATIONS=3` y el ciclo más largo posible, de vuelta a `product_agent`). Por eso `workflow.py` no fija `recursion_limit` a mano: sería infraestructura especulativa contra un riesgo que, con esta versión, no existe.

También se aprovechó `get_callback_handler()` (existe en `observability/langfuse_config.py` desde la Fase 2, sin usar hasta ahora): `default_invoke_config()` lo expone listo para pasar a `graph_app.invoke(state, config=...)`, y LangGraph lo propaga automáticamente a las llamadas LLM internas de los 6 agentes sin tocar `agents/` ni `nodes.py` — cada agente ya estaba trazado individualmente vía `@observe`; esto además correlaciona las seis llamadas bajo una misma corrida en Langfuse.

## Cómo se probó

Siguiendo el mismo patrón de aislamiento de toda la Fase 6 — cada pieza se valida sola antes de conectarla —, más el orden que pide explícitamente la Fase 7 (camino feliz primero, ciclo después):

1. `python graph/nodes.py`: confirma el camino de error sin gastar LLM (precondición rota en `architect_agent_node`) y el passthrough con una llamada real a `product_agent_node`.
2. `python graph/edges.py`: cuatro escenarios sintéticos, sin LLM ni API key — `APPROVED`→`END`, `REJECTED` dentro del límite→vuelve al agente indicado, `REJECTED` en `MAX_ITERATIONS`→escalada, `return_to` inválido (caso defensivo)→escalada.
3. `python graph/workflow.py`: primero se confirmó que el grafo compila y expone los 10 nodos esperados (incluyendo `__start__`/`__end__`) sin necesitar ningún LLM — la construcción del `StateGraph` y `.compile()` son puro cableado. La corrida real de punta a punta (un requerimiento real por los 6 agentes reales) quedó pendiente de ejecutar por el mismo límite diario gratuito de OpenRouter que ya había topado `testing_agent.py` y `reviewer_agent.py` durante la Fase 6 — se repite en cuanto resetea la cuota o se agregan créditos.

## Qué sigue

Con `graph/nodes.py`, `graph/edges.py` y `graph/workflow.py` construidos, la Fase 7 de `Guia_Construccion.md` queda cerrada: hay un grafo compilado (`graph_app`) que conecta los 6 agentes en el camino feliz y en el ciclo de revisión con corte por `MAX_ITERATIONS`. El siguiente paso es la Fase 8: `tests/` — `test_agents.py` (unitarios, reutilizando los smoke tests ya escritos), `test_graph.py` (routing condicional, forzar un `REJECTED` y confirmar que vuelve al agente correcto) y `test_scenarios.py` (los escenarios completos de punta a punta, incluyendo los diseñados para fallar).
