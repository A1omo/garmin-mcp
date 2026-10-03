#!/usr/bin/env python3
"""
garmin_mcp — Servidor MCP remoto (Streamable HTTP) que conecta Claude con Garmin Connect,
para análisis de entrenamiento y fisiología y gestión de entrenamientos desde el celular.

Un solo archivo de código + garmin_exercise_catalog.json. Secciones, en orden:
  1. Configuración y cliente Garmin (singleton con re-login automático en _call).
  2. Utilidades y resúmenes compactos (_slim_activity, _slim_training_status, ...).
  3. Catálogo oficial de ejercicios de fuerza (FIT SDK) y validaciones.
  4. Herramientas de lectura: actividades, fisiología diaria, dinámica (desacople,
     deriva, HRR), intradía, fuerza, natación, calibración.
  5. Herramientas de escritura: crear entrenamientos de carrera, fuerza y natación.
  6. Gestión: borrar entrenamientos, buscar/validar ejercicios.
  7. Equipo, récords, tendencias (garmin_get_trend) y eventos de Body Battery.
  8. Calendario: ver, agendar y desagendar.
  9. /health (keep-alive) y arranque.

Autenticación a Garmin:
  - GARMIN_TOKENS (string de garmin_login.py) o GARMIN_EMAIL + GARMIN_PASSWORD (sin MFA).
    Si el token caduca, _call re-autentica con credenciales y reintenta una vez.
Autenticación Claude -> servidor:
  - Secreto embebido en la ruta (MCP_PATH = /mcp/<secreto>). La URL completa es la
    credencial: solo HTTPS, nunca en el repo ni en servicios de terceros.
"""

from __future__ import annotations

import difflib
import json
import os
import threading
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.workout import (
    RunningWorkout,
    SwimmingWorkout,
    WorkoutSegment,
    create_warmup_step,
    create_interval_step,
    create_recovery_step,
    create_cooldown_step,
    create_repeat_group,
)

# --------------------------------------------------------------------------- #
# Configuración (variables de entorno)
# --------------------------------------------------------------------------- #
GARMIN_TOKENS = os.environ.get("GARMIN_TOKENS")          # string de client.dumps()
GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GARMIN_MFA = os.environ.get("GARMIN_MFA")                # solo para el primer arranque
GARMIN_IS_CN = os.environ.get("GARMIN_IS_CN", "false").lower() == "true"

# Desfase horario local para fechas por defecto y la curva de FC (Bogotá = -5).
TZ_OFFSET = int(os.environ.get("LOCAL_TZ_OFFSET_HOURS", "-5"))

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")            # pon /mcp/<secreto> en producción

mcp = FastMCP(
    "garmin_mcp",
    instructions=(
        "Acceso a Garmin Connect para análisis de entrenamiento "
        "(actividades, splits, zonas de FC y potencia, dinámica/decoupling) y de "
        "variables fisiológicas (training readiness, HRV, estrés, Body Battery, sueño, "
        "estado de entrenamiento, VO2max, umbral de lactato, SpO2, composición corporal, "
        "hidratación). Lee además sesiones de FUERZA (ejercicios, series, repeticiones, "
        "carga y volumen) y de NATACIÓN (largos, estilo, SWOLF, ritmo por 100 m). "
        "También puede crear y agendar entrenamientos de carrera, fuerza y natación en el reloj. "
        "Las fechas usan formato ISO 'YYYY-MM-DD'; si se omiten, se asume hoy.\n\n"
        "REGLA IMPORTANTE: antes de crear o agendar cualquier entrenamiento "
        "(garmin_create_running_workout), llama primero a garmin_get_training_calibration "
        "y fija los objetivos (techos/zonas de FC, ritmos) con esos valores ACTUALES "
        "—umbral de lactato y VO2max vigentes—, nunca con cifras asumidas o de memoria.\n\n"
        "Antes de crear un entrenamiento de fuerza, busca los nombres exactos con "
        "garmin_search_exercises: los nombres inválidos se rechazan. Al corregir un "
        "entrenamiento ya subido, borra el anterior con garmin_delete_workout(s) en vez "
        "de dejar duplicados (confirma con el usuario antes de borrar).\n\n"
        "Para trayectorias de varios días (HRV, FC en reposo, sueño, estrés, VO2max, "
        "SpO2, Body Battery, minutos de intensidad) usa garmin_get_trend en vez de "
        "llamar una herramienta diaria repetidas veces. Para MOVER una sesión agendada usa "
        "garmin_unschedule_workout + garmin_schedule_workout (no borres ni recrees la plantilla)."
    ),
)

# --------------------------------------------------------------------------- #
# Cliente Garmin (singleton perezoso con re-autenticación)
# --------------------------------------------------------------------------- #
_client: Optional[Garmin] = None
_lock = threading.Lock()


def _build_client(force_credentials: bool = False) -> Garmin:
    """Crea y autentica un cliente Garmin. Prioriza el token; si falla o se fuerza,
    inicia sesión con credenciales (auto-recuperación, requiere cuenta sin MFA)."""
    def _login_with_credentials():
        prompt = (lambda: GARMIN_MFA) if GARMIN_MFA else None
        g = Garmin(
            email=GARMIN_EMAIL,
            password=GARMIN_PASSWORD,
            is_cn=GARMIN_IS_CN,
            prompt_mfa=prompt,
        )
        g.login()
        return g

    # 1) Camino rápido: resumir sesión desde el string de tokens (si no se fuerza login).
    if not force_credentials and GARMIN_TOKENS:
        g = Garmin(is_cn=GARMIN_IS_CN)
        # login() trata el argumento como DATA si len > 512; si no, como ruta.
        g.login(GARMIN_TOKENS)
        return g

    # 2) Login con credenciales (sin MFA): es la red de seguridad ante token inválido.
    if GARMIN_EMAIL and GARMIN_PASSWORD:
        return _login_with_credentials()

    # 3) Si se forzó pero no hay credenciales, intentar el token como último recurso.
    if GARMIN_TOKENS:
        g = Garmin(is_cn=GARMIN_IS_CN)
        g.login(GARMIN_TOKENS)
        return g

    raise GarminConnectAuthenticationError(
        "Faltan credenciales: define GARMIN_TOKENS (recomendado) o "
        "GARMIN_EMAIL + GARMIN_PASSWORD en el entorno del servidor."
    )


def _get_client() -> Garmin:
    global _client
    with _lock:
        if _client is None:
            _client = _build_client()
        return _client


def _reset_client() -> None:
    global _client
    with _lock:
        _client = None


def _call(method: str, *args, **kwargs) -> Any:
    """Llama un método del cliente. Si la auth falla (p. ej. token caducado o
    invalidado por un cambio de contraseña), reconstruye forzando login con
    credenciales —auto-recuperación— y reintenta una vez."""
    try:
        return getattr(_get_client(), method)(*args, **kwargs)
    except GarminConnectAuthenticationError:
        global _client
        with _lock:
            _client = _build_client(force_credentials=True)
        return getattr(_client, method)(*args, **kwargs)


def _err(e: Exception) -> dict:
    """Mensajes de error accionables."""
    if isinstance(e, ValueError):
        return {"error": str(e)}
    if isinstance(e, GarminConnectAuthenticationError):
        return {"error": "Autenticación con Garmin fallida incluso tras intentar "
                          "re-login. Verifica GARMIN_EMAIL/GARMIN_PASSWORD (o regenera "
                          "GARMIN_TOKENS) en las variables de entorno del servidor.",
                "detail": str(e)}
    if isinstance(e, GarminConnectTooManyRequestsError):
        return {"error": "Garmin respondió 429 (demasiadas solicitudes). Espera unos "
                          "minutos antes de reintentar.", "detail": str(e)}
    if isinstance(e, GarminConnectConnectionError):
        return {"error": "Error de conexión con Garmin Connect. Reintenta más tarde.",
                "detail": str(e)}
    return {"error": f"Error inesperado: {type(e).__name__}", "detail": str(e)}


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _today() -> str:
    # Fecha local del usuario (evita adelantarse de día por el UTC del servidor).
    return (datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET)).date().isoformat()


def _d(value: Optional[str]) -> str:
    """Normaliza una fecha a ISO 'YYYY-MM-DD' (hoy si es None)."""
    if not value:
        return _today()
    return datetime.fromisoformat(value).date().isoformat()


# Claves voluminosas (series por punto / polilíneas GPS) que conviene descartar.
_BULKY_KEYS = {
    "metricDescriptors", "activityDetailMetrics", "geoPolylineDTO",
    "polyline", "samples", "detailsAvailable", "metrics",
}


