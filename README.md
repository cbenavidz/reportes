# 💰 Cartera Inteligente — Casa de los Mineros

App de gestión de cuentas por cobrar conectada en vivo a tu Odoo 19. Calcula rotación de cartera, aging, califica clientes A/B/C/D, dispara alertas de riesgo y propone un plan de cobro priorizado.

## 🎯 ¿Qué hace?

- **Rotación de cartera** (en días y veces) con período configurable
- **Aging report** por rangos (corriente, 1-30, 31-60, 61-90, 91-180, +180 días)
- **Calificación A/B/C/D** automática por hábito de pago, mora y comportamiento
- **Alertas de riesgo**: facturas críticas, exceso de cupo, concentración, sin pagos
- **Plan de cobro priorizado** con acción sugerida y datos de contacto
- **Próximos vencimientos** para cobro proactivo
- **Login multiusuario** con roles (admin, cartera, gerencia)
- **Export a Excel** de cualquier vista

## 🏗️ Arquitectura

```
┌──────────────┐    XML-RPC     ┌──────────────┐
│  Odoo 19     │ ◄────────────► │  Streamlit   │
│  (Odoo.sh)   │                │   App        │
└──────────────┘                └──────────────┘
                                      │
                                      ▼
                                ┌──────────────┐
                                │  Pandas      │
                                │  Plotly      │
                                └──────────────┘
```

## 🚀 Instalación local (Mac)

### 1. Requisitos
- Python 3.10+
- pip

### 2. Crear entorno virtual e instalar dependencias

```bash
cd "/Users/carlosbenavidesz/Documents/Claude/Projects/Cartera casa de los mineros"

# Crear venv
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt
```

### 3. Configurar credenciales

El archivo `.env` ya está configurado con tus credenciales reales (no se sube a Git por el `.gitignore`).

Para verificar:

```bash
cat .env
```

### 4. Probar la conexión a Odoo

```bash
python3 test_conexion.py
```

Deberías ver algo como:

```
✓ Versión de Odoo detectada: saas~17.5+e
✓ Autenticado como UID = 12
✓ Clientes activos: 245
✓ Facturas de venta posted: 1,832
✓ Facturas abiertas: 67
✓ Pagos de clientes: 950
✅ ¡Conexión exitosa!
```

Si falla, revisa la sección de **Troubleshooting** abajo.

### 5. Correr la app

```bash
streamlit run app.py
```

Abre el navegador en `http://localhost:8501`.

### 6. Login

Usuarios y contraseñas iniciales (CAMBIAR después):

| Usuario   | Password       | Rol      |
|-----------|----------------|----------|
| carlos    | cartera2026    | admin    |
| cartera   | cobranza2026   | cartera  |
| gerencia  | gerencia2026   | gerencia |

Para cambiar passwords, edita `config/auth_config.yaml` y genera el hash con:

```bash
python3 -c "import hashlib; print(hashlib.sha256('NUEVA_PASS'.encode()).hexdigest())"
```

## ☁️ Despliegue en la nube (para que el equipo acceda)

### Opción A: Streamlit Community Cloud (GRATIS)

