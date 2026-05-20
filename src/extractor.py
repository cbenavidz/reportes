# -*- coding: utf-8 -*-
"""
Extractor de datos de cartera desde Odoo 19.

Funciones para descargar facturas, pagos, partidas conciliadas y clientes,
y devolverlos como DataFrames de pandas listos para analizar.
"""
from __future__ import annotations

import logging
import re
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
    # Localización Colombia (l10n_co): si la base tiene los módulos
    # l10n_co_* instalados, el documento real puede estar en estos
    # campos en lugar de `vat`. _resolve_partner_fields los omite si
    # no están disponibles.
    "l10n_co_document_number",
    "l10n_latam_identification_type_id",
    "l10n_co_dv",   # dígito de verificación
    # Algunas instalaciones usan `identification_document` (otro módulo CO)
    "identification_document",
    "is_company",
    "email",
    "phone",
    "mobile",
    "street",
    "street2",
    "zip",
    "city",
    "state_id",
    "country_id",
    # Coordenadas GPS para mapas de georeferencia (módulo `base_geolocalize`).
    # Si no están disponibles en la base, _resolve_partner_fields los omite.
    "partner_latitude",
    "partner_longitude",
    # Equipo de ventas asignado al cliente (`crm.team`). Permite filtrar
    # clientes por equipo (ej. "Lubricantes") en el informe Ventas en Ruta.
    "team_id",
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

# Campos OPCIONALES de margen (Odoo Enterprise sale_margin):
#   `purchase_price` — costo unitario AL MOMENTO de la venta (histórico)
#   `margin`         — margen unitario calculado por Odoo
#   `margin_signed`  — margen con signo (NC negativo)
# Si la base no tiene el módulo sale_margin instalado, estos no existen.
# Los traemos solo si fields_get confirma que existen.
OPTIONAL_MARGIN_FIELDS = ["purchase_price", "margin", "margin_signed"]

# Cache de campos válidos de account.move.line por base
_MOVELINE_FIELDS_CACHE: dict[str, list[str]] = {}
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


# Regex para extraer el default_code del display_name de Odoo,
# que viene como "[CODE] Nombre del producto".
_DISPLAY_NAME_CODE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")


def _code_from_display_name(display_name: str | None) -> str | None:
    """Extrae el code de un display_name '[CODE] NAME'. None si no aplica."""
    if not display_name or not isinstance(display_name, str):
        return None
    m = _DISPLAY_NAME_CODE_RE.match(display_name)
    return m.group(1).strip() if m else None


def _backfill_code_from_name(df: "pd.DataFrame") -> "pd.DataFrame":
    """
    Si `product_default_code` está vacío en alguna fila pero `product_name`
    contiene '[CODE] NAME', extrae el code del nombre. Esto cubre el caso
    de productos eliminados/archivados donde el lookup a product.product
    falla pero el display_name de la línea de factura sigue siendo válido.
    """
    if "product_default_code" not in df.columns:
        df["product_default_code"] = None
    if "product_name" not in df.columns:
        return df

    mask_missing = df["product_default_code"].isna() | (
        df["product_default_code"].astype(str).str.strip().isin(["", "None"])
    )
    if not mask_missing.any():
        return df
    extracted = df.loc[mask_missing, "product_name"].map(_code_from_display_name)
    df.loc[mask_missing, "product_default_code"] = extracted
    return df


