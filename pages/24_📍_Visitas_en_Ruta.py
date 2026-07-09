# -*- coding: utf-8 -*-
"""
Página: Visitas en Ruta (datos reales de `sr.visit`).

Trabaja con el check-in real del vendedor —no con la factura como proxy—:
cobertura (cumplimiento de agenda), efectividad de la visita, visitas fuera
de geocerca, motivos de no efectividad y frecuencia real por cliente.

Solo lectura sobre Odoo.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import load_route_partners, load_sr_routes, load_sr_visits
from src import visitas_analyzer as va
from src.ui_components import render_sidebar_filters

st.set_page_config(page_title="Visitas en Ruta | Cartera", page_icon="📍",
                   layout="wide")

require_auth()
logout_button()

st.title("📍 Visitas en Ruta")
st.caption(
    "Sobre las **visitas reales** registradas por los vendedores en la app "
    "móvil (`sr.visit`): check-in con GPS, efectividad y geocerca. "
    "«Efectiva» = el vendedor logró realizar la visita; cuando no, queda el "
    "motivo comercial registrado."
)

filters = render_sidebar_filters()
company_ids = filters["company_ids"]

c1, c2 = st.columns([1, 1])
with c1:
    f_desde = st.date_input("Desde", value=date.today() - timedelta(days=30),
                            max_value=date.today(), format="DD/MM/YYYY")
with c2:
    f_hasta = st.date_input("Hasta", value=date.today(),
                            max_value=date.today(), format="DD/MM/YYYY")
if f_desde > f_hasta:
    st.error("La fecha 'Desde' no puede ser posterior a 'Hasta'.")
    st.stop()

visits = load_sr_visits(f_desde.isoformat(), f_hasta.isoformat())
routes = load_sr_routes()
partners = load_route_partners(
    company_ids=tuple(company_ids) if company_ids else None
)

if visits is None or visits.empty:
    st.info(
        "No hay visitas registradas en el período. Si esperabas ver datos, "
        "verifica que el usuario de API pueda leer `sr.visit`."
    )
    st.stop()

# Filtro por vendedor
vendedores = sorted(visits["user_name"].dropna().unique().tolist())
sel = st.multiselect("Vendedores", vendedores, default=vendedores)
if sel:
    visits = visits[visits["user_name"].isin(sel)]
if visits.empty:
    st.warning("Sin visitas para los vendedores seleccionados.")
    st.stop()

# ── KPIs ──
k = va.kpis(visits)
m = st.columns(5)
m[0].metric("Visitas", f"{k['n_visitas']:,}")
m[1].metric("Efectivas", f"{k['n_efectivas']:,}", f"{k['efectividad_pct']:.0f}%")
m[2].metric("Fuera de geocerca", f"{k['n_fuera']:,}", f"{k['fuera_pct']:.0f}%",
            delta_color="inverse")
m[3].metric("Clientes visitados", f"{k['n_clientes']:,}")
m[4].metric("Vendedores", f"{k['n_vendedores']:,}")

st.divider()

# ── Cumplimiento de agenda ──
st.markdown("### 🎯 Cumplimiento de agenda")
st.caption(
    "Clientes planeados del día (activos en ruta cuyo día de visita es ese) "
    "vs efectivamente visitados."
)
fechas = pd.date_range(f_desde, f_hasta, freq="D").date.tolist()
cum = va.cumplimiento_agenda(visits, partners, routes, fechas)
if cum.empty or cum["planeados"].sum() == 0:
    st.info("No hay clientes planeados en el período (revisa los días de los ruteros).")
else:
    prom = cum.loc[cum["planeados"] > 0, "cumplimiento_pct"].mean()
    st.metric("Cumplimiento promedio", f"{prom:.0f}%")
    fig = px.bar(cum, x="fecha", y=["planeados", "visitados"], barmode="group",
                 labels={"value": "Clientes", "fecha": "", "variable": ""})
    fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                      legend=dict(orientation="h", y=-0.2))
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ── Evolución y vendedores ──
cA, cB = st.columns(2)
with cA:
    st.markdown("### 📈 Evolución diaria")
    ev = va.evolucion_diaria(visits)
    fig = px.bar(ev, x="dia", y="n_visitas", labels={"n_visitas": "Visitas", "dia": ""})
    fig.add_scatter(x=ev["dia"], y=ev["efectividad_pct"], name="Efectividad %",
                    yaxis="y2", mode="lines+markers", line=dict(color="#1B7A3D"))
    fig.update_layout(
        yaxis2=dict(title="Efectividad %", overlaying="y", side="right",
                    showgrid=False, range=[0, 100]),
        height=340, margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=-0.2),
    )
    st.plotly_chart(fig, use_container_width=True)

with cB:
    st.markdown("### 👤 Por vendedor")
    pv = va.por_vendedor(visits)
    st.dataframe(
        pv, use_container_width=True, hide_index=True,
        column_config={
            "vendedor": "Vendedor", "n_visitas": "Visitas",
            "n_clientes": "Clientes", "n_efectivas": "Efectivas",
            "efectividad_pct": st.column_config.NumberColumn("Efectividad", format="%.0f%%"),
            "n_fuera": "Fuera geocerca",
        },
    )
    st.plotly_chart(
        px.bar(pv, x="vendedor", y="efectividad_pct",
               labels={"efectividad_pct": "Efectividad %", "vendedor": ""}
               ).update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0)),
        use_container_width=True,
    )

st.divider()

# ── Motivos de no efectividad ──
st.markdown("### 🚫 Motivos de visita no efectiva")
mot = va.motivos_no_efectiva(visits)
if mot.empty:
    st.success("Todas las visitas del período fueron efectivas.")
else:
    st.plotly_chart(
        px.bar(mot, x="n", y="motivo", orientation="h",
               labels={"n": "Visitas", "motivo": ""}
               ).update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0)),
        use_container_width=True,
    )

st.divider()

# ── Visitas sospechosas por distancia ──
st.markdown("### 🛰️ Visitas fuera de rango")
st.caption(
    "Check-in marcado fuera de geocerca o hecho lejos del punto GPS del "
    "cliente. Puede ser un GPS mal capturado del cliente, o un check-in "
    "hecho a distancia."
)
umbral = st.slider("Umbral de distancia (metros)", 100, 2000, 200, step=50)
sosp = va.visitas_sospechosas(visits, umbral_m=float(umbral))
if sosp.empty:
    st.success("Ninguna visita fuera de rango con ese umbral.")
else:
    st.warning(f"{len(sosp)} visitas fuera de rango.")
    st.dataframe(
        sosp, use_container_width=True, hide_index=True,
        column_config={
            "fecha": st.column_config.DatetimeColumn("Fecha", format="DD/MM/YYYY HH:mm"),
            "partner_name": "Cliente", "user_name": "Vendedor",
            "route_name": "Rutero",
            "distance_m": st.column_config.NumberColumn("Distancia (m)", format="%.0f"),
            "outside_geofence": "Fuera geocerca", "is_effective": "Efectiva",
            "reason_name": "Motivo",
            "accuracy": st.column_config.NumberColumn("Precisión GPS (m)", format="%.0f"),
        },
    )
    st.download_button("⬇️ Descargar visitas fuera de rango",
                       sosp.to_csv(index=False).encode("utf-8"),
                       "visitas_fuera_de_rango.csv", "text/csv")

# ── Mapa de check-ins ──
geo = visits.dropna(subset=["latitude", "longitude"]).copy()
geo = geo[(geo["latitude"] != 0) & (geo["longitude"] != 0)]
if not geo.empty:
    st.markdown("### 🗺️ Mapa de check-ins")
    geo["estado"] = geo.apply(
        lambda r: "Fuera de geocerca" if r["outside_geofence"]
        else ("Efectiva" if r["is_effective"] else "No efectiva"), axis=1,
    )
    figm = px.scatter_mapbox(
        geo, lat="latitude", lon="longitude", color="estado",
        color_discrete_map={"Efectiva": "#1B7A3D", "No efectiva": "#f59e0b",
                            "Fuera de geocerca": "#B3261E"},
        hover_name="partner_name",
        hover_data={"user_name": True, "distance_m": ":,.0f",
                    "latitude": False, "longitude": False},
        zoom=8, height=520,
    )
    figm.update_layout(mapbox_style="open-street-map",
                       margin=dict(l=0, r=0, t=0, b=0))
    st.plotly_chart(figm, use_container_width=True)

st.divider()

# ── Frecuencia real por cliente ──
st.markdown("### 🔁 Frecuencia real por cliente")
st.caption("Cuántas veces se visitó realmente y cada cuántos días, según los check-in.")
fr = va.frecuencia_real(visits, f_hasta)
st.dataframe(
    fr, use_container_width=True, hide_index=True,
    column_config={
        "partner_id": None, "partner_name": "Cliente",
        "n_visitas": "Visitas", "n_efectivas": "Efectivas",
        "dias_entre_visitas": st.column_config.NumberColumn("Días entre visitas", format="%.1f"),
        "ultima_visita": st.column_config.DateColumn("Última visita"),
        "dias_desde_ultima": "Días desde última",
    },
)

# ── Export ──
buf = io.BytesIO()
with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
    va.por_vendedor(visits).to_excel(xw, sheet_name="Por vendedor", index=False)
    if not cum.empty:
        cum.to_excel(xw, sheet_name="Cumplimiento", index=False)
    if not mot.empty:
        mot.to_excel(xw, sheet_name="Motivos", index=False)
    if not sosp.empty:
        sosp.to_excel(xw, sheet_name="Fuera de rango", index=False)
    fr.to_excel(xw, sheet_name="Frecuencia real", index=False)
st.download_button("⬇️ Descargar informe de visitas (Excel)", buf.getvalue(),
                   "visitas_en_ruta.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
