"""
Atilex Briefing Engine v4
- Compatible con v3 (campos individuales)
- NUEVO: acepta nota_lidia (texto de la nota de Lidia) y extrae datos con Claude
- Endpoint GET /ficha/<id> → HTML visual
"""

import os, json, uuid, logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
import anthropic
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
fichas_store = {}  # {id: {data, html, timestamp}}

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GHL_API_KEY = os.environ.get("GHL_API_KEY", "")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID", "")
GHL_DESPACHO_EMAIL = os.environ.get("GHL_DESPACHO_EMAIL", "despacho@atilex.es")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _safe_float(data, *keys):
    for k in keys:
        v = data.get(k)
        if v not in (None, "", "--"):
            try:
                return float(str(v).replace("€","").replace(".","").replace(",",".").strip())
            except:
                pass
    return 0.0

def _safe_str(data, *keys):
    for k in keys:
        v = data.get(k)
        if v and str(v).strip() not in ("", "--", "None"):
            return str(v).strip()
    return ""

def extraer_datos_de_nota(nota_texto):
    """Usa Claude para extraer datos estructurados del texto de la nota de Lidia."""
    prompt = f"""Extrae los datos del siguiente caso laboral del texto de la nota de un agente de intake.
Devuelve SOLO un JSON con estos campos exactos (usa null si no está disponible):

{{
  "nombre": "...",
  "telefono": "...",
  "email": "...",
  "tipo_despido": "disciplinario|objetivo|colectivo_ere|verbal|no_renovacion",
  "fecha_despido": "DD/MM/YYYY o null",
  "antiguedad_ano": número_decimal,
  "salario_bruto": número_anual_euros,
  "firmo_carta": true|false|null,
  "firmo_finiquito": true|false|null,
  "nombre_empresa": "...",
  "sector": "...",
  "baja_medica": true|false|null,
  "sindicato": true|false|null,
  "oferta_empresa": "...",
  "tipo_contrato": "indefinido|temporal|null",
  "documentacion": "...",
  "horario_contacto": "..."
}}

Nota del agente:
{nota_texto}

JSON extraído:"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = response.content[0].text.strip()
        # Extraer JSON si viene con texto extra
        inicio = texto.find("{")
        fin = texto.rfind("}") + 1
        if inicio >= 0 and fin > inicio:
            return json.loads(texto[inicio:fin])
    except Exception as e:
        logger.error(f"Error extrayendo datos de nota: {e}")
    return {}

def calcular_indemnizacion(datos):
    """Calcula indemnización y plazos según datos del caso."""
    tipo = str(datos.get("tipo_despido", "")).lower()
    antiguedad = _safe_float(datos, "antiguedad_ano")
    salario_bruto = _safe_float(datos, "salario_bruto")
    firmo_carta = datos.get("firmo_carta")
    firmo_finiquito = datos.get("firmo_finiquito")
    oferta = _safe_str(datos, "oferta_empresa")

    salario_diario = salario_bruto / 365 if salario_bruto > 0 else 0

    # Días según tipo de despido
    dias_legales = {
        "disciplinario": 33,
        "objetivo": 20,
        "colectivo_ere": 20,
        "verbal": 33,  # Se presume improcedente
        "no_renovacion": 12,
    }
    dias = dias_legales.get(tipo, 33)
    tope_meses = 24 if tipo in ("disciplinario", "verbal") else 12

    indemnizacion_legal = round(salario_diario * dias * min(antiguedad, tope_meses * 30 / 365 * (antiguedad / (tope_meses * 30 / 365)) if antiguedad > 0 else 1), 2)
    # Simplificado: salario_diario * dias * años (tope 24 meses para improcedente)
    tope_dias = tope_meses * 30
    dias_total = min(dias * antiguedad, tope_dias)
    indemnizacion_legal = round(salario_diario * dias_total, 2)

    # Plazos
    fecha_str = _safe_str(datos, "fecha_despido")
    fecha_despido = None
    plazo_demanda = None
    plazo_conciliacion = None
    dias_restantes = None

    if fecha_str:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                fecha_despido = datetime.strptime(fecha_str, fmt)
                break
            except:
                pass

    if fecha_despido:
        plazo_conciliacion = fecha_despido + timedelta(days=20)
        plazo_demanda = fecha_despido + timedelta(days=20)
        dias_restantes = (plazo_demanda - datetime.now()).days

    urgencia = "BAJA"
    if dias_restantes is not None:
        if dias_restantes <= 5:
            urgencia = "CRÍTICA"
        elif dias_restantes <= 10:
            urgencia = "ALTA"
        elif dias_restantes <= 15:
            urgencia = "MEDIA"

    return {
        "indemnizacion_legal": indemnizacion_legal,
        "salario_diario": round(salario_diario, 2),
        "dias_indemnizacion": round(dias_total, 1),
        "plazo_demanda": plazo_demanda.strftime("%d/%m/%Y") if plazo_demanda else "Sin fecha",
        "plazo_conciliacion": plazo_conciliacion.strftime("%d/%m/%Y") if plazo_conciliacion else "Sin fecha",
        "dias_restantes": dias_restantes,
        "urgencia": urgencia,
        "firmo_carta": firmo_carta,
        "firmo_finiquito": firmo_finiquito,
        "oferta_empresa": oferta,
    }

def generar_ficha_html(datos, calculos, ficha_id):
    """Genera HTML visual de la ficha para el abogado."""

    urgencia = calculos.get("urgencia", "BAJA")
    colores_urgencia = {
        "CRÍTICA": ("#dc2626", "#fef2f2"),
        "ALTA": ("#ea580c", "#fff7ed"),
        "MEDIA": ("#d97706", "#fffbeb"),
        "BAJA": ("#16a34a", "#f0fdf4"),
    }
    color_badge, color_bg = colores_urgencia.get(urgencia, ("#6b7280", "#f9fafb"))

    nombre = _safe_str(datos, "nombre") or "Sin nombre"
    telefono = _safe_str(datos, "telefono") or "—"
    email = _safe_str(datos, "email") or "—"
    empresa = _safe_str(datos, "nombre_empresa") or "—"
    sector = _safe_str(datos, "sector") or "—"
    tipo_despido = _safe_str(datos, "tipo_despido") or "—"
    antiguedad = datos.get("antiguedad_ano") or "—"
    salario = datos.get("salario_bruto") or "—"
    horario = _safe_str(datos, "horario_contacto") or "—"
    documentacion = _safe_str(datos, "documentacion") or "—"
    tipo_contrato = _safe_str(datos, "tipo_contrato") or "—"

    firmo_carta = datos.get("firmo_carta")
    firmo_finiquito = datos.get("firmo_finiquito")
    baja_medica = datos.get("baja_medica")
    sindicato = datos.get("sindicato")

    def bool_badge(val):
        if val is True or str(val).lower() in ("true", "si", "sí", "yes", "1"):
            return '<span style="color:#16a34a;font-weight:600">✅ Sí</span>'
        elif val is False or str(val).lower() in ("false", "no", "0"):
            return '<span style="color:#dc2626;font-weight:600">❌ No</span>'
        return '<span style="color:#6b7280">—</span>'

    fecha_gen = datetime.now().strftime("%d/%m/%Y %H:%M")
    dias_rest = calculos.get("dias_restantes")
    dias_str = f"{dias_rest} días" if dias_rest is not None else "—"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ficha Laboral — {nombre}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f1f5f9; color: #1e293b; padding: 24px; }}
  .container {{ max-width: 760px; margin: 0 auto; }}
  .header {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 16px;
             border-left: 6px solid {color_badge}; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
  .urgencia-badge {{ display: inline-block; background: {color_badge}; color: white;
                     padding: 4px 14px; border-radius: 20px; font-size: 12px;
                     font-weight: 700; letter-spacing: 1px; margin-bottom: 10px; }}
  h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 13px; }}
  .card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card h2 {{ font-size: 14px; font-weight: 700; text-transform: uppercase;
              letter-spacing: .5px; color: #64748b; margin-bottom: 14px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .field {{ }}
  .field label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase;
                  letter-spacing: .4px; display: block; margin-bottom: 2px; }}
  .field value {{ font-size: 15px; font-weight: 500; display: block; }}
  .eco-grid {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; }}
  .eco-card {{ background: #f8fafc; border-radius: 8px; padding: 14px; text-align: center; }}
  .eco-card .num {{ font-size: 20px; font-weight: 700; color: #0f172a; }}
  .eco-card .lbl {{ font-size: 11px; color: #64748b; margin-top: 2px; }}
  .alerta {{ background: {color_bg}; border: 1px solid {color_badge}33;
             border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;
             font-size: 14px; color: {color_badge}; font-weight: 500; }}
  @media(max-width:500px) {{ .grid,.eco-grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <span class="urgencia-badge">⚡ URGENCIA {urgencia}</span>
    <h1>{nombre}</h1>
    <p class="meta">Ficha generada el {fecha_gen} · ID: {ficha_id[:8]}</p>
  </div>

  {"" if dias_rest is None else f'<div class="alerta">⏱ Plazo demanda: {calculos.get("plazo_demanda")} — quedan {dias_str}</div>'}

  <div class="card">
    <h2>📋 Datos del trabajador</h2>
    <div class="grid">
      <div class="field"><label>Teléfono</label><value>{telefono}</value></div>
      <div class="field"><label>Email</label><value>{email}</value></div>
      <div class="field"><label>Empresa</label><value>{empresa}</value></div>
      <div class="field"><label>Sector</label><value>{sector}</value></div>
      <div class="field"><label>Antigüedad</label><value>{antiguedad} años</value></div>
      <div class="field"><label>Contrato</label><value>{tipo_contrato}</value></div>
      <div class="field"><label>Salario bruto anual</label><value>{f"{salario:,.0f} €" if isinstance(salario, (int,float)) else salario}</value></div>
      <div class="field"><label>Horario contacto</label><value>{horario}</value></div>
    </div>
  </div>

  <div class="card">
    <h2>⚖️ Datos del despido</h2>
    <div class="grid">
      <div class="field"><label>Tipo de despido</label><value>{tipo_despido.replace("_"," ").title()}</value></div>
      <div class="field"><label>Fecha comunicación</label><value>{_safe_str(datos,"fecha_despido") or "—"}</value></div>
      <div class="field"><label>¿Firmó carta?</label><value>{bool_badge(firmo_carta)}</value></div>
      <div class="field"><label>¿Firmó finiquito?</label><value>{bool_badge(firmo_finiquito)}</value></div>
      <div class="field"><label>Baja médica</label><value>{bool_badge(baja_medica)}</value></div>
      <div class="field"><label>Sindicato</label><value>{bool_badge(sindicato)}</value></div>
      <div class="field"><label>Oferta empresa</label><value>{calculos.get("oferta_empresa") or "—"}</value></div>
      <div class="field"><label>Documentación</label><value>{documentacion}</value></div>
    </div>
  </div>

  <div class="card">
    <h2>💶 Cálculo económico</h2>
    <div class="eco-grid">
      <div class="eco-card">
        <div class="num">{f"{calculos['indemnizacion_legal']:,.0f} €"}</div>
        <div class="lbl">Indemnización legal estimada</div>
      </div>
      <div class="eco-card">
        <div class="num">{calculos.get("plazo_demanda","—")}</div>
        <div class="lbl">Plazo máx. demanda</div>
      </div>
      <div class="eco-card">
        <div class="num">{dias_str}</div>
        <div class="lbl">Días restantes</div>
      </div>
    </div>
    <p style="font-size:11px;color:#94a3b8;margin-top:12px">
      * Estimación orientativa. Basado en {calculos.get("dias_indemnizacion",0):.0f} días × {calculos["salario_diario"]:.2f} €/día.
    </p>
  </div>

</div>
</body>
</html>"""
    return html

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

