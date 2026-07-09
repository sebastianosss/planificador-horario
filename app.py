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
import re
import html as html_lib

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

# Paleta de colores sugeridos para las asignaturas (hex -> nombre legible).
COLORES_ASIG = [
    ("#3b6fb6", "Azul"),        ("#8e44ad", "Morado"),
    ("#c0392b", "Rojo"),        ("#16a085", "Verde azulado"),
    ("#d35400", "Naranjo"),     ("#2c3e50", "Azul noche"),
    ("#2980b9", "Celeste"),     ("#27ae60", "Verde"),
    ("#e67e22", "Ámbar"),       ("#7f8c8d", "Gris"),
]

# Un identificador de asignatura tiene la forma "S1", "S2", ...
RE_ASIG = re.compile(r"S\d+")


def es_asignatura(code) -> bool:
    return bool(RE_ASIG.fullmatch(str(code).strip()))


def mapa_render(asignaturas: list) -> dict:
    """Combina las categorías base con las asignaturas: code -> (etiqueta, bg, fg)."""
    m = {code: (lbl, bg, fg) for code, (lbl, bg, fg) in CATEGORIAS.items()}
    for a in asignaturas:
        m[a["id"]] = (a["nombre"], a["color"], "#ffffff")
    return m


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
        # Jornada de tarde: dos bloques de 45', recreo, dos de 45', recreo,
        # dos de 45'. (desde el regreso de almuerzo, 14:00)
        ("14:00", "14:45", "Bloque 7",         False),
        ("14:45", "15:30", "Bloque 8",         False),
        ("15:30", "15:45", "Recreo 3",         True),
        ("15:45", "16:30", "Bloque 9",         False),
        ("16:30", "17:15", "Bloque 10",        False),
        ("17:15", "17:30", "Recreo 4",         True),
        ("17:30", "18:15", "Bloque 11",        False),
        ("18:15", "19:00", "Bloque 12",        False),
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


def nominal_min(df: pd.DataFrame, i: int):
    """Duración nominal (en minutos) del bloque de la fila i, según Inicio/Fin."""
    ini = time_to_mins(df.at[i, "Inicio"])
    fin = time_to_mins(df.at[i, "Fin"])
    if ini is None or fin is None or fin <= ini:
        return None
    return fin - ini


def minutos_celda(df: pd.DataFrame, dur_df: pd.DataFrame, i: int, d: str) -> int:
    """
    Minutos EFECTIVOS que aporta la celda (i, d): lo que realmente cae dentro
    de la jornada. Solo cae a la duración nominal del bloque si no hay dato de
    duración (celda desconocida / NaN); un 0 explícito se respeta como 0.
    Fuente única de verdad usada por el optimizador, la validación y los tooltips.
    """
    try:
        v = float(dur_df.at[i, d])
    except (KeyError, TypeError, ValueError):
        v = None
    if v is None or v != v:  # None o NaN
        nb = nominal_min(df, i)
        v = nb if nb is not None else 0
    return max(0, int(round(v)))


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
    if value not in CATEGORIAS and not es_asignatura(value):
        return df
    if day not in DIAS or not (0 <= row < len(df)):
        return df
    df = df.copy()
    df.at[row, day] = value
    return df


