#!/usr/bin/env python3
"""Сторож: проверяет бота, xray, диск, память и VK-токен; шлёт алерты владельцу.

Работает НЕЗАВИСИМО от бота: отправка через curl и SOCKS5-прокси xray,
поэтому уведомления доходят даже когда сам schedule_bot мёртв.

Запуск:
  watchdog.py run   — один проход проверок (вызывает systemd-таймер каждые 5 минут)
  watchdog.py boot  — сообщение о перезагрузке VM
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ENV_FILE = Path("/opt/schedule_bot/.env")
STATE_FILE = Path("/root/.schedule_bot_watchdog.json")
DOWNLOADS = Path("/opt/schedule_bot/downloads")

ALERT_EVERY = 6 * 3600  # не спамить: один и тот же алерт — раз в 6 часов
DISK_MIN_FREE_PCT = 10
MEM_MIN_AVAIL_MB = 80
VK_AUTH_CODES = {"5", "1117", "1114"}


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_tg(env: dict[str, str], text: str) -> bool:
    """Отправка через curl + SOCKS5: единственный канал, живущий без бота."""
    proxy = env.get("TG_PROXY") or "socks5h://127.0.0.1:10808"
    if proxy.startswith("socks5://"):
        proxy = "socks5h://" + proxy[len("socks5://"):]
    token, chat_id = env.get("BOT_TOKEN", ""), env.get("CHAT_ID", "")
    if not token or not chat_id:
        print("нет BOT_TOKEN/CHAT_ID в .env — отправить некуда")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in (1, 2):
        r = subprocess.run(
            ["curl", "-sS", "--max-time", "25", "-x", proxy, "-X", "POST", url,
             "-d", f"chat_id={chat_id}",
             "--data-urlencode", f"text={text}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and '"ok":true' in r.stdout.replace(" ", ""):
            return True
        print(f"TG попытка {attempt}: rc={r.returncode} {r.stdout[:200]} {r.stderr[:200]}")
        time.sleep(5)
    # Telegram недоступен (упал xray/прокси) — дублируем во VK «диалог с собой»
    return send_vk_self(env, "[ALERT TG недоступен] " + text)


def send_vk_self(env: dict[str, str], text: str) -> bool:
    """Запасной канал: VK доступен с VM напрямую, без прокси и без бота."""
    token = env.get("VK_TOKEN", "")
    if not token:
        return False
    try:
        data = urllib.parse.urlencode({"access_token": token, "v": "5.199"}).encode()
        with urllib.request.urlopen(
            urllib.request.Request("https://api.vk.com/method/users.get", data=data), timeout=20
        ) as resp:
            me = json.loads(resp.read().decode())["response"][0]["id"]
        data = urllib.parse.urlencode(
            {"access_token": token, "v": "5.199", "peer_id": me,
             "message": text, "random_id": int(time.time())}
        ).encode()
        with urllib.request.urlopen(
            urllib.request.Request("https://api.vk.com/method/messages.send", data=data), timeout=20
        ) as resp:
            ok = "response" in json.loads(resp.read().decode())
        if ok:
            print("ALERT продублирован в VK")
        return ok
    except Exception as exc:
        print(f"VK-фолбэк не удался: {exc}")
        return False


def throttle(key: str) -> bool:
    """True — пора алертить; False — недавно уже алертили."""
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
    now = time.time()
    last = state.get(key, 0)
    if now - last < ALERT_EVERY:
        return False
    state[key] = now
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return True


def service_active(name: str) -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", name])
    return r.returncode == 0


def check_service(env: dict[str, str], name: str, title: str) -> None:
    if service_active(name):
        return
    restarted = subprocess.run(["systemctl", "restart", name]).returncode == 0
    verdict = "перезапустил" if restarted else "перезапуск НЕ удался!"
    if throttle(f"service:{name}"):
        send_tg(env, f"🚨 {title} ({name}) упал — {verdict}.")


def check_disk(env: dict[str, str]) -> None:
    import shutil
    usage = shutil.disk_usage("/")
    free_pct = usage.free / usage.total * 100
    if free_pct < DISK_MIN_FREE_PCT and throttle("disk"):
        send_tg(env, f"🚨 Диск VM почти полный: свободно {free_pct:.1f}%.")


def check_memory(env: dict[str, str]) -> None:
    avail_mb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            avail_mb = int(line.split()[1]) // 1024
            break
    if avail_mb < MEM_MIN_AVAIL_MB and throttle("mem"):
        send_tg(env, f"🚨 На VM почти нет памяти: доступно {avail_mb} МБ.")


def check_vk_token(env: dict[str, str]) -> None:
    """Раннее обнаружение мёртвого токена — чтобы не узнали по пропавшей рассылке."""
    token = env.get("VK_TOKEN", "")
    if not token:
        return
    data = urllib.parse.urlencode({"access_token": token, "v": "5.199"}).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request("https://api.vk.com/method/users.get", data=data), timeout=20
        ) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        if throttle("vk_net"):
            send_tg(env, f"⚠️ VK API недоступен с VM: {exc}. Бот пропустит проверки, пока не восстановится.")
        return
    if "error" not in payload:
        return
    code = str(payload["error"].get("error_code"))
    msg = payload["error"].get("error_msg", "")
    if code in VK_AUTH_CODES:
        if throttle("vk_auth"):
            send_tg(
                env,
                "🚨 VK-токен мёртв (API: "
                f"[{code}] {msg}).\n"
                "Расписание перестанет приходить. Нужен новый токен:\n"
                "1) Зайди в VK с телефона/компа и выполни команду /start у бота — он напомнит.\n"
                "2) Новый токен: README → раздел «VK-токен», затем обнови VK_TOKEN в .env\n"
                "и выполни: systemctl restart schedule_bot",
            )
    elif throttle("vk_err"):
        send_tg(env, f"⚠️ VK API вернул ошибку [{code}] {msg} — проверь, всё ли в порядке.")


def cleanup_downloads() -> None:
    """Кэш файлов расписания: старые копии не нужны, файлы перекачиваются из VK."""
    cutoff = time.time() - 30 * 24 * 3600
    for f in DOWNLOADS.glob("*.docx"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def backup_state() -> None:
    backups = Path("/root/backups")
    backups.mkdir(exist_ok=True)
    src = Path("/opt/schedule_bot/state.json")
    if src.exists():
        dst = backups / f"state_{time.strftime('%Y%m%d')}.json"
        dst.write_bytes(src.read_bytes())
    for old in sorted(backups.glob("state_*.json"))[:-14:]:
        old.unlink(missing_ok=True)


def run_checks() -> None:
    env = load_env()
    check_service(env, "schedule_bot", "Бот расписания")
    check_service(env, "xray", "Xray-прокси (без него Telegram мёртв)")
    check_disk(env)
    check_memory(env)
    check_vk_token(env)
    cleanup_downloads()
    backup_state()
    print("watchdog: проверки пройдены", time.strftime("%Y-%m-%d %H:%M:%S"))


def boot() -> None:
    env = load_env()
    time.sleep(30)  # дать сети и прокси подняться
    send_tg(env, "🔄 VM перезагрузилась. Бот и проверки запускаются автоматически. "
                 "Если через 5 минут это сообщение — единственный признак жизни, пиши сюда.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "boot":
        boot()
    else:
        run_checks()
