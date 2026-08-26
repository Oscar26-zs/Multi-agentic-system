# Cómo funcionan las pruebas del sistema (`tests/`)

Este documento explica, con la misma analogía del estudio de arquitectura e ingeniería que usa [`agents/AGENTS.md`](../agents/AGENTS.md), cómo está armada la Fase 8 de `Guia_Construccion.md`: tres archivos, tres formas distintas de probar el mismo estudio.

## La analogía general: tres formas de evaluar un estudio

Imagina que quieres confirmar que el estudio de arquitectura e ingeniería (nuestro sistema de 6 agentes) funciona bien. Hay tres maneras de hacerlo, de más barata/rápida a más cara/lenta:

1. **Evaluar a cada empleado por separado**, dándole un caso de prueba escrito a mano y comprobando que su razonamiento y su papeleo interno son correctos — sin depender de que un cliente real llame ese día. Esto es `test_agents.py`.
2. **Hacer un simulacro de oficina**: poner seis actores que solo dicen "listo, siguiente" en el orden correcto, para confirmar que el circuito de escritorios (quién le pasa el expediente a quién, qué pasa si alguien lo rechaza y lo devuelve) está bien armado — sin que ningún empleado real tenga que pensar. Esto es `test_graph.py`.
3. **Atender un cliente real de principio a fin**, con todos los empleados reales trabajando el caso completo. Es la única prueba que te dice "el estudio de verdad funciona", pero le paga sueldo a todo el mundo por horas reales. Esto es `test_scenarios.py`.

Ninguna reemplaza a las otras dos: la (1) es rápida pero no prueba que los escritorios estén bien conectados; la (2) prueba los escritorios pero no si los empleados razonan bien; la (3) es la única prueba real, pero es la que menos querés correr todos los días.

---

## 1. `test_agents.py` — evaluar a cada empleado con un caso de prueba, no con un cliente real

Cuando una escuela de arquitectura evalúa a un estudiante, no espera a que llegue un cliente real — le da un caso de práctica y compara lo que el estudiante entrega contra lo esperado. Eso es exactamente lo que hace este archivo: en vez de dejar que cada agente llame de verdad a un LLM (lento, no determinista, y le cuesta dinero real a alguien), se le entrega una respuesta ya escrita ("esto es lo que el LLM habría dicho") y se verifica que el agente la procese bien.

### El mecanismo: un "actor de reparto" en el lugar del LLM

```python
monkeypatch.setattr(security_mod, "invoke_structured", _fake_invoke_structured(fake))
```

`invoke_structured` (`agents/llm_factory.py`) es el único punto por el que CADA agente habla con un LLM real. Reemplazarlo ahí es como poner un actor de reparto en el rol del "cliente" durante un ensayo: el empleado (el agente) hace exactamente el mismo trabajo que haría con un cliente real, pero el "cliente" ya trae el guion memorizado — no hay verdadera conversación, así que el resultado es instantáneo y siempre igual.

### Qué se prueba realmente (no la creatividad del LLM — el criterio del código)

La parte valiosa de estos tests **no** es verificar qué dice el LLM (eso ya no está bajo prueba, es un actor con guion) — es verificar que el **empleado no confía ciegamente en lo que el "cliente" le dice**, cuando el código ya decidió no hacerlo. Por ejemplo:

```python
def test_security_agent_recalcula_aprobado_pese_a_que_el_llm_diga_true(monkeypatch):
    fake = SecurityReview(..., aprobado=True)  # el actor dice "todo bien"
    ...
    resultado = security_agent(state)
    assert resultado["security_review"]["aprobado"] is False  # el empleado igual lo corrige
```

Es como confirmar que el auditor de seguridad, aunque el asistente que le pasa el borrador del informe escriba "aprobado: sí" en la portada, igual cuenta él mismo los hallazgos críticos de la lista antes de firmar — nunca confía en el resumen de otro para algo tan importante. Esa es la garantía real que vale la pena probar, y es exactamente lo mismo que hace `_coerce_verdict()` en el Reviewer Agent, el punto de mayor riesgo de todo el sistema: ahí se prueba directamente que un veredicto `APPROVED` del "cliente actor" se convierte en `REJECTED` si `security_review['aprobado']` o `test_results['aprobado']` quedaron en `False`, sin importar qué haya dicho el LLM.

### Los dos empleados que actúan sobre archivos reales (Developer, Testing)

`developer_agent` y `testing_agent` no solo hablan con un LLM — también conversan con el servidor MCP y tocan un repositorio real. Simular esa conversación completa (protocolo MCP + subprocess + `dotnet test`) con actores de reparto sería un ensayo tan elaborado que casi valdría más hacerlo de verdad. Por eso acá el archivo no los evalúa de punta a punta: prueba **las partes que sí son deterministas y aisladas** — que se nieguen a trabajar sin los planos (`architecture`/`implementation` vacíos), y las funciones de `agents/mcp_tools.py` que calculan qué archivo se tocó y su diff (`track_file_change`, `unified_diff`) sin necesitar ni LLM ni MCP corriendo. La evaluación de punta a punta de estos dos empleados queda para el punto 3.

---

