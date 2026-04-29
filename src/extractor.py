# -*- coding: utf-8 -*-
"""
Extractor de datos de cartera desde Odoo 19.

Funciones para descargar facturas, pagos, partidas conciliadas y clientes,
y devolverlos como DataFrames de pandas listos para analizar.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd

from .config import clamp_date_from, get_data_floor_date
from .odoo_client import OdooClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Campos que vamos a extraer de cada modelo
# ---------------------------------------------------------------------------

INVOICE_FIELDS = [
    "id",
    "name",
    "partner_id",
    "invoice_date",
    "invoice_date_due",
    "date",
    "invoice_payment_term_id",
    "amount_untaxed_signed",
    "amount_total_signed",
    "amount_residual_signed",
    "currency_id",
    "state",
    "payment_state",
    "move_type",
    "ref",
    "journal_id",
    "company_id",
    "user_id",  # Vendedor/responsable
    "team_id",
    "invoice_user_id",
]

PARTNER_FIELDS = [
    "id",
    "name",
    "vat",
    "ref",
    "email",
    "phone",
    "mobile",
    "street",
    "city",
    "country_id",
    "customer_rank",
    "credit",
    "credit_limit",
    # Booleano que activa el uso del límite de crédito en Odoo. Si está en
    # False, Odoo no controla el límite aunque el campo `credit_limit` tenga
    # un valor. Lo traemos para mostrarlo en la UI.
    "use_partner_credit_limit",
    # Campo nativo de Odoo (Enterprise) — período medio de cobro (DSO) que
    # calcula Odoo internamente. Lo usamos para comparar contra nuestro DSO.
    # Si el módulo que lo provee no está instalado, _resolve_partner_fields
    # lo omitirá automáticamente.
    "days_sales_outstanding",
    "property_payment_term_id",
    "category_id",
    "user_id",
    "create_date",
    "active",
]

# Cache de campos válidos de res.partner por base de datos (XML-RPC fields_get).
_PARTNER_FIELDS_CACHE: dict[str, list[str]] = {}

PAYMENT_FIELDS = [
    "id",
    "name",
    "partner_id",
    "date",
    "amount",
    "amount_signed",
    "payment_type",
    "state",
    "memo",
    "currency_id",
    # Many2many → account.move: facturas que este pago liquidó.
    # Es el vínculo directo factura↔pago en Odoo Enterprise. Lo usamos para
    # calcular el "settlement_date" exacto de cada factura (vs. el FIFO).
    "reconciled_invoice_ids",
]

# Estados que consideramos "abiertos" (cartera real por cobrar).
#
# IMPORTANTE: NO incluimos `in_payment`. En Odoo, `in_payment` significa que
# el cliente ya pagó (hay un account.payment registrado) pero el asiento
# está pendiente de conciliación bancaria. El saldo residual ya es 0 y no
# representa cuenta por cobrar real. Si lo dejamos en abiertas, infla
# falsamente el conteo de facturas abiertas, el aging y el monto vencido.
OPEN_PAYMENT_STATES = ["not_paid", "partial"]

# Estados que consideramos "pagados" para el cálculo de hábito de pago.
# Incluimos `in_payment` solo para conteos (`paid` sigue siendo el conjunto
# usado para mora/DSO porque ahí sí necesitamos fecha de pago conciliada).
PAID_PAYMENT_STATES = ["paid"]


MOVE_LINE_FIELDS = [
    "id",
    "move_id",
    "partner_id",
    "account_id",
    "date",
    "date_maturity",
    "debit",
    "credit",
    "balance",
    "amount_residual",
    "matched_debit_ids",
    "matched_credit_ids",
    "reconciled",
    "name",
]


# Campos para líneas de factura con producto (informe de ventas).
# Filtramos por display_type='product' para quedarnos solo con líneas de
# producto (no notas de pie, no secciones, no líneas de impuesto).
INVOICE_LINE_FIELDS = [
    "id",
    "move_id",
    "partner_id",
    "company_id",
    "product_id",
    "product_uom_id",
    "name",                    # descripción de la línea
    "quantity",
    "price_unit",
    "price_subtotal",          # subtotal sin signo (siempre positivo)
    "price_total",
    "discount",
    "date",                    # fecha contable (típicamente = invoice_date)
    "parent_state",            # estado del move padre (posted, draft, cancel)
    "move_type",               # heredado del move padre
    "display_type",            # 'product' / 'line_section' / 'line_note' / etc.
]
# Nota: `price_subtotal_signed` no existe en account.move.line en algunas
# versiones de Odoo (Odoo 19 lo quitó). Lo construimos en post-procesamiento
# multiplicando `price_subtotal` por el signo del move_type:
#   out_invoice → +1, out_refund → −1
# Así las NC restan automáticamente al sumar ventas.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unpack_m2o(value: Any) -> tuple[int | None, str | None]:
    """Desempaqueta un campo many2one [id, name] -> (id, name)."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), str(value[1])
    return None, None


