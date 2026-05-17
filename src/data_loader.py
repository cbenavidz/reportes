# -*- coding: utf-8 -*-
"""
Capa de carga de datos con caché para Streamlit.

Encapsula la conexión a Odoo y la extracción, y aplica @st.cache_data
para evitar recargar en cada interacción.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from .alerts import AlertConfig, generate_alerts
from .analyzer import (
    CarteraMetrics,
    analyze_cartera,
    compute_monthly_history,
    compute_rotation,
)
from .extractor import extract_all_for_cartera, extract_companies, extract_invoice_lines
from .odoo_client import OdooClient, OdooConnectionError
from .recommendations import build_collection_plan, upcoming_dues
from .scoring import ScoringConfig, compute_partner_scores

logger = logging.getLogger(__name__)


def load_environment() -> None:
    """Carga variables del .env (solo una vez por proceso)."""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


@st.cache_resource(show_spinner=False)
def get_odoo_client() -> OdooClient:
    """Devuelve un cliente Odoo autenticado, cacheado por sesión."""
    load_environment()
    client = OdooClient.from_env()
    client.authenticate()
    return client


@st.cache_data(ttl=3600, show_spinner="Cargando empresas...")
def load_companies() -> "pd.DataFrame":
    """Devuelve la lista de res.company visibles para el usuario. Cache 1h."""
    client = get_odoo_client()
    return extract_companies(client)


@st.cache_data(ttl=900, show_spinner="Descargando datos de Odoo...")
def load_raw_data(
    months_back: int = 12,
    company_ids: tuple[int, ...] | None = None,
) -> dict:
    """
    Descarga datos crudos de Odoo. Cache de 15 min.

    Devuelve dict serializable (dataframes y fecha de corte).
    `company_ids` se pasa como tupla para que sea hashable por @st.cache_data.
    """
    client = get_odoo_client()
    data = extract_all_for_cartera(
        client,
        months_back=months_back,
        company_ids=list(company_ids) if company_ids else None,
    )
    return data


@st.cache_data(ttl=900, show_spinner="Calculando análisis de cartera...")
def compute_full_analysis(
    months_back: int = 12,
    rotation_period_days: int = 365,
    company_ids: tuple[int, ...] | None = None,
    exclude_cash_sales: bool = True,
    analysis_window_days: int | None = None,
) -> dict:
    """
    Pipeline completo: descarga + análisis + scoring + alertas + plan de cobro.

    Devuelve un dict con todos los resultados listos para mostrar.

    `analysis_window_days` (lapso del estudio): si se pasa, restringe el
    histórico de facturas usado para hábito de pago / scoring a las últimas
    N días desde el corte. None = usa todo lo cargado.
    """
    raw = load_raw_data(months_back=months_back, company_ids=company_ids)

    metrics: CarteraMetrics = analyze_cartera(
        invoices=raw["invoices"],
        open_invoices=raw["open_invoices"],
        payments=raw["payments"],
        partners=raw["partners"],
        cutoff_date=raw["cutoff_date"],
        rotation_period_days=rotation_period_days,
        exclude_cash_sales=exclude_cash_sales,
        analysis_window_days=analysis_window_days,
    )

    scored = compute_partner_scores(metrics.by_partner, ScoringConfig.from_env())
    alerts = generate_alerts(
        metrics.open_invoices, scored, AlertConfig(), cutoff_date=metrics.cutoff_date
    )
    plan = build_collection_plan(scored)
    proximos = upcoming_dues(metrics.open_invoices, days_ahead=7, cutoff_date=metrics.cutoff_date)

    # Histórico mensual (12 últimos meses por defecto)
    # Pasamos open_invoices para que el saldo histórico se ancle en el saldo
    # actual real y se reconstruya hacia atrás (en lugar de acumular desde 0,
    # que hace que el cobrado "alcance" a las ventas y deje el saldo en 0).
    history = compute_monthly_history(
        invoices=raw["invoices"],
        payments=raw["payments"],
        months=12,
        cutoff_date=raw["cutoff_date"],
        exclude_cash_sales=exclude_cash_sales,
        open_invoices=raw["open_invoices"],
    )

    # DSO últimos 90 días: último valor del rolling al cierre del último mes.
    # Refleja la "salud reciente" de cobranza vs. la rotación acumulada.
    dso_ultimos_90 = (
        float(history["dso_rolling"].iloc[-1])
        if (history is not None and not history.empty and "dso_rolling" in history.columns)
        else 0.0
    )

    return {
        "cutoff_date": metrics.cutoff_date,
        "kpis": {
            "saldo_cartera": metrics.saldo_cartera,
            "rotacion_dias": metrics.rotacion_dias,
            "dso_ultimos_90": dso_ultimos_90,
            "rotacion_veces": metrics.rotacion_veces,
            "ventas_credito": metrics.ventas_credito_periodo,
            "saldo_promedio": metrics.saldo_cartera_promedio,
            "facturas_abiertas": metrics.facturas_abiertas,
            "facturas_vencidas": metrics.facturas_vencidas,
            "monto_vencido": metrics.monto_vencido,
            "pct_vencido": metrics.pct_vencido,
            "clientes_con_saldo": metrics.clientes_con_saldo,
            "facturas_credito_periodo": metrics.facturas_credito_periodo,
            "facturas_contado_excluidas": metrics.facturas_contado_excluidas,
            "exclude_cash_sales": metrics.exclude_cash_sales,
        },
        "aging": metrics.aging,
        "open_invoices": metrics.open_invoices,
        "scored": scored,
        "alerts": alerts,
        "plan_cobro": plan,
        "proximos_vencer": proximos,
        "raw_invoices": raw["invoices"],
        "raw_payments": raw["payments"],
        "raw_partners": raw["partners"],
        "companies": raw.get("companies"),
        "history": history,
    }


def filter_analysis_by_vendedor(
    data: dict,
    vendedor_user_ids: tuple[int, ...] | None,
    period_days: int = 365,
    exclude_cash_sales: bool = True,
) -> dict:
    """
    Re-filtra el resultado de `compute_full_analysis` para mostrar sólo los
    clientes asignados a los vendedores seleccionados (`res.partner.user_id`).

    Recalcula los KPIs sobre el subset (saldo, rotación, ventas, vencido) y
    también el histórico mensual para que el dashboard sea consistente.

    Si `vendedor_user_ids` es None o vacío, devuelve `data` sin cambios.
    """
    if not vendedor_user_ids:
        return data

    partners = data.get("raw_partners")
    if partners is None or partners.empty or "user_id" not in partners.columns:
        return data

    keep_partner_ids = set(
        partners.loc[partners["user_id"].isin(list(vendedor_user_ids)), "id"]
        .dropna()
        .astype(int)
        .tolist()
    )
    if not keep_partner_ids:
        # Devolvemos un dict vacío-friendly para que la UI muestre "sin datos"
        empty_kpis = {
            "saldo_cartera": 0.0,
            "rotacion_dias": 0.0,
            "dso_ultimos_90": 0.0,
            "rotacion_veces": 0.0,
            "ventas_credito": 0.0,
            "saldo_promedio": 0.0,
            "facturas_abiertas": 0,
            "facturas_vencidas": 0,
            "monto_vencido": 0.0,
            "pct_vencido": 0.0,
            "clientes_con_saldo": 0,
            "facturas_credito_periodo": 0,
            "facturas_contado_excluidas": 0,
            "exclude_cash_sales": exclude_cash_sales,
        }
        out = dict(data)
        out["kpis"] = empty_kpis
        out["open_invoices"] = data["open_invoices"].iloc[0:0]
        out["scored"] = data["scored"].iloc[0:0]
        out["plan_cobro"] = data["plan_cobro"].iloc[0:0]
        out["alerts"] = data["alerts"].iloc[0:0] if not data["alerts"].empty else data["alerts"]
        out["proximos_vencer"] = (
            data["proximos_vencer"].iloc[0:0]
            if not data["proximos_vencer"].empty
            else data["proximos_vencer"]
        )
        out["aging"] = data["aging"].iloc[0:0]
        out["history"] = data["history"].iloc[0:0]
        return out

    inv_full = data["raw_invoices"]
    pay_full = data["raw_payments"]
    open_inv_full = data["open_invoices"]

    inv_f = inv_full[inv_full["partner_id"].isin(keep_partner_ids)] if not inv_full.empty else inv_full
    pay_f = pay_full[pay_full["partner_id"].isin(keep_partner_ids)] if not pay_full.empty else pay_full
    open_inv_f = (
        open_inv_full[open_inv_full["partner_id"].isin(keep_partner_ids)]
        if not open_inv_full.empty
        else open_inv_full
    )

    # KPIs derivados de open_invoices (saldo, vencido, abiertas)
    if not open_inv_f.empty:
        saldo_total = float(open_inv_f["amount_residual_signed"].abs().sum())
        if "esta_vencida" in open_inv_f.columns:
            mask_v = open_inv_f["esta_vencida"]
            monto_vencido = float(open_inv_f.loc[mask_v, "amount_residual_signed"].abs().sum())
            n_vencidas = int(mask_v.sum())
        else:
            monto_vencido = 0.0
            n_vencidas = 0
        n_abiertas = len(open_inv_f)
    else:
        saldo_total = 0.0
        monto_vencido = 0.0
        n_vencidas = 0
        n_abiertas = 0

    # Rotación recalculada sobre el subset (igual fórmula que dashboard global)
    rotation = compute_rotation(
        inv_f,
        cutoff_date=data["cutoff_date"],
        period_days=period_days,
        exclude_cash_sales=exclude_cash_sales,
        open_invoices=open_inv_f,
        payments=pay_f,
    )

    # Histórico mensual recalculado
    history_f = compute_monthly_history(
        invoices=inv_f,
        payments=pay_f,
        months=12,
        cutoff_date=data["cutoff_date"],
        exclude_cash_sales=exclude_cash_sales,
        open_invoices=open_inv_f,
    )

    # Aging recalculado — pasamos payments filtrados para que el due efectivo
    # respete payment_term_name y settlement override (consistente con la
    # metodología de Detalle Cliente).
    from .analyzer import build_aging_report  # local import para evitar ciclo
    aging_f = build_aging_report(
        open_inv_f, data["cutoff_date"], payments=pay_f,
    )

    # Scored, plan, alerts, proximos: filtramos por partner_id
    scored = data["scored"]
    scored_f = scored[scored["partner_id"].isin(keep_partner_ids)] if not scored.empty else scored

    plan = data["plan_cobro"]
    plan_f = plan[plan["partner_id"].isin(keep_partner_ids)] if not plan.empty else plan

    alerts = data["alerts"]
    if not alerts.empty and "partner_id" in alerts.columns:
        alerts_f = alerts[alerts["partner_id"].isin(keep_partner_ids)]
    else:
        alerts_f = alerts

    proximos = data["proximos_vencer"]
    if not proximos.empty and "partner_id" in proximos.columns:
        proximos_f = proximos[proximos["partner_id"].isin(keep_partner_ids)]
    else:
        proximos_f = proximos

    # DSO últimos 90 días sobre el subset filtrado por vendedor
    dso_ultimos_90_f = (
        float(history_f["dso_rolling"].iloc[-1])
        if (history_f is not None and not history_f.empty and "dso_rolling" in history_f.columns)
        else 0.0
    )

    new_kpis = {
        "saldo_cartera": saldo_total,
        "rotacion_dias": rotation["rotacion_dias"],
        "dso_ultimos_90": dso_ultimos_90_f,
        "rotacion_veces": rotation["rotacion_veces"],
        "ventas_credito": rotation["ventas_credito"],
        "saldo_promedio": rotation["saldo_promedio"],
        "facturas_abiertas": n_abiertas,
        "facturas_vencidas": n_vencidas,
        "monto_vencido": monto_vencido,
        "pct_vencido": (monto_vencido / saldo_total * 100) if saldo_total else 0.0,
        "clientes_con_saldo": int((scored_f["saldo_actual"] > 0).sum()) if not scored_f.empty else 0,
        "facturas_credito_periodo": int(rotation.get("facturas_credito", 0)),
        "facturas_contado_excluidas": int(rotation.get("facturas_contado_excluidas", 0)),
        "exclude_cash_sales": exclude_cash_sales,
    }

    out = dict(data)
    out["kpis"] = new_kpis
    out["aging"] = aging_f
    out["open_invoices"] = open_inv_f
    out["scored"] = scored_f
    out["plan_cobro"] = plan_f
    out["alerts"] = alerts_f
    out["proximos_vencer"] = proximos_f
    out["history"] = history_f
    return out


# Versión del schema de invoice_lines. Incrementar cuando se modifique la
# estructura del DataFrame que devuelve `extract_invoice_lines` (nuevas
# columnas, fixes de enriquecimiento, etc.). El número entra en la firma
# de la función cacheada → Streamlit invalida el cache automáticamente
# cuando cambie. Histórico:
#   v1: versión inicial.
#   v2: fix categoría producto (NaN en product_id) + price_subtotal_signed
#       calculado en post-procesamiento (Odoo 19).
#   v3: refactor exclusión productos por default_code exacto (SOAT1, ANTCL).
#   v4: KPIs cambian a amount_untaxed_signed (sin IVA, igual que Odoo).
#   v5: KPIs reconstruyen amount desde líneas (coincidencia exacta).
#   v6: TODOS los reportes (mensual, vendedor, cliente) calculan desde líneas.
#   v7: agregado `product_volume` al esquema (campo `volume` de product.product).
#   v8: agregado `product_standard_price`, `line_cost`, `line_margin` (margen bruto).
#   v9: usar `purchase_price` y `margin_signed` (Enterprise) cuando estén
#       disponibles; fallback a `standard_price` actual. Columnas nuevas:
#       `cost_source`, `margin_source` para trazabilidad.
#   v10: pasar context={'company_id': X} al fetch de productos para
#        resolver standard_price multi-empresa (antes devolvía 0).
INVOICE_LINES_CACHE_VERSION = 11  # v11: active_test=False en product.product (incluir archivados)


@st.cache_data(ttl=900, show_spinner="Descargando líneas de factura (productos)...")
def load_invoice_lines(
    months_back: int = 12,
    company_ids: tuple[int, ...] | None = None,
    _cache_v: int = INVOICE_LINES_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Descarga líneas de factura (account.move.line con producto) para el
    informe de ventas por producto / categoría.

    ⚠️  Anclado a la fecha de FACTURACIÓN (account.move.line.date, que en
    Odoo coincide con invoice_date del move padre). NO usa date_order
    (ese campo vive en sale.order y no aplica aquí).

    Cache 15 min como el resto del pipeline. El parámetro `_cache_v`
    sirve de cache-buster: cuando se incremente `INVOICE_LINES_CACHE_VERSION`
    el cache se invalida automáticamente sin necesidad de reboot manual.
    """
    from datetime import date as _date, timedelta
    client = get_odoo_client()
    cutoff = _date.today()
    date_from = cutoff - timedelta(days=30 * months_back)
    return extract_invoice_lines(
        client,
        date_from=date_from,
        date_to=cutoff,
        company_ids=list(company_ids) if company_ids else None,
        include_refunds=True,
    )


