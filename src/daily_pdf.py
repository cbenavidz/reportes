# -*- coding: utf-8 -*-
"""
Generador del PDF diario combinado: Estado de Caja + Informe de Ventas
Diarias. Pensado para imprimir/archivar y enviar por correo cada día a
las 6:05 PM.

Función principal:
    build_daily_pdf(
        fecha, empresa, nit,
        estado_caja,           # dict de cash_report.compute_estado_caja
        ventas_kpis,           # SalesKPIs
        ventas_por_categoria,  # DataFrame
        ventas_por_producto,   # DataFrame (top N)
        top_productos=25,
    ) -> bytes
"""
from __future__ import annotations

import io
from datetime import date as _date, datetime

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Paleta CDM
_CDM_RED = colors.HexColor("#C8102E")
_CDM_NAVY = colors.HexColor("#102A43")
_LIGHT_GREY = colors.HexColor("#F2F2F2")
_BORDER = colors.HexColor("#CCCCCC")


# ── Helpers de formato ──
def _fmt_money(n) -> str:
    """Formato Colombia: $1.234.567,50"""
    try:
        s = f"{float(n):,.2f}"
        return "$" + s.replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "$0,00"


def _fmt_pct(n) -> str:
    try:
        return f"{float(n):,.2f}%".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "0,00%"


def _fmt_int(n) -> str:
    try:
        return f"{int(float(n)):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def _fmt_qty(n) -> str:
    try:
        return f"{float(n):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "0,00"


# ── Estilos de párrafo ──
def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=16, leading=18,
            textColor=_CDM_NAVY, alignment=1, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=10, leading=12,
            textColor=colors.HexColor("#666666"), alignment=1, spaceAfter=10,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontSize=13, leading=16,
            textColor=_CDM_RED, spaceBefore=10, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=11, leading=13,
            textColor=_CDM_NAVY, spaceBefore=8, spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "h3", parent=base["Heading3"], fontSize=10, leading=12,
            textColor=colors.black, spaceBefore=4, spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9, leading=11,
        ),
        "small": ParagraphStyle(
            "small", parent=base["Normal"], fontSize=8, leading=10,
            textColor=colors.HexColor("#777777"),
        ),
        "right": ParagraphStyle(
            "right", parent=base["Normal"], fontSize=9, leading=11,
            alignment=2,
        ),
    }


# ── Estilos de tabla ──
def _header_row_style(start_col=0, end_col=-1):
    return [
        ("BACKGROUND", (start_col, 0), (end_col, 0), _CDM_NAVY),
        ("TEXTCOLOR", (start_col, 0), (end_col, 0), colors.white),
        ("FONTNAME", (start_col, 0), (end_col, 0), "Helvetica-Bold"),
        ("FONTSIZE", (start_col, 0), (end_col, 0), 8),
        ("ALIGN", (start_col, 0), (end_col, 0), "CENTER"),
    ]


def _data_table_style(numeric_cols: list[int]):
    base_style = [
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, _BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _LIGHT_GREY]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    base_style.extend(_header_row_style())
    for c in numeric_cols:
        base_style.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
    return TableStyle(base_style)


def _subtotal_row_style(row_idx: int, n_cols: int):
    return [
        ("BACKGROUND", (0, row_idx), (n_cols - 1, row_idx),
         colors.HexColor("#E8EEF7")),
        ("FONTNAME", (0, row_idx), (n_cols - 1, row_idx), "Helvetica-Bold"),
        ("LINEABOVE", (0, row_idx), (n_cols - 1, row_idx), 0.5, _CDM_NAVY),
    ]


