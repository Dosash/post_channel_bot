"""Tests for app.buttons: text format parsing, URL/style normalization, validation, markup."""
from __future__ import annotations

import copy

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.buttons import (
    MAX_PER_ROW,
    MAX_ROWS,
    MAX_TEXT,
    STYLES,
    ButtonsError,
    build_markup,
    buttons_to_text,
    count_buttons,
    normalize_style,
    normalize_url,
    parse_buttons,
    validate_buttons,
)


def btn(text: str, url: str, style: str | None = None) -> dict:
    return {"text": text, "url": url, "style": style}


SPEC_EXAMPLE = (
    "Текст кнопки - https://link.com\n"
    "Купить - https://shop.com - зелёная | Сайт - example.com - синяя\n"
    "🔴 Отписаться - https://t.me/x"
)
SPEC_ROWS = [
    [btn("Текст кнопки", "https://link.com")],
    [btn("Купить", "https://shop.com", "success"), btn("Сайт", "https://example.com", "primary")],
    [btn("Отписаться", "https://t.me/x", "danger")],
]


# ----------------------------------------------------------------- parse_buttons

def test_parse_single_button():
    assert parse_buttons("Текст кнопки - https://link.com") == [
        [btn("Текст кнопки", "https://link.com")]
    ]


def test_parse_spec_example():
    assert parse_buttons(SPEC_EXAMPLE) == SPEC_ROWS


@pytest.mark.parametrize(
    "word, style",
    [
        ("зелёная", "success"), ("зеленая", "success"), ("Зелёный", "success"), ("green", "success"),
        ("success", "success"), ("🟢", "success"), ("🟩", "success"),
        ("синяя", "primary"), ("СИНИЙ", "primary"), ("blue", "primary"), ("primary", "primary"),
        ("🔵", "primary"), ("🟦", "primary"),
        ("красная", "danger"), ("Красный", "danger"), ("RED", "danger"), ("danger", "danger"),
        ("🔴", "danger"), ("🟥", "danger"),
        ("обычная", None), ("default", None),
    ],
)
def test_parse_trailing_color_aliases(word, style):
    assert parse_buttons(f"Купить - https://shop.com - {word}") == [
        [btn("Купить", "https://shop.com", style)]
    ]


@pytest.mark.parametrize(
    "line, text, style",
    [
        ("🔴 Отписаться - https://t.me/x", "Отписаться", "danger"),
        ("🟢Купить - https://t.me/x", "Купить", "success"),
        ("🟢️ Купить - https://t.me/x", "Купить", "success"),
        ("🟦  Инфо - https://t.me/x", "Инфо", "primary"),
        ("🟥 Стоп - https://t.me/x", "Стоп", "danger"),
    ],
)
def test_parse_leading_emoji_sets_style_and_is_stripped(line, text, style):
    assert parse_buttons(line) == [[btn(text, "https://t.me/x", style)]]


def test_explicit_color_wins_over_emoji_and_keeps_text_literal():
    assert parse_buttons("🔴 Текст - https://x.com - синяя") == [
        [btn("🔴 Текст", "https://x.com", "primary")]
    ]
    assert parse_buttons("🔴 Live - https://x.com - обычная") == [
        [btn("🔴 Live", "https://x.com", None)]
    ]


def test_emoji_not_at_start_is_plain_text():
    assert parse_buttons("Стоп 🔴 - https://x.com") == [[btn("Стоп 🔴", "https://x.com")]]


def test_text_may_contain_dash_separator():
    assert parse_buttons("Скидка - 50% - https://x.com") == [
        [btn("Скидка - 50%", "https://x.com")]
    ]
    assert parse_buttons("Курс - Python - https://x.com - зелёная") == [
        [btn("Курс - Python", "https://x.com", "success")]
    ]


def test_text_that_is_a_color_word_or_domain():
    assert parse_buttons("Красная - https://x.com") == [[btn("Красная", "https://x.com")]]
    assert parse_buttons("Цвет - зелёная - https://x.com") == [
        [btn("Цвет - зелёная", "https://x.com")]
    ]
    assert parse_buttons("x.com - https://y.com") == [[btn("x.com", "https://y.com")]]


def test_en_and_em_dash_separators_keep_original_text():
    assert parse_buttons("Купить — https://x.com – красная") == [
        [btn("Купить", "https://x.com", "danger")]
    ]
    assert parse_buttons("Акция — 2 дня - https://x.com") == [
        [btn("Акция — 2 дня", "https://x.com")]
    ]


