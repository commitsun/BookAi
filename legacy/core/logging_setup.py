# core/logging_setup.py
import logging
import sys

# Configura logging global con formato consistente.
# Se usa en el flujo de configuración básica de logging para preparar datos, validaciones o decisiones previas.
# Recibe `level` como entrada principal según la firma.
# No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
def setup_logging(level=logging.INFO):
    """Configura logging global con formato consistente."""
    fmt = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
