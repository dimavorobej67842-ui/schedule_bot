"""Домашнее задание из ЭлЖура (eljur.ru): веб-вход + парсинг страниц дневника.

Дневник отдаётся HTML по неделям: /journal-app/u.<id>/week.<N>,
где week.0 — текущая неделя, week.-1 — следующая, week.1 — прошлая.
Задание стоит в том дне, к которому его нужно сдать.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
NO_TASK = {"без задания", "нет задания", "нет", "—"}


class EljurError(RuntimeError):
    pass


class AuthError(EljurError):
    pass


def _clean(text: str) -> str:
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in (text or "").splitlines())
    return "\n".join(line for line in lines if line)


def _text(el: Tag | None) -> str:
    return _clean(el.get_text()) if el else ""


def _resolve_date(title: str, today: date) -> date | None:
    """«Среда, 21.10» -> date; год — ближайший к сегодняшнему дню."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})", title)
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    return min(candidates, key=lambda c: abs((c - today).days)) if candidates else None


def week_offset(target: date, today: date) -> int:
    """Смещение недели для /week.<N>: 0 — текущая, -1 — следующая, 1 — прошлая."""
    target_monday = target - timedelta(days=target.weekday())
    today_monday = today - timedelta(days=today.weekday())
    return -((target_monday - today_monday).days // 7)


class EljurClient:
    def __init__(self, base_url: str, login: str, password: str):
        self.base = base_url.rstrip("/")
        self._login = login
        self._password = password
        self._student_id: str | None = None
        self._logged_in = False
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": USER_AGENT})

    # ------------------------------------------------------------ вход

    def login(self) -> None:
        self._http.cookies.clear()
        url = "/ajaxauthorize"
        for _ in range(3):
            resp = self._http.post(
                self.base + url,
                files={
                    "username": (None, self._login),
                    "password": (None, self._password),
                    "return_uri": (None, "/journal-app"),
                },
                headers={"Referer": f"{self.base}/authorize?return_uri=%2Fjournal-app"},
                timeout=30,
            )
            try:
                data = resp.json()
            except ValueError as exc:
                raise EljurError(f"Неожиданный ответ при входе (HTTP {resp.status_code})") from exc
            if data.get("errors"):
                texts = [e.get("text", str(e)) if isinstance(e, dict) else str(e)
                         for e in data["errors"]]
                raise AuthError("Журнал не пустил: " + "; ".join(texts))
            actions = data.get("actions") or []
            next_login = next((a for a in actions if a.get("type") == "login" and a.get("url")), None)
            if next_login:
                url = next_login["url"]
                continue
            redirect = next((a for a in actions if a.get("type") == "redirect" and a.get("url")), None)
            if redirect:
                self._http.get(self.base + redirect["url"], timeout=30)
            self._logged_in = True
            log.info("ЭлЖур: вход выполнен")
            return
        raise AuthError("Слишком много шагов входа в ЭлЖур")

    @staticmethod
    def _is_login_page(resp: requests.Response) -> bool:
        return resp.url.path.startswith("/authorize") or "standalone.login" in resp.text

    def _get(self, path: str) -> str:
        if not self._logged_in:
            self.login()
        resp = self._http.get(self.base + path, timeout=30)
        if self._is_login_page(resp):  # сессия протухла
            self.login()
            resp = self._http.get(self.base + path, timeout=30)
            if self._is_login_page(resp):
                raise AuthError("Не удалось войти в журнал")
        resp.raise_for_status()
        return resp.text

    def _ensure_student(self) -> str:
        if not self._student_id:
            html = self._get("/journal-app")
            m = re.search(r"/journal-app/u\.(\d+)", html)
            if not m:
                raise EljurError("Не удалось определить id ученика на странице журнала")
            self._student_id = m.group(1)
        return self._student_id

    # ------------------------------------------------------------ данные

    def get_week(self, offset: int, today: date) -> list[tuple[date, list[tuple[str, str]]]]:
        """[(дата, [(предмет, домашка)])] по дням недели /week.<offset>."""
        student = self._ensure_student()
        html = self._get(f"/journal-app/u.{student}/week.{offset}")
        return _parse_week(html, today)


def _parse_task_text(el: Tag) -> str:
    body = BeautifulSoup(str(el), "html.parser").find()
    assert body is not None
    for junk in body.select('.dnevnik-lesson__attach, a[href*="task_id="], i'):
        junk.decompose()
    for a in body.select("a[href]"):
        a.replace_with(a["href"])
    return _text(body)


def _parse_week(html: str, today: date) -> list[tuple[date, list[tuple[str, str]]]]:
    soup = BeautifulSoup(html, "html.parser")
    days = []
    for day_el in soup.select(".dnevnik-day"):
        d = _resolve_date(_text(day_el.select_one(".dnevnik-day__title")), today)
        if not d:
            continue
        lessons: list[tuple[str, str]] = []
        for lesson_el in day_el.select(".dnevnik-lesson"):
            subject = _text(lesson_el.select_one(".js-rt_licey-dnevnik-subject"))
            tasks = []
            for task_el in lesson_el.select(".dnevnik-lesson__task"):
                text = _parse_task_text(task_el)
                if not text or text.lower().strip(" .") in NO_TASK:
                    continue
                files = [a.get("title") or _text(a)
                         for a in task_el.select(".dnevnik-lesson__attach a[href]")]
                if files:
                    text += " 📎 " + ", ".join(f for f in files if f)
                tasks.append(text)
            if subject and tasks:
                lessons.append((subject, "\n".join(tasks)))
        days.append((d, lessons))
    return days


# ---------------------------------------------------------- сопоставление

_MDK_RE = re.compile(r"мдк[\s._-]*(\d{1,2})[\s._-]*(\d{1,2})", re.IGNORECASE)


def _normalize(subject: str) -> str:
    return re.sub(r"[^a-zа-яё0-9 ]+", " ", subject.lower()).strip()


def _mdk_code(subject: str) -> str | None:
    m = _MDK_RE.search(subject)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def match_homework(schedule_subject: str, hw_by_subject: dict[str, str]) -> str:
    """Подбирает ДЗ из журнала к дисциплине расписания (названия часто не совпадают дословно)."""
    target = _normalize(schedule_subject)
    if not target:
        return ""
    target_code = _mdk_code(schedule_subject)
    target_tokens = set(target.split())

    best_score, best_text = 0.0, ""
    for subject, text in hw_by_subject.items():
        other = _normalize(subject)
        if not other:
            continue
        other_tokens = set(other.split())
        score = 0.0
        if target == other:
            score = 3.0
        elif target_code and target_code == _mdk_code(subject):
            score = 2.5
        elif target in other or other in target:
            score = 2.0
        elif target_tokens and target_tokens <= other_tokens:
            score = 1.5
        elif target_tokens and other_tokens <= target_tokens:
            score = 1.5
        else:
            common = len(target_tokens & other_tokens)
            if common:
                score = common / max(len(target_tokens), len(other_tokens))
        if score > best_score:
            best_score, best_text = score, text
    return best_text if best_score >= 1.5 else ""


# ------------------------------------------------------------- интерфейс

_clients: dict[tuple[str, str], EljurClient] = {}


def _get_client(base_url: str, login: str, password: str) -> EljurClient:
    key = (base_url.rstrip("/"), login)
    client = _clients.get(key)
    if client is None:
        client = EljurClient(base_url, login, password)
        _clients[key] = client
    return client


def fetch_homework(base_url: str, login: str, password: str, target: date) -> dict[str, str]:
    """Предмет -> домашнее задание на дату target (пусто, если ДЗ нет)."""
    if not base_url or not login:
        return {}
    today = date.today()
    client = _get_client(base_url, login, password)
    for day, lessons in client.get_week(week_offset(target, today), today):
        if day == target:
            return dict(lessons)
    return {}