def test_hyphens_inside_words_and_urls_do_not_split():
    line = "Как-то так - https://my-site.example-host.com/path-to/page?a=1&b=two-three#frag-x"
    assert parse_buttons(line) == [[btn(
        "Как-то так", "https://my-site.example-host.com/path-to/page?a=1&b=two-three#frag-x"
    )]]


def test_bar_inside_url_does_not_split_row():
    rows = parse_buttons("A - https://x.com/?q=a|b | B - y.com")
    assert rows == [[btn("A", "https://x.com/?q=a|b"), btn("B", "https://y.com")]]


@pytest.mark.parametrize(
    "raw, url",
    [
        ("@example_channel", "https://t.me/example_channel"),
        ("t.me/example_channel", "https://t.me/example_channel"),
        ("telegram.me/joinchat/abc", "https://telegram.me/joinchat/abc"),
        ("www.example.com", "https://www.example.com"),
        ("example.com/path?x=1", "https://example.com/path?x=1"),
        ("tg://resolve?domain=example_channel", "tg://resolve?domain=example_channel"),
        ("HTTPS://Example.com/A", "https://Example.com/A"),
    ],
)
def test_parse_url_forms(raw, url):
    assert parse_buttons(f"Канал - {raw}") == [[btn("Канал", url)]]


def test_multiple_rows_and_buttons_with_blank_lines_and_crlf():
    text = "\r\n  A - a.com | B - b.com | C - c.com  \r\n\r\n   \nD - d.com\n"
    rows = parse_buttons(text)
    assert [[b["text"] for b in row] for row in rows] == [["A", "B", "C"], ["D"]]
    assert count_buttons(rows) == 4


def test_stray_bars_and_whitespace_are_tolerated():
    assert parse_buttons("| A - a.com |  | B - b.com |") == [
        [btn("A", "https://a.com"), btn("B", "https://b.com")]
    ]
    assert parse_buttons("Много   пробелов\tтут - a.com") == [
        [btn("Много пробелов тут", "https://a.com")]
    ]


@pytest.mark.parametrize("text", ["", "   ", "\n\n \n", None])
def test_parse_empty_input(text):
    assert parse_buttons(text) == []


@pytest.mark.parametrize(
    "text, line, fragment",
    [
        ("A - https://x.com\n\nПросто текст", 3, "не найдена ссылка"),
        ("A-https://x.com", 1, "не найдена ссылка"),
        ("https://x.com", 1, "не указан текст"),
        ("- https://x.com", 1, "пустой текст"),
        ("🔴 - https://x.com", 1, "пустой текст"),
        ("Сайт - red", 1, "не найдена ссылка"),
        ("Сайт - 50%", 1, "не похоже на адрес"),
        ("ok - a.com\nA - javascript:alert(1)", 2, "неподдерживаемая ссылка"),
        ("A - mailto:me@example.com", 1, "неподдерживаемая ссылка"),
        ("A - https://x.com - фиолетовая", 1, "неизвестный цвет"),
        ("A - https://nodot", 1, "некорректный адрес сайта"),
        ("A - @abc", 1, "username"),
    ],
)
def test_parse_errors_have_line_numbers(text, line, fragment):
    with pytest.raises(ButtonsError) as info:
        parse_buttons(text)
    message = str(info.value)
    assert message.startswith(f"Строка {line}: ")
    assert fragment in message
    assert info.value.line == line


def test_parse_error_names_button_in_multi_button_row():
    with pytest.raises(ButtonsError) as info:
        parse_buttons("A - a.com\nB - b.com | C - nolink")
    assert str(info.value).startswith("Строка 2: ")
    assert "(кнопка 2)" in str(info.value)


def test_text_length_limit():
    ok = "x" * MAX_TEXT
    assert parse_buttons(f"{ok} - a.com")[0][0]["text"] == ok
    with pytest.raises(ButtonsError, match=f"Строка 1: текст кнопки длиннее {MAX_TEXT}"):
        parse_buttons(f"{ok}y - a.com")


def test_buttons_per_row_limit():
    row = " | ".join(f"B{i} - b{i}.com" for i in range(MAX_PER_ROW))
    assert len(parse_buttons(row)[0]) == MAX_PER_ROW
    with pytest.raises(ButtonsError, match="Строка 2: слишком много кнопок в ряду"):
        parse_buttons("A - a.com\n" + row + " | X - x.com")


