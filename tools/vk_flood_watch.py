#!/usr/bin/env python3
"""Ожидание снятия флуд-контроля VK: проверка раз в 30 минут.

Каждая попытка — короткое уведомление владельцу в Telegram (и только ему:
получатель — CHAT_ID из .env). При выздоровлении — финальное «✅» и
самоотключение таймера.

Работает независимо от schedule_bot (отправка через curl + SOCKS5, как в watchdog).
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, "/opt/schedule_bot/tools")
from watchdog import load_env, send_tg  # noqa: E402

TIMER = "vk-flood-watch.timer"
COUNTER = "/root/.vk_flood_watch_count"


def vk_call(env: dict[str, str], method: str, **params):
    data = {"access_token": env.get("VK_TOKEN", ""), "v": "5.199"}
    data.update(params)
    req = urllib.request.Request(
        f"https://api.vk.com/method/{method}",
        data=urllib.parse.urlencode(data).encode(),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def probe(env: dict[str, str], method: str, **params) -> tuple[str, str]:
    """('ok', '') | ('flood', msg) | ('dead', msg) | ('net', msg)"""
    try:
        payload = vk_call(env, method, **params)
    except Exception as exc:
        return "net", str(exc)
    if "error" not in payload:
        return "ok", ""
    err = payload["error"]
    code = str(err.get("error_code"))
    msg = f"[{code}] {err.get('error_msg')}"
    return ("flood", msg) if code == "9" else ("dead", msg)


def next_attempt_no() -> int:
    try:
        n = int(open(COUNTER).read().strip()) + 1
    except (OSError, ValueError):
        n = 1
    open(COUNTER, "w").write(str(n))
    return n


def main() -> None:
    env = load_env()
    no = next_attempt_no()

    status, msg = probe(env, "users.get")
    detail = "users.get"
    if status == "ok":
        peer = env.get("VK_CHAT_PEER_ID", "")
        status, msg = probe(env, "messages.getHistory", peer_id=peer, count=1)
        detail = "messages.getHistory"

    if status == "ok":
        send_tg(env, f"✅ VK API восстановился (попытка {no}) — флуд-контроль снят, "
                     f"бот снова читает беседу. Больше попыток не будет.")
        subprocess.run(["systemctl", "disable", "--now", TIMER], check=False)
        print(f"попытка {no}: VK ожил — уведомил владельца, таймер выключен")
        return

    send_tg(env, f"⏳ Попытка {no}: VK всё ещё недоступен ({detail}: {msg}). "
                 f"Следующая проверка через 30 минут.")
    print(f"попытка {no}: {detail} {status} {msg} — ждём дальше")


if __name__ == "__main__":
    main()
