# Setup: Informe Diario por correo automático

Esta guía configura el envío automático del PDF combinado (Estado de
Caja + Ventas Diarias) cada día a las **6:05 PM Bogotá** (al cierre de
operación).

El flujo:

1. **GitHub Actions** corre el script `scripts/send_daily_report.py`
   todos los días a las 23:05 UTC = 18:05 Bogotá.
2. El script se conecta a Odoo, genera el PDF y lo manda por SMTP.

---

## 1. Crear App Password de Gmail (5 min)

Si vas a enviar desde una cuenta de Gmail (la más fácil):

1. Entra a [myaccount.google.com](https://myaccount.google.com) con
   la cuenta desde la que quieres enviar (puede ser
   `reportes@casadelosmineros.com.co` o tu personal).
2. Activa la **verificación en 2 pasos** si no está activa.
3. Ve a [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
4. Crea una nueva App Password: nombre "Informes CDM".
5. Copia los **16 caracteres** que te muestra. Esa es tu
   `SMTP_PASSWORD` (no es tu contraseña normal de Google).

Si usas otro proveedor (Outlook, SendGrid, AWS SES, Zoho, etc.) los
parámetros SMTP cambian — escríbeme y te ajusto el setup.

---

## 2. Agregar los secretos a GitHub

En el repo `cbenavidz/reportes` en GitHub:

1. **Settings → Secrets and variables → Actions → New repository secret**.
2. Agrega estos 10 secretos uno por uno:

| Nombre del secret | Valor de ejemplo |
|---|---|
| `ODOO_URL` | `https://...odoo.com` (la misma que tienes en Streamlit Secrets) |
| `ODOO_DB` | `nombre_de_tu_base` |
| `ODOO_USERNAME` | `carlos@bzcapital.co` |
| `ODOO_API_KEY` | (la misma API key) |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `reportes@casadelosmineros.com.co` (la cuenta Gmail) |
| `SMTP_PASSWORD` | la App Password de 16 caracteres |
| `SMTP_FROM` | `Casa de los Mineros <reportes@casadelosmineros.com.co>` |
| `DAILY_REPORT_TO` | `carlos@casadelosmineros.com.co,mlzorag@gmail.com` |

Los 4 primeros (`ODOO_*`) son exactamente los mismos que ya tienes en
Streamlit Cloud.

---

## 3. Probar manualmente desde GitHub

Antes de esperar al cron de las 6:05 PM:

1. En GitHub → pestaña **Actions** → workflow **"Informe Diario CDM"**.
2. Botón **"Run workflow"** → "Run workflow" (deja la rama main).
3. Espera ~2 min y revisa el resultado.
   - ✅ verde = corrió y envió el correo. Mira tu inbox.
   - ❌ rojo = clic en el job para ver el log y el error exacto.

Errores comunes:
- `OdooConnectionError` → credenciales de Odoo mal configuradas.
- `SMTPAuthenticationError` → App Password mal copiada o no activada
  la verificación en 2 pasos.

---

## 4. Configurar el botón de envío manual en la app Streamlit

Si también quieres el botón "Enviar ahora" en las páginas 20
(Caja) y 21 (Ventas Diarias), agrega esta sección a tu **Streamlit
Cloud → Settings → Secrets** (junto a `[tiktok]` y los demás):

```toml
[smtp]
host = "smtp.gmail.com"
port = 587
user = "reportes@casadelosmineros.com.co"
password = "xxxxxxxxxxxxxxxx"   # Gmail App Password (16 chars)
from_addr = "Casa de los Mineros <reportes@casadelosmineros.com.co>"
recipients = "carlos@casadelosmineros.com.co,mlzorag@gmail.com"
```

Después de guardar, la app reinicia sola; el botón "Enviar ahora"
queda funcional.

---

## 5. Probar el envío manual desde la app

1. Abre **reportescdm.streamlit.app** → **💵 Informe de Caja** o
   **📊 Informe de Ventas Diarias**.
2. Al final de la página, sección **"Informe diario combinado (PDF)"**:
   - Clic en **"📄 Generar PDF"** → espera a que termine.
   - Aparece el botón **"⬇️ Descargar PDF"** y el bloque de envío
     por correo.
   - Clic en **"✉️ Enviar ahora"** → confirma que llega al inbox.

---

## 6. Cambiar la hora del envío automático

Si quieres mover el horario, edita
`.github/workflows/daily_report.yml` y cambia el `cron`. El formato
es UTC. Bogotá es UTC-5, así que:

- 18:05 Bogotá → `5 23 * * *`
- 19:00 Bogotá → `0 0 * * *` (medianoche UTC)
- 7:00 Bogotá → `0 12 * * *`

Después de cambiar, hacer push y GitHub Actions toma la nueva
programación automáticamente.

---

## Resolución de problemas

**El cron no se dispara**
- GitHub Actions a veces atrasa los crons hasta 15 min en horarios
  de alta demanda. Es normal.
- Verifica que el repo no esté archivado y que Actions esté habilitado.

**"OdooConnectionError" en el log**
- Verifica que el usuario ODOO_USERNAME tenga acceso a Odoo y la
  API key esté activa. Las credenciales son las mismas de Streamlit.

**"SMTPAuthenticationError"**
- App Password mal copiada (debe ser de 16 caracteres, sin espacios).
- Verifica que la cuenta Gmail tenga 2FA activa (requisito para App
  Passwords).

**Correo no llega**
- Revisa la carpeta de SPAM.
- Si Gmail bloquea el envío, verifica que el `SMTP_USER` y
  `SMTP_FROM` sean la misma cuenta (Gmail no permite suplantar otra
  cuenta sin SPF/DKIM).