def _strip(obj: Any) -> Any:
    """Quita recursivamente claves voluminosas para no inflar el contexto."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in _BULKY_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _pace(speed_mps: Optional[float]) -> Optional[str]:
    """Convierte velocidad (m/s) a ritmo min/km 'M:SS'."""
    if not speed_mps:
        return None
    sec = 1000.0 / speed_mps
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _lthr_pace(speed: Optional[float]) -> Optional[str]:
    """Ritmo de umbral a partir del campo 'speed' de Garmin, que a veces viene escalado
    (valores ~0.37 en vez de ~3.7 m/s). Se autocorrige validando que el ritmo sea
    plausible para correr (2:00–20:00/km)."""
    if not speed:
        return None
    sec = 1000.0 / float(speed)
    if sec > 1200:  # >20:00/km imposible para umbral -> el valor venía /10
        sec = 1000.0 / (float(speed) * 10)
    if sec < 120 or sec > 1200:
        return None
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _slim_activity_raw(a: dict) -> dict:
    """Resumen compacto de una actividad. Acepta tanto el formato plano de la lista de
    actividades como el de get_activity, que anida los datos en 'summaryDTO'."""
    src = {**a, **(a.get("summaryDTO") or {})}
    if not src.get("activityType") and src.get("activityTypeDTO"):
        src["activityType"] = src["activityTypeDTO"]

    def g(*keys, default=None):
        for k in keys:
            if src.get(k) is not None:
                return src[k]
        return default

    return {
        "activity_id": g("activityId"),
        "name": g("activityName"),
        "type": (g("activityType") or {}).get("typeKey"),
        "start_local": g("startTimeLocal"),
        "distance_km": round(g("distance") / 1000.0, 2) if g("distance") else None,
        "duration_min": round(g("duration") / 60.0, 1) if g("duration") else None,
        "avg_pace_min_km": _pace(g("averageSpeed")),
        "avg_hr": g("averageHR"),
        "max_hr": g("maxHR"),
        "avg_cadence_spm": g("averageRunningCadenceInStepsPerMinute", "averageRunCadence"),
        "elev_gain_m": g("elevationGain"),
        "avg_power_w": g("avgPower", "averagePower"),
        "norm_power_w": g("normPower", "normalizedPower"),
        "aerobic_te": g("aerobicTrainingEffect", "trainingEffect"),
        "anaerobic_te": g("anaerobicTrainingEffect"),
        "vo2max": g("vO2MaxValue"),
        "calories": g("calories"),
        # Biomecánica (cuando está disponible)
        "ground_contact_ms": g("avgGroundContactTime", "groundContactTime"),
        "vertical_osc_cm": g("avgVerticalOscillation", "verticalOscillation"),
        "vertical_ratio_pct": g("avgVerticalRatio", "verticalRatio"),
        "stride_length_cm": g("avgStrideLength", "strideLength"),
    }


# --------------------------------------------------------------------------- #
# Catálogo oficial de ejercicios (FIT SDK de Garmin) y validaciones
# --------------------------------------------------------------------------- #
_CATALOG_PATH = Path(__file__).with_name("garmin_exercise_catalog.json")
CATALOG: dict[str, set[str]] = {
    k: set(v) for k, v in json.loads(_CATALOG_PATH.read_text()).items()
}
_ALL_NAMES: dict[str, str] = {n: c for c, names in CATALOG.items() for n in names}
WATCH_NAME_LIMIT = 30   # el reloj corta los nombres de entrenamiento a 30 caracteres


def _norm(s: Optional[str]) -> str:
    """El buscador de Garmin ignora mayúsculas, espacios y guiones: replicamos eso."""
    return (s or "").strip().upper().replace(" ", "_").replace("-", "_")


def validate_exercise(category: str, exercise: Optional[str]) -> Optional[str]:
    """None si es válido; si no, un mensaje con sugerencias concretas."""
    cat = _norm(category)
    if cat not in CATALOG:
        sug = difflib.get_close_matches(cat, CATALOG.keys(), n=3, cutoff=0.6)
        return f"Categoría '{category}' no existe. ¿Quisiste decir: {', '.join(sug) or 'ninguna parecida'}?"
    if not exercise:
        return None  # solo categoría: Garmin lo acepta (se verá genérico en el reloj)
    ex = _norm(exercise)
    if ex in CATALOG[cat]:
        return None
    if ex in _ALL_NAMES:
        return f"'{ex}' existe pero en la categoría {_ALL_NAMES[ex]}, no en {cat}."
    sug_cat = difflib.get_close_matches(ex, CATALOG[cat], n=4, cutoff=0.5)
    sug_all = [f"{n} ({_ALL_NAMES[n]})"
               for n in difflib.get_close_matches(ex, _ALL_NAMES.keys(), n=3, cutoff=0.6)
               if n not in sug_cat]
    sug = sug_cat + sug_all
    return (f"Ejercicio '{exercise}' no existe en {cat}; Garmin lo descartaría en silencio. "
            f"Sugerencias: {', '.join(sug) or 'ninguna parecida; usa solo la categoría'}.")


def check_workout_name(name: str) -> Optional[str]:
    if len(name) > WATCH_NAME_LIMIT:
        return (f"Aviso: el nombre tiene {len(name)} caracteres; el reloj mostrará solo "
                f"'{name[:WATCH_NAME_LIMIT]}'.")
    return None


def validate_strength_payload(name: str, exercises: list) -> Optional[dict]:
    """Valida TODO antes de subir. Si hay errores no se sube nada (evita workouts a medias)."""
    errors = []
    for i, e in enumerate(exercises, 1):
        msg = validate_exercise(e.get("category", ""), e.get("exercise"))
        if msg:
            errors.append({"posicion": i, "category": e.get("category"),
                           "exercise": e.get("exercise"), "error": msg})
    if errors:
        return {"error": "Nombres de ejercicio inválidos; no se subió nada.", "detalles": errors}
    return None


PW_HR_PLAUSIBLE = 30.0  # % ; más allá de ±30 no es fisiología, es sensor o columna errónea


def sanitize_pw_hr(value: Optional[float]) -> dict:
    """{'aerobic_decoupling_pw_hr_pct': valor|None, 'pw_hr_note': ...}."""
    if value is None:
        return {"aerobic_decoupling_pw_hr_pct": None, "pw_hr_note": "sin datos de potencia"}
    if abs(value) > PW_HR_PLAUSIBLE:
        return {"aerobic_decoupling_pw_hr_pct": None,
                "pw_hr_note": f"valor {value:.1f}% fuera de rango fisiológico; descartado, usa Pa:HR"}
    return {"aerobic_decoupling_pw_hr_pct": round(value, 1), "pw_hr_note": None}


def summary_from_splits(typed_splits: list) -> dict:
    """Reconstruye un resumen básico cuando get_activity devuelve todo null."""
    runs = [s for s in typed_splits if s.get("type", "").startswith("INTERVAL_")]
    if not runs:
        return {}
    dist = sum(s.get("distance", 0) or 0 for s in runs)
    dur = sum(s.get("duration", 0) or 0 for s in runs)
    hr_w = sum((s.get("averageHR") or 0) * (s.get("duration") or 0) for s in runs)
    pace = dur / (dist / 1000) if dist else None
    return {
        "distance_km": round(dist / 1000, 2),
        "duration_min": round(dur / 60, 1),
        "avg_hr": round(hr_w / dur) if dur else None,
        "max_hr": max((s.get("maxHR") or 0) for s in runs) or None,
        "avg_pace_min_km": f"{int(pace // 60)}:{int(pace % 60):02d}" if pace else None,
        "source": "reconstruido desde splits (summary original vino vacío)",
    }


def summary_is_empty(summary: dict) -> bool:
    return all(v is None for k, v in summary.items() if k not in ("activity_id", "name"))


_INT_KEYS = {"avg_hr", "max_hr", "avg_cadence_spm", "elev_gain_m", "avg_power_w",
             "norm_power_w", "calories", "ground_contact_ms", "stride_length_cm"}


def _slim_activity(a: dict) -> dict:
    """_slim_activity_raw + redondeo (Garmin entrega floats de 15 decimales que solo
    inflan el contexto)."""
    out = _slim_activity_raw(a)
    for k, v in out.items():
        if isinstance(v, float):
            out[k] = round(v) if k in _INT_KEYS else round(v, 2 if k == "distance_km" else 1)
    return out


def _hms(seconds: Optional[float]) -> Optional[str]:
    if not seconds:
        return None
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_race_predictions(rp: Any) -> Any:
    """Tiempos de predicción en h:mm:ss con ritmo, en vez de segundos crudos."""
    if not isinstance(rp, dict):
        return _strip(rp)
    dist = {"5K": ("time5K", 5.0), "10K": ("time10K", 10.0),
            "21K": ("timeHalfMarathon", 21.0975), "42K": ("timeMarathon", 42.195)}
    out = {"as_of": rp.get("calendarDate")}
    for label, (key, km) in dist.items():
        sec = rp.get(key)
        if sec:
            p = sec / km
            out[label] = {"time": _hms(sec), "pace": f"{int(p // 60)}:{int(p % 60):02d}/km"}
    return out


def _primary(dto_map: Optional[dict]) -> dict:
    """Entrada del dispositivo principal en los mapas {deviceId: {...}} de Garmin."""
    vals = [v for v in (dto_map or {}).values() if isinstance(v, dict)]
    for v in vals:
        if v.get("primaryTrainingDevice"):
            return v
    return vals[0] if vals else {}


def _slim_training_status(ts: Any) -> Any:
    """Resumen legible de get_training_status (el crudo trae ids, URLs de imágenes y
    duplicados por dispositivo)."""
    if not isinstance(ts, dict):
        return _strip(ts)
    st = _primary((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
    acwr = st.get("acuteTrainingLoadDTO") or {}
    lb = _primary((ts.get("mostRecentTrainingLoadBalance") or {})
                  .get("metricsTrainingLoadBalanceDTOMap"))
    vo2 = ts.get("mostRecentVO2Max") or {}
    acc = vo2.get("heatAltitudeAcclimation") or ts.get("heatAltitudeAcclimationDTO") or {}

    def _lvl(v):
        return round(v) if isinstance(v, (int, float)) else v

    return {
        "status": st.get("trainingStatusFeedbackPhrase"),
        "status_since": st.get("sinceDate"),
        "fitness_trend": st.get("fitnessTrend"),
        "training_paused": st.get("trainingPaused"),
        "load": {
            "acute": acwr.get("dailyTrainingLoadAcute"),
            "chronic": acwr.get("dailyTrainingLoadChronic"),
            "chronic_optimal_range": [_lvl(acwr.get("minTrainingLoadChronic")),
                                      _lvl(acwr.get("maxTrainingLoadChronic"))],
            "acwr": acwr.get("dailyAcuteChronicWorkloadRatio"),
            "acwr_status": acwr.get("acwrStatus"),
        },
        "load_balance_4w": {
            "feedback": lb.get("trainingBalanceFeedbackPhrase"),
            "aerobic_low": [_lvl(lb.get("monthlyLoadAerobicLow")),
                            lb.get("monthlyLoadAerobicLowTargetMin"),
                            lb.get("monthlyLoadAerobicLowTargetMax")],
            "aerobic_high": [_lvl(lb.get("monthlyLoadAerobicHigh")),
                             lb.get("monthlyLoadAerobicHighTargetMin"),
                             lb.get("monthlyLoadAerobicHighTargetMax")],
            "anaerobic": [_lvl(lb.get("monthlyLoadAnaerobic")),
                          lb.get("monthlyLoadAnaerobicTargetMin"),
                          lb.get("monthlyLoadAnaerobicTargetMax")],
            "format": "[valor, objetivo_min, objetivo_max]",
        } if lb else None,
        "vo2max_running": (vo2.get("generic") or {}).get("vo2MaxPreciseValue"),
        "acclimation": {
            "altitude_pct": acc.get("acclimationPercentage"),
            "altitude_m": acc.get("altitudeAcclimation"),
            "current_altitude_m": acc.get("currentAltitude"),
            "altitude_trend": acc.get("altitudeTrend"),
            "heat_pct": acc.get("heatAcclimationPercentage"),
            "heat_trend": acc.get("heatTrend"),
        } if acc else None,
    }


# --------------------------------------------------------------------------- #
# Herramientas — conexión
# --------------------------------------------------------------------------- #
@mcp.tool(
    name="garmin_account_info",
    annotations={"title": "Verificar conexión Garmin", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_account_info() -> dict:
    """Verifica que la autenticación con Garmin funciona y devuelve el nombre de la cuenta.

    Returns:
        dict: {"full_name": str} o {"error": str, "detail": str}.
    """
    try:
        return {"full_name": _call("get_full_name")}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Herramientas — actividades / entrenamiento
# --------------------------------------------------------------------------- #
@mcp.tool(
    name="garmin_list_activities",
    annotations={"title": "Listar actividades recientes", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_list_activities(limit: int = 5, activity_type: Optional[str] = None) -> dict:
    """Lista las actividades más recientes (resumen compacto con su activity_id).

    Args:
        limit: Número máximo de actividades a devolver (1-50). Por defecto 5.
        activity_type: Filtro opcional por tipo (p.ej. 'running', 'cycling').

    Returns:
        dict: {"count": int, "activities": [ {activity_id, name, type, start_local,
               distance_km, duration_min, avg_pace_min_km, avg_hr, ...}, ... ] }.
    """
    try:
        limit = max(1, min(int(limit), 50))
        data = _call("get_activities", 0, limit, activity_type)
        items = data if isinstance(data, list) else data.get("activityList", [])
        slim = [_slim_activity(a) for a in items]
        return {"count": len(slim), "activities": slim}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_last_activity",
    annotations={"title": "Última actividad", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_last_activity() -> dict:
    """Devuelve el resumen compacto de la actividad más reciente, incluido su activity_id
    (úsalo luego con garmin_get_activity_analysis).

    Returns:
        dict: resumen de la actividad o {"error": ...}.
    """
    try:
        a = _call("get_last_activity")
        if not a:
            return {"error": "No se encontraron actividades."}
        return _slim_activity(a)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_activity_analysis",
    annotations={"title": "Análisis completo de actividad", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_activity_analysis(activity_id: str, include_weather: bool = False) -> dict:
    """Análisis completo de una actividad en una sola llamada: resumen + splits estándar
    (vuelta a vuelta) + splits tipados (por bloque) + zonas de FC + zonas de potencia.

    Combina los datos que normalmente requieren varias llamadas, para minimizar
    interacciones (ideal desde el celular).

    Args:
        activity_id: ID de la actividad (de garmin_list_activities o get_last_activity).
        include_weather: Si True, incluye el clima de la sesión. Por defecto False.

    Returns:
        dict: {"activity_id", "summary", "splits", "typed_splits", "hr_zones",
               "power_zones", "weather"(opc.)}. Cada sub-bloque puede ser null si no
               está disponible, con su error indicado en "_notes".
    """
    aid = str(activity_id)
    out: dict[str, Any] = {"activity_id": aid, "_notes": {}}

    def _sub(key: str, method: str, *args):
        try:
            out[key] = _strip(_call(method, *args))
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out["_notes"][key] = f"{type(e).__name__}: {e}"

    try:
        full = _call("get_activity", aid)
        out["summary"] = _slim_activity(full) if isinstance(full, dict) else full
    except Exception as e:  # noqa: BLE001
        out["summary"] = None
        out["_notes"]["summary"] = f"{type(e).__name__}: {e}"

    _sub("splits", "get_activity_splits", aid)
    _sub("typed_splits", "get_activity_typed_splits", aid)
    _sub("hr_zones", "get_activity_hr_in_timezones", aid)
    _sub("power_zones", "get_activity_power_in_timezones", aid)
    if include_weather:
        _sub("weather", "get_activity_weather", aid)

    ts = (out.get("typed_splits") or {}).get("splits") if isinstance(out.get("typed_splits"), dict) else None
    if isinstance(out.get("summary"), dict) and summary_is_empty(out["summary"]) and ts:
        out["summary"].update(summary_from_splits(ts))

    if not out["_notes"]:
        out.pop("_notes")
    return out


# --------------------------------------------------------------------------- #
# Herramientas — fisiología / readiness
# --------------------------------------------------------------------------- #
@mcp.tool(
    name="garmin_morning_check",
    annotations={"title": "Chequeo matutino (go/no-go)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_morning_check(date: Optional[str] = None) -> dict:
    """Reúne en una sola llamada las señales de decisión go/no-go de la mañana:
    training readiness, HRV, FC en reposo, resumen de sueño y Body Battery.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "training_readiness", "hrv", "resting_hr", "sleep", "body_battery"}.
              Cada bloque puede ser null si Garmin aún no lo calculó.
    """
    cdate = _d(date)
    out: dict[str, Any] = {"date": cdate, "_notes": {}}

    def _sub(key: str, fn):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out["_notes"][key] = f"{type(e).__name__}: {e}"

    def _readiness():
        r = _call("get_training_readiness", cdate)
        return _strip(r[0]) if isinstance(r, list) and r else _strip(r)

    def _hrv():
        h = _call("get_hrv_data", cdate) or {}
        return _strip(h.get("hrvSummary", h))

    def _rhr():
        r = _call("get_rhr_day", cdate) or {}
        m = (((r.get("allMetrics") or {}).get("metricsMap") or {})
             .get("WELLNESS_RESTING_HEART_RATE") or [])
        return {"resting_hr": m[0].get("value") if m else r.get("restingHeartRate")}

    def _sleep():
        s = (_call("get_sleep_data", cdate) or {}).get("dailySleepDTO", {}) or {}
        return {
            "sleep_score": (s.get("sleepScores") or {}).get("overall", {}).get("value"),
            "duration_h": round(s.get("sleepTimeSeconds", 0) / 3600.0, 1)
            if s.get("sleepTimeSeconds") else None,
            "deep_h": round(s.get("deepSleepSeconds", 0) / 3600.0, 1)
            if s.get("deepSleepSeconds") else None,
            "rem_h": round(s.get("remSleepSeconds", 0) / 3600.0, 1)
            if s.get("remSleepSeconds") else None,
        }

    def _bb():
        bb = _call("get_body_battery", cdate, cdate)
        if isinstance(bb, list) and bb:
            levels = bb[0].get("bodyBatteryValuesArray") or []
            vals = [p[1] for p in levels if isinstance(p, list) and len(p) > 1]
            return {"latest": vals[-1] if vals else None,
                    "max": max(vals) if vals else None,
                    "min": min(vals) if vals else None}
        return None

    _sub("training_readiness", _readiness)
    _sub("hrv", _hrv)
    _sub("resting_hr", _rhr)
    _sub("sleep", _sleep)
    _sub("body_battery", _bb)

    if not out["_notes"]:
        out.pop("_notes")
    return out


@mcp.tool(
    name="garmin_get_readiness_range",
    annotations={"title": "Tendencia de readiness (rango)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_readiness_range(start_date: str, end_date: Optional[str] = None) -> dict:
    """Training readiness diario en un rango de fechas (para ver la trayectoria de carga
    de la semana antes de planificar sesiones).

    Args:
        start_date: Fecha inicial ISO 'YYYY-MM-DD'.
        end_date: Fecha final ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"start", "end", "days": [ {date, score, level, ...}, ... ] }.
    """
    try:
        start = _d(start_date)
        end = _d(end_date)
        d0 = datetime.fromisoformat(start).date()
        d1 = datetime.fromisoformat(end).date()
        if d1 < d0:
            return {"error": "end_date no puede ser anterior a start_date."}
        if (d1 - d0).days > 31:
            return {"error": "Rango demasiado amplio (máx. 31 días)."}

        days = []
        cur = d0
        while cur <= d1:
            iso = cur.isoformat()
            try:
                r = _call("get_training_readiness", iso)
                rec = r[0] if isinstance(r, list) and r else r
                if isinstance(rec, dict):
                    days.append({
                        "date": iso,
                        "score": rec.get("score"),
                        "level": rec.get("level"),
                        "feedback": rec.get("feedbackShort") or rec.get("feedbackLong"),
                        "sleep_score": rec.get("sleepScore"),
                        "recovery_time_h": rec.get("recoveryTime"),
                        "hrv_factor": rec.get("hrvFactorPercent"),
                    })
            except Exception:  # noqa: BLE001 — día sin dato; lo omitimos
                pass
            cur = cur.fromordinal(cur.toordinal() + 1)
        return {"start": start, "end": end, "count": len(days), "days": days}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_stress",
    annotations={"title": "Estrés diario y Body Battery", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_stress(date: Optional[str] = None) -> dict:
    """Estrés diario (promedio y reparto por zonas) y Body Battery del día.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "stress", "body_battery"}.
    """
    cdate = _d(date)
    out: dict[str, Any] = {"date": cdate, "_notes": {}}
    try:
        s = _call("get_stress_data", cdate) or {}
        out["stress"] = {
            "avg_stress_level": s.get("avgStressLevel"),
            "max_stress_level": s.get("maxStressLevel"),
            "rest_pct": s.get("restStressPercentage"),
            "low_pct": s.get("lowStressPercentage"),
            "medium_pct": s.get("mediumStressPercentage"),
            "high_pct": s.get("highStressPercentage"),
        }
    except Exception as e:  # noqa: BLE001
        out["stress"] = None
        out["_notes"]["stress"] = f"{type(e).__name__}: {e}"

    try:
        bb = _call("get_body_battery", cdate, cdate)
        if isinstance(bb, list) and bb:
            arr = bb[0].get("bodyBatteryValuesArray") or []
            vals = [p[1] for p in arr if isinstance(p, list) and len(p) > 1]
            out["body_battery"] = {
                "start": vals[0] if vals else None,
                "latest": vals[-1] if vals else None,
                "max": max(vals) if vals else None,
                "min": min(vals) if vals else None,
                "charged": bb[0].get("charged"),
                "drained": bb[0].get("drained"),
            }
        else:
            out["body_battery"] = None
    except Exception as e:  # noqa: BLE001
        out["body_battery"] = None
        out["_notes"]["body_battery"] = f"{type(e).__name__}: {e}"

    if not out["_notes"]:
        out.pop("_notes")
    return out


@mcp.tool(
    name="garmin_get_sleep",
    annotations={"title": "Detalle de sueño", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_sleep(date: Optional[str] = None) -> dict:
    """Detalle del sueño de una noche (fases, puntuación, HRV nocturno, respiración).

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: resumen de sueño o {"error": ...}.
    """
    cdate = _d(date)
    try:
        data = _call("get_sleep_data", cdate) or {}
        s = data.get("dailySleepDTO", {}) or {}
        scores = s.get("sleepScores", {}) or {}
        return {
            "date": cdate,
            "sleep_score": (scores.get("overall") or {}).get("value"),
            "qualifier": (scores.get("overall") or {}).get("qualifierKey"),
            "total_h": round(s.get("sleepTimeSeconds", 0) / 3600.0, 1)
            if s.get("sleepTimeSeconds") else None,
            "deep_h": round(s.get("deepSleepSeconds", 0) / 3600.0, 1)
            if s.get("deepSleepSeconds") else None,
            "light_h": round(s.get("lightSleepSeconds", 0) / 3600.0, 1)
            if s.get("lightSleepSeconds") else None,
            "rem_h": round(s.get("remSleepSeconds", 0) / 3600.0, 1)
            if s.get("remSleepSeconds") else None,
            "awake_h": round(s.get("awakeSleepSeconds", 0) / 3600.0, 1)
            if s.get("awakeSleepSeconds") else None,
            "avg_overnight_hrv": data.get("avgOvernightHrv"),
            "resting_hr": data.get("restingHeartRate"),
            "avg_respiration": data.get("avgSleepRespirationValue"),
            "body_battery_change": data.get("bodyBatteryChange"),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_training_status",
    annotations={"title": "Estado de entrenamiento y fitness", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_training_status(date: Optional[str] = None) -> dict:
    """Foto del estado de fitness: training status, carga aguda/crónica, VO2max,
    umbral de lactato (LTHR) y predicciones de carrera.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "training_status", "vo2max", "lactate_threshold", "race_predictions"}.
    """
    cdate = _d(date)
    out: dict[str, Any] = {"date": cdate, "_notes": {}}

    def _sub(key: str, fn):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out["_notes"][key] = f"{type(e).__name__}: {e}"

    _sub("training_status", lambda: _slim_training_status(_call("get_training_status", cdate)))

    def _vo2():
        m = _call("get_max_metrics", cdate)
        rec = m[0] if isinstance(m, list) and m else m
        gs = (rec or {}).get("generic", {}) if isinstance(rec, dict) else {}
        return {"vo2max_running": gs.get("vo2MaxPreciseValue") or gs.get("vo2MaxValue"),
                "fitness_age": (rec or {}).get("fitnessAge")}
    _sub("vo2max", _vo2)

    def _lt():
        lt = _call("get_lactate_threshold", latest=True) or {}
        return _strip(lt)
    _sub("lactate_threshold", _lt)

    _sub("race_predictions", lambda: _fmt_race_predictions(_call("get_race_predictions")))

    if not out["_notes"]:
        out.pop("_notes")
    return out


@mcp.tool(
    name="garmin_get_hrv",
    annotations={"title": "HRV detallado", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_hrv(date: Optional[str] = None) -> dict:
    """HRV de la noche: promedio, estado vs. baseline y rango de equilibrio.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: resumen de HRV o {"error": ...}.
    """
    cdate = _d(date)
    try:
        h = _call("get_hrv_data", cdate) or {}
        summ = h.get("hrvSummary", {}) or {}
        baseline = summ.get("baseline", {}) or {}
        return {
            "date": cdate,
            "last_night_avg": summ.get("lastNightAvg"),
            "last_night_5min_high": summ.get("lastNight5MinHigh"),
            "status": summ.get("status"),
            "baseline_low_upper": baseline.get("lowUpper"),
            "baseline_balanced_low": baseline.get("balancedLow"),
            "baseline_balanced_upper": baseline.get("balancedUpper"),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_hrv_detail",
    annotations={"title": "HRV intradía (lecturas de la noche)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_hrv_detail(date: Optional[str] = None) -> dict:
    """Serie de HRV de la noche: lecturas individuales (~cada 5 min durante el sueño) y
    promedios por hora. NOTA: Garmin mide la HRV durante el sueño, no hay registro 24 h
    continuo; lo más fino disponible es esta serie nocturna.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "last_night_avg", "status", "num_readings", "hourly_avg",
               "readings": [{"time": "HH:MM", "hrv": ms}, ...]}.
    """
    cdate = _d(date)
    try:
        h = _call("get_hrv_data", cdate) or {}
        summ = h.get("hrvSummary", {}) or {}
        raw = h.get("hrvReadings") or []
        readings, buckets = [], {}
        for r in raw:
            val = r.get("hrvValue", r.get("value"))
            t = (r.get("readingTimeLocal") or r.get("readingTimeGMT")
                 or r.get("readingTimeGmt"))
            if val is None or not t:
                continue
            # 'readingTimeLocal' ya viene en hora local; si solo hay GMT, ajusta el bucket
            is_local = bool(r.get("readingTimeLocal"))
            hhmm = t[11:16] if len(t) >= 16 else str(t)
            try:
                hour = int(t[11:13])
                if not is_local:
                    hour = (hour + TZ_OFFSET) % 24
            except (ValueError, IndexError):
                hour = None
            readings.append({"time": hhmm, "hrv": val})
            if hour is not None:
                buckets.setdefault(hour, []).append(val)
        hourly = [{"hour": hh, "avg": round(sum(v) / len(v))}
                  for hh, v in sorted(buckets.items())]
        return {
            "date": cdate,
            "last_night_avg": summ.get("lastNightAvg"),
            "status": summ.get("status"),
            "num_readings": len(readings),
            "hourly_avg": hourly,
            "readings": readings,
            "note": "Garmin mide HRV durante el sueño; sin registro 24 h continuo.",
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_daily_summary",
    annotations={"title": "Resumen diario", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_daily_summary(date: Optional[str] = None) -> dict:
    """Resumen diario de actividad y bienestar: pasos, calorías, minutos de intensidad,
    estrés promedio, FC en reposo y Body Battery.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: resumen diario o {"error": ...}.
    """
    cdate = _d(date)
    try:
        s = _call("get_user_summary", cdate) or {}
        return {
            "date": cdate,
            "total_steps": s.get("totalSteps"),
            "step_goal": s.get("dailyStepGoal"),
            "total_distance_m": s.get("totalDistanceMeters"),
            "active_kcal": s.get("activeKilocalories"),
            "total_kcal": s.get("totalKilocalories"),
            "floors_climbed": s.get("floorsAscended"),
            "intensity_min_moderate": s.get("moderateIntensityMinutes"),
            "intensity_min_vigorous": s.get("vigorousIntensityMinutes"),
            "avg_stress": s.get("averageStressLevel"),
            "max_stress": s.get("maxStressLevel"),
            "resting_hr": s.get("restingHeartRate"),
            "min_hr": s.get("minHeartRate"),
            "max_hr": s.get("maxHeartRate"),
            "body_battery_high": s.get("bodyBatteryHighestValue"),
            "body_battery_low": s.get("bodyBatteryLowestValue"),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Herramientas — volumen, comparación y altitud
# --------------------------------------------------------------------------- #
@mcp.tool(
    name="garmin_get_progress_summary",
    annotations={"title": "Volumen por periodo (semana/mes)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_progress_summary(start_date: str, end_date: Optional[str] = None,
                                metric: str = "distance") -> dict:
    """Resumen acumulado entre dos fechas (kilometraje, tiempo o desnivel). Útil para el
    volumen mensual/semanal que el Forerunner 965 no muestra en el reloj.

    Args:
        start_date: Fecha inicial ISO 'YYYY-MM-DD'.
        end_date: Fecha final ISO 'YYYY-MM-DD'. Por defecto hoy.
        metric: 'distance' (por defecto), 'duration' o 'elevationGain'.

    Returns:
        dict: {"start", "end", "metric", "summary"} o {"error": ...}.
    """
    try:
        start = _d(start_date)
        end = _d(end_date)
        data = _call("get_progress_summary_between_dates", start, end, metric)
        return {"start": start, "end": end, "metric": metric, "summary": _strip(data)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_activities_by_date",
    annotations={"title": "Actividades por rango de fechas", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_activities_by_date(start_date: str, end_date: Optional[str] = None,
                                  activity_type: Optional[str] = None) -> dict:
    """Lista las actividades dentro de un rango de fechas (p. ej. la semana de
    entrenamiento), con resumen compacto y su activity_id.

    Args:
        start_date: Fecha inicial ISO 'YYYY-MM-DD'.
        end_date: Fecha final ISO 'YYYY-MM-DD'. Por defecto hoy.
        activity_type: Filtro opcional (p. ej. 'running').

    Returns:
        dict: {"start", "end", "count", "activities": [...]} o {"error": ...}.
    """
    try:
        start = _d(start_date)
        end = _d(end_date)
        data = _call("get_activities_by_date", start, end, activity_type)
        items = data if isinstance(data, list) else []
        slim = [_slim_activity(a) for a in items]
        return {"start": start, "end": end, "count": len(slim), "activities": slim}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_compare_activities",
    annotations={"title": "Comparar actividades lado a lado", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_compare_activities(activity_ids: list[str]) -> dict:
    """Compara 2-4 actividades lado a lado en métricas clave (ritmo, FC, potencia,
    biomecánica, training effect). Ideal para contrastar una sesión contra tu carrera
    larga de referencia.

    Args:
        activity_ids: Lista de 2 a 4 activity_id a comparar.

    Returns:
        dict: {"count", "activities": [resumen_compacto, ...]} o {"error": ...}.
    """
    try:
        if not isinstance(activity_ids, list) or not (2 <= len(activity_ids) <= 4):
            return {"error": "Proporciona entre 2 y 4 activity_id."}
        rows = []
        for aid in activity_ids:
            try:
                a = _call("get_activity", str(aid))
                rows.append(_slim_activity(a) if isinstance(a, dict) else {"activity_id": aid, "error": "no data"})
            except Exception as e:  # noqa: BLE001
                rows.append({"activity_id": aid, "error": f"{type(e).__name__}: {e}"})
        return {"count": len(rows), "activities": rows}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_altitude_vitals",
    annotations={"title": "Vitales de altitud (SpO2 / respiración)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_altitude_vitals(date: Optional[str] = None) -> dict:
    """SpO2, frecuencia respiratoria y endurance score del día. Relevante para entrenar
    en altitud (Bogotá ~2.600 m): aclimatación, calidad de sueño y tolerancia aeróbica.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "spo2", "respiration", "endurance_score"}.
    """
    cdate = _d(date)
    out: dict[str, Any] = {"date": cdate, "_notes": {}}

    def _sub(key: str, fn):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out["_notes"][key] = f"{type(e).__name__}: {e}"

    def _spo2():
        s = _call("get_spo2_data", cdate) or {}
        return {
            "average_spo2": s.get("averageSpO2"),
            "lowest_spo2": s.get("lowestSpO2"),
            "latest_spo2": s.get("latestSpO2Value") or s.get("latestSpO2"),
            "resting_spo2": s.get("restingSpO2"),
        }

    def _resp():
        r = _call("get_respiration_data", cdate) or {}
        return {
            "avg_waking_brpm": r.get("avgWakingRespirationValue"),
            "lowest_brpm": r.get("lowestRespirationValue"),
            "highest_brpm": r.get("highestRespirationValue"),
            "avg_sleep_brpm": r.get("avgSleepRespirationValue"),
        }

    def _endurance():
        e = _call("get_endurance_score", cdate) or {}
        return {"overall_score": e.get("overallScore"),
                "classification": e.get("classification") or e.get("gauge", {}).get("classification")} \
            if isinstance(e, dict) else _strip(e)

    _sub("spo2", _spo2)
    _sub("respiration", _resp)
    _sub("endurance_score", _endurance)

    if not out["_notes"]:
        out.pop("_notes")
    return out


# --------------------------------------------------------------------------- #
# Herramientas — métricas avanzadas (derivadas)
# --------------------------------------------------------------------------- #
def _extract_series(details: dict):
    """Extrae columnas (FC, velocidad, potencia, tiempo) de get_activity_details.

    Prioriza las claves EXACTAS de Garmin (directHeartRate, directSpeed, directPower,
    directTimestamp, sumElapsedDuration). La búsqueda por subcadena es solo respaldo y
    excluye columnas que contienen la palabra pero no son la señal (p. ej.
    directVerticalSpeed, directGradeAdjustedSpeed, zonas de potencia).

    Returns:
        (hr, speed, power, t_rel_s, t_abs_s, keys) — t_abs_s en epoch UTC si hay
        timestamp; keys = claves usadas (diagnóstico)."""
    descs = details.get("metricDescriptors") or []
    rows = details.get("activityDetailMetrics") or []
    idx: dict[str, int] = {}
    orig: dict[str, str] = {}
    for d in descs:
        key = (d.get("key") or "")
        mi = d.get("metricsIndex")
        if key and mi is not None:
            idx[key.lower()] = mi
            orig[key.lower()] = key

    used: dict[str, Optional[str]] = {}

    def find(label, exact, subs=(), exclude=()):
        for k in exact:
            if k in idx:
                used[label] = orig[k]
                return idx[k]
        for k, v in idx.items():
            if any(s in k for s in subs) and not any(x in k for x in exclude):
                used[label] = orig[k]
                return v
        used[label] = None
        return None

    def col(i):
        if i is None:
            return None
        out = []
        for r in rows:
            m = r.get("metrics") or []
            out.append(m[i] if i < len(m) else None)
        return out

    hr = col(find("hr", ("directheartrate",), ("heartrate",), ("zone",)))
    speed = col(find("speed", ("directspeed", "directenhancedspeed"), ("speed",),
                     ("vertical", "gradeadjusted", "zone", "max")))
    power = col(find("power", ("directpower",), ("power",),
                     ("zone", "air", "max", "avg", "normalized", "threshold")))
    ts = col(find("timestamp", ("directtimestamp",), ("timestamp",)))
    t_abs = [v / 1000.0 if v else None for v in ts] if ts and any(ts) else None
    t = col(find("time", ("sumelapsedduration", "sumduration"), ("duration",), ("moving",)))
    if t is None and t_abs:
        base = next(v for v in t_abs if v)
        t = [(v - base) if v is not None else None for v in t_abs]
    return hr, speed, power, t, t_abs, used


def _decoupling(output, hr):
    pairs = [(o, h) for o, h in zip(output or [], hr or []) if o not in (None, 0) and h]
    if len(pairs) < 20:
        return None
    half = len(pairs) // 2

    def ef(seg):
        hs = [h for _, h in seg]
        os = [o for o, _ in seg]
        mh = sum(hs) / len(hs)
        return (sum(os) / len(os)) / mh if mh else None

    ef1, ef2 = ef(pairs[:half]), ef(pairs[half:])
    return round((ef1 - ef2) / ef1 * 100, 1) if ef1 else None


def _drift(hr):
    hs = [h for h in (hr or []) if h]
    if len(hs) < 20:
        return None
    half = len(hs) // 2
    return round(sum(hs[half:]) / len(hs[half:]) - sum(hs[:half]) / len(hs[:half]), 1)


def _hr_recovery(hr, t):
    """Mayor caída de FC sostenida en una ventana de 60 s (proxy de HR recovery)."""
    if not t:
        return None
    pairs = [(tt, h) for tt, h in zip(t, hr or []) if tt is not None and h]
    if len(pairs) < 10:
        return None
    n, best, k = len(pairs), 0, 0
    for i in range(n):
        ti, hi = pairs[i]
        if k < i:
            k = i
        while k < n and pairs[k][0] < ti + 60:
            k += 1
        if k < n:
            best = max(best, hi - pairs[k][1])
    return round(best) if best > 0 else None


def _gmt_s(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    return datetime.fromisoformat(ts.split(".")[0]).replace(tzinfo=timezone.utc).timestamp()


def _hr_recovery_anchored(hr, t, aid: str, splits: Optional[list] = None):
    """Caída de FC en los 60 s siguientes al FINAL del último bloque activo del
    entrenamiento (no la mayor caída de toda la sesión). Así un remate rápido o
    una vuelta a la calma trotando no la distorsionan. Devuelve (valor, nota)."""
    if splits is None:
        try:
            splits = (_call("get_activity_typed_splits", aid) or {}).get("splits") or []
        except Exception:  # noqa: BLE001
            splits = []
    starts = [_gmt_s(s.get("startTimeGMT")) for s in splits if s.get("startTimeGMT")]
    ends = [_gmt_s(s.get("endTimeGMT")) for s in splits
            if s.get("type") == "INTERVAL_ACTIVE" and s.get("endTimeGMT")]
    if t and starts and ends:
        off = max(ends) - min(starts)
        pairs = [(tt, h) for tt, h in zip(t, hr or []) if tt is not None and h]
        at_end = [h for tt, h in pairs if off - 5 <= tt <= off]
        after = [h for tt, h in pairs if off + 55 <= tt <= off + 65]
        if at_end and after:
            return (round(max(at_end) - sum(after) / len(after)),
                    "medida desde el final del último bloque activo")
    legacy = _hr_recovery(hr, t)
    return (legacy, "proxy: mayor caída en 60 s de toda la sesión (sin bloques "
                    "estructurados); un remate rápido la infla" if legacy else None)


@mcp.tool(
    name="garmin_get_activity_dynamics",
    annotations={"title": "Dinámica de actividad (decoupling/deriva)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_activity_dynamics(activity_id: str) -> dict:
    """Métricas derivadas de la serie segundo a segundo: desacople aeróbico (Pa:HR y, si
    hay potencia, Pw:HR), deriva cardíaca y un proxy de HR recovery. Calculadas en el
    servidor a partir de get_activity_details.

    Referencia rápida: desacople <5% = buen acople aeróbico; >5-10% = deriva/fatiga.

    Args:
        activity_id: ID de la actividad.

    Returns:
        dict: {"activity_id", "samples_used", "aerobic_decoupling_pa_hr_pct",
               "aerobic_decoupling_pw_hr_pct", "cardiac_drift_bpm",
               "hr_recovery_60s_bpm", "hr_first_half_avg", "hr_second_half_avg"}.
    """
    aid = str(activity_id)
    try:
        details = _call("get_activity_details", aid, 2000, 0)
        hr, speed, power, t, t_abs, keys = _extract_series(details)
        if not hr or not any(h for h in hr):
            return {"activity_id": aid,
                    "error": "No se encontró serie de FC en los detalles de la actividad.",
                    "series_keys": keys}
        try:
            splits = (_call("get_activity_typed_splits", aid) or {}).get("splits") or []
        except Exception:  # noqa: BLE001
            splits = []

        # Ventana de análisis: solo bloques INTERVAL_ACTIVE (excluye calentamiento,
        # recuperaciones y vuelta a la calma). Sin bloques, actividad completa.
        active = [(_gmt_s(x.get("startTimeGMT")), _gmt_s(x.get("endTimeGMT")))
                  for x in splits if x.get("type") == "INTERVAL_ACTIVE"
                  and x.get("startTimeGMT") and x.get("endTimeGMT")]
        starts = [_gmt_s(x.get("startTimeGMT")) for x in splits if x.get("startTimeGMT")]
        n = len(hr)
        if active and (t_abs or (t and starts)):
            if t_abs:
                tt = t_abs
            else:
                base = min(starts)
                tt = [(v + base) if v is not None else None for v in t]
            mask = [tt[i] is not None and any(a <= tt[i] <= b for a, b in active)
                    for i in range(n)]
            win_min = round(sum(b - a for a, b in active) / 60.0, 1)
            window = f"{len(active)} bloque(s) activo(s), {win_min} min"
        else:
            mask = [True] * n
            window = "actividad completa (sin bloques estructurados: incluye calentamiento y vuelta a la calma)"

        def sel(series):
            return [v if m else None for v, m in zip(series, mask)] if series else series

        hr_w, speed_w, power_w = sel(hr), sel(speed), sel(power)
        hs = [h for h in hr_w if h]
        half = len(hs) // 2
        hrr, hrr_note = _hr_recovery_anchored(hr, t, aid, splits)
        out = {
            "activity_id": aid,
            "analysis_window": window,
            "samples_used": len(hs),
            "aerobic_decoupling_pa_hr_pct": _decoupling(speed_w, hr_w),
            **sanitize_pw_hr(_decoupling(power_w, hr_w) if power_w else None),
            "cardiac_drift_bpm": _drift(hr_w),
            "hr_recovery_60s_bpm": hrr,
            "hrr_note": hrr_note,
            "hr_first_half_avg": round(sum(hs[:half]) / half) if half else None,
            "hr_second_half_avg": round(sum(hs[half:]) / (len(hs) - half)) if half else None,
            "series_keys": keys,
            "reference": "decoupling <5% buen acople; >5-10% deriva/fatiga",
        }
        if len(active) > 1:
            out["decoupling_note"] = ("Varios bloques activos (intervalos): el desacople "
                                      "compara la primera mitad de los bloques con la "
                                      "segunda; solo es interpretable en rodajes continuos.")
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_hr_curve",
    annotations={"title": "Curva de FC intradía", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_hr_curve(date: Optional[str] = None) -> dict:
    """Patrón de frecuencia cardíaca a lo largo del día (promedios por hora + extremos).
    Útil para ver carga autonómica tras sesiones duras o noches malas.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "resting_hr", "min_hr", "max_hr", "hourly_avg": [{hour, avg}]}.
    """
    cdate = _d(date)
    try:
        h = _call("get_heart_rates", cdate) or {}
        values = h.get("heartRateValues") or []
        buckets: dict[int, list[int]] = {}
        for point in values:
            if isinstance(point, list) and len(point) > 1 and point[1]:
                hour = (datetime.fromtimestamp(point[0] / 1000, timezone.utc)
                        + timedelta(hours=TZ_OFFSET)).hour
                buckets.setdefault(hour, []).append(point[1])
        hourly = [{"hour": hr, "avg": round(sum(v) / len(v))}
                  for hr, v in sorted(buckets.items())]
        return {
            "date": cdate,
            "resting_hr": h.get("restingHeartRate"),
            "min_hr": h.get("minHeartRate"),
            "max_hr": h.get("maxHeartRate"),
            "hourly_avg": hourly,
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_body_composition",
    annotations={"title": "Composición corporal / peso", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_body_composition(start_date: str, end_date: Optional[str] = None) -> dict:
    """Tendencia de peso y composición corporal (báscula Garmin Index) entre dos fechas.

    Args:
        start_date: Fecha inicial ISO 'YYYY-MM-DD'.
        end_date: Fecha final ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: promedios y lista de mediciones, o {"error": ...}.
    """
    try:
        start, end = _d(start_date), _d(end_date)
        data = _call("get_body_composition", start, end) or {}
        avg = (data.get("totalAverage") or {})
        measurements = data.get("dateWeightList") or []
        out = {
            "start": start, "end": end,
            "avg_weight_kg": round(avg["weight"] / 1000.0, 1) if avg.get("weight") else None,
            "avg_bmi": avg.get("bmi"),
            "avg_body_fat_pct": avg.get("bodyFat"),
            "avg_body_water_pct": avg.get("bodyWater"),
            "avg_muscle_mass_kg": round(avg["muscleMass"] / 1000.0, 1) if avg.get("muscleMass") else None,
            "measurements": _strip(measurements),
        }
        if not measurements and out["avg_weight_kg"] is None:
            out["note"] = ("Sin mediciones de báscula en el rango (requiere una báscula "
                           "Garmin Index o pesajes registrados manualmente).")
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_hydration",
    annotations={"title": "Hidratación", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_hydration(date: Optional[str] = None) -> dict:
    """Ingesta de líquidos vs. objetivo del día, con pérdida estimada por sudor.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "intake_ml", "goal_ml", "sweat_loss_ml", "balance_ml"}.
    """
    cdate = _d(date)
    try:
        h = _call("get_hydration_data", cdate) or {}
        intake = h.get("valueInML")
        goal = h.get("goalInML") or h.get("baseGoalInML")
        sweat = h.get("sweatLossInML")
        return {
            "date": cdate,
            "intake_ml": intake,
            "goal_ml": goal,
            "sweat_loss_ml": sweat,
            "balance_ml": (intake - goal) if (intake is not None and goal is not None) else None,
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_training_calibration",
    annotations={"title": "Calibración actual (LTHR, zonas, VO2max)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_training_calibration(date: Optional[str] = None) -> dict:
    """Instantánea de la calibración fisiológica VIGENTE para prescribir entrenamientos:
    umbral de lactato (FC y ritmo), VO2max, estado de entrenamiento, predicciones de
    carrera y readiness del día. Llamar SIEMPRE antes de crear o agendar un workout, para
    fijar zonas/objetivos con datos actuales y no con cifras asumidas.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.

    Returns:
        dict: {"date", "lactate_threshold", "vo2max", "training_status",
               "race_predictions", "readiness_today"}.
    """
    cdate = _d(date)
    out: dict[str, Any] = {"date": cdate, "_notes": {}}

    def _sub(key, fn):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001
            out[key] = None
            out["_notes"][key] = f"{type(e).__name__}: {e}"

    def _lt():
        lt = _call("get_lactate_threshold", latest=True) or {}
        shr = lt.get("speed_and_heart_rate") or {}
        pw = lt.get("power") or {}
        return {
            "threshold_hr_bpm": shr.get("heartRate"),
            "threshold_pace_min_km": _lthr_pace(shr.get("speed")),
            "measured_on": (shr.get("calendarDate") or "")[:10] or None,
            "functional_threshold_power_w": pw.get("functionalThresholdPower"),
            "power_to_weight": round(pw["powerToWeight"], 2) if pw.get("powerToWeight") else None,
            "weight_kg": pw.get("weight"),
        }

    def _vo2():
        # VO2max del día puede venir vacío; retrocede hasta 3 días buscando lectura.
        base = datetime.now(timezone.utc) + timedelta(hours=TZ_OFFSET)
        for back in range(0, 4):
            d = (base - timedelta(days=back)).date().isoformat()
            m = _call("get_max_metrics", d)
            rec = m[0] if isinstance(m, list) and m else m
            gs = (rec or {}).get("generic", {}) if isinstance(rec, dict) else {}
            v = gs.get("vo2MaxPreciseValue") or gs.get("vo2MaxValue")
            if v:
                return {"vo2max_running": v, "as_of": d}
        return {"vo2max_running": None}

    def _readiness():
        r = _call("get_training_readiness", cdate)
        rec = r[0] if isinstance(r, list) and r else r
        if isinstance(rec, dict):
            return {"score": rec.get("score"), "level": rec.get("level")}
        return None

    _sub("lactate_threshold", _lt)
    _sub("vo2max", _vo2)
    _sub("training_status", lambda: _slim_training_status(_call("get_training_status", cdate)))
    _sub("race_predictions", lambda: _fmt_race_predictions(_call("get_race_predictions")))
    _sub("readiness_today", _readiness)

    if not out["_notes"]:
        out.pop("_notes")
    return out


# --------------------------------------------------------------------------- #
# Herramientas — crear / agendar entrenamientos (ESCRITURA)
# --------------------------------------------------------------------------- #
_STEP_BUILDERS = {
    "warmup": create_warmup_step,
    "interval": create_interval_step,
    "recovery": create_recovery_step,
    "rest": create_recovery_step,
    "cooldown": create_cooldown_step,
}


def _hr_target_dict() -> dict:
    return {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
            "displayOrder": 1}


def _pace_target_dict() -> dict:
    # Garmin guarda el ritmo como velocidad (m/s) en un objetivo PACE_ZONE (id 6).
    return {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone",
            "displayOrder": 6}


def _pace_to_mps(pace_str: str) -> float:
    """'M:SS' por km -> velocidad en m/s."""
    mm, ss = str(pace_str).split(":")
    sec_per_km = int(mm) * 60 + int(ss)
    if sec_per_km <= 0:
        raise ValueError(f"Ritmo inválido: '{pace_str}'.")
    return 1000.0 / sec_per_km


def _make_step(spec: dict, order: int):
    kind = (spec.get("kind") or "interval").lower()
    builder = _STEP_BUILDERS.get(kind)
    if not builder:
        raise ValueError(f"Tipo de paso no soportado: '{kind}'. Usa warmup, interval, "
                         "recovery, rest o cooldown.")

    minutes, km = spec.get("minutes"), spec.get("km")
    if minutes is None and km is None:
        raise ValueError(f"El paso '{kind}' necesita 'minutes' (tiempo) o 'km' (distancia).")
    if minutes is not None and km is not None:
        raise ValueError(f"El paso '{kind}' no puede definir 'minutes' y 'km' a la vez; "
                         "elige tiempo o distancia.")
    secs = float(minutes) * 60.0 if minutes is not None else 60.0  # placeholder si es distancia

    hr_low, hr_high = spec.get("hr_low"), spec.get("hr_high")
    pace_slow, pace_fast = spec.get("pace_slow"), spec.get("pace_fast")
    has_hr = bool(hr_low or hr_high)
    has_pace = bool(pace_slow or pace_fast)
    if has_hr and has_pace:
        raise ValueError(
            f"El paso '{kind}' no puede llevar objetivo de FC y de ritmo a la vez; "
            "Garmin admite un solo objetivo por paso. Elige uno."
        )

    target = _pace_target_dict() if has_pace else (_hr_target_dict() if has_hr else None)
    step = builder(secs, order, target)

    if has_pace:
        speeds = []
        if pace_slow:
            speeds.append(_pace_to_mps(pace_slow))
        if pace_fast:
            speeds.append(_pace_to_mps(pace_fast))
        if len(speeds) == 1:  # un solo ritmo -> ventana automática de ±4 s/km
            sec = 1000.0 / speeds[0]
            speeds = [1000.0 / (sec + 4), 1000.0 / (sec - 4)]
        step.targetValueOne = round(min(speeds), 3)  # velocidad baja (ritmo lento)
        step.targetValueTwo = round(max(speeds), 3)  # velocidad alta (ritmo rápido)
    elif has_hr:
        low = int(hr_low) if hr_low else max(int(hr_high) - 20, 90)
        high = int(hr_high) if hr_high else int(hr_low) + 10
        step.targetValueOne = float(low)
        step.targetValueTwo = float(high)
        step.zoneNumber = None

    # Condición de fin por distancia: sobrescribe la de tiempo del helper.
    if km is not None:
        step.endCondition = {"conditionTypeId": 3, "conditionTypeKey": "distance",
                             "displayOrder": 3, "displayable": True}
        step.endConditionValue = float(km) * 1000.0  # metros
    return step


def _build_steps(specs: list, counter: list):
    out = []
    for spec in specs:
        if spec.get("repeat") and spec.get("steps"):
            order = counter[0]; counter[0] += 1
            sub = _build_steps(spec["steps"], counter)
            out.append(create_repeat_group(int(spec["repeat"]), sub, order))
        else:
            order = counter[0]; counter[0] += 1
            out.append(_make_step(spec, order))
    return out


def _estimate_secs(specs: list) -> float:
    total = 0.0
    for spec in specs:
        if spec.get("repeat") and spec.get("steps"):
            total += int(spec["repeat"]) * _estimate_secs(spec["steps"])
        elif spec.get("minutes") is not None:
            total += float(spec["minutes"]) * 60.0
        elif spec.get("km") is not None:
            # Estimar con el ritmo objetivo del paso; si no hay, asumir 6:00/km.
            paces = []
            for k in ("pace_fast", "pace_slow"):
                if spec.get(k):
                    mm, ss = str(spec[k]).split(":")
                    paces.append(int(mm) * 60 + int(ss))
            sec_per_km = (sum(paces) / len(paces)) if paces else 360.0
            total += float(spec["km"]) * sec_per_km
    return total


@mcp.tool(
    name="garmin_create_running_workout",
    annotations={"title": "Crear y agendar entrenamiento en el reloj", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
def garmin_create_running_workout(name: str, steps: list, schedule_date: Optional[str] = None,
                                  description: Optional[str] = None) -> dict:
    """Crea un entrenamiento de carrera estructurado y lo sube al reloj; opcionalmente lo
    agenda en una fecha. ANTES de llamar, refresca la calibración con
    garmin_get_training_calibration y fija los objetivos de FC con esos valores actuales.

    Estructura de 'steps' (lista ordenada). Cada paso es un dict:
        {"kind": "warmup|interval|recovery|rest|cooldown",
         # Duración: elige tiempo O distancia (uno de los dos):
         "minutes": <número>,   # por tiempo
         "km": <número>,        # por distancia (p. ej. 1.0 = 1 km; 0.4 = 400 m)
         # Objetivo OPCIONAL — elige FC O ritmo (no ambos en el mismo paso):
         "hr_low": <bpm>, "hr_high": <bpm>,  # objetivo de FC (techo = hr_high)
         "pace_slow": "M:SS", "pace_fast": "M:SS"  # objetivo de RITMO por km
        }
    Notas de ritmo: se dan en min/km. 'pace_slow' es el extremo lento (p. ej. "4:40") y
    'pace_fast' el rápido ("4:30"). Si solo das uno, se crea una ventana de ±4 s/km.

    Para repeticiones (p. ej. 3×(10 min + 3 min trote)), usa un bloque:
        {"repeat": 3, "steps": [ {paso de trabajo}, {paso de recuperación} ]}

    Ejemplo con FC (3×10 umbral, techo 160, recuperaciones trotando):
        steps=[
          {"kind":"warmup","minutes":15,"hr_high":140},
          {"repeat":3,"steps":[
              {"kind":"interval","minutes":10,"hr_low":153,"hr_high":160},
              {"kind":"recovery","minutes":3,"hr_high":140}]},
          {"kind":"cooldown","minutes":10,"hr_high":140}
        ]

    Ejemplo por DISTANCIA y ritmo (5×1 km a 4:20-4:30 con 400 m de trote):
        steps=[
          {"kind":"warmup","minutes":15,"hr_high":140},
          {"repeat":5,"steps":[
              {"kind":"interval","km":1.0,"pace_fast":"4:20","pace_slow":"4:30"},
              {"kind":"recovery","km":0.4,"hr_high":140}]},
          {"kind":"cooldown","minutes":10,"hr_high":140}
        ]

    Args:
        name: Nombre del entrenamiento.
        steps: Lista de pasos/bloques como se describe arriba.
        schedule_date: Fecha ISO 'YYYY-MM-DD' para agendarlo (opcional).
        description: Nota libre del entrenamiento (opcional).

    Returns:
        dict: {"workout_id", "name", "num_steps", "estimated_min", "scheduled_for"}.
    """
    try:
        if not isinstance(steps, list) or not steps:
            return {"error": "Proporciona al menos un paso en 'steps'."}
        counter = [1]
        built = _build_steps(steps, counter)
        est = _estimate_secs(steps)
        workout = RunningWorkout(
            workoutName=name,
            estimatedDurationInSecs=int(est) or None,
            description=description,
            workoutSegments=[WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 1, "sportTypeKey": "running"},
                workoutSteps=built,
            )],
        )
        result = _call("upload_running_workout", workout) or {}
        wid = result.get("workoutId") or result.get("workoutid")
        scheduled = None
        if schedule_date and wid:
            sd = _d(schedule_date)
            _call("schedule_workout", wid, sd)
            scheduled = sd
        out = {
            "workout_id": wid,
            "name": name,
            "num_steps": counter[0] - 1,
            "estimated_min": round(est / 60.0, 1),
            "scheduled_for": scheduled,
        }
        warn = check_workout_name(name)
        if warn:
            out["aviso"] = warn
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Herramientas — series intradía con ventana horaria (payload pequeño)
# --------------------------------------------------------------------------- #
def _ts_local(ts_ms: float):
    """(str 'HH:MM', minutos del día) en hora local a partir de epoch ms."""
    dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc) + timedelta(hours=TZ_OFFSET)
    return dt.strftime("%H:%M"), dt.hour * 60 + dt.minute


def _hhmm_min(s: Optional[str]):
    """'HH:MM' -> minutos del día (o None si no aplica/mal formado)."""
    if not s:
        return None
    try:
        hh, mm = str(s).split(":")
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def _in_window(m: int, start_min: Optional[int], end_min: Optional[int]) -> bool:
    if start_min is not None and m < start_min:
        return False
    if end_min is not None and m > end_min:
        return False
    return True


@mcp.tool(
    name="garmin_get_hr_intraday",
    annotations={"title": "FC intradía (ventana)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_hr_intraday(date: Optional[str] = None, start_hhmm: Optional[str] = None,
                           end_hhmm: Optional[str] = None) -> dict:
    """Frecuencia cardíaca punto a punto (cadencia ~2 min) en una ventana horaria local.
    Filtrar por ventana mantiene el payload pequeño (p. ej. la hora de una carrera, ~50-75
    puntos, en vez del día entero).

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.
        start_hhmm: Inicio de la ventana en hora local 'HH:MM' (opcional).
        end_hhmm: Fin de la ventana en hora local 'HH:MM' (opcional).

    Returns:
        dict: {"date", "window", "summary": {mean, min, max, n}, "series": [{t, bpm}]}.
    """
    cdate = _d(date)
    try:
        h = _call("get_heart_rates", cdate) or {}
        vals = h.get("heartRateValues") or []
        s_min, e_min = _hhmm_min(start_hhmm), _hhmm_min(end_hhmm)
        series, bpms = [], []
        for p in vals:
            if not isinstance(p, list) or len(p) < 2 or p[1] is None:
                continue
            t, m = _ts_local(p[0])
            if not _in_window(m, s_min, e_min):
                continue
            bpm = int(p[1])
            series.append({"t": t, "bpm": bpm})
            bpms.append(bpm)
        summary = {
            "mean": round(sum(bpms) / len(bpms)) if bpms else None,
            "min": min(bpms) if bpms else None,
            "max": max(bpms) if bpms else None,
            "n": len(bpms),
        }
        return {"date": cdate, "window": {"start": start_hhmm, "end": end_hhmm},
                "summary": summary, "series": series}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_stress_intraday",
    annotations={"title": "Estrés intradía (ventana)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_stress_intraday(date: Optional[str] = None, start_hhmm: Optional[str] = None,
                               end_hhmm: Optional[str] = None) -> dict:
    """Estrés punto a punto (cadencia ~3 min) y Body Battery en una ventana horaria local.
    Excluye del promedio los valores -1 (movimiento) y -2 (no fiable). Filtrar por ventana
    mantiene el payload pequeño.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.
        start_hhmm: Inicio de la ventana en hora local 'HH:MM' (opcional).
        end_hhmm: Fin de la ventana en hora local 'HH:MM' (opcional).

    Returns:
        dict: {"date", "window", "summary": {mean_valid, max, n_valid, n_gated},
               "body_battery": {start, end, drained}, "series": [{t, stress}]}.
    """
    cdate = _d(date)
    try:
        data = _call("get_stress_data", cdate) or {}
        sv = data.get("stressValuesArray") or []
        bb = data.get("bodyBatteryValuesArray") or []
        s_min, e_min = _hhmm_min(start_hhmm), _hhmm_min(end_hhmm)

        series, valid, n_gated = [], [], 0
        for p in sv:
            if not isinstance(p, list) or len(p) < 2 or p[1] is None:
                continue
            t, m = _ts_local(p[0])
            if not _in_window(m, s_min, e_min):
                continue
            stress = int(p[1])
            series.append({"t": t, "stress": stress})
            if stress < 0:  # -1 movimiento, -2 no fiable
                n_gated += 1
            else:
                valid.append(stress)

        bb_levels = []
        for p in bb:
            if not isinstance(p, list) or len(p) < 2:
                continue
            t, m = _ts_local(p[0])
            if not _in_window(m, s_min, e_min):
                continue
            level = p[-1]  # el nivel es el último elemento (tolera [ts,lvl] y [ts,estado,lvl])
            if isinstance(level, (int, float)) and 0 <= level <= 100:
                bb_levels.append(int(level))

        body_battery = {
            "start": bb_levels[0] if bb_levels else None,
            "end": bb_levels[-1] if bb_levels else None,
            "drained": (bb_levels[0] - bb_levels[-1]) if len(bb_levels) >= 2 else None,
        }
        summary = {
            "mean_valid": round(sum(valid) / len(valid)) if valid else None,
            "max": max(valid) if valid else None,
            "n_valid": len(valid),
            "n_gated": n_gated,
        }
        return {"date": cdate, "window": {"start": start_hhmm, "end": end_hhmm},
                "summary": summary, "body_battery": body_battery, "series": series}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Herramientas — fuerza (gimnasio) y natación
# --------------------------------------------------------------------------- #
# sportTypeId de fuerza en el workout-service. No está documentado por Garmin y ha
# cambiado entre versiones; se puede sobrescribir sin tocar el código si hiciera falta.
# Verifícalo con garmin_inspect_workout_schema (paso previo obligatorio, ver docstring).
STRENGTH_SPORT_TYPE_ID = int(os.environ.get("STRENGTH_SPORT_TYPE_ID", "5"))

# IDs de estilo de nado y equipamiento. NO VERIFICADOS contra tu cuenta: Garmin no los
# documenta. Cada herramienta acepta también el id numérico directo para saltarse este
# mapa. Confírmalos con garmin_inspect_workout_schema antes de fiarte de ellos.
_STROKE_IDS = {"free": 5, "backstroke": 1, "breaststroke": 2, "butterfly": 4,
               "drill": 3, "im": 6, "mixed": 7, "choice": 0}
_EQUIPMENT_IDS = {"none": 0, "swim_fins": 1, "swim_kickboard": 2, "swim_paddles": 3,
                  "swim_pull_buoy": 4, "swim_snorkel": 5}

_GRAMS_PER_KG = 1000.0


def _kg(grams: Optional[float]) -> Optional[float]:
    """Gramos (unidad nativa de Garmin en series de fuerza) -> kg."""
    if grams is None:
        return None
    return round(float(grams) / _GRAMS_PER_KG, 1)


def _pace_100m(seconds: Optional[float], meters: Optional[float]) -> Optional[str]:
    """Ritmo 'M:SS' por 100 m."""
    if not seconds or not meters:
        return None
    sec = float(seconds) * 100.0 / float(meters)
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _top_exercise(exercises: list) -> dict:
    """Elige el ejercicio con mayor probabilidad de reconocimiento del reloj."""
    if not exercises:
        return {"category": None, "name": None, "confidence": None}
    top = max(exercises, key=lambda e: (e or {}).get("probability") or 0)
    return {"category": top.get("category"), "name": top.get("name"),
            "confidence": top.get("probability")}


@mcp.tool(
    name="garmin_get_strength_session",
    annotations={"title": "Leer sesión de fuerza", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_strength_session(activity_id: str) -> dict:
    """Identifica los ejercicios realizados en una sesión de gimnasio, con series,
    repeticiones, carga y volumen. Usa primero garmin_list_activities o
    garmin_get_last_activity para obtener el activity_id.

    Las series marcadas como REST se excluyen del conteo pero alimentan el descanso medio.
    El campo 'confidence' es la probabilidad del reconocimiento automático del reloj:
    valores bajos o name=null significan que el movimiento no se identificó bien y no
    debe usarse para decidir progresión de carga sin confirmarlo a mano.

    Args:
        activity_id: ID de la actividad de fuerza.

    Returns:
        dict: {"activity_id", "total_sets", "total_volume_kg", "avg_rest_s",
               "exercises": [{category, name, confidence, sets, total_reps,
               max_weight_kg, volume_kg, set_detail: [{reps, weight_kg, duration_s}]}]}.
    """
    try:
        raw = _call("get_activity_exercise_sets", activity_id) or {}
        sets = raw.get("exerciseSets") or []
        if not sets:
            return {"activity_id": activity_id,
                    "error": "Sin series registradas. Puede no ser una actividad de fuerza, "
                             "o el reloj no registró series (modo libre sin detección)."}

        grouped: dict = {}
        rests: list = []
        total_volume = 0.0
        active = 0

        for s in sets:
            if str(s.get("setType") or "").upper() == "REST":
                if s.get("duration"):
                    rests.append(float(s["duration"]))
                continue
            active += 1
            ident = _top_exercise(s.get("exercises") or [])
            key = ident["name"] or ident["category"] or "DESCONOCIDO"
            reps = s.get("repetitionCount") or 0
            w = _kg(s.get("weight"))
            vol = (w or 0) * reps
            total_volume += vol

            e = grouped.setdefault(key, {
                "category": ident["category"], "name": ident["name"],
                "confidence": ident["confidence"], "sets": 0, "total_reps": 0,
                "max_weight_kg": None, "volume_kg": 0.0, "set_detail": [],
            })
            e["sets"] += 1
            e["total_reps"] += reps
            e["volume_kg"] = round(e["volume_kg"] + vol, 1)
            if w is not None:
                e["max_weight_kg"] = w if e["max_weight_kg"] is None else max(e["max_weight_kg"], w)
            e["set_detail"].append({"reps": reps, "weight_kg": w,
                                    "duration_s": round(s["duration"], 1) if s.get("duration") else None})

        return {
            "activity_id": activity_id,
            "total_sets": active,
            "total_volume_kg": round(total_volume, 1),
            "avg_rest_s": round(sum(rests) / len(rests), 1) if rests else None,
            "exercises": list(grouped.values()),
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_get_swim_session",
    annotations={"title": "Leer sesión de natación", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_get_swim_session(activity_id: str) -> dict:
    """Analiza una sesión de natación: distancia, ritmo por 100 m, SWOLF, brazadas y
    estilo, con el desglose intervalo a intervalo.

    SWOLF = segundos por largo + brazadas por largo. Bajar SWOLF a igual ritmo indica
    mejor eficiencia técnica; útil para seguir el trabajo con pull buoy.

    Args:
        activity_id: ID de la actividad de natación.

    Returns:
        dict: {"activity_id", "pool_length_m", "total_distance_m", "total_time_s",
               "avg_pace_100m", "avg_swolf", "avg_strokes", "avg_hr",
               "intervals": [{index, distance_m, time_s, pace_100m, stroke, swolf,
               strokes, avg_hr}]}.
    """
    try:
        act = _call("get_activity", activity_id) or {}
        summary = act.get("summaryDTO") or act
        splits = _call("get_activity_splits", activity_id) or {}

        dist = summary.get("distance")
        dur = summary.get("duration")

        intervals = []
        for i, lap in enumerate(splits.get("lapDTOs") or [], start=1):
            d, t = lap.get("distance"), lap.get("duration")
            intervals.append({
                "index": i,
                "distance_m": round(d, 1) if d else None,
                "time_s": round(t, 1) if t else None,
                "pace_100m": _pace_100m(t, d),
                "stroke": lap.get("swimStroke"),
                "swolf": lap.get("avgSwolf"),
                "strokes": lap.get("totalNumberOfStrokes"),
                "avg_hr": lap.get("averageHR"),
            })

        return {
            "activity_id": activity_id,
            "pool_length_m": summary.get("poolLength"),
            "total_distance_m": round(dist, 1) if dist else None,
            "total_time_s": round(dur, 1) if dur else None,
            "avg_pace_100m": _pace_100m(dur, dist),
            "avg_swolf": summary.get("avgSwolf"),
            "avg_strokes": summary.get("avgStrokes"),
            "avg_hr": summary.get("averageHR"),
            "intervals": intervals,
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_create_strength_workout",
    annotations={"title": "Crear entrenamiento de fuerza", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
def garmin_create_strength_workout(name: str, exercises: list,
                                   schedule_date: Optional[str] = None,
                                   description: Optional[str] = None) -> dict:
    """Crea un entrenamiento de fuerza y lo sube al reloj; opcionalmente lo agenda.

    IMPORTANTE: la primera vez, corre garmin_inspect_workout_schema para confirmar el
    sportTypeId de fuerza. Garmin no lo documenta y aquí se asume 5; si tu cuenta usa
    otro, la subida falla o queda como "otro". Se corrige con la variable de entorno
    STRENGTH_SPORT_TYPE_ID, sin tocar código.

    Estructura de 'exercises' (lista ordenada). Cada ejercicio es un dict:
        {"category": "SQUAT",              # obligatorio, en MAYÚSCULAS
         "exercise": "DUMBBELL_SPLIT_SQUAT",  # opcional; nombre EXACTO del catálogo
                                           # (búscalo con garmin_search_exercises)
         "sets": 4,                        # series (por defecto 1)
         "reps": 8,                        # repeticiones por serie
         "seconds": 45,                    # O duración, para isométricos (excluyente con reps)
         "weight_kg": 20,                  # carga opcional
         "rest_s": 120}                    # descanso tras cada serie (por defecto 90)

    Ejemplo (bloque unilateral pesado tipo Kongsgaard):
        exercises=[
          {"category":"SQUAT","exercise":"DUMBBELL_SPLIT_SQUAT","sets":4,"reps":8,
           "weight_kg":20,"rest_s":150},
          {"category":"DEADLIFT","exercise":"SINGLE_LEG_ROMANIAN_DEADLIFT_WITH_DUMBBELL",
           "sets":3,"reps":8,"weight_kg":16,"rest_s":120},
          {"category":"PLANK","exercise":"SINGLE_LEG_SIDE_PLANK","sets":3,"seconds":30,"rest_s":60}
        ]

    Args:
        name: Nombre del entrenamiento.
        exercises: Lista de ejercicios como se describe arriba.
        schedule_date: Fecha ISO 'YYYY-MM-DD' para agendarlo (opcional).
        description: Nota libre (opcional).

    Returns:
        dict: {"workout_id", "name", "num_steps", "total_sets", "estimated_min",
               "scheduled_for"}.
    """
    try:
        if not isinstance(exercises, list) or not exercises:
            return {"error": "Proporciona al menos un ejercicio en 'exercises'."}
        invalid = validate_strength_payload(name, exercises)
        if invalid:
            return invalid

        steps: list = []
        order = 1
        est = 0.0
        total_sets = 0

        for ex in exercises:
            cat = (ex.get("category") or "").upper().strip()
            if not cat:
                return {"error": f"Cada ejercicio necesita 'category' (p. ej. 'SQUAT'). Recibido: {ex}"}
            reps, secs = ex.get("reps"), ex.get("seconds")
            if reps is not None and secs is not None:
                return {"error": f"El ejercicio '{cat}' no puede llevar 'reps' y 'seconds' a la vez."}
            if reps is None and secs is None:
                return {"error": f"El ejercicio '{cat}' necesita 'reps' o 'seconds'."}

            n_sets = int(ex.get("sets") or 1)
            rest_s = int(ex.get("rest_s") if ex.get("rest_s") is not None else 90)
            total_sets += n_sets

            for _ in range(n_sets):
                if secs is not None:
                    end = {"conditionTypeId": 2, "conditionTypeKey": "time",
                           "displayOrder": 2, "displayable": True}
                    end_val = float(secs)
                    est += float(secs)
                else:
                    # reps = conditionTypeId 10 en el workout-service de Garmin.
                    end = {"conditionTypeId": 10, "conditionTypeKey": "reps",
                           "displayOrder": 10, "displayable": True}
                    end_val = float(reps)
                    est += float(reps) * 3.5  # ~3,5 s por repetición en tempo lento

                step = {
                    "type": "ExecutableStepDTO",
                    "stepOrder": order,
                    "stepType": {"stepTypeId": 3, "stepTypeKey": "interval", "displayOrder": 3},
                    "endCondition": end,
                    "endConditionValue": end_val,
                    "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target",
                                   "displayOrder": 1},
                    "category": cat,
                }
                if ex.get("exercise"):
                    step["exerciseName"] = str(ex["exercise"]).upper().strip()
                if ex.get("weight_kg"):
                    step["weightValue"] = float(ex["weight_kg"])
                    step["weightUnit"] = {"unitKey": "kilogram"}
                steps.append(step)
                order += 1

                if rest_s > 0:
                    steps.append({
                        "type": "ExecutableStepDTO",
                        "stepOrder": order,
                        "stepType": {"stepTypeId": 5, "stepTypeKey": "rest", "displayOrder": 5},
                        "endCondition": {"conditionTypeId": 2, "conditionTypeKey": "time",
                                         "displayOrder": 2, "displayable": True},
                        "endConditionValue": float(rest_s),
                        "targetType": {"workoutTargetTypeId": 1,
                                       "workoutTargetTypeKey": "no.target", "displayOrder": 1},
                    })
                    order += 1
                    est += rest_s

        sport = {"sportTypeId": STRENGTH_SPORT_TYPE_ID,
                 "sportTypeKey": "strength_training", "displayOrder": STRENGTH_SPORT_TYPE_ID}
        payload = {
            "workoutName": name,
            "description": description,
            "sportType": sport,
            "estimatedDurationInSecs": int(est) or None,
            "workoutSegments": [{
                "segmentOrder": 1,
                "sportType": sport,
                "workoutSteps": steps,
            }],
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        result = _call("upload_workout", payload) or {}
        wid = result.get("workoutId") or result.get("workoutid")
        scheduled = None
        if schedule_date and wid:
            sd = _d(schedule_date)
            _call("schedule_workout", wid, sd)
            scheduled = sd
        out = {"workout_id": wid, "name": name, "num_steps": len(steps),
               "total_sets": total_sets, "estimated_min": round(est / 60.0, 1),
               "scheduled_for": scheduled}
        warn = check_workout_name(name)
        if warn:
            out["aviso"] = warn
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_create_swim_workout",
    annotations={"title": "Crear entrenamiento de natación", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
)
def garmin_create_swim_workout(name: str, steps: list, pool_length_m: float = 25.0,
                               schedule_date: Optional[str] = None,
                               description: Optional[str] = None) -> dict:
    """Crea un entrenamiento de natación en piscina y lo sube al reloj; opcionalmente
    lo agenda.

    Estructura de 'steps' (lista ordenada). Cada paso es un dict:
        {"kind": "warmup|interval|recovery|rest|cooldown",
         "meters": 200,          # por distancia (elige metros O segundos)
         "seconds": 60,          # por tiempo (p. ej. descansos)
         "stroke": "free",       # free, backstroke, breaststroke, butterfly, drill, im, mixed, choice
         "stroke_id": 5,         # alternativa: id numérico directo (tiene prioridad)
         "equipment": "swim_pull_buoy",   # none, swim_fins, swim_kickboard, swim_paddles,
                                          # swim_pull_buoy, swim_snorkel
         "rest_s": 20}           # descanso tras el paso (opcional)

    Para repeticiones usa un bloque:  {"repeat": 8, "steps": [ {...}, {...} ]}

    Ejemplo (sesión con pull buoy, sin patada, para descargar la cadera):
        steps=[
          {"kind":"warmup","meters":300,"stroke":"free"},
          {"repeat":8,"steps":[
              {"kind":"interval","meters":100,"stroke":"free",
               "equipment":"swim_pull_buoy","rest_s":20}]},
          {"kind":"cooldown","meters":200,"stroke":"choice"}
        ]

    NOTA: los ids de estilo y equipamiento no están documentados por Garmin. Si el reloj
    muestra un estilo distinto al pedido, confirma los ids con
    garmin_inspect_workout_schema y usa 'stroke_id' directamente.

    Args:
        name: Nombre del entrenamiento.
        steps: Lista de pasos/bloques como se describe arriba.
        pool_length_m: Longitud de la piscina en metros (por defecto 25).
        schedule_date: Fecha ISO 'YYYY-MM-DD' para agendarlo (opcional).
        description: Nota libre (opcional).

    Returns:
        dict: {"workout_id", "name", "num_steps", "total_distance_m", "estimated_min",
               "scheduled_for"}.
    """
    try:
        if not isinstance(steps, list) or not steps:
            return {"error": "Proporciona al menos un paso en 'steps'."}

        counter = [1]
        totals = {"m": 0.0, "s": 0.0}

        def _one(spec: dict):
            kind = (spec.get("kind") or "interval").lower()
            builder = _STEP_BUILDERS.get(kind)
            if not builder:
                raise ValueError(f"Tipo de paso no soportado: '{kind}'. Usa warmup, "
                                 "interval, recovery, rest o cooldown.")
            meters, seconds = spec.get("meters"), spec.get("seconds")
            if meters is None and seconds is None:
                raise ValueError(f"El paso '{kind}' necesita 'meters' o 'seconds'.")
            if meters is not None and seconds is not None:
                raise ValueError(f"El paso '{kind}' no puede definir 'meters' y 'seconds' a la vez.")

            order = counter[0]; counter[0] += 1
            secs = float(seconds) if seconds is not None else 60.0
            step = builder(secs, order, None)

            if meters is not None:
                step.endCondition = {"conditionTypeId": 3, "conditionTypeKey": "distance",
                                     "displayOrder": 3, "displayable": True}
                step.endConditionValue = float(meters)
                totals["m"] += float(meters)
                totals["s"] += float(meters) / 100.0 * 120.0  # estimación a 2:00/100 m
            else:
                totals["s"] += secs

            sid = spec.get("stroke_id")
            if sid is None:
                sid = _STROKE_IDS.get(str(spec.get("stroke") or "free").lower(), 5)
            step.strokeType = {"strokeTypeId": int(sid), "displayOrder": int(sid)}

            eq = spec.get("equipment")
            if eq:
                eid = _EQUIPMENT_IDS.get(str(eq).lower())
                if eid is None:
                    raise ValueError(f"Equipamiento no reconocido: '{eq}'. Opciones: "
                                     + ", ".join(_EQUIPMENT_IDS))
                step.equipmentType = {"equipmentTypeId": eid, "displayOrder": eid}

            out = [step]
            rest_s = spec.get("rest_s")
            if rest_s:
                r_order = counter[0]; counter[0] += 1
                rest = create_recovery_step(float(rest_s), r_order, None)
                out.append(rest)
                totals["s"] += float(rest_s)
            return out

        def _build(specs: list) -> list:
            out = []
            for spec in specs:
                if spec.get("repeat") and spec.get("steps"):
                    order = counter[0]; counter[0] += 1
                    n = int(spec["repeat"])
                    before = dict(totals)
                    sub = _build(spec["steps"])
                    # el bloque se ejecuta n veces: escalar lo acumulado dentro
                    totals["m"] = before["m"] + (totals["m"] - before["m"]) * n
                    totals["s"] = before["s"] + (totals["s"] - before["s"]) * n
                    out.append(create_repeat_group(n, sub, order))
                else:
                    out.extend(_one(spec))
            return out

        built = _build(steps)

        workout = SwimmingWorkout(
            workoutName=name,
            estimatedDurationInSecs=int(totals["s"]) or None,
            description=description,
            poolLength=float(pool_length_m),
            poolLengthUnit={"unitId": 1, "unitKey": "meter", "factor": 100.0},
            workoutSegments=[WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 4, "sportTypeKey": "swimming", "displayOrder": 3},
                workoutSteps=built,
            )],
        )
        result = _call("upload_swimming_workout", workout) or {}
        wid = result.get("workoutId") or result.get("workoutid")
        scheduled = None
        if schedule_date and wid:
            sd = _d(schedule_date)
            _call("schedule_workout", wid, sd)
            scheduled = sd
        return {"workout_id": wid, "name": name, "num_steps": counter[0] - 1,
                "total_distance_m": round(totals["m"]), "pool_length_m": pool_length_m,
                "estimated_min": round(totals["s"] / 60.0, 1), "scheduled_for": scheduled}
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(
    name="garmin_inspect_workout_schema",
    annotations={"title": "Inspeccionar esquema de entrenamiento", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
def garmin_inspect_workout_schema(workout_id: Optional[str] = None) -> dict:
    """Descarga un entrenamiento existente para ver el JSON real que usa Garmin.

    Sirve para verificar empíricamente los ids que Garmin no documenta: sportTypeId de
    fuerza, strokeTypeId de cada estilo de nado y equipmentTypeId. Procedimiento:
    crea a mano en la app Garmin Connect un entrenamiento del tipo que quieras verificar,
    llama a esta herramienta sin argumentos para listarlo, y luego con su workout_id
    para leer el JSON completo.

    Args:
        workout_id: ID del entrenamiento. Si se omite, lista los disponibles.

    Returns:
        dict: {"count", "workouts": [{workout_id, name, sport, sport_type_id}]} si no se
              da workout_id; si se da, el JSON completo del entrenamiento.
    """
    try:
        if not workout_id:
            data = _call("get_workouts") or []
            items = data if isinstance(data, list) else data.get("workoutList", [])
            out = []
            for w in items:
                st = w.get("sportType") or {}
                out.append({"workout_id": w.get("workoutId"),
                            "name": w.get("workoutName"),
                            "sport": st.get("sportTypeKey"),
                            "sport_type_id": st.get("sportTypeId")})
            return {"count": len(out), "workouts": out}
        return _strip(_call("get_workout_by_id", workout_id))
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Herramientas — gestión de entrenamientos y catálogo de ejercicios
# --------------------------------------------------------------------------- #
def _delete_workouts(workout_ids: list) -> dict:
    ok, fail = [], []
    for wid in workout_ids:
        try:
            _call("delete_workout", str(wid))
            ok.append(str(wid))
        except Exception as exc:  # noqa: BLE001 — se reporta el fallo por id
            fail.append({"workout_id": str(wid), "error": str(exc)[:200]})
    return {"borrados": ok, "fallidos": fail}


@mcp.tool(
    name="garmin_delete_workout",
    annotations={"title": "Borrar un entrenamiento", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
def garmin_delete_workout(workout_id: str) -> dict:
    """Borra UN entrenamiento de Garmin Connect (y su agenda en el reloj).
    Obtén el workout_id con garmin_inspect_workout_schema sin argumentos.
    Irreversible: confirma con el usuario antes de llamar."""
    return _delete_workouts([workout_id])


@mcp.tool(
    name="garmin_delete_workouts",
    annotations={"title": "Borrar varios entrenamientos", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": True},
)
def garmin_delete_workouts(workout_ids: list[str]) -> dict:
    """Borra VARIOS entrenamientos por id en una sola llamada. Reporta cuáles
    se borraron y cuáles fallaron. Irreversible: confirma con el usuario antes."""
    return _delete_workouts(workout_ids)


@mcp.tool(
    name="garmin_validate_exercise",
    annotations={"title": "Validar ejercicio de fuerza", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def garmin_validate_exercise(category: str, exercise: Optional[str] = None) -> dict:
    """Verifica si una categoría/ejercicio existe en el catálogo oficial de Garmin
    antes de crear un entrenamiento de fuerza. Devuelve sugerencias si no existe."""
    msg = validate_exercise(category, exercise)
    return {"valido": msg is None, "detalle": msg}


@mcp.tool(
    name="garmin_search_exercises",
    annotations={"title": "Buscar ejercicios de fuerza", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def garmin_search_exercises(texto: str, category: Optional[str] = None, limit: int = 20) -> dict:
    """Busca nombres válidos de ejercicio por texto (p. ej. 'hip thrust', 'stretch calf').
    Úsalo para encontrar el nombre exacto antes de crear un entrenamiento."""
    q = _norm(texto)
    pool = CATALOG.get(_norm(category), set()) if category else set(_ALL_NAMES)
    hits = sorted(n for n in pool if all(tok in n for tok in q.split("_") if tok))
    return {"resultados": [{"exercise": n, "category": _ALL_NAMES[n]} for n in hits[:limit]],
            "total": len(hits)}


# --------------------------------------------------------------------------- #
# Herramientas — equipo, récords, tendencias y Body Battery
# --------------------------------------------------------------------------- #
MAX_DAYS = 31          # métricas que se piden día por día (1 llamada/día a Garmin)
MAX_RANGE_DAYS = 366   # métricas que Garmin entrega por rango en una sola llamada

# Récords de carrera: typeId -> etiqueta (documentado en garminconnect.get_personal_record)
_RUN_PR = {1: "1 km", 2: "1 milla", 3: "5 km", 4: "10 km", 5: "Media maratón",
           6: "Maratón", 7: "Carrera más larga"}


# --------------------------------------------------------------------------- #
# Utilidades de fechas y formato para tendencias y récords
# --------------------------------------------------------------------------- #
def _iso(value: str) -> _date:
    return datetime.fromisoformat(value).date()


def _pace_from(seconds: Optional[float], meters: float) -> Optional[str]:
    if not seconds or not meters:
        return None
    p = seconds / (meters / 1000.0)
    return f"{int(p // 60)}:{int(round(p % 60)):02d}/km"


def _h(sec: Optional[float]) -> Optional[float]:
    return round(sec / 3600.0, 1) if sec else None


def _range(start_date: str, end_date: Optional[str], today: str, max_days: int):
    d0 = _iso(start_date)
    d1 = _iso(end_date) if end_date else _iso(today)
    if d1 < d0:
        raise ValueError("end_date no puede ser anterior a start_date.")
    if (d1 - d0).days + 1 > max_days:
        raise ValueError(f"Rango demasiado amplio para esta métrica (máx. {max_days} días).")
    return d0, d1


def _days(d0: _date, d1: _date):
    cur = d0
    while cur <= d1:
        yield cur.isoformat()
        cur += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Extractores por día (mismos campos que ya usa el servidor)
# --------------------------------------------------------------------------- #
def _day_hrv(call, iso):
    h = call("get_hrv_data", iso) or {}
    s = h.get("hrvSummary") or {}
    b = s.get("baseline") or {}
    return {"last_night_avg": s.get("lastNightAvg"), "weekly_avg": s.get("weeklyAvg"),
            "status": s.get("status"),
            "balanced_low": b.get("balancedLow"), "balanced_upper": b.get("balancedUpper")}


def _day_sleep(call, iso):
    data = call("get_sleep_data", iso) or {}
    s = data.get("dailySleepDTO") or {}
    overall = (s.get("sleepScores") or {}).get("overall") or {}
    return {"score": overall.get("value"), "total_h": _h(s.get("sleepTimeSeconds")),
            "deep_h": _h(s.get("deepSleepSeconds")), "rem_h": _h(s.get("remSleepSeconds")),
            "awake_h": _h(s.get("awakeSleepSeconds")),
            "avg_overnight_hrv": data.get("avgOvernightHrv"),
            "resting_hr": data.get("restingHeartRate")}


def _day_rhr(call, iso):
    r = call("get_rhr_day", iso) or {}
    m = (((r.get("allMetrics") or {}).get("metricsMap") or {})
         .get("WELLNESS_RESTING_HEART_RATE") or [])
    return {"resting_hr": m[0].get("value") if m else r.get("restingHeartRate")}


def _day_stress(call, iso):
    s = call("get_stress_data", iso) or {}
    return {"avg": s.get("avgStressLevel"), "max": s.get("maxStressLevel"),
            "rest_pct": s.get("restStressPercentage"), "high_pct": s.get("highStressPercentage")}


def _day_vo2(call, iso):
    m = call("get_max_metrics", iso)
    rec = m[0] if isinstance(m, list) and m else m
    g = (rec or {}).get("generic") or {} if isinstance(rec, dict) else {}
    return {"vo2max_running": g.get("vo2MaxPreciseValue") or g.get("vo2MaxValue")}


def _day_spo2(call, iso):
    s = call("get_spo2_data", iso) or {}
    return {"avg": s.get("averageSpO2"), "lowest": s.get("lowestSpO2")}


def _day_resp(call, iso):
    r = call("get_respiration_data", iso) or {}
    return {"avg_waking": r.get("avgWakingRespirationValue"),
            "avg_sleep": r.get("avgSleepRespirationValue")}


_DAILY = {"hrv": _day_hrv, "sleep": _day_sleep, "resting_hr": _day_rhr,
          "stress": _day_stress, "vo2max": _day_vo2, "spo2": _day_spo2,
          "respiration": _day_resp}
_RANGED = ("body_battery", "intensity_minutes", "hill_score", "endurance_score")
METRICS = tuple(_DAILY) + _RANGED


def _all_null(d: dict) -> bool:
    return all(v is None for v in d.values())


def _summary(days: list[dict], key: str) -> Optional[dict]:
    vals = [d[key] for d in days if isinstance(d.get(key), (int, float))]
    if not vals:
        return None
    return {"field": key, "n": len(vals), "mean": round(sum(vals) / len(vals), 1),
            "min": min(vals), "max": max(vals), "first": vals[0], "last": vals[-1]}


_SUMMARY_KEY = {"hrv": "last_night_avg", "sleep": "score", "resting_hr": "resting_hr",
                "stress": "avg", "vo2max": "vo2max_running", "spo2": "avg",
                "respiration": "avg_sleep", "body_battery": "max"}


_PK_CACHE: dict[str, Any] = {}


def _profile_pk() -> str:
    """userProfilePk (número de perfil) que exigen los endpoints de equipo."""
    if "pk" not in _PK_CACHE:
        pk = getattr(_get_client(), "profile_id", None)
        if not pk:
            pk = (_call("get_device_last_used") or {}).get("userProfileNumber")
        if not pk:
            raise RuntimeError("No se pudo obtener el número de perfil de Garmin.")
        _PK_CACHE["pk"] = str(pk)
    return _PK_CACHE["pk"]

_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
      "openWorldHint": True}

# ------------------------------------------------------------------ equipo
@mcp.tool(name="garmin_get_gear", annotations={"title": "Equipo y kilometraje", **_RO})
def garmin_get_gear(include_retired: bool = False, activity_id: Optional[str] = None) -> dict:
    """Equipo registrado en Garmin Connect (zapatillas, etc.) con kilómetros acumulados,
    número de actividades, límite de retiro configurado y % consumido.

    Args:
        include_retired: Incluir equipo retirado. Por defecto solo el activo.
        activity_id: Si se da, devuelve solo el equipo asociado a esa actividad.

    Returns:
        dict: {"count", "gear": [{gear_uuid, name, type, status, km, activities,
               max_km, pct_used, default_for}]} o {"activity_id", "gear": [...]}.
    """
    try:
        if activity_id:
            g = _call("get_activity_gear", activity_id) or []
            items = g if isinstance(g, list) else [g]
            return {"activity_id": str(activity_id),
                    "gear": [{"gear_uuid": x.get("uuid"),
                              "name": x.get("displayName") or x.get("customMakeModel"),
                              "type": x.get("gearTypeName")} for x in items if x]}
        pk = _profile_pk()
        raw = _call("get_gear", pk) or []
        items = raw if isinstance(raw, list) else raw.get("gearList", [])
        defaults: dict[str, list] = {}
        try:
            dl = _call("get_gear_defaults", pk) or []
            for d in dl if isinstance(dl, list) else []:
                if d.get("defaultGear") and d.get("uuid"):
                    defaults.setdefault(d["uuid"], []).append(
                        d.get("activityTypePk") or d.get("activityTypeName"))
        except Exception:  # noqa: BLE001 — los defaults son opcionales
            pass
        out = []
        for x in items:
            status = (x.get("gearStatusName") or "").lower()
            if status == "retired" and not include_retired:
                continue
            uuid = x.get("uuid")
            stats = {}
            try:
                stats = _call("get_gear_stats", uuid) or {}
            except Exception:  # noqa: BLE001
                pass
            km = round((stats.get("totalDistance") or 0) / 1000.0, 1)
            max_km = round(x["maximumMeters"] / 1000.0) if x.get("maximumMeters") else None
            out.append({
                "gear_uuid": uuid,
                "name": x.get("displayName") or x.get("customMakeModel")
                        or f"{x.get('gearMakeName', '')} {x.get('gearModelName', '')}".strip(),
                "type": x.get("gearTypeName"),
                "status": x.get("gearStatusName"),
                "since": x.get("dateBegin"),
                "km": km,
                "activities": stats.get("totalActivities"),
                "max_km": max_km,
                "pct_used": round(100 * km / max_km) if max_km else None,
                "default_for": defaults.get(uuid),
            })
        out.sort(key=lambda g: g["km"] or 0, reverse=True)
        return {"count": len(out), "gear": out}
    except Exception as e:  # noqa: BLE001
        return _err(e)

@mcp.tool(name="garmin_set_activity_gear",
          annotations={"title": "Asociar/quitar equipo de una actividad", "readOnlyHint": False,
                       "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
def garmin_set_activity_gear(activity_id: str, gear_uuid: str, action: str = "add") -> dict:
    """Asocia (action='add') o quita (action='remove') un equipo de una actividad, p. ej.
    para corregir qué zapatilla se usó y que el kilometraje quede bien.
    Obtén gear_uuid con garmin_get_gear. Confirma con el usuario antes de llamar.

    Args:
        activity_id: ID de la actividad.
        gear_uuid: UUID del equipo.
        action: 'add' o 'remove'.
    """
    try:
        act = action.strip().lower()
        if act not in ("add", "remove"):
            return {"error": "action debe ser 'add' o 'remove'."}
        method = "add_gear_to_activity" if act == "add" else "remove_gear_from_activity"
        _call(method, gear_uuid, activity_id)
        return {"ok": True, "action": act, "activity_id": str(activity_id),
                "gear_uuid": gear_uuid}
    except Exception as e:  # noqa: BLE001
        return _err(e)

# ------------------------------------------------------------------ récords
@mcp.tool(name="garmin_get_personal_records",
          annotations={"title": "Récords personales", **_RO})
def garmin_get_personal_records(activity_type: str = "running") -> dict:
    """Récords personales. Para carrera: 1 km, milla, 5 km, 10 km, media maratón,
    maratón (tiempo y ritmo) y carrera más larga (distancia), con fecha y activity_id.

    Args:
        activity_type: 'running' (por defecto), 'cycling', 'swimming' o 'all'.
    """
    try:
        raw = _call("get_personal_record") or []
        items = raw if isinstance(raw, list) else raw.get("personalRecords", [])
        want = activity_type.strip().lower()
        out = []
        for r in items:
            atype = (r.get("activityType") or "").lower()
            if want != "all" and want not in atype:
                continue
            tid = r.get("typeId")
            val = r.get("value")
            when = (r.get("actStartDateTimeInGMTFormatted")
                    or r.get("prStartTimeGmtFormatted") or "")[:10] or None
            rec = {"activity_type": atype, "type_id": tid, "date": when,
                   "activity_id": r.get("activityId"), "activity_name": r.get("activityName")}
            if "running" in atype and tid in _RUN_PR:
                rec["record"] = _RUN_PR[tid]
                if tid == 7:
                    rec["distance_km"] = round(val / 1000.0, 2) if val else None
                else:
                    meters = {1: 1000, 2: 1609.344, 3: 5000, 4: 10000,
                              5: 21097.5, 6: 42195}[tid]
                    rec["time"] = _hms(val)
                    rec["pace"] = _pace_from(val, meters)
            else:
                rec["value"] = val  # tipos no documentados: valor crudo de Garmin
            out.append(rec)
        out.sort(key=lambda x: (x["activity_type"], x["type_id"] or 0))
        return {"count": len(out), "records": out}
    except Exception as e:  # noqa: BLE001
        return _err(e)

# ------------------------------------------------------------------ tendencias
@mcp.tool(name="garmin_get_trend", annotations={"title": "Tendencia de una métrica", **_RO})
def garmin_get_trend(metric: str, start_date: str, end_date: Optional[str] = None) -> dict:
    """Serie día a día de UNA métrica en un rango, con resumen (media, mín, máx,
    primero, último). Sirve para ver trayectorias: HRV de las últimas 2-4 semanas,
    FC en reposo, sueño, estrés, VO2max, aclimatación (SpO2/respiración), etc.

    Args:
        metric: hrv | sleep | resting_hr | stress | vo2max | spo2 | respiration |
                body_battery | intensity_minutes | hill_score | endurance_score.
        start_date: Fecha inicial ISO 'YYYY-MM-DD'.
        end_date: Fecha final ISO 'YYYY-MM-DD'. Por defecto hoy.

    Límites: las métricas diarias (hrv, sleep, resting_hr, stress, vo2max, spo2,
    respiration) hacen 1 llamada por día → máx. 31 días. body_battery,
    intensity_minutes (semanal), hill_score y endurance_score van en una sola
    llamada → hasta 366 días.
    """
    m = (metric or "").strip().lower()
    if m not in METRICS:
        return {"error": f"Métrica '{metric}' no soportada. Opciones: {', '.join(METRICS)}."}
    try:
        if m in _DAILY:
            d0, d1 = _range(start_date, end_date, _today(), MAX_DAYS)
            days, missing = [], []
            for iso in _days(d0, d1):
                try:
                    rec = _DAILY[m](_call, iso)
                    if _all_null(rec):
                        missing.append(iso)
                    else:
                        days.append({"date": iso, **rec})
                except Exception:  # noqa: BLE001 — día sin dato
                    missing.append(iso)
            out = {"metric": m, "start": d0.isoformat(), "end": d1.isoformat(),
                   "count": len(days), "days": days}
            if missing:
                out["missing_days"] = missing
            s = _summary(days, _SUMMARY_KEY[m])
            if s:
                out["summary"] = s
            return out

        d0, d1 = _range(start_date, end_date, _today(), MAX_RANGE_DAYS)
        a, b = d0.isoformat(), d1.isoformat()

        if m == "body_battery":
            raw = _call("get_body_battery", a, b) or []
            days = []
            for d in raw if isinstance(raw, list) else []:
                arr = d.get("bodyBatteryValuesArray") or []
                vals = [p[1] for p in arr if isinstance(p, list) and len(p) > 1
                        and p[1] is not None]
                days.append({"date": d.get("date") or d.get("calendarDate"),
                             "charged": d.get("charged"), "drained": d.get("drained"),
                             "max": max(vals) if vals else None,
                             "min": min(vals) if vals else None})
            out = {"metric": m, "start": a, "end": b, "count": len(days), "days": days}
            s = _summary(days, "max")
            if s:
                out["summary"] = s
            return out

        if m == "intensity_minutes":
            raw = _call("get_weekly_intensity_minutes", a, b) or []
            weeks = [{"week_start": w.get("calendarDate"),
                      "moderate": w.get("moderateValue"),
                      "vigorous": w.get("vigorousValue"),
                      "total_equiv": (w.get("moderateValue") or 0)
                      + 2 * (w.get("vigorousValue") or 0),
                      "goal": w.get("weeklyGoal")} for w in (raw or [])]
            return {"metric": m, "start": a, "end": b, "count": len(weeks),
                    "weeks": weeks,
                    "note": "total_equiv = moderados + 2×vigorosos (criterio de Garmin)"}

        method = "get_hill_score" if m == "hill_score" else "get_endurance_score"
        return {"metric": m, "start": a, "end": b, "data": _strip(_call(method, a, b))}
    except Exception as e:  # noqa: BLE001
        return _err(e)

# ------------------------------------------------------------------ Body Battery
@mcp.tool(name="garmin_get_body_battery_events",
          annotations={"title": "Eventos de Body Battery", **_RO})
def garmin_get_body_battery_events(date: Optional[str] = None) -> dict:
    """Qué cargó y qué drenó el Body Battery en un día: sueño, actividades, siestas y
    periodos detectados, con el impacto de cada uno y la hora de inicio.

    Args:
        date: Fecha ISO 'YYYY-MM-DD'. Por defecto hoy.
    """
    try:
        cdate = _iso(date).isoformat() if date else _today()
        raw = _call("get_body_battery_events", cdate) or []
        events = []
        for item in raw if isinstance(raw, list) else []:
            ev = item.get("event") or item
            dur = ev.get("durationInMilliseconds")
            events.append({
                "type": ev.get("eventType"),
                "start_gmt": ev.get("eventStartTimeGmt"),
                "tz_offset_ms": ev.get("timezoneOffset"),
                "duration_min": round(dur / 60000) if dur else None,
                "impact": ev.get("bodyBatteryImpact"),
                "feedback": ev.get("shortFeedback") or ev.get("feedbackType"),
            })
        return {"date": cdate, "count": len(events), "events": events}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# Herramientas — calendario (ver, agendar, desagendar)
# --------------------------------------------------------------------------- #
MAX_CAL_DAYS = 62


def _months(d0: _date, d1: _date):
    y, m = d0.year, d0.month
    while (y, m) <= (d1.year, d1.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


@mcp.tool(name="garmin_get_calendar",
          annotations={"title": "Calendario de entrenamientos", "readOnlyHint": True,
                       "destructiveHint": False, "idempotentHint": True,
                       "openWorldHint": True})
def garmin_get_calendar(start_date: Optional[str] = None, end_date: Optional[str] = None,
                        include_activities: bool = True) -> dict:
    """Entrenamientos agendados en el calendario de Garmin (lo que el reloj propondrá
    cada día) y, opcionalmente, las actividades realizadas, para comparar plan vs.
    hecho. Cada entrenamiento trae workout_id (la plantilla) y scheduled_id (la cita
    en el calendario, necesaria para garmin_unschedule_workout).

    Args:
        start_date: Fecha inicial ISO. Por defecto hoy.
        end_date: Fecha final ISO. Por defecto start_date + 13 días. Máx. 62 días.
        include_activities: Incluir actividades realizadas en el rango.
    """
    try:
        d0 = _iso(start_date) if start_date else _iso(_today())
        d1 = _iso(end_date) if end_date else d0 + timedelta(days=13)
        if d1 < d0:
            return {"error": "end_date no puede ser anterior a start_date."}
        if (d1 - d0).days + 1 > MAX_CAL_DAYS:
            return {"error": f"Rango demasiado amplio (máx. {MAX_CAL_DAYS} días)."}
        a, b = d0.isoformat(), d1.isoformat()
        planned, done = [], []
        for y, m in _months(d0, d1):
            data = _call("get_scheduled_workouts", y, m) or {}
            for it in data.get("calendarItems") or []:
                dt = (it.get("date") or "")[:10]
                if not (a <= dt <= b):
                    continue
                kind = it.get("itemType")
                if kind == "workout":
                    planned.append({"date": dt, "title": it.get("title"),
                                    "workout_id": it.get("workoutId"),
                                    "scheduled_id": it.get("id"),
                                    "sport": it.get("sportTypeKey")})
                elif kind == "activity" and include_activities:
                    dur = it.get("duration")
                    dist = it.get("distance")
                    # calendar-service usa ms y cm (verificado 24-sep-2026 con una
                    # actividad real: 3.005.280 ms = 50,1 min; 723.620 cm = 7,24 km).
                    done.append({"date": dt, "title": it.get("title"),
                                 "activity_id": it.get("id"),
                                 "duration_min": round(dur / 60000.0, 1) if dur else None,
                                 "distance_km": round(dist / 100000.0, 2) if dist else None})
        # La vista mensual de Garmin incluye días de los meses vecinos: deduplicar.
        planned = list({(x["scheduled_id"], x["date"]): x for x in planned}.values())
        done = list({(x["activity_id"], x["date"]): x for x in done}.values())
        planned.sort(key=lambda x: x["date"])
        done.sort(key=lambda x: x["date"])
        out = {"start": a, "end": b, "scheduled": planned}
        if include_activities:
            out["activities"] = done
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)

@mcp.tool(name="garmin_schedule_workout",
          annotations={"title": "Agendar entrenamiento existente", "readOnlyHint": False,
                       "destructiveHint": False, "idempotentHint": False,
                       "openWorldHint": True})
def garmin_schedule_workout(workout_id: str, date: str) -> dict:
    """Agenda en el calendario (y en el reloj) un entrenamiento que YA existe, sin
    recrearlo. Para MOVER una sesión: garmin_unschedule_workout de la cita vieja +
    esta herramienta con la fecha nueva. Obtén workout_id con garmin_get_calendar o
    garmin_inspect_workout_schema.

    Args:
        workout_id: ID de la plantilla del entrenamiento.
        date: Fecha ISO 'YYYY-MM-DD'.
    """
    try:
        d = _iso(date).isoformat()
        r = _call("schedule_workout", workout_id, d)
        try:
            r = r.json() if hasattr(r, "json") else r
        except Exception:  # noqa: BLE001
            r = None
        sid = (r or {}).get("workoutScheduleId") or (r or {}).get("id") \
            if isinstance(r, dict) else None
        return {"ok": True, "workout_id": str(workout_id), "date": d, "scheduled_id": sid}
    except Exception as e:  # noqa: BLE001
        return _err(e)

@mcp.tool(name="garmin_unschedule_workout",
          annotations={"title": "Quitar entrenamiento del calendario", "readOnlyHint": False,
                       "destructiveHint": True, "idempotentHint": True,
                       "openWorldHint": True})
def garmin_unschedule_workout(scheduled_id: str) -> dict:
    """Quita una cita del calendario (y del reloj) SIN borrar la plantilla del
    entrenamiento. scheduled_id sale de garmin_get_calendar (no es el workout_id).
    Confirma con el usuario antes de llamar.

    Args:
        scheduled_id: ID de la cita en el calendario.
    """
    try:
        _call("unschedule_workout", scheduled_id)
        return {"ok": True, "scheduled_id": str(scheduled_id)}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# /health (keep-alive de UptimeRobot; no expone datos)
# --------------------------------------------------------------------------- #
from starlette.requests import Request  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# --------------------------------------------------------------------------- #
# Arranque
# --------------------------------------------------------------------------- #


if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT, path=MCP_PATH)
