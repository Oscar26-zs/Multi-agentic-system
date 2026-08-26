# Cómo funciona el sistema multiagente (`agents/`)

Este documento explica, agente por agente, cómo está construido cada uno bajo `agents/`. Se va completando en el mismo orden en que se construyen (Fase 6 de `Guia_Construccion.md`): primero el más simple, después cada uno agregando una sola dependencia nueva a la vez.

## La analogía general

Imagina que este sistema multiagente es un **estudio de arquitectura e ingeniería**. Llega un cliente y dice, en sus propias palabras: *"quiero que mis empleados puedan pedir vacaciones y que su jefe las apruebe"*. Eso es un requerimiento en lenguaje natural: vago, incompleto, sin forma técnica todavía.

Cada agente es una persona distinta de ese estudio, atendiendo una estación distinta del mismo expediente:

- El **Product Agent** es el **analista de requerimientos** — la primera persona que atiende al cliente. Traduce lo que dijo a un documento formal (`specification`).
- El **Architect Agent** es el **arquitecto del estudio** — recibe esa especificación y, consultando el manual de estilo del equipo (RAG de arquitectura), decide el "cómo": stack, componentes, decisiones técnicas (`architecture`).
- Los agentes que siguen (Security, Developer, Testing, Reviewer) son el resto del equipo: quien revisa riesgos, quien construye, quien prueba, quien da el veredicto final. Se documentan aquí a medida que se construyen.

## El pipeline completo, en una fila de producción

Piensa en el sistema como una **línea de ensamblaje** donde cada estación agrega una pieza a un mismo expediente que viaja de estación en estación:

```
requirement → [Product] → specification → [Architect] → architecture → [Developer] → ... → [Reviewer] → veredicto
```

Ese "expediente" es el `EngineeringState` (definido en `graph/state.py`) — un diccionario compartido que cada agente lee (lo que dejaron los anteriores) y al que le agrega su parte, sin tocar lo que no le corresponde. Cada agente devuelve un **update parcial** del estado (su campo + una línea para `messages`), nunca el expediente completo — así ya tiene la forma exacta que va a necesitar cuando se conecte al grafo de LangGraph en la Fase 7, sin tener que reescribirlo.

---

## `agents/llm_factory.py` — la factory compartida de LLM

Los seis agentes necesitan lo mismo de un LLM (un cliente que responda, y en la mayoría de los casos una forma de forzar salida estructurada), así que esa pieza vive en un solo lugar en vez de duplicarse — ver la nota histórica de extracción en la sección del Security Agent más abajo. Lo que documento acá es el estado ACTUAL de ese archivo, que evolucionó más allá de esa extracción inicial tras un par de fallos reales en producción.

**`build_llm(temperature=0)`** construye el cliente probando **4 proveedores gratuitos en cascada, en orden**: NVIDIA NIM → Groq → Google AI Studio → OpenRouter (último, como red de seguridad, porque es "el proveedor original, ya confirmado que funciona"). Cada proveedor solo se intenta si su variable `<PROVEEDOR>_API_KEY` está en el entorno, y se valida con un ping barato (`llm.invoke([HumanMessage(content="ping")])`) antes de devolverlo. Es la respuesta directa al problema de cuota compartida gratuita que topamos varias veces durante la construcción (`Rate limit exceeded: free-models-per-day` de OpenRouter): en vez de depender de un solo proveedor, se rota al siguiente si el que se está probando falla.

**`invoke_structured(schema, messages, method="function_calling", temperature=0)`** es la función que product_agent, architect_agent, security_agent, reviewer_agent (y el resumen final de developer_agent/testing_agent) usan para pedir salida estructurada — **no** `build_llm().with_structured_output(schema).invoke(messages)` directo. Nace de un fallo real: un proveedor podía pasar el ping simple de `build_llm()` (responde texto plano sin problema) pero fallar en silencio al pedirle un schema Pydantic con objetos anidados — `with_structured_output()` devolvía `None` en vez de lanzar una excepción, y ese `None` recién explotaba más adelante con un `AttributeError` opaco al hacer `.model_dump()` dentro del agente. `invoke_structured()` mueve la validación al nivel de la llamada REAL (mismo orden de 4 proveedores): si un proveedor devuelve `None` o lanza una excepción generando el schema, prueba el siguiente antes de rendirse — y solo entonces le entrega al agente una instancia válida del schema.

Ambas funciones reintentan una vez el MISMO proveedor (`_MAX_INTENTOS_POR_PROVEEDOR = 2`, con `time.sleep(2.0)` entre intentos) si el error es "reintentable" (`_es_reintentable`: 5xx, timeout, o error de red sin `status_code` — nunca un 4xx, porque un 401/404/400 no se arregla solo). Nace de un 504 real de NVIDIA en producción durante la construcción.

**Por qué `developer_agent.py` y `testing_agent.py` siguen usando `build_llm()` directo (no `invoke_structured()`) para su ciclo ReAct:** la selección de proveedor se hace UNA sola vez por llamada; esos dos agentes hacen `build_llm().bind_tools(tool_schemas)` y **reusan ese mismo cliente durante todo el ciclo de tool-calling** — cambiar de proveedor a mitad de esa conversación rompería el formato de los tool calls ya emitidos. Solo su resumen final (`ImplementationSummary`/`TestingSummary`, después de cerrar el ciclo de tools) usa `invoke_structured()`, porque ahí sí es una llamada nueva e independiente.

---

## 1. Product Agent (`agents/product_agent.py`)

El Product Agent es la **primera estación**: es el único que no depende de nadie más (no necesita RAG, no necesita acceso al código, no necesita nada salvo el requerimiento original y un LLM). Por eso es el agente #1 en el orden de construcción de la Fase 6 — no tiene piezas externas que puedan fallar.

### Qué hace, paso a paso

**1. Recibe el requerimiento**

```python
requirement = state["requirement"]
```

Es literalmente el string que escribió el usuario. Si viene vacío, el agente se niega a trabajar (`raise ValueError`) — como un analista que no puede escribir una especificación de la nada, sin que el cliente diga algo primero.

**2. Consulta a un "experto" (el LLM) con instrucciones muy claras de cómo hacer su trabajo**

`_SYSTEM_PROMPT` es el **manual de instrucciones** que le damos al LLM para que actúe como ese analista senior: le decimos que identifique actores, que saque reglas de negocio (explícitas e implícitas), que redacte criterios verificables y que señale riesgos. Es como darle a un practicante una checklist detallada en vez de decirle "haz un buen análisis" y esperar que adivine qué es "bueno".

Un punto importante de esa checklist: *"si el requerimiento es ambiguo, no inventes en silencio, documenta la suposición"*. Es el equivalente a que el analista, en vez de asumir cosas y ya, escriba en el documento: *"asumí que cada empleado tiene un jefe directo definido en el sistema"* — para que el cliente (o el resto del equipo) pueda corregirlo si esa suposición está mal.

**3. Obliga al LLM a responder con una forma fija (structured output)**

Un LLM normal respondería con un párrafo de texto libre — útil para un humano, inútil para que otro programa lo procese automáticamente. Es como pedirle a alguien "cuéntame sobre el clima" contra darle un formulario con casillas: **"temperatura: ___, lluvia: sí/no"**.

`ProductSpecification` es ese formulario, escrito como un modelo de Pydantic:

