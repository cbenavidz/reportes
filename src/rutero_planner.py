# -*- coding: utf-8 -*-
"""
Motor del RUTERO para vendedores externos (puerta a puerta).

Arma un plan de visitas semanal (Lun-Vie) a partir de la georreferenciación
de los clientes y su ritmo histórico de compra:

  - DÍA (Lun-Vie)  = zona geográfica (agrupación por cercanía, k-means).
  - FRECUENCIA      = según la cadencia histórica de compra del cliente
                      (semanal / quincenal / mensual / seguimiento).
  - SEMANAS del mes = derivadas de la frecuencia (rotación en 4 semanas).
  - ORDEN del día   = ruta más corta por vecino más cercano (haversine).

Todo es pandas/numpy puro (sin dependencias externas ni Streamlit), para
poder probarlo de forma aislada.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]


# ---------------------------------------------------------------------------
# Geometría
# ---------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2) -> float:
    """Distancia en km entre dos puntos (lat/lon en grados)."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def _dist_matrix(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Matriz de distancias haversine (km) entre todos los puntos."""
    n = len(lat)
    d = np.zeros((n, n))
    for i in range(n):
        d[i] = 2 * 6371.0 * np.arcsin(np.sqrt(
            np.sin(np.radians(lat - lat[i]) / 2) ** 2
            + np.cos(np.radians(lat[i])) * np.cos(np.radians(lat))
            * np.sin(np.radians(lon - lon[i]) / 2) ** 2
        ))
    return d


# ---------------------------------------------------------------------------
# Clustering geográfico (k-means simple, sin sklearn)
# ---------------------------------------------------------------------------
def kmeans_geo(lat: np.ndarray, lon: np.ndarray, k: int,
               iters: int = 50, seed: int = 42) -> np.ndarray:
    """Agrupa puntos en `k` zonas por cercanía. Devuelve labels [0..k-1]."""
    n = len(lat)
    if n == 0:
        return np.array([], dtype=int)
    k = max(1, min(k, n))
    pts = np.column_stack([lat, lon]).astype(float)
    rng = np.random.default_rng(seed)
    # Inicialización tipo k-means++ (simplificada): primer centro aleatorio,
    # los demás lejos de los ya elegidos.
    centers = [pts[rng.integers(n)]]
    for _ in range(1, k):
        d = np.min(
            [np.sum((pts - c) ** 2, axis=1) for c in centers], axis=0
        )
        probs = d / d.sum() if d.sum() > 0 else None
        idx = rng.choice(n, p=probs) if probs is not None else rng.integers(n)
        centers.append(pts[idx])
    centers = np.array(centers)

    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        # Asignar cada punto al centro más cercano
        dists = np.stack([np.sum((pts - c) ** 2, axis=1) for c in centers])
        new_labels = np.argmin(dists, axis=0)
        if np.array_equal(new_labels, labels) and _ > 0:
            labels = new_labels
            break
        labels = new_labels
        # Recalcular centros
        for j in range(k):
            m = labels == j
            if m.any():
                centers[j] = pts[m].mean(axis=0)
    return labels


def order_nearest_neighbor(lat: np.ndarray, lon: np.ndarray,
                           start: int = 0) -> list[int]:
    """Orden de visita por vecino más cercano. Devuelve índices ordenados."""
    n = len(lat)
    if n <= 2:
        return list(range(n))
    d = _dist_matrix(lat, lon)
    visitado = [False] * n
    orden = [start]
    visitado[start] = True
    actual = start
    for _ in range(n - 1):
        dd = d[actual].copy()
        dd[visitado] = np.inf
        nxt = int(np.argmin(dd))
        orden.append(nxt)
        visitado[nxt] = True
        actual = nxt
    return orden


# ---------------------------------------------------------------------------
# Frecuencia por cadencia histórica
# ---------------------------------------------------------------------------
def recomendar_frecuencia(dias_entre) -> tuple[str, list[int], int]:
    """
    A partir de la cadencia histórica (días promedio entre compras), devuelve
    (frecuencia, semanas_del_mes, visitas_por_mes).

    Rotación en 4 semanas:
      Semanal    (<= 10 d)  -> semanas [1,2,3,4]
      Quincenal  (11-20 d)  -> semanas [1,3]
      Mensual    (21-45 d)  -> semanas [1]
      Seguimiento(>45 / s.d.)-> semanas [1]
    """
    try:
        d = float(dias_entre)
    except (TypeError, ValueError):
        d = np.nan
    if np.isnan(d) or d <= 0:
        return ("Seguimiento", [1], 1)
    if d <= 10:
        return ("Semanal", [1, 2, 3, 4], 4)
    if d <= 20:
        return ("Quincenal", [1, 3], 2)
    if d <= 45:
        return ("Mensual", [1], 1)
    return ("Seguimiento", [1], 1)


