"""The server accepts and maps the same seven interface languages as the frontend (M3.4)."""

from __future__ import annotations

import pytest

from miniapp.store import SUPPORTED_LANGS, lang_from_telegram


def test_seven_languages() -> None:
    assert set(SUPPORTED_LANGS) == {"az", "ru", "en", "tr", "uz", "pt-BR", "es"}


@pytest.mark.parametrize(("code", "lang"), [
    ("tr", "tr"), ("uz", "uz"), ("uz-Latn", "uz"), ("pt-br", "pt-BR"), ("pt", "pt-BR"), ("pt-PT", "pt-BR"),
    ("es-419", "es"), ("ru-RU", "ru"), ("de", "ru"), (None, "ru"), ("", "ru"),
])
def test_telegram_codes_map_like_the_frontend(code, lang) -> None:
    assert lang_from_telegram(code) == lang
