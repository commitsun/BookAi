#!/bin/bash
set -e
VENV=".venv"

echo "⚙️  Configurando entorno virtual..."
if [ ! -d "$VENV" ]; then
  python3 -m venv $VENV
fi
source $VENV/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

echo "✅ Entorno listo."
