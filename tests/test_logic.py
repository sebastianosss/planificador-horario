# Tests de la lógica pura del Planificador de horario docente (app.py).
# Se ejecutan con:  .venv/Scripts/python.exe -m pytest
# No levantan Shiny: prueban las funciones puras que usa el server.

import pytest

from app import (
    DIAS,
    aplicar_etiqueta,
    aplicar_pintura,
    catalogo_vacio,
    catalogo_vacio_duraciones,
    construir_esqueleto_pure,
    es_asignatura,
    filas_catalogo,
    fmt_horas,
    fusionar_pintura,
    generar_pdf,
    mapa_render,
    minutos_celda,
    optimizar,
    resumen_html,
    time_to_mins,
)

# Índice de fila por nombre de bloque, para no depender de posiciones mágicas.
IDX = {nombre: i for i, (_, _, nombre, _) in enumerate(filas_catalogo())}


def dia_cfg(**kw):
    """Config de un día de trabajo estándar; los kwargs la modifican."""
    base = {"trabaja": True, "colacion": True, "ingreso": "07:55",
            "salida_alm": "13:00", "regreso": "14:00", "salida": "18:30"}
    base.update(kw)
    return base


def solo_lunes(**kw):
    """Semana con solo el lunes trabajado."""
    horarios = {d: {"trabaja": False} for d in DIAS}
    horarios["Lun"] = dia_cfg(**kw)
    return horarios


# -----------------------------------------------------------------------------
# Utilidades de tiempo
# -----------------------------------------------------------------------------

class TestTimeToMins:
    def test_hora_valida(self):
        assert time_to_mins("07:55") == 475
        assert time_to_mins("00:00") == 0
        assert time_to_mins(" 18:30 ") == 1110

    def test_invalidas_devuelven_none(self):
        assert time_to_mins("") is None
        assert time_to_mins("8") is None
        assert time_to_mins("ocho:15") is None
        assert time_to_mins(None) is None


def test_fmt_horas():
    assert fmt_horas(0) == "0h 00m"
    assert fmt_horas(90) == "1h 30m"
    assert fmt_horas(300) == "5h 00m"


def test_es_asignatura():
    assert es_asignatura("S1")
    assert es_asignatura("S12")
    assert not es_asignatura("L")
    assert not es_asignatura("s1")      # el pincel siempre llega en mayúscula
    assert not es_asignatura("S")
    assert not es_asignatura("")


# -----------------------------------------------------------------------------
# Asistente de horario por contrato
# -----------------------------------------------------------------------------

class TestConstruirEsqueleto:
    def test_dia_estandar_con_colacion(self):
        df, dur, errores = construir_esqueleto_pure(solo_lunes())
        assert errores == []
        # Bloque completo dentro de la jornada.
        assert df.at[IDX["Bloque 1"], "Lun"] == "D"
        assert dur.at[IDX["Bloque 1"], "Lun"] == 45
        # El ingreso 07:55 alcanza a cubrir el saludo (5 min).
        assert df.at[IDX["Ingreso / Saludo"], "Lun"] == "D"
        assert dur.at[IDX["Ingreso / Saludo"], "Lun"] == 5
        # Los recreos dentro de la jornada quedan marcados.
        assert df.at[IDX["Recreo 1"], "Lun"] == "R"
        assert dur.at[IDX["Recreo 1"], "Lun"] == 15
        # La colación 13:00-14:00 cae completa en el Bloque 6b.
        assert df.at[IDX["Bloque 6b"], "Lun"] == "C"
        assert dur.at[IDX["Bloque 6b"], "Lun"] == 60
        # Salida 18:30: el Bloque 11 (17:30-18:30) cubre justo hasta la salida.
        assert df.at[IDX["Bloque 11"], "Lun"] == "D"
        assert dur.at[IDX["Bloque 11"], "Lun"] == 60

    def test_salida_a_mitad_de_bloque(self):
        df, dur, errores = construir_esqueleto_pure(solo_lunes(salida="14:15"))
        assert errores == []
        # Bloque 7 (14:00-14:45) aporta solo los 15 min trabajados.
        assert df.at[IDX["Bloque 7"], "Lun"] == "D"
        assert dur.at[IDX["Bloque 7"], "Lun"] == 15
        # Después de la salida no hay jornada.
        assert df.at[IDX["Bloque 8"], "Lun"] == ""
        assert dur.at[IDX["Bloque 8"], "Lun"] == 0

    def test_dia_sin_colacion(self):
        horarios = solo_lunes(colacion=False, ingreso="08:00", salida="13:00")
        df, dur, errores = construir_esqueleto_pure(horarios)
        assert errores == []
        assert df.at[IDX["Bloque 6"], "Lun"] == "D"
        # Sin colación y con salida 13:00, el Bloque 6b queda fuera.
        assert df.at[IDX["Bloque 6b"], "Lun"] == ""
        assert dur.at[IDX["Bloque 6b"], "Lun"] == 0

    def test_dia_no_trabajado_queda_vacio(self):
        df, dur, errores = construir_esqueleto_pure(solo_lunes())
        assert errores == []
        assert (df["Mar"] == "").all()
        assert (dur["Mar"] == 0).all()

    def test_orden_invalido_reporta_error_y_no_pinta(self):
        horarios = solo_lunes(ingreso="14:00", salida_alm="13:00")
        df, dur, errores = construir_esqueleto_pure(horarios)
        assert len(errores) == 1
        assert "Lunes" in errores[0]
        assert (df["Lun"] == "").all()

    def test_formato_invalido_reporta_error(self):
        horarios = solo_lunes(colacion=False, ingreso="ocho", salida="13:00")
        _, _, errores = construir_esqueleto_pure(horarios)
        assert len(errores) == 1
        assert "Lunes" in errores[0]


