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
# 2b) Carga de visitas: cuánto "pesa" cada cliente al mes
# ---------------------------------------------------------------------------
VISITAS_MES = {"weekly": 4.0, "biweekly": 2.0, "monthly": 1.0, "on_demand": 0.5}


def visitas_mes(code) -> float:
    """Visitas al mes que exige un cliente según su frecuencia."""
    return VISITAS_MES.get(str(code), 1.0)


# ---------------------------------------------------------------------------
# 2c-bis) Asignación de huérfanos por CIUDAD (con respaldo por cercanía GPS)
# ---------------------------------------------------------------------------
import unicodedata  # noqa: E402

# Coordenadas de referencia de las cabeceras (Chocó)
CIUDAD_COORDS = {
    "QUIBDO": (5.6947, -76.6611),
    "ISTMINA": (5.1489, -76.6847),
}


def _norm_ciudad(valor) -> str:
    """Normaliza el nombre de ciudad: sin tildes, mayúsculas, sin espacios."""
    if valor is None or valor is False:
        return ""
    if isinstance(valor, float) and np.isnan(valor):
        return ""
    t = unicodedata.normalize("NFKD", str(valor))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return t.strip().upper()


def asignar_huerfanos_por_ciudad(
    huerfanos: pd.DataFrame, anclas: list[dict],
) -> pd.DataFrame:
    """
    Asigna vendedor a los clientes activos sin rutero según su CIUDAD.

    `anclas`: [{"ciudad": "Quibdó", "lat": .., "lon": .., "vendedor": ".."}, ...]

    Regla: si la ciudad del cliente coincide con la de un ancla, va a ese
    vendedor. Si el cliente no tiene ciudad (frecuente en Odoo) o no coincide
    con ninguna, se asigna al ancla geográficamente más cercana por GPS.
    Devuelve además `asignado_por`, para poder auditar cada decisión.
    """
    if huerfanos is None or huerfanos.empty or not anclas:
        return huerfanos
    out = huerfanos.copy()
    asign, motivo = [], []
    for _, r in out.iterrows():
        c = _norm_ciudad(r.get("city"))
        v, m = None, ""
        for a in anclas:
            an = _norm_ciudad(a["ciudad"])
            if an and an in c:
                v, m = a["vendedor"], f"ciudad: {a['ciudad']}"
                break
        if v is None:
            best = np.inf
            for a in anclas:
                d = haversine(float(r["lat"]), float(r["lon"]),
                              float(a["lat"]), float(a["lon"]))
                if d < best:
                    best, v = d, a["vendedor"]
                    m = f"cercanía a {a['ciudad']} ({d:.0f} km)"
        asign.append(v)
        motivo.append(m)
    out["vendedor"] = asign
    out["asignado_por"] = motivo
    return out


