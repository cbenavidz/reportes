# -*- coding: utf-8 -*-
"""
Página: Rotación de Inventario.

KPIs y análisis completo de rotación basado en:
  - Numerador: ventas del período (account.move.line de facturas).
  - Denominador: saldo cuenta 14 del balance contable (PUC colombiano).

Incluye:
  - KPIs cabecera (rotación, días, saldo, ventas).
  - Evolución mensual: ventas, saldo inventario, rotación anualizada.
  - Por categoría: rotación, días, margen.
  - Por producto: top rápidos, top lentos.
  - Recomendaciones automáticas accionables.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    load_account_balances_aggregated,
    load_chart_of_accounts,
    load_companies,
    load_inventory_balance_monthly_series,
    load_invoice_lines,
    load_purchase_invoice_lines,
    load_stock_quants,
)
from src.purchases_analyzer import (
    compute_category_crosstab,
    compute_inventory_recommendations,
    compute_product_crosstab,
    compute_purchases_vs_sales_summary,
    compute_rotacion_30d_historica_consolidada,
    compute_rotacion_categoria_30d_historica,
    compute_rotacion_categoria_multi_ventana,
    compute_rotacion_cuenta_14,
)
from src.financial_statements import enrich_chart_with_puc
from src.ui_components import render_company_context, render_sidebar_filters


st.set_page_config(
    page_title="Rotación de Inventario | Cartera",
    page_icon="🔄",
    layout="wide",
)

require_auth()
logout_button()

st.title("🔄 Rotación de Inventario")
st.caption(
    "Análisis completo de rotación: evolución mensual, por categoría, "
    "por producto y recomendaciones accionables. "
    "**Fórmula:** Ventas del período / Saldo cuenta 14 (Inventarios)."
)

# Sidebar
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()


# ── Período ──
st.markdown("### 🗓️ Período de análisis")
col_p1, col_p2, col_p3 = st.columns([1, 1, 2])
today = date.today()

with col_p1:
    fecha_desde = st.date_input(
        "Desde",
        value=today.replace(day=1) - timedelta(days=180),
        key="ri_desde",
    )
with col_p2:
    fecha_hasta = st.date_input("Hasta", value=today, key="ri_hasta")
with col_p3:
    quick = st.radio(
        "Atajos",
        options=[
            "Personalizado", "Mes actual", "Mes anterior", "Trimestre actual",
            "Últimos 90 días", "Año en curso", "Últimos 12 meses",
        ],
        index=6, horizontal=False, key="ri_atajo",
    )

if quick != "Personalizado":
    if quick == "Año en curso":
        fecha_desde, fecha_hasta = today.replace(month=1, day=1), today
    elif quick == "Últimos 12 meses":
        fecha_desde, fecha_hasta = today - timedelta(days=365), today
    elif quick == "Últimos 90 días":
        fecha_desde, fecha_hasta = today - timedelta(days=90), today
    elif quick == "Mes actual":
        fecha_desde, fecha_hasta = today.replace(day=1), today
    elif quick == "Mes anterior":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        fecha_desde, fecha_hasta = last_prev.replace(day=1), last_prev
    elif quick == "Trimestre actual":
        q_start_month = ((today.month - 1) // 3) * 3 + 1
        fecha_desde = today.replace(month=q_start_month, day=1)
        fecha_hasta = today

st.caption(f"📅 Período: **{fecha_desde}** → **{fecha_hasta}**")
periodo_dias = (fecha_hasta - fecha_desde).days + 1
fecha_desde_prev = fecha_desde - timedelta(days=periodo_dias)
fecha_hasta_prev = fecha_desde - timedelta(days=1)


# ── Banner empresa ──
companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])


# ── Helpers de cálculo (deben estar antes del filtro de categoría) ──
def _money(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"${v:,.0f}"


def _delta_pct(act: float, prev: float) -> str:
    if prev == 0:
        return "+100%" if act else "—"
    return f"{(act - prev) / abs(prev) * 100:+.1f}%"


def _filter_sales_by_category(sl: pd.DataFrame, cat: str | None) -> pd.DataFrame:
    """Filtra ventas por categoría. None o '(Todas)' devuelve todo."""
    if sl is None or sl.empty or not cat or cat == "(Todas)":
        return sl if sl is not None else pd.DataFrame()
    s = sl.copy()
    if "product_categ_name" not in s.columns:
        return s
    s["product_categ_name"] = s["product_categ_name"].fillna("(Sin categoría)")
    return s[s["product_categ_name"] == cat]


# Toggle de KPIs móviles (anclados a HOY) — cargan 4 balances + 1 sales extra
show_mobile = st.checkbox(
    "📐 Mostrar KPIs móviles 90/180/365 días (anclados a hoy)",
    value=False,
    help=(
        "Estos KPIs requieren 4 cargas adicionales de saldo cuenta 14 + "
        "1 carga de ventas de los últimos 365 días. Desactiva para acelerar "
        "el reporte si solo te interesa el período seleccionado."
    ),
)

# ── Carga base ──
# 1) El plan de cuentas se carga PRIMERO porque lo necesitamos para
#    identificar las cuentas de inventario antes de lanzar la serie
#    mensual de inventario en la tanda paralela.
# 2) Todo lo demás (ventas, compras, stock, balances Y la serie mensual
#    de inventario) se descarga EN PARALELO. Antes la serie mensual
#    corría sola DESPUÉS de la tanda paralela — ahora va dentro.
from concurrent.futures import ThreadPoolExecutor as _TPbase  # noqa: E402
from src.purchases_analyzer import get_inventory_account_ids  # noqa: E402

with st.spinner("Cargando plan de cuentas, ventas, compras y balances..."):
    _cids = filters["company_ids"]

    # Ventas: si el toggle de móviles está ON, cargar 365d (cubre todo);
    # si está OFF, solo el rango del período + previo.
    if show_mobile:
        sales_date_from = min(
            fecha_desde_prev, today - timedelta(days=365)
        ).isoformat()
        sales_date_to = max(fecha_hasta, today).isoformat()
    else:
        sales_date_from = fecha_desde_prev.isoformat()
        sales_date_to = fecha_hasta.isoformat()

    fecha_antes = (fecha_desde - timedelta(days=1)).isoformat()
    fecha_antes_prev = (fecha_desde_prev - timedelta(days=1)).isoformat()

    # Paso 1: plan de cuentas + cuentas de inventario.
    chart_df = load_chart_of_accounts(company_ids=_cids)
    inv_acc_ids = get_inventory_account_ids(chart_df)
    _inv_ids_tuple = tuple(sorted(inv_acc_ids)) if inv_acc_ids else ()

    # Paso 2: el resto de descargas, todas independientes → en paralelo.
    _tasks = {
        "sales": lambda: load_invoice_lines(
            company_ids=_cids, date_from=sales_date_from, date_to=sales_date_to,
        ),
        "purchases": lambda: load_purchase_invoice_lines(
            date_from=fecha_desde_prev.isoformat(),
            date_to=fecha_hasta.isoformat(), company_ids=_cids,
        ),
        "stock": lambda: load_stock_quants(company_ids=_cids),
        "bal_corte": lambda: load_account_balances_aggregated(
            date_to=fecha_hasta.isoformat(), company_ids=_cids,
        ),
        "bal_inicio": lambda: load_account_balances_aggregated(
            date_to=fecha_antes, company_ids=_cids,
        ),
        "bal_corte_prev": lambda: load_account_balances_aggregated(
            date_to=fecha_hasta_prev.isoformat(), company_ids=_cids,
        ),
        "bal_inicio_prev": lambda: load_account_balances_aggregated(
            date_to=fecha_antes_prev, company_ids=_cids,
        ),
        # Serie mensual de saldo de inventario (2 consultas internas).
        "serie_mensual": lambda: (
            load_inventory_balance_monthly_series(
                date_from=fecha_desde.isoformat(),
                date_to=fecha_hasta.isoformat(),
                inventory_account_ids=_inv_ids_tuple,
                company_ids=tuple(_cids) if _cids else None,
            ) if _inv_ids_tuple else pd.DataFrame()
        ),
    }
    if show_mobile:
        _tasks["bal_inicio_30"] = lambda: load_account_balances_aggregated(
            date_to=(today - timedelta(days=31)).isoformat(), company_ids=_cids,
        )
        _tasks["bal_inicio_90"] = lambda: load_account_balances_aggregated(
            date_to=(today - timedelta(days=91)).isoformat(), company_ids=_cids,
        )
        _tasks["bal_inicio_180"] = lambda: load_account_balances_aggregated(
            date_to=(today - timedelta(days=181)).isoformat(), company_ids=_cids,
        )
        _tasks["bal_inicio_365"] = lambda: load_account_balances_aggregated(
            date_to=(today - timedelta(days=366)).isoformat(), company_ids=_cids,
        )
        if fecha_hasta != today:
            _tasks["bal_hoy"] = lambda: load_account_balances_aggregated(
                date_to=today.isoformat(), company_ids=_cids,
            )

    _results: dict = {}
    with _TPbase(max_workers=8) as _pool:
        _futs = {k: _pool.submit(fn) for k, fn in _tasks.items()}
        for k, fut in _futs.items():
            _results[k] = fut.result()

    sales_lines = _results["sales"]
    sales_365 = sales_lines  # alias
    purchases_lines = _results["purchases"]
    stock_df = _results["stock"]
    balances_corte = _results["bal_corte"]
    balances_inicio = _results["bal_inicio"]
    balances_corte_prev = _results["bal_corte_prev"]
    balances_inicio_prev = _results["bal_inicio_prev"]
    serie_mensual_raw = _results["serie_mensual"]

    if show_mobile:
        balances_hoy = (
            balances_corte if fecha_hasta == today
            else _results.get("bal_hoy", balances_corte)
        )
        bal_inicio_30 = _results["bal_inicio_30"]
        bal_inicio_90 = _results["bal_inicio_90"]
        bal_inicio_180 = _results["bal_inicio_180"]
        bal_inicio_365 = _results["bal_inicio_365"]
    else:
        balances_hoy = balances_corte
        bal_inicio_30 = pd.DataFrame()
        bal_inicio_90 = pd.DataFrame()
        bal_inicio_180 = pd.DataFrame()
        bal_inicio_365 = pd.DataFrame()

if chart_df is None or chart_df.empty:
    st.error(
        "No se pudo cargar el plan de cuentas. La rotación requiere "
        "el saldo de la cuenta 14 desde el balance contable."
    )
    st.stop()


# ── Cálculo de summary y rotación ──
summary_act = compute_purchases_vs_sales_summary(
    purchases_lines, sales_lines, stock_df, fecha_desde, fecha_hasta,
)
summary_prev = compute_purchases_vs_sales_summary(
    purchases_lines, sales_lines, stock_df, fecha_desde_prev, fecha_hasta_prev,
)
rot14_act = compute_rotacion_cuenta_14(
    balances_inicio, balances_corte, chart_df,
    summary_act["total_ventas"], summary_act["costo_ventas"],
    fecha_desde, fecha_hasta,
)
rot14_prev = compute_rotacion_cuenta_14(
    balances_inicio_prev, balances_corte_prev, chart_df,
    summary_prev["total_ventas"], summary_prev["costo_ventas"],
    fecha_desde_prev, fecha_hasta_prev,
)

# Crosstab por producto y categoría (lo necesitamos antes de los KPIs
# para poder obtener el valor de stock por categoría cuando hay filtro)
crosstab = compute_product_crosstab(
    purchases_lines, sales_lines, stock_df, fecha_desde, fecha_hasta,
)
cat_tab = compute_category_crosstab(crosstab)


# ── Filtro de categoría ──
st.markdown("### 🏷️ Filtro por categoría de producto")
if sales_lines is not None and not sales_lines.empty and "product_categ_name" in sales_lines.columns:
    cats_disp = ["(Todas)"] + sorted(
        sales_lines["product_categ_name"].fillna("(Sin categoría)").unique().tolist()
    )
else:
    cats_disp = ["(Todas)"]
cat_filter = st.selectbox(
    "Categoría",
    options=cats_disp,
    key="ri_cat_filter",
    help=(
        "Si seleccionas una categoría, las ventas se filtran a esa categoría "
        "y el denominador se cambia al **valor de stock de esa categoría** "
        "(stock.quant × costo). Esto sustituye al saldo de cuenta 14, que no "
        "se puede desagregar por categoría desde la contabilidad."
    ),
)
es_filtrado = cat_filter and cat_filter != "(Todas)"

# Aplicar filtro a ventas
sales_filt = _filter_sales_by_category(sales_lines, cat_filter)

# Cuando hay filtro de categoría, recalculamos los totales filtrados
if es_filtrado:
    from src.purchases_analyzer import _apply_default_exclusions, _normalize_sales_signed
    sf = _apply_default_exclusions(_normalize_sales_signed(sales_filt))
    if not sf.empty and "invoice_date" in sf.columns:
        sf["_d"] = pd.to_datetime(sf["invoice_date"], errors="coerce").dt.date
        sf_per = sf[(sf["_d"] >= fecha_desde) & (sf["_d"] <= fecha_hasta)]
        ventas_filt_act = float(sf_per["price_subtotal_signed"].sum())
        costo_filt_act = float(sf_per.get("line_cost", 0).sum())
        sf_prv = sf[(sf["_d"] >= fecha_desde_prev) & (sf["_d"] <= fecha_hasta_prev)]
        ventas_filt_prv = float(sf_prv["price_subtotal_signed"].sum())
        costo_filt_prv = float(sf_prv.get("line_cost", 0).sum())
    else:
        ventas_filt_act = ventas_filt_prv = costo_filt_act = costo_filt_prv = 0.0

    # Valor de stock de la categoría seleccionada (proxy desde stock.quant)
    if cat_tab is not None and not cat_tab.empty:
        cat_row = cat_tab[cat_tab["product_categ_name"] == cat_filter]
        if cat_row.empty:
            cat_tab_norm = cat_tab.copy()
            cat_tab_norm["product_categ_name"] = cat_tab_norm[
                "product_categ_name"
            ].fillna("(Sin categoría)")
            cat_row = cat_tab_norm[cat_tab_norm["product_categ_name"] == cat_filter]
        saldo_filt = float(cat_row["stock_valor"].sum()) if not cat_row.empty else 0.0
    else:
        saldo_filt = 0.0
    # Para período anterior NO tenemos stock histórico → usamos el actual
    saldo_filt_act = saldo_filt
    saldo_filt_prv = saldo_filt
else:
    ventas_filt_act = summary_act["total_ventas"]
    costo_filt_act = summary_act["costo_ventas"]
    ventas_filt_prv = summary_prev["total_ventas"]
    costo_filt_prv = summary_prev["costo_ventas"]
    saldo_filt_act = rot14_act["saldo_promedio"]
    saldo_filt_prv = rot14_prev["saldo_promedio"]


# ── Helper para rotación móvil ──
def _rotacion_rango(
    sales_df: pd.DataFrame,
    date_from_: date,
    date_to_: date,
    bal_inicio: pd.DataFrame,
    bal_final: pd.DataFrame,
    cat: str | None,
    cat_tab_local: pd.DataFrame,
) -> dict:
    """Calcula rotación para un rango específico, aplicando filtro de categoría."""
    from src.purchases_analyzer import _apply_default_exclusions, _normalize_sales_signed
    sf = _filter_sales_by_category(sales_df, cat)
    sf = _apply_default_exclusions(_normalize_sales_signed(sf))
    if sf is not None and not sf.empty and "invoice_date" in sf.columns:
        sf["_d"] = pd.to_datetime(sf["invoice_date"], errors="coerce").dt.date
        sf_per = sf[(sf["_d"] >= date_from_) & (sf["_d"] <= date_to_)]
        ventas_r = float(sf_per["price_subtotal_signed"].sum())
        costo_r = float(sf_per.get("line_cost", 0).sum())
    else:
        ventas_r = costo_r = 0.0

    # Denominador: cuenta 14 (sin filtro) o stock_valor categoría (con filtro)
    if cat and cat != "(Todas)":
        if cat_tab_local is not None and not cat_tab_local.empty:
            ct = cat_tab_local.copy()
            ct["product_categ_name"] = ct["product_categ_name"].fillna("(Sin categoría)")
            row = ct[ct["product_categ_name"] == cat]
            saldo_prom = float(row["stock_valor"].sum()) if not row.empty else 0.0
        else:
            saldo_prom = 0.0
    else:
        r = compute_rotacion_cuenta_14(
            bal_inicio, bal_final, chart_df, ventas_r, costo_r,
            date_from_, date_to_,
        )
        saldo_prom = r["saldo_promedio"]

    rot_v = (ventas_r / saldo_prom) if saldo_prom else 0.0
    rot_c = (costo_r / saldo_prom) if saldo_prom else 0.0
    period_d = max((date_to_ - date_from_).days + 1, 1)
    rot_v_anu = rot_v * (365 / period_d) if period_d else 0.0
    dias_v = (365 / rot_v_anu) if rot_v_anu > 0 else 0.0
    return {
        "ventas": ventas_r, "costo": costo_r, "saldo_prom": saldo_prom,
        "rot_v": rot_v, "rot_c": rot_c,
        "rot_v_anu": rot_v_anu, "dias_v": dias_v,
        "period_d": period_d,
    }


# Calcular rotación a 30d, 90d, 180d, 365d — solo si el toggle está ON
if show_mobile:
    rot_30 = _rotacion_rango(
        sales_365, today - timedelta(days=30), today,
        bal_inicio_30, balances_hoy, cat_filter, cat_tab,
    )
    rot_90 = _rotacion_rango(
        sales_365, today - timedelta(days=90), today,
        bal_inicio_90, balances_hoy, cat_filter, cat_tab,
    )
    rot_180 = _rotacion_rango(
        sales_365, today - timedelta(days=180), today,
        bal_inicio_180, balances_hoy, cat_filter, cat_tab,
    )
    rot_365 = _rotacion_rango(
        sales_365, today - timedelta(days=365), today,
        bal_inicio_365, balances_hoy, cat_filter, cat_tab,
    )
else:
    _empty_rot = {
        "ventas": 0, "costo": 0, "saldo_prom": 0,
        "rot_v": 0, "rot_c": 0, "rot_v_anu": 0, "dias_v": 0, "period_d": 0,
    }
    rot_30 = _empty_rot
    rot_90 = _empty_rot
    rot_180 = _empty_rot
    rot_365 = _empty_rot

# Rotación del período (usando valores filtrados si hay filtro)
rot_v_act = (ventas_filt_act / saldo_filt_act) if saldo_filt_act > 0 else 0.0
rot_c_act = (costo_filt_act / saldo_filt_act) if saldo_filt_act > 0 else 0.0
rot_v_prv = (ventas_filt_prv / saldo_filt_prv) if saldo_filt_prv > 0 else 0.0
rot_c_prv = (costo_filt_prv / saldo_filt_prv) if saldo_filt_prv > 0 else 0.0

label_denominador = (
    f"stock valor de **{cat_filter}**"
    if es_filtrado
    else f"inventario promedio cuenta 14"
)

st.markdown(f"#### 📊 Rotación del período (cruda, sin anualizar) — {cat_filter}")
st.caption(
    f"Denominador: {label_denominador}. "
    f"Ejemplo: si vendiste $120M con denominador de $100M, rotación = **1.20x**."
)
k1, k2, k3, k4 = st.columns(4)
with k1:
    st.metric(
        "🔄 Rot. Ventas / Inv.",
        f"{rot_v_act:.2f}x" if rot_v_act else "—",
        delta=_delta_pct(rot_v_act, rot_v_prv),
        help=(
            f"Ventas ({_money(ventas_filt_act)}) / "
            f"Denominador ({_money(saldo_filt_act)})."
        ),
    )
with k2:
    st.metric(
        "🏛️ Rot. Costo / Inv. (NIIF)",
        f"{rot_c_act:.2f}x" if rot_c_act else "—",
        delta=_delta_pct(rot_c_act, rot_c_prv),
        help=(
            f"Costo ventas ({_money(costo_filt_act)}) / "
            f"Denominador ({_money(saldo_filt_act)})."
        ),
    )
with k3:
    st.metric(
        "📒 Denominador",
        _money(saldo_filt_act),
        help=(
            f"Saldo final cuenta 14: {_money(rot14_act['saldo_final'])} · "
            f"Inicial: {_money(rot14_act['saldo_inicial'])} · "
            f"Promedio: {_money(rot14_act['saldo_promedio'])}"
        ) if not es_filtrado else (
            f"Valor de stock de la categoría '{cat_filter}' "
            f"(snapshot actual de stock.quant)."
        ),
    )
with k4:
    st.metric(
        "💰 Ventas del período",
        _money(ventas_filt_act),
        delta=_delta_pct(ventas_filt_act, ventas_filt_prv),
    )

if st.button("🔄 Recargar datos (limpia caché)", key="ri_reload"):
    st.cache_data.clear()
    st.rerun()

st.markdown("---")

# ── KPIs móviles: 30d, 90d, 180d, 365d (anclados a HOY) — SOLO si toggle ON ──
if show_mobile:
    st.markdown(f"### 📐 Rotación móvil — anclada a hoy ({today})")
    st.caption(
        "Mismo cálculo (ventas / denominador) aplicado a ventanas de 30, 90, "
        "180 y 365 días que terminan hoy. Permite ver si el inventario está "
        "rotando mejor o peor en el corto, medio y largo plazo."
    )
    cm0, cm1, cm2, cm3 = st.columns(4)
    for col, label, r in [
        (cm0, "30 días", rot_30),
        (cm1, "90 días", rot_90),
        (cm2, "180 días", rot_180),
        (cm3, "365 días", rot_365),
    ]:
        with col:
            with st.container(border=True):
                st.markdown(f"#### Últimos {label}")
                st.metric(
                    "🔄 Rot. (Ventas/Inv)",
                    f"{r['rot_v']:.2f}x" if r["rot_v"] else "—",
                    delta=(
                        f"{r['rot_v_anu']:.2f}x anualizada"
                        if r["rot_v_anu"] else None
                    ),
                    delta_color="off",
                )
                sub1, sub2 = st.columns(2)
                with sub1:
                    st.metric(
                        "Rot. NIIF",
                        f"{r['rot_c']:.2f}x" if r["rot_c"] else "—",
                    )
                with sub2:
                    st.metric(
                        "Días inv.",
                        f"{r['dias_v']:.0f}" if r["dias_v"] > 0 else "—",
                    )
                st.caption(
                    f"Ventas: {_money(r['ventas'])} · "
                    f"Costo: {_money(r['costo'])} · "
                    f"Denominador: {_money(r['saldo_prom'])}"
                )

# Análisis de tendencia entre las 4 ventanas (solo si toggle ON)
trend_msg = []
if (
    show_mobile
    and rot_30["rot_v_anu"] and rot_90["rot_v_anu"]
    and rot_180["rot_v_anu"] and rot_365["rot_v_anu"]
):
    # Comparar la anualizada del último mes vs el promedio anual
    r30a = rot_30["rot_v_anu"]
    r365a = rot_365["rot_v_anu"]
    if r30a > r365a * 1.2:
        trend_msg.append(
            "📈 **Mes anterior acelerando:** rotación anualizada del "
            f"último mes ({r30a:.1f}x) supera al promedio anual ({r365a:.1f}x)."
        )
    elif r30a < r365a * 0.8:
        trend_msg.append(
            "📉 **Mes anterior desacelerando:** rotación anualizada del "
            f"último mes ({r30a:.1f}x) está por debajo del promedio anual "
            f"({r365a:.1f}x)."
        )
    if rot_30["rot_v"] > rot_90["rot_v"] > rot_180["rot_v"]:
        trend_msg.append("📈 Tendencia creciente progresiva (30d > 90d > 180d).")
    elif rot_30["rot_v"] < rot_90["rot_v"] < rot_180["rot_v"]:
        trend_msg.append("📉 Tendencia decreciente progresiva (30d < 90d < 180d).")
if trend_msg:
    st.info(" · ".join(trend_msg))

st.markdown("---")


# ── Serie mensual: saldo cuenta 14 + ventas del mes ──
# La serie de saldo de inventario ya se descargó EN PARALELO con el resto
# de datos (variable serie_mensual_raw). Aquí solo se consume; si quedó
# vacía/en cero se usa el fallback clásico (1 balance por mes).
with st.spinner("Construyendo serie mensual..."):
    from src.purchases_analyzer import _saldo_cuenta_14_from_balances

    saldo_mes = pd.DataFrame(columns=["mes", "saldo_inv_cierre"])
    _metodo_serie = "rápido (2 consultas)"

    if serie_mensual_raw is not None and not serie_mensual_raw.empty:
        saldo_mes = serie_mensual_raw.rename(
            columns={"saldo_cierre": "saldo_inv_cierre"}
        )[["mes", "saldo_inv_cierre"]].copy()

    # FALLBACK: si no se pudieron identificar cuentas de inventario o la
    # serie quedó vacía/en cero, usar el método clásico (1 balance por mes).
    if saldo_mes.empty or float(saldo_mes["saldo_inv_cierre"].abs().sum()) == 0:
        _metodo_serie = "clásico (1 consulta por mes)"
        from calendar import monthrange as _mr
        from concurrent.futures import ThreadPoolExecutor as _TP3

        chart_e = enrich_chart_with_puc(chart_df)
        eom_list: list[date] = []
        y, m = fecha_desde.year, fecha_desde.month
        while True:
            first = date(y, m, 1)
            if first > fecha_hasta:
                break
            last = date(y, m, _mr(y, m)[1])
            eom_list.append(min(last, fecha_hasta))
            m += 1
            if m > 12:
                m, y = 1, y + 1
            if y > fecha_hasta.year + 1:
                break

        def _saldo_at(d: date) -> tuple[date, float]:
            bal = load_account_balances_aggregated(
                date_to=d.isoformat(), company_ids=filters["company_ids"],
            )
            saldo, _ = _saldo_cuenta_14_from_balances(bal, chart_e)
            return d, saldo

        saldos_por_mes: dict[date, float] = {}
        with _TP3(max_workers=6) as pool:
            for d, saldo in pool.map(_saldo_at, eom_list):
                saldos_por_mes[d] = saldo
        saldo_mes = pd.DataFrame([
            {"mes": pd.Timestamp(d.replace(day=1)),
             "saldo_inv_cierre": saldos_por_mes.get(d, 0)}
            for d in eom_list
        ])

    # Diagnóstico
    with st.expander("🔍 Diagnóstico de saldo cuenta 14 mensual", expanded=False):
        st.write(f"**Método de cálculo de la serie:** {_metodo_serie}")
        st.write(f"**Cuentas de inventario detectadas:** {len(inv_acc_ids)}")
        st.write(f"**Meses calculados:** {len(saldo_mes)}")
        st.write(f"**Rango pedido:** {fecha_desde} → {fecha_hasta}")
        st.dataframe(saldo_mes, hide_index=True, use_container_width=True)
        if not saldo_mes.empty:
            st.write(
                f"**Suma de saldo_inv_cierre:** "
                f"${saldo_mes['saldo_inv_cierre'].sum():,.0f}"
            )
            st.write(
                f"**Max saldo_inv_cierre:** "
                f"${saldo_mes['saldo_inv_cierre'].max():,.0f}"
            )

    # Ventas del período mensualizadas (cálculo en memoria, ya tenemos las líneas)
    ventas_mes = pd.DataFrame()
    if sales_lines is not None and not sales_lines.empty:
        from src.purchases_analyzer import _apply_default_exclusions, _normalize_sales_signed
        sl = _apply_default_exclusions(_normalize_sales_signed(sales_lines))
        if not sl.empty and "invoice_date" in sl.columns:
            sl["_d"] = pd.to_datetime(sl["invoice_date"], errors="coerce").dt.date
            sl_per = sl[(sl["_d"] >= fecha_desde) & (sl["_d"] <= fecha_hasta)].copy()
            sl_per["mes"] = pd.to_datetime(sl_per["invoice_date"]).dt.to_period("M").dt.to_timestamp()
            ventas_mes = sl_per.groupby("mes", as_index=False)["price_subtotal_signed"].sum()
            ventas_mes = ventas_mes.rename(columns={"price_subtotal_signed": "ventas_mes"})

    # Combinar ventas + saldo (outer join para incluir meses con uno u otro)
    monthly = saldo_mes.merge(ventas_mes, on="mes", how="outer").fillna(
        {"ventas_mes": 0, "saldo_inv_cierre": 0}
    )
    monthly = monthly.sort_values("mes").reset_index(drop=True)

    # Saldo PROMEDIO del mes = (saldo cierre mes anterior + saldo cierre mes actual) / 2
    monthly["saldo_inv_apertura"] = monthly["saldo_inv_cierre"].shift(1)
    # Para el primer mes usamos solo el saldo de cierre
    monthly["saldo_inv_apertura"] = monthly["saldo_inv_apertura"].fillna(
        monthly["saldo_inv_cierre"]
    )
    monthly["saldo_inv_prom"] = (
        monthly["saldo_inv_apertura"] + monthly["saldo_inv_cierre"]
    ) / 2.0

    # Rotación del MES (cruda, sin anualizar): ventas_mes / saldo_promedio
    # Ejemplo: ventas 120M, saldo prom 100M → 1.2x veces en el mes
    monthly["rotacion_mes"] = monthly.apply(
        lambda r: (r["ventas_mes"] / r["saldo_inv_prom"])
        if r["saldo_inv_prom"] > 0 else None,
        axis=1,
    )
    # Días de inventario implícitos en ese mes (30 días / rotación)
    monthly["dias_inv_mes"] = monthly["rotacion_mes"].apply(
        lambda x: 30 / x if x and x > 0 else None
    )
    # Versión anualizada (para benchmarks contra industria)
    monthly["rotacion_anual_mes"] = monthly["rotacion_mes"] * 12

    monthly["mes_label"] = pd.to_datetime(monthly["mes"]).dt.strftime("%Y-%m")


# ── Sub-pestañas ──
t_evol, t_cat, t_prod, t_rec, t_diag = st.tabs([
    "📈 Evolución mensual",
    "🏷️ Por categoría",
    "📦 Por producto",
    "💡 Recomendaciones",
    "🔍 Diagnóstico",
])


# ─── Tab Evolución ───
with t_evol:
    st.markdown("### Evolución mensual de la rotación")
    st.caption(
        "**Rotación del mes = Ventas del mes / Saldo promedio del mes.** "
        "Sin anualizar. Ejemplo: si vendiste $120M con saldo promedio "
        "de $100M, la rotación del mes es **1.20x**."
    )

    if monthly.empty:
        st.info("Sin datos para construir la serie mensual.")
    else:
        # Gráfico 1: ventas vs saldo de inventario
        fig1 = go.Figure()
        fig1.add_trace(go.Bar(
            x=monthly["mes_label"], y=monthly["ventas_mes"],
            name="Ventas del mes", marker_color="#10b981",
            yaxis="y",
        ))
        fig1.add_trace(go.Scatter(
            x=monthly["mes_label"], y=monthly["saldo_inv_prom"],
            name="Saldo cuenta 14 (promedio)",
            line=dict(color="#a855f7", width=3),
            mode="lines+markers", yaxis="y2",
        ))
        fig1.add_trace(go.Scatter(
            x=monthly["mes_label"], y=monthly["saldo_inv_cierre"],
            name="Saldo cuenta 14 (cierre)",
            line=dict(color="#c084fc", width=2, dash="dot"),
            mode="lines", yaxis="y2",
        ))
        fig1.update_layout(
            height=420, margin=dict(l=0, r=0, t=30, b=0),
            title="Ventas mensuales vs Saldo de inventario",
            yaxis=dict(title="Ventas $", tickformat=",.0f"),
            yaxis2=dict(
                title="Saldo inv. $", overlaying="y", side="right",
                tickformat=",.0f",
            ),
            legend=dict(orientation="h", yanchor="bottom", y=1.05, x=0),
        )
        st.plotly_chart(fig1, use_container_width=True)

        # Gráfico 2: rotación cruda del mes
        rot_chart = monthly.dropna(subset=["rotacion_mes"]).copy()
        if not rot_chart.empty:
            fig2 = go.Figure()
            fig2.add_trace(go.Bar(
                x=rot_chart["mes_label"], y=rot_chart["rotacion_mes"],
                name="Rotación del mes",
                marker_color="#0ea5e9",
                text=rot_chart["rotacion_mes"],
                texttemplate="%{text:.2f}x",
                textposition="outside",
            ))
            fig2.add_trace(go.Scatter(
                x=rot_chart["mes_label"], y=rot_chart["dias_inv_mes"],
                name="Días de inventario (30 / rot)",
                line=dict(color="#ef4444", width=2, dash="dot"),
                mode="lines+markers", yaxis="y2",
            ))
            fig2.update_layout(
                height=400, margin=dict(l=0, r=0, t=30, b=0),
                title="Rotación cruda del mes y días de inventario",
                yaxis=dict(title="Rotación (veces en el mes)"),
                yaxis2=dict(title="Días", overlaying="y", side="right"),
                legend=dict(orientation="h", yanchor="bottom", y=1.05, x=0),
            )
            st.plotly_chart(fig2, use_container_width=True)

        # Tabla mensual
        with st.expander("📋 Tabla detallada por mes", expanded=False):
            tabla = monthly[[
                "mes_label", "ventas_mes",
                "saldo_inv_apertura", "saldo_inv_cierre", "saldo_inv_prom",
                "rotacion_mes", "dias_inv_mes", "rotacion_anual_mes",
            ]].rename(columns={"mes_label": "Mes"})
            st.dataframe(
                tabla,
                column_config={
                    "ventas_mes": st.column_config.NumberColumn(
                        "Ventas del mes", format="$%,.0f"
                    ),
                    "saldo_inv_apertura": st.column_config.NumberColumn(
                        "Saldo apertura", format="$%,.0f"
                    ),
                    "saldo_inv_cierre": st.column_config.NumberColumn(
                        "Saldo cierre", format="$%,.0f"
                    ),
                    "saldo_inv_prom": st.column_config.NumberColumn(
                        "Saldo promedio", format="$%,.0f"
                    ),
                    "rotacion_mes": st.column_config.NumberColumn(
                        "Rot. del mes", format="%.2fx"
                    ),
                    "dias_inv_mes": st.column_config.NumberColumn(
                        "Días inv.", format="%.0f"
                    ),
                    "rotacion_anual_mes": st.column_config.NumberColumn(
                        "Rot. anualizada", format="%.2fx"
                    ),
                },
                use_container_width=True, hide_index=True,
            )

    # ── Histórico consolidado: rotación a 30 días mes a mes ──
    st.markdown("---")
    st.markdown("### 📉 Histórico mes a mes — Rotación a 30 días (consolidado)")
    st.caption(
        "Rotación de toda la empresa: para cada mes, ventas de los últimos "
        "30 días / saldo de inventario (cuenta 14). El denominador es fijo "
        "(saldo actual), así la variación mes a mes refleja la velocidad "
        "de venta."
    )

    _denom_30d = float(rot14_act.get("saldo_final", 0) or 0)
    hist_30d_consol = compute_rotacion_30d_historica_consolidada(
        sales_lines if sales_lines is not None else sales_365,
        denominador=_denom_30d,
        today=today,
        meses=12,
        anualizar=False,
    )

    if _denom_30d <= 0:
        st.info(
            "No hay saldo de inventario (cuenta 14) disponible para "
            "calcular la rotación consolidada."
        )
    elif (
        hist_30d_consol is None or hist_30d_consol.empty
        or float(hist_30d_consol["ventas_30d"].abs().sum()) == 0
    ):
        st.info(
            "No hay suficiente histórico de ventas para esta gráfica. "
            "Activa el toggle **'Mostrar KPIs móviles 90/180/365'** arriba "
            "para cargar las ventas de los últimos 365 días."
        )
    else:
        fig_30d = go.Figure()
        fig_30d.add_trace(go.Scatter(
            x=hist_30d_consol["mes_label"],
            y=hist_30d_consol["rotacion_30d"],
            mode="lines+markers",
            name="Rotación 30 días",
            line=dict(color="#0ea5e9", width=3),
            marker=dict(size=9),
            hovertemplate="<b>%{x}</b><br>Rotación 30d: %{y:.2f}x<extra></extra>",
        ))
        # Línea de promedio del período
        _rot_prom = float(hist_30d_consol["rotacion_30d"].mean())
        fig_30d.add_hline(
            y=_rot_prom, line_dash="dash", line_color="#94a3b8",
            annotation_text=f"Promedio {_rot_prom:.2f}x",
            annotation_position="top left",
        )
        fig_30d.update_layout(
            height=400, margin=dict(l=0, r=0, t=20, b=0),
            title="Rotación a 30 días — consolidado empresa",
            yaxis=dict(title="Rotación (veces)"),
            xaxis=dict(title="Mes"),
        )
        st.plotly_chart(fig_30d, use_container_width=True)

        # KPIs rápidos
        _ult = hist_30d_consol.iloc[-1]
        kc1, kc2, kc3 = st.columns(3)
        with kc1:
            st.metric(
                f"Rotación 30d — {_ult['mes_label']}",
                f"{_ult['rotacion_30d']:.2f}x",
            )
        with kc2:
            st.metric("Promedio 12 meses", f"{_rot_prom:.2f}x")
        with kc3:
            _maxr = hist_30d_consol.loc[
                hist_30d_consol["rotacion_30d"].idxmax()
            ]
            st.metric(
                "Mejor mes",
                f"{_maxr['rotacion_30d']:.2f}x",
                delta=str(_maxr["mes_label"]), delta_color="off",
            )

        with st.expander("📋 Tabla — rotación 30d consolidada por mes"):
            st.dataframe(
                hist_30d_consol[["mes_label", "ventas_30d", "rotacion_30d"]]
                .rename(columns={"mes_label": "Mes"}),
                column_config={
                    "ventas_30d": st.column_config.NumberColumn(
                        "Ventas 30 días", format="$%,.0f",
                    ),
                    "rotacion_30d": st.column_config.NumberColumn(
                        "Rotación 30d", format="%.2fx",
                    ),
                },
                use_container_width=True, hide_index=True,
            )
        st.caption(
            f"Denominador (saldo cuenta 14 al corte): "
            f"{_money(_denom_30d)}. "
            "Una línea creciente indica que la empresa rota su inventario "
            "cada vez más rápido."
        )


# ─── Tab Por categoría ───
with t_cat:
    # ── Tabla NUEVA: rotación multi-ventana por categoría ──
    st.markdown("### 📊 Rotación por categoría — ventanas múltiples")
    st.caption(
        "**Rotación cruda del período:** Ventas de la ventana / Stock valor. "
        "Cuántas veces se vendió el inventario en esa ventana de tiempo. "
        "Ejemplo: rot. 30d = 0.5x significa que en el último mes vendió "
        "la mitad de su inventario."
    )
    col_t1, col_t2 = st.columns([1, 4])
    with col_t1:
        anualizar_rot = st.checkbox(
            "Anualizar",
            value=False,
            help=(
                "Si está ON: multiplica las ventanas cortas para hacerlas "
                "comparables (30d × 12, 90d × 4.06, etc.) → 'rotación anual "
                "implícita'. Si está OFF: rotación cruda del período."
            ),
            key="ri_anualizar_multi",
        )

    multi_rot = compute_rotacion_categoria_multi_ventana(
        sales_lines if sales_lines is not None else sales_365,
        stock_df,
        today=today,
        anualizar=anualizar_rot,
    )
    if multi_rot is None or multi_rot.empty:
        st.info(
            "No hay datos suficientes. Activa el toggle "
            "'Mostrar KPIs móviles 90/180/365' arriba para cargar las "
            "ventas de 365 días que requiere este análisis."
        )
    else:
        # Quitar categorías sin stock valorado (no se puede calcular)
        mr = multi_rot[multi_rot["stock_valor_categoria"] > 0].copy()
        if mr.empty:
            st.info(
                "Ninguna categoría tiene stock valorado. Las rotaciones "
                "requieren valor de stock (de stock.quant)."
            )
        else:
            cols_order = [
                "product_categ_name",
                "stock_valor_categoria",
                "rotacion_30d", "rotacion_90d",
                "rotacion_180d", "rotacion_365d",
            ]
            sufijo = "anual" if anualizar_rot else "del período"
            st.dataframe(
                mr[cols_order],
                column_config={
                    "product_categ_name": st.column_config.TextColumn(
                        "Categoría", width="large"
                    ),
                    "stock_valor_categoria": st.column_config.NumberColumn(
                        "Stock $", format="$%,.0f",
                    ),
                    "rotacion_30d": st.column_config.NumberColumn(
                        f"Rot. 30d ({sufijo})", format="%.2fx",
                        help=(
                            "Ventas últimos 30 días / Stock × 12"
                            if anualizar_rot
                            else "Ventas últimos 30 días / Stock"
                        ),
                    ),
                    "rotacion_90d": st.column_config.NumberColumn(
                        f"Rot. 90d ({sufijo})", format="%.2fx",
                        help=(
                            "Ventas últimos 90 días / Stock × 4.06"
                            if anualizar_rot
                            else "Ventas últimos 90 días / Stock"
                        ),
                    ),
                    "rotacion_180d": st.column_config.NumberColumn(
                        f"Rot. 180d ({sufijo})", format="%.2fx",
                        help=(
                            "Ventas últimos 180 días / Stock × 2.03"
                            if anualizar_rot
                            else "Ventas últimos 180 días / Stock"
                        ),
                    ),
                    "rotacion_365d": st.column_config.NumberColumn(
                        "Rot. 1 año", format="%.2fx",
                        help="Ventas últimos 365 días / Stock",
                    ),
                },
                use_container_width=True, hide_index=True, height=420,
            )
            if anualizar_rot:
                st.caption(
                    "💡 **Modo anualizado:** todas las columnas comparables "
                    "entre sí. Si 30d > 365d → categoría acelerando. "
                    "Si 30d < 365d → desacelerando."
                )
            else:
                st.caption(
                    "💡 **Modo cruda:** cada columna es la cantidad de veces "
                    "que se vendió el inventario en esa ventana. Los números "
                    "crecen naturalmente porque hay más tiempo. Activa el "
                    "toggle 'Anualizar' para comparar entre ventanas."
                )

    # ── Histórico mes a mes — rotación a 30 días por categoría ──
    st.markdown("---")
    st.markdown("### 📉 Histórico mes a mes — Rotación a 30 días")
    st.caption(
        "Para cada mes se calcula la rotación con una ventana de 30 días "
        "(ventas de los últimos 30 días del mes / valor de stock actual). "
        "Permite ver cómo varía la rotación a 30 días mes a mes."
    )

    hist_30d = compute_rotacion_categoria_30d_historica(
        sales_lines if sales_lines is not None else sales_365,
        stock_df,
        today=today,
        meses=12,
        anualizar=False,
    )

    if hist_30d is None or hist_30d.empty:
        st.info(
            "No hay suficiente histórico de ventas para construir esta "
            "gráfica. Activa el toggle **'Mostrar KPIs móviles 90/180/365'** "
            "arriba para cargar las ventas de los últimos 365 días."
        )
    else:
        # Selección de categorías — por defecto las de mayor venta total
        ventas_por_cat = (
            hist_30d.groupby("product_categ_name")["ventas_30d"]
            .sum().sort_values(ascending=False)
        )
        cats_disponibles = ventas_por_cat.index.tolist()
        default_cats = cats_disponibles[:6]

        cats_sel = st.multiselect(
            "Categorías a comparar",
            options=cats_disponibles,
            default=default_cats,
            key="ri_hist30_cats",
            help="Por defecto se muestran las 6 categorías de mayor venta.",
        )

        if not cats_sel:
            st.info("Selecciona al menos una categoría para ver la gráfica.")
        else:
            hist_show = hist_30d[
                hist_30d["product_categ_name"].isin(cats_sel)
            ].copy()
            fig_hist = px.line(
                hist_show,
                x="mes_label", y="rotacion_30d",
                color="product_categ_name",
                markers=True,
                labels={
                    "mes_label": "Mes",
                    "rotacion_30d": "Rotación 30 días (veces)",
                    "product_categ_name": "Categoría",
                },
            )
            fig_hist.update_layout(
                height=420, margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                hovermode="x unified",
            )
            fig_hist.update_traces(
                hovertemplate="%{y:.2f}x<extra>%{fullData.name}</extra>",
            )
            st.plotly_chart(fig_hist, use_container_width=True)

            # Tabla pivote mes × categoría
            with st.expander("📋 Tabla — rotación 30d por mes y categoría"):
                pivot = hist_show.pivot_table(
                    index="mes_label", columns="product_categ_name",
                    values="rotacion_30d", aggfunc="sum",
                ).reset_index().rename(columns={"mes_label": "Mes"})
                st.dataframe(
                    pivot, use_container_width=True, hide_index=True,
                )
            st.caption(
                "💡 Una línea creciente indica que esa categoría está "
                "rotando cada vez más rápido; una decreciente, que se está "
                "frenando. El denominador (stock) es el mismo en todos los "
                "meses, así que la variación refleja la velocidad de venta."
            )

    st.markdown("---")
    st.markdown("### Rotación por categoría (período seleccionado)")
    st.caption(
        "Rotación = costo de ventas / valor de stock por categoría. "
        "Calculada con el snapshot actual de stock.quant."
    )
    if cat_tab is None or cat_tab.empty:
        st.info("Sin datos por categoría.")
    else:
        # Solo categorías con stock valorado y rotación calculable
        cat_show = cat_tab[
            (cat_tab["stock_valor"] > 0)
            & (cat_tab["dias_inventario"].notna())
        ].copy()
        if cat_show.empty:
            st.info(
                "No hay categorías con stock valorado e información completa. "
                "Verifica que tus productos tengan costo y stock asignado."
            )
        else:
            # KPIs cabecera por categoría
            kc1, kc2, kc3 = st.columns(3)
            with kc1:
                top_r = cat_show.nsmallest(1, "dias_inventario").iloc[0]
                st.metric(
                    "🚀 Categoría más rápida",
                    str(top_r["product_categ_name"])[:30],
                    delta=f"{top_r['dias_inventario']:.0f} días",
                    delta_color="off",
                )
            with kc2:
                top_l = cat_show.nlargest(1, "dias_inventario").iloc[0]
                st.metric(
                    "🐢 Categoría más lenta",
                    str(top_l["product_categ_name"])[:30],
                    delta=f"{top_l['dias_inventario']:.0f} días",
                    delta_color="off",
                )
            with kc3:
                stock_total = float(cat_show["stock_valor"].sum())
                st.metric(
                    "💎 Stock valorado (todas)",
                    _money(stock_total),
                    delta=f"{len(cat_show)} categorías",
                    delta_color="off",
                )

            # Gráfico horizontal
            fig_cat = px.bar(
                cat_show.sort_values("rotacion_anual"),
                x="rotacion_anual", y="product_categ_name",
                orientation="h",
                color="rotacion_anual",
                color_continuous_scale="RdYlGn",
                text="rotacion_anual",
            )
            fig_cat.update_traces(
                texttemplate="%{text:.2f}x", textposition="outside",
            )
            fig_cat.update_layout(
                height=max(400, len(cat_show) * 30),
                margin=dict(l=0, r=0, t=10, b=0),
                yaxis=dict(title=""),
                xaxis=dict(title="Rotación anual (veces)"),
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_cat, use_container_width=True)

            # Tabla
            st.markdown("**Detalle por categoría**")
            st.dataframe(
                cat_show[[
                    "product_categ_name", "n_productos",
                    "stock_qty", "stock_valor",
                    "monto_ventas", "costo_ventas", "margen_pct",
                    "rotacion_anual", "dias_inventario",
                ]].sort_values("rotacion_anual", ascending=False),
                column_config={
                    "product_categ_name": st.column_config.TextColumn(
                        "Categoría", width="large"
                    ),
                    "n_productos": st.column_config.NumberColumn(
                        "# Prods", format="%d"
                    ),
                    "stock_qty": st.column_config.NumberColumn(
                        "Stock qty", format="%,.1f"
                    ),
                    "stock_valor": st.column_config.NumberColumn(
                        "Stock $", format="$%,.0f"
                    ),
                    "monto_ventas": st.column_config.NumberColumn(
                        "Ventas", format="$%,.0f"
                    ),
                    "costo_ventas": st.column_config.NumberColumn(
                        "Costo ventas", format="$%,.0f"
                    ),
                    "margen_pct": st.column_config.NumberColumn(
                        "% Margen", format="%.1f%%"
                    ),
                    "rotacion_anual": st.column_config.NumberColumn(
                        "Rot. anual", format="%.2fx"
                    ),
                    "dias_inventario": st.column_config.NumberColumn(
                        "Días inv.", format="%.0f"
                    ),
                },
                use_container_width=True, hide_index=True, height=480,
            )


# ─── Tab Por producto ───
with t_prod:
    st.markdown("### Rotación por producto")
    st.caption(
        "Análisis individual de productos con stock valorado. "
        "Identifica los más rápidos (riesgo de quiebre) y los más lentos "
        "(capital atrapado)."
    )
    if crosstab is None or crosstab.empty:
        st.info("Sin datos.")
    else:
        # Productos con datos válidos
        prod = crosstab[
            (crosstab["stock_qty"] > 0)
            & (crosstab["qty_vendida"] > 0)
        ].copy()
        if prod.empty:
            st.info(
                "No hay productos con stock y ventas simultáneamente. "
                "Revisa por separado en Compras vs Ventas."
            )
        else:
            cf1, cf2 = st.columns(2)
            with cf1:
                cats = ["(Todas)"] + sorted(
                    prod["product_categ_name"].fillna("(Sin categoría)").unique().tolist()
                )
                cat_filter = st.selectbox(
                    "Categoría", options=cats, key="ri_cat_prod",
                )
            with cf2:
                top_n = st.slider(
                    "Top N a mostrar",
                    min_value=10, max_value=100, value=20, step=5,
                    key="ri_top_n",
                )

            prod_show = prod.copy()
            prod_show["product_categ_name"] = prod_show[
                "product_categ_name"
            ].fillna("(Sin categoría)")
            if cat_filter and cat_filter != "(Todas)":
                prod_show = prod_show[
                    prod_show["product_categ_name"] == cat_filter
                ]

            # Top más rápidos
            col_r, col_l = st.columns(2)
            with col_r:
                st.markdown(f"**🚀 Top {top_n} más rápidos (más rotación)**")
                rapidos = prod_show.nlargest(top_n, "rotacion_anual")
                if not rapidos.empty:
                    st.dataframe(
                        rapidos[[
                            "product_default_code", "product_name",
                            "stock_qty", "qty_vendida",
                            "rotacion_anual", "dias_cobertura",
                        ]],
                        column_config={
                            "product_default_code": "Código",
                            "product_name": st.column_config.TextColumn(
                                "Producto", width="medium"
                            ),
                            "stock_qty": st.column_config.NumberColumn(
                                "Stock", format="%,.1f"
                            ),
                            "qty_vendida": st.column_config.NumberColumn(
                                "Vendida", format="%,.1f"
                            ),
                            "rotacion_anual": st.column_config.NumberColumn(
                                "Rot. anual", format="%.2fx"
                            ),
                            "dias_cobertura": st.column_config.NumberColumn(
                                "Días cob.", format="%.0f"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=500,
                    )

            with col_l:
                st.markdown(f"**🐢 Top {top_n} más lentos (menos rotación)**")
                lentos = prod_show[
                    prod_show["rotacion_anual"] > 0
                ].nsmallest(top_n, "rotacion_anual")
                if not lentos.empty:
                    st.dataframe(
                        lentos[[
                            "product_default_code", "product_name",
                            "stock_qty", "qty_vendida",
                            "rotacion_anual", "dias_cobertura",
                        ]],
                        column_config={
                            "product_default_code": "Código",
                            "product_name": st.column_config.TextColumn(
                                "Producto", width="medium"
                            ),
                            "stock_qty": st.column_config.NumberColumn(
                                "Stock", format="%,.1f"
                            ),
                            "qty_vendida": st.column_config.NumberColumn(
                                "Vendida", format="%,.1f"
                            ),
                            "rotacion_anual": st.column_config.NumberColumn(
                                "Rot. anual", format="%.2fx"
                            ),
                            "dias_cobertura": st.column_config.NumberColumn(
                                "Días cob.", format="%.0f"
                            ),
                        },
                        use_container_width=True, hide_index=True,
                        height=500,
                    )


# ─── Tab Recomendaciones ───
with t_rec:
    st.markdown("### 💡 Recomendaciones automáticas")
    st.caption(
        "Insights generados a partir de la evolución mensual, comparativos "
        "vs período anterior y análisis por categoría/producto."
    )

    recs = compute_inventory_recommendations(
        rot14_act, rot14_prev, cat_tab, crosstab,
        monthly, summary_act, summary_prev,
    )

    if not recs:
        st.success(
            "✅ Sin recomendaciones urgentes. El inventario está en buen estado."
        )
    else:
        # Resumen
        alta = sum(1 for r in recs if r.get("prioridad") == "alta")
        media = sum(1 for r in recs if r.get("prioridad") == "media")
        baja = sum(1 for r in recs if r.get("prioridad") == "baja")

        cr1, cr2, cr3 = st.columns(3)
        with cr1:
            st.metric("🔴 Alta prioridad", f"{alta}")
        with cr2:
            st.metric("🟡 Media prioridad", f"{media}")
        with cr3:
            st.metric("🟢 Informativa", f"{baja}")

        st.markdown("---")

        prio_colors = {
            "alta": "#fee2e2",
            "media": "#fef3c7",
            "baja": "#dcfce7",
        }
        prio_emoji = {
            "alta": "🔴",
            "media": "🟡",
            "baja": "🟢",
        }
        for r in recs:
            prio = r.get("prioridad", "baja")
            with st.container(border=True):
                col_h1, col_h2 = st.columns([4, 1])
                with col_h1:
                    st.markdown(
                        f"### {r.get('tipo', '')} — {r.get('titulo', '')}"
                    )
                with col_h2:
                    st.markdown(
                        f"**{prio_emoji.get(prio, '')} {prio.upper()}**"
                    )
                st.markdown(f"**Diagnóstico:** {r.get('detalle', '')}")
                st.markdown(f"**Acción sugerida:** {r.get('accion', '')}")


# ─── Tab Diagnóstico ───
with t_diag:
    st.markdown("### 🔍 Diagnóstico de cálculo")
    st.caption(
        "Información para auditar el cálculo de la rotación: cuentas 14 "
        "encontradas, supuestos y datos faltantes."
    )

    st.markdown("**Cuentas 14 (Inventarios) detectadas al corte**")
    cuentas_14 = rot14_act.get("cuentas_detalle", pd.DataFrame())
    if cuentas_14 is not None and not cuentas_14.empty:
        st.dataframe(
            cuentas_14,
            column_config={
                "account_code": "Código",
                "account_name": st.column_config.TextColumn(
                    "Cuenta", width="large"
                ),
                "saldo": st.column_config.NumberColumn(
                    "Saldo", format="$%,.0f"
                ),
            },
            use_container_width=True, hide_index=True,
        )
    else:
        st.warning(
            "No se detectaron cuentas con código que empiece con 14 ni con "
            "subgrupo PUC 14 ni con account_type tipo stock/inventory. "
            "Revisa tu plan de cuentas."
        )

    st.markdown("---")
    st.markdown("**Supuestos del cálculo**")
    st.markdown(
        """
        - **Rotación general:** `Ventas del período / Saldo cuenta 14 al corte`,
          anualizada multiplicando por `365 / días_del_período`.
        - **Rotación mensual:** `Ventas del mes × 12 / Saldo cuenta 14 al cierre del mes`.
        - **Por categoría/producto:** `Costo de ventas / valor de stock actual`
          (usa snapshot de `stock.quant`, no histórico mensual).
        - **Ventas:** suma de `price_subtotal_signed` de líneas con
          `move_type` ∈ {out_invoice, out_refund}, **excluyendo** SOAT y ANTCL
          (mismas exclusiones que el Informe de Ventas).
        - **Saldo cuenta 14:** suma de (debit − credit) de cuentas cuyo
          código empiece con "14", o cuyo subgrupo PUC sea 14, o cuyo
          account_type contenga "stock"/"inventory".
        """
    )

    st.markdown("---")
    st.markdown("**Comparativo período actual vs anterior**")
    comp_df = pd.DataFrame([
        {
            "Métrica": "Ventas",
            "Actual": summary_act["total_ventas"],
            "Anterior": summary_prev["total_ventas"],
        },
        {
            "Métrica": "Saldo cuenta 14",
            "Actual": rot14_act["saldo_inventario"],
            "Anterior": rot14_prev["saldo_inventario"],
        },
        {
            "Métrica": "Rotación anual",
            "Actual": rot14_act["rotacion_anual"],
            "Anterior": rot14_prev["rotacion_anual"],
        },
        {
            "Métrica": "Días inventario",
            "Actual": rot14_act["dias_inventario"],
            "Anterior": rot14_prev["dias_inventario"],
        },
    ])
    st.dataframe(
        comp_df,
        column_config={
            "Actual": st.column_config.NumberColumn("Actual", format="%,.2f"),
            "Anterior": st.column_config.NumberColumn("Anterior", format="%,.2f"),
        },
        use_container_width=True, hide_index=True,
    )