def fusionar_pintura(nuevo_df: pd.DataFrame, viejo_df) -> pd.DataFrame:
    """
    Al re-aplicar el horario de contrato, repone sobre el nuevo esqueleto las
    marcas que el usuario ya había pintado (clases, no lectiva y asignaturas),
    siempre que caigan en un bloque que sigue siendo tiempo de trabajo ('D')
    en el nuevo esqueleto. Así, corregir un día no obliga a re-pintar la semana.
    """
    if viejo_df is None:
        return nuevo_df
    df = nuevo_df.copy()
    for d in DIAS:
        if d not in viejo_df.columns:
            continue
        for i in range(len(df)):
            if str(df.at[i, d]).strip() != "D":
                continue  # solo repongo sobre tiempo disponible del nuevo esqueleto
            try:
                viejo = str(viejo_df.at[i, d]).strip()
            except (KeyError, IndexError):
                continue
            if viejo in ("L", "N") or es_asignatura(viejo):
                df.at[i, d] = viejo
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
        """Minutos efectivos de la celda (fuente única: minutos_celda)."""
        return minutos_celda(df, dur_df, i, d)

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
            if c == "L" or es_asignatura(c):
                # Una asignatura concreta (S1, S2, ...) es tiempo lectivo.
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
    n_prep = 0
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
                n_prep += 1

    total = lectiva + nolectiva + recreo_min + prep_min
    deficit = max(0, contract - total)

    # Tiempo Disponible dentro de la jornada que NO se pudo convertir en
    # preparación porque el contrato ya está completo: la jornada del paso 1
    # tiene más horas de las contratadas.
    disponible = sum(minutos(i, d) for (i, d) in cand)
    sobrante = disponible - prep_min
    sobrante_bloques = len(cand) - n_prep

    resumen = {
        "contract": contract, "lectiva": lectiva, "nolectiva": nolectiva,
        "prep": prep_min, "recreo": recreo_min, "colacion": col_min,
        "total": total, "deficit": deficit, "status": status,
        "disponible": disponible, "sobrante": sobrante,
        "sobrante_bloques": sobrante_bloques,
    }
    return res, resumen


# -----------------------------------------------------------------------------
# 6. JAVASCRIPT COMPARTIDO  (pintar celdas + guardar etiquetas)
# -----------------------------------------------------------------------------

CATS_JS = json.dumps({code: {"label": lbl, "bg": bg, "fg": fg}
                      for code, (lbl, bg, fg) in CATEGORIAS.items()})

