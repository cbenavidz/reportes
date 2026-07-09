# -*- coding: utf-8 -*-
"""
Análisis de las VISITAS REALES de ruta (modelo `sr.visit` del módulo
`sales_route_mobile`).

A diferencia del análisis histórico —que infiere la visita a partir de la
factura—, aquí trabajamos con el check-in real del vendedor: fecha/hora, GPS,
distancia al punto del cliente, si la visita fue efectiva y por qué no.

Métricas:
  - KPIs: visitas, efectividad, fuera de geocerca, vendedores activos.
  - Cumplimiento de agenda: clientes planeados del día vs visitados.
  - Frecuencia real por cliente (visitas y días entre visitas).
  - Motivos de visita no efectiva.
  - Visitas sospechosas por distancia al cliente.

Pandas puro: sin Streamlit ni Odoo.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from .route_module import DAY_COLS, SR_DAY_COLS

DAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


# ---------------------------------------------------------------------------
# KPIs generales
# ---------------------------------------------------------------------------
def kpis(visits: pd.DataFrame) -> dict:
    if visits is None or visits.empty:
        return dict(n_visitas=0, n_efectivas=0, efectividad_pct=0.0,
                    n_fuera=0, fuera_pct=0.0, n_clientes=0, n_vendedores=0)
    n = len(visits)
    efe = int(_bool(visits["is_effective"]).sum())
    fuera = int(_bool(visits["outside_geofence"]).sum())
    return dict(
        n_visitas=n,
        n_efectivas=efe,
        efectividad_pct=efe / n * 100.0 if n else 0.0,
        n_fuera=fuera,
        fuera_pct=fuera / n * 100.0 if n else 0.0,
        n_clientes=int(visits["partner_id"].nunique()),
        n_vendedores=int(visits["user_id"].nunique()),
    )


def por_vendedor(visits: pd.DataFrame) -> pd.DataFrame:
    if visits is None or visits.empty:
        return pd.DataFrame(columns=["vendedor", "n_visitas", "n_clientes",
                                     "n_efectivas", "efectividad_pct", "n_fuera"])
    v = visits.copy()
    v["_efe"] = _bool(v["is_effective"])
    v["_fuera"] = _bool(v["outside_geofence"])
    g = v.groupby("user_name", dropna=False)
    res = pd.DataFrame({
        "n_visitas": g.size(),
        "n_clientes": g["partner_id"].nunique(),
        "n_efectivas": g["_efe"].sum(),
        "n_fuera": g["_fuera"].sum(),
    }).reset_index().rename(columns={"user_name": "vendedor"})
    res["efectividad_pct"] = np.where(
        res["n_visitas"] > 0, res["n_efectivas"] / res["n_visitas"] * 100.0, 0.0
    )
    return res.sort_values("n_visitas", ascending=False).reset_index(drop=True)


def evolucion_diaria(visits: pd.DataFrame) -> pd.DataFrame:
    if visits is None or visits.empty or "fecha" not in visits.columns:
        return pd.DataFrame(columns=["dia", "n_visitas", "n_efectivas", "efectividad_pct"])
    v = visits.copy()
    v["dia"] = pd.to_datetime(v["fecha"], errors="coerce").dt.date
    v["_efe"] = _bool(v["is_effective"])
    g = v.dropna(subset=["dia"]).groupby("dia")
    res = pd.DataFrame({
        "n_visitas": g.size(),
        "n_efectivas": g["_efe"].sum(),
    }).reset_index()
    res["efectividad_pct"] = np.where(
        res["n_visitas"] > 0, res["n_efectivas"] / res["n_visitas"] * 100.0, 0.0
    )
    return res.sort_values("dia").reset_index(drop=True)


def motivos_no_efectiva(visits: pd.DataFrame) -> pd.DataFrame:
    if visits is None or visits.empty:
        return pd.DataFrame(columns=["motivo", "n"])
    ne = visits[~_bool(visits["is_effective"])]
    if ne.empty:
        return pd.DataFrame(columns=["motivo", "n"])
    res = (
        ne["reason_name"].fillna("(sin motivo)").value_counts()
        .rename_axis("motivo").reset_index(name="n")
    )
    return res


# ---------------------------------------------------------------------------
# Visitas sospechosas por distancia
# ---------------------------------------------------------------------------
def visitas_sospechosas(visits: pd.DataFrame, umbral_m: float = 200.0) -> pd.DataFrame:
    """Visitas marcadas fuera de geocerca o con distancia > umbral (metros)."""
    if visits is None or visits.empty:
        return pd.DataFrame()
    v = visits.copy()
    v["distance_m"] = pd.to_numeric(v["distance_m"], errors="coerce").fillna(0)
    mask = _bool(v["outside_geofence"]) | (v["distance_m"] > umbral_m)
    cols = [c for c in ["fecha", "partner_name", "user_name", "route_name",
                        "distance_m", "outside_geofence", "is_effective",
                        "reason_name", "accuracy"] if c in v.columns]
    return v[mask][cols].sort_values("distance_m", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Frecuencia real por cliente
# ---------------------------------------------------------------------------
def frecuencia_real(visits: pd.DataFrame, cutoff: date | pd.Timestamp) -> pd.DataFrame:
    cols = ["partner_id", "partner_name", "n_visitas", "n_efectivas",
            "dias_entre_visitas", "ultima_visita", "dias_desde_ultima"]
    if visits is None or visits.empty:
        return pd.DataFrame(columns=cols)
    v = visits.dropna(subset=["fecha"]).copy()
    v["_efe"] = _bool(v["is_effective"])
    cutoff_ts = pd.Timestamp(cutoff)
    filas = []
    for pid, sub in v.groupby("partner_id"):
        fechas = sorted(pd.to_datetime(sub["fecha"]).dt.normalize().unique())
        n = len(fechas)
        if n >= 2:
            diffs = [(fechas[i] - fechas[i - 1]).days for i in range(1, n)]
            dias_entre = float(np.mean(diffs))
        else:
            dias_entre = np.nan
        ultima = max(fechas)
        filas.append({
            "partner_id": int(pid),
            "partner_name": sub["partner_name"].iloc[0],
            "n_visitas": int(len(sub)),
            "n_efectivas": int(sub["_efe"].sum()),
            "dias_entre_visitas": dias_entre,
            "ultima_visita": ultima,
            "dias_desde_ultima": (cutoff_ts - pd.Timestamp(ultima)).days,
        })
    return pd.DataFrame(filas).sort_values("n_visitas", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Cumplimiento de agenda (planeados vs visitados)
# ---------------------------------------------------------------------------
def dias_efectivos_por_cliente(partners: pd.DataFrame,
                               routes: pd.DataFrame) -> dict[int, set[str]]:
    """
    Día(s) de visita de cada cliente: sus días propios si `sr_use_own_days`,
    si no, los del rutero al que pertenece.
    """
    route_days: dict[int, set[str]] = {}
    if routes is not None and not routes.empty:
        for _, r in routes.iterrows():
            route_days[int(r["id"])] = {
                c[4:] for c in DAY_COLS if r.get(c)
            }
    out: dict[int, set[str]] = {}
    if partners is None or partners.empty:
        return out
    for _, p in partners.iterrows():
        if p.get("sr_use_own_days"):
            dias = {c[7:] for c in SR_DAY_COLS if p.get(c)}
        else:
            rid = p.get("sr_route_id")
            dias = route_days.get(int(rid), set()) if pd.notna(rid) else set()
        out[int(p["id"])] = dias
    return out


def cumplimiento_agenda(
    visits: pd.DataFrame,
    partners: pd.DataFrame,
    routes: pd.DataFrame,
    fechas: list[date],
) -> pd.DataFrame:
    """
    Para cada fecha: clientes planeados (activos en ruta cuyo día efectivo es
    ese día de la semana) vs clientes efectivamente visitados.
    """
    cols = ["fecha", "planeados", "visitados", "cumplimiento_pct"]
    if partners is None or partners.empty:
        return pd.DataFrame(columns=cols)
    activos = partners[_bool(partners.get("sr_active_in_route", pd.Series(dtype=bool)))]
    dias_cli = dias_efectivos_por_cliente(activos, routes)

    v = visits.copy() if visits is not None and not visits.empty else pd.DataFrame()
    if not v.empty:
        v["_d"] = pd.to_datetime(v["fecha"], errors="coerce").dt.date

    filas = []
    for f in fechas:
        code = DAY_CODES[f.weekday()]
        planeados = {pid for pid, ds in dias_cli.items() if code in ds}
        visitados = set()
        if not v.empty:
            visitados = set(
                v.loc[v["_d"] == f, "partner_id"].dropna().astype(int).tolist()
            )
        cumplidos = planeados & visitados
        filas.append({
            "fecha": f,
            "planeados": len(planeados),
            "visitados": len(cumplidos),
            "cumplimiento_pct": (
                len(cumplidos) / len(planeados) * 100.0 if planeados else 0.0
            ),
        })
    return pd.DataFrame(filas)
