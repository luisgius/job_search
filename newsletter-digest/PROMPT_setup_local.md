# Prompt para crear el proyecto en local

> Cópialo entero. Pensado para Claude Code lanzado dentro de la carpeta vacía
> donde quieres el repo. Si tienes los ficheros descargados, arrástralos a la
> sesión antes de enviar el prompt.

---

Vas a crear un proyecto Python en el directorio actual. Se llama
`newsletter-digest`: lee las newsletters de mi Gmail por IMAP, las resume y
puntúa con la API de Claude, y me manda un digest por email cada 3 días,
ejecutado por GitHub Actions.

**Si te he adjuntado ficheros del proyecto, son la fuente de verdad: úsalos
tal cual y no reescribas la lógica.** Si no hay ficheros adjuntos, constrúyelo
desde la especificación de abajo.

## Estructura a crear

```
.
├── .github/workflows/digest.yml
├── config/profile.yaml
├── src/
│   ├── __init__.py
│   ├── mailbox.py
│   ├── classify.py
│   ├── score.py
│   └── digest.py
├── main.py
├── bootstrap.py
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Especificación funcional

**mailbox.py** — Conexión IMAP a `imap.gmail.com:993` con usuario y app
password desde `GMAIL_USER` / `GMAIL_APP_PASSWORD`. Descarga de INBOX en modo
readonly usando el criterio `SINCE`. Parsea cada mensaje a un dataclass `Mail`
con: uid, remitente (nombre y dirección), asunto, fecha, cuerpo en texto plano,
booleano `has_list_unsubscribe` y `html_ratio`. Decodifica cabeceras MIME.
Para el cuerpo prefiere `text/plain`; si es residual (<400 chars) o no existe,
convierte el `text/html` a texto con BeautifulSoup + lxml, eliminando script,
style, head y title. Normaliza saltos de línea múltiples.

**classify.py** — Cascada de tres capas sobre la lista de correos:
1. Descarta cuerpos de menos de 300 caracteres. Aplica denylist de remitentes
   (transaccionales) y allowlist, ambas cargadas de `config/senders.yaml` y
   comprobadas tanto por dirección completa como por dominio.
2. Si tiene cabecera `List-Unsubscribe` o `List-Id` (RFC 2369), es newsletter.
   Esta es la señal fuerte del sistema.
3. Si el remitente aparece 3+ veces en el histórico de frecuencia y su
   `html_ratio` supera 0.6, es newsletter.

Lo que no encaja pero tiene 2+ apariciones o ratio alto de HTML se devuelve
como "dudoso" para que lo resuelva el modelo. Devuelve la tupla
`(newsletters, dudosos)`.

**score.py** — Una llamada a la API de Claude por newsletter, en paralelo con
`ThreadPoolExecutor` (4 workers). System prompt que inyecta el perfil de
intereses y exige devolver **solo JSON**, sin markdown, con este esquema:
`is_newsletter` (bool), `relevance` (0-5), `actionability` (0-5), `summary`
(2-4 frases en español), `key_takeaway`, `action`, `reason`. Trunca el cuerpo
a 14.000 caracteres. Parseo defensivo del JSON: quita fences de markdown y,
si falla, busca el primer objeto `{...}` por regex. Un fallo en un correo
devuelve un score de 0 con flag de error, nunca tumba la ejecución. Función
`bucket()` que asigna cada correo a `read_full`, `skim` o `archive` según los
umbrales del perfil.

**digest.py** — Renderiza HTML y texto plano, ordenando por la suma de los dos
ejes descendente. Tres secciones: "Leer entero", "Resumen basta" y una lista
compacta de "Descartadas" con el `reason` de cada una. Envía por SMTP SSL vía
`smtp.gmail.com:465` con las mismas credenciales. **Mantén el sink desacoplado
en su propia función** `deliver_email(subject, html, text)`, porque más
adelante migraré el destino a Notion.

**main.py** — Orquestación. Lee `state.json` con el `last_run`. Si han pasado
menos de 3 días y no se pasó `--force`, sale sin hacer nada. Ventana de
búsqueda desde `last_run`, con tope de seguridad de 14 días por si el job
estuvo caído. Como IMAP `SINCE` tiene granularidad de día, filtra después por
timestamp exacto para no duplicar. Clasifica, puntúa, agrupa en cubos, y si
ninguno supera el umbral no envía email pero **sí avanza el estado**. Logging
informativo en cada fase. Guarda `state.json` al final.

**bootstrap.py** — Script de ejecución única. Escanea 90 días, agrupa por
remitente, y genera `config/senders.yaml` proponiendo tres grupos: allowlist
(ratio de `List-Unsubscribe` ≥ 0.8 y la dirección no contiene patrones como
noreply, notifications, billing, security, invoice, receipt, support, alert),
denylist (los que sí contienen esos patrones), y un bloque de **pendientes de
revisar** escrito como comentarios en la cabecera del YAML, con el conteo y el
nombre de cada remitente. Incluye también un diccionario `frequency` con los
conteos para la capa 2. Imprime un resumen por consola.

**config/profile.yaml** — Perfil de intereses con cuatro dominios (Data
Science / ML / forecasting, IA aplicada y tooling de dev, inversión y finanzas
personales, endurance y triatlón), cada uno con `name`, `weight` y un `detail`
descriptivo. Sección `exclusions` y sección `scoring` con los umbrales:
`read_full` requiere relevancia ≥4 y accionabilidad ≥3, `skim` requiere
relevancia ≥3, y flag `skip_send_if_empty: true`.

**.github/workflows/digest.yml** — Cron diario `0 4 * * *` (la cadencia real
de 3 días la decide el script, no el cron, porque `*/3` en día del mes se
rompe en los cambios de mes). Añade `workflow_dispatch` con input booleano
`force`. Python 3.12 con caché de pip. Secrets: `GMAIL_USER`,
`GMAIL_APP_PASSWORD`, `ANTHROPIC_API_KEY`, `DIGEST_TO`. Paso final que
commitea `state.json` de vuelta al repo solo si cambió, con `[skip ci]` en el
mensaje y `permissions: contents: write`.

**requirements.txt** — anthropic, beautifulsoup4, lxml, PyYAML.

## Tareas de setup que quiero que hagas

1. Crea todos los ficheros.
2. `git init` y primer commit. El `.gitignore` debe cubrir `.env`,
   `__pycache__/` y `*.pyc`.
3. Crea el venv en `.venv` e instala las dependencias.
4. Copia `.env.example` a `.env` con los valores como placeholders.
5. Verifica que todo compila con `python -m py_compile` y que los YAML parsean.

## Restricciones

- **No ejecutes `bootstrap.py` ni `main.py`.** Necesitan credenciales reales
  que aún no he configurado. Déjame a mí el primer run.
- **No inventes valores en `.env`.** Placeholders explícitos.
- No uses la Gmail API con OAuth: he elegido IMAP con app password a
  propósito, es más simple y estable para este caso.
- El identificador de modelo en `score.py` déjalo como constante `MODEL` en lo
  alto del fichero y **avísame en tu resumen final de que debo verificarlo**
  en docs.anthropic.com, porque los nombres de modelo cambian.
- No hagas `git push` ni crees repo remoto sin preguntarme antes.

Al terminar, dime en tres líneas qué debo hacer yo a continuación.
