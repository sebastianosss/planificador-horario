# =============================================================================
#  PLANIFICADOR DE HORARIO DOCENTE  —  Shiny for Python
#  ---------------------------------------------------------------------------
#  v2: asistente de horario por contrato, grilla "pintable" por clic, etiquetado
#  del horario final y exportación a PDF imprimible.
#
#  Referencia legal (Chile, Estatuto Docente / Ley 20.903, vigente):
#  la docencia de aula (lectiva) puede ser a lo más el 65% de las horas de
#  contrato y el resto debe ser no lectiva; los recreos NO forman parte de
#  ninguna de las dos categorías y por lo tanto nunca se cuentan dentro del
#  total de horas de contrato en esta app.
# =============================================================================

from shiny import App, ui, render, reactive
import pandas as pd
import pulp
import json
import io

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib import colors as rl_colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

# -----------------------------------------------------------------------------
# 1. CONSTANTES Y CATÁLOGO
# -----------------------------------------------------------------------------

DIAS = ["Lun", "Mar", "Mie", "Jue", "Vie"]
DIAS_NOMBRE = {"Lun": "Lunes", "Mar": "Martes", "Mie": "Miércoles",
               "Jue": "Jueves", "Vie": "Viernes"}

# Código -> (etiqueta, color de fondo, color de texto)
CATEGORIAS = {
    "L": ("Lectiva",                  "#3b6fb6", "#ffffff"),
    "N": ("No lectiva",               "#2a9d8f", "#ffffff"),
    "P": ("Preparación",              "#43a047", "#ffffff"),
    "R": ("Recreo",                   "#cfd8dc", "#37474f"),
    "C": ("Colación",                 "#f4a261", "#3b2a17"),
    "D": ("Disponible",               "#eef2f6", "#5b6b7b"),
    "":  ("Vacío / fuera de jornada", "#ffffff", "#c3ccd4"),
}
ORDEN_PALETA = ["L", "N", "R", "C", "D", ""]


def filas_catalogo():
    """Fuente única de verdad: (inicio, fin, nombre, es_recreo)."""
    return [
        ("07:55", "08:00", "Ingreso / Saludo", False),
        ("08:00", "08:45", "Bloque 1",         False),
        ("08:45", "09:30", "Bloque 2",         False),
        ("09:30", "09:45", "Recreo 1",         True),
        ("09:45", "10:30", "Bloque 3",         False),
        ("10:30", "11:15", "Bloque 4",         False),
        ("11:15", "11:30", "Recreo 2",         True),
        ("11:30", "12:15", "Bloque 5",         False),
        ("12:15", "13:00", "Bloque 6",         False),
        ("13:00", "14:00", "Bloque 6b",        False),
        ("14:00", "15:00", "Bloque 7",         False),
        ("15:00", "15:30", "Bloque 8",         False),
        ("15:30", "16:30", "Bloque 9",         False),
        ("16:30", "17:30", "Bloque 10",        False),
        ("17:30", "18:30", "Bloque 11",        False),
        ("18:30", "19:30", "Bloque 12",        False),
        ("19:30", "20:30", "Bloque 13",        False),
    ]


def opciones_hora():
    """Lista ordenada de horas únicas (límites de bloque) para los selectores."""
    s = set()
    for ini, fin, _, _ in filas_catalogo():
        s.add(ini)
        s.add(fin)
    return sorted(s, key=time_to_mins) if s else []


# -----------------------------------------------------------------------------
# 2. UTILIDADES DE TIEMPO
# -----------------------------------------------------------------------------

def time_to_mins(t):
    s = str(t).strip()
    if not s or ":" not in s:
        return None
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def fmt_horas(mins):
    mins = int(round(mins))
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m"


def catalogo_vacio() -> pd.DataFrame:
    """DataFrame base con todas las celdas en blanco ('fuera de jornada')."""
    filas = filas_catalogo()
    data = {"Inicio": [f[0] for f in filas],
            "Fin": [f[1] for f in filas],
            "Bloque": [f[2] for f in filas]}
    for d in DIAS:
        data[d] = ["" for _ in filas]
    return pd.DataFrame(data)


def catalogo_vacio_duraciones() -> pd.DataFrame:
    """
    Minutos 'efectivos' de cada celda. Por defecto (sin asistente aplicado)
    cada celda vale la duración nominal del bloque; el asistente los recorta
    según el ingreso/salida real del día (ver construir_esqueleto_pure).
    """
    filas = filas_catalogo()
    nominal = [time_to_mins(f[1]) - time_to_mins(f[0]) for f in filas]
    data = {"Inicio": [f[0] for f in filas],
            "Fin": [f[1] for f in filas],
            "Bloque": [f[2] for f in filas]}
    for d in DIAS:
        data[d] = list(nominal)
    return pd.DataFrame(data)


