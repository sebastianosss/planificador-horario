# Planificador de horario docente

Aplicación web para que un(a) docente (o quien arma horarios en un colegio) organice su semana
de clases respetando las reglas del **Estatuto Docente chileno (Ley 20.903)**: la docencia de
aula (lectiva) puede ser a lo más el **65%** de las horas de contrato y el resto debe ser no
lectiva. Los recreos no pertenecen a ninguna de las dos categorías, pero sí cuentan como tiempo
de permanencia en el establecimiento.

- **App publicada:** https://019ef096-ad57-81a6-77a6-6bd15b8812d5.share.connect.posit.cloud/
- **Stack:** Shiny for Python · pandas · PuLP (solver CBC) · ReportLab
- **Deploy:** Posit Connect Cloud (vía `rsconnect` / `manifest.json`, Python 3.11)

---

## 1. Features

| Feature | Descripción |
|---|---|
| Asistente de horario por contrato | Por cada día (Lun–Vie): trabaja o no, hora de ingreso, colación (salida/regreso) y salida, en campos de texto `HH:MM` que aceptan horas arbitrarias (ej. `14:15`). Botón "Copiar a los demás días" para semanas repetitivas. |
| Minutos efectivos por bloque | Si el ingreso o la salida cae a mitad de un bloque, ese bloque aporta solo los minutos que realmente caen dentro de la jornada (no el bloque completo). |
| Catálogo de asignaturas | Cada asignatura tiene nombre, bloques/semana objetivo y color. Se identifican internamente como `S1`, `S2`, … y se tratan como tiempo lectivo. |
| Grilla pintable por clic | Paleta de "pinceles" (asignaturas + categorías base). Se hace clic en la celda para pintarla; se puede repintar cuantas veces se quiera. |
| Validación en vivo por asignatura | Panel "Avance por asignatura": bloques pintados vs. objetivo (`X/Y bloques`, minutos acumulados, faltan/sobran/completo). |
| Pintura persistente al re-aplicar | Corregir el horario de un día y volver a "Aplicar horario" **no borra** lo ya pintado en el resto de la semana (`fusionar_pintura`). |
| Optimización de preparación | Con un clic, PuLP rellena los bloques `Disponible` con `Preparación` (no lectiva) hasta completar las horas de contrato, prefiriendo bloques "interiores" del día (evita huecos en los extremos). No mueve ni coloca asignaturas: eso es decisión manual del usuario. |
| Resumen y alertas | Tarjetas con contrato, lectiva (% del contrato), no lectiva, preparación, recreo y total. Avisos por sobrecarga, falta de bloques disponibles, déficit contra el contrato, superación del 65% lectivo y **exceso de jornada** (bloques Disponibles que quedan sin asignar porque la jornada supera las horas de contrato, con el detalle de cuántos bloques y horas sobran). |
| Tabla de suma de horas | Desglose por día y categoría (lectiva, no lectiva, preparación, recreo, total) + lista de los **bloques nuevos de preparación** que agregó la optimización: en qué día y bloque quedaron y cuántos minutos aporta cada uno, con total verificable contra el resumen. Va **plegada en un desplegable** para no alargar la página; se expande con un clic. |
| Etiquetado del horario final | Cada bloque del resultado tiene un campo de texto para el detalle ("Matemáticas 8°A", "Reunión de apoderados"); se guarda al salir del casillero. |
| Distintivo de bloques parciales | En el resultado, los bloques que aportan menos minutos que su duración nominal muestran un badge naranjo `Xm` y un tooltip "aporta X min". |
| Exportación a PDF | PDF imprimible (A4 apaisado) con la grilla coloreada, etiquetas y resumen de horas. |
| Reiniciar | Botón que limpia grilla, duraciones, etiquetas, resultado y el catálogo de asignaturas. |

---

## 2. Cómo está construida

Toda la app vive en **un solo archivo**, [`app.py`](app.py) (~1.240 líneas), organizado en 11
secciones numeradas con comentarios. Separa con bastante disciplina la **lógica pura**
(funciones testeables sin Shiny) de la **capa reactiva** (server).

