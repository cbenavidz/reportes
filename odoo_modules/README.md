# Módulos Odoo nativos — Casa de los Mineros

Estructura propuesta para portar la app Streamlit a módulos Odoo 19 nativos.
Cada módulo es **independiente y autoinstalable**.

## 📦 Estructura

```
odoo_modules/
├── casa_dashboard/                    ← MÓDULO MAESTRO (depende de los demás)
│   ├── __manifest__.py
│   ├── views/menu_root.xml
│   └── static/src/                    ← OWL components para dashboards ejecutivos
│
├── casa_cartera_scoring/              ← ✅ SCAFFOLD COMPLETO (este)
│   ├── models/
│   │   ├── res_partner.py             ← campos cartera + scoring
│   │   ├── account_move.py            ← aging por factura
│   │   ├── cartera_score_history.py
│   │   └── cartera_alert.py
│   ├── views/
│   ├── data/cartera_cron.xml          ← recompute nocturno
│   └── security/
│
├── casa_sales_analytics/              ← TODO
│   └── (extender sale.report con volumen, ruta, etc.)
│
├── casa_financial_reports/            ← TODO
│   └── (account.report XML para Enterprise)
│
├── casa_partner_intelligence/         ← TODO
│   └── (mapa, predicción, comparativa peer)
│
└── casa_social_connector/             ← TODO
    └── (Meta API + GA4 cache local)
```

## 🚀 Instalación

### Opción A: Instalación en Odoo propio (self-hosted)

1. Clonar este folder en `addons-path` de tu instancia Odoo:
   ```bash
   cp -r odoo_modules/* /path/to/odoo/custom-addons/
   ```

2. Reiniciar Odoo:
   ```bash
   sudo systemctl restart odoo
   ```

3. En Odoo: **Apps → Actualizar lista** → buscar "Casa" → Instalar

### Opción B: odoo.sh (Enterprise cloud)

1. Hacer fork del repo Odoo en GitHub
2. Crear branch `staging-casa-modules`
3. Pegar `odoo_modules/casa_cartera_scoring/` en el branch
4. Push → odoo.sh detecta y despliega automáticamente
5. En Apps: Instalar

## 🎯 ¿Qué hace el módulo `casa_cartera_scoring`?

**Extiende `res.partner` con campos computados (store=True):**

| Campo | Tipo | Descripción |
|---|---|---|
| `cartera_saldo_abierto` | Monetary | Suma de saldo residual de facturas no pagadas |
| `cartera_saldo_vencido` | Monetary | Saldo de facturas vencidas |
| `cartera_dso_real` | Float | DSO real con FIFO matching factura↔pago |
| `cartera_plazo_otorgado` | Integer | Plazo nominal del payment_term |
| `cartera_cumplimiento_pct` | Float | % de facturas pagadas dentro del plazo |
| `cartera_score` | Selection | A/B/C/D/S (auto-calculado) |
| `cartera_habito_pago` | Selection | "Excelente" / "Bueno" / "Lento" / "Crítico" |
| `cartera_aging_30/60/90/120/mas` | Monetary | Aging buckets |

**Extiende `account.move` con:**

| Campo | Descripción |
|---|---|
| `cartera_dias_al_pago` | Días entre emisión y pago (o desde emisión si abierta) |
| `cartera_dias_vencido` | Días vencidos (si aplica) |
| `cartera_aging_bucket` | Bucket de aging para esta factura |
| `cartera_proxima_a_vencer` | True si vence en próximos 7 días |

**Nuevos modelos:**

- `casa.cartera.score.history` — Snapshot mensual + manual del scoring
- `casa.cartera.alert` — Alertas auto-generadas (próxima a vencer, score bajó, etc.)

**Crons:**

- 02:00 AM diario: recalcular scoring para clientes con facturas en últimos 12m
- 07:00 AM diario: generar alertas frescas (próximas a vencer + bajadas de score)

**Vistas:**

