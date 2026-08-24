# OWASP Top 10 aplicado al Sistema de Solicitudes de Vacaciones

Resumen curado del OWASP Top 10 (2021), priorizado según lo que la Constitución Técnica del proyecto marca como crítico, con ejemplos concretos del dominio (solicitudes de vacaciones, saldo, roles Empleado/Aprobador/RRHH). El Security Agent debe usar este documento para verificar cada cambio.

## Categorías priorizadas explícitamente por la Constitución

### A01:2021 — Control de Acceso Roto (prioridad máxima)

**Por qué aplica aquí**: el sistema tiene 3 roles con ámbitos estrictamente disjuntos y una regla no negociable (un aprobador no puede aprobar su propia solicitud).

- Ejemplo de vulnerabilidad: un Empleado cambia el `id` en `/solicitudes-vacaciones/{id}` y accede al detalle de una solicitud ajena.
- Ejemplo de vulnerabilidad: un Aprobador manipula el `id` en `POST /bandeja-aprobador/{id}/aprobar` para aprobar una solicitud propia que el sistema debería haber ocultado de su bandeja.
- Ejemplo de vulnerabilidad: un usuario RRHH invoca directamente `POST /bandeja-aprobador/{id}/aprobar` (forced browsing) aunque la UI no le muestre el botón — la protección de UI no es una protección real.
- Mitigación esperada: validación de rol en **cada** endpoint (no solo ocultar el botón en la vista), comparación server-side del actor autenticado contra el dueño del recurso, verificación de `aprobador != autor` y `aprobador.isActive == true` antes de cualquier transición de aprobación/rechazo.

### A06:2021 — Diseño Inseguro

**Por qué aplica aquí**: hay lógica de negocio (cálculo de días, saldo, solapamientos) que es tentador calcular en el cliente por UX, pero nunca debe ser la fuente de verdad.

- Ejemplo de vulnerabilidad: el formulario de creación de solicitud calcula "días solicitados" en JavaScript y el servidor confía en ese valor al descontar saldo — un usuario podría manipular el request y solicitar más días de los que el cálculo real permitiría.
- Ejemplo de vulnerabilidad: el saldo "estimado tras aprobación" mostrado al aprobador se calcula solo en el cliente y no se revalida en el servidor al momento de aprobar (riesgo agravado por condiciones de carrera entre que se muestra el detalle y se confirma la aprobación).
- Mitigación esperada: todo cálculo de negocio (días hábiles, saldo disponible, solapamientos) se recalcula íntegramente en el servidor en el momento de la acción, sin excepción. La validación de cliente es cosmética.

### A09:2021 — Fallas de Registro y Alertas de Seguridad

**Por qué aplica aquí**: la trazabilidad de decisiones de aprobación/rechazo tiene implicación legal (auditoría de RRHH, posible litigio laboral).

- Ejemplo de vulnerabilidad: una cancelación de solicitud aprobada (que restaura saldo) no queda registrada con actor y timestamp — imposible reconstruir quién ejecutó la reversión.
- Mitigación esperada: cada transición de estado genera un registro inmutable en `HistorialSolicitud` (evento, estado anterior, estado nuevo, actor, timestamp, comentario si aplica), implementado vía interceptor de `SaveChangesAsync` para que sea imposible olvidarlo en un handler nuevo.
- Nota de alcance: la auditoría de *inicio de sesión* y de *movimientos de saldo* (`HistorialSaldo`) está fuera del alcance del MVP — no reportar su ausencia como hallazgo salvo que el requisito cambie.

## Otras categorías relevantes del Top 10 (contexto general, no priorizadas explícitamente pero aplicables)

### A02 — Fallas Criptográficas
Los secretos (connection strings, claves de Identity) nunca van al repositorio; se gestionan por Secret Manager / variables de entorno / Key Vault. Las cookies de sesión requieren `Secure=true` (solo HTTPS) y `HttpOnly=true`.

### A03 — Inyección
El uso de EF Core con LINQ y parámetros tipados mitiga SQL Injection por diseño — un hallazgo válido sería el uso de SQL crudo interpolado con valores de usuario en cualquier repositorio.

### A04 — Diseño Inseguro (relacionado con A06)
Ver invariantes universales de la constitución: saldo nunca negativo, transiciones de estado limitadas a las documentadas, estados finales inmutables salvo la excepción `Approved → Cancelled`. Cualquier código que permita una transición no listada (ej. `Rejected → Approved`) es una falla de diseño.

### A05 — Configuración de Seguridad Incorrecta
Cabeceras obligatorias en producción: CSP, HSTS (`max-age` ≥ 1 año), `X-Content-Type-Options: nosniff`, `X-Frame-Options`. Su ausencia en `Program.cs`/middleware es un hallazgo directo.

### A07 — Fallas de Identificación y Autenticación
Gestionado vía ASP.NET Core Identity — no se debe implementar lógica de autenticación custom (hash de contraseñas propio, tokens hechos a mano, etc.).

### A08 — Fallas de Integridad de Software y Datos
Relevante para condiciones de carrera: sin `RowVersion` (concurrencia optimista) en `SolicitudVacaciones` y `SaldoEmpleado`, dos aprobaciones simultáneas pueden producir saldo negativo o doble descuento — es una falla de integridad de datos, no solo de rendimiento.

### A10 — Server-Side Request Forgery (SSRF)
Baja aplicabilidad directa: el sistema no realiza llamadas salientes a URLs proporcionadas por el usuario. Si en el futuro se agregan integraciones externas (fuera del alcance del MVP: SSO, AD, nómina), este ítem se vuelve relevante y debe reevaluarse.

## Checklist de verificación rápida para el Security Agent

- [ ] ¿El endpoint valida el rol del actor autenticado antes de ejecutar la acción?
- [ ] ¿La comparación "es mi propio recurso" usa el ID del usuario autenticado (claims), no un valor del request?
- [ ] ¿Se bloquea explícitamente la auto-aprobación (`aprobador == autor`) y el aprobador inactivo?
- [ ] ¿Todo cálculo de negocio (días, saldo) se recalcula en el servidor, ignorando lo que envía el cliente?
- [ ] ¿La transición de estado ejecutada está en la lista de transiciones válidas de la máquina de estados?
- [ ] ¿La acción genera un registro de auditoría con actor y timestamp?
- [ ] ¿El formulario de escritura usa un ViewModel dedicado (no la entidad de dominio) y token anti-forgery?
- [ ] ¿Hay manejo explícito de `DbUpdateConcurrencyException` en escrituras concurrentes sobre saldo o estado de solicitud?
