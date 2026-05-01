# -*- coding: utf-8 -*-
"""
Análisis de Ventas en Ruta — versión simplificada.

No depende de `crm.team` (no está poblado en Odoo). Trabaja con los datos
disponibles:
  - `account.move.line` para ventas (subtotal sin IVA, excluye SOAT/ANTCL).
  - `account.move.invoice_user_id` para identificar al vendedor.
  - `res.partner.city`, `partner_latitude`, `partner_longitude` para
    análisis geográfico y mapa.

Funciones:
  - get_partners_for_sellers(partners, invoices, user_ids)
  - compute_monthly_clients_kpi(lines, dates)
  - compute_sales_by_city(lines, partners)
  - compute_visit_frequency(lines, partners)
  - build_geo_dataframe(partners, lines)
  - detect_inactive_clients(lines, partners, cutoff)
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from .sales_analyzer import _filter_lines_for_sales

logger = logging.getLogger(__name__)


def get_partners_for_sellers(
    partners: pd.DataFrame,
    invoices: pd.DataFrame | None,
    user_ids: Iterable[int],
) -> pd.DataFrame:
    """
    Devuelve los partners que están asignados a esos vendedores
    (`res.partner.user_id`) o que han recibido al menos una factura
    emitida por esos vendedores (`account.move.invoice_user_id`).

    La unión de ambos criterios cubre los dos modelos típicos:
      - Partners que tienen vendedor asignado en Odoo.
      - Partners cuyo único vínculo es haber sido facturados por el
        vendedor (sin asignación formal).
    """
    if partners is None or partners.empty or not user_ids:
        return pd.DataFrame()
    uids = set(int(u) for u in user_ids)

    asig: set[int] = set()

    # 1) Partners con user_id asignado
    if "user_id" in partners.columns:
        ids_p = pd.to_numeric(partners["user_id"], errors="coerce")
        asig.update(
            partners.loc[ids_p.isin(uids), "id"]
            .dropna().astype(int).tolist()
        )

    # 2) Partners en facturas con invoice_user_id en uids
    if (
        invoices is not None and not invoices.empty
        and "invoice_user_id" in invoices.columns
    ):
        ids_i = pd.to_numeric(invoices["invoice_user_id"], errors="coerce")
        asig.update(
            pd.to_numeric(
                invoices.loc[ids_i.isin(uids), "partner_id"], errors="coerce"
            ).dropna().astype(int).tolist()
        )

    if not asig:
        return pd.DataFrame()
    return partners[
        pd.to_numeric(partners["id"], errors="coerce").isin(asig)
    ].reset_index(drop=True)


def compute_monthly_clients_kpi(
    invoice_lines: pd.DataFrame,
    months: int = 12,
    cutoff_date: date | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Numérica mensual: para cada uno de los últimos `months` meses,
    calcula:
      - n_clientes_atendidos: clientes únicos con factura ese mes.
      - ventas_netas: subtotal sin IVA.
      - n_facturas: facturas únicas.
      - ticket_promedio: ventas / n_facturas.

    Retorna DataFrame con una fila por mes, ordenada cronológicamente.
    """
    if cutoff_date is None:
        cutoff_date = date.today()
    cutoff_ts = pd.Timestamp(cutoff_date)
    end_period = cutoff_ts.to_period("M").to_timestamp(how="end")
    start_period = (
        end_period.to_period("M") - (months - 1)
    ).to_timestamp(how="start")

    df = _filter_lines_for_sales(
        invoice_lines, date_from=start_period, date_to=end_period,
        company_ids=company_ids,
    )

    full_index = pd.period_range(start=start_period, end=end_period, freq="M")
    base = pd.DataFrame(index=full_index)
    base.index.name = "mes"

    if df.empty:
        out = base.assign(
            n_clientes_atendidos=0, ventas_netas=0.0,
            n_facturas=0, ticket_promedio=0.0,
        ).reset_index()
    else:
        df = df.copy()
        df["mes"] = df["_d"].dt.to_period("M")
        is_fac = df["move_type"] == "out_invoice"
        agg = pd.DataFrame({
            "n_clientes_atendidos": df.groupby("mes")["partner_id"].nunique(),
            "ventas_netas": df.groupby("mes")["price_subtotal_signed"].sum(),
            "n_facturas": df.loc[is_fac].groupby("mes")["move_id"].nunique(),
        })
        agg = base.join(agg, how="left").fillna(0.0)
        agg["n_clientes_atendidos"] = agg["n_clientes_atendidos"].astype(int)
        agg["n_facturas"] = agg["n_facturas"].astype(int)
        agg["ticket_promedio"] = np.where(
            agg["n_facturas"] > 0,
            agg["ventas_netas"] / agg["n_facturas"].replace(0, np.nan),
            0.0,
        )
        out = agg.reset_index()
    out["mes_label"] = out["mes"].astype(str)
    return out