- 📊 Kanban con código de color por score
- 📋 List con decoraciones (rojo para D, verde para A)
- 📈 Pivot por vendedor × score
- 📉 Graph bar de saldo por score
- 🔍 Form con tab "Cartera & Scoring" en partner

**Acceso:**

- Menú: `Contabilidad → 📊 Cartera`
- Submenús: Clientes (Scoring), Alertas, Histórico de Scores

## ⚡ Performance esperada

| Operación | App Streamlit | Módulo Odoo nativo |
|---|---|---|
| Cargar lista de 2000 clientes con KPIs | ~60s | ~1s (campos stored, SQL) |
| Filtrar por score "D" | ~5s | <100ms |
| Pivot vendedor × score | ~10s | <500ms |
| Drill-down a facturas del cliente | ~2s | Instantáneo (botón nativo) |
| Recompute nocturno (2000 clientes) | N/A | ~30-60s background |

## 🔧 Customización

### Cambiar umbrales de score

Editar `models/res_partner.py` función `_compute_cartera_score`:

```python
if cumplimiento >= 85 and delta <= 5:
    partner.cartera_score = "A"
# ... etc
```

### Agregar nuevo bucket de aging

1. Agregar campo en `res_partner.py`:
   ```python
   cartera_aging_150 = fields.Monetary(...)
   ```
2. Agregar lógica en `_compute_cartera_kpis`
3. Agregar al kanban/list view

### Modificar cron schedule

Editar `data/cartera_cron.xml`:

```xml
<field name="interval_number">2</field>  <!-- cada 2 horas -->
<field name="interval_type">hours</field>
```

## 🧪 Testing

```bash
# Tests unitarios
./odoo-bin -d test_db -i casa_cartera_scoring --test-enable --stop-after-init

# Test manual de scoring
./odoo-bin shell -d production_db
>>> partner = env['res.partner'].browse(123)
>>> partner.action_recompute_cartera()
>>> print(partner.cartera_score)
```

## 📚 Próximos módulos (mismo patrón)

### `casa_sales_analytics`

```python
class SaleReport(models.Model):
    _inherit = "sale.report"
    
    volume_galones = fields.Float(compute=...)
    is_route_sale = fields.Boolean(...)
    categoria_lubricante = fields.Char(...)
```

### `casa_financial_reports` (Enterprise)

```xml
<record id="report_pyg_casa" model="account.report">
    <field name="name">Estado de Resultados - Casa de los Mineros</field>
    <field name="line_ids" eval="[
        (0, 0, {'name': 'Ingresos', 'expression_ids': [
            (0, 0, {'engine': 'aggregation', 'formula': 'income.balance'})
        ]}),
        # ... etc
    ]"/>
</record>
```

## 💡 Tips de desarrollo

1. **Siempre `store=True`** en campos que vayas a usar en search/filter/groupby
2. **Usa `read_group`** para agregaciones masivas, no iteres records
3. **`@api.depends`** tiene que listar TODOS los campos que se usan en el compute
4. **Activa SQL logging** en debug: `--log-sql` para ver qué queries genera
5. **Cron jobs**: usa `sudo` en cron para evitar permisos extraños

## 🎓 Recursos

- [Odoo 19 Documentation](https://www.odoo.com/documentation/19.0/)
- [OWL Framework](https://github.com/odoo/owl)
- [account.report engine](https://www.odoo.com/documentation/19.0/applications/finance/accounting/reporting.html)
- [Best practices Odoo dev](https://www.odoo.com/documentation/19.0/contributing/development/coding_guidelines.html)

## 📋 Roadmap

- [x] `casa_cartera_scoring` — scaffold completo (este módulo)
- [ ] `casa_sales_analytics` — extender sale.report
- [ ] `casa_financial_reports` — account.report XML
- [ ] `casa_partner_intelligence` — predicción + mapa
- [ ] `casa_social_connector` — Meta + GA4 con cache
- [ ] `casa_dashboard` — OWL components ejecutivos
- [ ] Tests unitarios para cada módulo
- [ ] CI/CD con odoo-bin --test-enable