def _resolve_partner_fields(client: OdooClient) -> list[str]:
    """
    Devuelve la lista de campos de res.partner que están realmente disponibles
    en esta base (intersección entre PARTNER_FIELDS deseados y fields_get).

    Cacheado por DB. Si fields_get falla (permisos raros), cae a la lista
    deseada sin `days_sales_outstanding` para evitar el error
    'Invalid field' al hacer search_read.
    """
    cache_key = client.credentials.db
    if cache_key in _PARTNER_FIELDS_CACHE:
        return _PARTNER_FIELDS_CACHE[cache_key]

    try:
        all_fields = client.fields_get("res.partner", attributes=["string"])
        available = set(all_fields.keys())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "No se pudo verificar fields_get(res.partner): %s. "
            "Usando lista conservadora.", exc
        )
        resolved = [f for f in PARTNER_FIELDS if f != "days_sales_outstanding"]
        _PARTNER_FIELDS_CACHE[cache_key] = resolved
        return resolved

    resolved = [f for f in PARTNER_FIELDS if f in available]
    missing = [f for f in PARTNER_FIELDS if f not in available]
    if missing:
        logger.info(
            "Campos de res.partner no disponibles en esta base (omitidos): %s",
            missing,
        )
    _PARTNER_FIELDS_CACHE[cache_key] = resolved
    return resolved


