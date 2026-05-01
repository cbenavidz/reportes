# -*- coding: utf-8 -*-
"""
Análisis de Ventas en Ruta — vendedores externos.

Métricas específicas de un equipo que visita clientes en territorio:
  - Cobertura: % de clientes asignados que recibieron al menos 1 factura.
  - Frecuencia de visita: días promedio entre facturas a un mismo cliente.
  - Clientes nuevos en el período (primera factura emitida).
  - Clientes inactivos: asignados al vendedor que NO compran hace > N días.
  - Análisis geográfico: ventas por ciudad/depto, mapa GPS.
  - Zonificación: clusters de clientes por proximidad GPS (k-means).
  - Oportunidades: clientes con caída de ventas vs período anterior.

Anclado a `invoice_date` (fecha de FACTURACIÓN), igual que el resto del
informe de ventas. SOAT/ANTCL excluidos automáticamente.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

import numpy as np
import pandas as pd

from .sales_analyzer import _filter_lines_for_sales

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================

def get_partners_by_team(
    partners: pd.DataFrame,
    team_name: str = "Lubricantes",
) -> pd.DataFrame:
    """
    Filtra partners por equipo de ventas (`crm.team`).

    Match case-insensitive contains contra `team_name` columna del partner.
    Si el campo `team_name` no existe en el DataFrame (campo no extraído),
    devuelve un DataFrame vacío.
    """
    if partners is None or partners.empty or "team_name" not in partners.columns:
        return pd.DataFrame()
    target = team_name.strip().lower()
    if not target:
        return pd.DataFrame()
    mask = (
        partners["team_name"]
        .astype("string")
        .fillna("")
        .str.strip()
        .str.lower()
        .str.contains(target, regex=False, na=False)
    )
    return partners[mask].reset_index(drop=True)


def get_team_sellers(
    partners: pd.DataFrame,
    team_name: str = "Lubricantes",
) -> dict[int, str]:
    """
    Devuelve {user_id: user_name} con los vendedores activos en un equipo.

    Se infiere desde los partners: los `user_id` distintos asignados a
    clientes del equipo. Si no hay clientes con team asignado, devuelve {}.
    """
    team_partners = get_partners_by_team(partners, team_name)
    if team_partners.empty or "user_id" not in team_partners.columns:
        return {}
    df = team_partners[["user_id", "user_name"]].dropna(subset=["user_id"]).copy()
    if df.empty:
        return {}
    df["user_id"] = df["user_id"].astype(int)
    df = df.drop_duplicates("user_id")
    return dict(zip(df["user_id"], df["user_name"].fillna("—").astype(str)))


def get_external_sellers(
    partners: pd.DataFrame,
    seller_names: Iterable[str] = ("Luis Felipe Hurtado", "Yarley Vanessa"),
) -> dict[int, str]:
    """
    DEPRECATED — preferir `get_team_sellers("Lubricantes")` que es robusto
    a cambios de personal. Se mantiene por compatibilidad.

    Devuelve un dict {user_id: user_name} buscando por nombre.
    """
    if partners is None or partners.empty or "user_id" not in partners.columns:
        return {}

    df = partners[["user_id", "user_name"]].dropna(subset=["user_id"]).copy()
    if df.empty:
        return {}
    df["user_id"] = df["user_id"].astype(int)
    df = df.drop_duplicates(subset="user_id")

    target = [n.strip().lower() for n in seller_names if n]
    if not target:
        return {}

    out: dict[int, str] = {}
    for _, row in df.iterrows():
        name = str(row.get("user_name") or "").strip().lower()
        if not name:
            continue
        # Match por contains de cualquier token del target
        for t in target:
            if t in name or name in t:
                out[int(row["user_id"])] = row["user_name"]
                break
    return out


def get_assigned_partners(
    partners: pd.DataFrame,
    user_ids: Iterable[int],
) -> pd.DataFrame:
    """
    Devuelve los partners (clientes) asignados a esos vendedores
    (`res.partner.user_id`). Retorna las columnas relevantes para el
    análisis de ruta: ubicación, contacto, etc.
    """
    if partners is None or partners.empty or not user_ids:
        return pd.DataFrame()
    keep = partners[
        pd.to_numeric(partners.get("user_id"), errors="coerce").isin(list(user_ids))
    ].copy()
    return keep.reset_index(drop=True)


# =============================================================================
# Cobertura y actividad
# =============================================================================

def compute_coverage_kpis(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
) -> dict:
    """
    Métricas de cobertura del territorio:
      - n_clientes_asignados: total en la base del vendedor
      - n_clientes_atendidos: con al menos una factura en el período
      - cobertura_pct
      - n_clientes_nuevos: primera factura en este período (en todo el histórico)
      - n_clientes_inactivos_30d: asignados sin factura en últimos 30 días
      - n_clientes_inactivos_60d, 90d
    """
    if assigned_partners is None or assigned_partners.empty:
        return _empty_coverage()

    asig_ids = set(
        pd.to_numeric(assigned_partners["id"], errors="coerce")
        .dropna()
        .astype(int)
        .tolist()
    )
    n_asig = len(asig_ids)
    if n_asig == 0:
        return _empty_coverage()

    # Líneas del período
    df_periodo = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    df_periodo = df_periodo[df_periodo["partner_id"].isin(asig_ids)] if not df_periodo.empty else df_periodo
    atendidos_ids = (
        set(df_periodo["partner_id"].dropna().astype(int).unique().tolist())
        if not df_periodo.empty else set()
    )

    # Clientes nuevos: cuya PRIMERA factura está en el período
    primera_factura_global: dict[int, pd.Timestamp] = {}
    if invoice_lines is not None and not invoice_lines.empty:
        df_global = _filter_lines_for_sales(
            invoice_lines, company_ids=company_ids,
        )
        df_global = df_global[df_global["partner_id"].isin(asig_ids)] if not df_global.empty else df_global
        if not df_global.empty:
            primera_factura_global = (
                df_global.groupby("partner_id")["_d"]
                .min()
                .to_dict()
            )

    desde_ts = pd.Timestamp(date_from)
    hasta_ts = pd.Timestamp(date_to)
    nuevos = sum(
        1 for pid, fecha in primera_factura_global.items()
        if pid in atendidos_ids and desde_ts <= fecha <= hasta_ts
    )

    # Inactivos: clientes asignados cuya ÚLTIMA factura es vieja
    ultima_factura_global: dict[int, pd.Timestamp] = {}
    if invoice_lines is not None and not invoice_lines.empty:
        df_all = _filter_lines_for_sales(invoice_lines, company_ids=company_ids)
        df_all = df_all[df_all["partner_id"].isin(asig_ids)] if not df_all.empty else df_all
        if not df_all.empty:
            ultima_factura_global = (
                df_all.groupby("partner_id")["_d"].max().to_dict()
            )

    cutoff = hasta_ts
    n_inact_30 = 0
    n_inact_60 = 0
    n_inact_90 = 0
    n_jamas = 0
    for pid in asig_ids:
        ult = ultima_factura_global.get(pid)
        if ult is None or pd.isna(ult):
            n_jamas += 1
            n_inact_30 += 1
            n_inact_60 += 1
            n_inact_90 += 1
            continue
        dias = (cutoff - ult).days
        if dias > 30:
            n_inact_30 += 1
        if dias > 60:
            n_inact_60 += 1
        if dias > 90:
            n_inact_90 += 1

    return {
        "n_clientes_asignados": n_asig,
        "n_clientes_atendidos": len(atendidos_ids),
        "cobertura_pct": (len(atendidos_ids) / n_asig * 100) if n_asig else 0.0,
        "n_clientes_nuevos": nuevos,
        "n_clientes_inactivos_30d": n_inact_30,
        "n_clientes_inactivos_60d": n_inact_60,
        "n_clientes_inactivos_90d": n_inact_90,
        "n_clientes_jamas_comprado": n_jamas,
    }


def _empty_coverage() -> dict:
    return {
        "n_clientes_asignados": 0,
        "n_clientes_atendidos": 0,
        "cobertura_pct": 0.0,
        "n_clientes_nuevos": 0,
        "n_clientes_inactivos_30d": 0,
        "n_clientes_inactivos_60d": 0,
        "n_clientes_inactivos_90d": 0,
        "n_clientes_jamas_comprado": 0,
    }


def compute_visit_frequency(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Frecuencia de visita por cliente (visitas = facturas distintas).

    Para cada cliente atendido en el período, calcula:
      - num_visitas: # de facturas (move_id distintos) en el período
      - dias_entre_visitas_prom: promedio de días entre facturas consecutivas
      - ultima_visita: fecha de la última factura en el período
      - dias_desde_ultima: cutoff - ultima_visita
    """
    cols = [
        "partner_id", "partner_name", "num_visitas",
        "dias_entre_visitas_prom", "ultima_visita", "dias_desde_ultima",
        "ventas_periodo",
    ]
    if assigned_partners is None or assigned_partners.empty:
        return pd.DataFrame(columns=cols)

    asig_ids = set(
        pd.to_numeric(assigned_partners["id"], errors="coerce")
        .dropna().astype(int).tolist()
    )

    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df[df["partner_id"].isin(asig_ids)].copy()
    if df.empty:
        return pd.DataFrame(columns=cols)

    cutoff = pd.Timestamp(date_to)

    # Para cada cliente, fechas únicas de factura (move_id)
    visitas = (
        df.dropna(subset=["move_id", "_d"])
          .drop_duplicates(["partner_id", "move_id"])
          [["partner_id", "_d"]]
          .sort_values(["partner_id", "_d"])
    )
    grp = visitas.groupby("partner_id")
    out_rows = []
    ventas_pid = df.groupby("partner_id")["price_subtotal_signed"].sum().to_dict()
    name_pid = (
        df.dropna(subset=["partner_name"])
          .drop_duplicates("partner_id")
          .set_index("partner_id")["partner_name"]
          .to_dict()
    )
    for pid, sub in grp:
        fechas = sub["_d"].tolist()
        n_v = len(fechas)
        if n_v >= 2:
            diffs = [
                (fechas[i] - fechas[i - 1]).days
                for i in range(1, n_v)
            ]
            dias_prom = float(np.mean(diffs)) if diffs else np.nan
        else:
            dias_prom = np.nan
        ultima = max(fechas)
        out_rows.append({
            "partner_id": int(pid),
            "partner_name": name_pid.get(pid, "—"),
            "num_visitas": n_v,
            "dias_entre_visitas_prom": dias_prom,
            "ultima_visita": ultima,
            "dias_desde_ultima": (cutoff - ultima).days,
            "ventas_periodo": float(ventas_pid.get(pid, 0.0)),
        })

    return pd.DataFrame(out_rows).sort_values(
        "ventas_periodo", ascending=False
    ).reset_index(drop=True)


