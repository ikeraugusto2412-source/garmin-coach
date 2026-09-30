# garmin-coach

Conecta tu cuenta de **Garmin Connect** con **Claude** para que analice tus actividades y métricas de salud
y te asesore como un entrenador personal.

```
Garmin Connect ──(garminconnect)──► garmin.db (SQLite local) ──(servidor MCP)──► Claude Desktop / Claude Code
```

- **Sincronización**: la primera vez descarga todo el histórico; después solo lo nuevo. Hace pausas entre
  peticiones, reintenta con backoff y, si Garmin responde 429, espera y retoma más tarde donde lo dejó.
- **Almacenamiento**: todo queda en `garmin.db`, en tu ordenador. Se guarda también el JSON crudo de cada respuesta.
- **Servidor MCP**: 9 herramientas y un prompt "entrenador" que Claude usa para consultar tus datos en formato compacto.
- **Modo entrenador**: [`COACH.md`](COACH.md) (metodología) + `perfil.md` (tus objetivos; cópialo de
  [`perfil.example.md`](perfil.example.md) y rellénalo, no se sube a GitHub).
- **Web para el móvil**: un panel con tus datos **cifrados** publicado en GitHub Pages, que se actualiza solo cada mañana.

---

## 1. Instalación

Requisitos: macOS/Linux con Python ≥ 3.12.

```bash
cd ~/Desktop/garmin-coach
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

> Versiones comprobadas: `garminconnect` 0.3.17 (ya **no** usa `garth`: la autenticación es nativa y los tokens
> se guardan en `~/.garminconnect/garmin_tokens.json`) y `mcp` 2.2.

## 2. Credenciales y perfil

```bash
cp .env.example .env
chmod 600 .env
open -e .env        # rellena GARMIN_EMAIL y GARMIN_PASSWORD
```

```bash
cp perfil.example.md perfil.md   # y rellénalo: objetivos, lesiones, disponibilidad
```

`.env`, `perfil.md`, los tokens, `garmin.db` y `site/` están en `.gitignore`: tus datos personales nunca se suben
al repositorio. La contraseña nunca está en el código.

## 3. Primer uso

```bash
# 1) Login (si tu cuenta tiene MFA, te pedirá el código por consola). Guarda los tokens en ~/.garminconnect
.venv/bin/python -m garmin_coach login

# 2) (Opcional) Sonda: qué datos devuelve tu reloj en los últimos 7 días → probe_output/
.venv/bin/python -m garmin_coach probe

# 3) Sincronización completa del histórico (tarda unos 20-30 min la primera vez)
.venv/bin/python -m garmin_coach sync --full