# ---------------------------------------------------------------------------
# Armado del rutero
# ---------------------------------------------------------------------------
def build_rutero(
    geo_df: pd.DataFrame,
    freq_df: pd.DataFrame | None = None,
    dias: int = 5,
    lunes_ligero: bool = True,
) -> pd.DataFrame:
    """
    Construye el rutero a partir de:
      - geo_df: clientes con lat, lon, ventas_periodo (de build_geo_dataframe).
      - freq_df: frecuencia de visita (de compute_visit_frequency), con
                 partner_id y dias_entre_visitas_prom.

    Devuelve un DataFrame con una fila por cliente:
      partner_id, partner_name, city, lat, lon, ventas_periodo,
      cadencia_dias, frecuencia, semanas, dia, orden.
    """
    cols = [
        "partner_id", "partner_name", "city", "lat", "lon",
        "ventas_periodo", "cadencia_dias", "frecuencia", "semanas",
        "dia", "orden",
    ]
    if geo_df is None or geo_df.empty:
        return pd.DataFrame(columns=cols)

    df = geo_df.copy()
    df = df[df["lat"].notna() & df["lon"].notna()].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=cols)

    # Cadencia histórica
    if freq_df is not None and not freq_df.empty and "partner_id" in freq_df.columns:
        cad = (
            freq_df[["partner_id", "dias_entre_visitas_prom"]]
            .drop_duplicates("partner_id")
            .rename(columns={"dias_entre_visitas_prom": "cadencia_dias"})
        )
        df = df.merge(cad, on="partner_id", how="left")
    else:
        df["cadencia_dias"] = np.nan

    frec = df["cadencia_dias"].apply(recomendar_frecuencia)
    df["frecuencia"] = [f[0] for f in frec]
    df["semanas"] = [",".join(str(s) for s in f[1]) for f in frec]

    # Zonificación geográfica -> día (Lun-Vie)
    lat = df["lat"].to_numpy(dtype=float)
    lon = df["lon"].to_numpy(dtype=float)
    k = max(1, min(dias, len(df)))
    labels = kmeans_geo(lat, lon, k)

    # Asignación de clusters a días. Base: oeste→este por longitud del
    # centroide, para que los días progresen geográficamente.
    ventas_arr = (
        df["ventas_periodo"].to_numpy(dtype=float)
        if "ventas_periodo" in df.columns else np.zeros(len(df))
    )
    info = []
    for j in range(k):
        m = labels == j
        info.append({
            "cluster": j,
            "lon": float(lon[m].mean()) if m.any() else 0.0,
            "ventas": float(ventas_arr[m].sum()) if m.any() else 0.0,
        })
    por_lon = sorted(info, key=lambda x: x["lon"])
    if lunes_ligero and len(info) >= 2:
        # En Colombia muchos festivos caen en lunes (Ley Emiliani), así que la
        # zona de MENOR venta se pone el lunes: si el lunes es festivo, se
        # arriesga la menor cantidad de venta. El resto queda oeste→este.
        menor = min(info, key=lambda x: x["ventas"])
        resto = [c for c in por_lon if c["cluster"] != menor["cluster"]]
        orden_final = [menor] + resto
    else:
        orden_final = por_lon
    cluster_a_dia = {c["cluster"]: DIAS[i % len(DIAS)]
                     for i, c in enumerate(orden_final)}
    df["dia"] = [cluster_a_dia[l] for l in labels]

    # Orden dentro de cada día por vecino más cercano (arrancando por el
    # cliente de mayor venta, un buen ancla comercial).
    df["orden"] = 0
    for dia in df["dia"].unique():
        sub = df[df["dia"] == dia]
        if sub.empty:
            continue
        slat = sub["lat"].to_numpy(dtype=float)
        slon = sub["lon"].to_numpy(dtype=float)
        start = int(np.argmax(sub["ventas_periodo"].to_numpy(dtype=float))) \
            if "ventas_periodo" in sub.columns else 0
        orden_local = order_nearest_neighbor(slat, slon, start=start)
        # orden_local: posiciones dentro de `sub` en el orden de visita
        pos_to_rank = {pos: rank + 1 for rank, pos in enumerate(orden_local)}
        idx = sub.index.to_list()
        for pos, i in enumerate(idx):
            df.loc[i, "orden"] = pos_to_rank.get(pos, pos + 1)

    if "ventas_periodo" not in df.columns:
        df["ventas_periodo"] = 0.0
    for c in cols:
        if c not in df.columns:
            df[c] = None

    # Ordenar salida por día (Lun-Vie) y orden de visita
    df["_dia_idx"] = df["dia"].map({d: i for i, d in enumerate(DIAS)}).fillna(99)
    df = df.sort_values(["_dia_idx", "orden"]).reset_index(drop=True)
    return df[cols]


def resumen_por_dia(rutero: pd.DataFrame) -> pd.DataFrame:
    """Resumen: por día, # clientes, ventas del período y km estimados."""
    if rutero is None or rutero.empty:
        return pd.DataFrame(columns=["dia", "n_clientes", "ventas_periodo", "km_ruta"])
    filas = []
    for dia in DIAS:
        sub = rutero[rutero["dia"] == dia].sort_values("orden")
        if sub.empty:
            continue
        lat = sub["lat"].to_numpy(dtype=float)
        lon = sub["lon"].to_numpy(dtype=float)
        km = 0.0
        for i in range(1, len(sub)):
            km += haversine(lat[i - 1], lon[i - 1], lat[i], lon[i])
        filas.append({
            "dia": dia,
            "n_clientes": int(len(sub)),
            "ventas_periodo": float(sub["ventas_periodo"].sum()),
            "km_ruta": round(km, 1),
        })
    return pd.DataFrame(filas)
