#!/usr/bin/env python3
"""
Atilex Briefing Engine v3.0
GHL Webhook → Cálculo exacto → Claude API → Nota CRM + Link de ficha HTML

Endpoints:
  GET  /                – Panel de test
  POST /webhook         – Recibe intake de GHL
  GET  /health          – Estado del servidor
  GET  /last            – Último payload recibido de GHL
  GET  /ficha/<id>      – Ficha visual HTML para el abogado

Railway: requiere variables de entorno ANTHROPIC_API_KEY, GHL_API_KEY,
         GHL_LOCATION_ID, BASE_URL (ej: https://mi-app.railway.app)
"""

import os, json, uuid, logging, sys
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify
import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── Configuración ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
GHL_API_KEY        = os.getenv("GHL_API_KEY", "")
GHL_LOCATION_ID    = os.getenv("GHL_LOCATION_ID", "")
GHL_DESPACHO_EMAIL = os.getenv("GHL_DESPACHO_EMAIL", "")
CLAUDE_MODEL       = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
BASE_URL           = os.getenv("BASE_URL", "").rstrip("/")   # ej: https://mi-app.railway.app
PORT               = int(os.getenv("PORT", 5000))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Logging → stdout (Railway captura stdout directamente) ─────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

# ── Almacenes en memoria ───────────────────────────────────────────────────────
_ultimo_payload: dict = {}
_fichas_store: dict   = {}   # {ficha_id: {"ficha": str, "datos": dict, "tipo": str, "ts": str}}


# ══════════════════════════════════════════════════════════════════════════════
# FESTIVOS – España nacional + Comunitat Valenciana
# ══════════════════════════════════════════════════════════════════════════════

def _pascua(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, mes, dia)

def get_festivos(year: int) -> set:
    pascua = _pascua(year)
    return {
        date(year, 1, 1),
        date(year, 1, 6),
        date(year, 3, 19),
        pascua - timedelta(days=2),
        pascua + timedelta(days=1),
        date(year, 5, 1),
        date(year, 10, 9),
        date(year, 10, 12),
        date(year, 11, 1),
        date(year, 12, 6),
        date(year, 12, 8),
        date(year, 12, 25),
    }

def _todos_festivos() -> set:
    hoy = date.today()
    return get_festivos(hoy.year) | get_festivos(hoy.year + 1)

FESTIVOS = _todos_festivos()

def es_habil(d: date) -> bool:
    return d.weekday() < 5 and d not in FESTIVOS

def fecha_limite_habiles(inicio: date, n_dias: int) -> date:
    d, contados = inicio, 0
    while contados < n_dias:
        d += timedelta(days=1)
        if es_habil(d):
            contados += 1
    return d

def dias_habiles_restantes(limite: date) -> int:
    hoy = date.today()
    if hoy >= limite:
        return 0
    contados, d = 0, hoy
    while d < limite:
        d += timedelta(days=1)
        if es_habil(d):
            contados += 1
    return contados


# ══════════════════════════════════════════════════════════════════════════════
# CÁLCULOS ECONÓMICOS
# ══════════════════════════════════════════════════════════════════════════════

def indem_improcedente(salario_anual: float, antiguedad: float) -> float:
    """33 días/año, máx 24 mensualidades."""
    s_dia = salario_anual / 365
    return round(min(s_dia * 33 * antiguedad, (salario_anual / 12) * 24), 2)

def indem_objetivo(salario_anual: float, antiguedad: float) -> float:
    """20 días/año, máx 12 mensualidades."""
    s_dia = salario_anual / 365
    return round(min(s_dia * 20 * antiguedad, (salario_anual / 12) * 12), 2)

def coste_ere(n: int, salario_medio: float, antiguedad_media: float):
    s_dia = salario_medio / 365
    return (
        round(s_dia * 20 * antiguedad_media * n, 2),
        round(s_dia * 33 * antiguedad_media * n, 2),
    )


# ══════════════════════════════════════════════════════════════════════════════
# SCORE DE URGENCIA
# ══════════════════════════════════════════════════════════════════════════════

def score_urgencia(dias: int, inspeccion: bool = False) -> str:
    if inspeccion or dias <= 5:
        return "CRITICO"
    if dias <= 10:
        return "URGENTE"
    if dias <= 20:
        return "NORMAL"
    return "FRIO"


# ══════════════════════════════════════════════════════════════════════════════
# SUGERENCIAS AUTOMÁTICAS
# ══════════════════════════════════════════════════════════════════════════════

