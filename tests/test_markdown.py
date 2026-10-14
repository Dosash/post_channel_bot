"""Tests for app.markdown: markdown → Telegram HTML, HTML → markdown, helpers."""
from __future__ import annotations

import random
import re
import time
from html.parser import HTMLParser

import pytest
from aiogram.types import MessageEntity, User
from aiogram.utils.text_decorations import html_decoration

from app.markdown import (
    CAPTION_LIMIT,
    SYNTAX_HELP_HTML,
    TEXT_LIMIT,
    excerpt,
    html_to_md,
    md_to_html,
    strip_html,
    validate_telegram_html,
    visible_length,
)

# Every (markdown, expected html) pair below is also fed to validate_telegram_html.
SYNTAX_TABLE = [
    ("**bold**", "<b>bold</b>"),
    ("*italic*", "<i>italic</i>"),
    ("_italic_", "<i>italic</i>"),
    ("__underline__", "<u>underline</u>"),
    ("~~strike~~", "<s>strike</s>"),
    ("||spoiler||", "<tg-spoiler>spoiler</tg-spoiler>"),
    ("`code`", "<code>code</code>"),
    ("```python\nprint(1)\n```", '<pre><code class="language-python">print(1)</code></pre>'),
    ("```\nx = 1\ny = 2\n```", "<pre>x = 1\ny = 2</pre>"),
    ("[text](https://example.com)", '<a href="https://example.com">text</a>'),
    ("![😀](tg://emoji?id=123)", '<tg-emoji emoji-id="123">😀</tg-emoji>'),
    ("> quote", "<blockquote>quote</blockquote>"),
    (">> hidden", "<blockquote expandable>hidden</blockquote>"),
    ("# Heading", "<b>Heading</b>"),
    ("###### Small heading", "<b>Small heading</b>"),
    ("- item", "• item"),
    ("* item", "• item"),
    ("+ item", "• item"),
    ("1. item", "1. item"),
    ("\\*not italic\\*", "*not italic*"),
]

NESTING = [
    ("**a *b* c**", "<b>a <i>b</i> c</b>"),
    ("[**bold link**](https://x.com)", '<a href="https://x.com"><b>bold link</b></a>'),
    ("**[link](https://x.com)**", '<b><a href="https://x.com">link</a></b>'),
    ("||**x**||", "<tg-spoiler><b>x</b></tg-spoiler>"),
    ("> **quote** and _it_", "<blockquote><b>quote</b> and <i>it</i></blockquote>"),
    ("~~__x__~~", "<s><u>x</u></s>"),
    ("**bold _and italic_**", "<b>bold <i>and italic</i></b>"),
    ("*a**b**c*", "<i>a<b>b</b>c</i>"),
    ("**a*b*c**", "<b>a<i>b</i>c</b>"),
    ("x||y||z", "x<tg-spoiler>y</tg-spoiler>z"),
]

UNMATCHED = [
    "**a",
    "a**",
    "****",
    "__",
    "||",
    "~~",
    "~single~",
    "** not bold **",
    "a * b * c",
    "a || b",
    "|||x|||",
    "[not a link]",
    "[text](no closing paren",
    "a ` b",
]

LITERAL_UNDERSCORES = [
    "snake_case_name",
    "https://ex.com/a_b_c?x=1_2",
    "file_name.txt",
    "@my_channel_name",
    "переменная_с_подчёркиванием",
    "see https://ex.com/_x_/y_z.",
]


def all_corpus_html() -> list[str]:
    sources = [md for md, _ in SYNTAX_TABLE + NESTING] + UNMATCHED + LITERAL_UNDERSCORES
    return [md_to_html(md) for md in sources]


# ------------------------------------------------------------------ syntax table


