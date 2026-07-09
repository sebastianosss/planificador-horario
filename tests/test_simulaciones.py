# Simulaciones de casos completos: configurar contrato → pintar clases →
# optimizar → verificar que el conteo de horas (lectiva + no lectiva + recreo +
# preparación) cuadra exactamente, y que la tabla de suma reporta dónde
# quedaron los bloques nuevos.
#
# Se ejecutan con:  .venv/Scripts/python.exe -m pytest

from app import (
    DIAS,
    bloques_preparacion,
    construir_esqueleto_pure,
    desglose_horas,
    filas_catalogo,
    fmt_horas,
    optimizar,
    resumen_html,
    tabla_horas_html,
)

IDX = {nombre: i for i, (_, _, nombre, _) in enumerate(filas_catalogo())}


def semana_completa():
    """5 días de 08:00-13:00 y 14:00-16:30 con colación 13:00-14:00.

    Por día: 9 bloques Disponibles de 45' (405 min) + 3 recreos de 15' (45 min)
    + colación de 60' (no suma). Capacidad semanal: 2025 min D + 225 min R.
    """
    cfg = {"trabaja": True, "colacion": True, "ingreso": "08:00",
           "salida_alm": "13:00", "regreso": "14:00", "salida": "16:30"}
    return construir_esqueleto_pure({d: dict(cfg) for d in DIAS})


class TestSimulacionSemanaCompleta:
    """Profesor con 30h de contrato, 2 asignaturas (6 y 4 bloques) y 2 bloques
    de compromisos no lectivos fijos."""

    def _armar(self):
        df, dur, errores = semana_completa()
        assert errores == []
        # S1 "Matemáticas": 6 bloques (Lun/Mar/Mie a primera hora).
        for d in ("Lun", "Mar", "Mie"):
            df.at[IDX["Bloque 1"], d] = "S1"
            df.at[IDX["Bloque 2"], d] = "S1"
        # S2 "Ciencias": 4 bloques (Jue/Vie a primera hora).
        for d in ("Jue", "Vie"):
            df.at[IDX["Bloque 1"], d] = "S2"
            df.at[IDX["Bloque 2"], d] = "S2"
        # No lectiva fija: reuniones lunes y martes después de almuerzo.
        df.at[IDX["Bloque 7"], "Lun"] = "N"
        df.at[IDX["Bloque 7"], "Mar"] = "N"
        return df, dur

    def test_conteo_lectiva_y_no_lectiva(self):
        df, dur = self._armar()
        res, r = optimizar(df, dur, horas_contrato=30)  # 1800 min

        assert r["status"] == "ok"
        assert r["lectiva"] == 10 * 45          # 450: S1 (6) + S2 (4)
        assert r["nolectiva"] == 2 * 45         # 90: las dos reuniones
        assert r["recreo"] == 5 * 45            # 225: 3 recreos de 15' x 5 días
        # fijo = 765; la preparación completa exactamente el contrato.
        assert r["prep"] == 1800 - 765          # 1035 = 23 bloques de 45'
        assert r["total"] == 1800
        assert r["deficit"] == 0
        # Proporción legal: 25% lectiva, muy bajo el tope de 65%.
        assert r["lectiva"] / r["contract"] <= 0.65

    def test_aviso_de_jornada_sobre_el_contrato(self):
        # La jornada suma 37,5h de permanencia pero el contrato es de 30h:
        # tras optimizar deben sobrar 10 bloques Disponibles (7h 30m) y el
        # resumen debe advertirlo como error.
        df, dur = self._armar()
        _, r = optimizar(df, dur, horas_contrato=30)
        assert r["disponible"] == 33 * 45       # 1485: bloques D no pintados
        assert r["sobrante"] == 450             # 1485 - 1035 de preparación
        assert r["sobrante_bloques"] == 10
        html = resumen_html(r)
        assert "10 bloques Disponibles" in html
        assert fmt_horas(450) in html           # "7h 30m"
        assert "sin asignar" in html

    def test_desglose_cuadra_con_el_resumen(self):
        df, dur = self._armar()
        res, r = optimizar(df, dur, horas_contrato=30)
        des = desglose_horas(res, dur)

        assert sum(des["L"].values()) == r["lectiva"]
        assert sum(des["N"].values()) == r["nolectiva"]
        assert sum(des["P"].values()) == r["prep"]
        assert sum(des["R"].values()) == r["recreo"]
        # El total del desglose es el mismo total trabajado del resumen.
        total_desglose = sum(sum(des[c].values()) for c in ("L", "N", "P", "R"))
        assert total_desglose == r["total"]
        # Y por día: lectiva pintada a primera hora = 90 min cada día.
        for d in DIAS:
            assert des["L"][d] == 90

    def test_bloques_nuevos_ubicados_y_sumados(self):
        df, dur = self._armar()
        res, r = optimizar(df, dur, horas_contrato=30)
        prep = bloques_preparacion(res, dur)

        # La lista reporta exactamente los bloques P y su aporte suma el resumen.
        assert len(prep) == 23
        assert sum(p["min"] for p in prep) == r["prep"] == 1035
        assert all(p["min"] == 45 for p in prep)
        # Cada bloque nuevo quedó donde antes había un Disponible.
        for p in prep:
            fila = [i for i, (_, _, nom, _) in enumerate(filas_catalogo())
                    if nom == p["bloque"]]
            assert df.at[fila[0], p["dia"]] == "D"
        # Vienen agrupados en el orden de la semana.
        orden_dias = [DIAS.index(p["dia"]) for p in prep]
        assert orden_dias == sorted(orden_dias)

    def test_tabla_html_reporta_totales(self):
        df, dur = self._armar()
        res, r = optimizar(df, dur, horas_contrato=30)
        html = tabla_horas_html(res, dur)

        assert "Suma de horas por día" in html
        assert "Bloques nuevos de preparación" in html
        assert "Total: 23 bloques nuevos" in html
        assert fmt_horas(1035) in html           # "17h 15m"
        assert fmt_horas(r["total"]) in html     # total trabajado semanal
        # El detalle va plegado por defecto en un <details> desplegable.
        assert html.lstrip().startswith("<details")
        assert "<summary" in html
        assert "<details open" not in html


