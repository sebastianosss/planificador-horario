import sys
from pathlib import Path

# Permite importar app.py desde la raíz del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
