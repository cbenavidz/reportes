# -*- coding: utf-8 -*-
"""
Autenticación simple para Streamlit.

Mantiene credenciales hasheadas en config/auth_config.yaml.
Para producción puedes migrar a streamlit-authenticator con cookies firmadas.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import streamlit as st
import yaml

from .secrets_loader import get_secret_dict


CONFIG_PATH = Path(__file__).parent.parent / "config" / "auth_config.yaml"


def _hash(password: str) -> str:
    """Hash SHA-256 simple. (Para producción usar bcrypt vía streamlit-authenticator.)"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def load_users() -> dict:
    """
    Carga usuarios desde Streamlit secrets (en producción) o desde
    `config/auth_config.yaml` (en desarrollo local).

    El YAML local no se sube al repo (está en .gitignore). En Streamlit
    Cloud se configuran como `[auth.users.<usuario>]` en el panel de
    Secrets, con el mismo formato (name, password_hash, role, email).
    """
    auth_secrets = get_secret_dict("auth")
    if auth_secrets and "users" in auth_secrets:
        return dict(auth_secrets["users"])

    # Fallback a YAML local (desarrollo)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return config.get("users", {})

    return {}


def login_form() -> dict | None:
    """
    Renderiza formulario de login y retorna el usuario autenticado o None.

    Usa st.session_state para persistir login durante la sesión.
    """
    if st.session_state.get("auth_user"):
        return st.session_state["auth_user"]

    st.markdown("## 🔐 Iniciar sesión")
    st.caption("Cartera Inteligente – Casa de los Mineros")

    with st.form("login_form"):
        username = st.text_input("Usuario", placeholder="ej: carlos")
        password = st.text_input("Contraseña", type="password")
        submit = st.form_submit_button("Ingresar", use_container_width=True)

    if submit:
        users = load_users()
        if username in users and users[username].get("password_hash") == _hash(password):
            user_info = {
                "username": username,
                "name": users[username].get("name", username),
                "role": users[username].get("role", "cartera"),
                "email": users[username].get("email", ""),
            }
            st.session_state["auth_user"] = user_info
            st.success(f"Bienvenido, {user_info['name']} 👋")
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")
    return None


def logout_button() -> None:
    """Botón de cerrar sesión en el sidebar."""
    user = st.session_state.get("auth_user")
    if not user:
        return
    with st.sidebar:
        st.markdown(f"**👤 {user['name']}**  \n_{user['role']}_")
        if st.button("Cerrar sesión", use_container_width=True):
            st.session_state.pop("auth_user", None)
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()


def require_auth() -> dict:
    """Bloquea la página hasta que haya un usuario autenticado."""
    user = login_form()
    if not user:
        st.stop()
    return user


def require_role(*roles: str) -> dict:
    """Bloquea la página si el usuario no tiene uno de los roles permitidos."""
    user = require_auth()
    if user["role"] not in roles:
        st.error(f"⛔ Acceso restringido. Esta página requiere rol: {', '.join(roles)}.")
        st.stop()
    return user
