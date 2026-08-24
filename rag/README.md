# `rag/` — Retrieval Augmented Generation

Este módulo le da "memoria de consulta" a los agentes: en vez de que cada agente tenga que adivinar las reglas del proyecto, puede preguntarle a esta capa "dame lo que sepas sobre X" y recibe fragmentos reales de `knowledge/`.

Son 3 archivos que se usan en cadena, siempre en este orden:

```
knowledge/*.md  →  ingestion.py  →  vector_store.py (Chroma)  →  retrievers.py  →  agentes
                       ↑ usa
                  vector_store.py
```

---

## Analogía: una biblioteca con un bibliotecario que no lee, pero "huele" el tema

Imagina una biblioteca (`knowledge/`) con 7 libros repartidos en 4 estanterías: Arquitectura, Seguridad, Desarrollo y Testing.

- **`vector_store.py` es el edificio de la biblioteca**: define dónde está físicamente (`./chroma_db`), cómo están organizadas las estanterías, y sobre todo, contrata al "olfateador de temas" — el modelo de embeddings. Este olfateador no lee ni entiende español; lo único que sabe hacer es, dado un trozo de texto, decir "esto huele 73% a seguridad, 12% a arquitectura, 5% a testing..." y expresar ese "olor" como una lista de 384 números (el *embedding*). Frases que hablan de cosas parecidas producen listas de números parecidas — ese es todo el truco.

- **`ingestion.py` es el bibliotecario que recibe los libros nuevos**: agarra cada libro (`.md`), lo trocea en fichas más chicas y manejables (los *chunks* — no puedes oler un libro entero de una vez, pero sí una página), le pega una etiqueta a cada ficha (de qué estantería viene, de qué libro, bajo qué título), y le pasa cada ficha al olfateador del edificio para que le asigne su "olor" (embedding). Guarda la ficha + su olor en el fichero central. Si vuelve a recibir los mismos libros, tira el fichero viejo y lo rehace completo — así nunca queda una ficha vieja mezclada con una nueva.

- **`retrievers.py` es el mostrador de consultas**: alguien llega (un agente) y pregunta "necesito algo sobre auto-aprobación". El mostrador **no busca la palabra "auto-aprobación" letra por letra** — le pasa la pregunta al olfateador, obtiene su "olor", y va al fichero a buscar las fichas cuyo olor sea más parecido. Además, cada agente tiene su propia ventanilla (`get_security_retriever()`, `get_architecture_retriever()`...) que solo revisa fichas de su propia estantería — el Security Agent nunca recibe por error una ficha de Testing.

**Por qué "huele" y no "lee" es la parte importante**: esto es lo que permite que una pregunta como *"¿qué pasa si el aprobador es el mismo empleado que creó la solicitud?"* encuentre el fragmento que dice *"Un aprobador NO DEBE poder aprobar ni rechazar su propia solicitud"* — ninguna de esas dos frases comparte casi ninguna palabra literal, pero *huelen* casi igual porque hablan del mismo concepto (conflicto de interés / auto-aprobación).

---

## `vector_store.py` — el edificio

Único archivo que sabe que existe Chroma. Todo lo demás en el proyecto habla con él, no con Chroma directamente — así que si mañana cambiamos de vector store, solo se toca este archivo.

| Función | Qué hace | Analogía |
|---|---|---|
| `get_embedding_function()` | Devuelve el "olfateador": el modelo ONNX MiniLM-L6-v2 que ya trae `chromadb`, envuelto para que hable el idioma que espera LangChain. | Contratar al olfateador |
| `get_vector_store()` | Abre (o crea) la colección persistente en `./chroma_db`. | Abrir la puerta del edificio |
| `reset_collection()` | Vacía la colección por completo. | Tirar todo el fichero para rehacerlo de cero |

**Decisión clave y por qué**: se usa el embedding **ONNX** que ya trae `chromadb` en vez de `sentence-transformers` (que requiere `torch`, ~500MB+). Es el mismo modelo (MiniLM-L6-v2, 384 dimensiones), solo que en un formato más liviano — decisión forzada por poco espacio en disco, pero funcionalmente equivalente.

**Una sola estantería con etiquetas, no 4 estanterías separadas**: en vez de crear 4 colecciones físicas (una por dominio), hay **una sola colección** donde cada ficha lleva una etiqueta `domain`. Buscar "solo en la estantería de seguridad" es simplemente filtrar por esa etiqueta — más simple de mantener, y no impide que en el futuro alguien busque en dos estanterías a la vez.

---

## `ingestion.py` — el bibliotecario que ficha los libros nuevos

Se corre como script suelto: `python rag/ingestion.py`.

1. **Busca los libros**: recorre `knowledge/` y encuentra los `.md`, anotando de qué subcarpeta viene cada uno (esa subcarpeta *es* el dominio: `architecture`, `security`, `development`, `testing`).
2. **Trocea cada libro en fichas** (chunking), en dos pasadas:
   - Primero corta por títulos (`#`, `##`, `###`) — respeta la estructura que el humano ya escribió.
   - Si una sección todavía queda muy larga (una tabla grande, un bloque de código), la corta en pedazos de ~800 caracteres con un poco de superposición (120 caracteres) entre pedazos consecutivos, para no cortar una regla justo a la mitad.
3. **Etiqueta cada ficha**: dominio, archivo de origen, y el título más específico bajo el que vivía ese texto.
4. **Le pide al edificio (`vector_store.py`) que huela cada ficha** y la guarde.
5. **Reindexa desde cero cada vez que se corre**: primero vacía la colección (`reset_collection()`), luego mete todo de nuevo. Esto evita que, si editas un `.md` y vuelves a correr el script, queden fichas viejas y nuevas mezcladas y duplicadas.

Al final imprime cuántas fichas salieron de cada estantería — si alguna da 0, es la señal de que algo no se está encontrando bien.

---

## `retrievers.py` — el mostrador de consultas

Expone una función por dominio para que cada agente futuro pida justo lo suyo sin tener que saber cómo funciona el filtro por dentro:

```python
get_architecture_retriever()   # para el Architect Agent
get_security_retriever()       # para el Security Agent
get_development_retriever()    # para el Developer Agent
get_testing_retriever()        # para el Testing Agent
```

Por dentro, las 4 funciones llaman a una sola función genérica `get_retriever(domain, k=4)`, que arma el filtro `{"domain": "..."}` sobre la colección única — para no repetir la misma lógica 4 veces.

Uso típico desde un agente (más adelante, en Fase 6):

```python
from rag.retrievers import get_security_retriever

retriever = get_security_retriever(k=4)
fragmentos = retriever.invoke("¿puede un aprobador aprobar su propia solicitud?")
# fragmentos = lista de 4 Document, cada uno con .page_content y .metadata
```

---

## Cómo probar cada pieza de forma aislada

En ese orden — si un paso falla, no sigas al siguiente:

```powershell
.venv\Scripts\Activate.ps1

python rag/vector_store.py   # ¿el edificio abre y el olfateador funciona?
python rag/ingestion.py      # ¿los 7 libros se troceron y se guardaron?
python rag/retrievers.py     # ¿el mostrador devuelve fichas relevantes?
```

Cada script imprime pasos numerados y un resultado claro de éxito/error — están pensados para poder diagnosticar "¿el problema es el RAG o es el agente que lo usa?" sin tener que levantar todo el sistema.
