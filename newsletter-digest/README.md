# newsletter-digest

Lee las newsletters de tu Gmail, las resume con Claude, las puntúa en dos ejes
(relevancia y accionabilidad) y te manda un digest por email cada 3 días.

> **Ubicación.** Este proyecto vive en el subdirectorio `newsletter-digest/`
> del repo `job_search`, no en la raíz: comparte repo con el pipeline Job
> Hunter pero no comparte nada de código. Todos los comandos de abajo se
> ejecutan desde dentro de esta carpeta. La única pieza que queda fuera es
> el workflow, en `.github/workflows/digest.yml`, porque GitHub Actions solo
> lee workflows de la raíz del repo.

## Arquitectura

```
IMAP (Gmail)
   ↓  fetch desde last_run
mailbox.py      parseo, HTML→texto, cabecera List-Unsubscribe
   ↓
classify.py     cascada: allow/deny → List-Unsubscribe → frecuencia+HTML → dudosos
   ↓
score.py        1 llamada a Claude por newsletter (paralelo), JSON estructurado
   ↓
digest.py       3 cubos: Leer entero / Resumen basta / Archivar → email HTML
   ↓
state.json      commiteado de vuelta al repo por el workflow
```

## Puesta en marcha

**1. Contraseña de aplicación de Google**

Requiere verificación en dos pasos activada. Genérala en la sección de
seguridad de tu cuenta Google. Son 16 caracteres. Guárdala como
`GMAIL_APP_PASSWORD`.

**2. Local**

```bash
cd newsletter-digest
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # rellena las 4 variables
set -a && source .env && set +a
```

**3. Bootstrap (una sola vez)**

```bash
python bootstrap.py
```

Genera `config/senders.yaml`. **Ábrelo y revísalo.** En la cabecera tienes la
lista de remitentes que no supo clasificar: muévelos a mano a `newsletters` o
a `transactional`. Este paso es el que determina la precisión del sistema.

**4. Primera ejecución manual**

```bash
python main.py --force
```

**5. Producción**

Sube el repo a GitHub (privado). Añade en Settings → Secrets → Actions:
`GMAIL_USER`, `GMAIL_APP_PASSWORD`, `ANTHROPIC_API_KEY`, `DIGEST_TO`.

## Cosas que debes verificar tú

- **El identificador de modelo** en `src/score.py` (`MODEL`). Los nombres de
  modelo cambian; confirma el vigente en docs.anthropic.com.
- **El cron es UTC.** `0 4 * * *` son las 06:00 en Cracovia en verano y las
  05:00 en invierno. GitHub no ajusta el cambio de hora.
- **Los cron de GitHub Actions no son puntuales.** Pueden retrasarse minutos
  u horas en horas de pico. Para un digest da igual, pero conviene saberlo.
- **Coste real.** Mídelo tras la primera semana en la consola de Anthropic.
  Con ~20 newsletters semanales debería ser marginal, pero no lo des por
  hecho sin verlo.

## Iteración

El fichero que tocarás casi siempre es `config/profile.yaml`. Si el digest te
está subiendo cosas que no te interesan, no toques el código: añade una
exclusión o concreta más el `detail` del dominio correspondiente.

Si te está descartando algo que sí querías, mira `state.json` y la sección
"Descartadas" del email: cada ítem lleva el `reason` del modelo, que te dice
exactamente por qué lo bajó.
