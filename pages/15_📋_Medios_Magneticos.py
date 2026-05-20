# -*- coding: utf-8 -*-
"""
Página: Medios Magnéticos — Información Exógena DIAN.

Genera todos los formatos requeridos por la DIAN a partir de los datos
contables de Odoo.

Formatos generados:
  1001, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 1011, 1012,
  1015, 1056, 1647, 2275, 2276

Para revisar antes de presentar a la DIAN. Se exporta como Excel
multi-hoja para auditoría.
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import streamlit as st

from src.auth import logout_button, require_auth
from src.data_loader import (
    get_odoo_client,
    load_account_movements,
    load_chart_of_accounts,
    load_companies,
)
from src.extractor import extract_partners
from src.medios_magneticos import (
    build_formato_1001,
    build_formato_1003,
    build_formato_1004,
    build_formato_1005,
    build_formato_1006,
    build_formato_1007,
    build_formato_1008,
    build_formato_1009,
    build_formato_1010,
    build_formato_1011,
    build_formato_1012,
    build_formato_1015,
    build_formato_1056,
    build_formato_1647,
    build_formato_2275,
    build_formato_2276,
    build_validacion_por_cuenta,
    diagnosticar_formato_1001,
    generar_excel_medios_magneticos,
)
from src.ui_components import render_company_context, render_sidebar_filters


st.set_page_config(
    page_title="Medios Magnéticos | Cartera",
    page_icon="📋",
    layout="wide",
)

require_auth()
logout_button()

st.title("📋 Información Exógena — Medios Magnéticos DIAN")
st.caption(
    "Genera los formatos requeridos por la DIAN. Use solo para revisar "
    "antes de presentar oficialmente. Los conceptos DIAN se mapean "
    "automáticamente desde el PUC; ajuste si su empresa usa códigos diferentes."
)

# Sidebar
filters = render_sidebar_filters()
if filters["company_ids"] is not None and len(filters["company_ids"]) == 0:
    st.warning("Selecciona al menos una empresa.")
    st.stop()


# ── Selector de año ──
st.markdown("### 📅 Año fiscal a reportar")
today = date.today()
year_default = today.year - 1
year_fiscal = st.number_input(
    "Año gravable",
    min_value=2020, max_value=today.year,
    value=year_default, step=1,
    help=(
        "Año del cual se reporta la información. Típicamente el año "
        "anterior al actual (ej: en 2026 se reporta 2025)."
    ),
)
year_fiscal = int(year_fiscal)
st.caption(
    f"📅 Se generará la información del año **{year_fiscal}** "
    f"(01/01 → 31/12) para presentar en {year_fiscal + 1}."
)

# Banner empresa
companies_df = load_companies()
render_company_context(companies_df, filters["company_ids"])


# ── Formatos a generar ──
st.markdown("### 📂 Formatos a generar")
TODOS_LOS_FORMATOS = {
    "1001 — Pagos y retenciones practicadas": True,
    "1003 — Retenciones que nos practicaron": True,
    "1004 — Descuentos tributarios": True,
    "1005 — IVA descontable": True,
    "1006 — IVA generado": True,
    "1007 — Ingresos recibidos": True,
    "1008 — Cuentas por cobrar al cierre": True,
    "1009 — Cuentas por pagar al cierre": True,
    "1010 — Socios y accionistas": True,
    "1011 — Declaraciones tributarias (consolidado)": True,
    "1012 — Saldos disponible e inversiones": True,
    "1015 — Pasivos al cierre": True,
    "1056 — Devoluciones, anulaciones, rescisiones": True,
    "1647 — Ingresos recibidos para terceros": True,
    "2275 — Costos y deducciones (detallado)": True,
    "2276 — Pagos laborales": True,
}
seleccionados = st.multiselect(
    "Formatos a incluir en el reporte",
    options=list(TODOS_LOS_FORMATOS.keys()),
    default=list(TODOS_LOS_FORMATOS.keys()),
)

if not seleccionados:
    st.warning("Selecciona al menos un formato.")
    st.stop()


# ── Botones de control ──
ccol1, ccol2 = st.columns([3, 1])
with ccol2:
    if st.button("🗑️ Limpiar caché", use_container_width=True,
                 help="Fuerza recarga del plan de cuentas y movimientos."):
        st.cache_data.clear()
        st.success("Caché limpiado. Vuelve a generar el reporte.")
        st.rerun()

# ── Botón generar ──
with ccol1:
    generar_btn = st.button(
        f"🔄 Generar reporte año {year_fiscal}",
        type="primary",
        use_container_width=True,
    )

if generar_btn:
    fecha_desde = date(year_fiscal, 1, 1)
    fecha_hasta = date(year_fiscal, 12, 31)

    with st.spinner(
        f"Cargando datos del año {year_fiscal}... esto puede tardar 1-2 min"
    ):
        # 1. Plan de cuentas
        chart_df = load_chart_of_accounts(company_ids=filters["company_ids"])

        # 2. Movimientos contables del año (formatos de período) + acumulado
        # hasta 31 dic (para 1008/1009/1012/1015 que son saldos al cierre)
        moves_year = load_account_movements(
            date_from=fecha_desde.isoformat(),
            date_to=fecha_hasta.isoformat(),
            company_ids=filters["company_ids"],
        )
        # Para saldos al cierre necesitamos TODO el histórico hasta 31 dic
        moves_to_eoy = load_account_movements(
            date_from="2000-01-01",  # desde el principio
            date_to=fecha_hasta.isoformat(),
            company_ids=filters["company_ids"],
        )

        # 3. Partners (terceros) — IMPORTANTE: traer TODOS los que aparezcan
        # en movimientos (clientes Y proveedores Y empleados). El default de
        # extract_partners es only_customers=True, lo que excluye proveedores.
        # Para garantizar que TODOS los terceros usados en asientos contables
        # del año fiscal aparezcan, extraemos sus IDs de los moves y pedimos
        # exactamente esos al extractor (modo partner_ids).
        try:
            client = get_odoo_client()
            partner_ids_en_moves = set()
            for df_src in (moves_year, moves_to_eoy):
                if df_src is not None and not df_src.empty and "partner_id" in df_src.columns:
                    ids = df_src["partner_id"].dropna().unique().tolist()
                    partner_ids_en_moves.update(int(i) for i in ids if i)
            partner_ids_list = list(partner_ids_en_moves)

            if partner_ids_list:
                # Modo: pedir EXACTAMENTE esos IDs (ignora only_customers
                # y filtros de active/company_id internamente).
                partners_df = extract_partners(
                    client,
                    partner_ids=partner_ids_list,
                )
            else:
                # Fallback: traer clientes + proveedores activos
                partners_df = extract_partners(
                    client,
                    only_customers=False,
                    company_ids=list(filters["company_ids"])
                    if filters["company_ids"] else None,
                )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Error cargando partners: {exc}")
            partners_df = pd.DataFrame()

    if moves_year is None or moves_year.empty:
        st.error(
            f"No se encontraron movimientos contables para el año {year_fiscal}. "
            "Verifica el año seleccionado y los filtros de empresa."
        )
        st.stop()

    # Validar que todos los movimientos sean posted (defensa adicional)
    if "parent_state" in moves_year.columns:
        posted_count = (moves_year["parent_state"] == "posted").sum()
        otros_count = len(moves_year) - posted_count
        if otros_count > 0:
            st.warning(
                f"⚠️ {otros_count} líneas con estado distinto a 'posted' "
                "fueron filtradas (borradores o cancelados)."
            )
            moves_year = moves_year[moves_year["parent_state"] == "posted"]
            moves_to_eoy = moves_to_eoy[moves_to_eoy["parent_state"] == "posted"]
        st.success(
            f"✅ Datos cargados: {posted_count:,} líneas PUBLICADAS del "
            f"año {year_fiscal} · {len(moves_to_eoy):,} líneas históricas "
            f"(para saldos) · {len(partners_df):,} terceros. "
            "Borradores y cancelados excluidos."
        )
    else:
        st.success(
            f"✅ Datos cargados: {len(moves_year):,} líneas del año "
            f"{year_fiscal}, {len(moves_to_eoy):,} líneas históricas, "
            f"{len(partners_df):,} terceros."
        )

    # ── Diagnóstico del plan de cuentas ──
    # Verificar que los códigos de cuenta se cargaron correctamente.
    # Si vienen mayormente vacíos (problema Odoo 17+ con localización CO),
    # avisar al usuario antes de generar los formatos.
    if chart_df is not None and not chart_df.empty:
        codes_serie = chart_df["code"].astype(str) if "code" in chart_df.columns else pd.Series([])
        codes_vacios = codes_serie.isin(["", "False", "nan", "None"]).sum() if len(codes_serie) else 0
        total_cuentas = len(chart_df)
        if total_cuentas > 0:
            pct_vacios = codes_vacios / total_cuentas * 100
            if pct_vacios > 30:
                st.warning(
                    f"⚠️ Plan de cuentas: {codes_vacios:,}/{total_cuentas:,} "
                    f"({pct_vacios:.0f}%) sin código. Esto puede afectar la "
                    "calidad del Formato 1001 (columna `cuentas_contables` "
                    "podría salir vacía)."
                )
            else:
                st.caption(
                    f"📚 Plan de cuentas: {total_cuentas:,} cuentas, "
                    f"{total_cuentas - codes_vacios:,} con código."
                )

            # Diagnóstico detallado: qué estrategia resolvió los códigos
            diag_chart = getattr(chart_df, "attrs", {}).get("chart_code_diag") or {}
            if diag_chart:
                with st.expander(
                    "🔬 Diagnóstico técnico: ¿cómo se resolvieron los códigos?",
                    expanded=(pct_vacios > 30),
                ):
                    dcol1, dcol2, dcol3 = st.columns(3)
                    with dcol1:
                        st.metric("Total cuentas", diag_chart.get("total_cuentas", 0))
                        st.metric("Inicial sin código",
                                  diag_chart.get("inicial_vacios", 0))
                    with dcol2:
                        st.metric("Resueltos por code_mapping_ids",
                                  diag_chart.get("via_code_mapping_ids", 0))
                        st.metric("Resueltos por search_read mapping",
                                  diag_chart.get("via_mapping_directo", 0))
                    with dcol3:
                        st.metric("Resueltos por read() con context",
                                  diag_chart.get("via_read_context", 0))
                        st.metric("Resueltos por regex display_name",
                                  diag_chart.get("via_display_name", 0)
                                  + diag_chart.get("via_name_regex", 0))
                    if diag_chart.get("finales_vacios", 0) > 0:
                        st.error(
                            f"❌ {diag_chart.get('finales_vacios')} cuentas "
                            "quedaron SIN código después de las 4 estrategias. "
                            "Probablemente son cuentas auxiliares sin nombre "
                            "ni código en Odoo. Estas no aparecerán en la "
                            "columna `cuentas_contables`."
                        )
                    else:
                        st.success(
                            "✅ Todas las cuentas resolvieron un código."
                        )

            # Mostrar muestra del chart en expander para auditar
            with st.expander("🔍 Ver muestra del plan de cuentas (auditoría)"):
                muestra_cols = [c for c in ["id", "code", "name", "account_type"]
                               if c in chart_df.columns]
                # Mostrar primero las que tienen código vacío
                df_show = chart_df[muestra_cols].copy()
                df_show["_sin_code"] = df_show["code"].astype(str).isin(["", "False", "nan", "None"])
                df_show = df_show.sort_values("_sin_code", ascending=False).drop(columns="_sin_code")
                st.caption("Primeras 30 cuentas (priorizando las que tienen código vacío):")
                st.dataframe(
                    df_show.head(30),
                    use_container_width=True, hide_index=True,
                )

    # ── Construir formatos ──
    formatos: dict[str, pd.DataFrame] = {}

    def _build_safe(label: str, fn, *args):
        """Wrapper que captura errores y registra el formato."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"⚠️ Error en {label}: {exc}")
            return pd.DataFrame()

    builders = [
        ("1001 — Pagos y retenciones practicadas",
         build_formato_1001, moves_year, chart_df, partners_df, year_fiscal),
        ("1003 — Retenciones que nos practicaron",
         build_formato_1003, moves_year, chart_df, partners_df, year_fiscal),
        ("1004 — Descuentos tributarios",
         build_formato_1004, moves_year, chart_df, partners_df, year_fiscal),
        ("1005 — IVA descontable",
         build_formato_1005, moves_year, chart_df, partners_df, year_fiscal),
        ("1006 — IVA generado",
         build_formato_1006, moves_year, chart_df, partners_df, year_fiscal),
        ("1007 — Ingresos recibidos",
         build_formato_1007, moves_year, chart_df, partners_df, year_fiscal),
        # 1008/1009/1012/1015 usan moves históricos para saldos al cierre
        ("1008 — Cuentas por cobrar al cierre",
         build_formato_1008, moves_to_eoy, chart_df, partners_df, year_fiscal),
        ("1009 — Cuentas por pagar al cierre",
         build_formato_1009, moves_to_eoy, chart_df, partners_df, year_fiscal),
        ("1010 — Socios y accionistas",
         lambda p, y: build_formato_1010(p, y), partners_df, year_fiscal),
        ("1011 — Declaraciones tributarias (consolidado)",
         lambda m, c, y: build_formato_1011(m, c, y),
         moves_year, chart_df, year_fiscal),
        ("1012 — Saldos disponible e inversiones",
         lambda m, c, y: build_formato_1012(m, c, y),
         moves_to_eoy, chart_df, year_fiscal),
        ("1015 — Pasivos al cierre",
         build_formato_1015, moves_to_eoy, chart_df, partners_df, year_fiscal),
        ("1056 — Devoluciones, anulaciones, rescisiones",
         build_formato_1056, moves_year, chart_df, partners_df, year_fiscal),
        ("1647 — Ingresos recibidos para terceros",
         build_formato_1647, moves_year, chart_df, partners_df, year_fiscal),
        ("2275 — Costos y deducciones (detallado)",
         build_formato_2275, moves_year, chart_df, partners_df, year_fiscal),
        ("2276 — Pagos laborales",
         build_formato_2276, moves_year, chart_df, partners_df, year_fiscal),
    ]

    progress = st.progress(0)
    status = st.empty()
    selected_set = set(seleccionados)
    builders_filtered = [b for b in builders if b[0] in selected_set]

    for i, (label, fn, *args) in enumerate(builders_filtered):
        status.text(f"Generando {label}...")
        df = _build_safe(label, fn, *args)
        formatos[label] = df
        progress.progress((i + 1) / len(builders_filtered))

    status.empty()
    progress.empty()

    # Guardar en session state para no recalcular al pasar de tab
    st.session_state["mm_formatos"] = formatos
    st.session_state["mm_year"] = year_fiscal
    st.session_state["mm_moves_year"] = moves_year
    st.session_state["mm_chart_df"] = chart_df
    st.session_state["mm_partners_df"] = partners_df