| Campo | Qué captura | Analogía |
|---|---|---|
| `resumen` | 1-2 frases reformulando el pedido | El "asunto" del documento |
| `actores` | Quiénes participan (Empleado, Jefe directo, etc.) | Los "personajes" de la historia |
| `reglas_negocio` | Reglas que el sistema debe cumplir | Las "leyes" del dominio |
| `criterios_aceptacion` | Condiciones verificables de "está terminado" | El checklist de un inspector |
| `riesgos` | Casos borde, abusos posibles, datos sensibles | Las "banderas rojas" que el analista detecta |
| `supuestos` | Qué asumió el analista ante ambigüedad | Las notas al margen: "asumí que..." |

```python
specification = invoke_structured(
    ProductSpecification,
    [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=requirement)],
)
```

`invoke_structured(ProductSpecification, [...])` (ver la sección dedicada a `agents/llm_factory.py` más arriba) es la instrucción que le dice al LLM: *"no me respondas con prosa libre, llena exactamente este formulario"* — y además prueba varios proveedores gratuitos en cascada hasta que uno genere una respuesta estructurada válida, en vez de depender de uno solo. El framework (`langchain`) se encarga de validar que la respuesta realmente tenga esa forma — si el LLM se "olvida" de un campo obligatorio, esto falla ruidosamente en vez de dejar pasar un documento incompleto.

**4. Entrega solo su parte del expediente**

```python
return {
    "specification": specification.model_dump(),
    "messages": [...],
}
```

El agente no devuelve el expediente completo — devuelve un **update parcial**: "aquí está mi parte (`specification`), y una línea para la bitácora (`messages`)". Es como el analista que entrega su informe y lo grapa al expediente del cliente, sin tocar las páginas que van a llenar el arquitecto o el desarrollador después.

### Por qué está construido así (decisiones clave)

- **Nada de RAG, nada de MCP, nada de acceso a archivos.** Es deliberado: es el agente más simple posible, para poder probarlo aislado sin que un fallo en otra pieza (la base vectorial, el servidor MCP) contamine el diagnóstico. Es como probar el motor de un auto en un banco de pruebas antes de montarlo en el chasis.
- **`temperature=0`** en el LLM: le pedimos que sea lo más determinista posible (menos "creativo", más consistente) porque estamos generando un documento técnico, no un texto creativo.
- **`method="function_calling"`** en vez del modo estricto por defecto: los modelos gratuitos que prueba `agents/llm_factory.py` (NVIDIA NIM, Groq, Google AI Studio, OpenRouter) son más propensos a rechazar el modo JSON-schema estricto. `function_calling` es una forma más "flexible" de pedir el mismo formulario, con más chance de que un modelo gratuito la entienda bien — sea cual sea el proveedor que termine respondiendo.
- **`@observe(name="product_agent")`**: cada vez que el agente corre, Langfuse registra cuánto tardó, qué prompt usó y qué devolvió — como una cámara de seguridad sobre el escritorio del analista, útil para depurar sin tener que confiar en la memoria de nadie.

### Cómo se prueba (el bloque `if __name__ == "__main__":`)

El archivo, al correrse directamente (`python agents/product_agent.py`), no importa nada de un grafo — se prueba **solo**, como un analista al que le das un caso de prueba antes de dejarlo trabajar con clientes reales:

1. Verifica que exista la API key (sin eso, ni siquiera lo intenta).
2. Arma un requerimiento de ejemplo ("empleado pide vacaciones, jefe aprueba/rechaza").
3. Llama al agente de verdad (llamada real al LLM, no simulada).
4. Imprime el JSON resultante y valida que ningún campo importante haya quedado vacío.
5. Envía las trazas a Langfuse antes de terminar (los scripts cortos necesitan "forzar" el envío, porque el batch normal es asíncrono y el proceso podría cerrarse antes de que se mande solo).

---

## 2. Architect Agent (`agents/architect_agent.py`)

El documento que entrega el Product Agent (`specification`) llega ahora a la mesa del **arquitecto del estudio** — literalmente el rol que le da nombre a este agente. El arquitecto no vuelve a hablar con el cliente ni escribe código: su trabajo es decidir **el "cómo"** — qué stack, qué componentes, qué decisiones técnicas — y dejarlo por escrito para que el desarrollador (`developer_agent.py`, más adelante) sepa exactamente qué construir.

A diferencia del Product Agent, este arquitecto no improvisa de memoria: antes de proponer nada, **va a la biblioteca del estudio** (la base de conocimiento de arquitectura, `knowledge/architecture/`) y consulta las guías reales del equipo — convenciones de API, patrones aprobados, límites conocidos. Esa consulta es el **RAG**, y es la pieza que distingue a este agente del Product Agent.

Es el **agente 2 de 6** en el orden de construcción de la Fase 6 — el primero en introducir una dependencia externa nueva: el RAG (Fase 4, ya construido y probado de forma aislada antes de llegar aquí).

### Qué hace, paso a paso

**1. Recibe la especificación (no el requerimiento original)**

```python
specification = state["specification"]
```

El arquitecto no vuelve a leer lo que escribió el cliente en lenguaje natural — lee el documento formal que ya tradujo el analista. Si ese documento no existe (`specification` vacío), se niega a trabajar (`raise ValueError`): un arquitecto no puede diseñar sobre una especificación que todavía no llegó.

**2. Va a la biblioteca antes de proponer nada (RAG)**

```python
retriever = get_architecture_retriever()
docs = retriever.invoke(consulta_rag)
```

Esto es como un arquitecto real que, antes de decidir "usamos X patrón", va y revisa el manual de estilo del estudio para no proponer algo que contradiga una convención ya establecida. La consulta (`consulta_rag`) se arma con el resumen y las reglas de negocio de la especificación — no una pregunta genérica, sino una búsqueda dirigida a lo que este requerimiento puntual necesita.

`get_architecture_retriever()` (de `rag/retrievers.py`, Fase 4) filtra la base de conocimiento única para devolver **solo** fragmentos con `domain="architecture"` — el arquitecto no se distrae leyendo las guías de seguridad o testing, aunque estén en la misma base. Cada fragmento recuperado trae su fuente (`architecture-guidelines.md`, `api-design-guidelines.md`) como metadata, igual que una ficha de biblioteca con el libro de donde salió.

**3. Consulta al "experto" (LLM) con la especificación + lo que trajo de la biblioteca**

`_SYSTEM_PROMPT` es el manual de instrucciones del arquitecto senior: pedirle que proponga stack y componentes, que documente el trade-off de cada decisión (no solo la decisión), que el plan sea ejecutable por otro ingeniero (el Developer Agent), y — el punto más importante — que **base sus decisiones en el contexto real recuperado**, y si ese contexto no cubre algo, lo diga en vez de inventar una convención que el equipo no tiene. Es el mismo principio que "documenta la suposición" del Product Agent, aplicado a decisiones técnicas: nunca inventar en silencio.

El mensaje que recibe el LLM combina dos cosas: la especificación completa (en JSON) y el bloque de contexto con los fragmentos de `knowledge/architecture/` ya recuperados — como entregarle al arquitecto, sobre el mismo escritorio, el pedido del cliente y el manual de estilo abierto en la página relevante.

**4. Obliga al LLM a responder con una forma fija (structured output)**

`ArchitectureProposal` es el formulario — mismo mecanismo que `ProductSpecification`, pero con el vocabulario de un arquitecto:

| Campo | Qué captura | Analogía |
|---|---|---|
| `resumen` | 1-2 frases del enfoque técnico | El "concepto" del plano |
| `stack` | Lenguajes, frameworks, librerías propuestos | Los "materiales de construcción" |
| `componentes` | Módulos/capas/servicios principales | Los "planos" de cada ala del edificio |
| `decisiones_tecnicas` | Lista de `{decision, justificacion, trade_offs}` | Las notas del arquitecto: qué eligió, por qué, y qué sacrificó |
| `plan_alto_nivel` | Pasos de implementación en orden | La secuencia de obra para el maestro de obra (Developer Agent) |
| `riesgos_tecnicos` | Cuellos de botella, puntos únicos de falla, deuda técnica | Las advertencias estructurales del plano |
| `fuentes_consultadas` | Documentos de `knowledge/architecture/` usados | La bibliografía citada al pie del plano |

El campo `decisiones_tecnicas` es deliberadamente una lista de objetos y no de strings sueltos: obliga al LLM a nunca proponer algo sin decir también qué se sacrifica al elegirlo — igual que un arquitecto de verdad no puede decir solo "ponemos vigas de acero" sin que alguien pregunte "¿y eso qué nos cuesta?".

**5. No confía ciegamente en que el LLM cite bien sus fuentes**

```python
architecture["fuentes_consultadas"] = fuentes or architecture["fuentes_consultadas"]
```

Después de recibir la respuesta del LLM, el campo `fuentes_consultadas` se **sobreescribe** con los nombres de archivo que el retriever realmente devolvió (metadata `source`), no con lo que el LLM haya escrito por su cuenta en ese campo. Es la diferencia entre confiar en la memoria de alguien y revisar el registro real de qué libros sacó de la biblioteca: el LLM podría "recordar mal" o directamente omitir una fuente, pero el retriever no miente sobre qué le pasó al LLM como contexto.

**6. Entrega solo su parte del expediente**

```python
return {
    "architecture": architecture,
    "messages": [...],
}
```

Mismo patrón que el Product Agent: un **update parcial** del estado compartido. El arquitecto grapa su plano al expediente (`architecture`) y anota una línea en la bitácora (`messages`, acumulada gracias al reducer `operator.add` de `graph/state.py`), sin tocar `specification` ni adelantarse a escribir nada que le toque al Developer Agent.

### Por qué está construido así (decisiones clave)

- **RAG sí, MCP todavía no.** El arquitecto necesita conocimiento (las guías del equipo) pero no necesita tocar código real todavía — eso es trabajo del Developer Agent (agente 4/6, Fase 5 del MCP). Introducir una sola dependencia nueva a la vez es la regla de oro de la Fase 6: si algo falla acá, ya se sabe que el sospechoso es el RAG o el LLM, no el MCP.
- **La consulta al retriever se arma desde la especificación, no es una pregunta fija.** Así cada ejecución busca lo relevante a ESE requerimiento puntual, en vez de traer siempre el mismo fragmento genérico de las guías.
- **`fuentes_consultadas` se recalcula después del LLM, no se le pide "de memoria".** Decisión explicada arriba: la fuente de verdad de qué contexto entró es el retriever, no lo que el LLM decida recordar.
- **`_build_llm()` se duplicó desde `product_agent.py` en su momento, en vez de extraerse a un módulo común.** Con este segundo agente solo existía una segunda necesidad real de un cliente LLM; extraerlo antes habría sido tocar `product_agent.py` sin necesidad y construir infraestructura especulativa antes de que la pidiera un tercer caso de uso. Ese tercer caso de uso llegó con `security_agent.py` (agente 3/6): ahí es donde la duplicación se resolvió, extrayendo `agents/llm_factory.py` (ver la sección dedicada más arriba, que documenta cómo evolucionó después a un fallback de 4 proveedores más `invoke_structured()`) y migrando tanto `product_agent.py` como este archivo.
- **`temperature=0` y `method="function_calling"`**: mismas razones que el Product Agent — salida determinista para un documento técnico, y compatibilidad con un modelo gratuito de OpenRouter más tolerante a ese modo que al JSON-schema estricto.
- **`@observe(name="architect_agent")`**: cada corrida queda trazada en Langfuse — incluyendo, vía el LLM call interno, qué contexto RAG se le pasó al modelo. Eso permite depurar no solo "qué respondió el LLM" sino también "qué le dieron para leer".

### Cómo se prueba (el bloque `if __name__ == "__main__":`)

Al correrse directamente (`python agents/architect_agent.py`), el agente se prueba **solo**, sin pasar por el grafo:

1. Verifica que exista `OPENROUTER_API_KEY`.
2. Arma un estado con una `specification` de prueba escrita a mano (simulando lo que entregaría el Product Agent — no se invoca a ese agente, para mantener la prueba aislada).
3. Confirma que el retriever de arquitectura devuelve al menos un fragmento (si da 0 resultados, el error es del RAG — hay que correr `python rag/ingestion.py` antes — no del agente).
4. Invoca `architect_agent` de verdad (llamada real al LLM).
5. Imprime el JSON resultante y valida que ningún campo importante haya quedado vacío.
6. Fuerza el flush de traces a Langfuse antes de terminar.

---

## 3. Security Agent (`agents/security_agent.py`)

La propuesta que entrega el Architect Agent (`architecture`) llega ahora a la mesa del **ingeniero de seguridad del estudio**. No revisa código todavía — el Developer Agent (agente 4/6) ni siquiera existe en el pipeline en este punto — revisa el **diseño**: ¿qué pasa si este plano, tal como está escrito, se construye tal cual? ¿Hay un control de acceso roto, un cálculo de negocio que confía en el cliente, un dato sensible sin proteger? Es la última estación antes de que alguien escriba una sola línea de código real.

Es el **agente 3 de 6** en el orden de construcción de la Fase 6. Introduce una única dependencia nueva respecto al Architect Agent: el retriever de seguridad (`get_security_retriever`, ya probado aislado en Fase 4) — mismo patrón de RAG que ya se usaba, apuntando a un dominio distinto. Deliberadamente **no** introduce MCP todavía: el MCP (Fase 5) sirve para operar sobre archivos reales del repo, y en este punto del pipeline el repo real todavía no tiene cambios que revisar — eso llega recién con el Developer Agent.

### Qué hace, paso a paso

**1. Recibe la arquitectura (y la especificación, como contexto adicional)**

```python
architecture = state["architecture"]
specification = state.get("specification", {})
```

El insumo principal es lo que dejó el arquitecto. Si `architecture` está vacío, el agente se niega a trabajar (`raise ValueError`): un ingeniero de seguridad no puede auditar un plano que no existe. La `specification` se lee también, pero solo como contexto complementario (ej. qué riesgos funcionales ya había señalado el analista) — no es obligatoria por sí sola.

**2. Va a la biblioteca de seguridad antes de opinar (RAG)**

```python
retriever = get_security_retriever()
docs = retriever.invoke(consulta_rag)
```

Mismo mecanismo que el Architect Agent, pero contra `knowledge/security/` (`security-guidelines.md`, `owasp-guidelines.md`) en vez de `knowledge/architecture/`. La consulta se arma combinando el resumen y los componentes de la `architecture`, más los riesgos que ya había anotado la `specification` — así la búsqueda apunta a lo que ESTE diseño puntual necesita auditar, no a una checklist genérica de seguridad.

**3. Consulta al "experto" (LLM) con la arquitectura + la especificación + lo que trajo de la biblioteca**

