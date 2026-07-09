# -*- coding: utf-8 -*-
"""
Optimizador del rutero sobre los datos REALES del módulo `sales_route_mobile`.

  - Frecuencia sugerida por cliente: a partir de sus VENTAS y su número de
    FACTURAS POR MES (no del valor por defecto de Odoo, que está en 'weekly'
    para todos).
  - Zonificación por cercanía en 5 días (Lun-Vie), poniendo la zona de menor
    venta el lunes (en Colombia muchos festivos caen en lunes).
  - Secuencia de visita dentro de cada día por vecino más cercano.

Pandas/numpy puro: sin Streamlit ni Odoo, para poder probarlo aislado.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .rutero_planner import DIAS, kmeans_geo, order_nearest_neighbor, haversine

# Códigos de Odoo (res.partner.sr_visit_frequency), de menor a mayor intensidad
ORDEN_FREQ = ["on_demand", "monthly", "biweekly", "weekly"]
FREQ_LABEL = {
    "weekly": "Semanal", "biweekly": "Quincenal",
    "monthly": "Mensual", "on_demand": "Bajo demanda",
}
FREQ_SEMANAS = {
    "weekly": "1,2,3,4", "biweekly": "1,3", "monthly": "1", "on_demand": "1",
}


# ---------------------------------------------------------------------------
# 1) Métricas por cliente: ventas y facturas por mes
# ---------------------------------------------------------------------------
def metricas_clientes(lines: pd.DataFrame, meses: float = 12.0) -> pd.DataFrame:
    """
    A partir de las líneas de factura, calcula por cliente:
      ventas (netas del período), n_facturas, facturas_mes, ventas_mes.
    """
    cols = ["partner_id", "ventas", "n_facturas", "facturas_mes", "ventas_mes"]
    if lines is None or lines.empty:
        return pd.DataFrame(columns=cols)
    df = lines.copy()
    meses = max(float(meses), 1.0)

    ventas = df.groupby("partner_id")["price_subtotal_signed"].sum()
    es_fac = df["move_type"] == "out_invoice" if "move_type" in df.columns else True
    n_fac = (
        df[es_fac].groupby("partner_id")["move_id"].nunique()
        if "move_id" in df.columns else pd.Series(dtype=int)
    )
    res = pd.DataFrame({"ventas": ventas}).join(
        n_fac.rename("n_facturas"), how="left"
    ).reset_index()
    res["n_facturas"] = res["n_facturas"].fillna(0).astype(int)
    res["facturas_mes"] = res["n_facturas"] / meses
    res["ventas_mes"] = res["ventas"] / meses
    return res[cols]


# ---------------------------------------------------------------------------
# 2) Frecuencia sugerida (ventas + facturas/mes)
# ---------------------------------------------------------------------------
def _sube_un_nivel(code: str) -> str:
    i = ORDEN_FREQ.index(code) if code in ORDEN_FREQ else 0
    return ORDEN_FREQ[min(i + 1, len(ORDEN_FREQ) - 1)]


def frecuencia_por_facturas(facturas_mes: float) -> str:
    """Frecuencia base según cuántas facturas hace el cliente al mes."""
    try:
        f = float(facturas_mes)
    except (TypeError, ValueError):
        return "on_demand"
    if f >= 3.5:
        return "weekly"      # ~1 por semana o más
    if f >= 1.5:
        return "biweekly"    # ~cada 15 días
    if f >= 0.5:
        return "monthly"     # ~1 al mes
    return "on_demand"


def sugerir_frecuencias(
    met: pd.DataFrame, percentil_alto: float = 0.80,
) -> pd.DataFrame:
    """
    Añade la frecuencia sugerida. Base = facturas/mes; los clientes de ALTO
    VALOR (ventas en el percentil superior) suben un nivel de intensidad.
    """
    if met is None or met.empty:
        return pd.DataFrame(columns=list(met.columns if met is not None else [])
                            + ["alto_valor", "frecuencia_code",
                               "frecuencia", "semanas"])
    df = met.copy()
    umbral = float(df["ventas"].quantile(percentil_alto)) if len(df) > 1 else np.inf
    df["alto_valor"] = df["ventas"] >= umbral
    base = df["facturas_mes"].apply(frecuencia_por_facturas)
    df["frecuencia_code"] = [
        _sube_un_nivel(b) if alto else b
        for b, alto in zip(base, df["alto_valor"])
    ]
    df["frecuencia"] = df["frecuencia_code"].map(FREQ_LABEL)
    df["semanas"] = df["frecuencia_code"].map(FREQ_SEMANAS)
    return df


# ---------------------------------------------------------------------------
# 3) Optimización: día (zona) + secuencia (vecino más cercano)
# ---------------------------------------------------------------------------
def optimizar_rutero(
    clientes: pd.DataFrame,
    dias: int = 5,
    lunes_ligero: bool = True,
) -> pd.DataFrame:
    """
    `clientes` debe traer: partner_id, lat, lon, ventas (y lo que quieras
    arrastrar). Devuelve el mismo DF con columnas `dia` y `secuencia`.
    """
    if clientes is None or clientes.empty:
        return pd.DataFrame(columns=list(clientes.columns if clientes is not None else [])
                            + ["dia", "secuencia"])
    df = clientes.copy()
    df = df[df["lat"].notna() & df["lon"].notna()].reset_index(drop=True)
    if df.empty:
        return df.assign(dia=None, secuencia=None)

    lat = df["lat"].to_numpy(dtype=float)
    lon = df["lon"].to_numpy(dtype=float)
    ventas = (df["ventas"].to_numpy(dtype=float)
              if "ventas" in df.columns else np.zeros(len(df)))

    k = max(1, min(dias, len(df)))
    labels = kmeans_geo(lat, lon, k)

    info = []
    for j in range(k):
        m = labels == j
        info.append({
            "cluster": j,
            "lon": float(lon[m].mean()) if m.any() else 0.0,
            "ventas": float(ventas[m].sum()) if m.any() else 0.0,
        })
    por_lon = sorted(info, key=lambda x: x["lon"])
    if lunes_ligero and len(info) >= 2:
        menor = min(info, key=lambda x: x["ventas"])
        resto = [c for c in por_lon if c["cluster"] != menor["cluster"]]
        orden_final = [menor] + resto
    else:
        orden_final = por_lon
    cluster_a_dia = {c["cluster"]: DIAS[i % len(DIAS)]
                     for i, c in enumerate(orden_final)}
    df["dia"] = [cluster_a_dia[l] for l in labels]

    # Secuencia dentro de cada día: vecino más cercano arrancando por el
    # cliente de mayor venta (ancla comercial). Se numera de 10 en 10 para
    # dejar espacio a inserciones manuales en Odoo.
    df["secuencia"] = 0
    for dia in df["dia"].unique():
        sub = df[df["dia"] == dia]
        slat = sub["lat"].to_numpy(dtype=float)
        slon = sub["lon"].to_numpy(dtype=float)
        start = int(np.argmax(sub["ventas"].to_numpy(dtype=float))) \
            if "ventas" in sub.columns and len(sub) else 0
        orden_local = order_nearest_neighbor(slat, slon, start=start)
        rank = {pos: r for r, pos in enumerate(orden_local)}
        idx = sub.index.to_list()
        for pos, i in enumerate(idx):
            df.loc[i, "secuencia"] = (rank.get(pos, pos) + 1) * 10

    df["_d"] = df["dia"].map({d: i for i, d in enumerate(DIAS)}).fillna(99)
    return df.sort_values(["_d", "secuencia"]).drop(columns=["_d"]).reset_index(drop=True)


def km_por_dia(rutero: pd.DataFrame) -> pd.DataFrame:
    """Kilómetros de recorrido por día, siguiendo la secuencia."""
    filas = []
    if rutero is None or rutero.empty:
        return pd.DataFrame(columns=["dia", "n_clientes", "ventas", "km_ruta"])
    for dia in DIAS:
        sub = rutero[rutero["dia"] == dia].sort_values("secuencia")
        if sub.empty:
            continue
        lat = sub["lat"].to_numpy(dtype=float)
        lon = sub["lon"].to_numpy(dtype=float)
        km = sum(haversine(lat[i - 1], lon[i - 1], lat[i], lon[i])
                 for i in range(1, len(sub)))
        filas.append({
            "dia": dia,
            "n_clientes": int(len(sub)),
            "ventas": float(sub["ventas"].sum()) if "ventas" in sub.columns else 0.0,
            "km_ruta": round(km, 1),
        })
    return pd.DataFrame(filas)