def _solapa(a_ini, a_fin, b_ini, b_fin):
    """Minutos de superposición entre [a_ini,a_fin) y [b_ini,b_fin). 0 si falta algún límite."""
    if a_ini is None or a_fin is None or b_ini is None or b_fin is None:
        return 0
    return max(0, min(a_fin, b_fin) - max(a_ini, b_ini))


# -----------------------------------------------------------------------------
# 3. ASISTENTE DE HORARIO POR CONTRATO  ->  ESQUELETO DE LA GRILLA
# -----------------------------------------------------------------------------
#  La restricción horaria real (ingreso/salida/colación) es la que manda: si
#  la salida cae a mitad de un bloque, ese bloque se cuenta solo por los
#  minutos que realmente caen dentro de la jornada, no por el bloque completo.

def construir_esqueleto_pure(horarios_por_dia: dict) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """
    horarios_por_dia[d] = {
        "trabaja": bool, "colacion": bool,
        "ingreso": "HH:MM", "salida_alm": "HH:MM",
        "regreso": "HH:MM", "salida": "HH:MM",
    }
    Devuelve (df_codigos, df_duraciones_minutos, lista_de_errores).
    """
    filas = filas_catalogo()
    df = catalogo_vacio()
    dur = catalogo_vacio()  # mismo esqueleto de columnas; se llenará con minutos (no códigos)
    for d in DIAS:
        dur[d] = [0 for _ in filas]
    errores = []

    for d in DIAS:
        cfg = horarios_por_dia.get(d, {})
        if not cfg.get("trabaja"):
            continue  # columna queda en blanco, 0 minutos

        ingreso = time_to_mins(cfg.get("ingreso"))
        salida = time_to_mins(cfg.get("salida"))
        colacion = bool(cfg.get("colacion"))

        if colacion:
            salida_alm = time_to_mins(cfg.get("salida_alm"))
            regreso = time_to_mins(cfg.get("regreso"))
            valido = (None not in (ingreso, salida_alm, regreso, salida)
                      and ingreso < salida_alm < regreso < salida)
            if not valido:
                errores.append(
                    f"{DIAS_NOMBRE[d]}: revisa el orden y formato de las horas "
                    "(ingreso < salida a colación < regreso < salida final, HH:MM)."
                )
                continue
        else:
            salida_alm = regreso = None
            valido = (None not in (ingreso, salida) and ingreso < salida)
            if not valido:
                errores.append(
                    f"{DIAS_NOMBRE[d]}: revisa el formato de las horas y que la "
                    "salida sea después del ingreso (HH:MM)."
                )
                continue

        col_codes, col_durs = [], []
        for ini, fin, nom, es_recreo in filas:
            b_ini, b_fin = time_to_mins(ini), time_to_mins(fin)

            trabajo = _solapa(b_ini, b_fin, ingreso, salida)
            colacion_ov = _solapa(b_ini, b_fin, salida_alm, regreso) if colacion else 0
            trabajo_neto = max(0, trabajo - colacion_ov)

            if trabajo_neto > 0:
                code = "R" if es_recreo else "D"
                minutos = trabajo_neto
            elif colacion_ov > 0:
                code = "C"
                minutos = colacion_ov
            else:
                code = ""
                minutos = 0

            col_codes.append(code)
            col_durs.append(minutos)
        df[d] = col_codes
        dur[d] = col_durs

    return df, dur, errores


# -----------------------------------------------------------------------------
# 4. PINTAR / ETIQUETAR CELDAS  (funciones puras, usadas por los manejadores)
# -----------------------------------------------------------------------------

def aplicar_pintura(df: pd.DataFrame, row: int, day: str, value: str) -> pd.DataFrame:
    value = (value or "").strip().upper()
    if value not in CATEGORIAS:
        return df
    if day not in DIAS or not (0 <= row < len(df)):
        return df
    df = df.copy()
    df.at[row, day] = value
    return df


def aplicar_etiqueta(labels: dict, row: int, day: str, value: str) -> dict:
    labels = dict(labels)
    key = f"{row}_{day}"
    value = (value or "").strip()
    if value:
        labels[key] = value
    else:
        labels.pop(key, None)
    return labels


# -----------------------------------------------------------------------------
# 5. MOTOR DE OPTIMIZACIÓN
# -----------------------------------------------------------------------------
#  Los recreos NO se consideran lectiva ni no lectiva (no entran en la
#  proporción 65/35 del Estatuto Docente), pero SÍ son tiempo de permanencia
#  en el establecimiento: cuentan dentro del tope de horas de contrato y del
#  total trabajado final.