# -----------------------------------------------------------------------------
# Minutos efectivos por celda
# -----------------------------------------------------------------------------

class TestMinutosCelda:
    def test_cero_explicito_se_respeta(self):
        df = catalogo_vacio()
        dur = catalogo_vacio_duraciones()
        dur.at[IDX["Bloque 1"], "Lun"] = 0
        assert minutos_celda(df, dur, IDX["Bloque 1"], "Lun") == 0

    def test_sin_dato_cae_a_duracion_nominal(self):
        df = catalogo_vacio()
        dur = catalogo_vacio_duraciones()
        dur["Lun"] = [float("nan")] * len(dur)
        assert minutos_celda(df, dur, IDX["Bloque 1"], "Lun") == 45
        assert minutos_celda(df, dur, IDX["Recreo 1"], "Lun") == 15


# -----------------------------------------------------------------------------
# Pintar y etiquetar
# -----------------------------------------------------------------------------

class TestAplicarPintura:
    def test_pinta_categoria_y_asignatura(self):
        df = catalogo_vacio()
        df2 = aplicar_pintura(df, 1, "Lun", "L")
        assert df2.at[1, "Lun"] == "L"
        assert df.at[1, "Lun"] == ""  # no muta el original
        df3 = aplicar_pintura(df2, 2, "Mar", "s1")  # normaliza a mayúscula
        assert df3.at[2, "Mar"] == "S1"

    def test_valores_invalidos_no_cambian_nada(self):
        df = catalogo_vacio()
        assert aplicar_pintura(df, 1, "Lun", "Z9").at[1, "Lun"] == ""
        assert (aplicar_pintura(df, 999, "Lun", "L")["Lun"] == "").all()
        assert aplicar_pintura(df, 1, "Domingo", "L") is df


class TestFusionarPintura:
    def test_sin_estado_previo_devuelve_nuevo(self):
        nuevo = catalogo_vacio()
        assert fusionar_pintura(nuevo, None) is nuevo

    def test_repone_pintura_solo_sobre_disponible(self):
        nuevo, _, _ = construir_esqueleto_pure(solo_lunes())
        viejo = nuevo.copy()
        viejo.at[IDX["Bloque 1"], "Lun"] = "S1"   # asignatura pintada
        viejo.at[IDX["Bloque 2"], "Lun"] = "N"    # compromiso fijo
        viejo.at[IDX["Bloque 3"], "Lun"] = "P"    # preparación (no se repone)
        res = fusionar_pintura(nuevo, viejo)
        assert res.at[IDX["Bloque 1"], "Lun"] == "S1"
        assert res.at[IDX["Bloque 2"], "Lun"] == "N"
        assert res.at[IDX["Bloque 3"], "Lun"] == "D"

    def test_no_pisa_celdas_que_dejaron_de_ser_trabajo(self):
        # El nuevo esqueleto termina a las 13:00: lo pintado en la tarde se pierde.
        nuevo, _, _ = construir_esqueleto_pure(
            solo_lunes(colacion=False, ingreso="08:00", salida="13:00"))
        viejo, _, _ = construir_esqueleto_pure(solo_lunes())
        viejo.at[IDX["Bloque 7"], "Lun"] = "L"
        res = fusionar_pintura(nuevo, viejo)
        assert res.at[IDX["Bloque 7"], "Lun"] == ""