1. Crea repo en GitHub (privado) y sube todo **excepto `.env` y `config/auth_config.yaml`**.
2. Ve a [share.streamlit.io](https://share.streamlit.io) y conecta tu repo.
3. En **Settings > Secrets**, pega:

```toml
ODOO_URL = "https://grupocdm.odoo.com"
ODOO_DB = "groupcdm-main-9189116"
ODOO_USERNAME = "carlos@grupocdm.co"
ODOO_API_KEY = "tu_api_key_aqui"
```

4. La app se despliega automáticamente en `https://tu-app.streamlit.app`.

### Opción B: Render.com (más control, ~$7/mes)

1. Crea un Web Service en [render.com](https://render.com) apuntando a tu repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`
4. Configura las mismas variables de entorno que en Streamlit Cloud.

### Opción C: VPS propio (DigitalOcean, AWS, etc.)

```bash
# En el servidor
git clone tu-repo
cd "Cartera casa de los mineros"
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Crear .env con credenciales
nano .env

# Correr en background con systemd o tmux
streamlit run app.py --server.port=8501 --server.address=0.0.0.0
```

Recomiendo poner Nginx delante con HTTPS (Let's Encrypt).

## 🔧 Configuración del análisis

Edita `.env` para ajustar:

- `CUENTA_CARTERA`: cuenta contable de cuentas por cobrar (default: 1305)
- `AGING_RANGE_1/2/3`: días para los rangos de aging
- `UMBRAL_SCORING_A/B/C`: umbrales de días de mora para clasificación
- `CACHE_TTL_MINUTES`: cuánto tiempo cachear los datos (default: 15)

## 📋 Estructura del proyecto

```
.
├── app.py                          # Página principal (Dashboard)
├── pages/                          # Páginas adicionales (Streamlit multi-page)
│   ├── 1_📊_Aging_y_Facturas.py
│   ├── 2_👥_Clientes_y_Scoring.py
│   ├── 3_🚨_Alertas.py
│   ├── 4_📞_Plan_de_Cobro.py
│   └── 5_⚙️_Configuración.py
├── src/
│   ├── odoo_client.py              # Cliente XML-RPC
│   ├── extractor.py                # Extracción de facturas/pagos/clientes
│   ├── analyzer.py                 # Rotación, aging, métricas por cliente
│   ├── scoring.py                  # Calificación A/B/C/D
│   ├── alerts.py                   # Reglas de alertas
│   ├── recommendations.py          # Plan de cobro
│   ├── data_loader.py              # Pipeline + caché Streamlit
│   ├── ui_components.py            # Componentes reutilizables
│   └── auth.py                     # Login multiusuario
├── config/
│   └── auth_config.yaml            # Usuarios y roles (NO subir a Git)
├── casa_mineros_cartera/           # [FASE 2] Módulo Odoo nativo (parcial)
├── requirements.txt
├── .env                            # Credenciales (NO subir a Git)
├── .env.example
├── .gitignore
└── test_conexion.py                # Script para validar conexión
```

## 🔍 Troubleshooting

### "Autenticación falló"

- Verifica el nombre exacto de la base de datos en Odoo.sh → Settings.
- Asegúrate de que el usuario `carlos@grupocdm.co` exista en esa DB.
- Regenera la API key en Odoo: **Preferencias → Cuenta → Claves API**.

### "No se pudo conectar a..."

- Revisa que la URL no tenga `/` al final.
- Verifica que tu Odoo.sh esté online: abre la URL en el navegador.

### "Faltan variables de entorno"

- Asegúrate que el archivo `.env` exista y tenga las 4 variables (`ODOO_URL`, `ODOO_DB`, `ODOO_USERNAME`, `ODOO_API_KEY`).

### Las cifras no coinciden con el reporte de Odoo

- La rotación se calcula con la fórmula estándar: `(saldo promedio / ventas a crédito) × 365`.
- El saldo "histórico" es aproximado (usa `amount_residual_signed` actual). Para reconstrucción exacta histórica usaríamos `account.move.line` con conciliaciones — está implementado en `extract_receivable_lines` pero no se usa en el flujo principal por velocidad. Si necesitas precisión histórica al céntimo, lo activamos.

## 🛣️ Roadmap

**Fase 1 (esta app)** ✅
- Dashboard, aging, scoring, alertas, plan de cobro
- Conexión a Odoo vía API
- Login multiusuario
- Despliegue en nube

**Fase 2 (módulo Odoo nativo)** 🚧 _parcialmente iniciado en `casa_mineros_cartera/`_
- Una vez validada esta app, portamos toda la lógica a un módulo Odoo
- Vistas dentro del ERP, dashboard nativo, sin dependencia externa
- Crons para recalcular scoring/alertas automáticamente
- Reportes PDF integrados al menú de Contabilidad

## 🔐 Seguridad

- ⚠️ El archivo `.env` y `config/auth_config.yaml` **NUNCA** deben subirse a Git (ya excluidos vía `.gitignore`).
- Cambia las contraseñas iniciales después del primer login.
- Considera regenerar la API key periódicamente desde Odoo.
- Para producción seria, migra el login a `streamlit-authenticator` con cookies firmadas y bcrypt.

---

**Desarrollado para Casa de los Mineros · 2026**