# ---------------------------------------------------------------------------
# 2c) Asignación de huérfanos (activos sin rutero) al vendedor más cercano
# ---------------------------------------------------------------------------
def asignar_huerfanos(
    con_vendedor: pd.DataFrame,
    huerfanos: pd.DataFrame,
    balancear: bool = True,
) -> pd.DataFrame:
    """
    Asigna vendedor a los clientes activos SIN rutero ("huérfanos").

    Fase 1: cada huérfano va al vendedor cuyo cliente más cercano esté a menor
    distancia (criterio de territorio, no de centroide).

    Fase 2 (si `balancear`): como los huérfanos no tienen dueño, se reparten
    buscando igualar la CARGA (visitas/mes) entre vendedores. Se mueven solo
    huérfanos —nunca clientes ya asignados— empezando por aquellos a los que
    el cambio les cuesta menos distancia.
    """
    if huerfanos is None or huerfanos.empty:
        return huerfanos
    out = huerfanos.copy()
    if con_vendedor is None or con_vendedor.empty:
        out["vendedor"] = None
        return out

    vends = sorted(con_vendedor["vendedor"].dropna().unique().tolist())
    coords = {
        v: con_vendedor.loc[con_vendedor["vendedor"] == v, ["lat", "lon"]]
        .to_numpy(dtype=float)
        for v in vends
    }

    # Distancia de cada huérfano a cada vendedor (al cliente más cercano)
    dist = {v: [] for v in vends}
    for _, r in out.iterrows():
        for v in vends:
            pts = coords[v]
            dist[v].append(
                min(haversine(r["lat"], r["lon"], p[0], p[1]) for p in pts)
                if len(pts) else np.inf
            )
    D = pd.DataFrame(dist, index=out.index)
    out["vendedor"] = D.idxmin(axis=1)

    if not balancear or len(vends) < 2:
        return out

    # Carga de cada cliente (visitas/mes)
    def _carga(df):
        if "frecuencia_code" in df.columns:
            return df["frecuencia_code"].apply(visitas_mes)
        return pd.Series(1.0, index=df.index)

    carga_base = {
        v: float(_carga(con_vendedor[con_vendedor["vendedor"] == v]).sum())
        for v in vends
    }
    out["_carga"] = _carga(out)

    def _totales():
        t = {v: carga_base.get(v, 0.0) for v in vends}
        for v in vends:
            t[v] += float(out.loc[out["vendedor"] == v, "_carga"].sum())
        return t

    objetivo = sum(_totales().values()) / len(vends)
    for _ in range(500):
        tot = _totales()
        vmax = max(tot, key=tot.get)
        vmin = min(tot, key=tot.get)
        if tot[vmax] - tot[vmin] <= 0.05 * objetivo:
            break
        cand = out[out["vendedor"] == vmax]
        if cand.empty:
            break
        # el huérfano al que menos le cuesta cambiarse (menor km extra)
        costo = (D.loc[cand.index, vmin] - D.loc[cand.index, vmax]).sort_values()
        movido = False
        for i in costo.index:
            w = float(out.at[i, "_carga"])
            if tot[vmin] + w <= tot[vmax]:
                out.at[i, "vendedor"] = vmin
                movido = True
                break
        if not movido:
            break

    return out.drop(columns=["_carga"])


# ---------------------------------------------------------------------------
# 2d) K-means con restricción de capacidad (balancea la carga por día)
# ---------------------------------------------------------------------------
def balanced_kmeans(
    lat: np.ndarray, lon: np.ndarray, peso: np.ndarray,
    k: int, iters: int = 30, tol: float = 0.15, seed: int = 42,
) -> np.ndarray:
    """
    Agrupa en `k` zonas geográficas equilibrando la suma de `peso` por zona.

    Arranca de un k-means normal y luego reasigna los puntos por cercanía,
    respetando una capacidad máxima por zona (media × (1+tol)). Los puntos que
    no caben van a la zona menos cargada.
    """
    n = len(lat)
    if n == 0:
        return np.array([], dtype=int)
    k = max(1, min(k, n))
    pts = np.column_stack([lat, lon]).astype(float)
    peso = np.asarray(peso, dtype=float)
    if peso.sum() <= 0:
        peso = np.ones(n)

    labels = kmeans_geo(lat, lon, k, seed=seed)
    objetivo = peso.sum() / k
    cap = objetivo * (1.0 + tol)
    rng = np.random.default_rng(seed)

    for _ in range(iters):
        cents = np.zeros((k, 2))
        for j in range(k):
            m = labels == j
            cents[j] = pts[m].mean(axis=0) if m.any() else pts[rng.integers(n)]

        d = np.sqrt(((pts[:, None, :] - cents[None, :, :]) ** 2).sum(axis=2))
        pares = sorted(
            ((d[i, j], i, j) for i in range(n) for j in range(k)),
            key=lambda t: t[0],
        )
        nuevo = np.full(n, -1, dtype=int)
        cargas = np.zeros(k)
        for _dist, i, j in pares:
            if nuevo[i] != -1:
                continue
            if cargas[j] + peso[i] <= cap:
                nuevo[i] = j
                cargas[j] += peso[i]
        for i in range(n):  # sobrantes → zona menos cargada
            if nuevo[i] == -1:
                j = int(np.argmin(cargas))
                nuevo[i] = j
                cargas[j] += peso[i]

        # --- Reparación: nivelar el día más cargado contra el más liviano ---
        # El reparto voraz llena las primeras zonas hasta el tope y deja las
        # sobras en la última. Movemos, de la zona más cargada a la más
        # liviana, el cliente geográficamente más cercano a esta última,
        # siempre que el movimiento no invierta el desbalance.
        for _rep in range(500):
            cargas = np.array([peso[nuevo == j].sum() for j in range(k)])
            jmax, jmin = int(np.argmax(cargas)), int(np.argmin(cargas))
            if cargas[jmax] - cargas[jmin] <= tol * objetivo:
                break
            m_min = nuevo == jmin
            c_min = pts[m_min].mean(axis=0) if m_min.any() else pts.mean(axis=0)
            idx = np.where(nuevo == jmax)[0]
            if len(idx) <= 1:
                break
            dist_min = np.sqrt(((pts[idx] - c_min) ** 2).sum(axis=1))
            movido = False
            for i in idx[np.argsort(dist_min)]:
                if cargas[jmin] + peso[i] <= cargas[jmax]:
                    nuevo[i] = jmin
                    movido = True
                    break
            if not movido:
                break

        if np.array_equal(nuevo, labels):
            break
        labels = nuevo
    return labels


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