# =============================================================================
# Análisis geográfico
# =============================================================================

def compute_sales_by_city(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    date_from: date | pd.Timestamp,
    date_to: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    Ventas por ciudad. Cruza `invoice_lines` con la ciudad del partner.

    Columnas:
      - city, state_name (departamento, si está)
      - n_clientes (atendidos)
      - n_facturas
      - ventas_netas
      - ticket_promedio
      - participacion_pct
    """
    if assigned_partners is None or assigned_partners.empty:
        return pd.DataFrame()

    df = _filter_lines_for_sales(
        invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
    )
    if df.empty:
        return pd.DataFrame()

    # Map partner_id → city, state_name
    cols_keep = ["id", "city", "state_name"] if "state_name" in assigned_partners.columns else ["id", "city"]
    cols_keep = [c for c in cols_keep if c in assigned_partners.columns]
    geo = assigned_partners[cols_keep].copy()
    geo = geo.rename(columns={"id": "partner_id"})

    df = df.merge(geo, on="partner_id", how="inner")  # solo partners asignados
    if df.empty:
        return pd.DataFrame()

    df["city"] = df["city"].fillna("Sin ciudad").replace("", "Sin ciudad")
    if "state_name" in df.columns:
        df["state_name"] = df["state_name"].fillna("—").replace("", "—")

    is_fac = df["move_type"] == "out_invoice"

    group_cols = ["city"] + (["state_name"] if "state_name" in df.columns else [])
    grp = df.groupby(group_cols)
    res = pd.DataFrame({
        "n_clientes": grp["partner_id"].nunique(),
        "n_facturas": df.loc[is_fac].groupby(group_cols)["move_id"].nunique(),
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


def build_geo_dataframe(
    assigned_partners: pd.DataFrame,
    invoice_lines: pd.DataFrame | None = None,
    date_from: date | pd.Timestamp | None = None,
    date_to: date | pd.Timestamp | None = None,
    company_ids: Iterable[int] | None = None,
) -> pd.DataFrame:
    """
    DataFrame para mapas: lat/lon + ventas del cliente en el período (opcional).

    Devuelve solo clientes con coordenadas válidas. Columnas:
      - partner_id, partner_name, city, state_name
      - lat, lon
      - ventas_periodo (0 si no hay ventas en el período)
      - es_atendido (bool: tuvo al menos 1 factura en el período)
      - num_visitas (en el período)
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

    # Ventas en el período por partner_id
    ventas_pid: dict[int, float] = {}
    visitas_pid: dict[int, int] = {}
    if invoice_lines is not None and not invoice_lines.empty:
        ldf = _filter_lines_for_sales(
            invoice_lines, date_from=date_from, date_to=date_to, company_ids=company_ids,
        )
        if not ldf.empty:
            asig_ids = set(df["partner_id"].astype(int).tolist())
            ldf = ldf[ldf["partner_id"].isin(asig_ids)]
            if not ldf.empty:
                ventas_pid = ldf.groupby("partner_id")["price_subtotal_signed"].sum().to_dict()
                visitas_pid = (
                    ldf.drop_duplicates(["partner_id", "move_id"])
                    .groupby("partner_id")["move_id"].count()
                    .to_dict()
                )

    df["partner_id"] = df["partner_id"].astype(int)
    df["ventas_periodo"] = df["partner_id"].map(ventas_pid).fillna(0.0)
    df["num_visitas"] = df["partner_id"].map(visitas_pid).fillna(0).astype(int)
    df["es_atendido"] = df["ventas_periodo"] > 0

    cols_out = [
        "partner_id", "partner_name", "city",
        "lat", "lon", "ventas_periodo", "num_visitas", "es_atendido",
    ]
    if "state_name" in df.columns:
        cols_out.insert(3, "state_name")
    cols_out = [c for c in cols_out if c in df.columns]
    return df[cols_out].reset_index(drop=True)


# =============================================================================
# Zonificación (clustering)
# =============================================================================

def zonify_partners(
    geo_df: pd.DataFrame,
    n_zones: int = 5,
    min_partners_per_zone: int = 3,
) -> pd.DataFrame:
    """
    Agrupa clientes en N zonas geográficas usando k-means sobre lat/lon.

    Retorna `geo_df` con columna `zona` (str: "Zona 1", "Zona 2", ...).
    Si no hay suficientes clientes con GPS, retorna geo_df sin la columna.

    Implementación simple sin sklearn — usa k-means a mano (Lloyd's
    algorithm) para mantener dependencias mínimas.
    """
    if geo_df is None or geo_df.empty or len(geo_df) < min_partners_per_zone * 2:
        out = geo_df.copy() if geo_df is not None else pd.DataFrame()
        if not out.empty:
            out["zona"] = "Zona única"
        return out

    pts = geo_df[["lat", "lon"]].to_numpy(dtype=float)
    n = len(pts)
    k = min(n_zones, max(2, n // min_partners_per_zone))

    # K-means simple
    rng = np.random.default_rng(42)
    # Init: elegir k puntos aleatorios distintos
    idx = rng.choice(n, size=k, replace=False)
    centers = pts[idx].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(50):  # max iters
        # Asignar cada punto al centro más cercano (distancia euclidiana — basta
        # para clusters pequeños; para distancias en km usaríamos haversine)
        d = ((pts[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        if (new_labels == labels).all():
            break
        labels = new_labels
        # Recalcular centros
        for j in range(k):
            mask = labels == j
            if mask.any():
                centers[j] = pts[mask].mean(axis=0)

    out = geo_df.copy()
    # Renumerar zonas por # de clientes (zona 1 = la más grande)
    counts = pd.Series(labels).value_counts()
    rename_map = {old: f"Zona {new + 1}" for new, old in enumerate(counts.index)}
    out["zona"] = pd.Series(labels).map(rename_map).values
    return out


# =============================================================================
# Oportunidades de venta
# =============================================================================

def detect_opportunities(
    invoice_lines: pd.DataFrame,
    assigned_partners: pd.DataFrame,
    cutoff: date | pd.Timestamp,
    company_ids: Iterable[int] | None = None,
    inactivity_threshold_days: int = 60,
    drop_threshold_pct: float = 30.0,
) -> dict[str, pd.DataFrame]:
    """
    Detecta oportunidades de venta accionables:

      - inactivos: clientes asignados sin compra hace > N días.
      - en_caida: clientes activos cuyas ventas bajaron > X% vs período
        anterior (mes vs mes anterior, mismo largo).

    Retorna dict con dos DataFrames:
      {"inactivos": ..., "en_caida": ...}
    """
    cutoff_ts = pd.Timestamp(cutoff)
    empty = pd.DataFrame()
    res = {"inactivos": empty, "en_caida": empty}

    if assigned_partners is None or assigned_partners.empty:
        return res

    asig_ids = set(
        pd.to_numeric(assigned_partners["id"], errors="coerce")
        .dropna().astype(int).tolist()
    )

    df_all = _filter_lines_for_sales(invoice_lines, company_ids=company_ids)
    if df_all.empty:
        return res
    df_all = df_all[df_all["partner_id"].isin(asig_ids)]
    if df_all.empty:
        return res

    # ---- Inactivos: última factura > N días atrás
    ultima = (
        df_all.groupby("partner_id")["_d"].max()
        .rename("ultima_factura").to_frame()
    )
    ultima["dias_desde_ultima"] = (cutoff_ts - ultima["ultima_factura"]).dt.days
    ultima["ventas_historicas"] = (
        df_all.groupby("partner_id")["price_subtotal_signed"].sum()
    )
    inactivos = ultima[ultima["dias_desde_ultima"] > inactivity_threshold_days].copy()
    inactivos = inactivos.reset_index()
    # Enriquecer con nombre/ciudad
    name_map = (
        df_all.dropna(subset=["partner_name"])
        .drop_duplicates("partner_id")
        .set_index("partner_id")["partner_name"]
        .to_dict()
    )
    inactivos["partner_name"] = inactivos["partner_id"].map(name_map).fillna("—")
    if "city" in assigned_partners.columns:
        city_map = (
            assigned_partners[["id", "city"]].rename(columns={"id": "partner_id"})
            .set_index("partner_id")["city"].to_dict()
        )
        inactivos["city"] = inactivos["partner_id"].map(city_map).fillna("—")
    inactivos = inactivos.sort_values(
        "ventas_historicas", ascending=False
    ).reset_index(drop=True)
    res["inactivos"] = inactivos

    # ---- En caída: comparar último mes vs mes anterior
    end_actual = cutoff_ts.to_period("M").to_timestamp(how="end")
    start_actual = cutoff_ts.to_period("M").to_timestamp(how="start")
    end_prev = start_actual - pd.Timedelta(days=1)
    start_prev = end_prev.to_period("M").to_timestamp(how="start")

    actual_pid = (
        df_all[(df_all["_d"] >= start_actual) & (df_all["_d"] <= end_actual)]
        .groupby("partner_id")["price_subtotal_signed"].sum()
        .rename("ventas_actual")
    )
    prev_pid = (
        df_all[(df_all["_d"] >= start_prev) & (df_all["_d"] <= end_prev)]
        .groupby("partner_id")["price_subtotal_signed"].sum()
        .rename("ventas_anterior")
    )
    cmp_df = pd.concat([actual_pid, prev_pid], axis=1).fillna(0.0)
    cmp_df = cmp_df[cmp_df["ventas_anterior"] > 0]
    cmp_df["var_pct"] = (
        (cmp_df["ventas_actual"] - cmp_df["ventas_anterior"])
        / cmp_df["ventas_anterior"] * 100
    )
    en_caida = cmp_df[cmp_df["var_pct"] <= -drop_threshold_pct].copy()
    en_caida["caida_abs"] = en_caida["ventas_actual"] - en_caida["ventas_anterior"]
    en_caida = en_caida.reset_index()
    en_caida["partner_name"] = en_caida["partner_id"].map(name_map).fillna("—")
    if "city" in assigned_partners.columns:
        en_caida["city"] = en_caida["partner_id"].map(city_map).fillna("—")
    en_caida = en_caida.sort_values("caida_abs").reset_index(drop=True)
    res["en_caida"] = en_caida

    return res