# 4) Comprobar el estado y las alertas
.venv/bin/python -m garmin_coach status
```

Después, para actualizar:

```bash
.venv/bin/python -m garmin_coach sync
```

(o pídele a Claude "sincroniza mis datos de Garmin", que usa la herramienta `sync_garmin`).

### Qué se sincroniza

| Datos | Tabla | Desde |
|---|---|---|
| Actividades (tipo, fecha, duración, distancia, ritmo, FC, desnivel, cadencia, potencia, dinámica de carrera, calorías, Training Effect, carga, VO2max, tiempo en zonas) | `activities` | Primera actividad |
| Vueltas/splits, zonas de FC y series de gym por actividad | `activity_laps`, `activity_hr_zones`, `strength_sets` | Primera actividad |
| Sueño (fases, puntuación), HRV, FC en reposo, Body Battery, estrés, pasos | `daily_health` | Primer reloj registrado |
| VO2max, Training Readiness, estado de entrenamiento, carga aguda/crónica, balance de carga, resistencia, subidas | `training_daily` | VO2max: todo; el resto día a día desde el reloj actual |
| Predicciones de carrera | `race_predictions` | Reloj actual |
| Umbral de lactato, FTP de carrera, zonas de FC | `thresholds` | Instantánea en cada sincronización |
| Peso / composición corporal | `body_composition` | Todo (si hay registros) |
| Récords personales | `personal_records` | — |
| JSON crudo de todo lo anterior | `raw_responses` | — |
| Estado y registro de la sincronización (errores de endpoints) | `sync_state`, `sync_log` | — |

Si un endpoint falla o tu reloj no lo soporta, se anota en `sync_log` y la sincronización sigue con el resto.

## 4. Registrar el servidor MCP

El servidor se lanza con `.venv/bin/python -m garmin_coach.server` (transporte stdio). Usa rutas absolutas:
sustituye `/RUTA/A/garmin-coach` por la ruta real del proyecto (la ves con `pwd` dentro de la carpeta).

### Claude Desktop

Edita `~/Library/Application Support/Claude/claude_desktop_config.json` (Claude → Ajustes → Desarrollador →
Editar configuración) y añade:

```json
{
  "mcpServers": {
    "garmin-coach": {
      "command": "/RUTA/A/garmin-coach/.venv/bin/python",
      "args": ["-m", "garmin_coach.server"]
    }
  }
}
```

Si ya tienes otros servidores en `mcpServers`, añade solo el bloque `"garmin-coach"`. Reinicia Claude Desktop.
El prompt **entrenador** aparece en el menú "+" → garmin-coach.

### Claude Code

```bash
claude mcp add garmin-coach --scope user -- /RUTA/A/garmin-coach/.venv/bin/python -m garmin_coach.server
```

Compruébalo con `claude mcp list`. El prompt está disponible como `/mcp__garmin-coach__entrenador`.

## 5. Web en el móvil (GitHub Pages cifrado)

Un panel pensado para el móvil con 5 pestañas:
- **Hoy**: cuenta atrás para tu carrera (objetivo frente a predicción), la sesión que toca hoy, estado de
  recuperación, carga y alertas.
- **Plan**: el plan semanal que te guarda el entrenador desde Claude, con cada sesión marcada como hecha, pendiente
  o no hecha (se cruza sola con tus actividades), y sus notas y recomendaciones.
- **Progreso**: volumen e intensidad por semana, predicción de carrera, VO2max, umbral, zonas y récords.
- **Actividades**: todas tus actividades con vueltas, zonas y series de gimnasio.
- **Salud**: sueño, HRV, FC en reposo, Body Battery, preparación y estrés.

Para llenar la pestaña Plan, pídeselo al entrenador en Claude: *"Plantéame la semana que viene y guárdala en mi web"*.

**Cómo protege tus datos.** GitHub Pages es público. Por eso la página no contiene ningún dato legible: lleva un
bloque cifrado con AES-256-GCM, con una clave derivada de tu contraseña (PBKDF2-SHA256, 600.000 iteraciones).
El navegador lo descifra en tu móvil cuando escribes la contraseña. Sin ella solo hay ruido. Además:
- `garmin.db`, `.env` y `perfil.md` nunca se suben; la sincronización se hace siempre en tu Mac.
- La rama `gh-pages` se reescribe en cada publicación (un único commit), así que no queda historial de versiones antiguas.
- No se publican coordenadas GPS, y la página pide a los buscadores que no la indexen.
- Usa una contraseña larga (mínimo 10 caracteres; mejor una frase de 4-5 palabras), distinta de la de Garmin.

### Puesta en marcha (una vez)

```bash
# 1) Contraseña de la web (se guarda en .env)
.venv/bin/python -m garmin_coach web-password

# 2) Datos de tu carrera en .env (para la cuenta atrás)
#    RACE_NAME=Media Maratón de Barcelona
#    RACE_DATE=2027-02-14
#    RACE_TARGET=1:59:59

# 3) Sube el proyecto a GitHub (repositorio público; el plan gratuito necesita repo público para Pages)
gh repo create garmin-coach --public --source . --remote origin
git add -A && git commit -m "garmin-coach" && git push -u origin main

# 4) Genera y publica la web (crea la rama gh-pages)
.venv/bin/python -m garmin_coach web --publish

# 5) Activa GitHub Pages sobre la rama gh-pages
gh api -X POST repos/{owner}/{repo}/pages -f "source[branch]=gh-pages" -f "source[path]=/"
#    (o en GitHub: Settings → Pages → Deploy from a branch → gh-pages / root)
```

La web queda en `https://TU_USUARIO.github.io/garmin-coach/`. En el móvil, ábrela, escribe la contraseña y marca
"Recordar en este dispositivo". Añádela a la pantalla de inicio (Safari: Compartir → Añadir a pantalla de inicio)
para abrirla como una app.

### Actualización automática cada mañana

```bash
.venv/bin/python -m garmin_coach schedule install --hora 8:00   # instalar (launchd, en tu Mac)
.venv/bin/python -m garmin_coach schedule status                # ver estado y últimas ejecuciones
.venv/bin/python -m garmin_coach schedule uninstall             # quitarla
```

Cada día a esa hora el Mac sincroniza con Garmin, regenera la web cifrada y la publica. Si el Mac está dormido,
se ejecuta al despertar. El registro queda en `logs/daily.log`. Si Garmin pide MFA de nuevo (sesión caducada),
el registro lo indica: ejecuta `python -m garmin_coach login` en una terminal.

Para actualizar a mano después de entrenar: `.venv/bin/python -m garmin_coach daily`.

