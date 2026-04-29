# Deploy en Streamlit Community Cloud (gratis)

Esta guía te lleva de tu Mac a una URL pública del estilo
`casamineros-cartera.streamlit.app` que cualquier miembro de tu equipo
puede abrir desde cualquier computador, sin instalar nada.

**Tiempo estimado: 25 minutos.** Solo se hace una vez.

---

## Antes de empezar — checklist

- [ ] Tu Odoo es accesible desde internet (no solo dentro de la red de
      la oficina). Para Odoo.sh siempre lo es.
- [ ] Tienes una cuenta de GitHub (gratis: https://github.com/signup).
- [ ] Estás cómodo con que el código de la app sea visible en un repo
      público. Las credenciales NO se ven — solo el código.

Si alguno falla, esta guía no aplica. Para los otros caminos
(repo privado, Render, etc.) avísame.

---

## Paso 1 — Verificar que las credenciales NO van al repo

Esto es lo más importante. Antes de subir nada, asegúrate de que tu
`.env` y `config/auth_config.yaml` están en el `.gitignore`.

Abre `.gitignore` y verifica que estas líneas están presentes:

```
.env
*.env
!.env.example
config/auth_config.yaml
.streamlit/secrets.toml
```

Si las cinco están, sigue al paso 2. Si falta alguna, agrégala antes de
hacer cualquier `git add`.

---

## Paso 2 — Inicializar Git y subir el código a GitHub

Abre Terminal y ejecuta uno por uno:

```bash
cd "~/Documents/Claude/Projects/Cartera casa de los mineros"
git init
git add .
git status   # ← REVISA QUE NO APAREZCAN .env NI auth_config.yaml
```

Si `git status` muestra alguno de esos archivos como "to be committed",
**detente**: tu `.gitignore` no los está excluyendo. Bórralos del index
con `git rm --cached .env config/auth_config.yaml` antes de seguir.

Si todo se ve bien:

```bash
git commit -m "Inicial: app de cartera"
```

Crea el repo en GitHub:

1. Entra a https://github.com/new.
2. Nombre del repo: `cartera-casa-mineros` (o el que quieras).
3. Visibilidad: **Public**.
4. NO marques ninguna opción de "Initialize this repository with…".
5. Click **Create repository**.

GitHub te muestra unos comandos. Usa estos (cambia `tu-usuario`):

```bash
git remote add origin https://github.com/tu-usuario/cartera-casa-mineros.git
git branch -M main
git push -u origin main
```

Vas a ver tu código en `https://github.com/tu-usuario/cartera-casa-mineros`.

---

## Paso 3 — Conectar Streamlit Community Cloud

1. Entra a https://share.streamlit.io.
2. Click **Sign in with GitHub** y autoriza.
3. Click **New app** (botón arriba a la derecha).
4. Llena el formulario:
   - **Repository**: `tu-usuario/cartera-casa-mineros`
   - **Branch**: `main`
   - **Main file path**: `app.py`
   - **App URL**: `casamineros-cartera` (o el subdominio que prefieras).
5. **NO** hagas click en Deploy todavía. Antes de deployar, click en
   **Advanced settings** → **Secrets**.

---

## Paso 4 — Configurar los Secrets

En la caja de Secrets pegas el contenido de
`.streamlit/secrets.toml.example` PERO con tus credenciales reales.

Tu `.env` actual te da los valores. Copia y pega en el formato TOML:

```toml
ODOO_URL = "https://grupocdm.odoo.com"
ODOO_DB = "groupcdm-main-9189116"
ODOO_USERNAME = "carlos@grupocdm.co"
ODOO_API_KEY = "TU_API_KEY_AQUI"

CUENTA_CARTERA = "1305"
AGING_RANGE_1 = "30"
AGING_RANGE_2 = "60"
AGING_RANGE_3 = "90"
UMBRAL_SCORING_A = "5"
UMBRAL_SCORING_B = "15"
UMBRAL_SCORING_C = "30"
CACHE_TTL_MINUTES = "15"
DATA_FLOOR_DATE = "2025-09-01"

[auth.users.carlos]
name = "Carlos Benavides"
email = "carlos@casadelosmineros.com.co"
role = "admin"
password_hash = "EL_HASH_QUE_HAY_EN_TU_auth_config.yaml"
```

El `password_hash` lo sacas de tu `config/auth_config.yaml` actual:
es la línea `password_hash: ...`.

Para agregar más usuarios después, repites el bloque
`[auth.users.NOMBRE]` con su propio hash. Para generar un hash nuevo:

```bash
python3 -c "import hashlib; print(hashlib.sha256('PASSWORD'.encode()).hexdigest())"
```

Click **Save**.

---

## Paso 5 — Deploy

Vuelve al formulario y click **Deploy**.

Streamlit Cloud:
1. Clona tu repo.
2. Instala `requirements.txt`.
3. Arranca `streamlit run app.py`.
4. Te muestra los logs en vivo.

El primer deploy tarda 3–5 minutos. Cuando termine, te lleva a la app.
Si ves la pantalla de login, ya quedó.

---

## Paso 6 — Compartir la URL

La URL final es algo como:

```
https://casamineros-cartera.streamlit.app
```

Esa es la URL que mandas a tu equipo. Cada uno entra con su usuario y
contraseña que tú le creaste en la sección `[auth.users.NOMBRE]` de los
secrets.

---

## Cómo actualizar la app después

Cada vez que cambies código en tu Mac:

```bash
git add .
git commit -m "Descripción del cambio"
git push
```

Streamlit Cloud detecta el push y redeploya automáticamente en ~2 min.
Tus usuarios ven la nueva versión la próxima vez que recargan la página.

---

## Mantenimiento mínimo

- **Cambiar credenciales de Odoo**: Streamlit Cloud → tu app → ⋯ →
  Settings → Secrets. Editas y guardas. La app reinicia sola.
- **Agregar un usuario**: igual que arriba, agregas un bloque
  `[auth.users.NOMBRE]` con su hash.
- **Ver logs si algo falla**: Streamlit Cloud → tu app → ⋯ → Manage app
  → ahí ves los logs en vivo.
- **Apagar la app**: ⋯ → Delete app (no se puede pausar gratis; o vive
  o se borra).

---

## Si algo sale mal

1. **"App can't connect to Odoo"** → Tus secrets de Odoo están mal o
   tu Odoo no es accesible desde internet. Verifica entrando a
   `ODOO_URL` desde el navegador del computador de un colega.
2. **"ModuleNotFoundError"** → Falta una librería en `requirements.txt`.
   Agrégala, `git push`, y se redeploya.
3. **"st.secrets has no key X"** → Olvidaste algún secret. Vuelve a
   Settings → Secrets y agrégalo.
4. **App se queda cargando para siempre** → Probablemente el extractor
   está descargando datos pesados. Streamlit Cloud tiene 1 GB de RAM en
   el plan gratuito; si tu base de Odoo tiene millones de filas, puede
   no caber. Solución: bajar `months_back` en `compute_full_analysis` o
   subir el filtro de fecha.

---

## Costos y límites del plan gratuito

- **Apps**: ilimitadas (mientras el repo sea público).
- **Tráfico**: ilimitado.
- **RAM**: 1 GB por app.
- **CPU**: compartida.
- **Sleep**: la app NO se duerme (a diferencia de Render free tier).
- **Privacidad**: el código es público, los secrets son privados.

Si en algún momento necesitas repo privado, Streamlit for Teams cuesta
desde USD $250/mes — bastante caro. Alternativa para repo privado
gratis: Render free tier (con la app que se duerme tras 15 min sin uso).
