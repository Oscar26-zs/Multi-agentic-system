# Principios de Clean Code — Sistema de Solicitudes de Vacaciones

Aplicación concreta de SOLID y clean code al proyecto MVC real, según `constitution.md` sección 3 y las decisiones de diseño de `plan.md`.

## Responsabilidad única, aplicada por capa

Cada clase tiene una única razón para cambiar:

- Un **Command/Handler** de Application orquesta un único caso de uso (`CrearSolicitudCommand` solo crea solicitudes; no también valida saldo con lógica propia — delega esa regla al Dominio, ni tampoco persiste directamente — delega al repositorio).
- Un **Controlador** solo traduce HTTP ↔ Application. Un controlador que contiene un cálculo de días hábiles o una comparación de fechas de negocio tiene una responsabilidad de más — esa lógica no le pertenece.
- Una **entidad de Dominio** (`SolicitudVacaciones`) conoce sus propias reglas de transición de estado, pero no sabe cómo se persiste (no tiene atributos de EF Core) ni cómo se presenta (no conoce ViewModels).

Señal concreta de violación en este proyecto: si al leer un `*Controller.cs` encuentras un `if` que decide si hay saldo suficiente, esa lógica se filtró desde donde debía estar (Domain) hacia donde no debía (Web) — es un hallazgo de "responsabilidad única rota", no un detalle de estilo.

## Nombres significativos, en el dominio del negocio

El proyecto usa nombres en español que reflejan el lenguaje ubicuo del dominio real (no una traducción literal de términos técnicos genéricos): `SolicitudVacaciones` en vez de `Request`, `SaldoEmpleado` en vez de `Balance`, `pendingBalance` conceptualmente es "saldo comprometido" — congelado por solicitudes aún no resueltas, no simplemente "pending". Un nombre correcto en este proyecto es uno que un miembro de RRHH reconocería, no solo un programador.

## Funciones pequeñas, una sola responsabilidad

El cálculo de `DiasHabiles` (excluir sábados y domingos) es un ejemplo del patrón esperado: una función pura, sin efectos secundarios, sin dependencias de infraestructura, testeable con datos de entrada/salida directos — no un método de 80 líneas que también valida, persiste y notifica.

## Bajo acoplamiento entre capas

- Domain y Application **no conocen** Infrastructure ni Web — se comunican mediante interfaces (`IRepositorioSolicitudVacaciones`, `TimeProvider`) definidas en las capas internas e implementadas en las externas (inversión de dependencias).
- Consecuencia práctica: se debe poder instanciar y testear una entidad de `SolicitudVacaciones` o un handler de Application **sin** levantar una base de datos, sin ASP.NET Core, sin HTTP — si eso no es posible, hay una fuga de acoplamiento hacia Infrastructure.

## DRY aplicado con criterio (no DRY prematuro)

- El registro de auditoría se centraliza en **un** mecanismo (el interceptor `InterceptorAuditoriaSaveChanges` sobre `SaveChangesAsync`), en vez de duplicar la llamada de auditoría en cada handler — evita el riesgo real de "un desarrollador olvida registrar auditoría en el nuevo Command que agregó".
- `ApprovalAction` (definida inicialmente en la especificación como entidad separada) se consolidó dentro de `HistorialSolicitud` en el plan real, evitando duplicar el mismo tipo de registro de auditoría en dos tablas — un ejemplo de eliminar duplicación estructural detectada durante el diseño, no de abstraer prematuramente algo que aún no se repite.

## KISS / YAGNI, con ejemplos reales de scope explícitamente descartado

El proyecto documenta explícitamente funcionalidad que **no** se construye en el MVP para evitar sobre-ingeniería: recuperación de contraseña, integraciones externas (SSO, AD, nómina), múltiples niveles de aprobación con escalación automática, cancelación parcial de solicitudes, reportes/exportación CSV. Si una tarea de desarrollo empieza a construir hooks o abstracciones "por si en el futuro se necesita" alguna de estas, es una violación de YAGNI para este proyecto — la constitución exige explícitamente no asumir política no definida en una spec aprobada.

Mismo criterio con `HistorialSaldo`: la entidad está **definida** en el modelo de dominio pero **no implementada** en el MVP — existe como documentación de diseño futuro, no como código a medio construir. No se debe generar código parcial para esta entidad "por completitud".

## Límites claros entre módulos (ejemplo real de decisión de estructura)

El scaffold original del proyecto (`Solicitud_de_Vacaiones/`, generado por plantilla, sin separación de capas) no se migró incrementalmente — se decidió reescritura directa en 4 proyectos nuevos. Migrar incrementalmente hubiera arrastrado la violación de límites de capa (controladores con lógica de negocio) hacia el código nuevo. La lección aplicable: cuando el código existente no respeta los límites de módulo, mezclarlo con la nueva estructura es más costoso que empezar la separación de cero.

## Inmutabilidad donde el dominio lo exige

Los estados finales de una solicitud (`Approved`, `Rejected`, `Cancelled`, `Expired`) son inmutables salvo la única transición documentada (`Approved → Cancelled` antes del inicio del periodo). El código de Dominio debe hacer esta inmutabilidad explícita e imposible de saltarse (por ejemplo, rechazando cualquier método que intente mutar el estado desde un estado final no contemplado), no confiar en que "nadie va a llamar ese método en el estado equivocado".

## Autoevaluación del Developer Agent y criterio del Reviewer Agent

Antes de considerar completa una implementación, verificar:
- ¿Esta clase tiene una sola razón para cambiar?
- ¿Los nombres usados coinciden con el lenguaje de negocio del dominio (español, términos de `spec.md`), no con jerga técnica genérica?
- ¿Alguna lógica de negocio se filtró a Web o a un validador de FluentValidation en vez de vivir en Domain?
- ¿Se duplicó lógica de auditoría, cálculo de días o validación de fecha en más de un lugar en vez de reutilizar el mecanismo existente?
- ¿Se construyó algo para un caso de uso explícitamente fuera de alcance del MVP?