```
app.py
├── 1  Constantes y catálogo      DIAS, CATEGORIAS (códigos L/N/P/R/C/D/""),
│                                  colores de asignaturas, filas_catalogo() = campanada fija
├── 2  Utilidades de tiempo       time_to_mins, fmt_horas, minutos_celda (fuente única
│                                  de los minutos efectivos de una celda)
├── 3  Asistente de contrato      construir_esqueleto_pure(): horarios por día → grilla
│                                  de códigos + grilla de minutos efectivos + errores
├── 4  Pintar / etiquetar         aplicar_pintura, fusionar_pintura, aplicar_etiqueta
│                                  (funciones puras sobre DataFrames/dicts)
├── 5  Motor de optimización      optimizar(): suma fijo (L+N+R), y un LP binario (PuLP/CBC)
│                                  elige qué celdas D pasar a P sin exceder el contrato
├── 6  JavaScript compartido      setPen / paintCell / saveLabel → Shiny.setInputValue
├── 7  Grilla pintable (HTML)     grilla_pintable_html(): tabla con onclick por celda
├── 8  Grilla de resultado (HTML) grilla_resultado_html(): inputs de etiqueta + badges
├── 8b Tabla de suma de horas     desglose_horas(), bloques_preparacion(),
│                                  tabla_horas_html(): verificación de la suma
├── 9  PDF                        generar_pdf() con ReportLab (tabla coloreada + resumen)
├── 10 UI                         page_sidebar + 4 cards (los 4 pasos del flujo)
└── 11 Server                     estado reactivo y manejadores de eventos
```

### Modelo de datos (estado reactivo en `server`)

| Estado | Tipo | Contenido |
|---|---|---|
| `grid_state` | `DataFrame` | Columnas `Inicio/Fin/Bloque` + una por día con el **código** de cada celda: `L`, `N`, `P`, `R`, `C`, `D`, `""` (fuera de jornada) o `S#` (asignatura). |
| `dur_state` | `DataFrame` | Mismo esqueleto, pero con los **minutos efectivos** de cada celda según el contrato aplicado. |
| `labels_rv` | `dict` | Etiquetas del horario final, clave `"{fila}_{día}"`. |
| `asig_state` | `list[dict]` | Asignaturas: `{id: "S#", nombre, color, horas}`. |
| `resultado_rv` | tupla o `None` | `(df_resultado, resumen)` de la última optimización. |
| `grid_version` | `int` | Contador que fuerza el re-render de la grilla pintable. |

### Decisiones de diseño relevantes

- **La grilla no se re-renderiza en cada clic.** El JS pinta la celda al instante en el
  navegador y avisa al servidor con `Shiny.setInputValue('cell_paint', …)`; el servidor solo
  actualiza `grid_state`. El re-render completo ocurre únicamente al aplicar horario, al
  cambiar asignaturas o al reiniciar (vía `grid_version`). Esto hace el pintado fluido.
- **`minutos_celda()` es la fuente única de verdad** de cuántos minutos vale una celda; la
  usan el optimizador, la validación por asignatura y los tooltips. Evita descuadres.
- **El optimizador solo asigna Preparación.** Por decisión de producto, la colocación de
  asignaturas es manual (el usuario pinta); el LP maximiza minutos de preparación con un
  desempate que prefiere bloques entre clases ya fijadas (`es_interior`).
- **La campanada es fija** (`filas_catalogo()`): jornada 07:55–19:00, bloques de 45', recreos
  de 15', un bloque 6b de 60' (13:00–14:00) y jornada de tarde 14:00–19:00.
- El HTML de grillas, paleta, leyenda y resumen se genera **a mano como strings** con estilos
  inline (no se usan componentes de Shiny para esas partes).

### Archivos del proyecto

| Archivo | Rol |
|---|---|
| `app.py` | Toda la aplicación (UI + server + lógica). |
| `requirements.txt` | Dependencias directas de runtime, pineadas (shiny, pandas, PuLP, reportlab). |
| `requirements-dev.txt` | Herramientas de desarrollo (pytest, rsconnect); no se despliegan. |
| `tests/` | Suite de pytest: unitarios de la lógica pura (`test_logic.py`) y simulaciones de casos completos que verifican el conteo de horas (`test_simulaciones.py`). |
| `manifest.json` | Manifiesto del deploy en Posit Connect Cloud (Python 3.11.9, entrypoint `app`). |
| `.venv/` | Entorno virtual local (no se despliega). |