def _resolve_invoice_line_fields(client: OdooClient) -> tuple[list[str], list[str]]:
    """
    Devuelve (fields_to_fetch, available_margin_fields).

    Verifica vía fields_get cuáles de `OPTIONAL_MARGIN_FIELDS` existen
    realmente (depende de si el módulo sale_margin está instalado).
    """
    cache_key = client.credentials.db
    if cache_key in _MOVELINE_FIELDS_CACHE:
        cached = _MOVELINE_FIELDS_CACHE[cache_key]
        margin_fields = [f for f in OPTIONAL_MARGIN_FIELDS if f in cached]
        return cached, margin_fields

    try:
        all_fields = client.fields_get("account.move.line", attributes=["string"])
        available = set(all_fields.keys())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fields_get(account.move.line) falló: %s. "
            "Usando solo campos base (sin margen Enterprise).", exc
        )
        _MOVELINE_FIELDS_CACHE[cache_key] = list(INVOICE_LINE_FIELDS)
        return list(INVOICE_LINE_FIELDS), []

    margin_fields = [f for f in OPTIONAL_MARGIN_FIELDS if f in available]
    final = list(INVOICE_LINE_FIELDS) + margin_fields
    _MOVELINE_FIELDS_CACHE[cache_key] = final
    if margin_fields:
        logger.info(
            "Campos de margen Enterprise detectados: %s",
            ", ".join(margin_fields),
        )
    else:
        logger.info(
            "No se detectaron campos de margen Enterprise. "
            "Margen se calculará desde product.standard_price actual."
        )
    return final, margin_fields


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
    if "state_id" in df.columns:
        df[["state_id", "state_name"]] = df["state_id"].apply(
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
    if "team_id" in df.columns:
        df[["team_id", "team_name"]] = df["team_id"].apply(
            lambda v: pd.Series(_unpack_m2o(v))
        )

    if "create_date" in df.columns:
        df["create_date"] = pd.to_datetime(df["create_date"], errors="coerce")
    df["credit"] = pd.to_numeric(df.get("credit"), errors="coerce").fillna(0.0)
    df["credit_limit"] = pd.to_numeric(df.get("credit_limit"), errors="coerce").fillna(0.0)
    # Coordenadas GPS — pueden venir como False, None o número
    for geo_col in ("partner_latitude", "partner_longitude"):
        if geo_col in df.columns:
            df[geo_col] = pd.to_numeric(df[geo_col], errors="coerce")
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

    # Resolver campos disponibles (incluyendo margen Enterprise si existe)
    fields_to_fetch, margin_fields_available = _resolve_invoice_line_fields(client)

    logger.info(
        "Descargando invoice_lines con dominio: %s (campos margen: %s)",
        domain, margin_fields_available or "ninguno (usaré standard_price)",
    )
    records = client.search_read(
        "account.move.line",
        domain=domain,
        fields=fields_to_fetch,
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
            # IMPORTANTE: standard_price es company-dependent en Odoo.
            # Sin context={'company_id': X} devuelve 0 o el de la default company.
            # Pasamos el primer company_id del filtro (si hay).
            # active_test=False: incluir productos archivados.
            product_context: dict = {"active_test": False}
            if company_ids:
                # context con allowed_company_ids fuerza Odoo a leer
                # el valor del costo en la empresa correcta.
                first_co = list(company_ids)[0]
                product_context.update({
                    "company_id": first_co,
                    "allowed_company_ids": list(company_ids),
                })

            # Usar read() en vez de search_read(): ignora active_test
            # automáticamente y devuelve TODOS los registros por ID, incluso
            # los archivados (Odoo solo filtra por active en search/search_read,
            # no en read).
            prod_records = client.read(
                "product.product",
                ids=product_ids,
                fields=[
                    "id", "categ_id", "default_code", "name", "volume",
                    "standard_price", "product_tmpl_id",
                ],
                context=product_context,
            )
            # Si aún faltan productos (eliminados con unlink, no solo archivados),
            # intentamos resolverlos vía product.template.
            found_ids = {int(p["id"]) for p in prod_records if p.get("id")}
            missing_ids = [pid for pid in product_ids if int(pid) not in found_ids]
            if missing_ids:
                logger.info(
                    "%d productos no resueltos en product.product. "
                    "Intentando vía product.template.",
                    len(missing_ids),
                )
                # Estrategia: buscar product.template cuyos variant_ids
                # contengan estos IDs. Como product.product.id = template.id
                # cuando hay un solo variant, intentamos read directo sobre
                # product.template con los mismos IDs.
                try:
                    tmpl_records = client.read(
                        "product.template",
                        ids=missing_ids,
                        fields=[
                            "id", "categ_id", "default_code", "name",
                            "standard_price",
                        ],
                        context=product_context,
                    )
                    # Agregamos al pool de prod_records con un flag para
                    # diferenciar la fuente.
                    for t in tmpl_records:
                        prod_records.append({
                            "id": t["id"],
                            "categ_id": t.get("categ_id"),
                            "default_code": t.get("default_code"),
                            "name": t.get("name"),
                            "volume": 0,
                            "standard_price": t.get("standard_price", 0),
                        })
                    logger.info(
                        "Resueltos %d productos adicionales vía product.template.",
                        len(tmpl_records),
                    )
                except Exception as exc_tmpl:  # noqa: BLE001
                    logger.warning(
                        "Fallback a product.template falló: %s", exc_tmpl,
                    )
            cat_map: dict[int, tuple[int | None, str | None]] = {}
            code_map: dict[int, str | None] = {}
            volume_map: dict[int, float] = {}
            cost_map: dict[int, float] = {}
            for p in prod_records:
                cid, cname = _unpack_m2o(p.get("categ_id"))
                cat_map[int(p["id"])] = (cid, cname)
                code_map[int(p["id"])] = p.get("default_code") or None
                # `product.volume` es float (m³ típicamente, pero el negocio
                # decide la unidad — para Casa de los Mineros son galones).
                vol = p.get("volume")
                try:
                    volume_map[int(p["id"])] = float(vol) if vol else 0.0
                except (TypeError, ValueError):
                    volume_map[int(p["id"])] = 0.0
                # `standard_price` es el precio de costo unitario.
                cost = p.get("standard_price")
                try:
                    cost_map[int(p["id"])] = float(cost) if cost else 0.0
                except (TypeError, ValueError):
                    cost_map[int(p["id"])] = 0.0
            logger.info(
                "Enriquecimiento productos: %d productos, %d categorías únicas, "
                "%d con volume > 0",
                len(cat_map),
                len({c for c, _ in cat_map.values() if c}),
                sum(1 for v in volume_map.values() if v > 0),
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

            def _vol(i):
                if pd.isna(i):
                    return 0.0
                return volume_map.get(int(i), 0.0)

            def _cost(i):
                if pd.isna(i):
                    return 0.0
                return cost_map.get(int(i), 0.0)

            df["product_categ_id"] = df["product_id"].map(_cat_id)
            df["product_categ_name"] = df["product_id"].map(_cat_name)
            df["product_default_code"] = df["product_id"].map(_code)
            df["product_volume"] = df["product_id"].map(_vol)
            df["product_standard_price"] = df["product_id"].map(_cost)

            # --- Cálculo de costo y margen ---
            # Prioridad para `line_cost`:
            #   1. `purchase_price` × `quantity` × signo  (Enterprise, histórico)
            #   2. `product.standard_price` × `quantity` × signo  (snapshot actual)
            # Prioridad para `line_margin`:
            #   1. `margin_signed`        (Enterprise, signed automático)
            #   2. `margin` × signo       (Enterprise)
            #   3. price_subtotal_signed − line_cost  (fallback manual)
            sign_series = df["move_type"].map(
                {"out_invoice": 1, "out_refund": -1}
            ).fillna(1)

            if "purchase_price" in df.columns:
                # Costo histórico de Enterprise — mucho más preciso
                df["line_cost"] = (
                    pd.to_numeric(df["purchase_price"], errors="coerce").fillna(0)
                    * df["quantity"].fillna(0)
                    * sign_series
                )
                df["cost_source"] = "purchase_price (Enterprise, histórico)"
            else:
                # Fallback: snapshot actual del costo del producto
                df["line_cost"] = (
                    df["product_standard_price"].fillna(0)
                    * df["quantity"].fillna(0)
                    * sign_series
                )
                df["cost_source"] = "standard_price (actual)"

            if "margin_signed" in df.columns:
                df["line_margin"] = pd.to_numeric(
                    df["margin_signed"], errors="coerce"
                ).fillna(0)
                df["margin_source"] = "margin_signed (Enterprise)"
            elif "margin" in df.columns:
                df["line_margin"] = pd.to_numeric(
                    df["margin"], errors="coerce"
                ).fillna(0) * sign_series
                df["margin_source"] = "margin × signo (Enterprise)"
            else:
                df["line_margin"] = df["price_subtotal_signed"] - df["line_cost"]
                df["margin_source"] = "calculado manual"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "No se pudo enriquecer categoría de productos: %s", exc, exc_info=True
            )
            df["product_categ_id"] = None
            df["product_categ_name"] = None
            df["product_default_code"] = None
            df["product_volume"] = 0.0
            df["product_standard_price"] = 0.0
            df["line_cost"] = 0.0
            df["line_margin"] = df["price_subtotal_signed"]
            df["cost_source"] = "no disponible"
            df["margin_source"] = "no disponible"
    else:
        df["product_categ_id"] = None
        df["product_categ_name"] = None
        df["product_default_code"] = None
        df["product_volume"] = 0.0
        df["product_standard_price"] = 0.0
        df["line_cost"] = 0.0
        df["line_margin"] = df["price_subtotal_signed"]
        df["cost_source"] = "no disponible"
        df["margin_source"] = "no disponible"

    # Anclar invoice_date desde el move padre (move_id ya viene como nombre,
    # pero `date` de la línea es la fecha contable que en práctica = invoice_date).
    # Renombramos `date` → `invoice_date` para coincidir con la API del analyzer.
    df["invoice_date"] = df["date"]

    # Backfill del código desde el display_name de la línea cuando el lookup
    # a product.product/template no devolvió un default_code (producto
    # eliminado, archivado o sin permisos de lectura).
    df = _backfill_code_from_name(df)

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
# Extracción para Estados Financieros (Balance, P&L, KTNO, Flujo)
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = [
    "id", "code", "name", "account_type",
    "company_id", "currency_id", "deprecated", "reconcile",
]

# Tipos de cuenta de Odoo (`account_type`):
#   asset_receivable, asset_cash, asset_current, asset_non_current,
#   asset_prepayments, asset_fixed,
#   liability_payable, liability_credit_card, liability_current, liability_non_current,
#   equity, equity_unaffected,
#   income, income_other, expense, expense_depreciation, expense_direct_cost,
#   off_balance


def extract_chart_of_accounts(
    client: OdooClient,
    company_ids: list[int] | tuple[int, ...] | None = None,
) -> pd.DataFrame:
    """
    Trae el plan de cuentas (account.account) con código, nombre y tipo.

    Estrategia defensiva: intenta progresivamente con varios sets de campos,
    cayendo al mínimo (id, code, name) si los otros fallan. Compatible con
    Odoo 13/14/15/16/17/18/19 community y enterprise.
    """
    # Cast explícito de company_ids a int (Python int, no numpy)
    company_ids_clean: list[int] | None = None
    if company_ids:
        try:
            company_ids_clean = [int(c) for c in company_ids]
        except (TypeError, ValueError):
            company_ids_clean = None

    base_domain_with_co = (
        [("company_id", "in", company_ids_clean)]
        if company_ids_clean else []
    )

    # Cada nivel tiene SU PROPIO domain y order (más defensivo)
    levels_config = [
        # Nivel 0: con account_type + company
        {
            "fields": ["id", "code", "name", "account_type"],
            "domain": base_domain_with_co,
            "order": "code asc",
        },
        # Nivel 1: account_type sin company filter (por si company filter rompe)
        {
            "fields": ["id", "code", "name", "account_type"],
            "domain": [],
            "order": "code asc",
        },
        # Nivel 2: versión vieja con user_type_id
        {
            "fields": ["id", "code", "name", "user_type_id"],
            "domain": base_domain_with_co,
            "order": "code asc",
        },
        # Nivel 3: sin tipo, con company
        {
            "fields": ["id", "code", "name", "company_id"],
            "domain": base_domain_with_co,
            "order": "code asc",
        },
        # Nivel 4: mínimo con company y sin order
        {
            "fields": ["id", "code", "name"],
            "domain": base_domain_with_co,
            "order": None,
        },
        # Nivel 5: ABSOLUTO MÍNIMO — sin domain, sin order, solo id+code+name
        {
            "fields": ["id", "code", "name"],
            "domain": [],
            "order": None,
        },
        # Nivel 6: ÚLTIMO RECURSO — pedir solo id, ni siquiera filtramos campos
        {
            "fields": ["id", "name"],
            "domain": [],
            "order": None,
        },
    ]

    records = None
    last_exc = None
    used_level = 0
    error_messages: list[str] = []
    for i, cfg in enumerate(levels_config, start=1):
        try:
            kwargs = {
                "domain": cfg["domain"],
                "fields": cfg["fields"],
            }
            if cfg["order"]:
                kwargs["order"] = cfg["order"]
            logger.info(
                "Plan de cuentas nivel %d: %s",
                i, kwargs,
            )
            records = client.search_read("account.account", **kwargs)
            used_level = i
            break  # éxito
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            err_str = f"Nivel {i} ({cfg['fields']}): {exc}"
            error_messages.append(err_str)
            logger.warning(err_str)
            continue

    if records is None:
        # Todos fallaron → mensaje con TODOS los errores para diagnóstico
        full_error = "\n".join(error_messages)
        raise RuntimeError(
            f"No se pudo cargar el plan de cuentas. Errores por nivel:\n{full_error}"
        ) from last_exc

    logger.info("Plan de cuentas cargado en nivel %d: %d cuentas", used_level, len(records))

    if not records:
        return pd.DataFrame(columns=["id", "code", "name"])

    df = pd.DataFrame(records)

    # Normalizar nombre del campo de tipo (account_type / user_type_id)
    if "account_type" not in df.columns and "user_type_id" in df.columns:
        df["account_type"] = df["user_type_id"].apply(
            lambda v: _unpack_m2o(v)[1] if isinstance(v, list) else None
        )

    # Desempaquetar many2ones presentes
    for col in ("company_id", "currency_id", "user_type_id"):
        if col in df.columns:
            df[col + "_name"] = df[col].apply(
                lambda v: _unpack_m2o(v)[1] if isinstance(v, list) else None
            )
            df[col] = df[col].apply(
                lambda v: _unpack_m2o(v)[0] if isinstance(v, list) else None
            )

    # ─────────────────────────────────────────────────────────────────────
    # FIX Odoo 17+/19 con localización CO: el campo `code` de account.account
    # puede venir como False vía XML-RPC porque ahora es per-company
    # (`code_mapping_ids`). Si detectamos que muchos códigos vienen False,
    # forzamos un read() adicional con company-context para extraer codes,
    # y como fallback extraemos del display_name (formato "510515 Sueldos").
    # ─────────────────────────────────────────────────────────────────────
    if "code" in df.columns:
        # Normalizar: False → None, luego ver cuántos quedan vacíos
        df["code"] = df["code"].replace({False: None})
        nulos = df["code"].isna().sum()
        if nulos > 0:
            logger.info(
                "Plan de cuentas: %d/%d códigos vinieron vacíos. "
                "Intentando refetch con display_name...",
                nulos, len(df),
            )
            # Pedir display_name vía read() (más confiable que search_read
            # para campos computados/per-company)
            try:
                ids_to_refetch = df.loc[df["code"].isna(), "id"].astype(int).tolist()
                ctx = {}
                if company_ids_clean:
                    ctx["allowed_company_ids"] = company_ids_clean
                    ctx["force_company"] = company_ids_clean[0]
                extra = client.read(
                    "account.account",
                    ids_to_refetch,
                    fields=["id", "code", "display_name"],
                    context=ctx or None,
                )
                extra_map: dict[int, dict] = {
                    int(r["id"]): r for r in (extra or [])
                }

                def _resolve_code(row):
                    rid = int(row["id"])
                    rec = extra_map.get(rid, {})
                    # 1) code directo si vino con read()
                    c = rec.get("code")
                    if c and str(c).lower() != "false":
                        return str(c).strip()
                    # 2) extraer del display_name
                    dn = rec.get("display_name") or ""
                    if dn and str(dn).lower() != "false":
                        s = str(dn).strip()
                        # "510515 Sueldos" o "510515-Sueldos" o "510515 - Sueldos"
                        m = re.match(r"^\s*(\d{4,15})\b", s)
                        if m:
                            return m.group(1)
                        # "[510515] Sueldos"
                        m = re.match(r"^\s*\[\s*(\d{4,15})\s*\]", s)
                        if m:
                            return m.group(1)
                    # 3) último intento: parsear del campo name si está
                    n = row.get("name") or ""
                    if n and str(n).lower() != "false":
                        m = re.match(r"^\s*(\d{4,15})\b", str(n).strip())
                        if m:
                            return m.group(1)
                    return None

                df.loc[df["code"].isna(), "code"] = df.loc[
                    df["code"].isna()
                ].apply(_resolve_code, axis=1)
                aun_nulos = df["code"].isna().sum()
                logger.info(
                    "Tras refetch: %d códigos resueltos, %d siguen vacíos.",
                    nulos - aun_nulos, aun_nulos,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Refetch de códigos con display_name falló: %s. "
                    "Se mantiene el chart parcial.", exc,
                )
        # Garantizar string para downstream
        df["code"] = df["code"].fillna("").astype(str)

    # Primer dígito del código → grupo PUC colombiano
    df["puc_grupo"] = df["code"].astype(str).str[0]
    return df


def extract_account_movements(
    client: OdooClient,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    company_ids: list[int] | tuple[int, ...] | None = None,
) -> pd.DataFrame:
    """
    Trae TODOS los movimientos contables (account.move.line) en el rango,
    no solo facturas. Es la base para Balance, P&L, Flujo de efectivo.

    Solo trae líneas de moves en estado `posted` para evitar duplicar
    borradores y cancelados.
    """
    domain: list = [("parent_state", "=", "posted")]
    if date_from is not None:
        df_str = date_from.isoformat() if isinstance(date_from, date) else str(date_from)
        domain.append(("date", ">=", df_str))
    if date_to is not None:
        dt_str = date_to.isoformat() if isinstance(date_to, date) else str(date_to)
        domain.append(("date", "<=", dt_str))
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))

    desired_fields = [
        "id", "account_id", "partner_id", "move_id",
        "date", "name", "ref",
        "debit", "credit", "balance",
        "company_id", "currency_id",
        "parent_state", "journal_id",
        # move_type heredado del asiento padre: necesario para diferenciar
        # facturas de venta (out_invoice/out_refund) de compra (in_invoice/
        # in_refund) y asientos manuales (entry). Crítico para medios
        # magnéticos: el formato 1001 debe excluir facturas de venta.
        "move_type",
    ]
    # Filtrar a campos disponibles (defensivo contra versiones de Odoo)
    try:
        all_fields_meta = client.fields_get("account.move.line", attributes=["string"])
        available = set(all_fields_meta.keys())
        fields = [f for f in desired_fields if f in available]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fields_get(account.move.line) falló: %s. Usando lista completa.",
            exc,
        )
        fields = desired_fields

    logger.info("Descargando account_movements con dominio: %s, campos=%s", domain, fields)
    records = client.search_read(
        "account.move.line",
        domain=domain,
        fields=fields,
        order="date asc, id asc",
    )
    logger.info("Account movements descargados: %s", len(records))

    if not records:
        return pd.DataFrame(columns=fields + [
            "account_id_name", "partner_id_name",
            "move_id_name", "company_id_name", "journal_id_name",
        ])

    df = pd.DataFrame(records)
    # Desempaquetar many2ones
    for col in ("account_id", "partner_id", "move_id", "company_id",
                "currency_id", "journal_id"):
        if col in df.columns:
            df[col + "_name"] = df[col].apply(
                lambda v: _unpack_m2o(v)[1] if isinstance(v, list) else None
            )
            df[col] = df[col].apply(
                lambda v: _unpack_m2o(v)[0] if isinstance(v, list) else None
            )
    # Tipos numéricos
    for c in ("debit", "credit", "balance"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
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


# ---------------------------------------------------------------------------
# Extracción de compras (facturas de proveedor) y stock
# ---------------------------------------------------------------------------


def extract_purchase_invoice_lines(
    client: OdooClient,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    company_ids: list[int] | tuple[int, ...] | None = None,
    include_refunds: bool = True,
) -> pd.DataFrame:
    """
    Líneas de factura de PROVEEDOR (in_invoice / in_refund) para análisis
    de compras vs ventas.

    Filtros: parent_state='posted', display_type='product', rango de fechas
    y empresas. Devuelve cantidad, costo unitario, subtotal con signo (NC
    de proveedor negativas), categoría y código de producto.
    """
    move_types = ["in_invoice"]
    if include_refunds:
        move_types.append("in_refund")

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

    fields_to_fetch = list(INVOICE_LINE_FIELDS)

    logger.info("Descargando purchase_invoice_lines con dominio: %s", domain)
    records = client.search_read(
        "account.move.line",
        domain=domain,
        fields=fields_to_fetch,
        order="date desc",
    )
    logger.info("Purchase invoice lines descargadas: %s", len(records))

    df = _normalize_invoice_lines(records)
    if df.empty:
        return df

    # En `_normalize_invoice_lines` el signo se calcula para out_invoice/out_refund.
    # Para compras necesitamos: in_invoice → +qty/+subtotal, in_refund → −qty/−subtotal.
    if "move_type" in df.columns:
        sign = df["move_type"].map(
            {"in_invoice": 1, "in_refund": -1}
        ).fillna(1)
        df["price_subtotal_signed"] = df["price_subtotal"] * sign
        df["quantity_signed"] = df["quantity"] * sign
    else:
        df["quantity_signed"] = df["quantity"]

    # Enriquecer con categoría y código de producto
    product_ids = (
        df["product_id"].dropna().astype(int).unique().tolist()
        if "product_id" in df.columns else []
    )
    if product_ids:
        try:
            # active_test=False: incluir productos archivados.
            product_context: dict = {"active_test": False}
            if company_ids:
                first_co = list(company_ids)[0]
                product_context.update({
                    "company_id": first_co,
                    "allowed_company_ids": list(company_ids),
                })
            # read() ignora active_test automáticamente y trae registros
            # por ID directamente, incluso si están archivados.
            prod_records = client.read(
                "product.product",
                ids=product_ids,
                fields=["id", "categ_id", "default_code", "name",
                        "standard_price"],
                context=product_context,
            )
            # Fallback a product.template para productos eliminados con unlink
            found_ids = {int(p["id"]) for p in prod_records if p.get("id")}
            missing_ids = [pid for pid in product_ids if int(pid) not in found_ids]
            if missing_ids:
                try:
                    tmpl_records = client.read(
                        "product.template",
                        ids=missing_ids,
                        fields=["id", "categ_id", "default_code", "name",
                                "standard_price"],
                        context=product_context,
                    )
                    for t in tmpl_records:
                        prod_records.append({
                            "id": t["id"],
                            "categ_id": t.get("categ_id"),
                            "default_code": t.get("default_code"),
                            "name": t.get("name"),
                            "standard_price": t.get("standard_price", 0),
                        })
                except Exception:  # noqa: BLE001
                    pass
            cat_map: dict[int, tuple[int | None, str | None]] = {}
            code_map: dict[int, str | None] = {}
            cost_map: dict[int, float] = {}
            for p in prod_records:
                cid, cname = _unpack_m2o(p.get("categ_id"))
                cat_map[int(p["id"])] = (cid, cname)
                code_map[int(p["id"])] = p.get("default_code") or None
                cost = p.get("standard_price")
                try:
                    cost_map[int(p["id"])] = float(cost) if cost else 0.0
                except (TypeError, ValueError):
                    cost_map[int(p["id"])] = 0.0

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

            def _cost(i):
                if pd.isna(i):
                    return 0.0
                return cost_map.get(int(i), 0.0)

            df["product_categ_id"] = df["product_id"].map(_cat_id)
            df["product_categ_name"] = df["product_id"].map(_cat_name)
            df["product_default_code"] = df["product_id"].map(_code)
            df["product_standard_price"] = df["product_id"].map(_cost)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "No se pudo enriquecer productos en compras: %s", exc,
            )
            df["product_categ_id"] = None
            df["product_categ_name"] = None
            df["product_default_code"] = None
            df["product_standard_price"] = 0.0
    else:
        df["product_categ_id"] = None
        df["product_categ_name"] = None
        df["product_default_code"] = None
        df["product_standard_price"] = 0.0

    df["invoice_date"] = df["date"]
    # Backfill del código desde el display_name cuando el lookup falla
    df = _backfill_code_from_name(df)
    return df