# ── Header / footer (callback de cada página) ──
def _make_page_decorator(empresa: str, nit: str, fecha: _date):
    fecha_str = fecha.strftime("%d/%m/%Y") if isinstance(fecha, (_date, datetime)) else str(fecha)
    titulo_doc = "ESTADO DE CAJA + VENTAS DIARIAS"

    def _decorator(canv, doc):
        canv.saveState()
        # --- Línea 1: empresa (izquierda) + fecha (derecha) ---
        canv.setFont("Helvetica-Bold", 9)
        canv.setFillColor(_CDM_NAVY)
        canv.drawString(1.5 * cm, letter[1] - 1.0 * cm, empresa.upper())
        canv.drawRightString(
            letter[0] - 1.5 * cm, letter[1] - 1.0 * cm, f"Fecha: {fecha_str}",
        )
        # --- Línea 2: NIT (izquierda) + título del documento (centrado) ---
        canv.setFont("Helvetica", 8)
        canv.setFillColor(colors.HexColor("#444444"))
        canv.drawString(1.5 * cm, letter[1] - 1.5 * cm, f"NIT: {nit}")
        canv.setFont("Helvetica-Bold", 11)
        canv.setFillColor(_CDM_RED)
        canv.drawCentredString(
            letter[0] / 2, letter[1] - 1.5 * cm, titulo_doc,
        )
        # Línea horizontal bajo el header
        canv.setStrokeColor(_CDM_RED)
        canv.setLineWidth(0.8)
        canv.line(1.5 * cm, letter[1] - 1.85 * cm,
                  letter[0] - 1.5 * cm, letter[1] - 1.85 * cm)
        # Footer
        canv.setFont("Helvetica", 7)
        canv.setFillColor(colors.HexColor("#888888"))
        canv.drawString(
            1.5 * cm, 1 * cm,
            f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
            "Casa de los Mineros · Informe diario automático",
        )
        canv.drawRightString(
            letter[0] - 1.5 * cm, 1 * cm,
            f"Página {canv.getPageNumber()}",
        )
        canv.restoreState()

    return _decorator


# ── Construcción de secciones ──
def _section_estado_caja(estado: dict, st: dict) -> list:
    story: list = []
    story.append(Paragraph("ESTADO DE CAJA", st["h1"]))

    if not estado or not estado.get("cuentas"):
        story.append(Paragraph(
            "Sin movimientos ni saldos en cuentas de caja para esta fecha.",
            st["body"],
        ))
        return story

    for cta in estado["cuentas"]:
        # Header de cuenta
        story.append(Paragraph(
            f"{cta['code']} — {cta['name']}", st["h2"],
        ))

        # KPIs de la cuenta
        kpi_tbl = Table([
            ["Saldo inicial", "(+) Débitos", "(−) Créditos", "Saldo final"],
            [
                _fmt_money(cta["saldo_inicial"]),
                _fmt_money(cta["debitos"]),
                _fmt_money(cta["creditos"]),
                _fmt_money(cta["saldo_final"]),
            ],
        ], colWidths=[4.2 * cm] * 4)
        kpi_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _CDM_NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("FONTSIZE", (0, 1), (-1, 1), 10),
            ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BACKGROUND", (0, 1), (-1, 1), _LIGHT_GREY),
            ("GRID", (0, 0), (-1, -1), 0.25, _BORDER),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(kpi_tbl)
        story.append(Spacer(1, 4))

        if not cta["grupos"]:
            story.append(Paragraph(
                "Sin movimientos del día en esta cuenta.", st["small"],
            ))
            story.append(Spacer(1, 8))
            continue

        # Grupos por tipo de comprobante
        for g in cta["grupos"]:
            story.append(Paragraph(g["journal_name"], st["h3"]))
            rows = [["Comprobante", "Contacto", "Referencia / etiqueta", "Valor"]]
            for ln in g["lineas"]:
                rows.append([
                    Paragraph(str(ln["comprobante"]), st["body"]),
                    Paragraph(ln["contacto"], st["body"]),
                    Paragraph(ln["referencia"], st["body"]),
                    _fmt_money(ln["valor"]),
                ])
            rows.append([
                "", "",
                Paragraph(
                    f"<b>Subtotal {g['journal_name']}</b>", st["right"],
                ),
                _fmt_money(g["subtotal"]),
            ])
            tbl = Table(
                rows,
                colWidths=[3.4 * cm, 4.4 * cm, 6.7 * cm, 2.5 * cm],
                repeatRows=1,
            )
            base = _data_table_style(numeric_cols=[3])
            base.add("LINEABOVE", (0, len(rows) - 1), (-1, len(rows) - 1),
                     0.5, _CDM_NAVY)
            base.add("FONTNAME", (0, len(rows) - 1), (-1, len(rows) - 1),
                     "Helvetica-Bold")
            base.add("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1),
                     colors.HexColor("#E8EEF7"))
            tbl.setStyle(base)
            story.append(tbl)
            story.append(Spacer(1, 6))

        # Total flujo de la cuenta
        story.append(Paragraph(
            f"<b>Total flujo de la cuenta:</b> "
            f"<font color='#102A43'><b>{_fmt_money(cta['flujo_neto'])}</b></font>",
            st["body"],
        ))
        story.append(Spacer(1, 12))

    # Resumen por cuentas
    rc = estado.get("resumen_cuentas")
    if rc is not None and not rc.empty:
        story.append(Paragraph("Resumen por cuentas", st["h2"]))
        rows = [["Cuenta", "Nombre", "Saldo inicial", "Débitos",
                 "Créditos", "Saldo final"]]
        for _, r in rc.iterrows():
            rows.append([
                str(r["code"]),
                Paragraph(str(r["name"]), st["body"]),
                _fmt_money(r["saldo_inicial"]),
                _fmt_money(r["debitos"]),
                _fmt_money(r["creditos"]),
                _fmt_money(r["saldo_final"]),
            ])
        tbl = Table(
            rows,
            colWidths=[2 * cm, 5 * cm, 3 * cm, 2.5 * cm, 2.5 * cm, 3 * cm],
            repeatRows=1,
        )
        tbl.setStyle(_data_table_style(numeric_cols=[2, 3, 4, 5]))
        story.append(tbl)
        story.append(Spacer(1, 8))

    # Resumen por formas de pago
    rfp = estado.get("resumen_formas_pago")
    if rfp is not None and not rfp.empty:
        story.append(Paragraph("Resumen por formas de pago", st["h2"]))
        rows = [["Detalle", "Valor"]]
        for _, r in rfp.iterrows():
            rows.append([
                Paragraph(str(r["detalle"]), st["body"]),
                _fmt_money(r["valor"]),
            ])
        tbl = Table(rows, colWidths=[13 * cm, 5 * cm], repeatRows=1)
        tbl.setStyle(_data_table_style(numeric_cols=[1]))
        story.append(tbl)
        story.append(Spacer(1, 6))

    story.append(Paragraph(
        f"<b>Total flujo del día (todas las cuentas):</b> "
        f"<font color='#C8102E'><b>"
        f"{_fmt_money(estado.get('total_flujo', 0))}</b></font>",
        st["body"],
    ))

    return story