# ── Mostrar resultados ──
if "mm_formatos" in st.session_state:
    formatos = st.session_state["mm_formatos"]
    year_fiscal = st.session_state["mm_year"]

    st.markdown("---")
    st.markdown(f"### 📊 Resumen — Año {year_fiscal}")

    # Tabla resumen
    resumen = pd.DataFrame([
        {
            "Formato": k,
            "Filas": len(v),
            "Estado": "✅ OK" if not v.empty else "⚠️ VACÍO",
        }
        for k, v in formatos.items()
    ])
    st.dataframe(resumen, hide_index=True, use_container_width=True)

    # Validación cruzada cuenta × tercero (para auditar contra el mayor)
    validacion = None
    if (
        "mm_moves_year" in st.session_state
        and "mm_chart_df" in st.session_state
        and "mm_partners_df" in st.session_state
    ):
        validacion = build_validacion_por_cuenta(
            st.session_state["mm_moves_year"],
            st.session_state["mm_chart_df"],
            st.session_state["mm_partners_df"],
            year_fiscal,
        )

    # Descarga del Excel (con hoja de validación adicional)
    excel_buffer = io.BytesIO()
    generar_excel_medios_magneticos(
        formatos, excel_buffer, year_fiscal, validacion=validacion,
    )
    excel_buffer.seek(0)
    st.download_button(
        label=f"⬇️ Descargar Excel medios magnéticos {year_fiscal}",
        data=excel_buffer,
        file_name=f"medios_magneticos_{year_fiscal}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )

    st.markdown("---")
    st.markdown("### 🔍 Vista previa por formato")

    # Tabs para cada formato
    tab_labels = [k.split(" — ")[0] for k in formatos.keys()]
    tabs = st.tabs(tab_labels)
    for tab, (label, df) in zip(tabs, formatos.items()):
        with tab:
            st.markdown(f"#### {label}")
            if df is None or df.empty:
                st.info(
                    "Sin datos para este formato en el año seleccionado. "
                    "Puede ser normal si la empresa no tiene operaciones "
                    "que apliquen a este formato."
                )
            else:
                st.caption(f"Total filas: {len(df):,}")
                st.dataframe(
                    df,
                    use_container_width=True, hide_index=True,
                    height=500,
                )

    # ── Diagnóstico Formato 1001 ──
    st.markdown("---")
    st.markdown("### 🔍 Diagnóstico Formato 1001 (terceros con valores en cero)")
    st.caption(
        "Identifica casos sospechosos: terceros con pagos pero sin NIT, "
        "retenciones huérfanas (sin pago en el mismo asiento), y "
        "retenciones cuyo tercero no coincide con el del pago."
    )
    if (
        "mm_moves_year" in st.session_state
        and "mm_chart_df" in st.session_state
        and "mm_partners_df" in st.session_state
    ):
        diag = diagnosticar_formato_1001(
            st.session_state["mm_moves_year"],
            st.session_state["mm_chart_df"],
            st.session_state["mm_partners_df"],
            year_fiscal,
        )

        # A) Terceros sin NIT (resumen + detalle por asiento)
        sin_nit = diag.get("sin_nit")
        sin_nit_det = diag.get("sin_nit_detalle")
        if sin_nit is not None and not sin_nit.empty:
            with st.container(border=True):
                st.markdown(
                    f"#### ⚠️ A) {len(sin_nit)} terceros con pagos pero SIN NIT"
                )
                st.caption(
                    "Estos pagos aparecerán en el formato 1001 sin "
                    "identificación de tercero. Hay que actualizar el NIT "
                    "en Odoo (Contactos → editar → campo 'NIT/Cédula')."
                )
                st.markdown(
                    "**Resumen por tercero** (con info de diagnóstico — "
                    "muestra qué campos están vacíos en el partner):"
                )
                st.dataframe(
                    sin_nit,
                    column_config={
                        "partner_id": st.column_config.NumberColumn(
                            "ID", format="%d",
                        ),
                        "nombre": "Tercero",
                        "monto_pagos": st.column_config.NumberColumn(
                            "Total pagos", format="$%,.0f",
                        ),
                        "n_asientos": st.column_config.NumberColumn(
                            "# Asientos", format="%d",
                        ),
                        "vat": "vat",
                        "l10n_co_doc": "l10n_co_doc",
                        "ref": "ref",
                        "ident_doc": "ident_doc",
                        "encontrado_en_db": st.column_config.CheckboxColumn(
                            "En res.partner?",
                        ),
                        "is_company": st.column_config.CheckboxColumn(
                            "Empresa?",
                        ),
                        "active": st.column_config.CheckboxColumn(
                            "Activo?",
                        ),
                    },
                    use_container_width=True, hide_index=True, height=400,
                )
                st.caption(
                    "💡 Si `encontrado_en_db = False` → el partner_id de la "
                    "línea no existe en res.partner (caso raro). "
                    "Si `encontrado_en_db = True` pero todos los campos de "
                    "documento están vacíos → el partner existe pero no "
                    "tiene NIT registrado en ningún campo conocido."
                )

                # Detalle por asiento contable
                if sin_nit_det is not None and not sin_nit_det.empty:
                    st.markdown(
                        "**Detalle por asiento contable** "
                        f"({len(sin_nit_det):,} líneas):"
                    )
                    # Filtros para buscar fácil
                    cflt1, cflt2 = st.columns([2, 2])
                    with cflt1:
                        partners_uniq = sin_nit_det["nombre_tercero"].dropna().unique().tolist()
                        partner_sel = st.selectbox(
                            "Filtrar por tercero",
                            options=["(Todos)"] + sorted(partners_uniq),
                            key="mm_sin_nit_partner",
                        )
                    with cflt2:
                        q = st.text_input(
                            "Buscar (asiento, referencia, descripción)",
                            key="mm_sin_nit_buscar",
                        )

                    det_show = sin_nit_det.copy()
                    if partner_sel and partner_sel != "(Todos)":
                        det_show = det_show[
                            det_show["nombre_tercero"] == partner_sel
                        ]
                    if q:
                        ql = q.lower()
                        # Buscar en columnas de texto
                        text_cols = [
                            c for c in [
                                "move_id_name", "ref", "name",
                                "account_code", "account_name",
                            ] if c in det_show.columns
                        ]
                        if text_cols:
                            mask = pd.Series(False, index=det_show.index)
                            for c in text_cols:
                                mask = mask | (
                                    det_show[c].astype(str).str.lower()
                                    .str.contains(ql, na=False, regex=False)
                                )
                            det_show = det_show[mask]

                    st.caption(f"{len(det_show):,} líneas")
                    st.dataframe(
                        det_show,
                        column_config={
                            "partner_id": st.column_config.NumberColumn(
                                "ID", format="%d",
                            ),
                            "nombre_tercero": "Tercero",
                            "date": st.column_config.DateColumn(
                                "Fecha", format="YYYY-MM-DD",
                            ),
                            "move_id_name": "Asiento",
                            "move_id": "ID asiento",
                            "ref": "Referencia",
                            "name": "Descripción",
                            "account_code": "Cuenta",
                            "account_name": "Nombre cuenta",
                            "monto": st.column_config.NumberColumn(
                                "Monto", format="$%,.0f",
                            ),
                        },
                        use_container_width=True, hide_index=True, height=500,
                    )

                    # Botón de descarga del detalle
                    csv_data = det_show.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        label="⬇️ Descargar detalle en CSV",
                        data=csv_data,
                        file_name=f"sin_nit_detalle_{year_fiscal}.csv",
                        mime="text/csv",
                    )
        else:
            st.success("✅ A) Todos los terceros con pagos tienen NIT registrado.")

        # B) Retenciones huérfanas
        ret_h = diag.get("ret_huerfanas")
        if ret_h is not None and not ret_h.empty:
            with st.container(border=True):
                st.markdown(
                    f"#### 🔻 B) {len(ret_h)} retenciones HUÉRFANAS "
                    "(sin pago en el mismo asiento)"
                )
                st.caption(
                    "Estas retenciones están contabilizadas pero NO hay "
                    "un pago/gasto al mismo tercero en el mismo asiento. "
                    "Puede indicar un error de contabilización o que el "
                    "pago se hizo a otro tercero. Reviselo en Odoo."
                )
                cols_show = [
                    c for c in ["partner_id", "nombre", "monto_ret"]
                    if c in ret_h.columns
                ]
                st.dataframe(
                    ret_h[cols_show],
                    column_config={
                        "partner_id": st.column_config.NumberColumn(
                            "ID", format="%d",
                        ),
                        "nombre": "Tercero",
                        "monto_ret": st.column_config.NumberColumn(
                            "Total retención", format="$%,.0f",
                        ),
                    },
                    use_container_width=True, hide_index=True, height=300,
                )
        else:
            st.success(
                "✅ B) Todas las retenciones tienen un pago asociado en "
                "el mismo asiento."
            )

        # C) Retenciones con partner diferente al del pago
        ret_dif = diag.get("ret_diferente_partner")
        if ret_dif is not None and not ret_dif.empty:
            with st.container(border=True):
                st.markdown(
                    f"#### 🚨 C) {len(ret_dif)} retenciones con TERCERO "
                    "DIFERENTE al del pago"
                )
                st.caption(
                    "La retención está marcada con un partner_id distinto "
                    "al del gasto en el mismo asiento. Esto es un ERROR "
                    "de contabilización: en Odoo cambiar el tercero de la "
                    "línea de retención para que coincida con el proveedor."
                )
                cols_show = [
                    c for c in [
                        "move_id", "partner_id", "nombre_ret",
                        "partner_pago", "nombre_pago",
                        "account_code", "monto_ret",
                    ] if c in ret_dif.columns
                ]
                st.dataframe(
                    ret_dif[cols_show],
                    column_config={
                        "move_id": "Asiento",
                        "partner_id": "ID retención",
                        "nombre_ret": "Tercero retención",
                        "partner_pago": "ID pago",
                        "nombre_pago": "Tercero pago",
                        "account_code": "Cuenta ret.",
                        "monto_ret": st.column_config.NumberColumn(
                            "Monto", format="$%,.0f",
                        ),
                    },
                    use_container_width=True, hide_index=True, height=300,
                )
        else:
            st.success(
                "✅ C) Todas las retenciones tienen el mismo tercero "
                "que el pago asociado."
            )

    # Notas y advertencias
    st.markdown("---")
    st.markdown("### ⚠️ Notas importantes")
    st.markdown("""
    - **Conceptos DIAN automáticos**: el mapeo PUC → concepto DIAN es
      genérico. Revisa cada formato y ajusta los conceptos según la
      Resolución DIAN del año correspondiente.
    - **Umbral $100k en 1001**: solo se reportan pagos > $100k. Ajusta
      según el umbral oficial de cada año (típicamente $1M-$10M).
    - **Formato 1010 (socios)**: Odoo no marca socios automáticamente.
      Completa manualmente el porcentaje de participación y valor.
    - **Formato 2276 (laborales)**: si la empresa usa el módulo HR de
      Odoo, los datos de empleados pueden estar en otra tabla. Verifica
      que los terceros que aparecen sean empleados reales.
    - **Validar antes de presentar**: usa el prevalidador de la DIAN
      (MUISCA) antes de subir oficialmente.
    """)
else:
    st.info(
        "👆 Selecciona el año, los formatos, y haz click en "
        "**Generar reporte** para producir los archivos."
    )