def sugerencias_despido(d: dict) -> list:
    s = []
    firmo_finiquito = str(d.get("firmo_finiquito", "true")).lower() not in ("false", "no", "0")
    firmo_carta = str(d.get("firmo_carta", "true")).lower() not in ("false", "no", "0")
    baja_medica = str(d.get("baja_medica", "false")).lower() in ("true", "si", "sí", "1")

    if not firmo_finiquito:
        s.append("VENTAJA: NO firmó finiquito → no ha renunciado a ninguna acción. Alta prioridad.")
    else:
        s.append("Revisar si el finiquito incluye renuncia de acciones (puede limitar la reclamación).")
    if not firmo_carta:
        s.append("Sin carta de despido → posible despido verbal → calificación improcedente automática.")
    if baja_medica:
        s.append("Cliente en baja médica → valorar nulidad del despido por vulneración de derechos fundamentales.")
    if d.get("dias_habiles_restantes", 999) <= 5:
        s.append(f"⚠️ URGENTE: Presentar papeleta SMAC ANTES del {d.get('fecha_limite_smac', '?')}.")
    oferta = d.get("oferta_empresa", "") or d.get("oferta_recibida", "")
    if oferta and str(oferta).lower() not in ("no", "false", "0", ""):
        s.append(f"Empresa ha ofrecido: {oferta} → valorar si es conveniente o hay margen de mejora.")
    return s

def sugerencias_reclamacion(d: dict) -> list:
    s = []
    if str(d.get("sigue_en_empresa", "false")).lower() in ("true", "si", "sí", "1"):
        s.append("Cliente sigue en empresa → valorar si conviene reclamar en activo o esperar salida.")
    return s

def sugerencias_acoso(d: dict) -> list:
    s = []
    if str(d.get("baja_medica", "false")).lower() not in ("true", "si", "sí", "1"):
        s.append("Sin baja médica → valorar iniciar IT por ansiedad/estrés antes de actuar.")
    if str(d.get("denuncia_interna", "false")).lower() not in ("true", "si", "sí", "1"):
        s.append("Sin denuncia interna → recomendable como prueba de diligencia previa.")
    return s


# ══════════════════════════════════════════════════════════════════════════════
# PROMPTS POR PLANTILLA
# ══════════════════════════════════════════════════════════════════════════════

PROMPTS = {
    "despido": """\
Eres el asistente interno de Atilex, despacho laboral. Genera una ficha INTERNA para el abogado.
Los números ya están calculados – úsalos exactamente, sin condicionales ni aproximaciones.
La ficha de indemnización es SOLO para uso interno, nunca comunicar al cliente directamente.

DATOS DEL CASO (JSON):
{datos}

Genera la ficha con exactamente estas secciones y en este orden:

═══ FICHA DESPIDO – {nombre} ═══

DATOS DEL CLIENTE
Nombre: | Teléfono: | Horario de contacto: |

DATOS DEL DESPIDO
Empresa: | Sector: | Tipo de despido: | Fecha de despido: | Antigüedad: |
¿Firmó carta de despido?: | ¿Firmó finiquito?: | ¿Estaba de baja médica?: |
¿Hay sindicato?: | Oferta de la empresa: |

DOCUMENTACIÓN DISPONIBLE
(enumera la documentación que el cliente menciona tener)

CÁLCULO ECONÓMICO [USO INTERNO – CONFIDENCIAL]
Salario bruto anual: | Salario diario: |
Indemnización máxima (improcedente, 33d): | Indemnización objetivo negociación (20d): |

PLAZOS
Fecha límite SMAC: | Días hábiles restantes: | Urgencia: |

SUGERENCIAS PROCESALES
(lista las sugerencias recibidas; añade cualquier otra relevante según los datos del caso)
""",

    "reclamacion": """\
Eres el asistente interno de Atilex. Genera una ficha INTERNA para el abogado – caso de reclamación de cantidad.

DATOS DEL CASO (JSON):
{datos}

═══ FICHA RECLAMACIÓN DE CANTIDAD – {nombre} ═══

DATOS DEL CLIENTE
Nombre: | Teléfono: | Horario: |

DETALLE DE LA RECLAMACIÓN
Concepto reclamado: | Período afectado: | Importe aproximado: |
¿Sigue en empresa?: |

PLAZOS
Fecha devengo: | Fecha prescripción (1 año): | Meses restantes: | Urgencia: |

DOCUMENTACIÓN
Disponible: | Pendiente: |

SUGERENCIAS PROCESALES
""",

    "acoso": """\
Eres el asistente interno de Atilex. Genera una ficha INTERNA para el abogado – caso de acoso laboral.

DATOS DEL CASO (JSON):
{datos}

═══ FICHA ACOSO LABORAL – {nombre} ═══

DATOS DEL CLIENTE
Nombre: | Teléfono: | Horario: |

SITUACIÓN ACTUAL
¿Sigue en empresa?: | ¿Quién ejerce el acoso?: |
¿Tiene baja médica?: | ¿Denuncia interna presentada?: |

VÍAS POSIBLES (según situación)
(evalúa: denuncia interna, Inspección de Trabajo, art.50 ET, modificación sustancial)

EVIDENCIAS
Disponibles: | Pendientes: |

SUGERENCIAS PROCESALES
""",

    "empresa": """\
Eres el asistente interno de Atilex. Genera una ficha INTERNA para el abogado – cliente empresarial.

DATOS DEL CASO (JSON):
{datos}

═══ FICHA EMPRESA (ERE/ERTE/INSPECCIÓN) – {empresa} ═══

DATOS DE LA EMPRESA
Nombre: | Sector: | Contacto: | Cargo: | Teléfono directo: |

TIPO DE PROCEDIMIENTO
Tipo: | Empleados afectados: | ¿Inspección activa?: |

COSTE ESTIMADO [USO INTERNO – CONFIDENCIAL]
Mínimo ERE (20d): | Máximo ERE (33d): |

PLAZOS Y URGENCIA
Fecha límite: | Urgencia: |

DOCUMENTACIÓN MENCIONADA

SUGERENCIAS
""",
}