def _section_ventas_diarias(
    ventas_kpis,
    por_categoria: pd.DataFrame | None,
    por_producto: pd.DataFrame | None,
    top_productos: int,
    st: dict,
) -> list:
    story: list = []
    story.append(Paragraph("INFORME DE VENTAS DIARIAS", st["h1"]))

    if ventas_kpis is None:
        story.append(Paragraph(
            "No hay datos de ventas para esta fecha.", st["body"],
        ))
        return story

    # KPIs
    k = ventas_kpis
    kpi_tbl = Table([
        [
            "Ventas brutas", "Ventas netas", "Costo",
            "Margen", "Margen %",
        ],
        [
            _fmt_money(getattr(k, "ventas_brutas", 0)),
            _fmt_money(getattr(k, "ventas_netas", 0)),
            _fmt_money(getattr(k, "costo_ventas", 0)),
            _fmt_money(getattr(k, "margen", 0)),
            _fmt_pct(getattr(k, "margen_pct", 0)),
        ],
        [
            "# Facturas", "# Notas crédito", "Ticket prom.",
            "", "",
        ],
        [
            _fmt_int(getattr(k, "n_facturas", 0)),
            _fmt_int(getattr(k, "n_notas_credito", 0)),
            _fmt_money(
                (getattr(k, "ventas_netas", 0) / getattr(k, "n_facturas", 1))
                if getattr(k, "n_facturas", 0) else 0
            ),
            "", "",
        ],
    ], colWidths=[3.6 * cm] * 5)
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _CDM_NAVY),
        ("BACKGROUND", (0, 2), (-1, 2), _CDM_NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR", (0, 2), (-1, 2), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 1), (-1, 1), _LIGHT_GREY),
        ("BACKGROUND", (0, 3), (-1, 3), _LIGHT_GREY),
        ("GRID", (0, 0), (-1, -1), 0.25, _BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(kpi_tbl)
    story.append(Spacer(1, 10))

    # Por categoría
    story.append(Paragraph("Por categoría", st["h2"]))
    if por_categoria is None or por_categoria.empty:
        story.append(Paragraph(
            "Sin categorías para mostrar.", st["small"],
        ))
    else:
        rows = [["Categoría", "Cant.", "Ventas netas", "Costo",
                 "Margen", "Margen %", "# Fact.", "% Particip."]]
        for _, r in por_categoria.iterrows():
            rows.append([
                Paragraph(
                    str(r.get("categoria_nombre", "") or ""), st["body"],
                ),
                _fmt_qty(r.get("cantidad", 0)),
                _fmt_money(r.get("ventas_netas", 0)),
                _fmt_money(r.get("costo", 0)),
                _fmt_money(r.get("margen", 0)),
                _fmt_pct(r.get("margen_pct", 0)),
                _fmt_int(r.get("n_facturas", 0)),
                _fmt_pct(r.get("participacion_pct", 0)),
            ])
        tbl = Table(
            rows,
            colWidths=[4.5 * cm, 1.6 * cm, 2.3 * cm, 2 * cm, 2 * cm,
                       1.7 * cm, 1.4 * cm, 1.9 * cm],
            repeatRows=1,
        )
        tbl.setStyle(_data_table_style(numeric_cols=[1, 2, 3, 4, 5, 6, 7]))
        story.append(tbl)
        story.append(Spacer(1, 10))

    # Por producto (top N)
    story.append(Paragraph(
        f"Top {top_productos} productos del día", st["h2"],
    ))
    if por_producto is None or por_producto.empty:
        story.append(Paragraph(
            "Sin productos para mostrar.", st["small"],
        ))
    else:
        pp = por_producto.copy()
        if "ventas_netas" in pp.columns:
            pp = pp.sort_values("ventas_netas", ascending=False)
        pp = pp.head(top_productos)
        rows = [["Producto", "Cant.", "Ventas netas", "Costo",
                 "Margen", "Margen %"]]
        for _, r in pp.iterrows():
            rows.append([
                Paragraph(
                    str(r.get("product_nombre", "") or ""), st["body"],
                ),
                _fmt_qty(r.get("cantidad", 0)),
                _fmt_money(r.get("ventas_netas", 0)),
                _fmt_money(r.get("costo", 0)),
                _fmt_money(r.get("margen", 0)),
                _fmt_pct(r.get("margen_pct", 0)),
            ])
        tbl = Table(
            rows,
            colWidths=[6.5 * cm, 1.6 * cm, 2.6 * cm, 2.4 * cm, 2.4 * cm,
                       1.9 * cm],
            repeatRows=1,
        )
        tbl.setStyle(_data_table_style(numeric_cols=[1, 2, 3, 4, 5]))
        story.append(tbl)

    return story


# ── Función principal ──
def build_daily_pdf(
    fecha: _date,
    empresa: str,
    nit: str,
    estado_caja: dict,
    ventas_kpis=None,
    ventas_por_categoria: pd.DataFrame | None = None,
    ventas_por_producto: pd.DataFrame | None = None,
    top_productos: int = 25,
) -> bytes:
    """
    Genera el PDF diario combinado y lo devuelve como bytes (listo para
    `st.download_button` o para adjuntar a un correo).
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
        topMargin=2.2 * cm, bottomMargin=1.5 * cm,
        title=f"Informe Diario {fecha} — Casa de los Mineros",
        author="Casa de los Mineros — Cartera",
    )

    st = _styles()
    story: list = []

    # Sección Caja
    story.extend(_section_estado_caja(estado_caja or {}, st))
    story.append(PageBreak())

    # Sección Ventas
    story.extend(_section_ventas_diarias(
        ventas_kpis, ventas_por_categoria, ventas_por_producto,
        top_productos, st,
    ))

    decorator = _make_page_decorator(empresa, nit, fecha)
    doc.build(story, onFirstPage=decorator, onLaterPages=decorator)
    return buf.getvalue()
