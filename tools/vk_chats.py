"""Список бесед VK: помогает узнать vk_chat_peer_id для .env.

Запуск: python tools/vk_chats.py   (нужен VK_TOKEN в .env)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from services.vk_source import VkSource  # noqa: E402


def main() -> None:
    if not settings.vk_token:
        sys.exit("Заполни VK_TOKEN в .env")
    vk = VkSource(settings.vk_token, 0)
    response = vk._call("messages.getConversations", count=50, filter="all")
    items = response.get("items", [])
    if not items:
        print("Бесед не найдено (у токена может не быть доступа к messages)")
        return
    for item in items:
        conv = item.get("conversation", {})
        peer = conv.get("peer", {})
        title = (conv.get("chat_settings") or {}).get("title") or peer.get("type")
        print(f"{peer.get('id')}\t{peer.get('type')}\t{title}")


if __name__ == "__main__":
    main()