## 2. `test_graph.py` — el simulacro de oficina, sin empleados pensando de verdad

Ahora la pregunta no es "¿el arquitecto razona bien?" sino "¿el expediente circula por los escritorios correctos?". Para responderla no hace falta que el arquitecto piense de verdad — alcanza con seis actores que reciben una carpeta y devuelven una carpeta con una etiqueta puesta, instantáneamente.

```python
def _fake_node(field, value, etiqueta):
    def _node(state):
        return {field: value, "messages": [etiqueta]}
    return _node
```

Cada "actor" simplemente pone su sello (`{"architecture": {...}}`) y firma la bitácora. El único escritorio con guion variable es el del Revisor (Reviewer), porque es el que decide si el expediente sigue circulando o se cierra — así que a ese actor se le da un libreto de varias líneas (`_make_reviewer_fake`), una por cada vez que le toque hablar:

- **Ensayo 1 — camino feliz:** el Revisor dice "aprobado" la primera vez que le llega el expediente. Se verifica que las seis estaciones dejaron su sello y que el expediente termina ahí, sin vueltas.
- **Ensayo 2 — una vuelta de corrección:** el Revisor rechaza la primera vez ("devuélvanselo al arquitecto") y aprueba la segunda. Se verifica que el expediente de verdad vuelve al escritorio del arquitecto (no a cualquier otro) y que el contador de vueltas (`iteration`) subió exactamente una vez.
- **Ensayo 3 — se agotan los intentos:** el Revisor rechaza siempre. Se verifica que, después de exactamente `MAX_ITERATIONS` vueltas, alguien de más arriba (`escalate_to_human`) se hace cargo en vez de que el expediente circule para siempre entre los mismos dos escritorios.

Es un simulacro de incendio para la oficina: no importa si el "arquitecto" de ese día sabe diseñar de verdad, lo que se está poniendo a prueba es si las puertas y los pasillos (las conexiones entre nodos del grafo) llevan a donde tienen que llevar.

---

## 3. `test_scenarios.py` — el cliente real, de punta a punta

Acá sí se abre la oficina de verdad: seis empleados reales, hablando con un LLM real, consultando la biblioteca real (RAG) y tocando el repositorio real a través del servidor MCP real — igual que usaría el sistema cualquier persona en producción. Es la única prueba que certifica "el estudio, como conjunto, entrega algo razonable" — nada de lo anterior lo garantiza, porque ambas pruebas anteriores reemplazaron a alguien por un actor con guion.

### Los dos casos diseñados para fallar

De los cinco escenarios, dos no son requerimientos "normales" — son trampas deliberadas, como cuando una escuela de arquitectura le da a un estudiante un plano con un defecto estructural a propósito, para confirmar que lo detecta en vez de aprobarlo porque "se ve bien a simple vista":

- Un requerimiento que **pide explícitamente** una falla de control de acceso (que el empleado pueda aprobar su propia solicitud bajo ciertas condiciones) — el equivalente a un cliente pidiendo "quiero la puerta de emergencia sin alarma, así es más rápido salir". El test no exige que el estudio rechace el pedido de plano (`REJECTED`) — exige que **alguien en el estudio lo haya anotado en algún lado** (un hallazgo de seguridad, un riesgo documentado en la especificación), porque dejarlo pasar en completo silencio sería la falla real.
- Un requerimiento que **describe** alta concurrencia sin mencionar ningún control — el equivalente a un cliente que dice "va a entrar muchísima gente a la vez por esta única puerta" sin preguntar si eso es un problema. Mismo criterio: que quede señalado como riesgo técnico o hallazgo, en algún punto del expediente.

### Por qué este archivo casi nunca corre solo

```ini
addopts = -m "not integration"
```

Correr los cinco escenarios significa pagarle a seis empleados reales, cinco veces cada uno, con guías reales y (para dos de ellos) corriendo `dotnet test` de verdad sobre un repositorio real. Es exactamente el tipo de "auditoría completa" que uno hace antes de una entrega importante, no algo que se repite cada vez que se toca una línea de código — por eso `pytest.ini` lo deja afuera de una corrida normal de `pytest`, y solo se activa a propósito con `pytest -m integration`, cuando de verdad hace falta la certificación de punta a punta.

---

## Resumen: qué prueba cada capa, y qué NO prueba

| Archivo | Empleados reales | LLM real | RAG real | MCP real | Qué certifica | Qué NO certifica |
|---|---|---|---|---|---|---|
| `test_agents.py` | Sí (el código de cada agente) | No (mockeado) | Sí (Chroma local, gratis) | Parcial (solo funciones puras) | El criterio propio del agente: qué hace con lo que el LLM le da | Si el LLM de verdad razona bien |
| `test_graph.py` | No (actores con guion) | No | No | No | Que el cableado del grafo (rutas, ciclo, límite) sea correcto | Cualquier cosa sobre los agentes en sí |
| `test_scenarios.py` | Sí | Sí | Sí | Sí | Que el sistema completo, de punta a punta, entregue algo razonable | Nada queda sin probar — pero cuesta caro y tarda |

Las tres capas juntas son las que permiten decir, con confianza y sin quemar presupuesto en cada cambio chico, que el sistema multiagente funciona.
