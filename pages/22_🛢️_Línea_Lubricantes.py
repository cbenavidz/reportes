# -*- coding: utf-8 -*-
"""
Página: Línea Lubricantes — informe comercial completo.

Enfocada en la línea de LUBRICANTES (mayoreo, vendedores externos puerta a
puerta). Filtra las categorías cuya hoja empieza con "lubricantes" y arma:
KPIs, evolución, desempeño por categoría/producto/vendedor/ciudad,
zonificación ciudad × vendedor y recomendaciones (oportunidades de más
referencias, cross-sell, clientes inactivos, ciudades de baja cobertura).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import compute_full_analysis, load_invoice_lines
from src import lubricantes_analyzer as la
from src.route_sales import build_geo_dataframe, compute_visit_frequency
from src.ui_components import render_company_context, render_sidebar_filters

st.set_page_config(
    page_title="Línea Lubricantes | Cartera",
    page_icon="🛢️",
    layout="wide",
)

require_auth()
logout_button()

st.title("🛢️ Línea Lubricantes")
st.caption(
    "Informe comercial de la línea de **lubricantes** (mayoreo, vendedores "
    "externos puerta a puerta). Incluye todas las categorías cuya hoja "
    "empieza con «lubricantes». Ventas = subtotal sin IVA; volumen = "
    "cantidad × volumen del producto (galones)."
)


# ── Helpers de formato ──
def fmt_money(x) -> str:
    try:
        return f"${x:,.0f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0"


def fmt_num(x, dec=1) -> str:
    try:
        return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "0"


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


# ── Filtros ──
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa en el sidebar.")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    f_desde = st.date_input(
        "Desde", value=date.today() - timedelta(days=180),
        max_value=date.today(), format="DD/MM/YYYY",
    )
with c2:
    f_hasta = st.date_input(
        "Hasta", value=date.today(),
        max_value=date.today(), format="DD/MM/YYYY",
    )
with c3:
    if st.button("🔄 Recargar", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if f_desde > f_hasta:
    st.error("La fecha 'Desde' no puede ser posterior a 'Hasta'.")
    st.stop()

company_ids = filters["company_ids"]

# ── Carga de datos ──
with st.spinner("Cargando clientes y líneas de lubricantes..."):
    data = compute_full_analysis(
        months_back=filters["months_back"],
        rotation_period_days=filters["period_days"],
        company_ids=company_ids,
        exclude_cash_sales=filters["exclude_cash_sales"],
        analysis_window_days=filters.get("analysis_window_days"),
    )
    partners_all = data.get("raw_partners")
    lines = load_invoice_lines(
        company_ids=tuple(company_ids) if company_ids else None,
        date_from=f_desde.isoformat(), date_to=f_hasta.isoformat(),
    )

render_company_context(data.get("companies"), company_ids)

lub = la.filtrar_lubricantes(lines)
df = la.enriquecer(lub, partners_all)
df = la.filtrar_fechas(df, f_desde, f_hasta)

if df is None or df.empty:
    st.info(
        "No hay ventas de lubricantes en el período seleccionado. "
        "Verifica que existan categorías que empiecen con «lubricantes»."
    )
    st.stop()

# Clientes de la línea (para indicadores de ruta: visitas y mapa GPS).
if partners_all is not None and not partners_all.empty:
    lub_pids = set(
        pd.to_numeric(lub["partner_id"], errors="coerce").dropna().astype(int)
    )
    _pid = pd.to_numeric(partners_all["id"], errors="coerce")
    clientes_lub = partners_all[_pid.isin(lub_pids)].copy()
else:
    clientes_lub = pd.DataFrame()

# ── KPIs generales ──
k = la.kpis_generales(df)
st.markdown("### 📊 Resumen de la línea")
r1 = st.columns(4)
r1[0].metric("Ventas netas", fmt_money(k["ventas"]))
r1[1].metric("Volumen (gal)", fmt_num(k["volumen"]))
r1[2].metric("Margen", fmt_money(k["margen"]))
r1[3].metric("Margen %", f"{k['margen_pct']:.1f}%")
r2 = st.columns(4)
r2[0].metric("Clientes", f"{k['n_clientes']:,}")
r2[1].metric("Referencias (SKU)", f"{k['n_referencias']:,}")
r2[2].metric("Facturas", f"{k['n_facturas']:,}")
r2[3].metric("Ticket promedio", fmt_money(k["ticket"]))

st.divider()

# ── Evolución mensual ──
st.markdown("### 📈 Evolución mensual")
mens = df.copy()
mens["mes"] = mens["fecha"].dt.to_period("M").astype(str)
ev = mens.groupby("mes").agg(
    ventas=("ventas", "sum"), volumen=("volumen", "sum"),
    clientes=("partner_id", "nunique"),
).reset_index()
if not ev.empty:
    ce1, ce2 = st.columns([3, 2])
    with ce1:
        figev = px.bar(ev, x="mes", y="ventas", labels={"ventas": "Ventas netas", "mes": "Mes"})
        figev.add_scatter(x=ev["mes"], y=ev["volumen"], name="Volumen (gal)",
                          yaxis="y2", mode="lines+markers", line=dict(color="#f59e0b"))
        figev.update_layout(
            yaxis2=dict(title="Volumen (gal)", overlaying="y", side="right", showgrid=False),
            height=360, margin=dict(l=0, r=0, t=10, b=0),
            legend=dict(orientation="h", y=-0.2),
        )
        st.plotly_chart(figev, use_container_width=True)
    with ce2:
        st.markdown("**Clientes atendidos por mes (cobertura)**")
        figc = px.bar(ev, x="mes", y="clientes",
                      labels={"clientes": "# Clientes", "mes": "Mes"})
        figc.update_traces(marker_color="#1B7A3D")
        figc.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(figc, use_container_width=True)

st.divider()

# ── Por categoría y por producto ──
cat = la.por_categoria(df)
prod = la.por_producto(df, top=25)

colA, colB = st.columns(2)
with colA:
    st.markdown("### 🗂️ Por categoría")
    if not cat.empty:
        st.dataframe(
            cat[["categoria", "ventas", "volumen", "margen", "margen_pct",
                 "n_clientes", "n_referencias", "participacion_pct"]],
            use_container_width=True, hide_index=True,
            column_config={
                "ventas": st.column_config.NumberColumn("Ventas", format="localized"),
                "volumen": st.column_config.NumberColumn("Vol. (gal)", format="localized"),
                "margen": st.column_config.NumberColumn("Margen", format="localized"),
                "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
                "participacion_pct": st.column_config.NumberColumn("% Part.", format="%.1f%%"),
            },
        )
        st.plotly_chart(
            px.bar(cat, x="volumen", y="categoria", orientation="h",
                   labels={"volumen": "Volumen (gal)", "categoria": ""}),
            use_container_width=True,
        )
with colB:
    st.markdown("### 📦 Volumen por producto (top 25)")
    if not prod.empty:
        st.dataframe(
            prod[["producto", "volumen", "ventas", "margen_pct", "n_clientes"]],
            use_container_width=True, hide_index=True,
            column_config={
                "volumen": st.column_config.NumberColumn("Vol. (gal)", format="localized"),
                "ventas": st.column_config.NumberColumn("Ventas", format="localized"),
                "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
            },
        )
        st.plotly_chart(
            px.bar(prod.head(12), x="volumen", y="producto", orientation="h",
                   labels={"volumen": "Volumen (gal)", "producto": ""}),
            use_container_width=True,
        )

st.divider()

# ── KPI por vendedor ──
st.markdown("### 👤 KPI por vendedor (puerta a puerta)")
vend = la.por_vendedor(df)
if not vend.empty:
    st.dataframe(
        vend[["vendedor", "ventas", "volumen", "margen", "margen_pct",
              "n_clientes", "n_referencias", "n_facturas", "ticket"]],
        use_container_width=True, hide_index=True,
        column_config={
            "ventas": st.column_config.NumberColumn("Ventas", format="localized"),
            "volumen": st.column_config.NumberColumn("Vol. (gal)", format="localized"),
            "margen": st.column_config.NumberColumn("Margen", format="localized"),
            "margen_pct": st.column_config.NumberColumn("Margen %", format="%.1f%%"),
            "ticket": st.column_config.NumberColumn("Ticket", format="localized"),
        },
    )
    st.plotly_chart(
        px.bar(vend, x="vendedor", y="ventas", labels={"ventas": "Ventas", "vendedor": ""}),
        use_container_width=True,
    )

st.divider()

# ── Por ciudad / departamento ──
st.markdown("### 🌎 Ciudades y departamentos")
ciu = la.por_ciudad(df)
if not ciu.empty:
    st.dataframe(
        ciu[["departamento", "ciudad", "ventas", "volumen", "n_clientes",
             "n_facturas", "participacion_pct"]],
        use_container_width=True, hide_index=True,
        column_config={
            "ventas": st.column_config.NumberColumn("Ventas", format="localized"),
            "volumen": st.column_config.NumberColumn("Vol. (gal)", format="localized"),
            "participacion_pct": st.column_config.NumberColumn("% Part.", format="%.1f%%"),
        },
    )
    st.plotly_chart(
        px.bar(ciu.head(15), x="ventas", y="ciudad", orientation="h",
               color="departamento", labels={"ventas": "Ventas", "ciudad": ""}),
        use_container_width=True,
    )

st.divider()

# ── Frecuencia de visita (indicador de ruta) ──
st.markdown("### 🔁 Frecuencia de visita")
st.caption("Cada factura de lubricantes cuenta como una visita al cliente.")
freq = compute_visit_frequency(lub, clientes_lub, f_desde, f_hasta, company_ids)
if freq is None or freq.empty:
    st.info("Sin datos de visitas en el período.")
else:
    mfv = st.columns(3)
    mfv[0].metric("Visitas totales", f"{int(freq['num_visitas'].sum()):,}")
    mfv[1].metric("Visitas por cliente", f"{freq['num_visitas'].mean():.1f}")
    _pd = freq["dias_entre_visitas_prom"].dropna()
    mfv[2].metric("Días entre visitas (prom.)", f"{_pd.mean():.0f}" if not _pd.empty else "—")
    st.dataframe(
        freq[["partner_name", "city", "num_visitas", "dias_entre_visitas_prom",
              "ultima_visita", "dias_desde_ultima", "ventas_periodo", "volumen_periodo"]],
        use_container_width=True, hide_index=True,
        column_config={
            "num_visitas": st.column_config.NumberColumn("# Visitas"),
            "dias_entre_visitas_prom": st.column_config.NumberColumn("Días entre visitas", format="%.0f"),
            "ultima_visita": st.column_config.DateColumn("Última visita"),
            "dias_desde_ultima": st.column_config.NumberColumn("Días desde última"),
            "ventas_periodo": st.column_config.NumberColumn("Ventas", format="localized"),
            "volumen_periodo": st.column_config.NumberColumn("Vol. (gal)", format="localized"),
        },
    )

st.divider()

# ── Mapa GPS de clientes (indicador de ruta) ──
st.markdown("### 🗺️ Mapa de clientes de lubricantes")
geo = build_geo_dataframe(clientes_lub, lub, f_desde, f_hasta, company_ids)
if geo is None or geo.empty:
    st.warning(
        "Ningún cliente de lubricantes tiene coordenadas GPS válidas. "
        "En Odoo: Contactos → seleccionar clientes → Acción → Geolocalizar."
    )
else:
    geo["_size"] = geo["ventas_periodo"].clip(lower=0).fillna(0)
    _mx = float(geo["_size"].max() or 1)
    geo["_size"] = geo["_size"].apply(lambda v: max(v, _mx * 0.05))
    figm = px.scatter_mapbox(
        geo, lat="lat", lon="lon", color="ventas_periodo", size="_size",
        size_max=22, hover_name="partner_name",
        hover_data={
            "city": True if "city" in geo.columns else False,
            "ventas_periodo": ":,.0f", "num_visitas": True,
            "lat": False, "lon": False, "_size": False,
        },
        zoom=6, height=520, color_continuous_scale="Viridis",
    )
    figm.update_layout(mapbox_style="open-street-map", margin=dict(l=0, r=0, t=0, b=0))
    st.plotly_chart(figm, use_container_width=True)

st.divider()

# ── Zonificación ciudad × vendedor ──
st.markdown("### 🧭 Zonificación: ciudad × vendedor")
metrica = st.radio("Métrica", ["ventas", "volumen"], horizontal=True, key="zon_metric")
zon = la.zonificacion(df, valor=metrica)
if not zon.empty:
    st.caption("Cruce de cobertura por zona geográfica y comercial. TOTAL en filas/columnas.")
    st.dataframe(zon.style.format("{:,.0f}"), use_container_width=True)

st.divider()

# ── Recomendaciones ──
st.markdown("## 💡 Opciones de mejora y recomendaciones")

st.markdown("#### 1) Clientes que pueden comprar más referencias")
st.caption(
    "Compran volumen por encima de la mediana pero manejan pocas referencias: "
    "concentran en pocos SKUs y son candidatos a ampliar el portafolio."
)
opp = la.oportunidades_referencias(df)
if opp.empty:
    st.info("Sin candidatos claros en el período.")
else:
    st.dataframe(
        opp[["partner_name", "ciudad", "vendedor", "ventas", "volumen",
             "n_referencias", "ref_potencial", "n_facturas"]],
        use_container_width=True, hide_index=True,
        column_config={
            "ventas": st.column_config.NumberColumn("Ventas", format="localized"),
            "volumen": st.column_config.NumberColumn("Vol. (gal)", format="localized"),
            "ref_potencial": st.column_config.NumberColumn("Refs. a sumar (est.)"),
        },
    )
    st.download_button("⬇️ Descargar oportunidades", _csv(opp),
                       "lubricantes_oportunidades.csv", "text/csv")

st.markdown("#### 2) Cross-sell: referencias sugeridas por cliente")
st.caption(
    "Para cada cliente, referencias populares (compradas por muchos clientes) "
    "que ese cliente aún NO compra."
)
cs = la.cross_sell(df)
if cs.empty:
    st.info("Sin sugerencias de cross-sell en el período.")
else:
    st.dataframe(
        cs[["partner_name", "ciudad", "vendedor", "referencia_sugerida",
            "clientes_que_la_compran", "ventas_cliente"]],
        use_container_width=True, hide_index=True,
        column_config={
            "ventas_cliente": st.column_config.NumberColumn("Ventas cliente", format="localized"),
        },
    )
    st.download_button("⬇️ Descargar cross-sell", _csv(cs),
                       "lubricantes_crosssell.csv", "text/csv")

col1, col2 = st.columns(2)
with col1:
    st.markdown("#### 3) Clientes inactivos")
    dias = st.slider("Días sin comprar", 30, 180, 45, step=15)
    inact = la.clientes_inactivos(df, f_hasta, min_days=dias)
    if inact.empty:
        st.info("Sin clientes inactivos con ese umbral.")
    else:
        st.dataframe(
            inact[["partner_name", "ciudad", "vendedor", "dias_sin_comprar", "ventas"]],
            use_container_width=True, hide_index=True,
            column_config={
                "ventas": st.column_config.NumberColumn("Ventas período", format="localized"),
                "dias_sin_comprar": st.column_config.NumberColumn("Días sin comprar"),
            },
        )
with col2:
    st.markdown("#### 4) Ciudades de baja cobertura")
    st.caption("Pocos clientes pero venta/cliente alta: potencial de sumar clientes.")
    bcov = la.ciudades_baja_cobertura(df)
    if bcov.empty:
        st.info("Sin ciudades destacadas con baja cobertura.")
    else:
        st.dataframe(
            bcov[["departamento", "ciudad", "ventas", "n_clientes", "venta_x_cliente"]],
            use_container_width=True, hide_index=True,
            column_config={
                "ventas": st.column_config.NumberColumn("Ventas", format="localized"),
                "venta_x_cliente": st.column_config.NumberColumn("Venta/cliente", format="localized"),
            },
        )

st.divider()
st.caption(
    "💬 Ideas adicionales: definir metas de referencias por cliente y de "
    "volumen por ruta; priorizar visitas a clientes inactivos de alto valor; "
    "usar el cross-sell como guion de venta puerta a puerta; y revisar las "
    "ciudades de baja cobertura para abrir nuevos clientes."
)
