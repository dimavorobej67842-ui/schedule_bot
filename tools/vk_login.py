"""Получить VK-токен без копирования из адресной строки.

Запуск: python tools/vk_login.py
Откроется окно Edge (вход сохраняется между запусками): войдите в VK и нажмите
«Разрешить». Дальше токен вытаскивается программно — схемой из библиотеки vk_api
(данс через login.vk.ru), которая не требует client_secret приложения.
Токен записывается только в локальный .env.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vk_token import write_env  # noqa: E402

PROFILE_DIR = Path(__file__).resolve().parent / "vk_profile"
SCOPE = "messages,docs,offline"
SCOPE_MASK = 140492255  # как в vk_api: полный набор прав приложения
APPS = ["2685278", "2274003", "6121396"]  # Kate, VK Android, VK Admin
AUTH_URL = (
    "https://oauth.vk.ru/authorize?client_id={app}&display=page"
    "&redirect_uri=https://oauth.vk.ru/blank.html&scope=" + SCOPE +
    "&response_type=token&v=5.131"
)

RE_TOKEN_URL = re.compile(r'location\.href = "(.*?)"\+addr;')
RE_AUTH_TOKEN_URL = re.compile(r"window\.init = ({.*?});")
TOKEN_RE = re.compile(r"access_token=([^&\s]+)")
CODE_RE = re.compile(r"[#?&]code=([^&\s]+)")


def token_from_url(url: str) -> str | None:
    m = TOKEN_RE.search(url)
    return m.group(1) if m else None


def on_blank(url: str) -> bool:
    """Мы действительно на oauth blank.html (редирект), а не на странице входа."""
    u = urlparse(url)
    return u.hostname in ("oauth.vk.ru", "oauth.vk.com") and u.path.rstrip("/").endswith("blank.html")


def cookies_to_jar(context) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for c in context.cookies():
        jar.set(c["name"], c["value"], domain=c.get("domain") or "", path=c.get("path") or "/")
    return jar


def dance_for_token(session: requests.Session, app_id: str) -> str | None:
    """Данс vk_api._api_login: authorize -> connect_internal -> auth.getOauthToken."""
    resp = session.get(
        "https://oauth.vk.ru/authorize",
        params={"client_id": app_id, "scope": SCOPE_MASK, "response_type": "token"},
        timeout=30,
    )
    tok = token_from_url(resp.url)
    if tok:
        return tok

    m = RE_TOKEN_URL.search(resp.text)
    if m:
        inner = session.get(m.group(1), timeout=30)
        tok = token_from_url(inner.url)
        if tok:
            return tok

    if "redirect_uri" not in resp.url:
        print(f"    дэнс [{app_id}]: неожиданный ответ {resp.url[:90]}")
        return None

    m = RE_AUTH_TOKEN_URL.search(resp.text)
    if not m:
        print(f"    дэнс [{app_id}]: не нашёл window.init на странице VK ID")
        return None
    auth_json = json.loads(m.group(1))
    return_auth_hash = auth_json["data"]["hash"]["return_auth"]

    connect = session.post(
        "https://login.vk.ru/?act=connect_internal",
        data={
            "uuid": "",
            "service_group": "",
            "return_auth_hash": return_auth_hash,
            "version": 1,
            "app_id": app_id,
        },
        headers={"Origin": "https://id.vk.ru"},
        timeout=30,
    ).json()
    if connect.get("type") != "okay":
        print(f"    дэнс [{app_id}]: connect_internal -> {connect.get('type')}")
        return None

    got = session.post(
        "https://api.vk.ru/method/auth.getOauthToken",
        data={
            "hash": return_auth_hash,
            "auth_user_hash": connect["data"]["auth_user_hash"],
            "app_id": app_id,
            "client_id": app_id,
            "scope": SCOPE_MASK,
            "access_token": connect["data"]["access_token"],
            "is_seamless_auth": 1,
            "v": "5.207",
        },
        timeout=30,
    ).json()
    token = (got.get("response") or {}).get("access_token")
    if token:
        return token
    print(f"    дэнс [{app_id}]: auth.getOauthToken -> {got}")
    return None


def main() -> None:
    token = None
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="msedge", headless=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        print("Открываю авторизацию VK. Войди в аккаунт и нажми «Разрешить»...")
        page.goto(AUTH_URL.format(app=APPS[0]), wait_until="domcontentloaded")
        deadline = time.time() + 600
        tick = 0
        while time.time() < deadline:
            url = page.url
            tok = token_from_url(url)
            if tok:
                print("  токен пойман прямо из редиректа")
                token = tok
                break
            if CODE_RE.search(url) or on_blank(url):
                print("  редирект завершён, дальше работаю сам")
                break
            if tick % 15 == 0:
                print(f"  жду... сейчас: {urlparse(url).hostname}")
            tick += 1
            time.sleep(2)
        jar = cookies_to_jar(context)
        context.close()

    if token is None:
        if not any(c.name == "remixsid" for c in jar):
            sys.exit("Не вижу входа в VK (нет remixsid). Похоже, вход не был завершён.")
        session = requests.Session()
        session.cookies = jar
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )
        print("Вытаскиваю токен программно (данс vk_api)...")
        for app_id in APPS:
            token = dance_for_token(session, app_id)
            if token:
                break

    if not token:
        sys.exit("Токен получить не удалось — напиши ассистенту, что показало окно.")
    write_env(token)
    print(f"Готово: VK_TOKEN записан в .env (начинается на {token[:8]}..., {len(token)} символов)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"FAIL: {type(exc).__name__}: {exc}")
