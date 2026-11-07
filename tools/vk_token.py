"""Записать VK-токен в .env из ссылки, которую VK отдал после «Разрешить».

Запуск: python tools/vk_token.py
Вставьте ссылку из адресной строки целиком (со #access_token=... или #code=...).
Если там code — утилита попробует обменять его на токен. Токен никуда не
отправляется, только в локальный файл .env.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import requests

ENV = Path(__file__).resolve().parents[1] / ".env"

# Известные пары standalone-приложений VK (id, секрет).
# Код авторизации обменивается парой того приложения, на которое выдавался.
APP_SECRETS = [
    ("2685278", "lxhD8OD7kbMsCvPidwKQ"),   # Kate Mobile
    ("2274003", "hHbZxrka2uZ6cdHtNTKL"),   # VK Android
]
REDIRECTS = ["https://oauth.vk.ru/blank.html", "https://oauth.vk.com/blank.html"]


def exchange_code(code: str) -> str:
    errors = []
    for client_id, client_secret in APP_SECRETS:
        for redirect in REDIRECTS:
            resp = requests.get(
                "https://oauth.vk.ru/access_token",
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect,
                    "code": code,
                },
                timeout=30,
            )
            data = resp.json()
            if "access_token" in data:
                print(f"  обменял через приложение {client_id}")
                return data["access_token"]
            errors.append(f"{client_id}: {data.get('error_description') or data}")
    raise RuntimeError("Обмен кода не удался:\n  " + "\n  ".join(errors))


def write_env(token: str) -> None:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    if re.search(r"^VK_TOKEN=.*$", text, re.M):
        text = re.sub(r"^VK_TOKEN=.*$", f"VK_TOKEN={token}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\nVK_TOKEN={token}\n"
    ENV.write_text(text, encoding="utf-8")


def main() -> None:
    url = input("Вставь ссылку из адресной строки после «Разрешить»: ").strip()

    m = re.search(r"access_token=([^&\s]+)", url)
    if m:
        token = m.group(1)
        print("В ссылке уже есть access_token.")
    else:
        code_m = re.search(r"[#?&]code=([^&\s]+)", url)
        if not code_m:
            sys.exit("Не нашёл ни access_token, ни code в ссылке — проверь, что скопирована целиком.")
        print("В ссылке код (code), обмениваю на токен...")
        token = exchange_code(code_m.group(1))

    write_env(token)
    print(f"Готово: VK_TOKEN записан в .env (начинается на {token[:8]}..., {len(token)} символов)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"FAIL: {exc}")
