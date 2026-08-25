# Cómo funciona el Product Agent (`agents/product_agent.py`)

## La analogía general

Imagina que este sistema multiagente es un **estudio de arquitectura e ingeniería**. Llega un cliente y dice, en sus propias palabras: *"quiero que mis empleados puedan pedir vacaciones y que su jefe las apruebe"*. Eso es un requerimiento en lenguaje natural: vago, incompleto, sin forma técnica todavía.

El **Product Agent** es el **analista de requerimientos** de ese estudio — la primera persona que atiende al cliente. Su trabajo no es diseñar la solución (eso lo hace el arquitecto, más adelante) ni construirla (eso lo hace el desarrollador). Su trabajo es **traducir lo que el cliente dijo a un documento formal** que el resto del equipo pueda usar sin tener que volver a preguntarle nada al cliente.

Ese documento formal es la **especificación** (`specification`), y es lo único que el Product Agent entrega. Todo lo demás en el pipeline (arquitectura, desarrollo, seguridad, testing, revisión) parte de ahí.

## El pipeline completo, en una fila de producción

Piensa en el sistema como una **línea de ensamblaje** donde cada estación agrega una pieza a un mismo expediente que viaja de estación en estación:

```
requirement → [Product] → specification → [Architect] → architecture → [Developer] → ... → [Reviewer] → veredicto
```

Ese "expediente" es el `EngineeringState` (definido en `graph/state.py`) — un diccionario compartido que cada agente lee (lo que dejaron los anteriores) y al que le agrega su parte, sin tocar lo que no le corresponde. El Product Agent es la **primera estación**: es el único que no depende de nadie más (no necesita RAG, no necesita acceso al código, no necesita nada salvo el requerimiento original y un LLM). Por eso es el agente #1 en el orden de construcción de la Fase 6 — no tiene piezas externas que puedan fallar.

## Qué hace el agente, paso a paso

### 1. Recibe el requerimiento

```python
requirement = state["requirement"]
```

Es literalmente el string que escribió el usuario. Si viene vacío, el agente se niega a trabajar (`raise ValueError`) — como un analista que no puede escribir una especificación de la nada, sin que el cliente diga algo primero.

### 2. Consulta a un "experto" (el LLM) con instrucciones muy claras de cómo hacer su trabajo

`_SYSTEM_PROMPT` es literalmente el **manual de instrucciones** que le damos al LLM para que actúe como ese analista senior: le decimos que identifique actores, que saque reglas de negocio (explícitas e implícitas), que redacte criterios verificables y que señale riesgos. Es como darle a un practicante una checklist detallada en vez de decirle "haz un buen análisis" y esperar que adivine qué es "bueno".

Un punto importante de esa checklist: *"si el requerimiento es ambiguo, no inventes en silencio, documenta la suposición"*. Es el equivalente a que el analista, en vez de asumir cosas y ya, escriba en el documento: *"asumí que cada empleado tiene un jefe directo definido en el sistema"* — para que el cliente (o el resto del equipo) pueda corregirlo si esa suposición está mal.

### 3. Obliga al LLM a responder con una forma fija (structured output)

Aquí está la parte más importante técnicamente. Un LLM normal respondería con un párrafo de texto libre — útil para un humano, inútil para que otro programa lo procese automáticamente. Es como pedirle a alguien "cuéntame sobre el clima" contra darle un formulario con casillas: **"temperatura: ___, lluvia: sí/no"**.

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

### 4. Entrega solo su parte del expediente

```python
return {
    "specification": specification.model_dump(),
    "messages": [...],
}
```

El agente no devuelve el expediente completo — devuelve un **update parcial**: "aquí está mi parte (`specification`), y una línea para la bitácora (`messages`)". Es como el analista que entrega su informe y lo grapa al expediente del cliente, sin tocar las páginas que van a llenar el arquitecto o el desarrollador después. Esto es intencional: así el agente ya tiene la forma exacta que va a necesitar cuando se conecte al grafo de LangGraph (Fase 7) — no hay que reescribirlo, solo "enchufarlo".

## Por qué está construido así (decisiones clave)

- **Nada de RAG, nada de MCP, nada de acceso a archivos.** Es deliberado: es el agente más simple posible, para poder probarlo aislado sin que un fallo en otra pieza (la base vectorial, el servidor MCP) contamine el diagnóstico. Es como probar el motor de un auto en un banco de pruebas antes de montarlo en el chasis.
- **`temperature=0`** en el LLM: le pedimos que sea lo más determinista posible (menos "creativo", más consistente) porque estamos generando un documento técnico, no un texto creativo.
- **`method="function_calling"`** en vez del modo estricto por defecto: el modelo usado (`nemotron-3.5-lightning:free`, gratuito en OpenRouter) es más propenso a rechazar el modo JSON-schema estricto. `function_calling` es una forma más "flexible" de pedir el mismo formulario, con más chance de que un modelo gratuito la entienda bien.
- **`@observe(name="product_agent")`**: cada vez que el agente corre, Langfuse registra cuánto tardó, qué prompt usó y qué devolvió — como una cámara de seguridad sobre el escritorio del analista, útil para depurar sin tener que confiar en la memoria de nadie.

## Cómo se prueba (el bloque `if __name__ == "__main__":`)

El archivo, al correrse directamente (`python agents/product_agent.py`), no importa nada de un grafo — se prueba **solo**, como un analista al que le das un caso de prueba antes de dejarlo trabajar con clientes reales:

1. Verifica que exista la API key (sin eso, ni siquiera lo intenta).
2. Arma un requerimiento de ejemplo ("empleado pide vacaciones, jefe aprueba/rechaza").
3. Llama al agente de verdad (llamada real al LLM, no simulada).
4. Imprime el JSON resultante y valida que ningún campo importante haya quedado vacío.
5. Envía las trazas a Langfuse antes de terminar (los scripts cortos necesitan "forzar" el envío, porque el batch normal es asíncrono y el proceso podría cerrarse antes de que se mande solo).

Este es el mismo patrón de prueba que usa el resto del repo (RAG, MCP): **cada pieza se valida sola antes de conectarla al resto**, para que si algo falla más adelante en el grafo, ya sepas que el problema es de "cableado" entre piezas y no de una pieza individual rota.

## Qué sigue

El Product Agent es la entrada del pipeline; el siguiente (`architect_agent.py`, agente 2/6) va a **leer** la `specification` que este agente generó y usarla junto con RAG (una base de conocimiento de arquitectura) para proponer un diseño técnico. La cadena de dependencia crece de a un eslabón por vez — así es como está pensado todo el orden de la Fase 6.