`_SYSTEM_PROMPT` le da al LLM el rol de ingeniero de seguridad senior: evaluar control de acceso, autenticación, dónde vive cada validación (cliente vs. servidor), datos sensibles, auditoría y condiciones de carrera — y, algo específico de este agente, distinguir un **hallazgo real** de un **riesgo aceptado explícitamente fuera de alcance** (`knowledge/security/security-guidelines.md` documenta, por ejemplo, que la recuperación de contraseña o la auditoría de login quedan fuera del MVP). Sin esa distinción, un LLM de seguridad tiende a reportar como "hallazgo" cualquier cosa que le falte al diseño, aunque el equipo ya haya decidido conscientemente no cubrirla todavía.

**4. Obliga al LLM a responder con una forma fija (structured output)**

`SecurityReview` es el formulario:

| Campo | Qué captura | Analogía |
|---|---|---|
| `resumen` | 1-2 frases del veredicto de seguridad general | El "concepto" del informe de auditoría |
| `hallazgos` | Lista de `{severidad, categoria_owasp, descripcion, recomendacion}` | Cada bandera roja levantada, con su ficha completa |
| `riesgos_aceptados` | Riesgos reconocidos pero fuera de alcance según las guías | Las notas "esto lo sabemos, pero no es para esta versión" |
| `fuentes_consultadas` | Documentos de `knowledge/security/` usados | La bibliografía del informe |
| `aprobado` | `True`/`False` según si hay hallazgos bloqueantes | El sello final del auditor |

Igual que `decisiones_tecnicas` en el Architect Agent, `hallazgos` es una lista de objetos y no de strings sueltos: cada hallazgo tiene que traer su severidad, su categoría OWASP (o `"N/A"` si no aplica) y una recomendación accionable — nunca solo una frase de alerta suelta.

**5. No confía en que el LLM cite bien sus fuentes ni se autocalifique**

```python
security_review["fuentes_consultadas"] = fuentes or security_review["fuentes_consultadas"]
security_review["aprobado"] = not any(
    h["severidad"] in _SEVERIDADES_BLOQUEANTES for h in security_review["hallazgos"]
)
```

Mismo principio que `fuentes_consultadas` en el Architect Agent, aplicado dos veces acá: las fuentes se recalculan desde lo que el retriever realmente devolvió, y **`aprobado` se recalcula en Python, nunca se le pide al LLM que se autoevalúe**. Es la diferencia entre confiar en que un auditor diga "todo bien" de palabra y contar objetivamente cuántas banderas rojas de severidad `critica` o `alta` dejó escritas en su propio informe — si el LLM lista un hallazgo crítico pero "olvida" marcar `aprobado=False`, el campo derivado lo corrige igual.

**6. Entrega solo su parte del expediente**

```python
return {
    "security_review": security_review,
    "messages": [...],
}
```

Mismo patrón que los dos agentes anteriores: un **update parcial** del estado compartido (`security_review`), sin tocar `specification` ni `architecture`, dejando la puerta abierta para que el Developer Agent (agente 4/6) lea este informe antes de escribir código, y para que el Reviewer, más adelante, use `security_review["aprobado"]` como una de las señales de su veredicto final.

### Por qué está construido así (decisiones clave)

- **RAG de seguridad sí, MCP todavía no.** Según la tabla de la Fase 6 (`Guia_Construccion.md`), este agente introduce una única dependencia nueva a la vez — el retriever de seguridad — y explícitamente no necesita MCP para este análisis: audita una propuesta de diseño (texto estructurado en el estado), no archivos reales del repo. El MCP entra recién con el Developer Agent (agente 4/6), que sí lee/escribe código.
- **`riesgos_aceptados` como campo separado de `hallazgos`.** Decisión explicada arriba: sin un lugar correcto para "esto está fuera de alcance a propósito", el LLM contamina el informe con hallazgos que no son accionables para nadie (ya se decidió no resolverlos en esta versión).
- **`aprobado` se calcula en Python después del LLM, nunca se le pide "de memoria" al modelo.** Mismo principio que `fuentes_consultadas` en `architect_agent.py`: la fuente de verdad de si el diseño pasa o no es la lista de hallazgos que el propio LLM ya escribió, contada de forma determinista — no una casilla adicional que el modelo podría marcar de forma inconsistente con su propia lista.
- **`agents/llm_factory.py` nace con este agente.** `product_agent.py` y `architect_agent.py` documentaban explícitamente que duplicar `_build_llm()` era aceptable solo "hasta que un tercer caso de uso lo pidiera" — ese tercer caso de uso es `security_agent.py`. Ambos agentes anteriores se migraron para importar el factory común en vez de seguir duplicando la función. Con el tiempo (todavía dentro de esta misma etapa del proyecto) esa factory creció a un fallback de 4 proveedores y a `invoke_structured()` — ver la sección dedicada a `agents/llm_factory.py` más arriba; este agente ya usa `invoke_structured(SecurityReview, [...])` en vez de `build_llm().with_structured_output(...)` directo.
- **`temperature=0` y `method="function_calling"`**: mismas razones que los agentes anteriores — salida determinista para un informe técnico, compatibilidad con el modelo gratuito de OpenRouter.
- **`@observe(name="security_agent")`**: cada corrida queda trazada en Langfuse, incluyendo qué contexto de `knowledge/security/` se le pasó al modelo — útil para auditar no solo el veredicto sino también qué política concreta lo sustenta.

### Cómo se prueba (el bloque `if __name__ == "__main__":`)

Al correrse directamente (`python agents/security_agent.py`), el agente se prueba **solo**, sin pasar por el grafo:

1. Verifica que exista `OPENROUTER_API_KEY`.
2. Arma un estado con una `specification` y una `architecture` de prueba escritas a mano (simulando lo que entregarían el Product y el Architect Agent — no se invocan esos agentes, para mantener la prueba aislada).
3. Confirma que el retriever de seguridad devuelve al menos un fragmento (si da 0 resultados, el error es del RAG — hay que correr `python rag/ingestion.py` antes — no del agente).
4. Invoca `security_agent` de verdad (llamada real al LLM).
5. Imprime el JSON resultante, valida que `hallazgos` y `fuentes_consultadas` no hayan quedado vacíos, y muestra el `aprobado` calculado.
6. Fuerza el flush de traces a Langfuse antes de terminar.

---

## 4. Developer Agent (`agents/developer_agent.py`)

La propuesta del Architect Agent (`architecture`) llega ahora a la mesa del **ingeniero que construye de verdad**. A diferencia de los tres agentes anteriores, este no solo lee y redacta un documento estructurado: **actúa** sobre el repositorio real del sistema MVC (`REPO_TARGET_PATH`), explorando su estructura y creando/editando archivos a través de las tools MCP (`mcp_server/server.py`, Fase 5). Es el único agente autorizado a tocar el filesystem, y siempre lo hace a través de esas tools sandboxeadas — nunca abre un archivo directamente.

Es el **agente 4 de 6** en el orden de construcción de la Fase 6. Introduce dos cosas nuevas: (1) el retriever de desarrollo (`get_development_retriever`, dominio `knowledge/development/`) y (2) el primer uso real del **MCP** — un servidor externo con el que este agente conversa por protocolo, no por import directo.