class TestAplicarEtiqueta:
    def test_agrega_y_quita(self):
        labels = aplicar_etiqueta({}, 1, "Lun", "Matemáticas 8°A")
        assert labels == {"1_Lun": "Matemáticas 8°A"}
        labels = aplicar_etiqueta(labels, 1, "Lun", "")
        assert labels == {}

    def test_no_muta_el_original(self):
        original = {"0_Lun": "x"}
        aplicar_etiqueta(original, 1, "Mar", "y")
        assert original == {"0_Lun": "x"}


# -----------------------------------------------------------------------------
# Motor de optimización
# -----------------------------------------------------------------------------

def esqueleto_manana():
    """Lunes 08:00-13:00 sin colación: 6 bloques de 45' + 2 recreos de 15'."""
    return construir_esqueleto_pure(
        solo_lunes(colacion=False, ingreso="08:00", salida="13:00"))


class TestOptimizar:
    def test_rellena_preparacion_hasta_el_contrato(self):
        df, dur, _ = esqueleto_manana()
        df.at[IDX["Bloque 1"], "Lun"] = "L"
        df.at[IDX["Bloque 2"], "Lun"] = "S1"  # asignatura = lectiva
        res, r = optimizar(df, dur, horas_contrato=5)  # 300 min = jornada exacta
        assert r["status"] == "ok"
        assert r["lectiva"] == 90        # L + S1
        assert r["recreo"] == 30
        assert r["prep"] == 180          # los 4 bloques D restantes
        assert r["total"] == 300
        assert r["deficit"] == 0
        # Las celdas elegidas quedan marcadas como P en el resultado.
        assert (res["Lun"] == "P").sum() == 4

    def test_no_excede_el_contrato(self):
        df, dur, _ = esqueleto_manana()
        df.at[IDX["Bloque 1"], "Lun"] = "L"
        # Contrato de 3h (180 min): fijo = 45 + 30 = 75; cabe a lo más 105 de prep.
        _, r = optimizar(df, dur, horas_contrato=3)
        assert r["status"] == "ok"
        assert r["total"] <= 180
        assert r["prep"] <= 105

    def test_sobrecarga(self):
        df, dur, _ = esqueleto_manana()
        df.at[IDX["Bloque 1"], "Lun"] = "L"
        df.at[IDX["Bloque 2"], "Lun"] = "L"
        _, r = optimizar(df, dur, horas_contrato=1)  # 60 min < 90 fijos
        assert r["status"] == "sobrecarga"
        assert r["prep"] == 0

    def test_sin_espacio(self):
        df, dur, _ = esqueleto_manana()
        for i in range(len(df)):
            if df.at[i, "Lun"] == "D":
                df.at[i, "Lun"] = "N"
        _, r = optimizar(df, dur, horas_contrato=6)  # 360 > 300 disponibles
        assert r["status"] == "sin_espacio"
        assert r["deficit"] == 60

    def test_jornada_supera_al_contrato_avisa_sobrante(self):
        df, dur, _ = esqueleto_manana()
        # Sin clases pintadas: fijo = 30 (recreos) y 6 bloques Disponibles (270').
        _, r = optimizar(df, dur, horas_contrato=3.5)  # 210 min
        # Caben 4 bloques de preparación (180'); sobran 2 bloques (90').
        assert r["status"] == "ok"
        assert r["prep"] == 180
        assert r["total"] == 210
        assert r["disponible"] == 270
        assert r["sobrante"] == 90
        assert r["sobrante_bloques"] == 2
        html = resumen_html(r)
        assert "sin asignar" in html
        assert "2 bloques Disponibles" in html

    def test_sin_sobrante_no_avisa(self):
        df, dur, _ = esqueleto_manana()
        df.at[IDX["Bloque 1"], "Lun"] = "L"
        df.at[IDX["Bloque 2"], "Lun"] = "S1"
        # Contrato 5h = capacidad exacta de la jornada: no sobra nada.
        _, r = optimizar(df, dur, horas_contrato=5)
        assert r["sobrante"] == 0
        assert r["sobrante_bloques"] == 0
        assert "sin asignar" not in resumen_html(r)