def optimizar(df: pd.DataFrame, dur_df: pd.DataFrame, horas_contrato: float):
    """
    df:      códigos por celda (L/N/R/C/D/"").
    dur_df:  minutos EFECTIVOS de cada celda (puede ser menor que el bloque
             nominal si el ingreso/salida real del día cae a mitad de bloque;
             ver construir_esqueleto_pure). Esta es la restricción que manda
             para sumar horas lectivas/no lectivas y para el tope de contrato.
    """
    contract = int(round(float(horas_contrato) * 60))
    df = df.reset_index(drop=True)
    dur_df = dur_df.reset_index(drop=True)
    n = len(df)

    nominal, start = {}, {}
    for i in range(n):
        ini = time_to_mins(df.at[i, "Inicio"])
        fin = time_to_mins(df.at[i, "Fin"])
        if ini is None or fin is None or fin <= ini:
            nominal[i], start[i] = None, None
        else:
            nominal[i], start[i] = fin - ini, ini

    def minutos(i, d):
        """Minutos efectivos de la celda; si no hay dato, cae a la duración nominal."""
        try:
            v = dur_df.at[i, d]
            v = float(v)
        except (KeyError, TypeError, ValueError):
            v = None
        if v is None or v != v:  # NaN
            v = nominal[i] or 0
        return max(0, int(round(v)))

    lectiva = nolectiva = recreo_min = col_min = 0
    cand = []
    work_starts = {d: [] for d in DIAS}

    for i in range(n):
        if nominal[i] is None:
            continue
        for d in DIAS:
            c = str(df.at[i, d]).strip().upper()
            m = minutos(i, d)
            if m <= 0:
                continue
            if c == "L":
                lectiva += m
                work_starts[d].append(start[i])
            elif c == "N":
                nolectiva += m
                work_starts[d].append(start[i])
            elif c == "R":
                recreo_min += m
            elif c == "C":
                col_min += m
            elif c == "D":
                cand.append((i, d))

    # El recreo NO es lectiva ni no lectiva (no entra en la proporción 65/35),
    # pero sí es tiempo de permanencia en el establecimiento: cuenta dentro
    # del tope de las horas de contrato y del total trabajado final.
    fijo = lectiva + nolectiva + recreo_min

    res = df.copy()
    prep_min = 0
    status = "ok"

    if fijo >= contract:
        status = "sobrecarga"
    elif not cand:
        status = "sin_espacio"
    else:
        prob = pulp.LpProblem("preparacion", pulp.LpMaximize)
        p = {(i, d): pulp.LpVariable(f"p_{i}_{d}", cat="Binary") for (i, d) in cand}

        def es_interior(i, d):
            s = start[i]
            ws = work_starts[d]
            return 1 if (any(x < s for x in ws) and any(x > s for x in ws)) else 0

        prob += pulp.lpSum(p[(i, d)] * (minutos(i, d) * 1000 + es_interior(i, d))
                           for (i, d) in cand)
        prob += fijo + pulp.lpSum(p[(i, d)] * minutos(i, d) for (i, d) in cand) <= contract
        prob.solve(pulp.PULP_CBC_CMD(msg=0))

        for (i, d) in cand:
            val = pulp.value(p[(i, d)])
            if val is not None and val > 0.5:
                res.at[i, d] = "P"
                prep_min += minutos(i, d)

    total = lectiva + nolectiva + recreo_min + prep_min
    deficit = max(0, contract - total)

    resumen = {
        "contract": contract, "lectiva": lectiva, "nolectiva": nolectiva,
        "prep": prep_min, "recreo": recreo_min, "colacion": col_min,
        "total": total, "deficit": deficit, "status": status,
    }
    return res, resumen


# -----------------------------------------------------------------------------
# 6. JAVASCRIPT COMPARTIDO  (pintar celdas + guardar etiquetas)
# -----------------------------------------------------------------------------

CATS_JS = json.dumps({code: {"label": lbl, "bg": bg, "fg": fg}
                      for code, (lbl, bg, fg) in CATEGORIAS.items()})

SCRIPT_COMPARTIDO = ui.tags.script(f"""
const CATS = {CATS_JS};
window.currentPen = 'L';

function setPen(code, btn) {{
  window.currentPen = code;
  document.querySelectorAll('.pen-btn').forEach(function(b) {{
    b.style.outline = 'none'; b.style.boxShadow = 'none';
  }});
  btn.style.outline = '3px solid #1f2d3d';
  btn.style.boxShadow = '0 0 0 2px #fff inset';
}}

function paintCell(td) {{
  var pen = window.currentPen;
  var meta = CATS[pen];
  if (!meta) return;
  td.style.background = meta.bg;
  td.style.color = meta.fg;
  td.textContent = pen === '' ? '\\u00b7' : pen;
  Shiny.setInputValue('cell_paint', {{
    row: td.dataset.row, day: td.dataset.day, value: pen, t: Date.now()
  }}, {{priority: 'event'}});
}}

function saveLabel(inp) {{
  Shiny.setInputValue('cell_label', {{
    row: inp.dataset.row, day: inp.dataset.day, value: inp.value, t: Date.now()
  }}, {{priority: 'event'}});
}}

function labelKeydown(ev, inp) {{
  if (ev.key === 'Enter') {{ inp.blur(); }}
}}
""")


