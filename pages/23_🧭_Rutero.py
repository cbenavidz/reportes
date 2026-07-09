# -*- coding: utf-8 -*-
"""
Página: Rutero (sobre los datos reales del módulo `sales_route_mobile`).

Lee los ruteros reales de Odoo (sr.route) y los clientes activos en ruta con
GPS, calcula la frecuencia sugerida (ventas + facturas/mes), propone una
zonificación en 5 días (Lun-Vie, zona de menor venta el lunes) y una secuencia
optimizada por cercanía.

SOLO LECTURA: cada vendedor conserva sus clientes (no se reasignan entre
vendedores) y la propuesta se entrega en Excel para cargarla manualmente en
Odoo. Los clientes sin GPS quedan fuera del plan, en una lista aparte.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    load_invoice_lines,
    load_route_partners,
    load_sr_routes,
)
from src import route_module as rm
from src import rutero_optimizer as ro
from src.rutero_planner import DIAS
from src.ui_components import render_sidebar_filters

st.set_page_config(page_title="Rutero | Cartera", page_icon="🧭", layout="wide")

require_auth()
logout_button()

st.title("🧭 Rutero")
st.caption(
    "Sobre los datos reales del módulo **Ventas en Ruta** de Odoo. La "
    "**frecuencia** se sugiere según las ventas y las facturas por mes de cada "
    "cliente; el **día** por zona geográfica (la zona de menor venta va el "
    "lunes, por los festivos); y la **secuencia** se optimiza por cercanía."
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
        "No se pudieron leer los ruteros (`sr.route`). Verifica que el usuario "
        "de API tenga permiso de lectura sobre el módulo de Ventas en Ruta."
    )
    st.stop()

partners = load_route_partners(
    company_ids=tuple(company_ids) if company_ids else None,
)
lines = load_invoice_lines(
    company_ids=tuple(company_ids) if company_ids else None,
    date_from=desde.isoformat(), date_to=hoy.isoformat(),
)

# Día único por rutero (cada sr.route tiene un solo día activo)
def _dia_de_ruta(r) -> str:
    for c in rm.DAY_COLS:
        if r.get(c):
            return rm.DAY_LABELS[c[4:]]
    return "—"


routes = routes.copy()
routes["dia"] = routes.apply(_dia_de_ruta, axis=1)
# (vendedor, día) -> route_id, para poder reasignar el rutero al escribir
ruta_por_dia = {
    (str(r["user_name"]), str(r["dia"])): int(r["id"])
    for _, r in routes.iterrows()
}
dia_de_route_id = dict(zip(routes["id"].astype(int), routes["dia"]))
vendedor_de_route_id = dict(zip(routes["id"].astype(int), routes["user_name"]))

with st.expander("📋 Ruteros configurados en Odoo", expanded=False):
    st.dataframe(
        routes[["name", "user_name", "dia", "partner_count"]],
        use_container_width=True, hide_index=True,
        column_config={"name": "Rutero", "user_name": "Vendedor",
                       "dia": "Día", "partner_count": "# Clientes"},
    )

# ── Universo de ruta: activos con GPS ──
p = partners.copy()
for c in ["sr_active_in_route", "sr_has_geo"]:
    if c not in p.columns:
        p[c] = False
    p[c] = p[c].fillna(False).astype(bool)

activos = p[p["sr_active_in_route"]]
en_ruta = activos[activos["sr_has_geo"]].copy()
sin_gps = activos[~activos["sr_has_geo"]].copy()

if en_ruta.empty:
    st.warning("No hay clientes activos en ruta con GPS válido.")
    st.stop()

# Vendedor y día actual del cliente (desde su rutero)
en_ruta["route_id_num"] = pd.to_numeric(en_ruta["sr_route_id"], errors="coerce")
en_ruta["vendedor"] = en_ruta["route_id_num"].map(vendedor_de_route_id)
en_ruta["dia_actual"] = en_ruta["route_id_num"].map(dia_de_route_id).fillna("—")
en_ruta["vendedor"] = en_ruta["vendedor"].fillna(en_ruta.get("user_name"))
en_ruta = en_ruta[en_ruta["vendedor"].notna()]

if en_ruta.empty:
    st.warning("Los clientes activos con GPS no tienen rutero ni vendedor asignado.")
    st.stop()

# ── Métricas y frecuencia sugerida (ventas + facturas/mes) ──
met = ro.metricas_clientes(lines, meses=12)
sug = ro.sugerir_frecuencias(met)
en_ruta = en_ruta.merge(sug, left_on="id", right_on="partner_id", how="left")
en_ruta["ventas"] = en_ruta["ventas"].fillna(0.0)
en_ruta["n_facturas"] = en_ruta["n_facturas"].fillna(0).astype(int)
en_ruta["facturas_mes"] = en_ruta["facturas_mes"].fillna(0.0)
en_ruta["frecuencia_code"] = en_ruta["frecuencia_code"].fillna("on_demand")
en_ruta["frecuencia"] = en_ruta["frecuencia"].fillna("Bajo demanda")
en_ruta["semanas"] = en_ruta["semanas"].fillna("1")
en_ruta["lat"] = pd.to_numeric(en_ruta["partner_latitude"], errors="coerce")
en_ruta["lon"] = pd.to_numeric(en_ruta["partner_longitude"], errors="coerce")

m = st.columns(4)
m[0].metric("Clientes en ruta (con GPS)", f"{len(en_ruta):,}")
m[1].metric("Activos sin GPS", f"{len(sin_gps):,}")
m[2].metric("Vendedores", f"{en_ruta['vendedor'].nunique():,}")
m[3].metric("Ventas 12m", fmt_money(en_ruta["ventas"].sum()))

st.divider()

vendedores = sorted(en_ruta["vendedor"].dropna().unique().tolist())
propuestas: dict[str, pd.DataFrame] = {}
tabs = st.tabs(vendedores)

for nombre, tab in zip(vendedores, tabs):
    with tab:
        sub = en_ruta[en_ruta["vendedor"] == nombre].copy()
        base = sub[["id", "name", "city", "lat", "lon", "ventas", "n_facturas",
                    "facturas_mes", "frecuencia_code", "frecuencia", "semanas",
                    "dia_actual", "sr_route_sequence"]].rename(
            columns={"id": "partner_id", "name": "cliente"})
        opt = ro.optimizar_rutero(
            base.rename(columns={"cliente": "partner_name"}), dias=5,
        )
        if opt.empty:
            st.info(f"{nombre} no tiene clientes con coordenadas.")
            continue
        opt = opt.rename(columns={"partner_name": "cliente"})
        # Rutero (sr.route) que corresponde al día propuesto, MISMO vendedor.
        opt["route_id_propuesto"] = opt["dia"].map(
            lambda d: ruta_por_dia.get((nombre, d))
        )
        propuestas[nombre] = opt

        km = ro.km_por_dia(opt)
        c = st.columns(4)
        c[0].metric("Clientes", f"{len(opt):,}")
        c[1].metric("Ventas 12m", fmt_money(opt["ventas"].sum()))
        c[2].metric("Km/semana", f"{km['km_ruta'].sum():.0f}")
        cambian_dia = int((opt["dia"] != opt["dia_actual"]).sum())
        c[3].metric("Cambian de día", f"{cambian_dia:,}")

        st.plotly_chart(
            px.scatter_mapbox(
                opt, lat="lat", lon="lon", color="dia",
                category_orders={"dia": DIAS}, hover_name="cliente",
                hover_data={"secuencia": True, "frecuencia": True,
                            "ventas": ":,.0f", "dia_actual": True,
                            "lat": False, "lon": False, "dia": False},
                zoom=8, height=520,
            ).update_layout(mapbox_style="open-street-map",
                            margin=dict(l=0, r=0, t=0, b=0), legend_title="Día"),
            use_container_width=True,
        )

        st.markdown("#### 📅 Carga por día (propuesta)")
        st.dataframe(
            km, use_container_width=True, hide_index=True,
            column_config={
                "dia": "Día", "n_clientes": "# Clientes",
                "ventas": st.column_config.NumberColumn("Ventas 12m", format="localized"),
                "km_ruta": st.column_config.NumberColumn("Km", format="%.1f"),
            },
        )

        st.markdown("#### 🔄 Actual vs propuesto")
        comp = opt[["cliente", "city", "dia_actual", "dia",
                    "sr_route_sequence", "secuencia", "frecuencia",
                    "facturas_mes", "ventas"]]
        st.dataframe(
            comp, use_container_width=True, hide_index=True,
            column_config={
                "cliente": "Cliente", "city": "Ciudad",
                "dia_actual": "Día actual", "dia": "Día propuesto",
                "sr_route_sequence": "Secuencia actual",
                "secuencia": "Secuencia propuesta",
                "frecuencia": "Frecuencia sugerida",
                "facturas_mes": st.column_config.NumberColumn("Fact./mes", format="%.2f"),
                "ventas": st.column_config.NumberColumn("Ventas 12m", format="localized"),
            },
        )

# ── Clientes activos sin GPS ──
if not sin_gps.empty:
    st.divider()
    st.markdown("### 📍 Activos en ruta sin GPS (por geolocalizar)")
    st.caption("Están marcados como activos en ruta pero no tienen coordenadas, "
               "así que no entran a la optimización.")
    cols = [c for c in ["name", "city", "user_name"] if c in sin_gps.columns]
    st.dataframe(sin_gps[cols].rename(columns={"name": "Cliente", "city": "Ciudad",
                                               "user_name": "Vendedor"}),
                 use_container_width=True, hide_index=True)

# ── Export Excel (para cargar manualmente en Odoo) ──
if propuestas:
    st.divider()
    st.markdown("### 📥 Descargar la propuesta")
    st.caption(
        "La app **no escribe nada en Odoo**. El Excel trae una hoja por "
        "vendedor para revisar, una hoja **«Importar Odoo»** con solo las "
        "columnas necesarias para la importación, y la hoja **«Sin GPS»** con "
        "los clientes que quedaron fuera del plan."
    )

    # Hoja de importación: solo lo mínimo, con el ID de base de datos.
    imp_rows = []
    for nombre, opt in propuestas.items():
        for _, r in opt.iterrows():
            imp_rows.append({
                ".id": int(r["partner_id"]),
                "sr_route_sequence": int(r["secuencia"]),
                "sr_route_id/.id": (int(r["route_id_propuesto"])
                                    if pd.notna(r["route_id_propuesto"]) else ""),
                "sr_visit_frequency": r["frecuencia_code"],
                "Cliente (referencia)": r["cliente"],
                "Vendedor (referencia)": nombre,
            })
    imp = pd.DataFrame(imp_rows)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        for nombre, opt in propuestas.items():
            base_n = "".join(ch for ch in nombre if ch.isalnum() or ch == " ")[:22].strip()
            opt[["dia", "secuencia", "cliente", "city", "frecuencia", "semanas",
                 "ventas", "facturas_mes", "dia_actual", "sr_route_sequence",
                 "lat", "lon"]].rename(
                columns={"dia": "Día propuesto", "secuencia": "Secuencia propuesta",
                         "cliente": "Cliente", "city": "Ciudad",
                         "frecuencia": "Frecuencia sugerida", "semanas": "Semanas",
                         "ventas": "Ventas 12m", "facturas_mes": "Fact./mes",
                         "dia_actual": "Día actual",
                         "sr_route_sequence": "Secuencia actual",
                         "lat": "Lat", "lon": "Lon"}
            ).to_excel(xw, sheet_name=f"Rutero {base_n}"[:31], index=False)
        imp.to_excel(xw, sheet_name="Importar Odoo", index=False)
        if not sin_gps.empty:
            sin_gps[[c for c in ["name", "city", "user_name"] if c in sin_gps.columns]] \
                .rename(columns={"name": "Cliente", "city": "Ciudad",
                                 "user_name": "Vendedor"}) \
                .to_excel(xw, sheet_name="Sin GPS", index=False)

    st.download_button(
        "⬇️ Descargar propuesta en Excel", buf.getvalue(),
        "rutero_propuesta.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )

    with st.expander("ℹ️ Cómo cargarlo en Odoo"):
        st.markdown(
            "1. Abre la hoja **«Importar Odoo»** y borra las dos columnas de "
            "referencia (Cliente / Vendedor); son solo para que revises.\n"
            "2. En Odoo: **Contactos → vista Lista → Favoritos → Importar registros**.\n"
            "3. Sube el archivo y en el mapeo de columnas asegúrate de que:\n"
            "   - `.id` → **Database ID** (así actualiza en vez de crear).\n"
            "   - `sr_route_sequence` → *Secuencia en rutero*.\n"
            "   - `sr_route_id/.id` → *Rutero principal* (por Database ID).\n"
            "   - `sr_visit_frequency` → *Frecuencia de visita*.\n"
            "4. Usa **Probar** antes de **Importar**. Si solo quieres cambiar el "
            "orden y no el día, borra la columna `sr_route_id/.id`."
        )
