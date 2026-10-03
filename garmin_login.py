#!/usr/bin/env python3
"""
garmin_login.py — Genera UNA sola vez el string de tokens de Garmin para el servidor.

Lo ejecutas una vez (en tu PC, en una shell del host, o en Google Colab desde el
celular). Maneja MFA. Al final imprime y guarda el valor que debes poner en la
variable de entorno GARMIN_TOKENS del servidor. Los tokens duran ~1 año y se
refrescan solos; no tienes que volver a hacer esto salvo que caduquen o Garmin
revoque la sesión.

Uso:
    pip install garminconnect
    python garmin_login.py
"""
import getpass
import os

from garminconnect import Garmin

OUT_FILE = "garmin_tokens.txt"


def main() -> None:
    email = os.environ.get("GARMIN_EMAIL") or input("Email de Garmin Connect: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Contraseña: ")

    print("\nIniciando sesión en Garmin Connect...")
    garmin = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Código MFA (si Garmin lo solicita): ").strip(),
    )
    garmin.login()

    # Sanity check
    print(f"Sesión OK como: {garmin.get_full_name()}")

    tokens = garmin.client.dumps()
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(tokens)

    print("\n" + "=" * 70)
    print("GARMIN_TOKENS  (copia TODA la línea siguiente como valor de la variable)")
    print("=" * 70)
    print(tokens)
    print("=" * 70)
    print(f"\nTambién se guardó en: {os.path.abspath(OUT_FILE)}")
    print("Pega este valor en la variable de entorno GARMIN_TOKENS del servidor.")
    print("⚠️  Trátalo como una contraseña: da acceso de lectura a tu cuenta Garmin.")


if __name__ == "__main__":
    main()
