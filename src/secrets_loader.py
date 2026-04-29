# -*- coding: utf-8 -*-
"""
Lectura unificada de secretos: Streamlit Cloud (st.secrets) o .env local.

En producción (Streamlit Cloud) las credenciales se ponen en el panel de
"Secrets" de la app. Streamlit las expone vía `st.secrets`. En desarrollo
local, las mismas credenciales viven en un archivo `.env` y se leen con
`python-dotenv` + `os.getenv`.

Este módulo abstrae ambas fuentes para que el resto del código no se entere
de dónde vienen las credenciales.

Uso:
    from src.secrets_loader import get_secret
    api_key = get_secret("ODOO_API_KEY")
"""
from __future__ import annotations

import os
from typing import Any

# Intento cargar .env si python-dotenv está disponible. Si no, las env vars
# tienen que estar exportadas a mano.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass


def _streamlit_secrets() -> Any:
    """
    Devuelve `st.secrets` si Streamlit está corriendo y hay secretos
    configurados; None en cualquier otro caso (scripts CLI, tests, etc.)
    sin lanzar excepciones.
    """
    try:
        import streamlit as st
        # Acceso a st.secrets fuera de un script Streamlit lanza
        # FileNotFoundError. Lo absorbemos aquí.
        _ = st.secrets  # toca para forzar la carga
        return st.secrets
    except Exception:  # noqa: BLE001
        return None


def get_secret(key: str, default: str | None = None) -> str | None:
    """
    Lee `key` de Streamlit secrets (si existe) o de variables de entorno.

    Soporta dos formatos en `st.secrets`:
      1) Plano: `ODOO_URL = "https://..."` en el TOML.
      2) Anidado: `[odoo]` con `URL`, `DB`, etc. — no usamos por ahora.

    Si no encuentra la clave en ningún lado, devuelve `default`.
    """
    secrets = _streamlit_secrets()
    if secrets is not None:
        try:
            if key in secrets:
                return str(secrets[key])
        except Exception:  # noqa: BLE001 — defensa para entornos raros
            pass
    return os.getenv(key, default)


def get_secret_dict(section: str) -> dict | None:
    """
    Lee una sección anidada de st.secrets (útil para usuarios de auth).

    Ejemplo de TOML:
        [auth.users.carlos]
        name = "Carlos Benavides"
        password_hash = "..."

    `get_secret_dict("auth")` devolvería el dict completo de la sección
    "auth". Devuelve None si no existe o si no estamos en Streamlit.
    """
    secrets = _streamlit_secrets()
    if secrets is None:
        return None
    try:
        if section in secrets:
            return dict(secrets[section])
    except Exception:  # noqa: BLE001
        return None
    return None
