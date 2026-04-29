# -*- coding: utf-8 -*-
"""Página: configuración y diagnóstico de conexión (solo admin)."""
import os

import streamlit as st

from src.auth import logout_button, require_role
from src.data_loader import test_connection_summary

st.set_page_config(page_title="Configuración | Cartera", page_icon="⚙️", layout="wide")

require_role("admin")
logout_button()

st.title("⚙️ Configuración y diagnóstico")

st.subheader("🔌 Estado de conexión a Odoo")
with st.spinner("Probando conexión..."):
    summary = test_connection_summary()

if summary.get("status") == "ok":
    st.success("✅ Conexión exitosa")
    col1, col2, col3 = st.columns(3)
    col1.metric("Clientes", summary.get("partners_count", "—"))
    col2.metric("Facturas posted", summary.get("invoices_count", "—"))
    col3.metric("UID", summary.get("uid", "—"))
else:
    st.error(f"❌ Error de conexión: {summary.get('error', 'desconocido')}")

st.markdown("---")

st.subheader("Variables de entorno (solo lectura)")
env_vars = {
    "ODOO_URL": os.getenv("ODOO_URL", ""),
    "ODOO_DB": os.getenv("ODOO_DB", ""),
    "ODOO_USERNAME": os.getenv("ODOO_USERNAME", ""),
    "ODOO_API_KEY": (os.getenv("ODOO_API_KEY", "") or "")[:8] + "...",
    "CACHE_TTL_MINUTES": os.getenv("CACHE_TTL_MINUTES", "15"),
}
st.json(env_vars)

st.markdown("---")

st.subheader("🔄 Limpiar caché")
if st.button("Limpiar caché de datos (forzar nueva descarga)"):
    st.cache_data.clear()
    st.cache_resource.clear()
    st.success("Caché limpiado. Vuelve al dashboard para recargar.")

st.markdown("---")

st.subheader("👥 Usuarios configurados")
from src.auth import load_users

users = load_users()
for username, info in users.items():
    st.markdown(
        f"- **{username}** · {info.get('name', '—')} · `{info.get('role', 'cartera')}` · {info.get('email', '')}"
    )

st.caption(
    "Para agregar usuarios o cambiar contraseñas, edita `config/auth_config.yaml` y reinicia la app. "
    "El password_hash se genera con: `python3 -c \"import hashlib; print(hashlib.sha256('TUPASS'.encode()).hexdigest())\"`"
)