## 6. Herramientas disponibles para Claude

| Herramienta | Qué hace |
|---|---|
| `sync_garmin` | Sincronización incremental |
| `get_profile` | Lee `perfil.md` + `COACH.md` |
| `get_activities(desde, hasta, tipo)` | Lista de actividades; `tipo` = `correr`, `gym`, `bici`, `snowboard` o typeKey de Garmin; fechas `YYYY-MM-DD` o `30d`, `8w`, `6m`, `1y` |
| `get_activity_detail(id)` | Detalle con vueltas, zonas y series de fuerza |
| `get_daily_health(desde, hasta)` | Sueño, HRV, FC en reposo, Body Battery, estrés, pasos, readiness |
| `get_training_status()` | VO2max, readiness, carga A:C, umbral, zonas, predicciones y **alertas** |
| `get_weekly_summary(semanas)` | Volumen, intensidad, gym, bici y recuperación por semana |
| `get_trends(metrica, periodo)` | Evolución de vo2max, hrv, fc_reposo, sueño, pred_media, km_carrera, ritmo_carrera… |
| `query_sql(consulta)` | SQL solo lectura (conexión `mode=ro` + autorizador SQLite que bloquea cualquier escritura) |
| `save_training_plan(titulo, sesiones, resumen, objetivo)` | Guarda el plan semanal que propone el entrenador (aparece en la pestaña **Plan** de la web) |
| `save_coach_note(titulo, contenido, tipo, fijar)` | Guarda una recomendación, análisis, aviso u objetivo |
| `get_training_plan(desde, hasta)` | Plan frente a lo realizado: cada sesión marcada como hecha, pendiente o no hecha |
| `delete_training_plan(id)`, `delete_coach_note(id)` | Borra un plan o una nota |
| `publish_web()` | Regenera y publica la web al momento |

## 7. Ejemplos de preguntas

- "¿Cómo va mi forma este mes?"
- "¿Estoy listo para un 10K a 4:30/km?"
- "Plantéame la semana que viene."
- "¿Mis zonas de FC están bien configuradas? Casi todas mis carreras salen en Z5."
- "Compara mi tirada larga de junio con la última."
- "¿Duermo peor los días que entreno por la tarde?"
- "¿Cuántos km debería hacer la próxima semana sin arriesgar una lesión?"

Para activar el modo entrenador completo, usa el prompt **entrenador** o empieza con "Como mi entrenador, …".

## 8. Tests

```bash
.venv/bin/python -m pytest
```

Los tests usan un cliente de Garmin simulado y una base de datos temporal: no hacen ninguna llamada a Garmin.

## 9. Problemas frecuentes

- **429 / "Too many requests"**: Garmin limita las peticiones. Espera 15-30 min y vuelve a ejecutar `sync`:
  continúa donde se quedó. Puedes subir `GARMIN_REQUEST_DELAY` en `.env` (p. ej. `2.0`).
- **"No hay sesión guardada" desde Claude**: ejecuta `python -m garmin_coach login` en una terminal
  (el servidor MCP nunca pide MFA).
- **Tokens antiguos de garth** (`oauth1_token.json`): ya no sirven; haz login de nuevo.
- **La web dice "Contraseña incorrecta"** tras cambiarla: vuelve a publicar (`web --publish`) y en el móvil pulsa
  "Olvidar la contraseña en este dispositivo".
- **La publicación falla con un error de git**: comprueba `gh auth status` y que `git push` funciona desde la terminal.
- **Datos que faltan**: revisa `SELECT * FROM sync_log WHERE status='error' ORDER BY ts DESC`.

## Estructura

```
garmin_coach/
  auth.py        login, MFA y tokens
  api.py         pausas, backoff 429 y tolerancia a fallos
  sync.py        sincronización completa/incremental reanudable
  transform.py   JSON de Garmin → filas
  db.py          esquema SQLite y utilidades
  queries.py     análisis (carga A:C, alertas, resúmenes, tendencias, SQL seguro)
  server.py      servidor MCP (herramientas + prompt "entrenador")
  coach.py       plan y notas del entrenador; cruce plan ↔ actividades
  web.py         web cifrada: datos, cifrado AES-GCM y publicación en gh-pages
  web_template.html  la página (descifrado en el navegador y gráficas SVG)
  schedule.py    tarea diaria de macOS (launchd)
  probe.py       sonda de endpoints
  __main__.py    CLI
tests/           tests con datos simulados
COACH.md         metodología del entrenador
perfil.example.md  plantilla del perfil (cópiala a perfil.md, que no se sube)
```