def compute_sales_by_city(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Ventas y clientes atendidos por ciudad. Cruza con `partners.city`.
    """
    if assigned_partners is None or assigned_partners.empty:
        return pd.DataFrame()

    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return pd.DataFrame()

    geo_cols = [c for c in ["id", "city", "state_name"] if c in assigned_partners.columns]
    geo = assigned_partners[geo_cols].rename(columns={"id": "partner_id"}).copy()
    df = df.merge(geo, on="partner_id", how="inner")
    if df.empty:
        return pd.DataFrame()

    df["city"] = df["city"].fillna("Sin ciudad").replace("", "Sin ciudad")
    is_fac = df["move_type"] == "out_invoice"
    grp_cols = ["city"] + (["state_name"] if "state_name" in df.columns else [])

    grp = df.groupby(grp_cols)
    res = pd.DataFrame({
        "n_clientes": grp["partner_id"].nunique(),
        "n_facturas": df.loc[is_fac].groupby(grp_cols)["move_id"].nunique(),
        "ventas_netas": grp["price_subtotal_signed"].sum(),
    }).fillna(0.0).reset_index()
    res["n_facturas"] = res["n_facturas"].astype(int)
    res["ticket_promedio"] = np.where(
        res["n_facturas"] > 0,
        res["ventas_netas"] / res["n_facturas"].replace(0, np.nan),
        0.0,
    )
    total = float(res["ventas_netas"].sum())
    res["participacion_pct"] = (
        res["ventas_netas"] / total * 100 if total else 0.0
    )
    return res.sort_values("ventas_netas", ascending=False).reset_index(drop=True)


def compute_visit_frequency(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Frecuencia de visita por cliente (visita = factura).
    """
    cols = [
        "partner_id", "partner_name", "city", "num_visitas",
        "dias_entre_visitas_prom", "ultima_visita", "dias_desde_ultima",
        "ventas_periodo",
    ]
    if assigned_partners is None or assigned_partners.empty:
        return pd.DataFrame(columns=cols)

    asig_ids = set(assigned_partners["id"].astype(int).tolist())
    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df["partner_id"].isin(asig_ids)].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)

    cutoff = pd.Timestamp(date_to)
    visitas = (
        df.dropna(subset=["move_id", "_d"])
        .drop_duplicates(["partner_id", "move_id"])
        [["partner_id", "_d"]]
        .sort_values(["partner_id", "_d"])
    )
    ventas_pid = df.groupby("partner_id")["price_subtotal_signed"].sum().to_dict()
    name_pid = (
        df.dropna(subset=["partner_name"])
        .drop_duplicates("partner_id")
        .set_index("partner_id")["partner_name"].to_dict()
    )
    city_map = {}
    if "city" in assigned_partners.columns:
        city_map = (
            assigned_partners[["id", "city"]].rename(columns={"id": "partner_id"})
            .set_index("partner_id")["city"].to_dict()
        )

    out_rows = []
    for pid, sub in visitas.groupby("partner_id"):
        fechas = sub["_d"].tolist()
        n_v = len(fechas)
        if n_v >= 2:
            diffs = [(fechas[i] - fechas[i - 1]).days for i in range(1, n_v)]
            dias_prom = float(np.mean(diffs))
        else:
            dias_prom = np.nan
        ultima = max(fechas)
        out_rows.append({
            "partner_id": int(pid),
            "partner_name": name_pid.get(pid, "—"),
            "city": city_map.get(pid, "—") or "—",
            "num_visitas": n_v,
            "dias_entre_visitas_prom": dias_prom,
            "ultima_visita": ultima,
            "dias_desde_ultima": (cutoff - ultima).days,
            "ventas_periodo": float(ventas_pid.get(pid, 0.0)),
        })
    return pd.DataFrame(out_rows).sort_values(
        "ventas_periodo", ascending=False
    ).reset_index(drop=True)