def test_rows_limit_reports_physical_line():
    lines = [f"B{i} - b{i}.com" for i in range(MAX_ROWS)]
    assert len(parse_buttons("\n".join(lines))) == MAX_ROWS
    text = "\n\n".join(lines + ["Лишняя - x.com"])  # blank line between every row
    with pytest.raises(ButtonsError) as info:
        parse_buttons(text)
    assert str(info.value).startswith(f"Строка {2 * MAX_ROWS + 1}: слишком много рядов")


# ----------------------------------------------------------------- normalize_url

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://link.com", "https://link.com"),
        ("  http://link.com/a  ", "http://link.com/a"),
        ("Http://link.com", "http://link.com"),
        ("https://localhost:8080/x", "https://localhost:8080/x"),
        ("https://127.0.0.1/x", "https://127.0.0.1/x"),
        ("https://пример.рф", "https://пример.рф"),
        ("tg://user?id=123", "tg://user?id=123"),
        ("@durov_bot", "https://t.me/durov_bot"),
        ("t.me/x", "https://t.me/x"),
        ("t.me/+AbCdEf", "https://t.me/+AbCdEf"),
        ("www.x.com", "https://www.x.com"),
        ("x.com/a-b_c?q=1#top", "https://x.com/a-b_c?q=1#top"),
        ("example.com:8443/path", "https://example.com:8443/path"),
        ("пример.рф/путь", "https://пример.рф/путь"),
    ],
)
def test_normalize_url_valid(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", "https://exa mple.com", "javascript:alert(1)", "JavaScript:alert(1)",
        "data:text/html;base64,AAAA", "ftp://example.com/file", "mailto:me@example.com",
        "@abc", "@bad-name", "https://", "https://nodot", "https://x.com:99999", "tg://",
        "просто текст", "nolink", "localhost:8080",
    ],
)
def test_normalize_url_invalid(raw):
    with pytest.raises(ButtonsError):
        normalize_url(raw)


# --------------------------------------------------------------- normalize_style

@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None), ("", None), ("  ", None), ("default", None), ("Обычная", None), ("⚪️", None),
        ("primary", "primary"), ("Синяя", "primary"), ("🔵", "primary"),
        ("success", "success"), ("ЗЕЛЁНАЯ", "success"), ("🟢", "success"),
        ("danger", "danger"), ("красная", "danger"), ("🔴️", "danger"),
    ],
)
def test_normalize_style(raw, expected):
    assert normalize_style(raw) == expected


@pytest.mark.parametrize("raw", ["purple", "фиолетовая", "🟣", 5])
def test_normalize_style_unknown(raw):
    with pytest.raises(ButtonsError):
        normalize_style(raw)


def test_styles_constant_matches_aliases():
    assert {normalize_style(s) for s in STYLES} == set(STYLES)


# ------------------------------------------------------------------- round trip

def test_buttons_to_text_canonical_form():
    assert buttons_to_text(SPEC_ROWS) == (
        "Текст кнопки - https://link.com\n"
        "Купить - https://shop.com - зелёная | Сайт - https://example.com - синяя\n"
        "Отписаться - https://t.me/x - красная"
    )


@pytest.mark.parametrize(
    "text",
    [
        SPEC_EXAMPLE,
        "Скидка - 50% - https://x.com - красная | 🟢 Go - t.me/y",
        "A — B – C - a.com\n@example_channel_bot - link - @example_channel",
        "x.com - https://y.com - зелёная | Цвет - синяя - z.com",
        "A - https://x.com/?q=a|b | B - y.com",
    ],
)
def test_round_trip_from_text(text):
    rows = parse_buttons(text)
    canonical = buttons_to_text(rows)
    assert parse_buttons(canonical) == rows
    assert buttons_to_text(parse_buttons(canonical)) == canonical


def test_round_trip_of_tricky_web_buttons():
    rows = validate_buttons([
        [btn("🔴 Live", "https://x.com"), btn("🟢 Go", "https://x.com", "success")],
        [btn("A -", "https://x.com"), btn("-", "https://x.com", "danger")],
        [btn("Цвет - зелёная", "https://x.com"), btn("A|B", "https://x.com")],
        [btn("Сайт - x.com", "https://y.com", "primary"), btn("— тире", "https://y.com")],
    ])
    assert parse_buttons(buttons_to_text(rows)) == rows


def test_spaced_bar_in_text_becomes_fullwidth():
    rows = validate_buttons([[btn("A | B", "https://x.com")]])
    text = buttons_to_text(rows)
    assert text == "A ｜ B - https://x.com"
    assert parse_buttons(text) == [[btn("A ｜ B", "https://x.com")]]


