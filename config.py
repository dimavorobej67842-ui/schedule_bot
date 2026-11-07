from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram
    bot_token: str
    chat_id: int  # куда присылать расписание (telegram id пользователя)
    tg_proxy: str = ""  # прокси для Telegram: socks5://127.0.0.1:1080 или http://user:pass@host:port

    # Расписание
    group: str = "2025-ТНГ-96Д"

    # VK (юзер-токен: читает беседу, откуда кидают файл)
    vk_token: str = ""
    vk_chat_peer_id: int = 0  # peer_id беседы, например 2000000001
    vk_check_interval: int = 300  # секунд между проверками беседы

    # ЭлЖур
    eljur_url: str = ""  # например https://school.eljur.ru
    eljur_login: str = ""
    eljur_password: str = ""

    # Файлы
    bells_file: Path = Path("bells.json")
    state_file: Path = Path("state.json")
    download_dir: Path = Path("downloads")


settings = Settings()