def build_geo_dataframe(
    assigned_partners: pd.DataFrame,
    invoice_lines: pd.DataFrame | None = None,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    DataFrame para mapas: lat/lon + ventas + visitas en el período.
    Devuelve solo clientes con coordenadas válidas.
    """
    if assigned_partners is None or assigned_partners.empty:
        return pd.DataFrame()
    df = assigned_partners.copy()
    if not {"partner_latitude", "partner_longitude"}.issubset(df.columns):
        return pd.DataFrame()

    df["lat"] = pd.to_numeric(df["partner_latitude"], errors="coerce")
    df["lon"] = pd.to_numeric(df["partner_longitude"], errors="coerce")
    df = df[
        df["lat"].notna() & df["lon"].notna()
        & (df["lat"] != 0) & (df["lon"] != 0)
    ].copy()
    if df.empty:
        return df
    df = df.rename(columns={"id": "partner_id", "name": "partner_name"})

    ventas_pid: dict[int, float] = {}
    visitas_pid: dict[int, int] = {}
    if invoice_lines is not None and not invoice_lines.empty:
        ldf = _filter_lines_for_sales(
            invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
        )
        if not ldf.empty:
            asig = set(df["partner_id"].astype(int).tolist())
            ldf = ldf[ldf["partner_id"].isin(asig)]
            if not ldf.empty:
                ventas_pid = ldf.groupby("partner_id")["price_subtotal_signed"].sum().to_dict()
                visitas_pid = (
                    ldf.drop_duplicates(["partner_id", "move_id"])
                    .groupby("partner_id")["move_id"].count().to_dict()
                )
    df["partner_id"] = df["partner_id"].astype(int)
    df["ventas_periodo"] = df["partner_id"].map(ventas_pid).fillna(0.0)
    df["num_visitas"] = df["partner_id"].map(visitas_pid).fillna(0).astype(int)
    df["es_atendido"] = df["ventas_periodo"] > 0

    cols_out = [
        "partner_id", "partner_name", "city",
        "lat", "lon", "ventas_periodo", "num_visitas", "es_atendido",
    ]
    cols_out = [c for c in cols_out if c in df.columns]
    return df[cols_out].reset_index(drop=True)


def detect_inactive_clients(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    cutoff: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
    threshold_days: int = 60,
) -> pd.DataFrame:
    """
    Clientes asignados sin compra hace > N días.
    """
    cutoff_ts = pd.Timestamp(cutoff)
    if assigned_partners is None or assigned_partners.empty:
        return pd.DataFrame()
    asig_ids = set(assigned_partners["id"].astype(int).tolist())

    df_all = _filter_lines_for_sales(invoice_lines, company_ids=company_ids)
    if df_all.empty:
        return pd.DataFrame()
    df_all = df_all[df_all["partner_id"].isin(asig_ids)]
    if df_all.empty:
        return pd.DataFrame()

    ultima = (
        df_all.groupby("partner_id")["_d"].max()
        .rename("ultima_factura").to_frame()
    )
    ultima["dias_desde_ultima"] = (cutoff_ts - ultima["ultima_factura"]).dt.days
    ultima["ventas_historicas"] = (
        df_all.groupby("partner_id")["price_subtotal_signed"].sum()
    )
    inactivos = ultima[ultima["dias_desde_ultima"] > threshold_days].copy()
    inactivos = inactivos.reset_index()
    name_map = (
        df_all.dropna(subset=["partner_name"])
        .drop_duplicates("partner_id")
        .set_index("partner_id")["partner_name"].to_dict()
    )
    inactivos["partner_name"] = inactivos["partner_id"].map(name_map).fillna("—")
    if "city" in assigned_partners.columns:
        city_map = (
            assigned_partners[["id", "city"]].rename(columns={"id": "partner_id"})
            .set_index("partner_id")["city"].to_dict()
        )
        inactivos["city"] = inactivos["partner_id"].map(city_map).fillna("—")
    return inactivos.sort_values(
        "ventas_historicas", ascending=False
    ).reset_index(drop=True)
