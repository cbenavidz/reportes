#!/bin/bash
# ==========================================================
# Script de instalación automática (versión robusta)
# Cartera Inteligente - Casa de los Mineros
# ==========================================================
set -e

echo ""
echo "============================================================"
echo "  Cartera Inteligente - Casa de los Mineros"
echo "  Instalador automático"
echo "============================================================"
echo ""

# Verificar Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 no está instalado."
    echo "   Instala Python desde https://www.python.org/downloads/"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PY_VERSION detectado"

# Borrar venv si existe (para evitar pip corrupto)
if [ -d "venv" ]; then
    echo "→ Eliminando entorno virtual anterior..."
    rm -rf venv
fi

# Crear venv nuevo
echo "→ Creando entorno virtual limpio..."
python3 -m venv venv

# Activar venv
echo "→ Activando entorno virtual..."
source venv/bin/activate

# Actualizar pip PRIMERO (crítico para evitar errores)
echo "→ Actualizando pip y herramientas base..."
python3 -m pip install --upgrade --quiet pip setuptools wheel

# Instalar dependencias
echo "→ Instalando dependencias (puede tomar 2-3 min la primera vez)..."
python3 -m pip install --quiet -r requirements.txt

# Verificar que streamlit quedó instalado
if ! python3 -m pip show streamlit > /dev/null 2>&1; then
    echo ""
    echo "❌ Algo falló: streamlit no se instaló."
    echo "   Por favor mándame el error completo."
    exit 1
fi

echo ""
echo "✅ Instalación completa y verificada."
echo ""
echo "Próximos pasos:"
echo ""
echo "  1) Probar conexión a Odoo:"
echo "       python3 test_conexion.py"
echo ""
echo "  2) Lanzar la app:"
echo "       python3 -m streamlit run app.py"
echo ""
echo "  3) Abrir en el navegador: http://localhost:8501"
echo ""
echo "  Login inicial:"
echo "       Usuario: carlos"
echo "       Pass:    cartera2026"
echo ""
echo "============================================================"