Un detalle importante de **orden de ejecución**, no solo de orden de construcción: el pipeline real del grafo (ver `README.md`) es `Product → Architect → Developer → Security → Testing → Reviewer` — el Developer Agent corre **antes** que el Security Agent. Por eso este agente lee `architecture` (obligatoria) y `specification` (opcional, como contexto de negocio), pero **no** depende de `security_review`: en el punto del flujo donde corre, ese campo todavía no existe. (Esto también explica retroactivamente por qué `security_agent.py`, agente 3/6, se construyó y probó solo contra una `architecture` de prueba: el orden de construcción de la Fase 6 optimiza para introducir una dependencia nueva a la vez al probar cada agente aislado, y no necesariamente coincide con el orden de ejecución final del grafo.)

### Qué hace, paso a paso

**1. Recibe la arquitectura (y la especificación, como contexto de negocio)**

```python
architecture = state["architecture"]
specification = state.get("specification", {})
```

Igual que los agentes anteriores: si `architecture` está vacío, se niega a trabajar (`raise ValueError`).

**2. Va a la biblioteca de desarrollo antes de escribir código (RAG)**

Mismo mecanismo que Architect y Security, pero contra `knowledge/development/` (`coding-standards.md`, `clean-code-guidelines.md`), con la consulta armada desde el resumen, los componentes y el `plan_alto_nivel` de la arquitectura.

**3. Se conecta al servidor MCP por protocolo real, no por import**

```python
async with stdio_client(_mcp_server_params()) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools_result = await session.list_tools()
```

`_mcp_server_params()` lanza `python -m mcp_server.server` como subproceso stdio — exactamente el mismo camino que ejercita `tests/test_mcp_protocol.py`. El agente nunca importa `mcp_server/server.py` directamente: conversa con él como lo haría cualquier cliente MCP externo, lo que también significa que el sandboxing de rutas (`_safe_resolve`) queda enteramente del lado del servidor, fuera del alcance de este archivo.

**4. Bindea las tools MCP al LLM sin depender de un adapter externo**

```python
tool_schemas = [_tool_to_schema(t) for t in tools_result.tools]
llm_with_tools = build_llm().bind_tools(tool_schemas)
```

`requirements.txt` no incluye `langchain-mcp-adapters`. No hace falta: una `mcp.types.Tool` ya trae `name`, `description` e `input_schema` (JSON Schema), que es exactamente la forma `{"type": "function", "function": {"name", "description", "parameters"}}` que `bind_tools()` espera de un LLM compatible con OpenAI tools. `_tool_to_schema()` es solo ese remapeo de nombres de campo.

**5. Corre un ciclo ReAct manual, con presupuesto limitado**

```python
for _ in range(MAX_TOOL_ITERATIONS):
    ai_message = llm_with_tools.invoke(messages)
    messages.append(ai_message)
    if not ai_message.tool_calls:
        break
    for tool_call in ai_message.tool_calls:
        result = await session.call_tool(tool_call["name"], tool_call.get("args", {}))
        ...
```

Cada vuelta: el LLM decide si pide una tool o si ya terminó. Si pide una, el agente la ejecuta de verdad contra el servidor MCP real (no una simulación) y le devuelve el resultado como `ToolMessage` — incluido el mensaje de error tal cual, si la tool falla (ej. "ya existe", "escapa del repositorio"), para que el LLM tenga la chance de corregirse en la siguiente vuelta. `MAX_TOOL_ITERATIONS = 12` evita un loop infinito con un modelo gratuito que no sepa cuándo parar; si se agota, el agente no falla — sigue al resumen final con lo que se alcanzó a hacer, dejando una nota explícita (`⚠ se alcanzó MAX_TOOL_ITERATIONS...`) en vez de fallar en silencio. Esto se verificó en la práctica durante el smoke test: en una corrida con un `_mcp_scratch/` que ya tenía un archivo de una prueba anterior, el modelo repitió el mismo `create_file` fallido varias veces sin recuperarse solo — el límite cortó el loop igual, y el reporte final reflejó fielmente "0 archivos creados", sin inventar un resultado que no ocurrió.

**6. No le pregunta al LLM qué archivos tocó — lo cuenta él mismo**

```python
def _track_change(tool_call, result_text, is_error, diffs, archivos_creados, archivos_modificados):
    if is_error:
        return
    if tool_call["name"] == "create_file":
        ...
    elif tool_call["name"] == "update_file":
        ...
```

Esta es la decisión más importante del agente, y lleva un paso más allá el principio de "no confiar en que el LLM recuerde bien" que ya usaban `fuentes_consultadas` (Architect) y `aprobado` (Security): ahí el patrón era pedirle un valor al LLM y **corregirlo** después con un dato verificable. Acá directamente **no se le pide** al LLM que enumere qué archivos creó o modificó — después de varias vueltas de tool-calling, un modelo gratuito tiende a recordar mal una ruta exacta o el contenido preciso de lo que escribió. En cambio, `_track_change()` intercepta cada `create_file`/`update_file` que el LLM REALMENTE ejecutó (con éxito) contra el servidor y arma `archivos_creados`, `archivos_modificados` y un diff real (`difflib.unified_diff`, sobre el fragmento en `update_file` o el archivo completo en `create_file`) a partir de los argumentos y el resultado reales de esa llamada — no de lo que el LLM cree haber hecho.

No existe todavía una tool `get_diff` en `mcp_server/server.py` (su docstring dice explícitamente "run_tests y get_diff se agregarán después"), así que el diff se calcula en el propio agente con `difflib`, sobre el texto que el propio agente le pasó a `update_file`/`create_file` como argumento — no requiere una lectura adicional del archivo.

**7. Solo le pide criterio al LLM para la parte que sí lo requiere**

```python
summary = invoke_structured(
    ImplementationSummary, messages + [HumanMessage(content=_SUMMARY_PROMPT)]
)
```

`invoke_structured()` (no `build_llm().with_structured_output(...)` directo — ver la sección dedicada a `agents/llm_factory.py` más arriba) se usa acá porque esta llamada de resumen SÍ es independiente del ciclo de tools que la precedió: a diferencia del `bind_tools()` de más arriba (que necesita el mismo cliente durante todo el ciclo ReAct), acá no hay problema en que el fallback pruebe un proveedor distinto si hace falta. `ImplementationSummary` (structured output, en una llamada aparte DESPUÉS de cerrar el ciclo de tools) solo tiene dos campos: `resumen` y `notas` (desviaciones del plan, limitaciones, seguimientos sugeridos). Es la única parte del reporte que es opinión, no un hecho verificable — todo lo demás (`archivos_creados`, `archivos_modificados`, `diff`, `pasos_seguidos`) ya se calculó en el paso anterior y se copia tal cual al dict final.

**8. Entrega solo su parte del expediente**

```python
implementation = {
    "resumen": summary.resumen,
    "notas": summary.notas,
    "archivos_creados": archivos_creados,
    "archivos_modificados": archivos_modificados,
    "diff": _format_diffs(diffs),
    "pasos_seguidos": pasos,
    "fuentes_consultadas": fuentes,
}
return {"implementation": implementation, "messages": [...]}
```

Mismo patrón de update parcial que los tres agentes anteriores.

### Por qué está construido así (decisiones clave)