def rebalancear(
    clientes: pd.DataFrame,
    dias: int = 5,
    lunes_ligero: bool = True,
    tol: float = 0.15,
) -> pd.DataFrame:
    """
    Reparte los clientes de UN vendedor en `dias` días, equilibrando la CARGA
    DE VISITAS (visitas/mes) y manteniendo compactas las zonas geográficas.

    `clientes` requiere: partner_id, lat, lon, ventas, frecuencia_code.
    Devuelve el DF con `carga` (visitas/mes), `dia` y `secuencia`.
    """
    if clientes is None or clientes.empty:
        return pd.DataFrame(columns=list(clientes.columns if clientes is not None else [])
                            + ["carga", "dia", "secuencia"])
    df = clientes.copy()
    df = df[df["lat"].notna() & df["lon"].notna()].reset_index(drop=True)
    if df.empty:
        return df.assign(carga=0.0, dia=None, secuencia=None)

    df["carga"] = df["frecuencia_code"].apply(visitas_mes)
    lat = df["lat"].to_numpy(dtype=float)
    lon = df["lon"].to_numpy(dtype=float)
    peso = df["carga"].to_numpy(dtype=float)
    ventas = (df["ventas"].to_numpy(dtype=float)
              if "ventas" in df.columns else np.zeros(len(df)))

    k = max(1, min(dias, len(df)))
    labels = balanced_kmeans(lat, lon, peso, k, tol=tol)

    info = []
    for j in range(k):
        m = labels == j
        info.append({
            "cluster": j,
            "lon": float(lon[m].mean()) if m.any() else 0.0,
            "ventas": float(ventas[m].sum()) if m.any() else 0.0,
            "carga": float(peso[m].sum()) if m.any() else 0.0,
        })
    por_lon = sorted(info, key=lambda x: x["lon"])
    if lunes_ligero and len(info) >= 2:
        # El lunes recibe la zona de menor VENTA: si cae festivo (muy común en
        # Colombia), se arriesga la menor cantidad de negocio.
        menor = min(info, key=lambda x: x["ventas"])
        resto = [c for c in por_lon if c["cluster"] != menor["cluster"]]
        orden = [menor] + resto
    else:
        orden = por_lon
    cluster_a_dia = {c["cluster"]: DIAS[i % len(DIAS)] for i, c in enumerate(orden)}
    df["dia"] = [cluster_a_dia[l] for l in labels]

    # Secuencia por vecino más cercano, arrancando por el mayor venta.
    df["secuencia"] = 0
    for dia in df["dia"].unique():
        sub = df[df["dia"] == dia]
        slat = sub["lat"].to_numpy(dtype=float)
        slon = sub["lon"].to_numpy(dtype=float)
        start = int(np.argmax(sub["ventas"].to_numpy(dtype=float))) if len(sub) else 0
        orden_local = order_nearest_neighbor(slat, slon, start=start)
        rank = {pos: r for r, pos in enumerate(orden_local)}
        for pos, i in enumerate(sub.index.to_list()):
            df.loc[i, "secuencia"] = (rank.get(pos, pos) + 1) * 10

    df["_d"] = df["dia"].map({d: i for i, d in enumerate(DIAS)}).fillna(99)
    return df.sort_values(["_d", "secuencia"]).drop(columns=["_d"]).reset_index(drop=True)