# -----------------------------------------------------------------------------
# PDF (prueba de humo)
# -----------------------------------------------------------------------------

def test_generar_pdf_produce_un_pdf():
    df, dur, _ = esqueleto_manana()
    df.at[IDX["Bloque 1"], "Lun"] = "S1"
    res, resumen = optimizar(df, dur, horas_contrato=5)
    mapa = mapa_render([{"id": "S1", "nombre": "Matemáticas 8°A",
                         "color": "#3b6fb6", "horas": 6}])
    labels = {f"{IDX['Bloque 2']}_Lun": "Reunión de apoderados"}
    pdf = generar_pdf(res, labels, resumen, mapa)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


# -----------------------------------------------------------------------------
# Segundo Ciclo (ciclo="2")
# -----------------------------------------------------------------------------

# Índice de fila por nombre de bloque para el segundo ciclo.
IDX2 = {nombre: i for i, (_, _, nombre, _) in enumerate(filas_catalogo(ciclo="2"))}


def dia_cfg_c2(**kw):
    """Config de un día de trabajo estándar del segundo ciclo."""
    base = {"trabaja": True, "colacion": True, "ingreso": "08:10",
            "salida_alm": "13:15", "regreso": "14:00", "salida": "18:30"}
    base.update(kw)
    return base


def solo_lunes_c2(**kw):
    horarios = {d: {"trabaja": False} for d in DIAS}
    horarios["Lun"] = dia_cfg_c2(**kw)
    return horarios


class TestSegundoCiclo:
    """Verifica que la lógica funcione correctamente con ciclo='2'."""

    def test_catalogo_segundo_ciclo_tiene_mismos_bloques(self):
        """Ambos ciclos tienen los mismos nombres de bloque (misma cantidad)."""
        filas1 = filas_catalogo(ciclo="1")
        filas2 = filas_catalogo(ciclo="2")
        assert len(filas1) == len(filas2)
        nombres1 = [f[2] for f in filas1]
        nombres2 = [f[2] for f in filas2]
        assert nombres1 == nombres2

    def test_tarde_identica(self):
        """Los bloques de la tarde (14:00 en adelante) son iguales."""
        filas1 = filas_catalogo(ciclo="1")
        filas2 = filas_catalogo(ciclo="2")
        tarde1 = [(i, f) for i, f in enumerate(filas1) if time_to_mins(f[0]) >= 840]
        tarde2 = [(i, f) for i, f in enumerate(filas2) if time_to_mins(f[0]) >= 840]
        assert tarde1 == tarde2

    def test_esqueleto_segundo_ciclo(self):
        df, dur, errores = construir_esqueleto_pure(solo_lunes_c2(), ciclo="2")
        assert errores == []
        # Ingreso 08:10: el saludo (08:10-08:15) aporta 5 min.
        assert df.at[IDX2["Ingreso / Saludo"], "Lun"] == "D"
        assert dur.at[IDX2["Ingreso / Saludo"], "Lun"] == 5
        # Bloque 1 (08:15-09:00) completo.
        assert df.at[IDX2["Bloque 1"], "Lun"] == "D"
        assert dur.at[IDX2["Bloque 1"], "Lun"] == 45
        # Recreo 1 (09:45-10:00) = 15 min.
        assert df.at[IDX2["Recreo 1"], "Lun"] == "R"
        assert dur.at[IDX2["Recreo 1"], "Lun"] == 15
        # Colación 13:15-14:00 cae en Bloque 6b.
        assert df.at[IDX2["Bloque 6b"], "Lun"] == "C"
        assert dur.at[IDX2["Bloque 6b"], "Lun"] == 45
        # El Bloque 11 cubre hasta 18:30, igual que ciclo 1.
        assert df.at[IDX2["Bloque 11"], "Lun"] == "D"
        assert dur.at[IDX2["Bloque 11"], "Lun"] == 60

    def test_optimizador_segundo_ciclo(self):
        horarios = solo_lunes_c2(colacion=False, ingreso="08:15", salida="13:15")
        df, dur, _ = construir_esqueleto_pure(horarios, ciclo="2")
        df.at[IDX2["Bloque 1"], "Lun"] = "L"
        res, r = optimizar(df, dur, horas_contrato=5)
        assert r["status"] == "ok"
        assert r["lectiva"] == 45
        assert r["total"] <= 300
