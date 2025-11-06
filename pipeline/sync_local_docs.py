import os

from .upload_doc import upload_doc          # 👈 import relativo correcto
from . import run_pipeline                  # para llamar al pipeline tras sincronizar


def main():
    # Carpeta local del repo donde pondrás los documentos a sincronizar
    docs_dir = os.path.join(os.getcwd(), "docs")

    if not os.path.isdir(docs_dir):
        print("⚠️ No se encontró carpeta docs/ en el repo.")
        return

    print("📤 Sincronizando documentos de docs/ → bucket...\n")
    updated = []

    for fname in os.listdir(docs_dir):
        if fname.lower().endswith((".pdf", ".docx", ".txt")):
            path = os.path.join(docs_dir, fname)
            if os.path.isfile(path):
                upload_doc(path, remote_name=fname)   # sube (con reemplazo) y verifica hash
                updated.append(fname)

    print("\n📑 Archivos sincronizados:", ", ".join(updated) if updated else "ninguno")

    # Lanza el pipeline de vectorización. Si pasamos la lista, solo re-vectoriza esos ficheros.
    if updated:
    # Convertimos a rutas completas dentro de docs/
        full_paths = [os.path.join(docs_dir, f) for f in updated]
        run_pipeline.main(files=full_paths)
    else:
        run_pipeline.main()


if __name__ == "__main__":
    main()
