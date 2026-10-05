# Auditoría de fiabilidad — 5 de octubre de 2026

Alcance: búsqueda actual en Europa. No se enviaron candidaturas ni comunicaciones, no se ejecutó el pipeline diario contra producción y no se modificaron sus registros. Los cambios se implementaron y probaron en la copia aislada `/tmp/job-reliability-oct05/work`, partiendo de `7ace4b11`. El worktree hijo externo de Orca, rama `luisgius/reliability-oct05`, no recibió los cambios de scratch y permanece vacío de estas correcciones. Tras la aprobación de las revisiones, el coordinador integró los 40 archivos revisados en `/Users/luisgimenez/job_search`, rama `job-hunter-daily-pipeline`, verificando sus SHA-256 y respetando el bloqueo del planificador. No se ha ejecutado el pipeline diario con ellos ni se ha modificado la base de producción; queda por observar la siguiente ejecución programada.

## Diagnóstico comprobado

Fuentes locales: `output/logs/daily-2026-10-05.log`, `output/digest_2026-10-05.html`, CV originales `cv/*.md`, configuración y copia SQLite obtenida mediante conexión de solo lectura a `output/tracker.sqlite3`. La copia y las respuestas locales de diagnóstico permanecen fuera del repositorio, en `/tmp/job-reliability-oct05/`; no se incluyen CV, credenciales ni razonamientos completos en este informe.

La ejecución **28**, iniciada a las **06:00:03 UTC**, conserva estos valores:

| Métrica | Registro original | Interpretación verificada |
|---|---:|---|
| Recogidas | 5.185 | Correcto según log y estadísticas persistidas |
| Tras deduplicar | 4.592 | Correcto |
| Tras filtros | 3 | Dos ofertas nuevas elegibles y un reintento de la cola |
| Evaluaciones | 3 | Tres intentos; solamente **una completada** |
| Coincidencias | 2 | Incorrecto: eran errores; **cero coincidencias válidas** |
| Tarjetas del resumen | 2 | Predium y Lemrock, ambas con `model returned nothing` |
| CV adaptados / candidaturas enviadas | 0 / 0 | Confirmado; tabla de intentos de envío vacía en la copia |

La evaluación completada fue **Legartis, Legal AI Engineer (Berlin or Leipzig), 10/100**. Sus motivos persistidos citan alemán excelente obligatorio y titulación jurídica; el CV indica alemán A2 y grados en Informática y Administración de Empresas. Predium y Lemrock fallaron, no obtuvieron puntuación válida. Por tanto, sería incorrecto decir que fallaron las tres evaluaciones.

El log de filtros nuevos registra **2 conservadas y 4.588 descartadas sobre 4.590 entradas**; se añade después un reintento de la cola. Los primeros motivos de rechazo fueron: título excluido 2.654, título no incluido 1.911, ubicación 13, antigüedad 9 e idioma 1. Son contadores del **primer rechazo**, no una evaluación independiente de todos los requisitos de cada oferta.

El campo histórico `finished_at` repetía la hora inicial, aunque el log termina aproximadamente a las 06:06 UTC. Se corrige el reloj utilizado al finalizar; no se reescribió el registro histórico.

## Cadena del modelo: evidencia y límites

El log muestra respuestas HTTP **429** de las dos alternativas Gemma gratuitas, con tres intentos por solicitud. Nemotron produjo dos respuestas que no eran JSON utilizable y una evaluación válida. El recurso local Qwen produjo dos respuestas finales vacías, ambas con 1.500 tokens de salida. No hay evidencia de que esas respuestas vacías se debieran a un fallo de conexión o a un timeout.

Se reprodujo el mecanismo con **dos solicitudes locales**, sobre la oferta conservada de Predium y el mismo prompt/CV/modelo/límite de generación. Ollama informó versión **0.33.2**; el modelo siguió siendo **`qwen3.8:27b`**.

| Solicitud local | Límite | Resultado | Tokens generados | Tiempo |
|---|---:|---|---:|---:|
| Configuración anterior | 1.500 | `finish_reason=length`, contenido final vacío, 6.348 caracteres de razonamiento | 1.500 | 100,62 s |
| `reasoning_effort=none` | 1.500 | `finish_reason=stop`, JSON válido de cinco campos, 62/100 | 408 | 24,23 s |

