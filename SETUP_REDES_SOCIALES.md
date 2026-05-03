# Guía de setup — Redes Sociales y Google Analytics

Tres plataformas, tres niveles de dificultad. **Mi recomendación: empieza por GA4** (30 min, aprobación inmediata). Mientras Meta y TikTok están en revisión (semanas), ya tienes algo funcionando.

---

## 1. Google Analytics 4 (30 min, aprobación inmediata) ⭐ EMPIEZA POR AQUÍ

### Paso 1.1 — Crear proyecto en Google Cloud

1. Entra a [console.cloud.google.com](https://console.cloud.google.com).
2. Arriba a la izquierda click el selector de proyecto → **Nuevo proyecto**.
3. Nombre: `cartera-mineros-ga` (o el que quieras). Click **Crear**.

### Paso 1.2 — Habilitar la API

1. Con el proyecto activo, en la barra de búsqueda arriba escribe **"Google Analytics Data API"**.
2. Click el resultado → **Habilitar**.

### Paso 1.3 — Crear Service Account

1. Menú lateral → **IAM y administración → Cuentas de servicio**.
2. Click **+ Crear cuenta de servicio**.
3. Nombre: `streamlit-ga-reader`. Click **Crear y continuar**.
4. **No** asignes roles aún. Click **Continuar** → **Listo**.
5. En la lista, click la cuenta recién creada.
6. Pestaña **Claves** → **Agregar clave** → **Crear clave nueva** → **JSON** → **Crear**.
7. Se descarga un archivo `xxx.json`. **Guárdalo seguro**, no lo compartas.

### Paso 1.4 — Dar acceso a la propiedad de GA4

1. Abre [analytics.google.com](https://analytics.google.com) y entra a tu propiedad.
2. Abajo a la izquierda click **Administrador** (engranaje).
3. En la columna **Propiedad** → **Acceso a la propiedad**.
4. Click el botón **+** (arriba derecha) → **Agregar usuarios**.
5. Pega el email del service account (algo tipo `streamlit-ga-reader@xxx.iam.gserviceaccount.com`, lo encuentras en el JSON descargado o en la consola).
6. Rol: **Lector**. Quita el check de "Notificar por email". Click **Agregar**.

### Paso 1.5 — Anotar el Property ID

1. En GA4 → **Administrador** → **Propiedad** → **Detalles de la propiedad**.
2. Anota el ID de la propiedad (formato: `123456789`).

### Paso 1.6 — Configurar en Streamlit Cloud

1. Ve a `share.streamlit.io` → tu app `reportescdm` → **⋯ → Settings → Secrets**.
2. Agrega al final:

```toml
[ga4]
property_id = "123456789"
service_account_json = '''
{
  "type": "service_account",
  "project_id": "cartera-mineros-ga",
  "private_key_id": "xxx",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "streamlit-ga-reader@xxx.iam.gserviceaccount.com",
  ...
}
'''
```

3. Pega el contenido COMPLETO del JSON descargado dentro de `service_account_json`. Conserva las triples comillas `'''` para que TOML acepte multilínea.
4. Click **Save**. La app se reinicia y la pestaña Google Analytics quedará con ✅ API conectada.

---

## 2. Meta — Facebook + Instagram (1 hora setup + 1-3 semanas App Review)

### Paso 2.1 — Crear app de Meta

1. Entra a [developers.facebook.com](https://developers.facebook.com) con tu cuenta de Meta.
2. Click **Mis apps → Crear app**.
3. Tipo: **Empresa** (Business). Click **Siguiente**.
4. Nombre: `cartera-mineros-redes`. Email de contacto: el tuyo. Click **Crear app**.

### Paso 2.2 — Agregar productos

1. En el panel de la app → **Agregar productos**.
2. Agrega: **Inicio de sesión de Facebook** y **Pages API**.
3. Si tienes Instagram Business: agrega también **Instagram Graph API**.

### Paso 2.3 — Configurar permisos

1. Menú lateral → **Revisión de la app → Permisos y funciones**.
2. Solicita estos permisos (click "Solicitar"):
   - `pages_show_list`
   - `pages_read_engagement`
   - `read_insights`
   - `instagram_basic` (si usas IG)
   - `instagram_manage_insights` (si usas IG)

> ⚠️ **App Review** puede tardar **1 a 3 semanas**. Sin estos permisos aprobados solo puedes leer datos de cuentas de prueba.

### Paso 2.4 — Generar token de acceso (largo)

1. Mientras esperas la aprobación, puedes empezar con un **User Token** corto (60 días).
2. Ve a [Graph API Explorer](https://developers.facebook.com/tools/explorer).
3. Selecciona tu app arriba. Selecciona los permisos del paso 2.3.
4. Click **Generate Access Token** → autoriza con tu cuenta.
5. Copia el token.
6. Para hacerlo de larga duración (60 días):
   ```
   GET https://graph.facebook.com/v19.0/oauth/access_token?
     grant_type=fb_exchange_token&
     client_id={app_id}&
     client_secret={app_secret}&
     fb_exchange_token={token_corto}
   ```

### Paso 2.5 — Anotar Page ID e Instagram Business Account ID

```
GET /me/accounts?access_token={token}
```
Te da una lista de Pages con su `id`.

Para IG (si tienes IG Business vinculado a la Page):
```
GET /{page-id}?fields=instagram_business_account&access_token={token}
```

### Paso 2.6 — Configurar en Streamlit Cloud

```toml
[meta]
access_token = "EAA..."
facebook_page_id = "1234567890"
instagram_user_id = "1789..."  # opcional, déjalo si no tienes IG
```

---

## 3. TikTok Business / Marketing API (1 hora setup + 2-4 semanas App Review)

> ⚠️ **TikTok es la más restrictiva.** No todas las apps son aprobadas. Si tu cuenta de TikTok Business es nueva o no tienes mucho contenido, puede ser rechazada. Si pasa eso, sigues usando upload manual de CSV.

### Paso 3.1 — Cuenta de Developer

1. Entra a [developers.tiktok.com](https://developers.tiktok.com) con tu cuenta TikTok.
2. **Manage apps → Create new app**.
3. Selecciona el tipo de API:
   - **Login Kit + Display API**: para datos básicos del perfil propio.
   - **Research API**: para datos públicos (científicos, periodistas).
   - **Marketing API**: si quieres datos de Ads.
4. Llena los datos de la app y solicita revisión.

### Paso 3.2 — Implementar OAuth 2.0

1. Configura **Redirect URI** (puede ser un placeholder al principio).
2. Anota `client_key` y `client_secret`.
3. Implementa el flujo:
   ```
   https://www.tiktok.com/v2/auth/authorize/?
     client_key=...&
     scope=user.info.basic,video.list&
     response_type=code&
     redirect_uri=...
   ```
4. Recibes `code` → cámbialo por `access_token`:
   ```
   POST https://open.tiktokapis.com/v2/oauth/token/
   ```

### Paso 3.3 — Configurar en Streamlit Cloud (cuando tengas tokens)

```toml
[tiktok]
access_token = "act.xxx"
refresh_token = "rft.xxx"
client_key = "xxx"
client_secret = "xxx"
```

---

## Resumen — Orden recomendado

1. **Hoy mismo (30 min)**: Setup GA4. ✅
2. **Esta semana (1h)**: Crear app de Meta y solicitar permisos. Empezar el reloj de App Review.
3. **Esta semana (1h)**: Crear app de TikTok y solicitar permisos.
4. **Mientras esperas**: Usa upload manual de CSV en cada pestaña.
5. **Cuando lleguen las aprobaciones**: Pega los tokens en `secrets` y la app conecta automáticamente.

Si en algún paso te traba algo, mándame captura del error y te ayudo a debuggear.