# ---------------------------------------------------------------------------
# Loaders para análisis de Compras vs Ventas
# ---------------------------------------------------------------------------

PURCHASE_LINES_CACHE_VERSION = 2  # v2: active_test=False en product.product
STOCK_QUANTS_CACHE_VERSION = 1


@st.cache_data(ttl=900, show_spinner="Descargando facturas de proveedor...")
def load_purchase_invoice_lines(
    date_from: str,
    date_to: str,
    company_ids: tuple[int, ...] | None = None,
    _cache_v: int = PURCHASE_LINES_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Líneas de facturas de proveedor (in_invoice / in_refund) para el
    análisis de compras vs ventas.
    """
    from .extractor import extract_purchase_invoice_lines
    client = get_odoo_client()
    df_date = pd.to_datetime(date_from).date()
    dt_date = pd.to_datetime(date_to).date()
    return extract_purchase_invoice_lines(
        client,
        date_from=df_date,
        date_to=dt_date,
        company_ids=list(company_ids) if company_ids else None,
        include_refunds=True,
    )


@st.cache_data(ttl=900, show_spinner="Cargando stock por producto...")
def load_stock_quants(
    company_ids: tuple[int, ...] | None = None,
    product_ids: tuple[int, ...] | None = None,
    _cache_v: int = STOCK_QUANTS_CACHE_VERSION,
) -> "pd.DataFrame":
    """Stock disponible por producto (read_group server-side)."""
    from .extractor import extract_stock_quants
    client = get_odoo_client()
    return extract_stock_quants(
        client,
        company_ids=list(company_ids) if company_ids else None,
        product_ids=list(product_ids) if product_ids else None,
    )


# ---------------------------------------------------------------------------
# Loaders para Estados Financieros
# ---------------------------------------------------------------------------

ACCOUNT_MOVEMENTS_CACHE_VERSION = 2  # Cargar solo período seleccionado, no histórico completo


@st.cache_data(ttl=900, show_spinner="Descargando plan de cuentas...")
def load_chart_of_accounts(
    company_ids: tuple[int, ...] | None = None,
    _cache_v: int = 3,  # bump: forzar recarga con account_type
) -> "pd.DataFrame":
    """Descarga plan de cuentas (account.account)."""
    from .extractor import extract_chart_of_accounts
    client = get_odoo_client()
    return extract_chart_of_accounts(
        client, company_ids=list(company_ids) if company_ids else None
    )


@st.cache_data(ttl=900, show_spinner="Descargando movimientos contables...")
def load_account_movements(
    months_back: int = 12,
    company_ids: tuple[int, ...] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    _cache_v: int = ACCOUNT_MOVEMENTS_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Descarga movimientos contables (account.move.line) en el rango.

    Si se pasan `date_from` y `date_to` (strings YYYY-MM-DD), usa ese rango
    exacto. Si no, calcula desde `months_back` hacia atrás.
    """
    from datetime import date as _date, timedelta
    from .extractor import extract_account_movements
    client = get_odoo_client()
    if date_from and date_to:
        df_date = pd.to_datetime(date_from).date()
        dt_date = pd.to_datetime(date_to).date()
    else:
        cutoff = _date.today()
        df_date = cutoff - timedelta(days=30 * months_back)
        dt_date = cutoff
    return extract_account_movements(
        client,
        date_from=df_date,
        date_to=dt_date,
        company_ids=list(company_ids) if company_ids else None,
    )


@st.cache_data(ttl=900, show_spinner="Calculando saldos contables...")
def load_account_balances_aggregated(
    date_to: str,
    company_ids: tuple[int, ...] | None = None,
    date_from: str | None = None,
    _cache_v: int = ACCOUNT_MOVEMENTS_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Saldo agregado por cuenta usando `read_group` server-side.

    Si se pasa `date_from`, agrega solo movimientos del rango (para P&L).
    Si no, agrega TODO hasta date_to (para Balance General histórico).
    """
    client = get_odoo_client()
    domain: list = [
        ("parent_state", "=", "posted"),
        ("date", "<=", date_to),
    ]
    if date_from:
        domain.append(("date", ">=", date_from))
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))
    try:
        result = client.execute_kw(
            "account.move.line", "read_group",
            [domain, ["account_id", "debit:sum", "credit:sum"], ["account_id"]],
            {"lazy": False},
        )
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["account_id", "debit", "credit"])

    rows = []
    for r in result:
        acc = r.get("account_id")
        if isinstance(acc, list) and acc:
            acc_id = acc[0]
        else:
            acc_id = acc
        rows.append({
            "account_id": acc_id,
            "debit": float(r.get("debit", 0) or 0),
            "credit": float(r.get("credit", 0) or 0),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=900, show_spinner="Calculando saldos mensuales...")
def load_account_balances_monthly(
    date_from: str,
    date_to: str,
    company_ids: tuple[int, ...] | None = None,
    _cache_v: int = ACCOUNT_MOVEMENTS_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Saldo agregado por cuenta Y por MES en el rango. Usa read_group con
    groupby por mes — server-side, muy rápido.

    Ideal para Estado de Resultados mensual (Comparativos tab).
    """
    client = get_odoo_client()
    domain: list = [
        ("parent_state", "=", "posted"),
        ("date", ">=", date_from),
        ("date", "<=", date_to),
    ]
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))
    try:
        result = client.execute_kw(
            "account.move.line", "read_group",
            [
                domain,
                ["account_id", "date", "debit:sum", "credit:sum"],
                ["account_id", "date:month"],
            ],
            {"lazy": False},
        )
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["account_id", "mes", "debit", "credit"])

    rows = []
    for r in result:
        acc = r.get("account_id")
        if isinstance(acc, list) and acc:
            acc_id = acc[0]
        else:
            acc_id = acc
        mes_str = r.get("date:month")  # formato "January 2026" o similar
        rows.append({
            "account_id": acc_id,
            "mes": mes_str,
            "debit": float(r.get("debit", 0) or 0),
            "credit": float(r.get("credit", 0) or 0),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=900, show_spinner="Cargando movimientos de caja...")
def load_cash_movements_only(
    date_from: str,
    date_to: str,
    company_ids: tuple[int, ...] | None = None,
    cash_account_ids: tuple[int, ...] | None = None,
    _cache_v: int = ACCOUNT_MOVEMENTS_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Trae SOLO movimientos de las cuentas de disponible (caja/bancos).
    Mucho más rápido que cargar todas las líneas. Usado para Flujo de Efectivo.
    """
    from .extractor import extract_account_movements
    client = get_odoo_client()
    df_date = pd.to_datetime(date_from).date()
    dt_date = pd.to_datetime(date_to).date()

    moves = extract_account_movements(
        client,
        date_from=df_date,
        date_to=dt_date,
        company_ids=list(company_ids) if company_ids else None,
    )
    if cash_account_ids and not moves.empty and "account_id" in moves.columns:
        moves = moves[moves["account_id"].isin(list(cash_account_ids))]
    return moves


@st.cache_data(ttl=900, show_spinner="Cargando movimientos de gastos...")
def load_expense_movements(
    date_from: str,
    date_to: str,
    expense_account_ids: tuple[int, ...],
    company_ids: tuple[int, ...] | None = None,
    _cache_v: int = ACCOUNT_MOVEMENTS_CACHE_VERSION,
) -> "pd.DataFrame":
    """
    Trae account.move.line filtrado a cuentas de gasto (grupo 5) y rango.

    Mucho más rápido que cargar TODOS los movimientos: solo trae líneas
    asociadas a las cuentas de gasto específicas que pasamos. Esto permite
    desglose por mes real, por tercero y drill-down a movimientos.
    """
    from .extractor import extract_account_movements
    if not expense_account_ids:
        return pd.DataFrame()
    client = get_odoo_client()
    df_date = pd.to_datetime(date_from).date()
    dt_date = pd.to_datetime(date_to).date()

    # Pasamos el domain manualmente para incluir el filtro de account_id
    # (extract_account_movements no acepta ese filtro directo, así que
    # cargamos todo el rango y luego filtramos in-memory; pero ojo:
    # un rango grande puede ser lento. Mejoramos llamando directo a
    # search_read con el dominio completo.)
    domain: list = [
        ("parent_state", "=", "posted"),
        ("date", ">=", df_date.isoformat()),
        ("date", "<=", dt_date.isoformat()),
        ("account_id", "in", list(expense_account_ids)),
    ]
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))

    fields = [
        "id", "account_id", "partner_id", "move_id",
        "date", "name", "ref",
        "debit", "credit", "balance",
        "company_id", "journal_id",
    ]
    try:
        all_fields_meta = client.fields_get(
            "account.move.line", attributes=["string"]
        )
        available = set(all_fields_meta.keys())
        fields = [f for f in fields if f in available]
    except Exception:  # noqa: BLE001
        pass

    try:
        records = client.search_read(
            "account.move.line",
            domain=domain,
            fields=fields,
            order="date asc, id asc",
        )
    except Exception:  # noqa: BLE001
        return pd.DataFrame()

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    # Desempaquetar many2ones
    def _m2o_id(v):
        return v[0] if isinstance(v, list) and v else None
    def _m2o_name(v):
        return v[1] if isinstance(v, list) and len(v) > 1 else None
    for col in ("account_id", "partner_id", "move_id", "company_id", "journal_id"):
        if col in df.columns:
            df[col + "_name"] = df[col].apply(_m2o_name)
            df[col] = df[col].apply(_m2o_id)
    for c in ("debit", "credit", "balance"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df


@st.cache_data(ttl=900, show_spinner="Calculando saldo de cartera...")
def load_cartera_summary(
    company_ids: tuple[int, ...] | None = None,
    _cache_v: int = 1,
) -> dict:
    """
    Saldo de cartera (CxC) vía read_group server-side — rápido.

    NO hace análisis completo (scoring, DSO per-cliente, etc.). Solo trae
    el total de saldo abierto y vencido para calcular DSO a nivel empresa.

    Devuelve dict con:
      - saldo_cartera: total amount_residual de facturas no pagadas
      - saldo_vencido: total de facturas vencidas
      - n_facturas_abiertas: # de facturas con saldo
    """
    from datetime import date as _date
    client = get_odoo_client()
    today_str = _date.today().isoformat()

    domain_base = [
        ("move_type", "=", "out_invoice"),
        ("state", "=", "posted"),
        ("payment_state", "in", ["not_paid", "partial", "in_payment"]),
    ]
    if company_ids:
        domain_base = domain_base + [("company_id", "in", list(company_ids))]

    try:
        # Saldo total abierto
        result = client.execute_kw(
            "account.move", "read_group",
            [domain_base, ["amount_residual_signed:sum"], []],
            {"lazy": False},
        )
        saldo_cartera = float(
            result[0].get("amount_residual_signed", 0) if result else 0
        )
        n_abiertas = int(result[0].get("__count", 0) if result else 0)

        # Saldo vencido (invoice_date_due < hoy)
        domain_vencido = domain_base + [("invoice_date_due", "<", today_str)]
        result_v = client.execute_kw(
            "account.move", "read_group",
            [domain_vencido, ["amount_residual_signed:sum"], []],
            {"lazy": False},
        )
        saldo_vencido = float(
            result_v[0].get("amount_residual_signed", 0) if result_v else 0
        )
    except Exception:  # noqa: BLE001
        return {
            "saldo_cartera": 0.0, "saldo_vencido": 0.0,
            "n_facturas_abiertas": 0,
        }

    return {
        "saldo_cartera": saldo_cartera,
        "saldo_vencido": saldo_vencido,
        "n_facturas_abiertas": n_abiertas,
    }


def test_connection_summary() -> dict:
    """Para mostrar en la UI: estado de conexión a Odoo."""
    try:
        load_environment()
        client = OdooClient.from_env()
        return client.test_connection()
    except OdooConnectionError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:
        return {"status": "error", "error": f"Error inesperado: {exc}"}