def _normalize_invoices(records: list[dict]) -> pd.DataFrame:
    """Convierte facturas crudas de Odoo a DataFrame normalizado."""
    if not records:
        return pd.DataFrame(columns=INVOICE_FIELDS + ["partner_name", "currency_name"])

    df = pd.DataFrame(records)

    # Desempaquetar many2ones
    df[["partner_id", "partner_name"]] = df["partner_id"].apply(
        lambda v: pd.Series(_unpack_m2o(v))
    )
    df[["currency_id", "currency_name"]] = df["currency_id"].apply(
        lambda v: pd.Series(_unpack_m2o(v))
    )
    if "invoice_payment_term_id" in df.columns:
        df[["payment_term_id", "payment_term_name"]] = df[
            "invoice_payment_term_id"
        ].apply(lambda v: pd.Series(_unpack_m2o(v)))
        df = df.drop(columns=["invoice_payment_term_id"])
    for col in ["journal_id", "company_id", "user_id", "team_id", "invoice_user_id"]:
        if col in df.columns:
            df[[f"{col}", f"{col}_name"]] = df[col].apply(
                lambda v: pd.Series(_unpack_m2o(v))
            )

    # Tipos de fecha
    for col in ["invoice_date", "invoice_date_due", "date"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    # Asegurar tipos numéricos
    for col in ["amount_untaxed_signed", "amount_total_signed", "amount_residual_signed"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    return df


def _normalize_partners(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=PARTNER_FIELDS)

    df = pd.DataFrame(records)
    if "country_id" in df.columns:
        df[["country_id", "country_name"]] = df["country_id"].apply(
            lambda v: pd.Series(_unpack_m2o(v))
        )
    if "property_payment_term_id" in df.columns:
        df[["payment_term_id", "payment_term_name"]] = df[
            "property_payment_term_id"
        ].apply(lambda v: pd.Series(_unpack_m2o(v)))
        df = df.drop(columns=["property_payment_term_id"])
    if "user_id" in df.columns:
        df[["user_id", "user_name"]] = df["user_id"].apply(
            lambda v: pd.Series(_unpack_m2o(v))
        )

    if "create_date" in df.columns:
        df["create_date"] = pd.to_datetime(df["create_date"], errors="coerce")
    df["credit"] = pd.to_numeric(df.get("credit"), errors="coerce").fillna(0.0)
    df["credit_limit"] = pd.to_numeric(df.get("credit_limit"), errors="coerce").fillna(0.0)
    if "days_sales_outstanding" in df.columns:
        df["days_sales_outstanding"] = pd.to_numeric(
            df["days_sales_outstanding"], errors="coerce"
        )
    return df


def _normalize_payments(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=PAYMENT_FIELDS)

    df = pd.DataFrame(records)
    df[["partner_id", "partner_name"]] = df["partner_id"].apply(
        lambda v: pd.Series(_unpack_m2o(v))
    )
    df[["currency_id", "currency_name"]] = df["currency_id"].apply(
        lambda v: pd.Series(_unpack_m2o(v))
    )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    df["amount_signed"] = pd.to_numeric(df.get("amount_signed"), errors="coerce").fillna(0.0)
    return df


def _normalize_move_lines(records: list[dict]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=MOVE_LINE_FIELDS)

    df = pd.DataFrame(records)
    for col in ["move_id", "partner_id", "account_id"]:
        if col in df.columns:
            df[[col, f"{col}_name"]] = df[col].apply(
                lambda v: pd.Series(_unpack_m2o(v))
            )
    for col in ["date", "date_maturity"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ["debit", "credit", "balance", "amount_residual"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df


# ---------------------------------------------------------------------------
# Extractores públicos
# ---------------------------------------------------------------------------


def extract_companies(client: OdooClient) -> pd.DataFrame:
    """Descarga la lista de compañías (multi-empresa) visibles para el usuario."""
    logger.info("Descargando compañías (res.company)")
    records = client.search_read(
        "res.company",
        domain=[],
        fields=["id", "name", "currency_id", "partner_id"],
        order="name asc",
    )
    if not records:
        return pd.DataFrame(columns=["id", "name"])
    df = pd.DataFrame(records)
    if "currency_id" in df.columns:
        df[["currency_id", "currency_name"]] = df["currency_id"].apply(
            lambda v: pd.Series(_unpack_m2o(v))
        )
    if "partner_id" in df.columns:
        df[["partner_id", "partner_name"]] = df["partner_id"].apply(
            lambda v: pd.Series(_unpack_m2o(v))
        )
    logger.info("Compañías descargadas: %s", len(df))
    return df


def extract_invoices(
    client: OdooClient,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    only_open: bool = False,
    include_refunds: bool = True,
    company_ids: list[int] | tuple[int, ...] | None = None,
) -> pd.DataFrame:
    """
    Descarga facturas de venta (out_invoice) y opcionalmente notas crédito (out_refund).

    Args:
        client: Cliente Odoo autenticado.
        date_from: Fecha mínima de invoice_date (opcional).
        date_to: Fecha máxima de invoice_date (opcional).
        only_open: Si True, solo facturas con saldo pendiente.
        include_refunds: Si True, incluye notas crédito (out_refund).
        company_ids: IDs de res.company a incluir (None = todas).
    """
    move_types = ["out_invoice"]
    if include_refunds:
        move_types.append("out_refund")

    domain: list = [
        ("move_type", "in", move_types),
        ("state", "=", "posted"),
    ]
    if date_from:
        domain.append(("invoice_date", ">=", str(date_from)))
    if date_to:
        domain.append(("invoice_date", "<=", str(date_to)))
    if only_open:
        # Solo facturas con saldo real pendiente. Excluimos `in_payment`
        # (ver OPEN_PAYMENT_STATES más arriba): esas ya están pagadas y
        # solo esperan conciliación bancaria.
        domain.append(("payment_state", "in", OPEN_PAYMENT_STATES))
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))

    logger.info("Descargando facturas con dominio: %s", domain)
    records = client.search_read(
        "account.move",
        domain=domain,
        fields=INVOICE_FIELDS,
        order="invoice_date desc",
    )
    logger.info("Facturas descargadas: %s", len(records))
    return _normalize_invoices(records)


def extract_partners(
    client: OdooClient,
    only_customers: bool = True,
    company_ids: list[int] | tuple[int, ...] | None = None,
    partner_ids: list[int] | tuple[int, ...] | None = None,
    context: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """
    Descarga clientes (res.partner).

    Modos:
    - Si `partner_ids` viene con valores, se trae EXACTAMENTE esos IDs
      (ignorando customer_rank y company_id). Esto se usa para no perder
      clientes que aparecen en facturas pero no tienen `customer_rank>0`.
    - En caso contrario, se aplica el filtro habitual (active + customer_rank
      + company_id si aplica).

    `context` se propaga al RPC. Lo usamos para pasar `allowed_company_ids`
    y que Odoo resuelva campos `company_dependent` (credit_limit,
    days_sales_outstanding) contra la empresa correcta.
    """
    if partner_ids:
        ids = list({int(pid) for pid in partner_ids if pid})
        if not ids:
            return _normalize_partners([])
        domain: list = [("id", "in", ids)]
    else:
        domain = [("active", "=", True)]
        if only_customers:
            domain.append(("customer_rank", ">", 0))
        if company_ids:
            # res.partner.company_id es opcional (NULL = compartido entre empresas).
            # Filtramos: clientes asignados a alguna de las compañías o sin compañía.
            domain.append("|")
            domain.append(("company_id", "in", list(company_ids)))
            domain.append(("company_id", "=", False))

    fields = _resolve_partner_fields(client)
    logger.info(
        "Descargando partners con dominio: %s (campos: %s, context: %s)",
        domain, fields, context,
    )
    # Solo pasamos context si está presente — así somos compatibles con
    # versiones de OdooClient que aún no tienen el parámetro `context`
    # (importante cuando Streamlit recarga extractor.py pero no
    # odoo_client.py en hot-reload).
    extra_kwargs: dict[str, Any] = {}
    if context:
        extra_kwargs["context"] = context
    try:
        records = client.search_read(
            "res.partner",
            domain=domain,
            fields=fields,
            order="name asc",
            **extra_kwargs,
        )
    except TypeError as exc:
        # Cliente con firma vieja (sin `context`). Reintentamos sin él.
        if "context" in str(exc) and extra_kwargs:
            logger.warning(
                "OdooClient.search_read no acepta `context` (versión antigua "
                "cargada). Reintentando sin context. Reinicia Streamlit "
                "para tomar la versión nueva."
            )
            records = client.search_read(
                "res.partner",
                domain=domain,
                fields=fields,
                order="name asc",
            )
        else:
            raise
    logger.info("Partners descargados: %s", len(records))
    return _normalize_partners(records)


def extract_payments(
    client: OdooClient,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    company_ids: list[int] | tuple[int, ...] | None = None,
) -> pd.DataFrame:
    """Descarga pagos de clientes (inbound)."""
    domain: list = [
        ("payment_type", "=", "inbound"),
        ("state", "in", ["posted", "paid"]),
    ]
    if date_from:
        domain.append(("date", ">=", str(date_from)))
    if date_to:
        domain.append(("date", "<=", str(date_to)))
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))

    logger.info("Descargando pagos con dominio: %s", domain)
    records = client.search_read(
        "account.payment",
        domain=domain,
        fields=PAYMENT_FIELDS,
        order="date desc",
    )
    logger.info("Pagos descargados: %s", len(records))
    return _normalize_payments(records)


def extract_receivable_lines(
    client: OdooClient,
    account_codes: list[str] | None = None,
    date_to: date | str | None = None,
) -> pd.DataFrame:
    """
    Descarga partidas (account.move.line) de cuentas por cobrar.

    Útil para reconstruir el aging exacto y los días reales que toma cobrar
    cada factura (fecha de partida débito vs. fecha de partida crédito conciliada).
    """
    domain: list = [
        ("account_id.account_type", "=", "asset_receivable"),
        ("parent_state", "=", "posted"),
    ]
    if account_codes:
        domain.append(("account_id.code", "in", account_codes))
    if date_to:
        domain.append(("date", "<=", str(date_to)))

    logger.info("Descargando move_lines con dominio: %s", domain)
    records = client.search_read(
        "account.move.line",
        domain=domain,
        fields=MOVE_LINE_FIELDS,
        order="date desc",
    )
    logger.info("Move lines descargadas: %s", len(records))
    return _normalize_move_lines(records)


def extract_invoice_lines(
    client: OdooClient,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    company_ids: list[int] | tuple[int, ...] | None = None,
    include_refunds: bool = True,
) -> pd.DataFrame:
    """
    Descarga líneas de factura (account.move.line con `display_type=product`)
    para el informe de ventas por producto / categoría.

    ⚠️ Anclado a la fecha de FACTURACIÓN del move padre (invoice_date), no
    a la fecha de la orden de venta. account.move.line no tiene `date_order`
    (eso vive en sale.order).

    Filtros:
      - move_type ∈ {out_invoice, out_refund} (NC con signo negativo)
      - parent_state = posted (excluye draft / cancel)
      - display_type = product (excluye secciones, notas, líneas de impuesto)
      - date ∈ [date_from, date_to]
      - company_id ∈ company_ids (opcional)

    El subtotal usado para el reporte es `price_subtotal_signed`, que en
    Odoo ya viene con signo correcto: positivo en facturas, negativo en NC.

    Adicionalmente, el resultado se enriquece con la categoría del producto
    (product_categ_id, product_categ_name) en una segunda llamada a
    `product.product` para evitar joins XML-RPC pesados.
    """
    move_types = ["out_invoice"]
    if include_refunds:
        move_types.append("out_refund")

    domain: list = [
        ("move_type", "in", move_types),
        ("parent_state", "=", "posted"),
        ("display_type", "=", "product"),
    ]
    if date_from:
        domain.append(("date", ">=", str(date_from)))
    if date_to:
        domain.append(("date", "<=", str(date_to)))
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))

    logger.info("Descargando invoice_lines con dominio: %s", domain)
    records = client.search_read(
        "account.move.line",
        domain=domain,
        fields=INVOICE_LINE_FIELDS,
        order="date desc",
    )
    logger.info("Invoice lines descargadas: %s", len(records))

    df = _normalize_invoice_lines(records)
    if df.empty:
        return df

    # Enriquecer con categoría de producto (un solo round-trip por todos los
    # product_id distintos). Defensa contra NaN: algunas líneas pueden no
    # tener product_id (descuentos, líneas manuales) y `int(NaN)` lanza error.
    product_ids = (
        df["product_id"]
        .dropna()
        .astype(int)
        .unique()
        .tolist()
    )
    if product_ids:
        try:
            prod_records = client.search_read(
                "product.product",
                domain=[("id", "in", product_ids)],
                fields=["id", "categ_id", "default_code", "name"],
            )
            cat_map: dict[int, tuple[int | None, str | None]] = {}
            code_map: dict[int, str | None] = {}
            for p in prod_records:
                cid, cname = _unpack_m2o(p.get("categ_id"))
                cat_map[int(p["id"])] = (cid, cname)
                code_map[int(p["id"])] = p.get("default_code") or None
            logger.info(
                "Enriquecimiento productos: %d productos, %d categorías únicas",
                len(cat_map),
                len({c for c, _ in cat_map.values() if c}),
            )

            def _cat_id(i):
                if pd.isna(i):
                    return None
                return cat_map.get(int(i), (None, None))[0]

            def _cat_name(i):
                if pd.isna(i):
                    return None
                return cat_map.get(int(i), (None, None))[1]

            def _code(i):
                if pd.isna(i):
                    return None
                return code_map.get(int(i))

            df["product_categ_id"] = df["product_id"].map(_cat_id)
            df["product_categ_name"] = df["product_id"].map(_cat_name)
            df["product_default_code"] = df["product_id"].map(_code)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "No se pudo enriquecer categoría de productos: %s", exc, exc_info=True
            )
            df["product_categ_id"] = None
            df["product_categ_name"] = None
            df["product_default_code"] = None
    else:
        df["product_categ_id"] = None
        df["product_categ_name"] = None
        df["product_default_code"] = None

    # Anclar invoice_date desde el move padre (move_id ya viene como nombre,
    # pero `date` de la línea es la fecha contable que en práctica = invoice_date).
    # Renombramos `date` → `invoice_date` para coincidir con la API del analyzer.
    df["invoice_date"] = df["date"]
    return df


