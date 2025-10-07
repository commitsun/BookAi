#!/bin/bash

LOGS_DIR="/home/rafaelpg/Proyectos/hotel_ai/logs"

echo "🧹 Limpiando logs en: $LOGS_DIR"

# Usar sudo para asegurar permisos
sudo rm -rf "$LOGS_DIR"/*

echo "✅ Logs eliminados correctamente."