def asignar_a_ruteros(
    nuevos: pd.DataFrame,
    rutas: pd.DataFrame,
    factor_cercania: float = 1.5,
) -> pd.DataFrame:
    """
    Asigna clientes NUEVOS a ruteros existentes, combinando cercanía y carga.

    `nuevos`: df con al menos lat, lon (y opcional `carga`).
    `rutas` : df con route_id, route_name, lat_c, lon_c, carga_actual.

    Para cada cliente: entre las rutas cuyo centroide está a <=
    `factor_cercania` × la distancia mínima, elige la de MENOR carga acumulada.
    Devuelve `nuevos` con route_id_sugerido, route_name_sugerido, dist_km.
    """
    out = nuevos.copy()
    if out.empty or rutas is None or rutas.empty:
        out["route_id_sugerido"] = None
        out["route_name_sugerido"] = None
        out["dist_km"] = np.nan
        return out

    cargas = {int(r["route_id"]): float(r.get("carga_actual", 0) or 0)
              for _, r in rutas.iterrows()}
    rid_name = {int(r["route_id"]): r["route_name"] for _, r in rutas.iterrows()}
    rlat = {int(r["route_id"]): float(r["lat_c"]) for _, r in rutas.iterrows()}
    rlon = {int(r["route_id"]): float(r["lon_c"]) for _, r in rutas.iterrows()}
    ids = list(cargas.keys())

    sug_id, sug_name, sug_dist = [], [], []
    # Orden estable: primero los de mayor carga propia (para colocarlos donde
    # aún hay espacio), luego el resto.
    orden = out.assign(_c=out.get("carga", 1.0)).sort_values(
        "_c", ascending=False).index
    asign_por_idx: dict = {}
    for i in orden:
        r = out.loc[i]
        dists = {rid: haversine(float(r["lat"]), float(r["lon"]),
                                rlat[rid], rlon[rid]) for rid in ids}
        dmin = min(dists.values())
        candidatos = [rid for rid in ids
                      if dists[rid] <= max(dmin * factor_cercania, dmin + 0.5)]
        elegido = min(candidatos, key=lambda rid: (cargas[rid], dists[rid]))
        cargas[elegido] += float(r.get("carga", 1.0) or 1.0)
        asign_por_idx[i] = (elegido, dists[elegido])

    for i in out.index:
        rid, d = asign_por_idx.get(i, (None, np.nan))
        sug_id.append(rid)
        sug_name.append(rid_name.get(rid) if rid else None)
        sug_dist.append(round(d, 1) if rid else np.nan)
    out["route_id_sugerido"] = sug_id
    out["route_name_sugerido"] = sug_name
    out["dist_km"] = sug_dist
    return out


def resumen_carga(
    rutero: pd.DataFrame,
    min_por_visita: float = 20.0,
    vel_kmh: float = 40.0,
) -> pd.DataFrame:
    """
    Por día: clientes, carga (visitas/mes), ventas, km y tiempo estimado.

    El tiempo es el que realmente limita a un vendedor puerta a puerta:
        horas = (n_clientes × min_por_visita + km ÷ vel_kmh × 60) ÷ 60
    """
    cols = ["dia", "n_clientes", "carga_visitas_mes", "ventas", "km_ruta",
            "horas_estimadas"]
    filas = []
    if rutero is None or rutero.empty:
        return pd.DataFrame(columns=cols)
    for dia in DIAS:
        sub = rutero[rutero["dia"] == dia].sort_values("secuencia")
        if sub.empty:
            continue
        lat = sub["lat"].to_numpy(dtype=float)
        lon = sub["lon"].to_numpy(dtype=float)
        km = sum(haversine(lat[i - 1], lon[i - 1], lat[i], lon[i])
                 for i in range(1, len(sub)))
        n = int(len(sub))
        minutos = n * float(min_por_visita) + (km / max(vel_kmh, 1.0)) * 60.0
        filas.append({
            "dia": dia,
            "n_clientes": n,
            "carga_visitas_mes": round(float(sub["carga"].sum()), 1),
            "ventas": float(sub["ventas"].sum()) if "ventas" in sub.columns else 0.0,
            "km_ruta": round(km, 1),
            "horas_estimadas": round(minutos / 60.0, 1),
        })
    return pd.DataFrame(filas, columns=cols)


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
