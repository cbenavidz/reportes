#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Obtiene el refresh_token de TikTok (Display API) a partir del
'authorization code' que se genera al autorizar la app en el navegador.

USO:
    python3 tiktok_get_token.py <AUTHORIZATION_CODE>

El script pide el Client secret de forma segura (no se ve en pantalla)
y al final imprime el bloque [tiktok] listo para pegar en Streamlit Secrets.

IMPORTANTE: el authorization code dura pocos minutos. Ejecuta este script
inmediatamente despues de autorizar.
"""
import sys
import json
import getpass
import urllib.parse
import urllib.request
import urllib.error

CLIENT_KEY = "sbawjddhflipzmgx1p"            # Sandbox "CDM Reportes"
REDIRECT_URI = "https://casadelosmineros.com/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


def main():
    if len(sys.argv) < 2:
        print("ERROR: falta el authorization code.")
        print("Uso: python3 tiktok_get_token.py <AUTHORIZATION_CODE>")
        sys.exit(1)

    # El code puede venir con sufijos tipo '*1' o parametros extra: limpiamos.
    code = sys.argv[1].strip()
    code = code.split("&")[0]
    if code.endswith("*1"):
        code = code[:-2]

    client_secret = getpass.getpass(
        "Pega el Client secret de TikTok y presiona Enter: "
    ).strip()
    if not client_secret:
        print("ERROR: client_secret vacio.")
        sys.exit(1)

    data = urllib.parse.urlencode({
        "client_key": CLIENT_KEY,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR de red: {exc}")
        sys.exit(1)

    try:
        result = json.loads(body)
    except Exception:  # noqa: BLE001
        print("Respuesta no valida de TikTok:")
        print(body)
        sys.exit(1)

    if "refresh_token" not in result:
        print("\n=== TikTok devolvio un error ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("\nPosibles causas:")
        print(" - El code expiro (dura pocos minutos): vuelve a autorizar.")
        print(" - El Client secret es incorrecto.")
        print(" - El redirect_uri no coincide con el registrado.")
        sys.exit(1)

    refresh_token = result["refresh_token"]
    access_token = result.get("access_token", "")
    open_id = result.get("open_id", "")

    print("\n=== TOKENS OBTENIDOS ===")
    print(f"open_id       : {open_id}")
    print(f"access_token  : {access_token[:14]}...  (dura ~24h, se refresca solo)")
    print(f"refresh_token : {refresh_token}")
    print(f"scope         : {result.get('scope', '')}")

    print("\n=== PEGA ESTE BLOQUE EN STREAMLIT SECRETS ===\n")
    print("[tiktok]")
    print(f'client_key    = "{CLIENT_KEY}"')
    print(f'client_secret = "{client_secret}"')
    print(f'refresh_token = "{refresh_token}"')
    print()
    print("Listo. El refresh_token dura ~1 anio.")


if __name__ == "__main__":
    main()
