# -*- coding: utf-8 -*-
"""Conectores de API para redes sociales y analytics."""
from .ga4 import is_ga4_configured, fetch_ga4_data
from .meta import is_meta_configured, fetch_meta_facebook_data, fetch_meta_instagram_data
from .tiktok import is_tiktok_configured, fetch_tiktok_data

__all__ = [
    "is_ga4_configured", "fetch_ga4_data",
    "is_meta_configured", "fetch_meta_facebook_data", "fetch_meta_instagram_data",
    "is_tiktok_configured", "fetch_tiktok_data",
]
