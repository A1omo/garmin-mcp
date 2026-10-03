# GARMIN-MCP

Servidor MCP remoto (Python, FastMCP) que conecta Claude con Garmin Connect. Lee actividades y
fisiología, calcula métricas derivadas (desacople aeróbico, deriva, HR recovery) y crea, agenda y
borra entrenamientos en el Forerunner 965. Corre en Render y se usa como conector personalizado
de claude.ai, así que funciona igual desde el celular que desde la web.

## Estructura

| Archivo | Rol |
|---|---|
| `garmin_mcp_server.py` | Todo el servidor: 41 herramientas y `/health` |
| `garmin_exercise_catalog.json` | Catálogo oficial de ejercicios de fuerza (FIT SDK de Garmin): 53 categorías, 1.846 ejercicios |
| `requirements.txt` | `fastmcp>=4.0.5,<4.1`, `garminconnect>=0.3.16,<0.4` |
| `Dockerfile` | Imagen `python:3.12-slim`; copia el servidor y el catálogo |
| `render.yaml` | Referencia del servicio en Render (variables, health check) |
| `garmin_login.py`, `garmin_login_colab.ipynb` | Generan `GARMIN_TOKENS` (solo si algún día hace falta) |
| `.gitignore` | Excluye `garmin_tokens.txt`, `.env`, cachés de Python y `.DS_Store` |

## Herramientas (41)

**Actividades**
`garmin_account_info` · `garmin_list_activities` · `garmin_get_last_activity` ·
`garmin_get_activities_by_date` · `garmin_get_activity_analysis` (resumen, splits, splits por
bloque, zonas de FC y potencia, clima opcional) · `garmin_compare_activities` ·
`garmin_get_progress_summary` · `garmin_get_personal_records`

**Métricas derivadas**
`garmin_get_activity_dynamics`: desacople Pa:HR y Pw:HR, deriva cardíaca y HR recovery. Se
calcula solo sobre los bloques activos del entrenamiento (excluye calentamiento y vuelta a la
calma) y reporta la ventana usada (`analysis_window`) y las columnas de Garmin leídas
(`series_keys`). El desacople solo es interpretable en rodajes continuos.

**Fisiología diaria**
`garmin_morning_check` (readiness, HRV, FC reposo, sueño, Body Battery) · `garmin_get_hrv` ·
`garmin_get_hrv_detail` · `garmin_get_sleep` · `garmin_get_stress` ·
`garmin_get_body_battery_events` · `garmin_get_daily_summary` · `garmin_get_altitude_vitals` ·
`garmin_get_body_composition` · `garmin_get_hydration` · `garmin_get_hr_curve`

**Tendencias**
`garmin_get_trend`: serie día a día con resumen de hrv, sleep, resting_hr, stress, vo2max, spo2,
respiration (máx. 31 días) o body_battery, intensity_minutes, hill_score, endurance_score (hasta
366 días). `garmin_get_readiness_range` para readiness.

**Intradía** (ventana horaria local)
`garmin_get_hr_intraday` (FC ~2 min) · `garmin_get_stress_intraday` (estrés ~3 min, excluye
muestras con movimiento)

**Estado y calibración**
`garmin_get_training_status` (estado, carga aguda/crónica y ACWR, balance de carga 4 semanas,
aclimatación, predicciones) · `garmin_get_training_calibration` (LTHR, ritmo y potencia de
umbral, VO2max, readiness: llamar SIEMPRE antes de crear un entrenamiento)

**Fuerza y natación**
`garmin_get_strength_session` · `garmin_get_swim_session` · `garmin_search_exercises` ·
`garmin_validate_exercise`

**Escritura** (confirmar con el usuario antes de borrar o desagendar)
`garmin_create_running_workout` · `garmin_create_strength_workout` ·
`garmin_create_swim_workout` · `garmin_inspect_workout_schema` · `garmin_delete_workout` ·
`garmin_delete_workouts` · `garmin_get_calendar` · `garmin_schedule_workout` ·
`garmin_unschedule_workout` · `garmin_get_gear` · `garmin_set_activity_gear`

## Despliegue (Render)

1. Web Service, runtime Docker, rama `main`. Cada commit redespliega solo.
2. Variables de entorno:

| Variable | Valor |
|---|---|
| `GARMIN_TOKENS` | String de `garmin_login.py` |
| `GARMIN_EMAIL`, `GARMIN_PASSWORD` | Respaldo: si el token falla, el servidor re-autentica solo (la cuenta no tiene MFA) |
| `MCP_PATH` | `/mcp/<secreto>`; generar con `openssl rand -hex 24` |
| `LOCAL_TZ_OFFSET_HOURS` | `-5` (Bogotá) |
| `STRENGTH_SPORT_TYPE_ID` | Opcional; por defecto `5` |

3. Keep-alive: el plan Free apaga el servicio tras 15 min sin tráfico. Un monitor HTTP(s) de
   UptimeRobot a `<URL>/health` cada 5 min lo mantiene despierto. No usar GitHub Actions: en un
   repo privado agota los minutos gratis.

## Conector en claude.ai

Ajustes → Conectores → Agregar conector personalizado (solo desde la web), con la URL completa
`<URL de Render><MCP_PATH>`. Tras un deploy que agrega herramientas o cambia sus descripciones,
actualizar el conector; si no aparecen, abrir un chat nuevo.

## Convenciones útiles

- Nombres de ejercicio: buscarlos siempre con `garmin_search_exercises`; los inválidos se
  rechazan antes de subir (Garmin los borraría en silencio).
- Ejercicios unilaterales: un paso por pierna (series × 2), empezando por la izquierda; si no, el
  reloj suma las repeticiones.
- Nombres de entrenamiento de máximo 30 caracteres (el reloj corta el resto).
- Mover una sesión: `garmin_unschedule_workout` (usa `scheduled_id`) + `garmin_schedule_workout`
  (usa `workout_id`). No borrar ni recrear la plantilla.
- El calendario de Garmin entrega duración en ms y distancia en cm (el servidor ya convierte).

## Seguridad

Nunca al repo, al chat ni a servicios de terceros: tokens, contraseña, ni la URL con el secreto
de `MCP_PATH`. A servicios externos solo `/health`.

## Mantenimiento

- Si Garmin cambia el login y la autenticación falla: subir la versión de `garminconnect` dentro
  de 0.3.x (o ampliar el rango tras probarlo) y redesplegar.
- Error 429: esperar unos minutos; evitar ráfagas de consultas.

## Probar en local

```bash
pip install -r requirements.txt
export GARMIN_TOKENS="..."          # o GARMIN_EMAIL / GARMIN_PASSWORD
export MCP_PATH="/mcp/dev" PORT=8000
python garmin_mcp_server.py         # MCP en http://127.0.0.1:8000/mcp/dev
```

Sin Garmin: importar el módulo, reemplazar `garmin_mcp_server._call` por un simulador y usar
`fastmcp.Client(garmin_mcp_server.mcp)` en memoria.