last_payload = {}

@app.route("/webhook", methods=["POST"])
def webhook():
    global last_payload
    try:
        raw = request.get_json(force=True) or {}
        last_payload = raw
        logger.info(f"Webhook recibido: {list(raw.keys())}")

        # ── Extraer datos: campos individuales o nota de Lidia ──
        nota_lidia = _safe_str(raw, "nota_lidia", "conversation_note", "last_note")
        datos = dict(raw)  # copia

        if nota_lidia:
            logger.info("nota_lidia detectada → extrayendo datos con Claude")
            extraidos = extraer_datos_de_nota(nota_lidia)
            # Merge: campos individuales tienen prioridad si no están vacíos
            for k, v in extraidos.items():
                if v is not None and _safe_str(datos, k) in ("", "--", "None", "null"):
                    datos[k] = v
            logger.info(f"Datos extraídos de nota: {extraidos}")

        # ── Calcular ──
        calculos = calcular_indemnizacion(datos)

        # ── Generar ficha HTML ──
        ficha_id = str(uuid.uuid4())
        html = generar_ficha_html(datos, calculos, ficha_id)
        ficha_url = f"{BASE_URL}/ficha/{ficha_id}"

        fichas_store[ficha_id] = {
            "data": datos,
            "calculos": calculos,
            "html": html,
            "url": ficha_url,
            "timestamp": datetime.now().isoformat(),
        }

        # ── Guardar nota en GHL ──
        nombre = _safe_str(datos, "nombre") or "Cliente"
        contact_id = _safe_str(datos, "contact_id", "contactId", "id")
        nota_texto = f"""🤖 BRIEFING AUTOMÁTICO — {nombre}
━━━━━━━━━━━━━━━━━━━━━━
📋 Tipo despido: {_safe_str(datos,"tipo_despido") or "—"}
📅 Fecha: {_safe_str(datos,"fecha_despido") or "—"}
🏢 Empresa: {_safe_str(datos,"nombre_empresa") or "—"}
📆 Antigüedad: {datos.get("antiguedad_ano","—")} años
💶 Salario bruto: {datos.get("salario_bruto","—")} €/año
✍️ Firmó carta: {"Sí" if datos.get("firmo_carta") else "No"}
✍️ Firmó finiquito: {"Sí" if datos.get("firmo_finiquito") else "No"}
🏥 Baja médica: {"Sí" if datos.get("baja_medica") else "No"}
👥 Sindicato: {"Sí" if datos.get("sindicato") else "No"}
━━━━━━━━━━━━━━━━━━━━━━
💶 INDEMNIZACIÓN ESTIMADA: {calculos["indemnizacion_legal"]:,.0f} €
⏱ PLAZO DEMANDA: {calculos["plazo_demanda"]} ({calculos.get("dias_restantes","??")} días)
⚡ URGENCIA: {calculos["urgencia"]}
━━━━━━━━━━━━━━━━━━━━━━
🔗 FICHA COMPLETA: {ficha_url}"""

        if contact_id and GHL_API_KEY and GHL_LOCATION_ID:
            try:
                resp = requests.post(
                    "https://services.leadconnectorhq.com/contacts/notes",
                    headers={"Authorization": f"Bearer {GHL_API_KEY}",
                             "Content-Type": "application/json",
                             "Version": "2021-07-28"},
                    json={"contactId": contact_id, "body": nota_texto, "userId": "system"},
                    timeout=10
                )
                logger.info(f"Nota GHL: {resp.status_code}")
            except Exception as e:
                logger.warning(f"Error guardando nota GHL: {e}")

        logger.info(f"Ficha generada: {ficha_url}")
        return jsonify({"ok": True, "ficha_url": ficha_url, "urgencia": calculos["urgencia"]}), 200

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/ficha/<ficha_id>")
def ver_ficha(ficha_id):
    ficha = fichas_store.get(ficha_id)
    if not ficha:
        return "<h2>Ficha no encontrada o el servidor se reinició. Revisa la nota del contacto en GHL.</h2>", 404
    return ficha["html"]


@app.route("/last")
def last():
    return jsonify(last_payload)


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "v4",
        "fichas_en_memoria": len(fichas_store),
        "anthropic_key": "✅" if ANTHROPIC_API_KEY else "❌",
        "ghl_key": "✅" if GHL_API_KEY else "❌",
        "base_url": BASE_URL,
    })


@app.route("/")
def index():
    return """<!DOCTYPE html>
<html><head><title>Atilex Briefing Engine v4</title>
<style>body{font-family:sans-serif;padding:40px;max-width:600px;margin:0 auto;}
h1{color:#1e293b;}a{display:inline-block;margin:8px 0;color:#2563eb;}</style>
</head><body>
<h1>🤖 Atilex Briefing Engine v4</h1>
<p>Servidor activo. Endpoints:</p>
<a href="/health">/health — estado</a><br>
<a href="/last">/last — último payload recibido</a>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