# ══════════════════════════════════════════════════════════════════════════════
# PROCESADORES POR TIPO
# ══════════════════════════════════════════════════════════════════════════════

def _safe_float(raw: dict, *keys) -> float:
    for key in keys:
        val = raw.get(key)
        if val is None or val == "" or val == 0:
            continue
        try:
            cleaned = str(val).strip().replace(",", ".")
            result = float(cleaned)
            if result > 0:
                return result
        except (ValueError, TypeError):
            continue
    return 0.0

def procesar_despido(raw: dict) -> dict:
    fecha_str = raw.get("fecha_despido", "")
    try:
        fecha_despido = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except Exception:
        fecha_despido = date.today()

    limite = fecha_limite_habiles(fecha_despido, 20)
    dias   = dias_habiles_restantes(limite)

    sal = _safe_float(raw, "salario_anual", "salario_bruto_anual", "salario")

    ant = _safe_float(
        raw,
        "antiguedad_ano",       # GHL envía sin 's' al nivel raíz
        "antiguedad_ar",
        "antiguedad_anos",
        "antiguedad_años",
        "antiguedad",
    )
    if not ant:
        custom = raw.get("customData") or {}
        ant = _safe_float(custom, "antiguedad_anos", "antiguedad_ano", "antiguedad_ar", "antiguedad")

    if not ant and fecha_despido:
        ant = round((date.today() - fecha_despido).days / 365.25, 1)
        logging.warning("FALLBACK antigüedad calculada desde fecha_despido: %.1f años", ant)

    logging.info("procesar_despido → sal=%.2f ant=%.2f raw_keys=%s", sal, ant, list(raw.keys()))

    d = {
        **raw,
        "salario_diario":               round(sal / 365, 2) if sal else 0,
        "indemnizacion_improcedente":   indem_improcedente(sal, ant),
        "indemnizacion_objetivo":       indem_objetivo(sal, ant),
        "fecha_limite_smac":            str(limite),
        "dias_habiles_restantes":       dias,
        "score_urgencia":               score_urgencia(dias),
        "_debug_sal_recibido":          raw.get("salario_anual") or raw.get("salario_bruto_anual"),
        "_debug_ant_recibido":          raw.get("antiguedad_ano") or raw.get("antiguedad_ar") or raw.get("antiguedad_anos"),
    }
    d["sugerencias"] = sugerencias_despido(d)
    return d

