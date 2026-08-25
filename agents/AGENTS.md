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

`llm.with_structured_output(ProductSpecification, method="function_calling")` es la instrucción que le dice al LLM: *"no me respondas con prosa libre, llena exactamente este formulario"*. El framework (`langchain`) se encarga de validar que la respuesta realmente tenga esa forma — si el LLM se "olvida" de un campo obligatorio, esto falla ruidosamente en vez de dejar pasar un documento incompleto.

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
- **`method="function_calling"`** en vez del modo estricto por defecto: el modelo usado (`nemotron-3.5-lightning:free`, gratuito en OpenRouter) es más propenso a rechazar el modo JSON-schema estricto. `function_calling` es una forma más "flexible" de pedir el mismo formulario, con más chance de que un modelo gratuito la entienda bien.
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
- **`_build_llm()` se duplicó desde `product_agent.py` en su momento, en vez de extraerse a un módulo común.** Con este segundo agente solo existía una segunda necesidad real de un cliente LLM; extraerlo antes habría sido tocar `product_agent.py` sin necesidad y construir infraestructura especulativa antes de que la pidiera un tercer caso de uso. Ese tercer caso de uso llegó con `security_agent.py` (agente 3/6): ahí es donde la duplicación se resolvió, extrayendo `agents/llm_factory.py` y migrando tanto `product_agent.py` como este archivo a importar `build_llm()` desde ahí.
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
- **`agents/llm_factory.py` nace con este agente.** `product_agent.py` y `architect_agent.py` documentaban explícitamente que duplicar `_build_llm()` era aceptable solo "hasta que un tercer caso de uso lo pidiera" — ese tercer caso de uso es `security_agent.py`. Ambos agentes anteriores se migraron para importar `build_llm()` desde el factory común en vez de seguir duplicando la función.
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

## Qué sigue

El siguiente agente (`developer_agent.py`, agente 4/6) va a **leer** la `architecture` (y el `security_review`, para no reintroducir lo que el Security Agent ya marcó como hallazgo) y va a ser el primero en usar el servidor MCP (`mcp_server/server.py`, Fase 5) para leer y modificar código real del repo objetivo — la primera dependencia nueva del pipeline que no es RAG. Se documenta acá mismo, como una sección nueva, cuando se construya.
