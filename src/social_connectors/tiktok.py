# -*- coding: utf-8 -*-
"""
Conector TikTok Business / Creator API.

Setup (1 hora + tiempo de App Review — 2-4 semanas):
  1. https://developers.tiktok.com → Crear app (Login Kit + Research API
     o TikTok for Business — depende del uso).
  2. Solicitar acceso al Research API o Display API.
  3. App Review (esperar aprobación).
  4. OAuth 2.0:
       - GET https://www.tiktok.com/v2/auth/authorize/?client_key=X&...
       - El usuario autoriza
       - Recibes code → cambias por access_token (válido ~24h, refresh válido ~1 año)
  5. Anotar `access_token` y `refresh_token`.

Credenciales en st.secrets:
    [tiktok]
    access_token = "act.xxx..."
    refresh_token = "rft.xxx..."
    business_account_id = "..."

NOTA: TikTok es la API más restrictiva de las 4. Si tu app no logra
aprobación, queda como fallback el upload de CSV manual.
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from ..secrets_loader import get_secret_dict

logger = logging.getLogger(__name__)

BASE_URL = "https://open.tiktokapis.com/v2"


def is_tiktok_configured() -> bool:
    cfg = get_secret_dict("tiktok")
    if not cfg:
        return False
    return bool(cfg.get("access_token"))


def fetch_tiktok_data(date_from: date, date_to: date) -> pd.DataFrame:
    """
    Trae stats de los últimos N días para el perfil autenticado.

    PENDIENTE: TikTok cambia frecuentemente sus endpoints. Esta función
    es un esqueleto que se completará cuando se obtenga acceso al API y
    se documente la respuesta exacta.
    """
    cfg = get_secret_dict("tiktok")
    if not cfg or not cfg.get("access_token"):
        raise RuntimeError("TikTok no está configurado.")

    raise NotImplementedError(
        "Conector TikTok pendiente. Obtén acceso a la API y se completa "
        "esta función con las llamadas reales. Mientras tanto usa el "
        "upload de CSV en la página."
    )
