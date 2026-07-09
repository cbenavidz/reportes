# -*- coding: utf-8 -*-
"""
Página: Rutero — rebalanceo sobre los datos reales de `sales_route_mobile`.

Universo: los clientes ACTIVOS EN RUTA en la app móvil (`sr_active_in_route`).

  - Cada vendedor CONSERVA sus clientes (no se reasignan entre vendedores).
  - Los clientes activos sin rutero ("huérfanos") se asignan al vendedor cuyo
    territorio les quede más cerca.
  - Los 5 días de cada vendedor se rebalancean por CARGA DE VISITAS/MES
    (derivada de las facturas por mes y las ventas), manteniendo compactas las
    zonas geográficas.
  - La secuencia de cada día se optimiza por vecino más cercano.

SOLO LECTURA: la propuesta se entrega en Excel para cargarla manualmente en
Odoo. Los clientes sin GPS quedan fuera del plan, en una lista aparte.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import load_invoice_lines, load_route_partners, load_sr_routes
from src import route_module as rm
from src import rutero_optimizer as ro
from src.rutero_planner import DIAS
from src.ui_components import render_sidebar_filters

st.set_page_config(page_title="Rutero | Cartera", page_icon="🧭", layout="wide")

require_auth()
logout_button()

st.title("🧭 Rutero — rebalanceo")
st.caption(
    "Universo: clientes **activos en ruta** en la app. Cada vendedor conserva "
    "sus clientes; se rebalancean sus 5 días por **carga de visitas/mes** "
    "(según facturas/mes y ventas) manteniendo zonas compactas. La zona de "
    "menor venta va el lunes, por los festivos."
)


def fmt_money(x) -> str:
    try:
        return f"${x:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0"


filters = render_sidebar_filters()
company_ids = filters["company_ids"]
hoy = date.today()
desde = hoy - timedelta(days=365)

# ── Carga ──
routes = load_sr_routes()
if routes is None or routes.empty:
    st.error(
        "No se pudieron leer los ruteros (`sr.route`). Verifica los permisos "
        "del usuario de API sobre el módulo de Ventas en Ruta."
    )
    st.stop()

# Universo = activos en ruta. Sin filtrar por empresa: los contactos suelen ser
# compartidos entre compañías y el filtro dejaría fuera a la mayoría.
partners = load_route_partners(solo_activos=True)
lines = load_invoice_lines(
    company_ids=tuple(company_ids) if company_ids else None,
    date_from=desde.isoformat(), date_to=hoy.isoformat(),
)


def _dia_de_ruta(r) -> str:
    for c in rm.DAY_COLS:
        if r.get(c):
            return rm.DAY_LABELS[c[4:]]
    return "—"


routes = routes.copy()
routes["dia"] = routes.apply(_dia_de_ruta, axis=1)
ruta_por_dia = {(str(r["user_name"]), str(r["dia"])): int(r["id"])
                for _, r in routes.iterrows()}
dia_de_route_id = dict(zip(routes["id"].astype(int), routes["dia"]))
vend_de_route_id = dict(zip(routes["id"].astype(int), routes["user_name"]))
vend_validos = set(routes["user_name"].dropna().astype(str))

with st.expander("📋 Ruteros actuales en Odoo", expanded=False):
    st.dataframe(
        routes[["name", "user_name", "dia", "partner_count"]],
        use_container_width=True, hide_index=True,
        column_config={"name": "Rutero", "user_name": "Vendedor",
                       "dia": "Día", "partner_count": "# Clientes"},
    )

# ── Universo ──
p = partners.copy()
for c in ["sr_active_in_route", "sr_has_geo"]:
    if c not in p.columns:
        p[c] = False
    p[c] = p[c].fillna(False).astype(bool)

activos = p[p["sr_active_in_route"]].copy()
if activos.empty:
    st.warning("No hay clientes marcados como «Activo en ruta» en Odoo.")
    st.stop()

en_ruta = activos[activos["sr_has_geo"]].copy()
sin_gps = activos[~activos["sr_has_geo"]].copy()
if en_ruta.empty:
    st.warning(f"Los {len(activos):,} clientes activos no tienen coordenadas GPS.")
    st.stop()

en_ruta["lat"] = pd.to_numeric(en_ruta["partner_latitude"], errors="coerce")
en_ruta["lon"] = pd.to_numeric(en_ruta["partner_longitude"], errors="coerce")
en_ruta = en_ruta.dropna(subset=["lat", "lon"])

# ── Frecuencia sugerida (ventas + facturas/mes) ──
# Se calcula ANTES de repartir huérfanos, porque el reparto equilibra por la
# carga de visitas/mes, que se deriva de la frecuencia.
met = ro.metricas_clientes(lines, meses=12)
sug = ro.sugerir_frecuencias(met)
en_ruta = en_ruta.merge(sug, left_on="id", right_on="partner_id", how="left")
en_ruta["ventas"] = en_ruta["ventas"].fillna(0.0)
en_ruta["n_facturas"] = en_ruta["n_facturas"].fillna(0).astype(int)
en_ruta["facturas_mes"] = en_ruta["facturas_mes"].fillna(0.0)
en_ruta["frecuencia_code"] = en_ruta["frecuencia_code"].fillna("on_demand")
en_ruta["frecuencia"] = en_ruta["frecuencia"].fillna("Bajo demanda")
en_ruta["semanas"] = en_ruta["semanas"].fillna("1")
en_ruta["partner_id"] = en_ruta["id"]

# Vendedor actual: por su rutero; si no, por el comercial asignado (solo si
# ese comercial tiene ruteros). El resto son "huérfanos".
en_ruta["route_id_num"] = pd.to_numeric(en_ruta["sr_route_id"], errors="coerce")
en_ruta["vendedor"] = en_ruta["route_id_num"].map(vend_de_route_id)
en_ruta["dia_actual"] = en_ruta["route_id_num"].map(dia_de_route_id).fillna("—")
_fb = en_ruta["user_name"].where(en_ruta["user_name"].astype(str).isin(vend_validos))
en_ruta["vendedor"] = en_ruta["vendedor"].fillna(_fb)

con_vend = en_ruta[en_ruta["vendedor"].notna()].copy()
huerfanos = en_ruta[en_ruta["vendedor"].isna()].copy()
n_huerfanos = len(huerfanos)
huerf_detalle = pd.DataFrame()

if n_huerfanos:
    st.markdown("#### 🏙️ Reparto de clientes sin rutero")
    st.caption(
        f"Hay **{n_huerfanos}** clientes activos sin rutero. Se asignan por "
        "**ciudad**; los que no tienen ciudad registrada se resuelven por "
        "cercanía GPS a esas cabeceras."
    )
    opciones = sorted(vend_validos)

    def _default(patrones: list[str]) -> int:
        for i, v in enumerate(opciones):
            if any(p in v.lower() for p in patrones):
                return i
        return 0

    ca, cb = st.columns(2)
    with ca:
        v_quibdo = st.selectbox("Quibdó y alrededores →", opciones,
                                index=_default(["vanessa", "yarley"]))
    with cb:
        v_istmina = st.selectbox("Istmina y alrededores →", opciones,
                                 index=_default(["felipe"]))

    anclas = [
        {"ciudad": "Quibdó", "lat": ro.CIUDAD_COORDS["QUIBDO"][0],
         "lon": ro.CIUDAD_COORDS["QUIBDO"][1], "vendedor": v_quibdo},
        {"ciudad": "Istmina", "lat": ro.CIUDAD_COORDS["ISTMINA"][0],
         "lon": ro.CIUDAD_COORDS["ISTMINA"][1], "vendedor": v_istmina},
    ]
    huerfanos = ro.asignar_huerfanos_por_ciudad(huerfanos, anclas)
    huerf_detalle = huerfanos[["name", "city", "vendedor", "asignado_por"]].copy()

    rep = huerfanos["vendedor"].value_counts()
    st.write(" · ".join(f"**{v}**: {n} clientes" for v, n in rep.items()))
    with st.expander("Ver el detalle del reparto y por qué"):
        st.dataframe(
            huerf_detalle.rename(columns={
                "name": "Cliente", "city": "Ciudad",
                "vendedor": "Vendedor asignado", "asignado_por": "Criterio"}),
            use_container_width=True, hide_index=True,
        )
    en_ruta = pd.concat([con_vend, huerfanos], ignore_index=True)
else:
    en_ruta = con_vend

st.divider()
m = st.columns(5)
m[0].metric("Activos en ruta", f"{len(activos):,}")
m[1].metric("En el plan (con GPS)", f"{len(en_ruta):,}")
m[2].metric("Sin GPS (fuera)", f"{len(sin_gps):,}")
m[3].metric("Sin rutero (repartidos)", f"{n_huerfanos:,}")
m[4].metric("Ventas 12m", fmt_money(en_ruta["ventas"].sum()))

st.divider()
tol = st.slider(
    "Prioridad del rebalanceo", 0.05, 0.60, 0.15, step=0.05,
    help="Bajo = carga muy pareja entre días (pero más kilómetros). "
         "Alto = rutas más compactas (pero días más desiguales).",
    format="%.2f",
)
st.caption(
    "⬅️ Más balance de carga · Más compacidad geográfica ➡️ &nbsp;&nbsp; "
    "Revisa la tabla de carga y los km por día antes de decidir."
)

# ── Rebalanceo por vendedor ──
vendedores = sorted(en_ruta["vendedor"].dropna().unique().tolist())
propuestas: dict[str, pd.DataFrame] = {}
tabs = st.tabs(vendedores)

for nombre, tab in zip(vendedores, tabs):
    with tab:
        sub = en_ruta[en_ruta["vendedor"] == nombre].copy()
        base = sub[["partner_id", "name", "city", "lat", "lon", "ventas",
                    "n_facturas", "facturas_mes", "frecuencia_code",
                    "frecuencia", "semanas", "dia_actual", "sr_route_sequence"]]
        reb = ro.rebalancear(base, dias=5, tol=float(tol))
        if reb.empty:
            st.info(f"{nombre} no tiene clientes con coordenadas.")
            continue
        reb["route_id_propuesto"] = reb["dia"].map(
            lambda d: ruta_por_dia.get((nombre, d))
        )
        propuestas[nombre] = reb

        res = ro.resumen_carga(reb)
        c = st.columns(4)
        c[0].metric("Clientes", f"{len(reb):,}")
        c[1].metric("Carga total (visitas/mes)", f"{reb['carga'].sum():.0f}")
        c[2].metric("Km/semana", f"{res['km_ruta'].sum():.0f}")
        c[3].metric("Cambian de día", f"{int((reb['dia'] != reb['dia_actual']).sum()):,}")

        st.markdown("#### ⚖️ Carga por día (propuesta)")
        desv = res["carga_visitas_mes"].std()
        st.caption(
            f"Objetivo por día: **{reb['carga'].sum() / 5:.1f}** visitas/mes · "
            f"desviación lograda: **{desv:.2f}**"
        )
        st.dataframe(
            res, use_container_width=True, hide_index=True,
            column_config={
                "dia": "Día", "n_clientes": "# Clientes",
                "carga_visitas_mes": st.column_config.NumberColumn("Carga (visitas/mes)", format="%.1f"),
                "ventas": st.column_config.NumberColumn("Ventas 12m", format="localized"),
                "km_ruta": st.column_config.NumberColumn("Km", format="%.1f"),
            },
        )

        st.plotly_chart(
            px.scatter_mapbox(
                reb, lat="lat", lon="lon", color="dia",
                category_orders={"dia": DIAS}, hover_name="name",
                hover_data={"secuencia": True, "frecuencia": True,
                            "carga": True, "ventas": ":,.0f",
                            "dia_actual": True, "lat": False, "lon": False,
                            "dia": False},
                zoom=8, height=520,
            ).update_layout(mapbox_style="open-street-map",
                            margin=dict(l=0, r=0, t=0, b=0), legend_title="Día"),
            use_container_width=True,
        )

        st.markdown("#### 🔄 Actual vs propuesto")
        st.dataframe(
            reb[["name", "city", "dia_actual", "dia", "sr_route_sequence",
                 "secuencia", "frecuencia", "carga", "facturas_mes", "ventas"]],
            use_container_width=True, hide_index=True,
            column_config={
                "name": "Cliente", "city": "Ciudad",
                "dia_actual": "Día actual", "dia": "Día propuesto",
                "sr_route_sequence": "Sec. actual", "secuencia": "Sec. propuesta",
                "frecuencia": "Frecuencia", "carga": st.column_config.NumberColumn("Visitas/mes", format="%.1f"),
                "facturas_mes": st.column_config.NumberColumn("Fact./mes", format="%.2f"),
                "ventas": st.column_config.NumberColumn("Ventas 12m", format="localized"),
            },
        )

# ── Sin GPS ──
if not sin_gps.empty:
    st.divider()
    st.markdown("### 📍 Activos sin GPS (fuera del plan)")
    st.caption("Los agregas tú manualmente; no entran a la optimización.")
    cols = [c for c in ["name", "city", "user_name"] if c in sin_gps.columns]
    st.dataframe(
        sin_gps[cols].rename(columns={"name": "Cliente", "city": "Ciudad",
                                      "user_name": "Vendedor"}),
        use_container_width=True, hide_index=True,
    )

# ── Excel ──
if propuestas:
    st.divider()
    st.markdown("### 📥 Descargar la propuesta")
    imp_rows = []
    for nombre, reb in propuestas.items():
        for _, r in reb.iterrows():
            imp_rows.append({
                ".id": int(r["partner_id"]),
                "sr_route_sequence": int(r["secuencia"]),
                "sr_route_id/.id": (int(r["route_id_propuesto"])
                                    if pd.notna(r["route_id_propuesto"]) else ""),
                "sr_visit_frequency": r["frecuencia_code"],
                "Cliente (referencia)": r["name"],
                "Vendedor (referencia)": nombre,
                "Día (referencia)": r["dia"],
            })

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        for nombre, reb in propuestas.items():
            base_n = "".join(ch for ch in nombre if ch.isalnum() or ch == " ")[:20].strip()
            reb[["dia", "secuencia", "name", "city", "frecuencia", "carga",
                 "ventas", "facturas_mes", "dia_actual", "sr_route_sequence",
                 "lat", "lon"]].rename(
                columns={"dia": "Día propuesto", "secuencia": "Secuencia",
                         "name": "Cliente", "city": "Ciudad",
                         "frecuencia": "Frecuencia", "carga": "Visitas/mes",
                         "ventas": "Ventas 12m", "facturas_mes": "Fact./mes",
                         "dia_actual": "Día actual",
                         "sr_route_sequence": "Sec. actual",
                         "lat": "Lat", "lon": "Lon"}
            ).to_excel(xw, sheet_name=f"Rutero {base_n}"[:31], index=False)
            ro.resumen_carga(reb).to_excel(
                xw, sheet_name=f"Carga {base_n}"[:31], index=False)
        pd.DataFrame(imp_rows).to_excel(xw, sheet_name="Importar Odoo", index=False)
        if not sin_gps.empty:
            sin_gps[[c for c in ["name", "city", "user_name"] if c in sin_gps.columns]] \
                .rename(columns={"name": "Cliente", "city": "Ciudad",
                                 "user_name": "Vendedor"}) \
                .to_excel(xw, sheet_name="Sin GPS", index=False)

    st.download_button(
        "⬇️ Descargar rebalanceo en Excel", buf.getvalue(),
        "rutero_rebalanceo.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

    with st.expander("ℹ️ Cómo cargarlo en Odoo"):
        st.markdown(
            "1. En la hoja **«Importar Odoo»**, borra las columnas de referencia "
            "(Cliente / Vendedor / Día); son solo para que revises.\n"
            "2. En Odoo: **Contactos → vista Lista → Favoritos → Importar registros**.\n"
            "3. Mapea las columnas así:\n"
            "   - `.id` → **Database ID** (actualiza en vez de crear).\n"
            "   - `sr_route_sequence` → *Secuencia en rutero*.\n"
            "   - `sr_route_id/.id` → *Rutero principal* (por Database ID).\n"
            "   - `sr_visit_frequency` → *Frecuencia de visita*.\n"
            "4. Usa **Probar** antes de **Importar**.\n\n"
            "Si solo quieres cambiar el orden y no mover a nadie de día, borra "
            "la columna `sr_route_id/.id`."
        )