def paleta_html() -> str:
    chips = []
    for code in ORDEN_PALETA:
        label, bg, fg = CATEGORIAS[code]
        activo = "outline:3px solid #1f2d3d;box-shadow:0 0 0 2px #fff inset;" if code == "L" else ""
        chips.append(
            f'<button type="button" class="pen-btn" data-code="{code}" '
            f'onclick="setPen(\'{code}\', this)" '
            f'style="background:{bg};color:{fg};border:none;{activo}'
            f'padding:9px 16px;border-radius:9px;font-weight:700;font-size:13px;'
            f'cursor:pointer;margin:3px;">{label}</button>'
        )
    return ('<div style="margin:4px 0 10px 0;">' + "".join(chips) +
            '</div><div style="font-size:12px;color:#7a8a99;margin-bottom:8px;">'
            'Elige una categoría y luego haz clic sobre los bloques de la grilla '
            'para pintarlos.</div>')


# -----------------------------------------------------------------------------
# 7. GRILLA PINTABLE  (paso 2)
# -----------------------------------------------------------------------------

def grilla_pintable_html(df: pd.DataFrame) -> str:
    cell_css = ("padding:8px 4px;text-align:center;font-weight:700;"
                "font-size:13px;border:1px solid #ffffff;border-radius:5px;"
                "cursor:pointer;user-select:none;transition:transform .05s;")
    head_css = ("padding:6px 8px;text-align:left;font-size:12px;"
                "color:#5b6b7b;font-weight:600;border-bottom:2px solid #e3e8ee;")

    html = ['<div style="overflow-x:auto;"><table style="border-collapse:separate;'
            'border-spacing:3px;width:100%;font-family:system-ui,sans-serif;">']
    html.append("<tr>")
    html.append(f'<th style="{head_css}">Bloque</th>')
    for d in DIAS:
        html.append(f'<th style="{head_css}text-align:center;">{DIAS_NOMBRE[d]}</th>')
    html.append("</tr>")

    for i in range(len(df)):
        ini, fin, nom = df.at[i, "Inicio"], df.at[i, "Fin"], df.at[i, "Bloque"]
        etiqueta = f"{ini}–{fin}  {nom}"
        html.append("<tr>")
        html.append(f'<td style="{head_css}white-space:nowrap;">{etiqueta}</td>')
        for d in DIAS:
            code = str(df.at[i, d]).strip().upper()
            if code not in CATEGORIAS:
                code = ""
            label, bg, fg = CATEGORIAS[code]
            txt = "·" if code == "" else code
            html.append(
                f'<td title="{label}" data-row="{i}" data-day="{d}" onclick="paintCell(this)" '
                f'style="{cell_css}background:{bg};color:{fg};">{txt}</td>'
            )
        html.append("</tr>")
    html.append("</table></div>")
    return "".join(html)


# -----------------------------------------------------------------------------
# 8. GRILLA DE RESULTADO CON ETIQUETAS  (paso 3)
# -----------------------------------------------------------------------------

def grilla_resultado_html(res: pd.DataFrame, labels: dict) -> str:
    cell_css = "padding:3px;border:1px solid #ffffff;border-radius:5px;"
    head_css = ("padding:6px 8px;text-align:left;font-size:12px;"
                "color:#5b6b7b;font-weight:600;border-bottom:2px solid #e3e8ee;")
    input_css = ("width:100%;border:none;background:transparent;font-size:11px;"
                "text-align:center;font-weight:600;padding:6px 2px;border-radius:4px;")

    html = ['<div style="overflow-x:auto;"><table style="border-collapse:separate;'
            'border-spacing:3px;width:100%;font-family:system-ui,sans-serif;">']
    html.append("<tr>")
    html.append(f'<th style="{head_css}">Bloque</th>')
    for d in DIAS:
        html.append(f'<th style="{head_css}text-align:center;">{DIAS_NOMBRE[d]}</th>')
    html.append("</tr>")

    for i in range(len(res)):
        ini, fin, nom = res.at[i, "Inicio"], res.at[i, "Fin"], res.at[i, "Bloque"]
        etiqueta = f"{ini}–{fin}  {nom}"
        html.append("<tr>")
        html.append(f'<td style="{head_css}white-space:nowrap;">{etiqueta}</td>')
        for d in DIAS:
            code = str(res.at[i, d]).strip().upper()
            if code not in CATEGORIAS or code == "":
                html.append(f'<td style="{cell_css}background:#fbfcfd;"></td>')
                continue
            cat_label, bg, fg = CATEGORIAS[code]
            key = f"{i}_{d}"
            valor = labels.get(key, "" if code in ("L", "N", "P") else cat_label)
            placeholder = cat_label if code in ("L", "N", "P") else ""
            html.append(
                f'<td style="{cell_css}background:{bg};">'
                f'<input type="text" value="{valor}" placeholder="{placeholder}" '
                f'data-row="{i}" data-day="{d}" onblur="saveLabel(this)" '
                f'onkeydown="labelKeydown(event,this)" '
                f'style="{input_css}color:{fg};"></td>'
            )
        html.append("</tr>")
    html.append("</table></div>")
    return "".join(html)


