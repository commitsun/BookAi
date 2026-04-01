import logging

# Resuelve el logging.
# Se usa en el flujo de configuración auxiliar de logging para preparar datos, validaciones o decisiones previas.
# Recibe `level` como entrada principal según la firma.
# No devuelve un valor relevante; deja preparado el estado o ejecuta la acción necesaria. Sin efectos secundarios relevantes.
def configure_logging(level=logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