- **Conexión MCP por protocolo (`stdio_client` + `ClientSession`), no por import directo de `mcp_server/server.py`.** Ejercita el mismo camino que un cliente MCP externo real (igual que `tests/test_mcp_protocol.py`), y mantiene el sandboxing de rutas completamente del lado del servidor.
- **Sin `langchain-mcp-adapters`.** El schema de una `mcp.types.Tool` ya calza con el formato que `bind_tools()` espera; agregar una dependencia extra para ese remapeo habría sido infraestructura innecesaria.
- **`archivos_creados`/`archivos_modificados`/`diff`/`pasos_seguidos` se calculan en Python, nunca se le piden al LLM.** Ver el punto 6 arriba — es la aplicación más estricta hasta ahora del principio "no confiar en la memoria del modelo para hechos verificables", validado en la práctica durante el smoke test de este mismo agente.
- **`MAX_TOOL_ITERATIONS = 12` como salvavidas, no como fallo.** Si se agota, el agente igual entrega un reporte coherente con lo que realmente pasó (aunque sea parcial), en vez de lanzar una excepción y tirar todo el progreso del ciclo.
- **Sin dependencia de `security_review`.** Ver la nota de orden de ejecución más arriba: en el grafo real, Developer corre antes que Security, así que ese campo del estado todavía no existe cuando este agente se ejecuta.
- **`temperature=0` y `method="function_calling"`** para la llamada de resumen final: mismas razones que los agentes anteriores.
- **`@observe(name="developer_agent")`**: traza la corrida completa en Langfuse, incluyendo (vía los LLM calls internos) tanto las decisiones de tool-calling del ciclo ReAct como la llamada de resumen final.

### Cómo se prueba (el bloque `if __name__ == "__main__":`)

Al correrse directamente (`python agents/developer_agent.py`), el agente se prueba **solo**, sin pasar por el grafo — pero a diferencia de los tres agentes anteriores, esta prueba SÍ toca un repositorio real (`REPO_TARGET_PATH`), aunque sea con una `architecture` de prueba deliberadamente inofensiva (crear una nota de documentación en `_mcp_scratch/`, nunca código de producción):

1. Verifica `OPENROUTER_API_KEY` y que `REPO_TARGET_PATH` exista (si no, corta con un mensaje claro pidiendo clonar el repo MVC ahí — no intenta arrancar el servidor MCP contra una ruta inexistente).
2. Arma un estado con `specification` + `architecture` de prueba escritas a mano.
3. Confirma que el retriever de desarrollo devuelve al menos un fragmento.
4. Invoca `developer_agent` de verdad (LLM real + servidor MCP real vía subproceso stdio), dentro de un `try/finally`.
5. Imprime el JSON resultante y valida que `archivos_creados` y `pasos_seguidos` no hayan quedado vacíos.
6. **Siempre**, incluso si el paso anterior lanza una excepción al imprimir, limpia `_mcp_scratch/` en el `finally` — necesario porque un `_mcp_scratch/` con un archivo de una corrida previa hace que el siguiente `create_file` falle con "ya existe", confundiendo innecesariamente al LLM en la próxima corrida (bug real encontrado y corregido durante la construcción de este agente: el primer smoke test dejó basura porque el `print()` final crasheó por un carácter Unicode fuera de `cp1252` en la consola de Windows, y el cleanup —que corría después del print— nunca se ejecutó).
7. Fuerza el flush de traces a Langfuse antes de terminar.

**Nota de entorno (Windows):** si al correr este smoke test ves `UnicodeEncodeError: 'charmap' codec...` al imprimir el JSON, es la consola de Windows (cp1252), no un bug del agente — corre con `PYTHONIOENCODING=utf-8` por delante (`PYTHONIOENCODING=utf-8 python agents/developer_agent.py`).

---

## `agents/mcp_tools.py` — plomería MCP compartida (nace con el agente 5)

Con `testing_agent.py` (agente 5/6) apareció el segundo agente que necesita exactamente el mismo ciclo ReAct contra el servidor MCP que ya usaba `developer_agent.py` (agente 4/6): lanzar el servidor como subproceso, convertir sus tools al formato de `bind_tools()`, leer el resultado de una tool call, y trackear qué archivo se creó/modificó. Duplicar esa plomería una segunda vez ya no se justificaba — mismo criterio que llevó a extraer `agents/llm_factory.py` con el tercer agente que necesitó un cliente LLM.

`agents/mcp_tools.py` centraliza:
- `mcp_server_params()` — parámetros para lanzar `python -m mcp_server.server` como subproceso stdio.
- `tool_to_openai_schema(tool)` — convierte una `mcp.types.Tool` al formato `{"type": "function", ...}`.
- `result_text(result)` — extrae el texto de un `CallToolResult`.
- `summarize_args(args)` — representación corta de argumentos, para bitácora legible.
- `unified_diff(old, new, file_path)` y `track_file_change(tool_call, text, is_error)` — detectan si una tool call fue un `create_file`/`update_file` exitoso y devuelven `(file_path, bucket, diff)`; cada agente decide qué hacer con eso (`developer_agent.py` guarda el diff completo, `testing_agent.py` solo necesita saber qué archivo se tocó).

`developer_agent.py` se migró para importar estas funciones en vez de mantener su copia local (`_mcp_server_params`, `_tool_to_schema`, `_result_text`, `_unified_diff`, `_track_change` desaparecieron de ese archivo).

---

## 5. Testing Agent (`agents/testing_agent.py`)

Lo que implementó el Developer Agent (`implementation`) llega ahora a la mesa del **QA engineer del estudio**. Es el primer agente que no solo lee/escribe información — **verifica objetivamente** si algo funciona, corriendo pruebas reales sobre el repositorio real, no describiendo lo que "debería" pasar.

Es el **agente 5 de 6** en el orden de construcción de la Fase 6. Introduce una única tool nueva: **`run_tests`**, agregada a `mcp_server/server.py` en este mismo paso (su docstring decía explícitamente "run_tests y get_diff se agregarán después" — get_diff sigue sin agregarse, `developer_agent.py` nunca la necesitó). El resto de la plomería (conexión MCP, ciclo ReAct, tools bindeadas) es la misma que `developer_agent.py`, ahora compartida vía `agents/mcp_tools.py`.

### La tool nueva: `run_tests` (`mcp_server/server.py`)

```python
@server.tool()
def run_tests(subpath: str = "", filter: str = "") -> dict:
    ...
    cmd = ["dotnet", "test", str(base), "--nologo"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=RUN_TESTS_TIMEOUT_SECONDS)
    ...
```

Corre `dotnet test` de verdad (vía `subprocess.run`, con timeout — `RUN_TESTS_TIMEOUT_SECONDS`, default 300s, para que un test colgado no cuelgue también al agente) sobre `subpath` (o la raíz del repo si no se especifica) y parsea con una regex (`_DOTNET_TEST_SUMMARY_RE`) las líneas de resumen que `dotnet test` imprime por cada proyecto de test, ej.:

```
Failed!  - Failed:     1, Passed:     2, Skipped:     0, Total:     3, Duration: 422 ms - Smoke.Tests.dll (net10.0)
```

Suma `passed`/`failed`/`skipped`/`total` de TODAS las líneas que aparezcan (una solución con varios proyectos de test imprime una por proyecto). Se validó en la práctica contra un proyecto xUnit real (2 tests que pasan, 1 diseñado para fallar): `run_tests("Smoke.Tests")` devolvió `{"passed": 2, "failed": 1, "total": 3, "exit_code": 1, ...}`, coincidiendo exactamente con la salida real de `dotnet test`.

### Qué hace el agente, paso a paso

**1. Recibe la implementación (y especificación + revisión de seguridad, como contexto opcional)**

```python
implementation = state["implementation"]
specification = state.get("specification", {})
security_review = state.get("security_review", {})
```

`implementation` es obligatoria (si está vacía, `raise ValueError`: no hay nada que probar sin que el Developer Agent haya corrido antes). `specification` (criterios de aceptación) y `security_review` (hallazgos) son opcionales, leídas con `state.get(...)`.