def leyenda_html() -> str:
    chips = []
    for code in ORDEN_PALETA:
        label, bg, fg = CATEGORIAS[code]
        marca = code if code else "·"
        chips.append(
            f'<span style="display:inline-flex;align-items:center;gap:6px;'
            f'margin:2px 10px 2px 0;font-size:12px;color:#445;">'
            f'<span style="display:inline-block;width:20px;height:18px;border-radius:4px;'
            f'background:{bg};color:{fg};text-align:center;font-weight:700;'
            f'line-height:18px;font-size:11px;">{marca}</span>{label}</span>'
        )
    return '<div style="margin:4px 0 2px 0;">' + "".join(chips) + "</div>"


def resumen_html(r: dict) -> str:
    def card(titulo, valor, sub="", color="#1f2d3d"):
        return (
            f'<div style="flex:1;min-width:140px;background:#fff;border:1px solid #e3e8ee;'
            f'border-radius:10px;padding:12px 14px;">'
            f'<div style="font-size:11px;color:#7a8a99;text-transform:uppercase;'
            f'letter-spacing:.04em;">{titulo}</div>'
            f'<div style="font-size:20px;font-weight:700;color:{color};margin-top:2px;">{valor}</div>'
            f'<div style="font-size:11px;color:#7a8a99;">{sub}</div></div>'
        )

    contract = r["contract"]
    lectiva_pct = (r["lectiva"] / contract * 100) if contract else 0
    nolectiva_total = r["nolectiva"] + r["prep"]
    nolectiva_pct = (nolectiva_total / contract * 100) if contract else 0

    cards = [
        card("Contrato", fmt_horas(r["contract"]), "objetivo semanal"),
        card("Lectiva", fmt_horas(r["lectiva"]), f"{lectiva_pct:.0f}% del contrato", "#3b6fb6"),
        card("No lectiva (asignada + prep.)", fmt_horas(nolectiva_total),
             f"{nolectiva_pct:.0f}% del contrato", "#2a9d8f"),
        card("Preparación asignada", fmt_horas(r["prep"]), "bloques nuevos", "#43a047"),
        card("Recreo", fmt_horas(r["recreo"]), "no es lectiva ni no lectiva", "#5b6b7b"),
        card("Total trabajado", fmt_horas(r["total"]), "lectiva + no lectiva + recreo"),
    ]
    grid = ('<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:6px;">'
            + "".join(cards) + "</div>")

    avisos = []
    if r["status"] == "sobrecarga":
        avisos.append(("#fdecea", "#b71c1c",
                       "Las horas fijas (lectiva + no lectiva + recreo) ya igualan o superan "
                       "el contrato. No quedan horas para preparación; revisa las clases marcadas."))
    elif r["status"] == "sin_espacio":
        avisos.append(("#fff8e1", "#8a6d00",
                       "No hay bloques marcados como Disponibles; no se pudo asignar preparación."))
    if r["deficit"] > 0 and r["status"] == "ok":
        avisos.append(("#fff8e1", "#8a6d00",
                       f"Faltan {fmt_horas(r['deficit'])} para llegar al contrato: no hay "
                       "suficientes bloques Disponibles dentro de la jornada."))
    if lectiva_pct > 65.0 + 1e-6:
        avisos.append(("#fff8e1", "#8a6d00",
                       f"La carga lectiva es {lectiva_pct:.0f}% del contrato. Como referencia, "
                       "el Estatuto Docente fija un máximo de 65% lectivo (mín. 35% no lectivo) "
                       "sobre el total de contrato; el recreo no entra en esa proporción, aunque "
                       "sí se suma al total de horas trabajadas."))

    avisos_html = ""
    for bg, fg, txt in avisos:
        avisos_html += (
            f'<div style="background:{bg};color:{fg};border-radius:8px;'
            f'padding:10px 12px;margin-top:8px;font-size:13px;">{txt}</div>'
        )

    nota = ('<div style="font-size:11px;color:#9aa7b3;margin-top:10px;">'
            'Herramienta de planificación; no constituye asesoría legal ni laboral.</div>')

    return grid + avisos_html + nota


# -----------------------------------------------------------------------------
# 9. GENERACIÓN DE PDF IMPRIMIBLE
# -----------------------------------------------------------------------------

