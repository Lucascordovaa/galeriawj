# -*- coding: utf-8 -*-
"""
Define (ou troca) a senha do painel /admin.

Uso:
  python app/set_password.py            -> pergunta a senha (oculta)
  python app/set_password.py MINHASENHA -> define direto pelo argumento
"""
import getpass
import json
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_auth.json")


def main():
    if len(sys.argv) > 1:
        pw = sys.argv[1]
    else:
        pw = getpass.getpass("Nova senha do admin: ")
        if pw != getpass.getpass("Confirme a senha: "):
            print("As senhas não conferem. Nada foi alterado.")
            return
    if len(pw) < 4:
        print("Senha muito curta (mínimo 4 caracteres). Nada foi alterado.")
        return

    data = {}
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["hash"] = generate_password_hash(pw)
    data.setdefault("secret", secrets.token_hex(32))
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print("Senha do admin definida com sucesso.")


if __name__ == "__main__":
    main()