def test_buttons_to_text_empty_and_tolerant():
    assert buttons_to_text([]) == ""
    assert buttons_to_text([[], []]) == ""
    assert buttons_to_text([[{"text": "A", "url": "https://a.com", "style": "weird"}]]) == (
        "A - https://a.com"
    )


# ---------------------------------------------------------------- validate_buttons

def test_validate_buttons_normalizes_and_copies():
    source = [
        [{"text": "  Купить\n сейчас ", "url": " shop.com ", "style": "Зелёная", "extra": 1}],
        [],
        [{"text": "Канал", "url": "@example_channel"}, {"text": "B", "url": "b.com", "style": ""}],
        [{"text": "C", "url": "https://c.com", "style": None}, {"text": " ", "url": ""}],
    ]
    snapshot = copy.deepcopy(source)
    result = validate_buttons(source)
    assert result == [
        [btn("Купить сейчас", "https://shop.com", "success")],
        [btn("Канал", "https://t.me/example_channel"), btn("B", "https://b.com")],
        [btn("C", "https://c.com")],
    ]
    assert source == snapshot
    result[0][0]["text"] = "changed"
    assert source == snapshot


def test_validate_buttons_keeps_leading_emoji_literally():
    assert validate_buttons([[{"text": "🔴 Live", "url": "x.com"}]]) == [
        [btn("🔴 Live", "https://x.com")]
    ]


@pytest.mark.parametrize("rows", [None, [], [[]], [[], None]])
def test_validate_buttons_empty(rows):
    assert validate_buttons(rows) == []


@pytest.mark.parametrize(
    "rows, fragment",
    [
        ("Купить - x.com", "списком рядов"),
        ({"text": "A"}, "списком рядов"),
        ([{"text": "A", "url": "a.com"}], "Ряд 1: ожидается список"),
        ([["A - a.com"]], "Ряд 1, кнопка 1: кнопка должна быть объектом"),
        ([[btn("A", "a.com")], [btn("B", "b.com"), btn("", "c.com")]], "Ряд 2, кнопка 2: пустой текст"),
        ([[btn("A", "")]], "Ряд 1, кнопка 1: пустая ссылка"),
        ([[btn("A", "javascript:alert(1)")]], "Ряд 1, кнопка 1: неподдерживаемая ссылка"),
        ([[btn("A", "a.com", "purple")]], "Ряд 1, кнопка 1: неизвестный цвет"),
        ([[{"text": 5, "url": "a.com"}]], "текст кнопки должен быть строкой"),
        ([[btn("x" * (MAX_TEXT + 1), "a.com")]], f"длиннее {MAX_TEXT}"),
        ([[btn(f"B{i}", "b.com") for i in range(MAX_PER_ROW + 1)]], "Ряд 1: слишком много кнопок"),
        ([[btn(f"B{i}", "b.com")] for i in range(MAX_ROWS + 1)], "Слишком много рядов"),
    ],
)
def test_validate_buttons_errors(rows, fragment):
    with pytest.raises(ButtonsError) as info:
        validate_buttons(rows)
    assert fragment in str(info.value)


# ------------------------------------------------------------------ build_markup

@pytest.mark.parametrize("rows", [None, [], [[]], [[{"text": "", "url": ""}]]])
def test_build_markup_none_without_buttons(rows):
    assert build_markup(rows) is None


def test_build_markup_styles_only_when_set():
    markup = build_markup(SPEC_ROWS)
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.model_dump(exclude_none=True) == {
        "inline_keyboard": [
            [{"text": "Текст кнопки", "url": "https://link.com"}],
            [
                {"text": "Купить", "url": "https://shop.com", "style": "success"},
                {"text": "Сайт", "url": "https://example.com", "style": "primary"},
            ],
            [{"text": "Отписаться", "url": "https://t.me/x", "style": "danger"}],
        ]
    }


def test_build_markup_normalizes_stored_rows_and_drops_empty_rows():
    markup = build_markup([[], [{"text": "Канал", "url": "@example_channel", "style": "green"}]])
    assert markup is not None
    assert markup.model_dump(exclude_none=True) == {
        "inline_keyboard": [
            [{"text": "Канал", "url": "https://t.me/example_channel", "style": "success"}]
        ]
    }


def test_build_markup_rejects_invalid_rows():
    with pytest.raises(ButtonsError):
        build_markup([[{"text": "A", "url": "javascript:alert(1)"}]])