def generar_pdf(res: pd.DataFrame, labels: dict, resumen: dict, titulo: str = "Horario semanal") -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=1.0 * cm, rightMargin=1.0 * cm,
        topMargin=1.0 * cm, bottomMargin=1.0 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("titulo", parent=styles["Heading1"],
                                 alignment=TA_CENTER, fontSize=16, spaceAfter=2)
    nota_style = ParagraphStyle("nota", parent=styles["Normal"], fontSize=9,
                                textColor=rl_colors.HexColor("#5b6b7b"))

    elementos = [Paragraph(titulo, title_style), Spacer(1, 0.25 * cm)]

    header = ["Bloque"] + [DIAS_NOMBRE[d] for d in DIAS]
    data = [header]
    for i in range(len(res)):
        ini, fin, nom = res.at[i, "Inicio"], res.at[i, "Fin"], res.at[i, "Bloque"]
        fila = [f"{ini}–{fin} {nom}"]
        for d in DIAS:
            code = str(res.at[i, d]).strip().upper()
            if code == "" or code not in CATEGORIAS:
                fila.append("")
                continue
            cat_label = CATEGORIAS[code][0]
            texto = labels.get(f"{i}_{d}", "").strip() or (cat_label if code != "L" and code != "N" else "")
            fila.append(texto)
        data.append(fila)

    col_widths = [3.6 * cm] + [4.6 * cm] * len(DIAS)
    tabla = Table(data, colWidths=col_widths, repeatRows=1)

    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#1f2d3d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7.8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.6, rl_colors.HexColor("#dddddd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (0, -1), rl_colors.HexColor("#f4f6f8")),
        ("ROWBACKGROUNDS", (0, 1), (0, -1), [rl_colors.HexColor("#f4f6f8")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for i in range(len(res)):
        for j, d in enumerate(DIAS, start=1):
            code = str(res.at[i, d]).strip().upper()
            if code not in CATEGORIAS:
                code = ""
            _, bg, fg = CATEGORIAS[code]
            estilo.append(("BACKGROUND", (j, i + 1), (j, i + 1), rl_colors.HexColor(bg)))
            estilo.append(("TEXTCOLOR", (j, i + 1), (j, i + 1), rl_colors.HexColor(fg)))
    tabla.setStyle(TableStyle(estilo))
    elementos.append(tabla)
    elementos.append(Spacer(1, 0.35 * cm))

    contract = resumen.get("contract", 0)
    resumen_txt = (
        f"Contrato: {fmt_horas(contract)} &nbsp;·&nbsp; "
        f"Lectiva: {fmt_horas(resumen.get('lectiva', 0))} &nbsp;·&nbsp; "
        f"No lectiva (asignada + preparación): {fmt_horas(resumen.get('nolectiva', 0) + resumen.get('prep', 0))} "
        f"&nbsp;·&nbsp; Recreo: {fmt_horas(resumen.get('recreo', 0))} "
        f"&nbsp;·&nbsp; Total trabajado: {fmt_horas(resumen.get('total', 0))} "
        "&nbsp;·&nbsp; (el recreo no es lectiva ni no lectiva, pero sí suma al total)"
    )
    elementos.append(Paragraph(resumen_txt, nota_style))

    doc.build(elementos)
    buf.seek(0)
    return buf.getvalue()


# -----------------------------------------------------------------------------
# 10. INTERFAZ DE USUARIO
# -----------------------------------------------------------------------------

OPCIONES_HORA = opciones_hora()


def dia_wizard_ui(d):
    nombre = DIAS_NOMBRE[d]
    return ui.div(
        ui.div(
            ui.span(nombre, style="font-weight:700;width:90px;display:inline-block;"),
            ui.input_checkbox(f"trabaja_{d}", "Trabaja este día", value=True),
            ui.input_checkbox(f"colacion_{d}", "Tiene colación", value=True),
            style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;"
                  "margin-bottom:6px;",
        ),
        ui.panel_conditional(
            f"input.trabaja_{d}",
            ui.div(
                ui.div(ui.input_select(f"ingreso_{d}", "Ingreso", OPCIONES_HORA,
                                       selected="07:55", width="115px")),
                ui.panel_conditional(
                    f"input.colacion_{d}",
                    ui.div(
                        ui.div(ui.input_select(f"salida_alm_{d}", "Salida (almuerzo)",
                                               OPCIONES_HORA, selected="13:00", width="150px")),
                        ui.div(ui.input_select(f"regreso_{d}", "Regreso", OPCIONES_HORA,
                                               selected="14:00", width="115px")),
                        style="display:flex;gap:14px;",
                    ),
                ),
                ui.div(
                    ui.input_text(f"salida_{d}", "Salida", value="18:30",
                                 placeholder="HH:MM", width="115px"),
                    ui.tags.small("Formato 24h, ej: 18:45", style="color:#9aa7b3;"),
                ),
                style="display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap;"
                      "margin-bottom:10px;",
            ),
        ),
        ui.hr(style="margin:4px 0 14px 0;border-color:#eef1f4;"),
    )


INSTRUCCIONES_PASO1 = ui.HTML(
    '<div style="font-size:13px;color:#445;line-height:1.5;margin-bottom:10px;">'
    'Ingresa tu horario tal como aparece en tu contrato. Si un día no tiene '
    'colación, desactiva "Tiene colación" y solo verás Ingreso/Salida. Si tu '
    'semana se repite, configura el primer día y usa "Copiar a los demás días".'
    '</div>'
)

INSTRUCCIONES_PASO2 = ui.HTML(
    '<div style="font-size:13px;color:#445;line-height:1.5;margin-bottom:4px;">'
    'Esta grilla ya viene marcada con tu jornada, colación y recreos. Ahora marca '
    'tus clases (<b>Lectiva</b>) y compromisos fijos (<b>No lectiva</b>): elige la '
    'categoría en la paleta y haz clic sobre los bloques. Lo que dejes en '
    '<b>Disponible</b> se completará al optimizar.</div>'
)

INSTRUCCIONES_PASO3 = ui.HTML(
    '<div style="font-size:13px;color:#445;line-height:1.5;margin-bottom:4px;">'
    'Escribe el detalle de cada bloque (ej. "Matemáticas 8°A", "Reunión de '
    'apoderados"). El cambio se guarda automáticamente al salir del casillero. '
    'Cuando esté listo, descarga el PDF imprimible.</div>'
)

app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.h5("Configuración"),
        ui.input_numeric("horas", "Horas de contrato (semanales)",
                         value=36, min=1, max=44, step=1),
        ui.hr(),
        ui.input_action_button("reset", "Reiniciar todo", width="100%",
                               class_="btn-outline-secondary"),
        ui.hr(),
        ui.HTML(
            '<div style="font-size:12px;color:#7a8a99;line-height:1.4;">'
            'Los <b>recreos no son lectiva ni no lectiva</b> (no entran en la '
            'proporción 65/35 del Estatuto Docente), pero sí son tiempo en el '
            'establecimiento y se suman al total de horas de contrato.</div>'
        ),
        width=340,
        open="open",
    ),
    ui.card(
        ui.card_header("1 · Tu horario de contrato"),
        INSTRUCCIONES_PASO1,
        *[dia_wizard_ui(d) for d in DIAS],
        ui.div(
            ui.input_select("dia_base", "Día base para copiar",
                            {d: DIAS_NOMBRE[d] for d in DIAS}, selected="Lun",
                            width="180px"),
            ui.input_action_button("copiar_dia", "Copiar a los demás días",
                                   class_="btn-outline-secondary"),
            style="display:flex;gap:12px;align-items:end;margin-bottom:12px;",
        ),
        ui.input_action_button("aplicar", "Aplicar horario", class_="btn-primary"),
        ui.output_ui("errores_wizard"),
    ),
    ui.card(
        ui.card_header("2 · Marca tus bloques"),
        INSTRUCCIONES_PASO2,
        ui.HTML(paleta_html()),
        ui.output_ui("grilla_edit"),
        ui.div(
            ui.input_action_button("optimizar", "Optimizar horario", class_="btn-primary"),
            style="margin-top:12px;",
        ),
    ),
    ui.card(
        ui.card_header("3 · Horario optimizado — agrega el detalle"),
        INSTRUCCIONES_PASO3,
        ui.output_ui("leyenda_resultado"),
        ui.output_ui("grilla_resultado"),
        ui.output_ui("resumen_semana"),
        ui.div(
            ui.download_button("descargar_pdf", "Descargar PDF", class_="btn-primary"),
            style="margin-top:12px;",
        ),
    ),
    SCRIPT_COMPARTIDO,
    title="Planificador de horario docente",
    fillable=False,
)