@pytest.mark.parametrize(("md", "expected"), SYNTAX_TABLE)
def test_syntax_table(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


@pytest.mark.parametrize(("md", "expected"), NESTING)
def test_nesting(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


def test_triple_star_is_bold_italic() -> None:
    assert md_to_html("***x***") in ("<b><i>x</i></b>", "<i><b>x</b></i>")


def test_triple_underscore_is_italic_underline() -> None:
    assert md_to_html("___x___") in ("<i><u>x</u></i>", "<u><i>x</i></u>")


def test_heading_does_not_double_bold() -> None:
    assert md_to_html("# **Title** and *it*") == "<b>Title and <i>it</i></b>"


def test_partial_runs_keep_leftover_literal() -> None:
    assert md_to_html("***a**") == "*<b>a</b>"
    assert md_to_html("**a***") == "<b>a</b>*"
    assert md_to_html("**a**b**") == "<b>a</b>b**"


def test_bold_right_after_punctuation_closes() -> None:
    assert md_to_html("**Цена:**100₽") == "<b>Цена:</b>100₽"


def test_intraword_star_emphasis() -> None:
    assert md_to_html("un*frigging*believable") == "un<i>frigging</i>believable"


def test_underscore_italic_with_inner_underscore() -> None:
    assert md_to_html("_foo_bar_") == "<i>foo_bar</i>"
    assert md_to_html("(_x_)") == "(<i>x</i>)"


# ------------------------------------------------------------------ unmatched / literals


@pytest.mark.parametrize("md", UNMATCHED)
def test_unmatched_delimiters_stay_literal(md: str) -> None:
    html = md_to_html(md)
    assert "<" not in html.replace("&lt;", "")
    assert strip_html(html) == md


@pytest.mark.parametrize("md", LITERAL_UNDERSCORES)
def test_intraword_underscores_and_urls_stay_intact(md: str) -> None:
    assert md_to_html(md) == md


def test_url_protected_inside_bold() -> None:
    assert md_to_html("**https://ex.com/a_b_c**") == "<b>https://ex.com/a_b_c</b>"


def test_autolink_trailing_punctuation_is_not_part_of_url() -> None:
    html = md_to_html("(see https://ex.com/a_(b)), _italic_.")
    assert html == "(see https://ex.com/a_(b)), <i>italic</i>."


# ------------------------------------------------------------------ escapes


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("\\_x\\_", "_x_"),
        ("\\\\", "\\"),
        ("\\a stays", "\\a stays"),
        ("\\`not code\\`", "`not code`"),
        ("\\[x](https://ex.com)", "[x](https://ex.com)"),
        ("\\# not heading", "# not heading"),
        ("\\- not bullet", "- not bullet"),
        ("**C:\\\\**", "<b>C:\\</b>"),
    ],
)
def test_backslash_escapes(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


# ------------------------------------------------------------------ HTML escaping


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ("&amp;", "&amp;amp;"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("<b>not bold</b>", "&lt;b&gt;not bold&lt;/b&gt;"),
        ('**"quoted" & <tag>**', '<b>"quoted" &amp; &lt;tag&gt;</b>'),
    ],
)
def test_html_is_escaped(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


def test_href_quotes_and_ampersands_escaped() -> None:
    html = md_to_html('[a](https://e.com/?a=1&b="2")')
    assert html == '<a href="https://e.com/?a=1&amp;b=&quot;2&quot;">a</a>'
    validate_telegram_html(html)


# ------------------------------------------------------------------ code


def test_code_span_content_is_literal() -> None:
    assert md_to_html("`**not bold** <b> _x_`") == "<code>**not bold** &lt;b&gt; _x_</code>"


def test_code_span_with_backticks_inside() -> None:
    assert md_to_html("`` a`b ``") == "<code>a`b</code>"


def test_code_block_content_is_literal() -> None:
    md = "```\n**x** <i>\n> not quote\n# not heading\n```"
    assert md_to_html(md) == "<pre>**x** &lt;i&gt;\n&gt; not quote\n# not heading</pre>"


def test_unterminated_fence_runs_to_end() -> None:
    assert md_to_html("```py\nline1\n**x**") == (
        '<pre><code class="language-py">line1\n**x**</code></pre>'
    )


def test_text_around_code_block() -> None:
    md = "before **b**\n```js\nlet a = 1 < 2;\n```\nafter"
    assert md_to_html(md) == (
        'before <b>b</b>\n<pre><code class="language-js">let a = 1 &lt; 2;</code></pre>\nafter'
    )


def test_empty_code_block_is_literal() -> None:
    assert md_to_html("```\n```") == "```\n```"


def test_bad_language_is_dropped() -> None:
    assert md_to_html('```"><script>\nx\n```') == "<pre>x</pre>"


def test_one_line_triple_backticks_is_inline_code() -> None:
    assert md_to_html("```inline```") == "<code>inline</code>"


# ------------------------------------------------------------------ links


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("[x](javascript:alert(1))", "x"),
        ("[**x**](ftp://files)", "<b>x</b>"),
        ("[x](t.me/channel)", '<a href="https://t.me/channel">x</a>'),
        ("[x](tg://user?id=1)", '<a href="tg://user?id=1">x</a>'),
        ("[x](mailto:a@b.c)", '<a href="mailto:a@b.c">x</a>'),
        ("[x](https://x.com/a_(b))", '<a href="https://x.com/a_(b)">x</a>'),
        ("[x](https://x.com/_a_)", '<a href="https://x.com/_a_">x</a>'),
        ("[x]( https://x.com )", '<a href="https://x.com">x</a>'),
        ("[](https://x.com)", "[](https://x.com)"),
        ("[x](https://x.com/a b)", "[x](https://x.com/a b)"),
        ("![x](https://img.png)", '!<a href="https://img.png">x</a>'),
        ("[a [b] c](https://x.com)", '<a href="https://x.com">a [b] c</a>'),
    ],
)
def test_links(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


def test_link_inside_link_text_is_not_nested() -> None:
    html = md_to_html("[a [b](https://b.com)](https://a.com)")
    validate_telegram_html(html)
    assert html.count("<a ") == 1


# ------------------------------------------------------------------ blocks


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("> a\n> b", "<blockquote>a\nb</blockquote>"),
        ("> a\n>\n> b", "<blockquote>a\n\nb</blockquote>"),
        (">> a\n>> b", "<blockquote expandable>a\nb</blockquote>"),
        ("> a\n>> b", "<blockquote>a</blockquote>\n<blockquote expandable>b</blockquote>"),
        ("> a\ntext", "<blockquote>a</blockquote>\ntext"),
        ("> - item\n> # Title", "<blockquote>• item\n<b>Title</b></blockquote>"),
        ("> ```\n> code *x*\n> ```", "<blockquote><pre>code *x*</pre></blockquote>"),
        (">no space", "&gt;no space"),
        (">>> x", "&gt;&gt;&gt; x"),
        ("> > nested", "<blockquote>&gt; nested</blockquote>"),
        (">", "&gt;"),
    ],
)
def test_blockquotes(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("## Sub **x**", "<b>Sub x</b>"),
        ("# Title #", "<b>Title</b>"),
        ("#hashtag", "#hashtag"),
        ("####### seven", "####### seven"),
        ("#", "#"),
        ("# C#", "<b>C#</b>"),
    ],
)
def test_headings(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


@pytest.mark.parametrize(
    ("md", "expected"),
    [
        ("*italic* text", "<i>italic</i> text"),
        ("**bold** start", "<b>bold</b> start"),
        ("* item *it*", "• item <i>it</i>"),
        ("- **bold** item", "• <b>bold</b> item"),
        ("  - nested", "  • nested"),
        ("-not bullet", "-not bullet"),
        ("+7 999 000", "+7 999 000"),
        ("- a\n- b", "• a\n• b"),
    ],
)
def test_bullets_vs_emphasis_at_line_start(md: str, expected: str) -> None:
    assert md_to_html(md) == expected


def test_newlines_preserved_and_trimmed() -> None:
    assert md_to_html("\n\n a\n\n\nb  \n\n") == " a\n\n\nb"
    assert md_to_html("a\r\nb") == "a\nb"
    assert "<br" not in md_to_html("a\nb\n\nc")


def test_emphasis_spans_lines_within_paragraph_only() -> None:
    assert md_to_html("**a\nb**") == "<b>a\nb</b>"
    assert md_to_html("**a\n\nb**") == "**a\n\nb**"


def test_empty_input() -> None:
    assert md_to_html("") == ""
    assert md_to_html("   \n  ") == ""


# ------------------------------------------------------------------ visible_length / strip


@pytest.mark.parametrize(
    ("html", "length"),
    [
        ("👍", 2),
        ("<b>ab</b>", 2),
        ("&lt;&amp;&gt;", 3),
        ('<tg-emoji emoji-id="1">👍</tg-emoji> ok', 5),
        ("привет", 6),
        ("", 0),
    ],
)
def test_visible_length(html: str, length: int) -> None:
    assert visible_length(html) == length


def test_visible_length_of_rendered_markdown() -> None:
    assert visible_length(md_to_html("**👍** [link](https://x.com)")) == 7


def test_strip_html() -> None:
    assert strip_html("<b>a</b> &amp; <i>b</i> &quot;c&quot;") == 'a & b "c"'


def test_limits() -> None:
    assert (TEXT_LIMIT, CAPTION_LIMIT) == (4096, 1024)


# ------------------------------------------------------------------ validate


@pytest.mark.parametrize("html", all_corpus_html())
def test_validate_accepts_corpus(html: str) -> None:
    validate_telegram_html(html)


@pytest.mark.parametrize(
    "html",
    [
        '<span class="tg-spoiler">x</span>',
        "<blockquote expandable>x</blockquote>",
        '<pre><code class="language-py">x</code></pre>',
        "<strong>a</strong><em>b</em><ins>c</ins><strike>d</strike><del>e</del>",
        "a &lt; b &#128077; &#x1F44D;",
        SYNTAX_HELP_HTML,
    ],
)
def test_validate_accepts_telegram_subset(html: str) -> None:
    validate_telegram_html(html)


@pytest.mark.parametrize(
    "html",
    [
        "<b><i></b></i>",
        "<script>alert(1)</script>",
        "<b>unclosed",
        "stray</b>",
        "<b",
        "a < b",
        "a > b",
        "a & b",
        "&nbsp;",
        "&amp",
        "<br/>",
        "<a>no href</a>",
        '<a href="x" onclick="y">x</a>',
        "<span>x</span>",
        '<span class="other">x</span>',
        "<!-- comment -->",
        "<blockquote><blockquote>x</blockquote></blockquote>",
        '<a href="x"><a href="y">x</a></a>',
        '<tg-emoji emoji-id="abc">x</tg-emoji>',
        '<code class="evil">x</code>',
        '<b class="x">x</b>',
        "<div>x</div>",
    ],
)
def test_validate_rejects(html: str) -> None:
    with pytest.raises(ValueError):
        validate_telegram_html(html)


# ------------------------------------------------------------------ html_to_md


def E(kind: str, offset: int, length: int, **extra) -> MessageEntity:  # noqa: N802
    return MessageEntity(type=kind, offset=offset, length=length, **extra)


AIOGRAM_CASES = [
    # (text, entities, expected markdown, expected md_to_html of the markdown)
    (
        "bold italic under strike spoil",
        [E("bold", 0, 4), E("italic", 5, 6), E("underline", 12, 5),
         E("strikethrough", 18, 6), E("spoiler", 25, 5)],
        "**bold** *italic* __under__ ~~strike~~ ||spoil||",
        "<b>bold</b> <i>italic</i> <u>under</u> <s>strike</s> <tg-spoiler>spoil</tg-spoiler>",
    ),
    ("code here", [E("code", 0, 4)], "`code` here", "<code>code</code> here"),
    (
        "print(1)",
        [E("pre", 0, 8, language="python")],
        "```python\nprint(1)\n```",
        '<pre><code class="language-python">print(1)</code></pre>',
    ),
    ("plain pre", [E("pre", 0, 9)], "```\nplain pre\n```", "<pre>plain pre</pre>"),
    (
        "Site link",
        [E("text_link", 5, 4, url="https://example.com/a_b")],
        "Site [link](https://example.com/a_b)",
        'Site <a href="https://example.com/a_b">link</a>',
    ),
    (
        "q1\nq2\nafter",
        [E("blockquote", 0, 5)],
        "> q1\n> q2\nafter",
        "<blockquote>q1\nq2</blockquote>\nafter",
    ),
    (
        "q1\nq2\nafter",
        [E("expandable_blockquote", 0, 6)],
        ">> q1\n>> q2\nafter",
        "<blockquote expandable>q1\nq2</blockquote>\nafter",
    ),
    (
        "👍 ok",
        [E("custom_emoji", 0, 2, custom_emoji_id="5368324170671202286")],
        "![👍](tg://emoji?id=5368324170671202286) ok",
        '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji> ok',
    ),
    (
        "Ivan hi",
        [E("text_mention", 0, 4, user=User(id=42, is_bot=False, first_name="Ivan"))],
        "[Ivan](tg://user?id=42) hi",
        '<a href="tg://user?id=42">Ivan</a> hi',
    ),
    (
        "bold both",
        [E("bold", 0, 9), E("italic", 5, 4)],
        "**bold *both***",
        "<b>bold <i>both</i></b>",
    ),
    (
        "both bold",
        [E("bold", 0, 9), E("italic", 0, 4)],
        "***both* bold**",
        "<b><i>both</i> bold</b>",
    ),
    ("a < b & c", [], "a < b & c", "a &lt; b &amp; c"),
    ("today", [E("date_time", 0, 5, unix_time=1700000000)], "today", "today"),
]


@pytest.mark.parametrize(("text", "entities", "md", "html"), AIOGRAM_CASES)
def test_html_to_md_aiogram_round_trip(text, entities, md, html) -> None:
    source = html_decoration.unparse(text, entities)
    assert html_to_md(source) == md
    assert md_to_html(html_to_md(source)) == html


@pytest.mark.parametrize(
    "html",
    [
        "<b>bold</b> and <i>italic</i>",
        '<a href="https://example.com/path?q=1&amp;r=2">link</a>',
        "<blockquote>line one\nline <b>two</b></blockquote>\ntext",
        "<blockquote expandable>long\ntext</blockquote>",
        '<pre><code class="language-python">x = "a_b" * 2</code></pre>',
        "<tg-spoiler>secret</tg-spoiler> <s>old</s> <u>new</u>",
        '<b>bold <a href="https://x.com">link</a></b>',
        "<code>a &lt; b</code>",
    ],
)
def test_round_trip_is_identity(html: str) -> None:
    assert md_to_html(html_to_md(html)) == html


@pytest.mark.parametrize(
    ("html", "md"),
    [
        ('<span class="tg-spoiler">x</span>', "||x||"),
        ("<strong>a</strong> <em>b</em> <ins>c</ins> <del>d</del> <strike>e</strike>",
         "**a** *b* __c__ ~~d~~ ~~e~~"),
        ("<b>bold </b>text", "**bold** text"),
        ("<b>a\n\nb</b>", "**a**\n\n**b**"),
        ("<code>a`b</code>", "``a`b``"),
        ("<code>`x`</code>", "`` `x` ``"),
        ("<x-unknown>hi</x-unknown> <span>there</span>", "hi there"),
        ("a &lt;b&gt; &amp; &quot;c&quot;", 'a <b> & "c"'),
        ("<blockquote>q</blockquote>after", "> q\nafter"),
        ("text<pre>code</pre>tail", "text\n```\ncode\n```\ntail"),
        ("<pre>```\ninner\n```</pre>", "````\n```\ninner\n```\n````"),
        ('<a href="https://x.com/a)b">u</a>', "[u](https://x.com/a\\)b)"),
        ('<a href="https://x.com">[t]</a>', "[\\[t\\]](https://x.com)"),
        ("<b></b>empty", "empty"),
        ("line<br>break", "line\nbreak"),
    ],
)
def test_html_to_md_conversions(html: str, md: str) -> None:
    assert html_to_md(html) == md


def test_html_to_md_tricky_links_render_back() -> None:
    html = '<a href="https://x.com/a)b">u</a> <a href="https://x.com">[t]</a>'
    assert md_to_html(html_to_md(html)) == html


def test_html_to_md_pre_with_backticks_round_trip() -> None:
    html = "<pre>```\ninner\n```</pre>"
    assert md_to_html(html_to_md(html)) == html


def test_html_to_md_pre_inside_blockquote_round_trip() -> None:
    html = "<blockquote>q\n<pre>code *x*</pre></blockquote>"
    assert html_to_md(html) == "> q\n> ```\n> code *x*\n> ```"
    assert md_to_html(html_to_md(html)) == html


def test_adjacent_different_delimiters_regression() -> None:
    # Found by fuzzing: "**" right after "__" must open, not close the outer "*".
    md = "***||alpha||** __**alpha**__* x"
    expected = "<i><b><tg-spoiler>alpha</tg-spoiler></b> <u><b>alpha</b></u></i> x"
    assert md_to_html(md) == expected


def test_html_to_md_empty() -> None:
    assert html_to_md("") == ""


# ------------------------------------------------------------------ excerpt


def test_excerpt_strips_markup() -> None:
    assert excerpt("**Hello** _world_") == "Hello world"


def test_excerpt_single_line() -> None:
    assert excerpt("# Title\n\n- item\n> quote") == "Title • item quote"


def test_excerpt_cuts_with_ellipsis() -> None:
    result = excerpt("word " * 50, 20)
    assert result.endswith("…")
    assert len(result) <= 20


def test_excerpt_html_mode() -> None:
    assert excerpt("<b>a</b>\n&lt;b&gt;", is_html=True) == "a <b>"


def test_excerpt_empty() -> None:
    assert excerpt("") == ""


# ------------------------------------------------------------------ property-style


FRAGMENTS = [
    "**", "*", "_", "__", "~~", "~", "||", "|", "`", "``", "```", "```py", "[", "]", "(",
    ")", "![", "](", "https://ex.com/a_b", "t.me/x", "tg://emoji?id=1", "javascript:x",
    "\\", "\\*", "<", ">", "&", '"', "&amp;", "> ", ">> ", ">", "# ", "## ", "- ", "* ",
    "+ ", "1. ", "\n", "\n", "\n\n", " ", " ", "word", "слово", "👍", "snake_case", "#",
    "<b>", "</i>", "x",
]


def random_markdown(rng: random.Random) -> str:
    return "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(1, 40)))


@pytest.mark.parametrize("seed", range(200))
def test_random_markdown_always_valid(seed: int) -> None:
    rng = random.Random(seed)
    md = random_markdown(rng)
    html = md_to_html(md)
    validate_telegram_html(html)
    assert not re.search(r"<(b|i|u|s|tg-spoiler|code|pre|blockquote)( [^>]*)?></\1>", html)
    assert "<br" not in html
    # html_to_md must also produce markdown that renders to valid HTML again.
    validate_telegram_html(md_to_html(html_to_md(html)))


class _CharFormats(HTMLParser):
    """Visible non-space characters paired with the set of tags applied to each."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, tuple]] = []
        self.chars: list[tuple[str, frozenset]] = []

    def handle_starttag(self, tag, attrs) -> None:
        # aiogram writes <code language="language-x">, md_to_html writes class=.
        attrs = tuple(("class" if k == "language" else k, v) for k, v in attrs)
        self.stack.append((tag, attrs))

    def handle_endtag(self, tag) -> None:
        self.stack.pop()

    def handle_data(self, data) -> None:
        self.chars.extend((ch, frozenset(self.stack)) for ch in data if not ch.isspace())


def char_formats(html: str) -> list[tuple[str, frozenset]]:
    parser = _CharFormats()
    parser.feed(html)
    parser.close()
    return parser.chars


WORDS = ["alpha", "beta", "гамма", "x", "👍", "a < b", "q&a", "end."]
INLINE_KINDS = ["bold", "italic", "underline", "strikethrough", "spoiler", "code", "text_link"]


def random_entities_html(rng: random.Random) -> str:
    """aiogram HTML for random properly nested entities on whole words."""
    words = [rng.choice(WORDS) for _ in range(rng.randint(1, 8))]
    spans, pos = [], 0
    for word in words:
        size = len(word.encode("utf-16-le")) // 2
        spans.append((pos, size))
        pos += size + 1
    entities: list[MessageEntity] = []

    def build(lo: int, hi: int, used_kinds: frozenset, depth: int) -> None:
        taken: list[tuple[int, int]] = []
        for _ in range(rng.randint(0, 2)):
            a = rng.randint(lo, hi - 1)
            b = rng.randint(a, hi - 1)
            if depth > 2 or any(not (b < x or a > y) for x, y in taken):
                continue
            taken.append((a, b))
            kind = rng.choice([k for k in INLINE_KINDS if k not in used_kinds])
            start, end = spans[a][0], spans[b][0] + spans[b][1]
            extra = {"url": "https://ex.com/p_q"} if kind == "text_link" else {}
            entities.append(E(kind, start, end - start, **extra))
            if kind not in ("code", "text_link"):
                build(a, b + 1, used_kinds | {kind}, depth + 1)

    build(0, len(words), frozenset(), 0)
    return html_decoration.unparse(" ".join(words), entities)


@pytest.mark.parametrize("seed", range(150))
def test_random_entities_round_trip(seed: int) -> None:
    html = random_entities_html(random.Random(seed))
    back = md_to_html(html_to_md(html))
    validate_telegram_html(back)
    assert char_formats(back) == char_formats(html)


@pytest.mark.parametrize(
    "md",
    [
        "[" * 20000,
        "*a" * 10000,
        "[a](" * 5000,
        "**a*" * 5000,
        "".join("`" * k + " " for k in range(1, 150)),
        "https://x.com/" + ")" * 20000,
    ],
)
def test_pathological_input_is_fast(md: str) -> None:
    started = time.perf_counter()
    validate_telegram_html(md_to_html(md))
    assert time.perf_counter() - started < 2


# --------------------------------------------------------------------------- SPEC_V23: custom emoji

from app.markdown import has_custom_emoji, strip_custom_emoji_html  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("![😀](tg://emoji?id=123)", True),
    ("текст ![👍](TG://EMOJI?ID=5368324170671202286) **жирный**", True),
    ('<tg-emoji emoji-id="1">👍</tg-emoji> ok', True),
    ("<b>x</b> <TG-EMOJI emoji-id=\"1\">👍</TG-EMOJI>", True),
    ("`![😀](tg://emoji?id=123)`", False),  # inside code it is just text
    ("```\n![😀](tg://emoji?id=123)\n```", False),
    ("\\![😀](tg://emoji?id=123)", False),  # escaped: an ordinary «!» and a link to tg://
    ("![😀](tg://emoji?id=abc)", False),
    ("tg://emoji?id=1", False),
    ("обычный 😀 текст", False),
    ("", False),
])
def test_has_custom_emoji(text: str, expected: bool) -> None:
    assert has_custom_emoji(text) is expected


@pytest.mark.parametrize("html, expected", [
    ('<tg-emoji emoji-id="1">👍</tg-emoji> ok', "👍 ok"),
    ('<b>a <tg-emoji emoji-id="5">😀</tg-emoji></b> <tg-emoji emoji-id="6">🔥</tg-emoji>', "<b>a 😀</b> 🔥"),
    ('<blockquote><TG-EMOJI emoji-id="7">&lt;3</TG-EMOJI></blockquote>', "<blockquote>&lt;3</blockquote>"),
    ("<b>без эмодзи</b>", "<b>без эмодзи</b>"),
    ("", ""),
])
def test_strip_custom_emoji_html(html: str, expected: str) -> None:
    result = strip_custom_emoji_html(html)
    assert result == expected
    validate_telegram_html(result)
    assert visible_length(result) == visible_length(html)


def test_strip_custom_emoji_round_trip_from_markdown() -> None:
    html = md_to_html("Привет ![👍](tg://emoji?id=5368324170671202286) **мир**")
    assert has_custom_emoji(html)
    stripped = strip_custom_emoji_html(html)
    assert stripped == "Привет 👍 <b>мир</b>" and not has_custom_emoji(stripped)


def test_syntax_help_mentions_fragment() -> None:
    assert "Fragment" in SYNTAX_HELP_HTML
