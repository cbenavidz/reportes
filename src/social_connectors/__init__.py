# -*- coding: utf-8 -*-
"""Conectores de API para redes sociales y analytics."""
from .ga4 import is_ga4_configured, fetch_ga4_data
from .meta import (
    diagnose_meta_connection,
    fetch_meta_ads_insights,
    fetch_meta_facebook_data,
    fetch_meta_facebook_monthly_evolution,
    fetch_meta_facebook_top_posts,
    fetch_meta_instagram_data,
    fetch_meta_instagram_monthly_evolution,
    fetch_meta_instagram_top_posts,
    is_meta_configured,
)
from .tiktok import (
    is_tiktok_configured,
    fetch_tiktok_data,
    fetch_tiktok_account_stats,
)

__all__ = [
    "is_ga4_configured", "fetch_ga4_data",
    "is_meta_configured",
    "diagnose_meta_connection",
    "fetch_meta_ads_insights",
    "fetch_meta_facebook_data",
    "fetch_meta_facebook_monthly_evolution",
    "fetch_meta_facebook_top_posts",
    "fetch_meta_instagram_data",
    "fetch_meta_instagram_monthly_evolution",
    "fetch_meta_instagram_top_posts",
    "is_tiktok_configured", "fetch_tiktok_data",
    "fetch_tiktok_account_stats",
]
