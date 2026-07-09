# -*- coding: utf-8 -*-
"""
Lectores de los modelos del módulo de Odoo `sales_route_mobile`
(Ventas en Ruta - App Móvil):

  - sr.route    → ruteros (vendedor, días, clientes)
  - sr.visit    → visitas reales (check-in con GPS, efectividad, geocerca)
  - res.partner → campos sr_* de operación de ruta (rutero, secuencia,
                  frecuencia configurada, días, GPS, activo en ruta)

Todas las funciones degradan de forma segura: si el usuario de API no tiene
permiso de lectura o el modelo no existe, devuelven un DataFrame vacío y
registran una advertencia (no rompen las páginas).
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from .extractor import _unpack_m2o
from .odoo_client import OdooClient

logger = logging.getLogger(__name__)

DAY_COLS = ["day_mon", "day_tue", "day_wed", "day_thu", "day_fri",
            "day_sat", "day_sun"]
SR_DAY_COLS = ["sr_day_mon", "sr_day_tue", "sr_day_wed", "sr_day_thu",
               "sr_day_fri", "sr_day_sat", "sr_day_sun"]
DAY_LABELS = {
    "mon": "Lunes", "tue": "Martes", "wed": "Miércoles", "thu": "Jueves",
    "fri": "Viernes", "sat": "Sábado", "sun": "Domingo",
}


def _safe_search_read(client: OdooClient, model: str, domain, fields,
                      order=None) -> list[dict]:
    """search_read con manejo de errores (permisos / modelo inexistente)."""
    try:
        return client.search_read(model, domain=domain, fields=fields, order=order)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "No se pudo leer '%s' (¿permisos del usuario de API o módulo no "
            "instalado?): %s", model, exc,
        )
        return []


def route_module_disponible(client: OdooClient) -> bool:
    """True si el modelo sr.route es legible por el usuario actual."""
    try:
        client.search_read("sr.route", domain=[], fields=["id"], limit=1)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# sr.route
# ---------------------------------------------------------------------------
def extract_sr_routes(client: OdooClient) -> pd.DataFrame:
    """Ruteros (sr.route) con su vendedor y días de visita."""
    cols = ["id", "name", "active", "user_id"] + DAY_COLS + ["partner_count"]
    recs = _safe_search_read(
        client, "sr.route",
        domain=[("active", "=", True)],
        fields=cols, order="name asc",
    )
    if not recs:
        return pd.DataFrame(columns=["id", "name", "user_id", "user_name",
                                     "dias", *DAY_COLS])
    df = pd.DataFrame(recs)
    df[["user_id", "user_name"]] = df["user_id"].apply(
        lambda v: pd.Series(_unpack_m2o(v))
    )
    for c in DAY_COLS:
        if c not in df.columns:
            df[c] = False
    df["dias"] = df.apply(
        lambda r: ", ".join(
            DAY_LABELS[c[4:]] for c in DAY_COLS if r.get(c)
        ) or "—", axis=1,
    )
    return df


# ---------------------------------------------------------------------------
# res.partner (campos sr_*)
# ---------------------------------------------------------------------------
def extract_route_partners(
    client: OdooClient,
    company_ids: list[int] | tuple[int, ...] | None = None,
    solo_activos: bool = False,
) -> pd.DataFrame:
    """
    Clientes con su configuración de ruta (sr_*): rutero, secuencia, días,
    frecuencia configurada, GPS y estado en ruta.

    ⚠️ `company_ids`: úsalo con cuidado. En Odoo los contactos suelen ser
    COMPARTIDOS entre compañías (`company_id` vacío), así que filtrar por
    empresa deja fuera a la mayoría de los clientes. Para el universo de ruta
    usa `solo_activos=True` y NO pases `company_ids`.
    """
    fields = [
        "id", "name", "city", "state_id", "user_id",
        "partner_latitude", "partner_longitude",
        "sr_has_geo", "sr_geo_accuracy", "sr_geo_source",
        "sr_visit_frequency", "sr_active_in_route",
        "sr_route_id", "sr_route_sequence", "sr_use_own_days",
        *SR_DAY_COLS,
    ]
    domain = [("customer_rank", ">", 0)]
    if solo_activos:
        domain.append(("sr_active_in_route", "=", True))
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))
    recs = _safe_search_read(client, "res.partner", domain, fields, order="name asc")
    if not recs:
        return pd.DataFrame(columns=fields + ["user_name", "state_name",
                                              "sr_route_name"])
    df = pd.DataFrame(recs)
    for col in ["user_id", "state_id", "sr_route_id"]:
        if col in df.columns:
            name_col = {"user_id": "user_name", "state_id": "state_name",
                        "sr_route_id": "sr_route_name"}[col]
            df[[col, name_col]] = df[col].apply(
                lambda v: pd.Series(_unpack_m2o(v))
            )
    return df


# ---------------------------------------------------------------------------
# sr.visit
# ---------------------------------------------------------------------------
def extract_sr_visits(
    client: OdooClient,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
) -> pd.DataFrame:
    """
    Visitas reales (check-in) del período: vendedor, cliente, fecha/hora, GPS,
    distancia, si fue efectiva, geocerca y motivo.
    """
    fields = [
        "id", "partner_id", "user_id", "route_id", "check_in",
        "latitude", "longitude", "accuracy", "distance_m",
        "outside_geofence", "is_effective", "reason_id",
    ]
    domain = []
    if date_from:
        domain.append(("check_in", ">=", f"{date_from} 00:00:00"))
    if date_to:
        domain.append(("check_in", "<=", f"{date_to} 23:59:59"))
    recs = _safe_search_read(client, "sr.visit", domain, fields, order="check_in desc")
    if not recs:
        return pd.DataFrame(columns=fields + ["partner_name", "user_name",
                                              "route_name", "reason_name",
                                              "fecha"])
    df = pd.DataFrame(recs)
    for col in ["partner_id", "user_id", "route_id", "reason_id"]:
        if col in df.columns:
            name_col = {"partner_id": "partner_name", "user_id": "user_name",
                        "route_id": "route_name", "reason_id": "reason_name"}[col]
            df[[col, name_col]] = df[col].apply(
                lambda v: pd.Series(_unpack_m2o(v))
            )
    df["fecha"] = pd.to_datetime(df["check_in"], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Frecuencia configurada -> semanas del mes (mismo criterio que el rutero)
# ---------------------------------------------------------------------------
FREQ_LABEL = {
    "weekly": "Semanal", "biweekly": "Quincenal",
    "monthly": "Mensual", "on_demand": "Bajo demanda",
}
FREQ_SEMANAS = {
    "weekly": [1, 2, 3, 4], "biweekly": [1, 3],
    "monthly": [1], "on_demand": [1],
}


def frecuencia_config(valor) -> tuple[str, list[int]]:
    """Traduce sr_visit_frequency a (etiqueta, semanas del mes)."""
    v = str(valor) if valor else "weekly"
    return FREQ_LABEL.get(v, "Semanal"), FREQ_SEMANAS.get(v, [1, 2, 3, 4])


# NOTA: este módulo es de SOLO LECTURA por decisión del negocio. Las
# propuestas de rutero se entregan en Excel para cargarlas manualmente en
# Odoo; no se escribe en `res.partner` desde aquí.