La documentación oficial de [Ollama sobre compatibilidad OpenAI](https://docs.ollama.com/api/openai-compatibility) y [razonamiento](https://docs.ollama.com/capabilities/thinking) explica el control de razonamiento y su separación de la respuesta. [OpenRouter documenta los tokens de razonamiento](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens). La prueba local demuestra que, en esta instalación/modelo, el presupuesto podía consumirse sin producir respuesta final y que desactivar el razonamiento recupera una respuesta. Las respuestas HTTP crudas de la ejecución original no se habían conservado: atribuir exactamente el mismo mecanismo a cada fallo histórico sigue siendo una inferencia respaldada por el log y la reproducción, no una observación directa de aquellos cuerpos HTTP.

El código anterior también aceptaba `reasoning` como sustituto del contenido final. Se elimina esa confusión. La causa exacta de las dos salidas no JSON de Nemotron no puede determinarse sin sus cuerpos originales; no se cambió de modelo para ocultarlo.

Las respuestas reales capturadas se reprodujeron, sin más solicitudes, a través del nuevo cliente y `score_job`: la primera queda como error diagnosticado; la segunda se valida con 62/100. **62 está por debajo del umbral 65**. Recuperar JSON no convierte Predium en adecuada: exige comunicación en alemán además de inglés, mientras el CV indica A2. La valoración local sigue siendo un resultado del modelo, no una etiqueta humana de calidad.

## Correcciones implementadas

- `src/llm.py`: únicamente el contenido final puede ser respuesta; registra uso incluso si falla la extracción; distingue salida vacía de agotamiento del límite con metadatos, sin registrar razonamiento ni CV. Una respuesta truncada no repite el mismo presupuesto; los vacíos transitorios conservan reintentos acotados. La cadena valida claves, tipos, finitud, límites y campos permitidos **antes** de aceptar un modelo y avanza a la alternativa si no son válidos. Conserva las causas de todos los fallos y atribuye el modelo por hilo, evitando contaminación entre evaluaciones concurrentes.
- `config.yaml`, `src/config.py`: el recurso **local de evaluación** desactiva razonamiento; no cambia ningún modelo. Los controles opcionales de generación por alternativa se validan. La configuración de adaptación de CV no se modifica por este diagnóstico.
- `src/scoring.py`, `src/models.py`, `src/main.py`: estado `scoring_pending`, métricas separadas de intentadas/completadas/fallidas/pendientes/coincidencias. El campo antiguo `scored` pasa a significar completadas. Solo una evaluación válida por encima del umbral cuenta como coincidencia. Una ejecución donde todo falla indica explícitamente que no pudo seleccionar ofertas.
- `src/db.py`: al agotar intentos o edad conserva el JSON de la oferta y el diagnóstico para revisión o reintento controlado; siguen vigentes los límites. No se reactivan automáticamente registros agotados. Los errores no se persisten como candidaturas gestionadas.
- `src/tailor.py`, `src/apply/autoapply.py`: una evaluación fallida no genera CV aunque el umbral sea cero; la etapa de envío deja intactos estados pendientes y confirmaciones inciertas. Se conservan los registros previos a pulsar envío, deduplicación y bloqueo ante confirmación incierta.
- `src/digest.py`, plantilla: separa pendientes de coincidencias, mantiene visibles confirmaciones inciertas incluso en el HTML de emergencia, muestra cola activa y evaluaciones detenidas, y diferencia publicación, actualización y primera detección.
- `src/sources/ats_boards.py`: Greenhouse conserva `updated_at` como actualización; si falta `first_published`, la publicación es desconocida, no se sustituye por una actualización reciente.

## Filtros frente al CV

Los tres CV disponibles sostienen experiencia de ciencia de datos en Uber y Buynomics, analítica en Porsche, Python/SQL, previsión, experimentación, A/B testing e inferencia causal. Idiomas declarados: inglés C1, español nativo, alemán A2 y polaco principiante. No se inventa autorización de trabajo a partir de residencia o idioma.

1. Se añaden títulos adyacentes acotados: Product Analyst/Product Analytics, Experimentation Analyst/Scientist, Decision Science Analyst y Causal Inference Scientist. Requieren **funciones cuantitativas** (SQL/Python/estadística más experimentación, causalidad o métricas de producto). No basta con el título. Se mantienen exclusiones de senior, lead, manager, research scientist y prácticas, además del rechazo de mínimos explícitos de cinco o más años para esta vía adyacente. Los rangos, requisitos preferidos y experiencia no indicada no se convierten en mínimos inventados. Los prefiltros de Landing.jobs y JustJoin se alinean con estas familias para no perderlas antes del filtro final.
2. La lengua del anuncio deja de ser un filtro de elegibilidad. Solo evidencia explícita de un requisito lingüístico profesional incompatible con un nivel conocido puede provocar rechazo. La cita se conserva. Idiomas no indicados, redacción ambigua o nivel desconocido pasan como desconocidos a evaluación. Se prueban negaciones, requisitos opcionales, distintos niveles por idioma, cursos ofrecidos y competencias de compañeros para evitar falsos descartes. El parser es deliberadamente conservador, no un extractor lingüístico universal.
3. El prompt distingue requisitos obligatorios de preferencias y distingue idioma del anuncio del requerido para trabajar. Los requisitos no indicados y la autorización laboral no demostrada permanecen desconocidos.
4. Se mantienen los países europeos configurados, GB condicionado a patrocinio explícito y la prioridad de **72 horas**. La primera detección no sustituye publicación; las ofertas sin fecha siguen la política existente de omisión. Un reintento conserva la fecha original y necesita evidencia actual de la fuente.

Ejemplo real que merece revisión: **Mindbox, Product Analyst, Kraków**, guardado como título no incluido, publicado el 1 de octubre a las 08:12 UTC y detectado el 2. Era reciente cuando se detectó, pero ya superaba 72 horas el día 5. No se conserva su descripción completa: es evidencia de una exclusión por título que conviene investigar, **no prueba de una oportunidad compatible perdida**. La nueva vía se verifica con ejemplos funcionales representativos, sin declarar apta esta oferta concreta.

## Pruebas y revisión

Dos agentes Astra revisaron de forma independiente filtros y cadena/estados. Sus hallazgos —mezcla de niveles entre idiomas, cursos confundidos con requisitos, puntuaciones fuera de rango y sobrescritura de pendientes por autoapply— se devolvieron para corrección y se cubrieron con regresiones.

- Suite completa sin red: **2.500 aprobadas, 2 omitidas, 69 de red excluidas y 1 fallo esperado**, antes de los últimos ocho casos adicionales de idioma. La validación final de esa revisión en scratch se registra a continuación.
- Últimos casos de idioma junto con regresiones de fiabilidad: **93 aprobados**.
- Validación final de la revisión previa a estas correcciones de presentación: **2.508 aprobadas, 2 omitidas, 69 excluidas, 1 fallo esperado y 1 advertencia en 66,79 s**, salida 0, ejecutada en `/tmp/job-reliability-oct05/work` con `/Users/luisgimenez/job_search/.venv/bin/python -m pytest -o addopts="" -m "not network" -q --tb=short`. Evidencia: `/tmp/job-reliability-oct05/orca-validation.md` y `orca-validation-pytest.log`. Incluye los ocho casos adicionales de idioma; no es una ejecución del worktree externo ni de producción. Las dos omisiones dependen de Git, ausente en scratch; el fallo esperado es la limitación conocida de un doble de prueba de disco lleno. Tras las correcciones de presentación, la suite enfocada `tests/test_digest.py tests/test_reliability.py tests/test_main.py` obtuvo **192 aprobadas y 1 advertencia en 1,28 s**, con salida 0 y sin red, en la misma copia scratch. No se volvió a ejecutar la suite completa después de estos ajustes de presentación; el resultado de 2.508 corresponde a la revisión anterior.
- Evaluación congelada de diez casos: contrato aprobado; informe regenerado en `evals/job_fit/offline-report.json`. Las respuestas simuladas no prueban precisión de ranking.
- Pruebas de errores totales, mezcla histórica (3 intentadas/1 completada/2 fallidas/0 coincidencias), recuperación posterior de pendientes, umbral cero, alternativa ante JSON inválido, límites de tokens, fechas, concurrencia y estados de envío.
- Dos solicitudes reales, exclusivamente al modelo local; no se consultaron proveedores remotos con CV ni se enviaron mensajes. Los controles de fuentes en red no se ejecutaron masivamente.

Comando de la suite: `python -m pytest -o addopts='' -m 'not network' -q --tb=short`.

## Ejemplo del resumen corregido

El [HTML reconstruido](examples/reliability-2026-10-05.html) utiliza evidencia histórica y está marcado como ejemplo; no es una ejecución nueva. Incluye la puntuación y los motivos persistidos de Legartis sin inventar la descripción ausente. Distingue la hora de inicio histórica de una hora de generación, los resultados de esta ejecución de la cola acumulada, y la falta de datos de la entrada histórica agotada.

> Recogidas 5.185 → deduplicadas 4.592 → elegibles/reintentos 3. Evaluaciones intentadas: 3; completadas: 1; fallidas: 2. Coincidencias válidas: **0**. Predium y Lemrock: **pendientes de evaluación, no validadas**. Cola total pendiente: 7, incluidas 5 que necesitan observación actual. Una evaluación adicional está detenida por límite de intentos. CV adaptados: 0; candidaturas enviadas: 0.

Si todas las evaluaciones fallan, se añade: **«La evaluación falló para todas las ofertas intentadas; esta ejecución no pudo seleccionar oportunidades»**. No se presenta como ausencia demostrada de ofertas adecuadas.

## Limitaciones y siguientes requisitos

- No se conserva el contenido íntegro de las 5.185 entradas originales. No se puede medir retrospectivamente cobertura o falsos negativos globales, ni demostrar que se cubren todas las empresas. La auditoría se limita a los datos retenidos, mecanismos de código y casos verificables.
- JustJoin tuvo HTTP 503 y Allegro descartó 40 registros malformados y alcanzó el límite de páginas. No se ha demostrado ni reparado aquí la causa del cambio de payload de Allegro. Requiere una muestra actual verificable y pruebas de contrato antes de ampliar cobertura.
- El registro que ya estaba agotado tenía `job_json=NULL`: la nueva retención protege futuros casos, no recupera datos borrados previamente. Los siete pendientes no fueron reintentados ni modificados en producción.
- Los 429 de los modelos gratuitos siguen siendo una limitación externa. El recurso local depende de Ollama disponible y del modelo instalado. Las mejoras de extracción no garantizan corrección semántica ni calibración del ranking. Hace falta un conjunto de ofertas reales etiquetadas para medirla; el prompt revisado aún no tiene una evaluación comparativa amplia.
- El parser de requisitos conserva como desconocidos redacciones no reconocidas y niveles inferiores que no compara exhaustivamente. El mínimo de experiencia es una salvaguarda acotada para títulos adyacentes, no una auditoría universal de experiencia de todas las ofertas.
- Las fechas de los agregadores pueden reflejar su propia publicación/ingesta; la corrección demostrada aquí se refiere a Greenhouse. No se afirma equivalencia universal con la primera publicación del empleador.
- Las pruebas se ejecutaron con el entorno existente Python **3.9.6**, aunque `pyproject.toml` declara >=3.10, y apareció la advertencia preexistente urllib3/LibreSSL. Conviene actualizar el entorno en una tarea separada y repetir los controles; no se cambiaron dependencias durante esta auditoría.
- No hacen falta preferencias personales nuevas para estas correcciones. Para ampliar después la selección será útil confirmar países concretos con autorización de trabajo, disposición a mudarse/presencialidad y si se deben revisar ofertas sin fecha. Asia queda fuera de este trabajo.


## Coordinación de la entrega

Se utilizó la orquestación real de Orca, run `run_9255f3ab6643`, con dos trabajadores Codex Astra:

- `task_4d0e54d9ac31`: validación completa aislada aprobada (2.508 pruebas), con evidencia de protecciones de envío y pendientes.
- `task_4e2402820dd3`: revisión de evidencia completada; detectó etiquetas incorrectas en el ejemplo, separación insuficiente entre ejecución y cola, y procedencia de integración imprecisa.
- `task_d80892900310`: correcciones devueltas al mismo trabajador, implementadas y aprobadas tras 192 pruebas específicas y comprobación del HTML contra la copia SQLite. El coordinador revisó el resultado antes de integrar.

Los informes de los trabajadores están en `/tmp/job-reliability-oct05/orca-validation.md`, `orca-evidence-review.md` y `orca-evidence-fixes.md`. Sus terminales se liberaron tras aceptar los resultados; no quedan trabajadores con una decisión de cierre pendiente.


Validación final tras integrar en el repositorio: **2.513 aprobadas, 1 omitida, 69 de red excluidas, 1 fallo esperado y 1 advertencia**, sin fallos inesperados, en 66,97 s. Evidencia: `/tmp/job-reliability-oct05/integrated-pytest.log`. Esta ejecución incluye las últimas correcciones del resumen y los controles que requieren Git. El conjunto congelado de diez evaluaciones también volvió a pasar. El grafo de contexto se reconstruyó con `graft build` (80 archivos), y `git diff --check` no encontró errores.

La única prueba omitida en el repositorio corresponde a la ausencia del renderizador PDF personal: aquí está instalado, y ese estado de ausencia se cubre mediante `module=None`. Un control específico posterior de secretos y PDF dio 17 aprobadas y esa misma omisión.