# -----------------------------------------------------------------------------
# 11. SERVIDOR
# -----------------------------------------------------------------------------

def server(input, output, session):

    grid_state = reactive.value(catalogo_vacio())             # códigos L/N/R/C/D
    dur_state = reactive.value(catalogo_vacio_duraciones())   # minutos efectivos por celda
    grid_version = reactive.value(0)                # fuerza re-render de la grilla 2
    labels_rv = reactive.value({})                  # etiquetas del horario final
    resultado_rv = reactive.value(None)             # (df_resultado, resumen)
    errores_rv = reactive.value([])

    # --- 1. Asistente de horario por contrato -------------------------------

    @reactive.effect
    @reactive.event(input.copiar_dia)
    def _copiar_dia():
        base = input.dia_base()
        trabaja = input[f"trabaja_{base}"]()
        colacion = input[f"colacion_{base}"]()
        ingreso = input[f"ingreso_{base}"]()
        salida_alm = input[f"salida_alm_{base}"]()
        regreso = input[f"regreso_{base}"]()
        salida = input[f"salida_{base}"]()
        for d in DIAS:
            if d == base:
                continue
            ui.update_checkbox(f"trabaja_{d}", value=trabaja)
            ui.update_checkbox(f"colacion_{d}", value=colacion)
            ui.update_select(f"ingreso_{d}", selected=ingreso)
            ui.update_select(f"salida_alm_{d}", selected=salida_alm)
            ui.update_select(f"regreso_{d}", selected=regreso)
            ui.update_text(f"salida_{d}", value=salida)

    @reactive.effect
    @reactive.event(input.aplicar)
    def _aplicar_horario():
        horarios = {}
        for d in DIAS:
            horarios[d] = {
                "trabaja": input[f"trabaja_{d}"](),
                "colacion": input[f"colacion_{d}"](),
                "ingreso": input[f"ingreso_{d}"](),
                "salida_alm": input[f"salida_alm_{d}"](),
                "regreso": input[f"regreso_{d}"](),
                "salida": input[f"salida_{d}"](),
            }
        df, dur, errores = construir_esqueleto_pure(horarios)
        grid_state.set(df)
        dur_state.set(dur)
        grid_version.set(grid_version.get() + 1)
        errores_rv.set(errores)
        labels_rv.set({})
        resultado_rv.set(None)

    @render.ui
    def errores_wizard():
        errores = errores_rv.get()
        if not errores:
            return ui.HTML("")
        items = "".join(f"<li>{e}</li>" for e in errores)
        return ui.HTML(
            f'<div style="background:#fdecea;color:#b71c1c;border-radius:8px;'
            f'padding:10px 12px;margin-top:10px;font-size:13px;">'
            f'No se aplicaron esos días, revisa lo siguiente:<ul style="margin:6px 0 0 18px;">'
            f'{items}</ul></div>'
        )

    @reactive.effect
    @reactive.event(input.reset)
    def _reset():
        grid_state.set(catalogo_vacio())
        dur_state.set(catalogo_vacio_duraciones())
        grid_version.set(grid_version.get() + 1)
        labels_rv.set({})
        resultado_rv.set(None)
        errores_rv.set([])

    # --- 2. Grilla pintable ---------------------------------------------------

    @render.ui
    def grilla_edit():
        grid_version.get()  # única dependencia explícita de re-render
        with reactive.isolate():
            df = grid_state.get()
        return ui.HTML(grilla_pintable_html(df))

    @reactive.effect
    @reactive.event(input.cell_paint)
    def _on_paint():
        info = input.cell_paint()
        row = int(info["row"])
        day = info["day"]
        value = info["value"]
        df = grid_state.get()
        grid_state.set(aplicar_pintura(df, row, day, value))

    # --- 3. Optimización --------------------------------------------------

    @reactive.effect
    @reactive.event(input.optimizar)
    def _optimizar():
        df = grid_state.get()
        dur = dur_state.get()
        res, resumen = optimizar(df, dur, input.horas())
        resultado_rv.set((res, resumen))

    @render.ui
    def leyenda_resultado():
        if resultado_rv.get() is None:
            return ui.HTML(
                '<div style="color:#9aa7b3;font-size:13px;padding:4px 0 8px 0;">'
                'Configura tu horario y pulsa <b>Optimizar horario</b> para ver la '
                'propuesta aquí.</div>'
            )
        return ui.HTML(leyenda_html())

    @render.ui
    def grilla_resultado():
        data = resultado_rv.get()
        if data is None:
            return ui.HTML("")
        res, _ = data
        with reactive.isolate():
            labels = labels_rv.get()
        return ui.HTML(grilla_resultado_html(res, labels))

    @reactive.effect
    @reactive.event(input.cell_label)
    def _on_label():
        info = input.cell_label()
        row = int(info["row"])
        day = info["day"]
        value = info["value"]
        labels_rv.set(aplicar_etiqueta(labels_rv.get(), row, day, value))

    @render.ui
    def resumen_semana():
        data = resultado_rv.get()
        if data is None:
            return ui.HTML("")
        _, r = data
        return ui.HTML(resumen_html(r))

    @render.download(filename="horario.pdf", media_type="application/pdf")
    def descargar_pdf():
        data = resultado_rv.get()
        if data is None:
            df_vacio = catalogo_vacio()
            dur_vacio = catalogo_vacio_duraciones()
            res, resumen = optimizar(df_vacio, dur_vacio, input.horas())
        else:
            res, resumen = data
        labels = labels_rv.get()
        yield generar_pdf(res, labels, resumen, "Horario semanal")


app = App(app_ui, server)


# =============================================================================
#  CÓMO EJECUTAR
# =============================================================================
#  • Local:        shiny run --reload app.py     -> abre http://127.0.0.1:8000
#
#  • Google Colab (en una celda):
#       !pip install shiny pulp pandas reportlab
#       get_ipython().system_raw('shiny run --port 8000 app.py &')
#       from google.colab.output import serve_kernel_port_as_window
#       serve_kernel_port_as_window(8000)
#
#  • shinyapps.io (desde la terminal de VS Code):
#       pip install rsconnect-python
#       rsconnect add --account TU_CUENTA --name TU_CUENTA --token XXXX --secret YYYY
#       rsconnect deploy shiny . --name TU_CUENTA --title horario-docente
#     (la carpeta debe contener app.py y requirements.txt)
# =============================================================================
