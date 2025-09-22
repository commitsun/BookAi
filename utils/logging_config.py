import logging

def silence_logs():
    """Silencia todos los logs molestos de librerías externas."""
    logging.getLogger().handlers.clear()
    logging.basicConfig(level=logging.CRITICAL, force=True)
    for lib in ["uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "fastmcp"]:
        logging.getLogger(lib).setLevel(logging.CRITICAL)