def procesar_reclamacion(raw: dict) -> dict:
    fecha_str = raw.get("fecha_devengo", "")
    try:
        fd = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except Exception:
        fd = date.today()

    prescripcion = date(fd.year + 1, fd.month, fd.day)
    meses = max(0, (prescripcion - date.today()).days // 30)

    d = {
        **raw,
        "fecha_prescripcion": str(prescripcion),
        "meses_restantes":    meses,
        "score_urgencia":     score_urgencia(meses * 20),
    }
    d["sugerencias"] = sugerencias_reclamacion(d)
    return d

def procesar_acoso(raw: dict) -> dict:
    d = {**raw, "score_urgencia": "NORMAL"}
    d["sugerencias"] = sugerencias_acoso(d)
    return d

def procesar_empresa(raw: dict) -> dict:
    n   = int(raw.get("empleados_afectados", 0) or 0)
    sal = _safe_float(raw, "salario_medio_anual")
    ant = _safe_float(raw, "antiguedad_media_anos", "antiguedad_media_años")
    ins = str(raw.get("inspeccion_activa", "false")).lower() in ("true", "si", "sí", "1")
    cmin, cmax = coste_ere(n, sal, ant) if sal else (0, 0)
    return {
        **raw,
        "coste_estimado_minimo_ere": cmin,
        "coste_estimado_maximo_ere": cmax,
        "score_urgencia":            score_urgencia(999, inspeccion=ins),
    }

PROCESADORES = {
    "despido":     procesar_despido,
    "reclamacion": procesar_reclamacion,
    "acoso":       procesar_acoso,
    "empresa":     procesar_empresa,
}


# ══════════════════════════════════════════════════════════════════════════════
# LLAMADA A CLAUDE
# ══════════════════════════════════════════════════════════════════════════════

def generar_ficha_claude(datos: dict, tipo: str) -> str:
    nombre = datos.get("nombre") or datos.get("empresa") or "Sin nombre"
    prompt = PROMPTS[tipo].format(
        datos=json.dumps(datos, ensure_ascii=False, indent=2),
        nombre=nombre,
        empresa=nombre,
    )
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1800,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# ══════════════════════════════════════════════════════════════════════════════
# GHL – CREAR NOTA EN CRM
# ══════════════════════════════════════════════════════════════════════════════

def crear_nota_ghl(contact_id: str, ficha: str, score: str, ficha_url: str = ""):
    if not GHL_API_KEY or not contact_id:
        return None
    url = f"https://services.leadconnectorhq.com/contacts/{contact_id}/notes"
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }
    cuerpo = f"[{score}]\n\n{ficha}"
    if ficha_url:
        cuerpo += f"\n\n📋 Ficha completa: {ficha_url}"
    body = {"body": cuerpo, "userId": ""}
    r = requests.post(url, headers=headers, json=body, timeout=10)
    return r.json() if r.ok else {"error": r.text}


# ══════════════════════════════════════════════════════════════════════════════
# ALMACÉN DE FICHAS  +  RENDER HTML
# ══════════════════════════════════════════════════════════════════════════════

