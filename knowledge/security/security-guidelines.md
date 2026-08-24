# Políticas de Seguridad — Sistema de Solicitudes de Vacaciones

Basado en la Sección 8 de la Constitución Técnica del proyecto MVC real.

## Autenticación y gestión de sesiones

- **ASP.NET Core Identity Framework** es la única vía de autenticación aprobada — no se implementa autenticación custom.
- Configuración obligatoria de la cookie de sesión:
  - `HttpOnly = true` — nunca accesible desde JavaScript.
  - `Secure = true` — solo se envía por HTTPS.
  - `SameSite = Strict` o `Lax` según el contexto del flujo.
  - La sesión expira tras un periodo configurable de inactividad; se renueva automáticamente mientras el usuario está activo.
- Invalidación de sesiones activas soportada vía Identity (actualización de `SecurityStamp`, revocación de tokens, `SignOutAsync`) — se incluye como funcionalidad básica de administración de usuarios.
- Recuperación de contraseña: **fuera de alcance del MVP** (funcionalidad de versión futura) — no debe implementarse ni asumirse disponible.

## Autorización por rol

- Todo endpoint valida el rol del actor autenticado **antes** de ejecutar cualquier lógica. Cualquier acceso fuera del ámbito definido se rechaza con **HTTP 403**.
- Reglas de ámbito por rol (no negociables, ver `constitution.md` sección 1):
  - **Empleado**: solo sus propias solicitudes y saldo. Nunca ve solicitudes de otros empleados.
  - **Aprobador**: ve todas las solicitudes `Pending` de todos los empleados (rol plano, sin jerarquía), pero **nunca las suyas propias** — prohibición explícita de auto-aprobación, validada tanto al listar (no debe aparecer en su propia bandeja) como al intentar aprobar/rechazar (bloqueo explícito si `aprobador == autor`).
  - **Un aprobador inactivo no puede aprobar ni rechazar** — se valida el estado activo del actor, no solo su rol.
  - **RRHH**: solo lectura. Nunca puede crear, editar, aprobar, rechazar ni cancelar solicitudes, ni en nombre propio ni de terceros.
- La verificación de "es dueño del recurso" se hace siempre contra el ID del usuario autenticado en el servidor (claims de Identity), nunca contra un valor recibido del cliente (parámetro de ruta, campo oculto de formulario, etc.) — previene IDOR.

## Validación: dónde vive cada tipo

Esta separación es una regla de seguridad, no solo de estilo:

- **Validación de entrada** (formato, requerido, longitud, estructura del ViewModel): FluentValidation, resuelto vía DI y ejecutado explícitamente con `ValidateAsync` desde el caso de uso o un filtro de acción propio. **Prohibido** el pipeline de auto-validación de MVC (`AddFluentValidationAutoValidation`) y la integración cliente `FluentValidation.AspNetCore` (deprecada).
- **Regla de negocio** (saldo disponible, solapamiento de fechas, transición de estado válida, autoridad del actor, restauración de saldo): SIEMPRE en el Dominio, NUNCA en FluentValidation ni en el cliente.
- **Toda validación de negocio ocurre en el servidor.** La validación del lado del cliente (JS) es solo para experiencia de usuario — nunca se considera una medida de seguridad ni sustituye la validación server-side.
- El número de días solicitados, el saldo estimado y cualquier cálculo derivado se recalculan siempre en el servidor; nunca se confía en el valor que envía el formulario.

## Protección contra overposting

Toda acción de escritura (crear, editar) usa un ViewModel/DTO dedicado — nunca la entidad de dominio expuesta directamente como modelo de binding. El binding se limita explícitamente a las propiedades permitidas.

## Gestión de secretos

Connection strings, claves de cifrado y cadenas de configuración de Identity **nunca** se almacenan en el repositorio. Se gestionan vía Secret Manager en desarrollo y variables de entorno / Key Vault / Azure App Configuration en producción.

## Cabeceras de seguridad obligatorias en producción

- `Content-Security-Policy` (CSP): restringe orígenes de scripts, estilos y fuentes.
- `Strict-Transport-Security` (HSTS): fuerza HTTPS, `max-age` ≥ 1 año.
- `X-Content-Type-Options: nosniff`.
- `X-Frame-Options: DENY` (o `SAMEORIGIN` si se requiere iframe explícitamente).

## Rate limiting

Proporcional al riesgo de cada tipo de endpoint (dimensionado para 50-100 usuarios concurrentes):

| Tipo de endpoint | Límite sugerido |
|---|---|
| Autenticación (login) | 5-10 intentos/min por IP/usuario |
| Escritura (crear/editar/aprobar/rechazar) | ~30/min por usuario |
| Lectura (consultas/listados) | ~120/min por usuario |

## Auditoría y trazabilidad (control de detección, OWASP A09)

- Toda transición de estado de una solicitud genera un registro de auditoría **inmutable** con actor, timestamp y tipo de evento — sin excepción.
- La auditoría de movimientos de solicitud (`HistorialSolicitud`) está dentro del alcance del MVP; la auditoría granular de movimientos de saldo (`HistorialSaldo`) queda fuera de alcance del MVP (fase futura) — **no debe asumirse implementada** al revisar código.
- Explícitamente **fuera de alcance en esta versión**: auditoría de inicio de sesión, cambios de usuario o acciones administrativas.

## Casos de abuso — prueba obligatoria antes de cada release

Estos escenarios deben tener un test explícito de seguridad; su ausencia es un hallazgo válido de revisión:

1. **Acceso cruzado entre empleados**: Empleado A intenta ver/editar solicitudes del Empleado B.
2. **IDOR**: modificar parámetros de ruta/query para acceder a recursos ajenos.
3. **Forced browsing**: acceder a rutas de aprobador siendo empleado, o viceversa.
4. **Escalación de privilegios**: usuario sin rol de aprobador intenta ejecutar acciones de aprobador.
5. **Auto-aprobación**: aprobador intenta aprobar su propia solicitud.
6. **Fallos de CSRF**: enviar POST sin token anti-forgery válido.
7. **Transiciones duplicadas**: reenviar la misma acción de aprobación/rechazo múltiples veces (debe fallar de forma controlada tras la primera).
8. **Envíos duplicados**: crear la misma solicitud múltiples veces por concurrencia/doble clic.

## Clasificación de datos sensibles

- El campo `Motivo` de la solicitud puede contener información médica y se clasifica como **Sensible/PII**.
- Visible únicamente para: el empleado dueño de la solicitud, el aprobador que la resuelve, y RRHH. Nunca debe exponerse en listados públicos ni en logs no protegidos.
- El historial de auditoría se clasifica como **Regulado** (posible sujeción a legislación laboral) — retención por defecto de 5 años desde la finalización del evento; pasado ese periodo, anonimizar o eliminar según política de la empresa.

## Concurrencia como control de integridad, no solo de rendimiento

`RowVersion` (concurrencia optimista) en `SolicitudVacaciones` y `SaldoEmpleado` no es solo una optimización — es el control que evita que dos aprobaciones simultáneas produzcan saldo negativo o doble descuento. Cualquier flujo de escritura sobre estas entidades debe manejar `DbUpdateConcurrencyException` explícitamente; ignorarla es un hallazgo de seguridad, no solo un bug.