Un detalle de **orden de ejecución** distinto al del Developer Agent: el pipeline real del grafo es `Product → Architect → Developer → Security → Testing → Reviewer` (ver `README.md`) — a diferencia de Developer (que corre ANTES que Security y por eso no puede depender de `security_review`), cuando Testing corre, Security ya corrió. Por eso este agente sí puede usar `security_review["hallazgos"]` como contexto: `knowledge/testing/testing-strategy.md` pide explícitamente cubrir con test los "casos de abuso" (auto-aprobación, IDOR, forced browsing...), y esos hallazgos son la señal más directa de cuáles priorizar. Se lee opcionalmente y no como dependencia dura para que el agente siga siendo probable de forma aislada (Fase 6) con solo una `implementation` de prueba.

**2. Va a la biblioteca de testing antes de decidir qué correr (RAG)**

Mismo mecanismo que los agentes anteriores, contra `knowledge/testing/testing-strategy.md`, con la consulta armada desde el resumen y archivos de la implementación, los criterios de aceptación y las descripciones de los hallazgos de seguridad.

**3-4. Se conecta al MCP y bindea las tools — igual que Developer, vía `agents/mcp_tools.py`**

**5. Corre el mismo ciclo ReAct, pero con una obligación explícita en el prompt**

```
Es OBLIGATORIO invocar la tool run_tests al menos una vez antes de
terminar [...] No has terminado tu trabajo hasta que hayas corrido
run_tests de verdad y visto un resultado real.
```

A diferencia de `developer_agent.py` (donde "no hizo nada" ya se ve reflejado en `archivos_creados` vacío), acá un ciclo que solo explora sin nunca correr `run_tests` produce un reporte con `aprobado=False` y cero tests — indistinguible de "no se pudo verificar nada". Por eso el prompt lo deja explícito en vez de confiar en que el modelo lo infiera solo.

**6. Nunca le pregunta al LLM cuántos tests pasaron — lo cuenta él mismo**

```python
def _aggregate_run_tests(run_tests_calls: list[dict]) -> dict:
    totals = {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
    ...
    aprobado = bool(run_tests_calls) and totals["failed"] == 0 and totals["total"] > 0 and not algun_timeout
```

Cada llamada real y exitosa a `run_tests` que el LLM ejecutó se captura (`json.loads` sobre el texto que devuelve la tool) en `run_tests_calls`; `_aggregate_run_tests()` suma sus totales en Python. Es la aplicación más crítica hasta ahora del principio "no confiar en la memoria del modelo para hechos verificables" (mismo que `archivos_creados` en Developer, `aprobado` en Security): un número de tests pasados inventado por el LLM sería particularmente engañoso para el Reviewer, que lo usa como señal objetiva de si la implementación funciona.

`aprobado` exige explícitamente `total > 0`, no solo `failed == 0` — "no se encontraron/corrieron tests" NO es lo mismo que "los tests pasan", aunque ambos casos den `failed=0`.

**7. Solo le pide criterio al LLM para la parte que sí lo requiere**

`TestingSummary` (vía `invoke_structured()` — ver la sección dedicada a `agents/llm_factory.py` más arriba —, en una llamada aparte después del ciclo, igual que `ImplementationSummary` en el Developer Agent): `resumen`, `casos_generados` (tests nuevos que agregó, si hubo), `hallazgos` (criterios sin cobertura, fallos relevantes) y `notas`. Todo lo numérico/verificable (`passed`/`failed`/`skipped`/`total`/`aprobado`, `archivos_creados`, `archivos_modificados`, `pasos_seguidos`, `comandos_ejecutados`) se calcula en Python y se copia tal cual al `test_results` final.

### Cómo se prueba (el bloque `if __name__ == "__main__":`)

Igual que `developer_agent.py`: preflight de `OPENROUTER_API_KEY` y `REPO_TARGET_PATH`, estado con `implementation` de prueba escrita a mano (simulando al Developer Agent), confirma que el retriever de testing devuelve resultados, invoca `testing_agent` de verdad (LLM real + MCP real + `dotnet test` real) y valida que `pasos_seguidos`/`comandos_ejecutados`/`total` no queden vacíos.

**Validación durante la construcción:** se armó un sandbox temporal con un proyecto xUnit real (`Smoke.Tests`, 2 tests que pasan + 1 diseñado para fallar) y se confirmó que `mcp_server.server.run_tests()` devuelve el pass/fail exacto (`passed=2, failed=1, total=3`) llamándolo directamente. La corrida completa del agente (ciclo ReAct de punta a punta vía LLM) quedó bloqueada por el límite diario gratuito de OpenRouter (`Rate limit exceeded: free-models-per-day`) antes de completarse — la tool MCP y el patrón de ciclo ReAct están validados (es el mismo que ya se probó de punta a punta en `developer_agent.py`), pero la corrida específica de `testing_agent.py` con LLM real queda pendiente de repetirse cuando se libere el límite (reinicia diariamente) o se agreguen créditos a la cuenta de OpenRouter.

---

## 6. Reviewer Agent (`agents/reviewer_agent.py`)

Todo el expediente llega ahora a la última mesa: la del **tech lead del estudio**, el socio que firma el trabajo antes de dárselo por terminado. No consulta ninguna biblioteca nueva (no hay RAG) ni toca el repositorio (no hay MCP) — su materia prima es el expediente completo que ya armaron los cinco agentes anteriores: la especificación, el plano de arquitectura, lo que construyó el desarrollador, el informe de seguridad y los resultados de las pruebas. Su trabajo es leerlo todo de punta a punta y decidir: ¿esto se entrega (`APPROVED`), o hay que devolverlo a alguien puntual del equipo para que lo corrija (`REJECTED`, con `return_to`)?

Es el **agente 6 de 6**, y el único que no introduce ninguna dependencia nueva — es pura evaluación sobre el estado ya acumulado. Con este agente se completa la Fase 6 de la guía: los seis agentes existen y están probados de forma aislada.

### Qué hace, paso a paso

**1. Recibe el expediente completo**

```python
specification = state.get("specification", {})
architecture = state.get("architecture", {})
implementation = state.get("implementation", {})
security_review = state.get("security_review", {})
test_results = state.get("test_results", {})
```

Todos se leen con `state.get(...)`, salvo `implementation`, que es la única que se exige (`raise ValueError` si está vacía): sin al menos lo que construyó el Developer Agent, no hay nada concreto que un tech lead pueda firmar. `security_review` y `test_results` vacíos son válidos para poder probar este agente de forma aislada (Fase 6) sin tener que simular el pipeline completo — aunque en el grafo real, para cuando el Reviewer corre, ya existen los cinco.

**2. Le pide al LLM un veredicto con motivos y feedback accionable**

```python
verdict = invoke_structured(
    ReviewVerdict,
    [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=mensaje_usuario)],
)
```

Igual que Product/Architect/Security, usa `invoke_structured()` (ver la sección dedicada a `agents/llm_factory.py` más arriba) — no `build_llm().with_structured_output(...)` directo. `_SYSTEM_PROMPT` le da al LLM el rol de tech lead: evaluar el expediente completo (no un solo aspecto), ser exigente pero justo, y — el punto más importante — dirigir el `feedback` específicamente al agente que tiene que corregirlo, no "al equipo" en general. `ReviewVerdict` es el formulario:

| Campo | Qué captura | Analogía |
|---|---|---|
| `status` | `APPROVED` o `REJECTED` | El sello de aprobado/rechazado en la carpeta |
| `resumen` | Evaluación breve del expediente completo | La nota de tapa del informe |
| `motivos` | Razones concretas detrás del veredicto | Los puntos que el tech lead marcó en la revisión |
| `feedback` | Accionable, dirigido al agente destino | La nota post-it pegada en la página exacta que hay que corregir |
| `return_to` | A qué agente vuelve el trabajo si `REJECTED` | A qué escritorio del estudio vuelve la carpeta |

**3. No confía en que el LLM respete lo que otros agentes ya verificaron de forma objetiva**

```python
security_bloquea = security_review.get("aprobado") is False
testing_bloquea = test_results.get("aprobado") is False

if security_bloquea or testing_bloquea:
    data["status"] = "REJECTED"
    ...
```

Esta es la decisión más importante del agente, y la aplicación más crítica hasta ahora del principio que ya venía de agentes anteriores ("no confiar en la memoria del modelo para hechos verificables"): `security_review["aprobado"]` y `test_results["aprobado"]` NO fueron calculados por el LLM del Reviewer — ya los calculó Python de forma determinista en `security_agent.py` y `testing_agent.py`, contando hallazgos bloqueantes y tests en rojo reales. Si cualquiera de los dos es `False`, `_coerce_verdict()` fuerza `REJECTED` con un `return_to` fijo (`architect_agent` para seguridad, `developer_agent` para testing) **sin importar qué haya decidido el LLM** — incluso si el LLM, por ser "amable" o no darle suficiente peso a un hallazgo, hubiera dicho `APPROVED`. Es el mismo principio que corregía `fuentes_consultadas` o contaba `archivos_creados`, llevado a la decisión de mayor riesgo de todo el pipeline: el Reviewer es el último filtro, y es el peor lugar posible para que un modelo gratuito se equivoque por exceso de indulgencia.

El guardrail de seguridad manda específicamente a `architect_agent` y no a `security_agent`: tal como está construido `security_agent.py` (agente 3/6), audita la ARQUITECTURA propuesta, no código — si hay un hallazgo bloqueante, lo que hay que corregir es el diseño.

Fuera de esos dos guardrails (todo aprobado, o el LLM ya dijo `REJECTED` por su cuenta con un `return_to` válido), el veredicto del LLM se respeta tal cual — el Reviewer no le saca autoridad al modelo, solo le pone un piso que no puede cruzar.

**4. Entrega su parte del expediente — y no toca el contador del ciclo**

```python
return {"review": review, "messages": [...]}
```

A diferencia de todos los agentes anteriores, `reviewer_agent()` NO incrementa `state["iteration"]`. Ese contador (y la comparación contra `MAX_ITERATIONS`) es responsabilidad de la conditional edge en `graph/edges.py`, que todavía no existe (Fase 7) — el comentario de `graph/state.py` ya lo dejaba explícito: "lo compara MAX_ITERATIONS en workflow.py". El Reviewer solo dice qué pasó; decidir si el ciclo sigue o se corta es trabajo del grafo, no del agente.

### Por qué está construido así (decisiones clave)

- **Sin RAG, sin MCP.** Es deliberado: no hay una guía externa que consultar en esta etapa ni código que tocar — el material de revisión es el estado que ya dejaron los cinco agentes anteriores.
- **Los guardrails deterministas se validaron directamente, sin gastar LLM.** `_coerce_verdict()` se probó con un `ReviewVerdict` sintético que dice `APPROVED` a propósito, confirmando que igual se fuerza `REJECTED`/`architect_agent` cuando `security_review["aprobado"]` es `False`, y `REJECTED`/`developer_agent` cuando `test_results["aprobado"]` es `False` — la parte más crítica del agente no depende de que el LLM esté disponible para poder verificarse.
- **`return_to` limitado a un `Literal` de 5 valores exactos** (los cinco agentes anteriores), no texto libre: así el grafo (Fase 7) puede enrutar la conditional edge con un `match`/diccionario simple, sin tener que interpretar lenguaje natural.
- **`temperature=0` y `method="function_calling"`**: mismas razones que los cinco agentes anteriores.
- **`@observe(name="reviewer_agent")`**: traza la corrida en Langfuse, incluyendo el expediente completo que se le pasó al modelo como contexto — útil para auditar, caso por caso, si un `APPROVED`/`REJECTED` fue razonable dado lo que el modelo tenía delante.

### Cómo se prueba (el bloque `if __name__ == "__main__":`)

Al correrse directamente (`python agents/reviewer_agent.py`), corre DOS escenarios sobre el mismo expediente de prueba (specification + architecture + implementation escritas a mano, simulando a los tres primeros agentes):

1. **Escenario A** — `security_review["aprobado"]=True` y `test_results["aprobado"]=True`: el veredicto queda a criterio del LLM.
2. **Escenario B** — se fuerza `security_review["aprobado"]=False` con un hallazgo simulado: el script hace `assert` de que el resultado sea `REJECTED` con `return_to == "architect_agent"`, sin importar qué haya dicho el LLM — verificación explícita de que el guardrail determinista manda.

**Validación durante la construcción:** el ciclo LLM completo (escenarios A y B) quedó bloqueado por el límite diario gratuito de OpenRouter (`Rate limit exceeded: free-models-per-day`, mismo límite que ya había topado `testing_agent.py`, resetea a las 00:00 UTC). En su lugar, `_coerce_verdict()` se validó de forma aislada con cuatro casos sintéticos (sin gastar cuota de LLM): guardrail de seguridad fuerza `REJECTED`/`architect_agent` aunque el LLM diga `APPROVED`; guardrail de testing fuerza `REJECTED`/`developer_agent` aunque el LLM diga `APPROVED`; sin bloqueos se respeta el veredicto del LLM; y un `REJECTED` sin `return_to` válido cae a `developer_agent` por defecto. Los cuatro pasaron. Queda pendiente repetir el smoke test completo (con LLM real) cuando se libere el límite diario o se agreguen créditos a la cuenta de OpenRouter.

---

## Los 6 agentes, completos

Con `reviewer_agent.py` se cierra la Fase 6 de `Guia_Construccion.md`: los seis agentes (`product_agent`, `architect_agent`, `security_agent`, `developer_agent`, `testing_agent`, `reviewer_agent`) existen y fueron probados de forma aislada, cada uno agregando una sola dependencia nueva a la vez (LLM solo → RAG → RAG de otro dominio → MCP → MCP + nueva tool → nada nuevo, solo evaluación).

**Actualización — el proyecto completo ya está terminado.** Las Fases 7-10 de `Guia_Construccion.md` se cerraron después de que se escribió esta sección: `graph/nodes.py`, `graph/edges.py` y `graph/workflow.py` (Fase 7, documentados en `graph/GRAPH.md` con la misma analogía del estudio, extendida a "el gerente de proyecto que coordina la fila de producción"); `tests/` en 3 capas — unitarios mockeados, estructurales del grafo, y 5 escenarios end-to-end reales (Fase 8); `app.py` como CLI vía `argparse` (Fase 9); y `README.md` reescrito con el estado real del proyecto (Fase 10). El límite diario gratuito de OpenRouter que bloqueó varios smoke tests durante la construcción de este documento (ver las notas de validación de Testing y Reviewer más arriba) se resolvió con el fallback de 4 proveedores de `agents/llm_factory.py` documentado al principio de este archivo — ya no depende de un solo proveedor gratuito.