def extract_stock_quants(
    client: OdooClient,
    company_ids: list[int] | tuple[int, ...] | None = None,
    product_ids: list[int] | tuple[int, ...] | None = None,
) -> pd.DataFrame:
    """
    Stock disponible por producto desde `stock.quant`, sumando todas las
    ubicaciones internas. Devuelve un DF con: product_id, qty_available,
    value (valor inventariado si está disponible).

    Sólo trae ubicaciones tipo `internal` (ignora vistas, clientes, etc.).
    """
    domain: list = [("location_id.usage", "=", "internal")]
    if company_ids:
        domain.append(("company_id", "in", list(company_ids)))
    if product_ids:
        domain.append(("product_id", "in", list(product_ids)))

    try:
        # Usamos read_group para sumar server-side por producto (mucho más rápido)
        result = client.execute_kw(
            "stock.quant", "read_group",
            [domain, ["product_id", "quantity:sum", "value:sum"], ["product_id"]],
            {"lazy": False},
        )
    except Exception as exc:  # noqa: BLE001
        # `value` puede no estar disponible en todas las bases (depende del
        # módulo de valoración). Reintentar sin él.
        logger.warning(
            "read_group stock.quant con `value` falló (%s). Reintentando sin él.",
            exc,
        )
        try:
            result = client.execute_kw(
                "stock.quant", "read_group",
                [domain, ["product_id", "quantity:sum"], ["product_id"]],
                {"lazy": False},
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error("read_group stock.quant falló: %s", exc2)
            return pd.DataFrame(columns=["product_id", "qty_available", "stock_value"])

    rows = []
    for r in result:
        prod = r.get("product_id")
        if isinstance(prod, list) and prod:
            prod_id = prod[0]
        else:
            prod_id = prod
        rows.append({
            "product_id": prod_id,
            "qty_available": float(r.get("quantity", 0) or 0),
            "stock_value": float(r.get("value", 0) or 0),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["product_id", "qty_available", "stock_value"])
    return df