SCRIPT_COMPARTIDO = ui.tags.script(f"""
const CATS = {CATS_JS};
window.currentPen = {{code: 'L', bg: '#3b6fb6', fg: '#ffffff', mark: 'L'}};

function setPen(code, bg, fg, mark, btn) {{
  window.currentPen = {{code: code, bg: bg, fg: fg, mark: mark}};
  document.querySelectorAll('.pen-btn').forEach(function(b) {{
    b.style.outline = 'none'; b.style.boxShadow = 'none';
  }});
  btn.style.outline = '3px solid #1f2d3d';
  btn.style.boxShadow = '0 0 0 2px #fff inset';
}}

function paintCell(td) {{
  var pen = window.currentPen;
  if (!pen) return;
  td.style.background = pen.bg;
  td.style.color = pen.fg;
  td.textContent = (pen.mark === '' || pen.mark == null) ? '\\u00b7' : pen.mark;
  td.title = pen.mark;
  Shiny.setInputValue('cell_paint', {{
    row: td.dataset.row, day: td.dataset.day, value: pen.code, t: Date.now()
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


def _chip_pen(code, label, bg, fg, activo=False) -> str:
    """Un botón de la paleta. Para categorías la marca es su letra; para
    asignaturas, su nombre."""
    if code in CATEGORIAS:
        mark = code if code else "·"
    else:
        mark = label
    args = ", ".join(json.dumps(x) for x in (code, bg, fg, mark))
    onclick = html_lib.escape(f"setPen({args}, this)", quote=True)
    act = ("outline:3px solid #1f2d3d;box-shadow:0 0 0 2px #fff inset;"
           if activo else "")
    return (f'<button type="button" class="pen-btn" '
            f'onclick="{onclick}" '
            f'style="background:{bg};color:{fg};border:none;{act}'
            f'padding:9px 16px;border-radius:9px;font-weight:700;font-size:13px;'
            f'cursor:pointer;margin:3px;">{html_lib.escape(label)}</button>')


def paleta_html(asignaturas: list) -> str:
    chips = [_chip_pen("L", "Lectiva (genérica)",
                       CATEGORIAS["L"][1], CATEGORIAS["L"][2],
                       activo=not asignaturas)]
    for a in asignaturas:
        chips.append(_chip_pen(a["id"], a["nombre"], a["color"], "#ffffff"))
    for code in ["N", "R", "C", "D", ""]:
        label, bg, fg = CATEGORIAS[code]
        chips.append(_chip_pen(code, label, bg, fg))
    aviso = ('<div style="font-size:12px;color:#7a8a99;margin-bottom:8px;">'
             'Elige una asignatura (o categoría) y haz clic sobre los bloques de '
             'la grilla para pintarlos. Puedes repintar un bloque cuando quieras.'
             '</div>')
    return '<div style="margin:4px 0 10px 0;">' + "".join(chips) + '</div>' + aviso


# -----------------------------------------------------------------------------
# 7. GRILLA PINTABLE  (paso 2)
# -----------------------------------------------------------------------------

def grilla_pintable_html(df: pd.DataFrame, mapa: dict, dur_df=None) -> str:
    cell_css = ("padding:8px 4px;text-align:center;font-weight:700;"
                "font-size:12px;border:1px solid #ffffff;border-radius:5px;"
                "cursor:pointer;user-select:none;transition:transform .05s;"
                "word-break:break-word;line-height:1.15;")
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
            code = str(df.at[i, d]).strip()
            if code not in mapa:
                code = ""
            label, bg, fg = mapa[code]
            if code == "":
                txt = "·"
            elif code in CATEGORIAS:
                txt = code
            else:
                txt = html_lib.escape(label)
            tip = label
            if dur_df is not None and code != "":
                eff = minutos_celda(df, dur_df, i, d)
                tip = f"{label} · aporta {eff} min"
            html.append(
                f'<td title="{html_lib.escape(tip)}" data-row="{i}" data-day="{d}" '
                f'onclick="paintCell(this)" '
                f'style="{cell_css}background:{bg};color:{fg};">{txt}</td>'
            )
        html.append("</tr>")
    html.append("</table></div>")
    return "".join(html)


# -----------------------------------------------------------------------------
# 8. GRILLA DE RESULTADO CON ETIQUETAS  (paso 3)
# -----------------------------------------------------------------------------

def grilla_resultado_html(res: pd.DataFrame, labels: dict, mapa: dict, dur_df=None) -> str:
    cell_css = ("padding:3px;border:1px solid #ffffff;border-radius:5px;"
                "position:relative;")
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
            code = str(res.at[i, d]).strip()
            if code not in mapa or code == "":
                html.append(f'<td style="{cell_css}background:#fbfcfd;"></td>')
                continue
            cat_label, bg, fg = mapa[code]
            key = f"{i}_{d}"
            es_asig = es_asignatura(code)
            if es_asig:
                # La asignatura ya trae su nombre; el detalle es opcional.
                valor = labels.get(key, cat_label)
                placeholder = ""
            elif code in ("L", "N", "P"):
                valor = labels.get(key, "")
                placeholder = cat_label
            else:
                valor = labels.get(key, cat_label)
                placeholder = ""
            valor = html_lib.escape(str(valor), quote=True)
            placeholder = html_lib.escape(str(placeholder), quote=True)

            # Minutos efectivos que aporta esta celda + distintivo si es parcial.
            eff = minutos_celda(res, dur_df, i, d) if dur_df is not None else None
            nomi = nominal_min(res, i)
            badge = ""
            tip = f"{cat_label}"
            if eff is not None:
                tip = f"{cat_label} · aporta {eff} min al total"
                if nomi is not None and eff != nomi:
                    # El bloque no aporta su duración completa (ej. salida 14:15).
                    col = "#b45309" if eff > 0 else "#b71c1c"
                    badge = (f'<span title="{html_lib.escape(tip)}" '
                             f'style="position:absolute;top:1px;right:3px;font-size:9px;'
                             f'font-weight:800;color:{col};background:#fff7ed;'
                             f'border:1px solid {col};border-radius:4px;padding:0 3px;'
                             f'line-height:1.3;pointer-events:none;">{eff}m</span>')
            tip = html_lib.escape(tip, quote=True)
            html.append(
                f'<td title="{tip}" style="{cell_css}background:{bg};">{badge}'
                f'<input type="text" value="{valor}" placeholder="{placeholder}" '
                f'title="{tip}" '
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
    if r.get("sobrante", 0) > 0:
        avisos.append(("#fdecea", "#b71c1c",
                       f"Tu jornada tiene más tiempo del que exige el contrato: quedaron "
                       f"{r.get('sobrante_bloques', 0)} bloques Disponibles "
                       f"({fmt_horas(r['sobrante'])}) sin asignar después de optimizar. "
                       "Si no corresponde, recorta el horario del paso 1 o corrige las "
                       "horas de contrato."))
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
# 8b. TABLA DE SUMA DE HORAS  (desglose por día + bloques nuevos de preparación)
# -----------------------------------------------------------------------------

def desglose_horas(res: pd.DataFrame, dur_df: pd.DataFrame) -> dict:
    """
    Minutos por categoría y día del horario resultante. "L" agrupa la lectiva
    genérica y las asignaturas (S1, S2, ...). Solo suma minutos efectivos
    (minutos_celda), igual que el optimizador y el resumen.
    """
    out = {c: {d: 0 for d in DIAS} for c in ("L", "N", "P", "R")}
    for i in range(len(res)):
        for d in DIAS:
            c = str(res.at[i, d]).strip().upper()
            if not c:
                continue
            m = minutos_celda(res, dur_df, i, d)
            if m <= 0:
                continue
            if c == "L" or es_asignatura(c):
                out["L"][d] += m
            elif c in ("N", "P", "R"):
                out[c][d] += m
    return out


def bloques_preparacion(res: pd.DataFrame, dur_df: pd.DataFrame) -> list:
    """
    Los bloques "P" del resultado (los que agrega la optimización), agrupados
    por día y en orden cronológico: dónde quedaron y cuántos minutos aportan.
    """
    out = []
    for d in DIAS:
        for i in range(len(res)):
            if str(res.at[i, d]).strip().upper() != "P":
                continue
            out.append({
                "dia": d,
                "bloque": str(res.at[i, "Bloque"]),
                "horario": f"{res.at[i, 'Inicio']}–{res.at[i, 'Fin']}",
                "min": minutos_celda(res, dur_df, i, d),
            })
    return out


def tabla_horas_html(res: pd.DataFrame, dur_df: pd.DataFrame) -> str:
    des = desglose_horas(res, dur_df)
    prep = bloques_preparacion(res, dur_df)

    head_css = ("padding:6px 8px;font-size:12px;color:#5b6b7b;font-weight:600;"
                "border-bottom:2px solid #e3e8ee;text-align:center;")
    cell_css = "padding:5px 8px;font-size:12px;text-align:center;color:#37474f;"
    title_css = ("font-size:12px;color:#7a8a99;text-transform:uppercase;"
                 "letter-spacing:.04em;margin:0 0 6px 0;")

    def celda(mins, bold=False):
        txt = fmt_horas(mins) if mins > 0 else "—"
        peso = "font-weight:700;" if bold else ""
        return f'<td style="{cell_css}{peso}">{txt}</td>'

    # --- A. Desglose de la suma por día y categoría --------------------------
    filas_cat = [
        ("L", "Lectiva (clases y asignaturas)"),
        ("N", "No lectiva asignada por ti"),
        ("P", "Preparación (bloques nuevos)"),
        ("R", "Recreo"),
    ]
    a = ['<table style="border-collapse:collapse;width:100%;'
         'font-family:system-ui,sans-serif;">']
    a.append(f'<tr><th style="{head_css}text-align:left;">Categoría</th>')
    for d in DIAS:
        a.append(f'<th style="{head_css}">{DIAS_NOMBRE[d]}</th>')
    a.append(f'<th style="{head_css}">Semana</th></tr>')

    tot_dia = {d: 0 for d in DIAS}
    for code, nombre in filas_cat:
        _, bg, _ = CATEGORIAS[code]
        chip = (f'<span style="display:inline-block;width:12px;height:12px;'
                f'border-radius:3px;background:{bg};margin-right:6px;'
                f'vertical-align:-1px;"></span>')
        a.append(f'<tr><td style="{cell_css}text-align:left;">{chip}{nombre}</td>')
        tot_cat = 0
        for d in DIAS:
            m = des[code][d]
            tot_cat += m
            tot_dia[d] += m
            a.append(celda(m))
        a.append(celda(tot_cat, bold=True) + "</tr>")

    a.append(f'<tr style="border-top:2px solid #e3e8ee;">'
             f'<td style="{cell_css}text-align:left;font-weight:700;">Total trabajado</td>')
    for d in DIAS:
        a.append(celda(tot_dia[d], bold=True))
    a.append(celda(sum(tot_dia.values()), bold=True) + "</tr></table>")
    tabla_desglose = "".join(a)

    # --- B. Dónde quedaron los bloques nuevos (Preparación) ------------------
    if not prep:
        tabla_prep = ('<div style="font-size:13px;color:#9aa7b3;">La optimización '
                      'no agregó bloques nuevos de preparación.</div>')
    else:
        b = ['<table style="border-collapse:collapse;width:100%;'
             'font-family:system-ui,sans-serif;">']
        b.append(f'<tr><th style="{head_css}text-align:left;">Día</th>'
                 f'<th style="{head_css}text-align:left;">Bloque</th>'
                 f'<th style="{head_css}">Horario</th>'
                 f'<th style="{head_css}">Aporta</th></tr>')
        dia_prev = None
        for p in prep:
            dia_txt = DIAS_NOMBRE[p["dia"]] if p["dia"] != dia_prev else ""
            dia_prev = p["dia"]
            borde = ("border-top:1px solid #e3e8ee;" if dia_txt else "")
            b.append(
                f'<tr><td style="{cell_css}{borde}text-align:left;font-weight:700;">'
                f'{dia_txt}</td>'
                f'<td style="{cell_css}{borde}text-align:left;">{p["bloque"]}</td>'
                f'<td style="{cell_css}{borde}">{p["horario"]}</td>'
                f'<td style="{cell_css}{borde}font-weight:600;">{p["min"]} min</td></tr>'
            )
        total_prep = sum(p["min"] for p in prep)
        b.append(f'<tr style="border-top:2px solid #e3e8ee;">'
                 f'<td colspan="3" style="{cell_css}text-align:left;font-weight:700;">'
                 f'Total: {len(prep)} bloques nuevos</td>'
                 f'<td style="{cell_css}font-weight:700;">{fmt_horas(total_prep)}</td>'
                 f'</tr></table>')
        tabla_prep = "".join(b)

    nota = ('<div style="font-size:11px;color:#9aa7b3;margin-top:8px;">'
            'La colación no suma horas. El recreo suma al total trabajado pero no '
            'entra en la proporción 65/35 lectiva/no lectiva.</div>')

    # Todo el detalle va plegado en un <details>: no alarga la página y se
    # expande con un clic cuando el usuario quiere verificar la suma.
    summary = (
        '<summary style="cursor:pointer;font-size:13px;font-weight:600;color:#445;'
        'user-select:none;">Detalle de la suma de horas y bloques nuevos '
        '<span style="font-weight:400;color:#7a8a99;">(clic para ver/ocultar)</span>'
        '</summary>'
    )
    return (
        '<details style="background:#f8fafc;border:1px solid #e3e8ee;border-radius:10px;'
        'padding:12px 14px;margin-top:12px;">'
        + summary +
        f'<div style="{title_css}margin-top:12px;">Suma de horas por día</div>'
        + tabla_desglose +
        f'<div style="{title_css}margin-top:14px;">Bloques nuevos de preparación '
        '(agregados por la optimización)</div>'
        + tabla_prep + nota + '</details>'
    )


# -----------------------------------------------------------------------------
# 9. GENERACIÓN DE PDF IMPRIMIBLE
# -----------------------------------------------------------------------------

def generar_pdf(res: pd.DataFrame, labels: dict, resumen: dict, mapa: dict,
                titulo: str = "Horario semanal") -> bytes:
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
            code = str(res.at[i, d]).strip()
            if code == "" or code not in mapa:
                fila.append("")
                continue
            cat_label = mapa[code][0]
            # Para lectiva/no lectiva genéricas el nombre no aporta; para
            # asignaturas y el resto se muestra la etiqueta por defecto.
            defecto = "" if code in ("L", "N") else cat_label
            texto = labels.get(f"{i}_{d}", "").strip() or defecto
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
            code = str(res.at[i, d]).strip()
            if code not in mapa:
                code = ""
            _, bg, fg = mapa[code]
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
                ui.div(ui.input_text(f"ingreso_{d}", "Ingreso", value="07:55",
                                     placeholder="HH:MM", width="115px")),
                ui.panel_conditional(
                    f"input.colacion_{d}",
                    ui.div(
                        ui.div(ui.input_text(f"salida_alm_{d}", "Salida (almuerzo)",
                                             value="13:00", placeholder="HH:MM",
                                             width="150px")),
                        ui.div(ui.input_text(f"regreso_{d}", "Regreso", value="14:00",
                                             placeholder="HH:MM", width="115px")),
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
    'Escribe cada hora en formato <b>HH:MM</b> (24h), incluida la de ingreso. Si '
    'un día no tiene colación, desactiva "Tiene colación" y solo verás '
    'Ingreso/Salida. Si tu semana se repite, configura el primer día y usa '
    '"Copiar a los demás días". Puedes <b>corregir un solo día y volver a aplicar</b>: '
    'lo que ya hayas pintado en el resto de la semana se conserva.</div>'
)

INSTRUCCIONES_ASIG = ui.HTML(
    '<div style="font-size:13px;color:#445;line-height:1.5;margin-bottom:10px;">'
    'Registra cada asignatura o curso que dicta el profesor, con cuántos '
    '<b>bloques por semana</b> le corresponden y un color para reconocerla en la '
    'grilla. Luego, en el paso siguiente, la pintas sobre los bloques y la app te '
    'avisa cuántos te faltan o te sobran.</div>'
)

INSTRUCCIONES_PASO2 = ui.HTML(
    '<div style="font-size:13px;color:#445;line-height:1.5;margin-bottom:4px;">'
    'Esta grilla ya viene marcada con tu jornada, colación y recreos. Ahora marca '
    'tus clases eligiendo la <b>asignatura</b> en la paleta (o <b>Lectiva</b> '
    'genérica) y los compromisos fijos como <b>No lectiva</b>; haz clic sobre los '
    'bloques. Puedes repintar cualquier bloque para corregirlo. Lo que dejes en '
    '<b>Disponible</b> se completará al optimizar.</div>'
)

INSTRUCCIONES_PASO3 = ui.HTML(
    '<div style="font-size:13px;color:#445;line-height:1.5;margin-bottom:4px;">'
    'Escribe el detalle de cada bloque (ej. "Matemáticas 8°A", "Reunión de '
    'apoderados"). El cambio se guarda automáticamente al salir del casillero. '
    '<b>Pasa el cursor sobre un bloque</b> para ver cuántos minutos aporta al '
    'total; los bloques que aportan <b>menos</b> que su duración (por una salida a '
    'mitad de bloque) muestran un distintivo naranjo con los minutos reales. '
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
        ui.card_header("2 · Asignaturas del profesor"),
        INSTRUCCIONES_ASIG,
        ui.div(
            ui.input_text("asig_nombre", "Nombre", placeholder="Ej: Matemáticas 8°A",
                          width="230px"),
            ui.input_numeric("asig_horas", "Bloques/semana", value=6, min=1, max=40,
                             step=1, width="140px"),
            ui.input_select("asig_color", "Color",
                            {hexc: nom for hexc, nom in COLORES_ASIG}, width="150px"),
            ui.input_action_button("asig_add", "Agregar", class_="btn-primary"),
            style="display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:10px;",
        ),
        ui.output_ui("lista_asig"),
        ui.div(
            ui.input_select("asig_del", "Quitar asignatura", {}, width="230px"),
            ui.input_action_button("asig_remove", "Quitar seleccionada",
                                   class_="btn-outline-secondary"),
            style="display:flex;gap:10px;align-items:end;margin-top:8px;",
        ),
    ),
    ui.card(
        ui.card_header("3 · Marca tus bloques"),
        INSTRUCCIONES_PASO2,
        ui.output_ui("paleta_ui"),
        ui.output_ui("grilla_edit"),
        ui.output_ui("validacion_asig"),
        ui.div(
            ui.input_action_button("optimizar", "Optimizar horario", class_="btn-primary"),
            style="margin-top:12px;",
        ),
    ),
    ui.card(
        ui.card_header("4 · Horario optimizado — agrega el detalle"),
        INSTRUCCIONES_PASO3,
        ui.output_ui("leyenda_resultado"),
        ui.output_ui("grilla_resultado"),
        ui.output_ui("resumen_semana"),
        ui.output_ui("tabla_horas"),
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

    grid_state = reactive.value(catalogo_vacio())             # códigos L/N/R/C/D o S#
    dur_state = reactive.value(catalogo_vacio_duraciones())   # minutos efectivos por celda
    grid_version = reactive.value(0)                # fuerza re-render de la grilla 2
    labels_rv = reactive.value({})                  # etiquetas del horario final
    resultado_rv = reactive.value(None)             # (df_resultado, resumen)
    errores_rv = reactive.value([])
    asig_state = reactive.value([])                 # asignaturas: {id, nombre, color, horas}
    asig_counter = reactive.value(0)                # correlativo para los ids S#

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
            ui.update_text(f"ingreso_{d}", value=ingreso)
            ui.update_text(f"salida_alm_{d}", value=salida_alm)
            ui.update_text(f"regreso_{d}", value=regreso)
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
        # Preserva lo ya pintado (clases/no lectiva/asignaturas) donde siga
        # habiendo tiempo de trabajo: corregir un día no borra la semana.
        df = fusionar_pintura(df, grid_state.get())
        grid_state.set(df)
        dur_state.set(dur)
        grid_version.set(grid_version.get() + 1)
        errores_rv.set(errores)
        resultado_rv.set(None)  # la optimización previa queda obsoleta

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
        asig_state.set([])
        asig_counter.set(0)

    # --- 2. Grilla pintable ---------------------------------------------------

    @render.ui
    def paleta_ui():
        return ui.HTML(paleta_html(asig_state.get()))

    @render.ui
    def grilla_edit():
        grid_version.get()   # dependencias explícitas de re-render
        asig_state.get()     # re-render al agregar/quitar asignaturas (colores)
        with reactive.isolate():
            df = grid_state.get()
            dur = dur_state.get()
            mapa = mapa_render(asig_state.get())
        return ui.HTML(grilla_pintable_html(df, mapa, dur))

    @reactive.effect
    @reactive.event(input.cell_paint)
    def _on_paint():
        info = input.cell_paint()
        row = int(info["row"])
        day = info["day"]
        value = info["value"]
        df = grid_state.get()
        grid_state.set(aplicar_pintura(df, row, day, value))

    # --- 2b. Asignaturas ------------------------------------------------------

    @reactive.effect
    @reactive.event(input.asig_add)
    def _asig_add():
        nombre = (input.asig_nombre() or "").strip()
        if not nombre:
            return
        n = asig_counter.get() + 1
        asig_counter.set(n)
        nueva = {
            "id": f"S{n}",
            "nombre": nombre,
            "color": input.asig_color(),
            "horas": int(input.asig_horas() or 0),
        }
        asig_state.set(asig_state.get() + [nueva])
        ui.update_text("asig_nombre", value="")

    @reactive.effect
    @reactive.event(input.asig_remove)
    def _asig_remove():
        sel = input.asig_del()
        if not sel:
            return
        asig_state.set([a for a in asig_state.get() if a["id"] != sel])
        # Las celdas pintadas con esa asignatura vuelven a "Disponible".
        df = grid_state.get().copy()
        for d in DIAS:
            df[d] = ["D" if str(v).strip() == sel else v for v in df[d]]
        grid_state.set(df)
        grid_version.set(grid_version.get() + 1)

    @reactive.effect
    def _sync_asig_del():
        asigs = asig_state.get()
        choices = {a["id"]: a["nombre"] for a in asigs}
        ui.update_select("asig_del", choices=choices)

    @render.ui
    def lista_asig():
        asigs = asig_state.get()
        if not asigs:
            return ui.HTML(
                '<div style="font-size:13px;color:#9aa7b3;">Aún no agregas '
                'asignaturas. Agrega al menos una para pintarla en la grilla.</div>'
            )
        chips = []
        for a in asigs:
            chips.append(
                f'<span style="display:inline-flex;align-items:center;gap:6px;'
                f'background:{a["color"]};color:#fff;padding:5px 10px;border-radius:8px;'
                f'font-size:12px;font-weight:700;margin:3px;">'
                f'{html_lib.escape(a["nombre"])} · {int(a.get("horas") or 0)} bloques'
                f'</span>'
            )
        return ui.HTML('<div>' + "".join(chips) + '</div>')

    @render.ui
    def validacion_asig():
        asigs = asig_state.get()
        if not asigs:
            return ui.HTML("")
        df = grid_state.get()
        dur = dur_state.get()
        n = len(df)

        filas = []
        for a in asigs:
            cnt = mins = 0
            for i in range(n):
                for d in DIAS:
                    if str(df.at[i, d]).strip() == a["id"]:
                        cnt += 1
                        mins += minutos_celda(df, dur, i, d)
            objetivo = int(a.get("horas") or 0)
            falta = objetivo - cnt
            if falta > 0:
                estado, col = f"faltan {falta}", "#8a6d00"
            elif falta < 0:
                estado, col = f"{-falta} de más", "#b71c1c"
            else:
                estado, col = "completo", "#2e7d32"
            filas.append(
                f'<div style="display:flex;align-items:center;gap:8px;margin:3px 0;'
                f'font-size:13px;">'
                f'<span style="display:inline-block;width:14px;height:14px;'
                f'border-radius:3px;background:{a["color"]};"></span>'
                f'<b>{html_lib.escape(a["nombre"])}</b>'
                f'<span style="color:#5b6b7b;">{cnt}/{objetivo} bloques · '
                f'{fmt_horas(mins)}</span>'
                f'<span style="color:{col};font-weight:600;">{estado}</span></div>'
            )
        return ui.HTML(
            '<div style="background:#f8fafc;border:1px solid #e3e8ee;border-radius:10px;'
            'padding:10px 12px;margin-top:10px;">'
            '<div style="font-size:12px;color:#7a8a99;text-transform:uppercase;'
            'letter-spacing:.04em;margin-bottom:4px;">Avance por asignatura</div>'
            + "".join(filas) + '</div>'
        )

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
            mapa = mapa_render(asig_state.get())
            dur = dur_state.get()
        return ui.HTML(grilla_resultado_html(res, labels, mapa, dur))

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

    @render.ui
    def tabla_horas():
        data = resultado_rv.get()
        if data is None:
            return ui.HTML("")
        res, _ = data
        with reactive.isolate():
            dur = dur_state.get()
        return ui.HTML(tabla_horas_html(res, dur))

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
        mapa = mapa_render(asig_state.get())
        yield generar_pdf(res, labels, resumen, mapa, "Horario semanal")


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
