# -*- coding: utf-8 -*-
"""
Motor del Estado de Caja diario.

Construye la estructura del informe de Caja al estilo Casa de los Mineros
(replica del PDF SysCafé.ESTACAJ): para cada cuenta `1105.*` agrupa los
movimientos del día por tipo de comprobante (journal) con sus saldos
inicial y final.

Entrada: chart of accounts + movimientos del día + diccionario de saldos
iniciales (saldo al cierre del día anterior por cada account_id).
"""
from __future__ import annotations

import pandas as pd

# Cuentas del PUC colombiano que representan caja física.
CASH_PREFIX = "1105"
TOL = 0.01


def get_cash_accounts(chart_df: pd.DataFrame) -> pd.DataFrame:
    """Cuentas del plan cuyo código empieza con `1105` (caja física)."""
    cols = ["id", "code", "name"]
    if chart_df is None or chart_df.empty:
        return pd.DataFrame(columns=cols)
    c = chart_df.copy()
    c["code"] = c["code"].astype(str)
    return (
        c[c["code"].str.startswith(CASH_PREFIX)][cols]
        .sort_values("code")
        .reset_index(drop=True)
        .copy()
    )


def _partner_y_referencia(ln) -> tuple[str, str]:
    """Devuelve (contacto, referencia) por separado para cada línea."""
    partner = (ln.get("partner_id_name") or "").strip()
    desc = (ln.get("name") or ln.get("ref") or "").strip()
    return partner, desc


def compute_estado_caja(
    chart_df: pd.DataFrame,
    moves_dia: pd.DataFrame,
    saldo_inicial: dict[int, float] | None,
    fecha,
) -> dict:
    """
    Construye el Estado de Caja para `fecha`.

    Devuelve un dict con la estructura:
      {
        "fecha": date,
        "cuentas": [
          {
            "id": int, "code": str, "name": str,
            "saldo_inicial", "debitos", "creditos",
            "saldo_final", "flujo_neto",
            "grupos": [
              {"journal_id": int, "journal_name": str, "subtotal": float,
               "lineas": [{"comprobante", "detalle", "valor"}]}
            ],
          },
          ...
        ],
        "resumen_cuentas":      DataFrame(code, name, saldo_inicial,
                                          debitos, creditos, saldo_final),
        "resumen_formas_pago":  DataFrame(detalle, valor),
        "total_flujo": float,
      }
    """
    saldo_inicial = saldo_inicial or {}
    cash_accs = get_cash_accounts(chart_df)
    out: dict = {
        "fecha": fecha,
        "cuentas": [],
        "resumen_cuentas": pd.DataFrame(),
        "resumen_formas_pago": pd.DataFrame(),
        "total_flujo": 0.0,
    }
    if cash_accs.empty:
        return out

    cash_ids = set(int(x) for x in cash_accs["id"].tolist())

    # Filtrar movimientos del día solo a cuentas de caja
    m = moves_dia.copy() if moves_dia is not None else pd.DataFrame()
    if not m.empty and "account_id" in m.columns:
        m["account_id"] = pd.to_numeric(m["account_id"], errors="coerce")
        m = m[m["account_id"].isin(cash_ids)].copy()
        # Tipos numéricos defensivos
        for col in ("debit", "credit"):
            if col in m.columns:
                m[col] = pd.to_numeric(m[col], errors="coerce").fillna(0.0)

    total_flujo = 0.0
    for _, acc in cash_accs.iterrows():
        acc_id = int(acc["id"])
        si = float(saldo_inicial.get(acc_id, 0.0))

        m_acc = m[m["account_id"] == acc_id] if not m.empty else pd.DataFrame()
        debitos = float(m_acc["debit"].sum()) if not m_acc.empty else 0.0
        creditos = float(m_acc["credit"].sum()) if not m_acc.empty else 0.0
        flujo = debitos - creditos
        sf = si + flujo

        # Agrupar movimientos por journal (tipo de comprobante)
        grupos: list[dict] = []
        if not m_acc.empty:
            # Orden estable: por id del journal, manteniendo orden de aparición.
            grouped = m_acc.groupby(
                ["journal_id", "journal_id_name"],
                dropna=False, sort=False,
            )
            for (j_id, j_name), g in grouped:
                lineas = []
                subtotal = 0.0
                # Orden interno: por id de línea (cronológico)
                g_sorted = g.sort_values("id") if "id" in g.columns else g
                for _, ln in g_sorted.iterrows():
                    valor = (
                        float(ln.get("debit", 0) or 0)
                        - float(ln.get("credit", 0) or 0)
                    )
                    subtotal += valor
                    contacto, referencia = _partner_y_referencia(ln)
                    lineas.append({
                        "comprobante": str(ln.get("move_id_name") or ""),
                        "contacto": contacto,
                        "referencia": referencia,
                        "valor": valor,
                    })
                grupos.append({
                    "journal_id": int(j_id) if pd.notna(j_id) else None,
                    "journal_name": str(j_name) if j_name else "",
                    "subtotal": subtotal,
                    "lineas": lineas,
                })

        # Solo se reporta la cuenta si tiene saldo o movimientos.
        if abs(si) > TOL or abs(sf) > TOL or abs(flujo) > TOL:
            out["cuentas"].append({
                "id": acc_id,
                "code": str(acc["code"]),
                "name": str(acc["name"]),
                "saldo_inicial": si,
                "debitos": debitos,
                "creditos": creditos,
                "saldo_final": sf,
                "flujo_neto": flujo,
                "grupos": grupos,
            })
            total_flujo += flujo

    # Resumen por cuenta
    out["resumen_cuentas"] = pd.DataFrame([
        {
            "code": c["code"], "name": c["name"],
            "saldo_inicial": c["saldo_inicial"],
            "debitos": c["debitos"],
            "creditos": c["creditos"],
            "saldo_final": c["saldo_final"],
        }
        for c in out["cuentas"]
    ])

    # Resumen por formas de pago (= flujo neto por cuenta)
    out["resumen_formas_pago"] = pd.DataFrame([
        {"detalle": f"{c['code']} {c['name']}", "valor": c["flujo_neto"]}
        for c in out["cuentas"] if abs(c["flujo_neto"]) > TOL
    ])

    out["total_flujo"] = total_flujo
    return out


def build_saldo_inicial_dict(
    balances_df: pd.DataFrame, cash_account_ids: set[int],
) -> dict[int, float]:
    """
    Convierte el DataFrame de `load_account_balances_aggregated`
    (con columnas account_id + debit/credit acumulados) en un dict
    {account_id: saldo} para las cuentas de caja.
    """
    out: dict[int, float] = {}
    if balances_df is None or balances_df.empty:
        return out
    b = balances_df.copy()
    if "account_id" not in b.columns:
        return out
    b["account_id"] = pd.to_numeric(b["account_id"], errors="coerce")
    b = b[b["account_id"].isin(cash_account_ids)]
    for col in ("debit", "credit"):
        if col in b.columns:
            b[col] = pd.to_numeric(b[col], errors="coerce").fillna(0.0)
    if "debit" in b.columns and "credit" in b.columns:
        b["saldo"] = b["debit"] - b["credit"]
    elif "balance" in b.columns:
        b["saldo"] = pd.to_numeric(b["balance"], errors="coerce").fillna(0.0)
    else:
        return out
    g = b.groupby("account_id", as_index=False)["saldo"].sum()
    for _, r in g.iterrows():
        out[int(r["account_id"])] = float(r["saldo"])
    return out
