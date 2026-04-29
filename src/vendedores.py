# -*- coding: utf-8 -*-
"""
KPIs por vendedor (responsable comercial asignado al cliente).

Entrega un DataFrame con una fila por vendedor con métricas de cartera, hábito
de pago y ventas, listo para mostrar como comparativo en la UI.

Vendedor = `res.partner.user_id` (el comercial asignado al cliente). Los
clientes sin vendedor se agrupan en "Sin asignar".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .analyzer import _is_credit_sale
from .config import get_data_floor_date


SIN_ASIGNAR_LABEL = "Sin asignar"


def _attach_user_id(df: pd.DataFrame, partners: pd.DataFrame) -> pd.DataFrame:
    """
    Une la columna `user_id` (vendedor del cliente) al DataFrame por
    `partner_id`. Si `df` ya tiene `user_id`, la respeta.
    """
    if df is None or df.empty:
        return df
    if "user_id" in df.columns:
        return df.copy()
    if partners is None or partners.empty or "user_id" not in partners.columns:
        out = df.copy()
        out["user_id"] = np.nan
        return out
    pmap = partners[["id", "user_id"]].rename(columns={"id": "partner_id"})
    out = df.merge(pmap, on="partner_id", how="left")
    return out


def _wavg(values: pd.Series, weights: pd.Series) -> float:
    """Promedio ponderado robusto. Devuelve 0 cuando no hay peso."""
    v = pd.to_numeric(values, errors="coerce").fillna(0.0)
    w = pd.to_numeric(weights, errors="coerce").fillna(0.0).abs()
    total = w.sum()
    if total <= 0:
        return 0.0
    return float((v * w).sum() / total)


def compute_kpis_por_vendedor(
    scored: pd.DataFrame,
    open_invoices: pd.DataFrame,
    raw_invoices: pd.DataFrame,
    partners: pd.DataFrame,
    plan_cobro: pd.DataFrame | None = None,
    cutoff_date: date | None = None,
    period_days: int = 365,
    exclude_cash_sales: bool = True,
) -> pd.DataFrame:
    """
    Calcula KPIs comparativos por vendedor.

    Devuelve un DataFrame con columnas (una fila por vendedor):
        user_id, user_name,
        num_clientes_total, num_clientes_con_saldo,
        saldo_total, monto_vencido, pct_vencido,
        dso_ponderado, mora_prom_ponderada, pct_pagado_a_tiempo_ponderado,
        score_prom_ponderado,
        num_a, num_b, num_c, num_d, num_sin_hist,
        num_urgente, num_alta, num_media,
        num_facturas_abiertas, num_facturas_vencidas,
        num_facturas_vencidas_90,
        ventas_credito_periodo, num_facturas_periodo,
        ticket_promedio,
        plazo_otorgado_promedio,
    """
    cutoff = pd.Timestamp(cutoff_date or datetime.now().date())
    floor_ts = pd.Timestamp(get_data_floor_date())
    period_start = max(cutoff - pd.Timedelta(days=period_days), floor_ts)

    # 1) Scored con vendedor
    sc = _attach_user_id(scored, partners) if scored is not None else None
    if sc is None or sc.empty:
        return pd.DataFrame(
            columns=[
                "user_id", "user_name", "num_clientes_total",
                "num_clientes_con_saldo", "saldo_total", "monto_vencido",
                "pct_vencido", "dso_ponderado", "mora_prom_ponderada",
                "pct_pagado_a_tiempo_ponderado", "score_prom_ponderado",
                "num_a", "num_b", "num_c", "num_d", "num_sin_hist",
                "num_urgente", "num_alta", "num_media",
                "num_facturas_abiertas", "num_facturas_vencidas",
                "num_facturas_vencidas_90",
                "ventas_credito_periodo", "num_facturas_periodo",
                "ticket_promedio", "plazo_otorgado_promedio",
            ]
        )

    sc = sc.copy()
    sc["user_id"] = sc["user_id"].fillna(0).astype(int)

    # Mapa user_id -> nombre
    if partners is not None and not partners.empty and "user_name" in partners.columns:
        name_map = (
            partners.dropna(subset=["user_id"])
            .drop_duplicates(subset=["user_id"])
            .set_index("user_id")["user_name"]
            .to_dict()
        )
    else:
        name_map = {}

    # 2) Aggregar scored por vendedor
    grp = sc.groupby("user_id", dropna=False)
    base = grp.agg(
        num_clientes_total=("partner_id", "nunique"),
        saldo_total=("saldo_actual", "sum"),
        monto_vencido=("monto_vencido", "sum"),
    ).reset_index()
    con_saldo = (
        sc.assign(_con_saldo=(sc["saldo_actual"] > 0).astype(int))
        .groupby("user_id", dropna=False)["_con_saldo"]
        .sum()
        .rename("num_clientes_con_saldo")
        .reset_index()
    )
    base = base.merge(con_saldo, on="user_id", how="left")
    base["num_clientes_con_saldo"] = base["num_clientes_con_saldo"].fillna(0).astype(int)
    base["pct_vencido"] = np.where(
        base["saldo_total"] > 0,
        base["monto_vencido"] / base["saldo_total"] * 100,
        0.0,
    )

    # Ponderados (DSO, mora, % a tiempo, score) por saldo_actual
    pond_rows = []
    for uid, g in grp:
        pond_rows.append({
            "user_id": uid,
            "dso_ponderado": _wavg(g.get("dso_cliente", pd.Series([0])), g["saldo_actual"]),
            "mora_prom_ponderada": _wavg(
                g.get("dias_sobre_plazo", pd.Series([0])), g["saldo_actual"]
            ),
            "pct_pagado_a_tiempo_ponderado": _wavg(
                g.get("pct_pagado_a_tiempo", pd.Series([0])), g["saldo_actual"]
            ),
            "score_prom_ponderado": _wavg(
                g.get("score_total", pd.Series([0])), g["saldo_actual"]
            ),
            "plazo_otorgado_promedio": _wavg(
                g.get("plazo_promedio_dias", pd.Series([0])), g["saldo_actual"]
            ),
        })
    pond_df = pd.DataFrame(pond_rows)

    # Distribución A/B/C/D
    if "calificacion" in sc.columns:
        cnt = (
            sc.groupby(["user_id", "calificacion"]).size().unstack(fill_value=0)
        )
        for c in ["A", "B", "C", "D", "SIN_HISTORICO"]:
            if c not in cnt.columns:
                cnt[c] = 0
        cnt = cnt.rename(
            columns={
                "A": "num_a", "B": "num_b", "C": "num_c", "D": "num_d",
                "SIN_HISTORICO": "num_sin_hist",
            }
        ).reset_index()
        cnt = cnt[["user_id", "num_a", "num_b", "num_c", "num_d", "num_sin_hist"]]
    else:
        cnt = pd.DataFrame(
            {"user_id": base["user_id"]}
        ).assign(num_a=0, num_b=0, num_c=0, num_d=0, num_sin_hist=0)

    # 3) Plan de cobro: prioridades por vendedor
    if plan_cobro is not None and not plan_cobro.empty:
        plan_v = _attach_user_id(plan_cobro, partners)
        if plan_v is not None and not plan_v.empty and "prioridad" in plan_v.columns:
            plan_v["user_id"] = plan_v["user_id"].fillna(0).astype(int)
            prio = (
                plan_v.groupby(["user_id", "prioridad"]).size().unstack(fill_value=0)
            )
            for p in ["URGENTE", "ALTA", "MEDIA"]:
                if p not in prio.columns:
                    prio[p] = 0
            prio = prio.rename(
                columns={
                    "URGENTE": "num_urgente",
                    "ALTA": "num_alta",
                    "MEDIA": "num_media",
                }
            ).reset_index()
            prio = prio[["user_id", "num_urgente", "num_alta", "num_media"]]
        else:
            prio = pd.DataFrame(
                {"user_id": base["user_id"]}
            ).assign(num_urgente=0, num_alta=0, num_media=0)
    else:
        prio = pd.DataFrame(
            {"user_id": base["user_id"]}
        ).assign(num_urgente=0, num_alta=0, num_media=0)

    # 4) Facturas abiertas / vencidas / >90d
    if open_invoices is not None and not open_invoices.empty:
        oi = _attach_user_id(open_invoices, partners)
        oi["user_id"] = oi["user_id"].fillna(0).astype(int)
        if "esta_vencida" in oi.columns:
            mask_v = oi["esta_vencida"]
        elif "dias_vencido" in oi.columns:
            mask_v = oi["dias_vencido"] > 0
        else:
            mask_v = pd.Series(False, index=oi.index)
        if "dias_vencido" in oi.columns:
            mask_90 = oi["dias_vencido"] > 90
        else:
            mask_90 = pd.Series(False, index=oi.index)

        fac = (
            oi.groupby("user_id")
            .agg(
                num_facturas_abiertas=("id", "count")
                if "id" in oi.columns
                else ("partner_id", "count"),
            )
            .reset_index()
        )
        venc = (
            oi[mask_v].groupby("user_id").size().rename("num_facturas_vencidas")
            .reset_index()
        )
        venc90 = (
            oi[mask_90].groupby("user_id").size().rename("num_facturas_vencidas_90")
            .reset_index()
        )
        fac = fac.merge(venc, on="user_id", how="left").merge(venc90, on="user_id", how="left")
        fac[["num_facturas_vencidas", "num_facturas_vencidas_90"]] = (
            fac[["num_facturas_vencidas", "num_facturas_vencidas_90"]].fillna(0).astype(int)
        )
    else:
        fac = pd.DataFrame(
            {"user_id": base["user_id"]}
        ).assign(num_facturas_abiertas=0, num_facturas_vencidas=0, num_facturas_vencidas_90=0)

    # 5) Ventas a crédito en el período por vendedor
    if raw_invoices is not None and not raw_invoices.empty:
        inv = _attach_user_id(raw_invoices, partners)
        inv["user_id"] = inv["user_id"].fillna(0).astype(int)
        inv["invoice_date"] = pd.to_datetime(inv["invoice_date"], errors="coerce")
        is_credit = _is_credit_sale(inv)
        pool = inv[is_credit] if exclude_cash_sales else inv
        sales_mask = (
            (pool["move_type"] == "out_invoice")
            & (pool["invoice_date"] >= period_start)
            & (pool["invoice_date"] <= cutoff)
        )
        sales = pool[sales_mask].copy()
        sales["amount"] = pd.to_numeric(
            sales["amount_total_signed"], errors="coerce"
        ).fillna(0.0).abs()
        ven = (
            sales.groupby("user_id")
            .agg(
                ventas_credito_periodo=("amount", "sum"),
                num_facturas_periodo=("amount", "count"),
            )
            .reset_index()
        )
        ven["ticket_promedio"] = np.where(
            ven["num_facturas_periodo"] > 0,
            ven["ventas_credito_periodo"] / ven["num_facturas_periodo"],
            0.0,
        )
    else:
        ven = pd.DataFrame(
            {"user_id": base["user_id"]}
        ).assign(ventas_credito_periodo=0.0, num_facturas_periodo=0, ticket_promedio=0.0)

    # 6) Merge final
    out = base.merge(pond_df, on="user_id", how="left")
    out = out.merge(cnt, on="user_id", how="left")
    out = out.merge(prio, on="user_id", how="left")
    out = out.merge(fac, on="user_id", how="left")
    out = out.merge(ven, on="user_id", how="left")

    for col in [
        "num_a", "num_b", "num_c", "num_d", "num_sin_hist",
        "num_urgente", "num_alta", "num_media",
        "num_facturas_abiertas", "num_facturas_vencidas", "num_facturas_vencidas_90",
        "num_facturas_periodo",
    ]:
        if col in out.columns:
            out[col] = out[col].fillna(0).astype(int)
    for col in [
        "dso_ponderado", "mora_prom_ponderada",
        "pct_pagado_a_tiempo_ponderado", "score_prom_ponderado",
        "plazo_otorgado_promedio", "ventas_credito_periodo", "ticket_promedio",
    ]:
        if col in out.columns:
            out[col] = out[col].fillna(0.0)

    # Nombre del vendedor
    out["user_name"] = out["user_id"].map(name_map)
    out["user_name"] = out["user_name"].where(
        out["user_id"] != 0, SIN_ASIGNAR_LABEL
    ).fillna(SIN_ASIGNAR_LABEL)

    # KPI derivado: % cartera urgente (sobre saldo)
    out["pct_cartera_urgente"] = np.where(
        out["num_clientes_con_saldo"] > 0,
        out["num_urgente"] / out["num_clientes_con_saldo"] * 100,
        0.0,
    )

    # Orden por saldo descendente
    out = out.sort_values("saldo_total", ascending=False).reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Observaciones / puntos a mejorar (heurísticas)
# ---------------------------------------------------------------------------


def generate_observaciones(kpis: pd.DataFrame) -> list[dict]:
    """
    Genera una lista de observaciones interpretables a partir del comparativo.

    Cada item: {nivel, vendedor, mensaje}. Niveles: "ok", "warning", "critical".
    """
    if kpis is None or kpis.empty:
        return []
    obs: list[dict] = []

    # Promedios globales (referencias)
    saldo_total = float(kpis["saldo_total"].sum())
    pct_vencido_global = (
        float(kpis["monto_vencido"].sum()) / saldo_total * 100
        if saldo_total > 0
        else 0.0
    )
    dso_global = _wavg(kpis["dso_ponderado"], kpis["saldo_total"])
    mora_global = _wavg(kpis["mora_prom_ponderada"], kpis["saldo_total"])

    # Por vendedor
    for _, r in kpis.iterrows():
        vend = r["user_name"]
        pct_v = float(r.get("pct_vencido", 0))
        dso_v = float(r.get("dso_ponderado", 0))
        mora_v = float(r.get("mora_prom_ponderada", 0))
        n_d = int(r.get("num_d", 0))
        n_urg = int(r.get("num_urgente", 0))
        n_v90 = int(r.get("num_facturas_vencidas_90", 0))
        n_clientes = int(r.get("num_clientes_con_saldo", 0))

        # Crítico
        if pct_v > 35:
            obs.append({
                "nivel": "critical",
                "vendedor": vend,
                "mensaje": (
                    f"📉 **{pct_v:.0f}% de su cartera está vencida** "
                    f"(global: {pct_vencido_global:.0f}%). Riesgo alto de incobrables."
                ),
            })
        if n_v90 >= 5:
            obs.append({
                "nivel": "critical",
                "vendedor": vend,
                "mensaje": (
                    f"⏳ **{n_v90} facturas con más de 90 días** vencidas. "
                    "Considerar provisión o gestión jurídica."
                ),
            })
        if n_d >= max(3, int(n_clientes * 0.15)):
            obs.append({
                "nivel": "critical",
                "vendedor": vend,
                "mensaje": (
                    f"🔴 **{n_d} clientes en calificación D**. "
                    "Revisar política de crédito otorgado a este vendedor."
                ),
            })

        # Advertencia
        if dso_v > dso_global + 10 and dso_global > 0:
            obs.append({
                "nivel": "warning",
                "vendedor": vend,
                "mensaje": (
                    f"⏱️ DSO de **{dso_v:.0f} días** vs global {dso_global:.0f}. "
                    "Sus clientes tardan más en pagar — reforzar gestión preventiva."
                ),
            })
        if mora_v > mora_global + 5 and mora_global > -100:
            obs.append({
                "nivel": "warning",
                "vendedor": vend,
                "mensaje": (
                    f"📅 Mora promedio de **{mora_v:+.0f} días** vs global "
                    f"{mora_global:+.0f}. Pago tardío sostenido."
                ),
            })
        if n_urg >= 3:
            obs.append({
                "nivel": "warning",
                "vendedor": vend,
                "mensaje": (
                    f"📞 **{n_urg} clientes URGENTES** sin gestionar. "
                    "Priorizar contactos esta semana."
                ),
            })

        # Positivo
        if pct_v < 10 and n_clientes >= 3:
            obs.append({
                "nivel": "ok",
                "vendedor": vend,
                "mensaje": (
                    f"✅ Cartera muy saludable ({pct_v:.0f}% vencido, "
                    f"DSO {dso_v:.0f}d). Buen ejemplo a replicar."
                ),
            })

    return obs