def guardar_ficha(ficha_text: str, datos: dict, tipo: str) -> str:
    ficha_id = str(uuid.uuid4())[:8]
    _fichas_store[ficha_id] = {
        "ficha": ficha_text,
        "datos": datos,
        "tipo":  tipo,
        "ts":    datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    return ficha_id

def _fmt_eur(val) -> str:
    try:
        return f"{float(val):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(val) if val else "-"

def _score_badge(score: str) -> str:
    colors = {
        "CRITICO": ("#dc2626", "#fef2f2"),
        "URGENTE": ("#d97706", "#fffbeb"),
        "NORMAL":  ("#2563eb", "#eff6ff"),
        "FRIO":    ("#059669", "#f0fdf4"),
    }
    color, bg = colors.get(score, ("#64748b", "#f8fafc"))
    return (
        f'<span style="display:inline-block;padding:6px 18px;border-radius:20px;'
        f'font-weight:700;font-size:15px;color:{color};background:{bg};'
        f'border:2px solid {color}">{score}</span>'
    )

def render_ficha_html(datos: dict, ficha_text: str, tipo: str, ts: str) -> str:
    nombre = datos.get("nombre") or datos.get("empresa") or "Cliente"
    score  = datos.get("score_urgencia", "NORMAL")
    tipo_label = {
        "despido":     "Despido Laboral",
        "reclamacion": "Reclamación de Cantidad",
        "acoso":       "Acoso Laboral",
        "empresa":     "Asesoría Empresarial",
    }.get(tipo, tipo.capitalize())

    # Convertir saltos de línea y líneas horizontales del texto de ficha
    ficha_html = (
        ficha_text
        .replace("═══", "<hr class='sec-hr'>")
        .replace("\n", "<br>")
        .replace("CÁLCULO ECONÓMICO [USO INTERNO – CONFIDENCIAL]",
                 '<span class="confidential-label">🔒 CÁLCULO ECONÓMICO — CONFIDENCIAL</span>')
        .replace("[USO INTERNO]",
                 '<span class="confidential-label">🔒 USO INTERNO</span>')
    )

    indem_imp = _fmt_eur(datos.get("indemnizacion_improcedente", 0))
    indem_obj = _fmt_eur(datos.get("indemnizacion_objetivo", 0))
    fecha_lim = datos.get("fecha_limite_smac", "-")
    dias_hab  = datos.get("dias_habiles_restantes", "-")
    sal_anual = _fmt_eur(datos.get("salario_anual") or datos.get("salario_bruto_anual", 0))

    # Tarjetas de datos clave (solo para despido)
    kpi_cards = ""
    if tipo == "despido" and datos.get("indemnizacion_improcedente"):
        kpi_cards = f"""
        <div class="kpi-row">
          <div class="kpi-card">
            <div class="kpi-label">🔒 Indemnización máxima</div>
            <div class="kpi-value">{indem_imp}</div>
            <div class="kpi-sub">33 días/año – USO INTERNO</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">🎯 Objetivo negociación</div>
            <div class="kpi-value">{indem_obj}</div>
            <div class="kpi-sub">20 días/año</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">📅 Límite SMAC</div>
            <div class="kpi-value kpi-date">{fecha_lim}</div>
            <div class="kpi-sub">{dias_hab} días hábiles restantes</div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ficha {tipo_label} – {nombre}</title>
<style>
  :root {{
    --azul:    #1a3c6e;
    --azul-l:  #2451a0;
    --gris-bg: #f0f4f9;
    --gris-bd: #d0d8e4;
    --rojo:    #dc2626;
    --texto:   #1e293b;
    --muted:   #64748b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: var(--gris-bg);
    color: var(--texto);
    padding: 0;
  }}
  /* HEADER */
  .header {{
    background: var(--azul);
    color: white;
    padding: 24px 32px 20px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }}
  .header-left {{ flex: 1; }}
  .logo-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
  .logo-text {{ font-size: 22px; font-weight: 800; letter-spacing: 1px; }}
  .logo-sub  {{ font-size: 11px; opacity: .7; letter-spacing: 2px; text-transform: uppercase; }}
  .case-title {{ font-size: 18px; font-weight: 300; opacity: .9; }}
  .case-name  {{ font-size: 26px; font-weight: 700; line-height: 1.2; }}
  .header-right {{ text-align: right; }}
  .ts {{ font-size: 11px; opacity: .6; margin-top: 4px; }}
  /* CONTENT */
  .content {{ max-width: 860px; margin: 0 auto; padding: 28px 20px 60px; }}
  /* KPI CARDS */
  .kpi-row {{
    display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap;
  }}
  .kpi-card {{
    flex: 1; min-width: 200px;
    background: white;
    border: 1px solid var(--gris-bd);
    border-top: 4px solid var(--azul);
    border-radius: 8px;
    padding: 16px 20px;
  }}
  .kpi-label {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }}
  .kpi-value {{ font-size: 24px; font-weight: 700; color: var(--azul); line-height: 1.1; }}
  .kpi-date  {{ font-size: 20px; }}
  .kpi-sub   {{ font-size: 11px; color: var(--muted); margin-top: 4px; }}
  /* URGENCY */
  .urgency-bar {{
    background: white;
    border: 1px solid var(--gris-bd);
    border-radius: 8px;
    padding: 16px 20px;
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 24px;
  }}
  .urgency-label {{ font-weight: 600; color: var(--muted); font-size: 13px; }}
  /* FICHA TEXTO */
  .ficha-card {{
    background: white;
    border: 1px solid var(--gris-bd);
    border-radius: 8px;
    padding: 28px 32px;
    line-height: 1.8;
    font-size: 14px;
  }}
  .ficha-card .sec-hr {{
    border: none;
    border-top: 2px solid var(--gris-bd);
    margin: 20px 0;
  }}
  .confidential-label {{
    display: inline-block;
    background: #fef3c7;
    color: #92400e;
    font-weight: 700;
    font-size: 12px;
    padding: 2px 10px;
    border-radius: 4px;
    border: 1px solid #fcd34d;
    margin-bottom: 4px;
  }}
  /* FOOTER */
  .footer {{
    text-align: center;
    color: var(--muted);
    font-size: 12px;
    padding: 20px;
    margin-top: 32px;
    border-top: 1px solid var(--gris-bd);
  }}
  @media (max-width: 600px) {{
    .header {{ padding: 16px; }}
    .case-name {{ font-size: 20px; }}
    .kpi-value {{ font-size: 20px; }}
    .ficha-card {{ padding: 16px; }}
  }}
  @media print {{
    body {{ background: white; }}
    .header {{ background: var(--azul) !important; -webkit-print-color-adjust: exact; }}
  }}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <div class="logo-row">
      <span class="logo-text">ATILEX</span>
      <span class="logo-sub">Asesoría Laboral</span>
    </div>
    <div class="case-title">Ficha interna – {tipo_label}</div>
    <div class="case-name">{nombre}</div>
  </div>
  <div class="header-right">
    <div>Documento interno</div>
    <div class="ts">Generado: {ts}</div>
  </div>
</div>

<div class="content">
  <div class="urgency-bar">
    <span class="urgency-label">NIVEL DE URGENCIA:</span>
    {_score_badge(score)}
  </div>

  {kpi_cards}

  <div class="ficha-card">
    {ficha_html}
  </div>
</div>

<div class="footer">
  Atilex Asesoría Laboral · Documento de uso interno · No distribuir al cliente
</div>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS FLASK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":     "ok",
        "version":    "3.0",
        "modelo":     CLAUDE_MODEL,
        "api_key_ok": bool(ANTHROPIC_API_KEY),
        "ghl_key_ok": bool(GHL_API_KEY),
        "base_url":   BASE_URL or "(local)",
    })

@app.route("/last", methods=["GET"])
def last_payload():
    return jsonify(_ultimo_payload or {"info": "Todavía no se ha recibido ningún webhook de GHL."})

@app.route("/ficha/<ficha_id>", methods=["GET"])
def ver_ficha(ficha_id: str):
    entry = _fichas_store.get(ficha_id)
    if not entry:
        return (
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>Ficha no encontrada</h2>"
            "<p>Este enlace ha expirado o no existe. El servidor reinicia periódicamente.</p>"
            "</body></html>",
            404,
        )
    return render_ficha_html(
        entry["datos"],
        entry["ficha"],
        entry["tipo"],
        entry["ts"],
    )

@app.route("/webhook", methods=["POST"])
def webhook():
    global _ultimo_payload
    raw = request.json or {}
    _ultimo_payload = {**raw, "_recibido_en": datetime.now().isoformat()}
    logging.info("WEBHOOK recibido: %s", json.dumps(raw, ensure_ascii=False))

    tipo = (raw.get("tipo_caso") or raw.get("tipo") or "despido").lower().strip()
    if tipo not in PROCESADORES:
        return jsonify({"error": f"tipo_caso '{tipo}' desconocido. Válidos: {list(PROCESADORES)}"}), 400

    datos    = PROCESADORES[tipo](raw)
    ficha    = generar_ficha_claude(datos, tipo)

    # Guardar ficha y generar URL
    ficha_id  = guardar_ficha(ficha, datos, tipo)
    base      = BASE_URL or request.host_url.rstrip("/")
    ficha_url = f"{base}/ficha/{ficha_id}"
    logging.info("Ficha guardada: %s", ficha_url)

    contact_id = raw.get("contact_id") or raw.get("contactId") or ""
    nota_ghl   = crear_nota_ghl(contact_id, ficha, datos.get("score_urgencia", ""), ficha_url)

    return jsonify({
        "ok":             True,
        "tipo_caso":      tipo,
        "score_urgencia": datos.get("score_urgencia", ""),
        "ficha_url":      ficha_url,
        "ficha":          ficha,
        "datos":          datos,
        "nota_ghl":       nota_ghl,
    })


# ══════════════════════════════════════════════════════════════════════════════
# PANEL WEB
# ══════════════════════════════════════════════════════════════════════════════

PANEL_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Atilex Briefing Engine v3 – Panel</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; max-width: 920px; margin: 40px auto; padding: 0 20px; background: #f0f4f9; color: #1e293b; }
  h1 { color: #1a3c6e; margin-bottom: 4px; }
  .sub { color: #64748b; font-size: 13px; margin-bottom: 24px; }
  .card { background: white; border-radius: 8px; padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 6px rgba(0,0,0,.08); }
  .status { display: flex; gap: 10px; flex-wrap: wrap; }
  .badge { padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; }
  .ok   { background: #d4edda; color: #155724; }
  .fail { background: #f8d7da; color: #721c24; }
  label { font-weight: 600; display: block; margin-top: 12px; margin-bottom: 4px; font-size: 13px; }
  input, select { width: 100%; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 14px; box-sizing: border-box; }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .row3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
  button.primary { margin-top: 20px; padding: 12px 28px; background: #1a3c6e; color: white; border: none; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; }
  button.primary:hover { background: #0f2548; }
  button.primary:disabled { background: #94a3b8; cursor: wait; }
  #resultado { white-space: pre-wrap; font-family: monospace; font-size: 13px; background: #1e1e1e; color: #d4d4d4; padding: 20px; border-radius: 6px; display: none; margin-top: 16px; max-height: 500px; overflow-y: auto; }
  .tab-bar { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
  .tab { padding: 8px 18px; border: 2px solid #1a3c6e; border-radius: 4px; cursor: pointer; font-weight: 600; color: #1a3c6e; background: white; font-size: 14px; }
  .tab.active { background: #1a3c6e; color: white; }
  .section { display: none; }
  .section.active { display: block; }
  .ficha-link { margin-top: 12px; padding: 12px 16px; background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; font-size: 13px; }
  .ficha-link a { color: #1d4ed8; font-weight: 600; }
  .sep { border: none; border-top: 1px solid #e2e8f0; margin: 16px 0; }
</style>
</head>
<body>
<h1>🏛️ Atilex Briefing Engine <small style="font-size:14px;color:#64748b">v3.0</small></h1>
<p class="sub">Panel interno · Railway Cloud</p>

<div class="card">
  <strong>Estado del servidor</strong>
  <div class="status" id="status-badges" style="margin-top:10px"><span class="badge" style="background:#e2e8f0">Comprobando…</span></div>
</div>

<div class="card">
  <div class="tab-bar">
    <button class="tab active" onclick="switchTab('despido',this)">Despido</button>
    <button class="tab" onclick="switchTab('reclamacion',this)">Reclamación</button>
    <button class="tab" onclick="switchTab('ghl',this)">Último GHL payload</button>
  </div>

  <!-- DESPIDO -->
  <div id="tab-despido" class="section active">
    <div class="row2">
      <div><label>Nombre</label><input id="d_nombre" value="Omar Gil (TEST)"/></div>
      <div><label>Teléfono</label><input id="d_telefono" value="600123456"/></div>
    </div>
    <div class="row2">
      <div><label>Empresa</label><input id="d_empresa" value="Empresa Ejemplo S.L."/></div>
      <div><label>Sector</label><input id="d_sector" value="Comercio"/></div>
    </div>
    <div class="row3">
      <div><label>Fecha despido</label><input id="d_fecha" value="2024-09-01"/></div>
      <div><label>Salario bruto anual (€)</label><input id="d_salario" type="number" value="28000"/></div>
      <div><label>Antigüedad (años)</label><input id="d_antiguedad" type="number" step="0.1" value="5"/></div>
    </div>
    <div class="row3">
      <div>
        <label>Tipo de despido</label>
        <select id="d_tipo_despido">
          <option value="Objetivo">Objetivo</option>
          <option value="Disciplinario">Disciplinario</option>
          <option value="Colectivo (ERE)">Colectivo (ERE)</option>
          <option value="Verbal">Verbal</option>
        </select>
      </div>
      <div>
        <label>¿Firmó carta?</label>
        <select id="d_carta"><option value="true">Sí</option><option value="false">No</option></select>
      </div>
      <div>
        <label>¿Firmó finiquito?</label>
        <select id="d_finiquito"><option value="false">No</option><option value="true">Sí</option></select>
      </div>
    </div>
    <div class="row3">
      <div>
        <label>¿Baja médica?</label>
        <select id="d_baja"><option value="false">No</option><option value="true">Sí</option></select>
      </div>
      <div>
        <label>¿Hay sindicato?</label>
        <select id="d_sindicato"><option value="false">No</option><option value="true">Sí</option></select>
      </div>
      <div><label>Horario contacto</label><input id="d_horario" value="Mañanas"/></div>
    </div>
    <div class="row2">
      <div><label>Documentación disponible</label><input id="d_docs" value="Contrato, nóminas, carta de despido"/></div>
      <div><label>Oferta empresa</label><input id="d_oferta" value="" placeholder="Ninguna / importe…"/></div>
    </div>
    <hr class="sep"/>
    <div><label>Contact ID GHL (opcional)</label><input id="d_contact" value="" placeholder="Deja vacío para no crear nota en GHL"/></div>
    <button class="primary" id="btn-despido" onclick="testDespido()">▶ Generar Ficha de Despido</button>
  </div>

  <!-- RECLAMACIÓN -->
  <div id="tab-reclamacion" class="section">
    <div class="row2">
      <div><label>Nombre</label><input id="r_nombre" value="Prueba Reclamación"/></div>
      <div><label>Teléfono</label><input id="r_tel" value="600000000"/></div>
    </div>
    <div class="row2">
      <div><label>Concepto</label><input id="r_concepto" value="Horas extra no pagadas"/></div>
      <div><label>Importe aproximado (€)</label><input id="r_importe" type="number" value="3500"/></div>
    </div>
    <div class="row2">
      <div><label>Fecha devengo</label><input id="r_fecha" value="2024-01-01"/></div>
      <div>
        <label>¿Sigue en empresa?</label>
        <select id="r_sigue"><option value="true">Sí</option><option value="false">No</option></select>
      </div>
    </div>
    <button class="primary" id="btn-reclamacion" onclick="testReclamacion()">▶ Generar Ficha de Reclamación</button>
  </div>

  <!-- ÚLTIMO PAYLOAD GHL -->
  <div id="tab-ghl" class="section">
    <p style="color:#64748b;font-size:14px;margin-bottom:16px">
      Muestra exactamente lo que GHL envió en el último webhook. Útil para verificar qué campos llegan.
    </p>
    <button class="primary" onclick="verUltimoPayload()">🔍 Ver último payload de GHL</button>
  </div>

  <div id="ficha-link-box" class="ficha-link" style="display:none">
    📋 <strong>Ficha visual:</strong> <a id="ficha-link-a" href="#" target="_blank">Abrir ficha completa</a>
  </div>

  <div id="resultado"></div>
</div>

<script>
async function checkHealth() {
  try {
    const r = await fetch('/health');
    const d = await r.json();
    document.getElementById('status-badges').innerHTML = [
      d.api_key_ok ? '<span class="badge ok">✅ Claude API</span>' : '<span class="badge fail">❌ Claude API sin clave</span>',
      d.ghl_key_ok ? '<span class="badge ok">✅ GHL API</span>' : '<span class="badge fail">⚠️ GHL sin clave</span>',
      '<span class="badge ok">✅ Servidor · ' + d.modelo + '</span>',
      '<span class="badge ok">🌐 ' + (d.base_url || 'local') + '</span>',
    ].join('');
  } catch(e) {
    document.getElementById('status-badges').innerHTML = '<span class="badge fail">❌ Servidor no responde</span>';
  }
}

function switchTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  document.getElementById('resultado').style.display = 'none';
  document.getElementById('ficha-link-box').style.display = 'none';
}

async function callWebhook(payload, btnId) {
  const btn = document.getElementById(btnId);
  const out = document.getElementById('resultado');
  btn.disabled = true;
  btn.textContent = '⏳ Generando con Claude…';
  out.style.display = 'none';
  document.getElementById('ficha-link-box').style.display = 'none';
  try {
    const r = await fetch('/webhook', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.ficha) {
      out.textContent = d.ficha +
        '\\n\\n--- DATOS CALCULADOS ---\\n' +
        'Indemnización improcedente: ' + (d.datos.indemnizacion_improcedente||0).toLocaleString('es-ES',{style:'currency',currency:'EUR'}) + '\\n' +
        'Indemnización objetivo:     ' + (d.datos.indemnizacion_objetivo||0).toLocaleString('es-ES',{style:'currency',currency:'EUR'}) + '\\n' +
        'Score urgencia:             ' + d.datos.score_urgencia + '\\n' +
        'Días hábiles restantes:     ' + (d.datos.dias_habiles_restantes||'-') + '\\n' +
        (d.nota_ghl ? '\\nNota GHL: creada ✅' : '');
    } else {
      out.textContent = JSON.stringify(d, null, 2);
    }
    out.style.display = 'block';
    if (d.ficha_url) {
      document.getElementById('ficha-link-a').href = d.ficha_url;
      document.getElementById('ficha-link-a').textContent = d.ficha_url;
      document.getElementById('ficha-link-box').style.display = 'block';
    }
  } catch(e) {
    out.textContent = 'Error: ' + e.message;
    out.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = '▶ Generar ficha';
}

function testDespido() {
  callWebhook({
    tipo_caso:             'despido',
    nombre:                document.getElementById('d_nombre').value,
    telefono:              document.getElementById('d_telefono').value,
    nombre_empresa:        document.getElementById('d_empresa').value,
    empresa:               document.getElementById('d_empresa').value,
    sector:                document.getElementById('d_sector').value,
    fecha_despido:         document.getElementById('d_fecha').value,
    salario_anual:         parseFloat(document.getElementById('d_salario').value)||0,
    antiguedad_ar:         parseFloat(document.getElementById('d_antiguedad').value)||0,
    tipo_despido:          document.getElementById('d_tipo_despido').value,
    firmo_carta:           document.getElementById('d_carta').value,
    firmo_finiquito:       document.getElementById('d_finiquito').value,
    baja_medica:           document.getElementById('d_baja').value,
    sindicato:             document.getElementById('d_sindicato').value,
    horario_contacto:      document.getElementById('d_horario').value,
    documentacion_disponible: document.getElementById('d_docs').value,
    oferta_empresa:        document.getElementById('d_oferta').value,
    contact_id:            document.getElementById('d_contact').value,
  }, 'btn-despido');
}

function testReclamacion() {
  callWebhook({
    tipo_caso:        'reclamacion',
    nombre:           document.getElementById('r_nombre').value,
    telefono:         document.getElementById('r_tel').value,
    concepto:         document.getElementById('r_concepto').value,
    importe:          parseFloat(document.getElementById('r_importe').value)||0,
    fecha_devengo:    document.getElementById('r_fecha').value,
    sigue_en_empresa: document.getElementById('r_sigue').value,
  }, 'btn-reclamacion');
}

async function verUltimoPayload() {
  const out = document.getElementById('resultado');
  const r = await fetch('/last');
  const d = await r.json();
  out.textContent = JSON.stringify(d, null, 2);
  out.style.display = 'block';
}

checkHealth();
</script>
</body>
</html>"""

@app.route("/", methods=["GET"])
def panel():
    return PANEL_HTML


# ══════════════════════════════════════════════════════════════════════════════
# ARRANQUE
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Atilex Briefing Engine v3.0")
    print(f"  Panel → http://localhost:{PORT}")
    print(f"  POST /webhook  GET /health  GET /last  GET /ficha/<id>")
    print("=" * 60)
    if ANTHROPIC_API_KEY:
        print("✅  ANTHROPIC_API_KEY cargada")
    else:
        print("⚠️  ANTHROPIC_API_KEY no configurada")
    if GHL_API_KEY:
        print("✅  GHL_API_KEY cargada")
    else:
        print("⚠️  GHL_API_KEY no configurada")
    if BASE_URL:
        print(f"🌐  BASE_URL = {BASE_URL}")
    app.run(debug=False, host="0.0.0.0", port=PORT)