class TestSimulacionBloqueParcial:
    """Día corto con salida 12:30: el último bloque solo aporta 15 minutos y
    la optimización debe usarlo para acercarse al contrato."""

    def _armar(self):
        horarios = {d: {"trabaja": False} for d in DIAS}
        horarios["Lun"] = {"trabaja": True, "colacion": False,
                           "ingreso": "08:00", "salida": "12:30"}
        df, dur, errores = construir_esqueleto_pure(horarios)
        assert errores == []
        df.at[IDX["Bloque 1"], "Lun"] = "L"
        return df, dur

    def test_usa_el_bloque_parcial(self):
        df, dur = self._armar()
        res, r = optimizar(df, dur, horas_contrato=4)  # 240 min

        # fijo = 45 lectiva + 30 recreo = 75; espacio para prep = 165.
        # Con bloques de 45' y uno parcial de 15', el máximo alcanzable es 150.
        assert r["status"] == "ok"
        assert r["prep"] == 150
        assert r["total"] == 225
        assert r["deficit"] == 15

        prep = bloques_preparacion(res, dur)
        assert sum(p["min"] for p in prep) == 150
        # El bloque 6 (12:15-13:00) entra con sus 15 minutos reales.
        parciales = [p for p in prep if p["min"] == 15]
        assert len(parciales) == 1
        assert parciales[0]["bloque"] == "Bloque 6"
        assert parciales[0]["dia"] == "Lun"

    def test_tabla_html_sin_bloques_nuevos(self):
        # Con contrato mínimo no cabe preparación: la tabla lo dice claro.
        df, dur = self._armar()
        res, r = optimizar(df, dur, horas_contrato=1)
        assert r["status"] == "sobrecarga"
        html = tabla_horas_html(res, dur)
        assert "no agregó bloques nuevos" in html