def _normalize_invoice_lines(records: list[dict]) -> pd.DataFrame:
    """Convierte líneas de factura crudas (Odoo) a DataFrame normalizado."""
    if not records:
        return pd.DataFrame(columns=INVOICE_LINE_FIELDS + [
            "partner_name", "product_name", "company_name", "move_name",
        ])

    df = pd.DataFrame(records)

    # Desempaquetar many2ones que pueden venir como [id, name]
    for m2o_col, id_col, name_col in [
        ("partner_id", "partner_id", "partner_name"),
        ("product_id", "product_id", "product_name"),
        ("product_uom_id", "product_uom_id", "product_uom_name"),
        ("company_id", "company_id", "company_name"),
        ("move_id", "move_id", "move_name"),
    ]:
        if m2o_col in df.columns:
            df[[id_col, name_col]] = df[m2o_col].apply(
                lambda v: pd.Series(_unpack_m2o(v))
            )

    # Tipos numéricos
    for col in ["quantity", "price_unit", "price_subtotal",
                "price_total", "discount"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Construimos `price_subtotal_signed` manualmente porque algunas versiones
    # de Odoo (incluyendo Odoo 19) no exponen ese campo en account.move.line.
    #   out_invoice → +price_subtotal
    #   out_refund  → −price_subtotal (NC restan al sumar)
    #   otros tipos → +price_subtotal (defensa por si entra algo raro)
    if "move_type" in df.columns and "price_subtotal" in df.columns:
        sign = df["move_type"].map({"out_invoice": 1, "out_refund": -1}).fillna(1)
        df["price_subtotal_signed"] = df["price_subtotal"] * sign
    elif "price_subtotal" in df.columns:
        df["price_subtotal_signed"] = df["price_subtotal"]
    else:
        df["price_subtotal_signed"] = 0.0

    # Fecha
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # `state` lo necesita sales_analyzer.filter — duplicamos parent_state.
    if "parent_state" in df.columns:
        df["state"] = df["parent_state"]

    return df


# ---------------------------------------------------------------------------
# Extracción combinada para el flujo principal de la app
# ---------------------------------------------------------------------------


def extract_all_for_cartera(
    client: OdooClient,
    months_back: int = 12,
    cutoff_date: date | None = None,
    company_ids: list[int] | tuple[int, ...] | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Extracción combinada para alimentar el motor de análisis.

    Args:
        client: Cliente Odoo autenticado.
        months_back: Meses hacia atrás de historia a descargar.
        cutoff_date: Fecha de corte (default = hoy).
        company_ids: IDs de res.company a incluir (None = todas las visibles).

    Returns:
        Dict con dataframes: invoices, open_invoices, partners, payments, companies.
    """
    if cutoff_date is None:
        cutoff_date = datetime.now().date()

    # Fecha desde = hoy - months_back meses (aproximación)
    from datetime import timedelta

    date_from = cutoff_date - timedelta(days=30 * months_back)

    # Anclar al piso de datos confiables (cargue del sistema). Antes de esa
    # fecha pueden existir saldos iniciales o facturas parciales que ensucian
    # el cálculo de rotación, DSO y hábito de pago.
    floor = get_data_floor_date()
    date_from = clamp_date_from(date_from)
    if cutoff_date < floor:
        # Caso muy borde: el cutoff pedido está antes del go-live. No hay datos
        # confiables que devolver. Devolvemos un piso vacío válido.
        date_from = floor

    logger.info(
        "Ventana efectiva de extracción: %s → %s (piso de datos confiables: %s)",
        date_from, cutoff_date, floor,
    )

    invoices = extract_invoices(
        client,
        date_from=date_from,
        date_to=cutoff_date,
        company_ids=company_ids,
    )
    open_invoices = extract_invoices(
        client, only_open=True, company_ids=company_ids
    )
    payments = extract_payments(
        client,
        date_from=date_from,
        date_to=cutoff_date,
        company_ids=company_ids,
    )

    # ------------------------------------------------------------------
    # Resolver partners por IDs reales presentes en facturas/pagos.
    #
    # Por qué:
    # 1. Algunos clientes NO tienen `customer_rank>0` aunque tengan
    #    facturas (caso típico: importados desde otro sistema, o partners
    #    creados como "contact" sin marcar como cliente). Si filtramos por
    #    customer_rank los perdemos y al hacer merge nos queda credit_limit
    #    y DSO en NaN -> se muestra como 0.
    # 2. credit_limit y days_sales_outstanding son `company_dependent`
    #    en Odoo 17+: se almacenan por empresa vía ir.property. Sin
    #    `allowed_company_ids` en el contexto, Odoo devuelve el valor de
    #    la empresa por defecto del usuario (a menudo cero para empresas
    #    secundarias). Pasamos las compañías filtradas como allowed para
    #    que Odoo resuelva el valor correcto.
    # ------------------------------------------------------------------
    partner_ids = set()
    for df in (invoices, open_invoices, payments):
        if df is None or df.empty or "partner_id" not in df.columns:
            continue
        for pid in df["partner_id"].dropna().unique():
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                continue
            if pid_int > 0:
                partner_ids.add(pid_int)

    partner_context = None
    if company_ids:
        partner_context = {"allowed_company_ids": [int(c) for c in company_ids]}

    partners = None
    if partner_ids:
        try:
            partners = extract_partners(
                client,
                partner_ids=sorted(partner_ids),
                context=partner_context,
            )
        except Exception as exc:  # noqa: BLE001
            # Si el filtro por IDs + context falla por permisos o un campo
            # que Odoo rechaza con allowed_company_ids, caemos al modo
            # tradicional para no romper la app.
            logger.warning(
                "extract_partners(partner_ids=...) falló (%s). "
                "Reintentando sin context y con filtro tradicional.",
                exc,
            )
            partners = None

    if partners is None:
        # Fallback: filtra por customer_rank y devuelve catálogo completo.
        # Mantenemos el context para que credit_limit/DSO se resuelvan
        # contra la empresa correcta cuando sea posible.
        try:
            partners = extract_partners(
                client,
                company_ids=company_ids,
                context=partner_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "extract_partners con context falló (%s). Reintentando sin context.",
                exc,
            )
            partners = extract_partners(client, company_ids=company_ids)

    return {
        "invoices": invoices,
        "open_invoices": open_invoices,
        "partners": partners,
        "payments": payments,
        "companies": extract_companies(client),
        "cutoff_date": cutoff_date,
    }
