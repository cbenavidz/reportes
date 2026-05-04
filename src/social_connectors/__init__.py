# -*- coding: utf-8 -*-
"""Conectores de API para redes sociales y analytics."""
from .ga4 import is_ga4_configured, fetch_ga4_data
from .meta import (
    fetch_meta_facebook_data,
    fetch_meta_facebook_monthly_evolution,
    fetch_meta_facebook_top_posts,
    fetch_meta_instagram_data,
    fetch_meta_instagram_monthly_evolution,
    fetch_meta_instagram_top_posts,
    is_meta_configured,
)
from .tiktok import is_tiktok_configured, fetch_tiktok_data

__all__ = [
    "is_ga4_configured", "fetch_ga4_data",
    "is_meta_configured",
    "fetch_meta_facebook_data",
    "fetch_meta_facebook_monthly_evolution",
    "fetch_meta_facebook_top_posts",
    "fetch_meta_instagram_data",
    "fetch_meta_instagram_monthly_evolution",
    "fetch_meta_instagram_top_posts",
    "is_tiktok_configured", "fetch_tiktok_data",
]
