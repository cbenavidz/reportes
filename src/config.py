# -*- coding: utf-8 -*-
"""
Configuración global del análisis.

Aquí vive el "piso" de fecha desde el cual los datos son confiables. Antes de
esa fecha (cargue inicial del sistema), pueden existir saldos iniciales y
facturas parciales que distorsionan rotación, DSO y hábito de pago. Toda la
app respeta este piso al calcular.

Para cambiarlo sin tocar código, basta con definir la variable de entorno
`DATA_FLOOR_DATE` (formato `YYYY-MM-DD`) en el `.env`.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional


# Fecha por defecto: 1 de septiembre de 2025 — Casa de los Mineros cargó el
# sistema en agosto de 2025; desde el 1 de septiembre las ventas se ingresan
# completas mes a mes.
_DEFAULT_DATA_FLOOR = date(2025, 9, 1)


def _parse_env_date(raw: str | None) -> Optional[date]:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_data_floor_date() -> date:
    """
    Devuelve la fecha mínima desde la cual se considera que los datos en
    Odoo son confiables y completos. Cualquier cálculo de rotación, DSO,
    hábito de pago e histórico mensual se ancla a esta fecha como piso.

    Configurable via env `DATA_FLOOR_DATE` (YYYY-MM-DD).
    """
    parsed = _parse_env_date(os.getenv("DATA_FLOOR_DATE"))
    return parsed or _DEFAULT_DATA_FLOOR


def clamp_date_from(d: date | None) -> date:
    """
    Sube `d` al piso de datos confiables si se queda por debajo.

    Útil en `extract_all_for_cartera` y en cualquier `period_start`.
    """
    floor = get_data_floor_date()
    if d is None:
        return floor
    return d if d >= floor else floor
