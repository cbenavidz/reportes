# -*- coding: utf-8 -*-
"""
Conector Google Analytics 4 — usa la Google Analytics Data API.

Setup (30 min):
  1. https://console.cloud.google.com → crear proyecto.
  2. Habilitar API: "Google Analytics Data API".
  3. Service Account: IAM → Service Accounts → Create.
  4. Descargar key JSON.
  5. En GA4 (analytics.google.com) → Admin → Acceso a propiedad → agregar
     el email del service account como "Lector".
  6. Anotar el property_id (Admin → Detalles de propiedad).

Credenciales en st.secrets:
    [ga4]
    property_id = "123456789"
    service_account_json = '''{
      "type": "service_account",
      ...
    }'''
"""
from __future__ import annotations

import json
import logging
from datetime import date

import pandas as pd

from ..secrets_loader import get_secret_dict

logger = logging.getLogger(__name__)


def is_ga4_configured() -> bool:
    """True si hay credenciales válidas en st.secrets."""
    cfg = get_secret_dict("ga4")
    if not cfg:
        return False
    return bool(cfg.get("property_id") and cfg.get("service_account_json"))


def fetch_ga4_data(
    date_from: date,
    date_to: date,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
) -> pd.DataFrame:
    """
    Trae datos de GA4 entre `date_from` y `date_to`.

    Métricas por defecto:
      - sessions
      - activeUsers
      - screenPageViews
      - bounceRate
      - conversions
      - totalRevenue

    Dimensiones por defecto:
      - date
    """
    cfg = get_secret_dict("ga4")
    if not cfg:
        raise RuntimeError(
            "Google Analytics 4 no está configurado. Agrega [ga4] en secrets."
        )

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            DateRange, Dimension, Metric, RunReportRequest,
        )
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise RuntimeError(
            "Falta instalar `google-analytics-data`. Agrégalo a requirements.txt."
        ) from exc

    sa_json = cfg["service_account_json"]
    if isinstance(sa_json, str):
        sa_info = json.loads(sa_json)
    else:
        sa_info = dict(sa_json)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    client = BetaAnalyticsDataClient(credentials=creds)

    metrics = metrics or [
        "sessions", "activeUsers", "screenPageViews",
        "bounceRate", "conversions", "totalRevenue",
    ]
    dimensions = dimensions or ["date"]

    req = RunReportRequest(
        property=f"properties/{cfg['property_id']}",
        date_ranges=[DateRange(start_date=str(date_from), end_date=str(date_to))],
        metrics=[Metric(name=m) for m in metrics],
        dimensions=[Dimension(name=d) for d in dimensions],
    )
    resp = client.run_report(req)

    rows = []
    for r in resp.rows:
        row = {}
        for i, dim in enumerate(dimensions):
            row[dim] = r.dimension_values[i].value
        for i, m in enumerate(metrics):
            try:
                row[m] = float(r.metric_values[i].value)
            except (ValueError, TypeError):
                row[m] = 0.0
        rows.append(row)

    df = pd.DataFrame(rows)
    if "date" in df.columns:
        # GA4 devuelve date como YYYYMMDD
        df["fecha"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        df = df.drop(columns=["date"])
    # Renombrar a esquema común
    df = df.rename(columns={
        "sessions": "sesiones",
        "activeUsers": "usuarios",
        "screenPageViews": "paginas_vistas",
        "bounceRate": "tasa_rebote",
        "conversions": "conversiones",
        "totalRevenue": "ingresos",
    })
    return df.sort_values("fecha").reset_index(drop=True) if "fecha" in df.columns else df
