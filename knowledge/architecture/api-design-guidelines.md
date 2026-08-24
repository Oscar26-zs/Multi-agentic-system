# Guía de Diseño de API — Sistema de Solicitudes de Vacaciones

Convenciones reales aplicadas en el contrato de endpoints MVC del proyecto (`plan.md` sección 6).

## Convención de rutas

- **Idioma**: español, **kebab-case**. Ejemplo real: `/solicitudes-vacaciones`, `/bandeja-aprobador`, `/solicitudes-vacaciones/{id}/cancelar-aprobada`.
- **Recursos en plural**: `/solicitudes-vacaciones`, no `/solicitud-vacaciones`.
- **Acciones no-CRUD como sub-recurso verbal**: cuando una operación no es un CRUD puro (aprobar, rechazar, cancelar), se expresa como un sub-path de acción sobre el recurso: `POST /bandeja-aprobador/{id}/aprobar`, `POST /bandeja-aprobador/{id}/rechazar`, no como un PATCH genérico de estado. Esto hace explícito en la URL qué transición de negocio ocurre, mejorando trazabilidad y logs.

## Contrato real de endpoints

| Método | Ruta | Caso de uso | Actor |
|---|---|---|---|
| `POST` | `/solicitudes-vacaciones` | Crear solicitud | Empleado |
| `GET` | `/solicitudes-vacaciones` | Listar mis solicitudes (paginado) | Empleado |
| `GET` | `/solicitudes-vacaciones/{id}` | Detalle + historial de auditoría | Empleado |
| `PUT` | `/solicitudes-vacaciones/{id}` | Editar solicitud Pending | Empleado |
| `POST` | `/solicitudes-vacaciones/{id}/cancelar` | Cancelar Pending | Empleado |
| `GET` | `/saldo` | Consultar saldo e historial | Empleado / RRHH |
| `GET` | `/bandeja-aprobador` | Listar Pending de todos los empleados | Aprobador |
| `GET` | `/bandeja-aprobador/{id}` | Detalle con saldo estimado tras aprobación | Aprobador |
| `POST` | `/bandeja-aprobador/{id}/aprobar` | Aprobar (descuenta saldo) | Aprobador |
| `POST` | `/bandeja-aprobador/{id}/rechazar` | Rechazar (comentario obligatorio) | Aprobador |
| `POST` | `/solicitudes-vacaciones/{id}/cancelar-aprobada` | Cancelar Approved antes del inicio | Aprobador |
| `GET` | `/rrhh/solicitudes` | Consulta filtrada de solo lectura | RRHH |
| `GET` | `/rrhh/saldos/{empleadoId}` | Saldo de un empleado específico | RRHH |

Regla: **cada endpoint debe trazar a un caso de uso documentado**. No se agregan endpoints "por si se necesitan" — si no hay un caso de uso (`CU-XX`) o requisito funcional (`RF-XXX`) que lo respalde, no se implementa.

## Versionado

No hay estrategia de versionado de API explícita en el MVP (aplicación MVC server-rendered, no API pública consumida por terceros). Si en el futuro se expone una API REST independiente del front Razor, debe definirse versionado por path (`/api/v1/...`) antes de publicarla — no se asume implícitamente.

## Paginación

- **Listados** (`GET /solicitudes-vacaciones`, `GET /bandeja-aprobador`, `GET /rrhh/solicitudes`): estrategia **offset-based** (server-side). No se implementa cursor-based en el MVP.
- Limitación aceptada y documentada: bajo concurrencia extrema, offset-based puede producir duplicados o saltos entre páginas — riesgo de bajo impacto, aceptado explícitamente por el Product Owner para este MVP.
- Objetivo de rendimiento: listado paginado ≤ 2s p95.

## Filtrado

- La bandeja de aprobador soporta filtro por empleado y rango de fechas, combinables.
- RRHH soporta filtro por estado, empleado (autocompletar) y rango de fechas, combinables.
- Si no hay coincidencias, la respuesta debe indicar explícitamente la ausencia de resultados (no un listado vacío sin contexto): `"No se encontraron solicitudes que coincidan con los filtros aplicados"`.
- Tiempo de respuesta objetivo ante cambio de filtro: ≤ 2 segundos.

## Manejo de errores y mensajes

Los mensajes de error de negocio son **texto exacto y literal**, no interpretaciones libres del agente — están definidos en la especificación y deben reproducirse tal cual:

- `"Saldo insuficiente para esta solicitud"`
- `"La fecha de inicio no puede ser anterior a mañana"`
- `"La fecha de fin no puede ser anterior a la de inicio"`
- `"La solicitud incluye días que ya están comprometidos en otra solicitud"`
- `"No se puede aprobar: existe solapamiento con solicitud aprobada"`
- `"No se puede aprobar: saldo insuficiente al momento de la aprobación"`
- `"No se puede cancelar: el periodo de vacaciones ya ha iniciado"`
- `"No puedes aprobar ni rechazar tu propia solicitud; otro aprobador debe resolverla"`

Regla: cuando el Developer Agent genere manejo de errores para estos casos, debe usar el mensaje exacto de la especificación, no una paráfrasis.

## Idempotencia y protección contra envíos duplicados

- Las transiciones de estado (aprobar/rechazar/cancelar) deben protegerse contra reenvíos duplicados (doble clic, reintentos de red): la segunda invocación sobre una solicitud que ya cambió de estado debe fallar de forma controlada (estado ya no es `Pending`), no re-ejecutar el efecto (doble descuento de saldo).
- La creación de solicitudes debe protegerse contra creación duplicada por concurrencia (mismo empleado, mismo rango de fechas, doble submit).
- Toda acción de escritura vía formulario POST requiere token anti-forgery válido (CSRF) — es uno de los casos de abuso con prueba obligatoria.

## Contratos de entrada: ViewModels, no entidades de dominio

Toda acción de escritura (crear, editar) recibe un ViewModel/DTO dedicado, nunca la entidad de dominio directamente — previene overposting (que el cliente envíe campos como `Status` o `RowVersion` que no debería poder alterar). El binding de modelo se limita explícitamente a las propiedades permitidas.

## Códigos de estado HTTP

- Acceso fuera del rol/ámbito permitido del actor autenticado → **403 Forbidden** (no 404, para no ambigüar entre "no existe" y "no autorizado" salvo que ocultar la existencia del recurso sea intencional).
- Violación de regla de negocio en un POST/PUT (saldo insuficiente, solapamiento, transición inválida) → respuesta de validación con el mensaje exacto de negocio, renderizada de vuelta en el formulario (patrón MVC estándar, no JSON API).