### Correr en local

```bash
.venv/Scripts/python.exe -m shiny run --reload --port 8765 app.py
# abre http://127.0.0.1:8765
```

### Correr los tests

```bash
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt   # una vez
.venv/Scripts/python.exe -m pytest
```

---

## 3. Cómo funciona cada sección (y para qué la usa el usuario)

### Barra lateral — Configuración
Define las **horas de contrato semanales** (1–44, por defecto 36), que son el objetivo que la
optimización intenta completar. También está el botón **Reiniciar todo** y la nota legal sobre
recreos. El usuario la toca una vez al inicio y rara vez después.

### Paso 1 · Tu horario de contrato
Formulario por día: checkbox "Trabaja este día", checkbox "Tiene colación" y horas en texto
`HH:MM`. Al pulsar **Aplicar horario**, `construir_esqueleto_pure()` valida el orden de las
horas (ingreso < salida a colación < regreso < salida) y construye el esqueleto: recreos → `R`,
colación → `C`, resto de la jornada → `D` (disponible), fuera de jornada → vacío. Los días con
error se reportan en un panel rojo y quedan en blanco, sin bloquear los demás días.

**Uso típico:** configurar el lunes, "Copiar a los demás días" y ajustar las excepciones (ej.
viernes con salida 14:15). Se puede corregir un solo día y re-aplicar sin perder lo pintado.

### Paso 2 · Asignaturas del profesor
CRUD mínimo de asignaturas: nombre, bloques/semana objetivo y color. Cada asignatura agregada
aparece como chip de color y como pincel nuevo en la paleta del paso 3. Al **quitar** una
asignatura, todas sus celdas pintadas vuelven automáticamente a `Disponible`.

**Uso típico:** registrar la carga real del docente ("Matemáticas 8°A · 6 bloques",
"Ciencias 7°B · 4 bloques") antes de ponerse a pintar.

### Paso 3 · Marca tus bloques
La grilla llega pre-marcada con jornada, colación y recreos. El usuario elige un pincel
(asignatura, `Lectiva` genérica, `No lectiva` para compromisos fijos, etc.) y hace clic sobre
los bloques. Debajo, el panel **Avance por asignatura** se actualiza en vivo: `4/6 bloques ·
3h 00m · faltan 2`. Cuando la semana está pintada, pulsa **Optimizar horario**.

**Uso típico:** distribuir manualmente las clases donde el docente quiere/puede tenerlas, dejar
en `Disponible` lo que la app deba completar como preparación.

### Paso 4 · Horario optimizado — agrega el detalle
Muestra el resultado: la grilla pintada + los bloques `P` (Preparación) que agregó el
optimizador. Cada celda tiene un input para escribir el detalle, que se guarda al salir del
casillero y aparece en el PDF. Los tooltips indican cuántos minutos aporta cada bloque; los
parciales llevan badge naranjo. Abajo, las tarjetas de resumen y las alertas (sobrecarga,
déficit, >65% lectiva, exceso de jornada sobre el contrato), seguidas de la
**tabla de suma de horas** en un desplegable plegado por defecto ("Detalle de la suma de
horas y bloques nuevos"): al expandirlo muestra un desglose por día y categoría cuyo total
cuadra con las tarjetas, y la lista de los bloques nuevos de preparación (día, bloque,
horario y minutos aportados). Finalmente, **Descargar PDF** genera el horario imprimible.

**Uso típico:** revisar que los porcentajes cuadren con el contrato, poner nombres de cursos y
actividades, e imprimir/compartir el PDF.

---

## 4. Limitaciones conocidas

### De producto / UX
- **Sin persistencia.** Todo el estado vive en la sesión del navegador: al recargar la página
  o si el servidor de Posit recicla la sesión, se pierde todo el trabajo. No hay
  guardar/cargar (ni archivo, ni URL, ni base de datos). Es la limitación más importante para
  un usuario real.
- **Campanada fija.** Los bloques y recreos (07:55–19:00) están escritos en el código
  (`filas_catalogo()`). Un colegio con otra estructura de bloques no puede adaptarla desde la
  UI; solo puede aproximarla con las horas de ingreso/salida.
- **Un docente, una semana tipo.** No hay semanas alternadas (A/B), ni múltiples docentes, ni
  detección de choques de sala/curso entre profesores.
- **El optimizador solo rellena Preparación.** No coloca asignaturas ni aplica condiciones
  pedagógicas (máximo de bloques por día de una asignatura, no dos bloques seguidos,
  disponibilidad/bloqueos por día). Esto está planificado como Parte 2.
- **El 65% lectivo es solo una alerta**, no una restricción dura: la app deja guardar y
  exportar un horario que supera el máximo legal (con aviso).
- **Se puede pintar fuera de la jornada.** Las celdas vacías (fuera del horario de contrato)
  también aceptan pintura; cuentan como bloque en el avance por asignatura pero aportan
  0 minutos, lo que puede confundir (el conteo de bloques cuadra pero las horas no).
- **Sin deshacer (undo)** ni pintado por arrastre: todo es clic a clic.
- **Etiquetas ancladas a la posición**, no al contenido: se guardan por `fila_día`, así que si
  después se repinta esa celda con otra cosa, la etiqueta antigua reaparece sobre el nuevo
  contenido.
- **Accesibilidad limitada:** la distinción es principalmente por color (sin patrón/textura),
  celdas pequeñas, sin manejo por teclado. En el PDF, colores oscuros con texto blanco pueden
  imprimirse mal en B/N.

### Técnicas / de mantenimiento
- **Monolito de un archivo.** UI, lógica, HTML/JS embebido y PDF en un solo `app.py` de ~1.240
  líneas. Funciona, pero dificulta hacer crecer la app (candidato natural a separar
  en módulos: `core/` puro, `ui/`, `pdf.py`).
- **Cobertura de tests parcial.** `tests/test_logic.py` cubre la lógica pura (esqueleto,
  minutos efectivos, pintura, optimizador, PDF), pero la capa reactiva de Shiny y el JS de la
  grilla no tienen pruebas automatizadas.
- **HTML/JS como strings con estilos inline.** Frágil ante cambios (fácil romper el escape o
  el layout), difícil de tematizar; no hay CSS separado ni componentes reutilizables.
- **PuLP avisa deprecaciones.** Con PuLP 3.3, `LpVariable(...)` y `PULP_CBC_CMD` están
  deprecados y se eliminan en PuLP 4.0; habrá que migrar a `prob.add_variable(...)` y
  `COIN_CMD` antes de subir de versión.
- **Validación de horas solo al aplicar.** Los campos `HH:MM` no validan mientras se escribe;
  un formato inválido recién se detecta al pulsar "Aplicar horario".
- **Descarga de PDF sin resultado:** si no se ha optimizado, el botón genera un PDF de una
  grilla vacía en vez de avisar.
- **Dependencia del solver CBC** (binario que trae PuLP): en algunos entornos de deploy puede
  no estar disponible o tardar; hoy no hay manejo de error si `prob.solve()` falla.
- **Posit Connect Cloud (plan gratuito):** la app se suspende por inactividad (arranque lento
  la primera vez) y tiene límites de horas/instancias; cada visita nueva parte de cero por la
  falta de persistencia.

---

## 5. Roadmap corto (acordado)

- **Parte 2 (pendiente):** condiciones de pintado/validación — disponibilidad y bloqueos por
  día, máximo de bloques por día por asignatura, evitar bloques consecutivos, detección de
  choques — y una opción de "autocompletar" opcional con el optimizador (siempre manual
  primero).
- **Buenas prácticas (en curso):** tests de la lógica pura ✔, `requirements.txt` saneado ✔,
  fix de "Reiniciar todo" ✔. Siguen: modularizar `app.py` y persistencia (exportar/importar
  la configuración como JSON sería el paso más simple).

> Nota: la app es una herramienta de planificación; no constituye asesoría legal ni laboral.
