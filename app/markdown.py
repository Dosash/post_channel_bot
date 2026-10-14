"""Markdown ⇄ Telegram HTML для постов.

Поддерживаемый диалект Markdown (показывается пользователю в справке):

    **жирный**                      → жирный
    *курсив*  или  _курсив_         → курсив
    __подчёркнутый__                → подчёркнутый
    ~~зачёркнутый~~                 → зачёркнутый
    ||спойлер||                     → скрытый текст
    `код`                           → моноширинный текст
    ```python                       → блок кода (язык можно не указывать)
    print("привет")
    ```
    [текст](https://site.ru)        → ссылка (текст ссылки можно форматировать)
    ![😀](tg://emoji?id=123)        → премиум-эмодзи
    > цитата                        → цитата (соседние строки объединяются)
    >> текст                        → сворачиваемая цитата
    # Заголовок … ###### Заголовок  → жирная строка
    - пункт   * пункт   + пункт     → • пункт
    1. пункт                        → без изменений
    \\*                              → символ «*» без форматирования

Правила:
    * всё, что не является разметкой, выводится как есть (HTML-теги, набранные
      руками, показываются текстом);
    * внутри `кода` и блоков кода разметка не работает;
    * ссылки (http://, https://, tg://, mailto:, а также t.me/...) не ломаются из-за
      подчёркиваний: https://ex.com/a_b_c и snake_case остаются как есть;
    * `_` выделяет только целые слова, `*` работает и внутри слова;
    * незакрытая разметка остаётся обычным текстом.

Implementation notes (for developers):
    md_to_html works in two stages. Block level runs on the raw text, line by line:
    fenced code blocks, blockquote runs ("> " / ">> "), headings, bullets and
    paragraphs (runs of consecutive plain lines). Every inline unit is then parsed by a
    CommonMark-lite inline parser: backslash escapes and code spans first, then links /
    custom emoji (link text parsed recursively) and bare http(s) autolinks (protected
    from emphasis), then emphasis delimiter runs resolved with the CommonMark delimiter
    algorithm ("process emphasis", including the rule of 3). Tags are emitted through a
    writer that keeps a stack, so the output is always properly nested.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from html.parser import HTMLParser

__all__ = [
    "CAPTION_LIMIT",
    "SYNTAX_HELP_HTML",
    "TEXT_LIMIT",
    "excerpt",
    "has_custom_emoji",
    "html_to_md",
    "md_to_html",
    "strip_custom_emoji_html",
    "strip_html",
    "validate_telegram_html",
    "visible_length",
]

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

# Ready-to-send (parse_mode="HTML") cheat sheet for the bot's /help.
SYNTAX_HELP_HTML = (
    "<b>Форматирование текста (Markdown)</b>\n"
    "<code>**жирный**</code> → <b>жирный</b>\n"
    "<code>*курсив*</code> или <code>_курсив_</code> → <i>курсив</i>\n"
    "<code>__подчёркнутый__</code> → <u>подчёркнутый</u>\n"
    "<code>~~зачёркнутый~~</code> → <s>зачёркнутый</s>\n"
    "<code>||спойлер||</code> → <tg-spoiler>спойлер</tg-spoiler>\n"
    "<code>`код`</code> → <code>код</code>\n"
    "<code>```python</code> … <code>```</code> → блок кода\n"
    "<code>[текст](https://example.com)</code> → "
    '<a href="https://example.com">текст</a>\n'
    "<code>![😀](tg://emoji?id=123)</code> → премиум-эмодзи (в каналах — только если у бота есть "
    "юзернейм с Fragment)\n"
    "<code>&gt; цитата</code> → цитата (строки подряд объединяются)\n"
    "<code>&gt;&gt; текст</code> → сворачиваемая цитата\n"
    "<code># Заголовок</code> → <b>Заголовок</b>\n"
    "<code>- пункт</code> → • пункт\n"
    "<code>\\*</code> → символ * без форматирования"
)

_PUNCT = frozenset(string.punctuation)


def _esc(text: str) -> str:
    """Escape text for Telegram HTML (only &, <, > are special)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(text: str) -> str:
    """Escape an attribute value (additionally escapes the double quote)."""
    return _esc(text).replace('"', "&quot;")


def _unescape_backslashes(text: str) -> str:
    """Resolve markdown backslash escapes (``\\X`` → ``X`` for ASCII punctuation)."""
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)


# --------------------------------------------------------------------------- blocks

_FENCE_OPEN = re.compile(r"^( {0,3})(`{3,})([^`]*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,})[ \t]*$")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+(\S.*)$")
_QUOTE = re.compile(r"^ {0,3}(>>?)(?:[ \t]|$)")
_LANG = re.compile(r"[A-Za-z0-9_+#.\-]+")


def md_to_html(text: str) -> str:
    """Convert the post markdown dialect into Telegram HTML (never raises)."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = _render_blocks(lines, allow_quotes=True)
    while out and not out[0].strip():
        out.pop(0)
    return _drop_empty_tags("\n".join(out).rstrip())


def _render_blocks(lines: list[str], *, allow_quotes: bool) -> list[str]:
    """Render block structure; returns output lines (an entry may span several lines)."""
    out: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append(_render_inline("\n".join(paragraph)))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_OPEN.match(line)
        if fence:
            flush_paragraph()
            i = _render_fence(lines, i, fence, out)
            continue
        quote = _QUOTE.match(line) if allow_quotes else None
        if quote:
            flush_paragraph()
            i = _render_quote(lines, i, quote.group(1), out)
            continue
        if not line.strip():
            flush_paragraph()
            out.append("")
        elif (heading := _HEADING.match(line)) and heading.group(1).strip():
            flush_paragraph()
            out.append(f"<b>{_render_inline(heading.group(1), active=('b',))}</b>")
        elif bullet := _BULLET.match(line):
            flush_paragraph()
            out.append(f"{bullet.group(1)}• {_render_inline(bullet.group(2))}")
        else:
            paragraph.append(line)
        i += 1
    flush_paragraph()
    return out


def _render_fence(lines: list[str], start: int, fence: re.Match[str], out: list[str]) -> int:
    """Render a fenced code block starting at ``lines[start]``; returns the next index.

    An unterminated fence runs to the end of the text. A block without any visible
    content is shown literally (Telegram rejects empty entities).
    """
    indent = len(fence.group(1))
    fence_len = len(fence.group(2))
    info = fence.group(3).strip()
    lang = info.split()[0] if info else ""
    if lang and not _LANG.fullmatch(lang):
        lang = ""
    body: list[str] = []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        closing = _FENCE_CLOSE.match(lines[j])
        if closing and len(closing.group(1)) >= fence_len:
            end = j + 1
            break
        body.append(_strip_indent(lines[j], indent))
    code = "\n".join(body)
    if not code.strip():
        out.extend(_esc(raw) for raw in lines[start:end])
    elif lang:
        out.append(f'<pre><code class="language-{_esc_attr(lang)}">{_esc(code)}</code></pre>')
    else:
        out.append(f"<pre>{_esc(code)}</pre>")
    return end


def _strip_indent(line: str, indent: int) -> str:
    """Remove up to ``indent`` leading spaces (the indentation of the opening fence)."""
    removable = len(line) - len(line.lstrip(" "))
    return line[min(indent, removable):]


def _render_quote(lines: list[str], start: int, kind: str, out: list[str]) -> int:
    """Merge consecutive quote lines of the same kind into one <blockquote>."""
    body: list[str] = []
    j = start
    while j < len(lines):
        match = _QUOTE.match(lines[j])
        if not match or match.group(1) != kind:
            break
        body.append(lines[j][match.end():])
        j += 1
    inner = _render_blocks(body, allow_quotes=False)
    while inner and not inner[0].strip():
        inner.pop(0)
    while inner and not inner[-1].strip():
        inner.pop()
    if not inner:
        out.extend(_esc(raw) for raw in lines[start:j])
        return j
    opening = "<blockquote expandable>" if kind == ">>" else "<blockquote>"
    out.append(opening + "\n".join(inner) + "</blockquote>")
    return j


_EMPTY_TAG = re.compile(r"<(b|i|u|s|tg-spoiler)></\1>")


def _drop_empty_tags(html: str) -> str:
    """Safety net: remove empty formatting pairs (Telegram rejects empty entities)."""
    while True:
        cleaned = _EMPTY_TAG.sub("", html)
        if cleaned == html:
            return html
        html = cleaned


# --------------------------------------------------------------------------- inline


@dataclass
class _Delim:
    """A run of emphasis delimiter characters (``*``, ``_``, ``~~`` or ``||``)."""

    char: str
    count: int  # characters not consumed by matches yet
    orig: int  # original run length (for the rule of 3)
    can_open: bool
    can_close: bool
    active: bool = True  # still on the delimiter stack
    open_tags: list[str] = field(default_factory=list)  # in match order (inner first)
    close_tags: list[str] = field(default_factory=list)  # in match order (inner first)


@dataclass
class _Code:
    text: str


@dataclass
class _Link:
    href: str | None  # None: disallowed scheme, render the text only
    children: list[_Item]


@dataclass
class _Emoji:
    emoji_id: str
    alt: str


_Item = str | _Delim | _Code | _Link | _Emoji

_TAGS = {
    ("*", 1): "i",
    ("*", 2): "b",
    ("_", 1): "i",
    ("_", 2): "u",
    ("~", 2): "s",
    ("|", 2): "tg-spoiler",
}
_MERGEABLE = frozenset({"b", "i", "u", "s", "tg-spoiler"})
_MARKUP_CHARS = frozenset("*_~|")

_AUTOLINK = re.compile(r"https?://[^\s<>`\\]+", re.IGNORECASE)
_URL_TRAILING = frozenset(".,:;!?\"'*_~|")
_EMOJI_URL = re.compile(r"tg://emoji\?id=(\d+)", re.IGNORECASE)
_TG_SHORT = re.compile(r"(?:t|telegram)\.me/", re.IGNORECASE)
_ALLOWED_HREF = re.compile(r"(?:https?://[^\s/]|tg://\S|mailto:\S)", re.IGNORECASE)


class _NestingError(Exception):
    """Internal invariant violation while emitting tags (should never happen)."""


class _Writer:
    """Collects HTML output, tracks open tags and drops redundant nested ones."""

    def __init__(self, active: tuple[str, ...] = ()) -> None:
        self.parts: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._depth: dict[str, int] = {tag: 1 for tag in active}

    def text(self, value: str) -> None:
        self.parts.append(_esc(value))

    def raw(self, html: str) -> None:
        self.parts.append(html)

    def open(self, tag: str, attrs: str = "") -> None:
        # <b> inside <b> etc. adds nothing visually, so it is not emitted.
        emit = not (tag in _MERGEABLE and self._depth.get(tag, 0) > 0)
        self._stack.append((tag, emit))
        self._depth[tag] = self._depth.get(tag, 0) + 1
        if emit:
            self.parts.append(f"<{tag}{attrs}>")

    def close(self, tag: str) -> None:
        if not self._stack or self._stack[-1][0] != tag:
            raise _NestingError(tag)
        _, emitted = self._stack.pop()
        self._depth[tag] -= 1
        if emitted:
            self.parts.append(f"</{tag}>")

    def result(self) -> str:
        if self._stack:
            raise _NestingError(self._stack[-1][0])
        return "".join(self.parts)


def _render_inline(src: str, active: tuple[str, ...] = ()) -> str:
    """Render one inline unit (paragraph, heading text, list item text) to HTML."""
    writer = _Writer(active)
    try:
        _render_items(_parse_inline(src, allow_links=True), writer)
        return writer.result()
    except _NestingError:
        # Unreachable by construction; degrade to plain text rather than send bad HTML.
        return _esc(src)


def _parse_inline(src: str, *, allow_links: bool) -> list[_Item]:
    items = _tokenize(src, allow_links=allow_links)
    _process_emphasis([item for item in items if isinstance(item, _Delim)])
    return items


def _render_items(items: list[_Item], writer: _Writer) -> None:
    for item in items:
        if isinstance(item, str):
            writer.text(item)
        elif isinstance(item, _Delim):
            # Closing tags consume the left part of the run, opening tags the right part.
            for tag in item.close_tags:
                writer.close(tag)
            if item.count:
                writer.text(item.char * item.count)
            for tag in reversed(item.open_tags):
                writer.open(tag)
        elif isinstance(item, _Code):
            writer.raw(f"<code>{_esc(item.text)}</code>")
        elif isinstance(item, _Emoji):
            writer.raw(f'<tg-emoji emoji-id="{item.emoji_id}">{_esc(item.alt)}</tg-emoji>')
        elif item.href:
            writer.open("a", f' href="{_esc_attr(item.href)}"')
            _render_items(item.children, writer)
            writer.close("a")
        else:
            _render_items(item.children, writer)


def _tokenize(src: str, *, allow_links: bool) -> list[_Item]:
    """Split an inline unit into text, code, link, emoji and delimiter items."""
    code_spans, brackets = _prescan(src)
    parens = _match_parens(src) if allow_links else {}
    items: list[_Item] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            items.append("".join(buf))
            buf.clear()

    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] in _PUNCT:
            buf.append(src[i + 1])
            i += 2
            continue
        if c == "`":
            run = _run_length(src, i)
            close = code_spans.get(i)
            if close is None:
                buf.append("`" * run)
                i += run
                continue
            content = _code_content(src[i + run:close])
            if content.strip():
                flush()
                items.append(_Code(content))
            else:
                buf.append(src[i:close + run])  # whitespace-only code: literal
            i = close + run
            continue
        if allow_links and c in "![":
            parsed = _try_link(src, i, brackets, parens)
            if parsed is not None:
                flush()
                items.append(parsed[0])
                i = parsed[1]
                continue
        if c in "hH" and (i == 0 or not src[i - 1].isalnum()):
            url = _match_autolink(src, i)
            if url:
                buf.append(url)  # plain text, but never parsed for emphasis
                i += len(url)
                continue
        if c in "*_~|":
            run = _run_length(src, i)
            delim = _make_delim(src, i, run)
            if delim is None:
                buf.append(src[i:i + run])
            else:
                flush()
                items.append(delim)
            i += run
            continue
        buf.append(c)
        i += 1
    flush()
    return items


def _run_length(src: str, i: int) -> int:
    j = i
    while j < len(src) and src[j] == src[i]:
        j += 1
    return j - i


def _prescan(src: str) -> tuple[dict[int, int], dict[int, int]]:
    """Find code spans and bracket pairs in one left-to-right pass.

    Returns ``(code_spans, brackets)``: opening backtick index → index of the closing
    run; ``[`` index → matching ``]`` index (brackets in code spans and escaped ones are
    ignored). Precomputing keeps tokenizing linear even on pathological input.
    """
    code_spans: dict[int, int] = {}
    brackets: dict[int, int] = {}
    stack: list[int] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] in _PUNCT:
            i += 2
            continue
        if c == "`":
            run = _run_length(src, i)
            close = _find_code_close(src, i + run, run)
            if close == -1:
                i += run
            else:
                code_spans[i] = close
                i = close + run
            continue
        if c == "[":
            stack.append(i)
        elif c == "]" and stack:
            brackets[stack.pop()] = i
        i += 1
    return code_spans, brackets


def _find_code_close(src: str, start: int, length: int) -> int:
    """Index of the next backtick run of exactly ``length`` at/after ``start``, or -1."""
    j = src.find("`", start)
    while j != -1:
        run = _run_length(src, j)
        if run == length:
            return j
        j = src.find("`", j + run)
    return -1


def _match_parens(src: str) -> dict[int, int]:
    """Map each ``(`` to its matching ``)`` (backslash-escaped parens are ignored)."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\" and i + 1 < n and src[i + 1] in _PUNCT:
            i += 2
            continue
        if c == "(":
            stack.append(i)
        elif c == ")" and stack:
            pairs[stack.pop()] = i
        i += 1
    return pairs


def _code_content(raw: str) -> str:
    """CommonMark rule: strip one space on both sides when both are present."""
    if len(raw) >= 2 and raw[0] == " " and raw[-1] == " " and raw.strip():
        return raw[1:-1]
    return raw


def _try_link(
    src: str, i: int, brackets: dict[int, int], parens: dict[int, int]
) -> tuple[_Item, int] | None:
    """Parse ``[text](url)`` or ``![alt](tg://emoji?id=N)`` at ``i``; None if not a link."""
    is_emoji = src[i] == "!"
    start = i + 1 if is_emoji else i
    if start >= len(src) or src[start] != "[":
        return None
    close = brackets.get(start)
    if close is None or close + 1 >= len(src) or src[close + 1] != "(":
        return None
    url_end = parens.get(close + 1)
    if url_end is None:
        return None
    raw_url = src[close + 2:url_end].strip()
    label = src[start + 1:close]
    if not raw_url or any(ch.isspace() for ch in raw_url) or not label.strip():
        return None
    url = _unescape_backslashes(raw_url)
    if is_emoji:
        emoji = _EMOJI_URL.fullmatch(url)
        if not emoji:
            return None  # "!" stays literal, "[...](...)" is retried as a plain link
        return _Emoji(emoji.group(1), _unescape_backslashes(label)), url_end + 1
    # Telegram cannot nest links, so link text is parsed without links.
    return _Link(_normalize_href(url), _parse_inline(label, allow_links=False)), url_end + 1


def _normalize_href(url: str) -> str | None:
    """Return a safe href (t.me/x → https://t.me/x) or None for disallowed schemes."""
    if _TG_SHORT.match(url):
        url = "https://" + url
    return url if _ALLOWED_HREF.match(url) else None


def _match_autolink(src: str, i: int) -> str:
    """Bare http(s) URL at ``i`` without trailing punctuation, or ''."""
    match = _AUTOLINK.match(src, i)
    if not match:
        return ""
    url = match.group(0)
    open_paren, close_paren = url.count("("), url.count(")")
    open_sq, close_sq = url.count("["), url.count("]")
    while url:
        last = url[-1]
        if last in _URL_TRAILING:
            pass
        elif last == ")" and close_paren > open_paren:
            close_paren -= 1
        elif last == "]" and close_sq > open_sq:
            close_sq -= 1
        else:
            break
        url = url[:-1]
    if len(url) <= url.find("://") + 3:
        return ""
    return url


def _make_delim(src: str, i: int, run: int) -> _Delim | None:
    """Classify a delimiter run; None when it can neither open nor close.

    CommonMark flanking rules, except that only markup characters (``*_~|``) count as
    "punctuation". So ``**Цена:**100`` still closes after the colon, while in
    ``__**x**__`` the inner ``**`` next to ``__`` can only open/close the right way.
    """
    char = src[i]
    if char in "~|" and run != 2:
        return None
    before = src[i - 1] if i > 0 else " "
    after = src[i + run] if i + run < len(src) else " "
    before_ws, after_ws = before.isspace(), after.isspace()
    before_mark, after_mark = before in _MARKUP_CHARS, after in _MARKUP_CHARS
    can_open = not after_ws and (not after_mark or before_ws or before_mark)
    can_close = not before_ws and (not before_mark or after_ws or after_mark)
    if char == "_":  # underscores never open/close inside a word (snake_case)
        can_open = can_open and not before.isalnum()
        can_close = can_close and not after.isalnum()
    if not (can_open or can_close):
        return None
    return _Delim(char, run, run, can_open, can_close)


def _odd_match(opener: _Delim, closer: _Delim) -> bool:
    """CommonMark "rule of 3" for runs that can both open and close."""
    if closer.char in "~|":
        return False
    return (
        (closer.can_open or opener.can_close)
        and closer.orig % 3 != 0
        and (opener.orig + closer.orig) % 3 == 0
    )


def _use_count(opener: _Delim, closer: _Delim) -> int:
    """How many delimiter characters a match consumes from each side.

    ``~~``/``||`` always use 2. For ``*``/``_`` CommonMark takes 2 when possible; when
    both sides have 3+ (``***x***``) we take 1 first so the italic ends up inside the
    bold: ``<b><i>x</i></b>`` — the same order html_to_md writes ``***x***`` in.
    """
    if closer.char in "~|":
        return 2
    if opener.count >= 3 and closer.count >= 3:
        return 1
    return 2 if opener.count >= 2 and closer.count >= 2 else 1


def _process_emphasis(delims: list[_Delim]) -> None:
    """Match delimiter runs (CommonMark "process emphasis"), recording tags on them."""
    bottoms: dict[tuple[str, bool, int], int] = {}
    # skip_to[k]: next index to inspect when scanning back from k; after a match all
    # delimiters strictly between the pair are inactive, so the scan jumps over them.
    skip_to = list(range(-1, len(delims) - 1))
    ci = 0
    while ci < len(delims):
        closer = delims[ci]
        if not (closer.active and closer.can_close):
            ci += 1
            continue
        key = (closer.char, closer.can_open, closer.orig % 3)
        floor = bottoms.get(key, -1)
        oi = skip_to[ci]
        while oi > floor:
            cand = delims[oi]
            if (
                cand.active
                and cand.can_open
                and cand.char == closer.char
                and not _odd_match(cand, closer)
            ):
                break
            oi = skip_to[oi]
        if oi <= floor:
            bottoms[key] = ci - 1
            if not closer.can_open:
                closer.active = False
            ci += 1
            continue
        opener = delims[oi]
        use = _use_count(opener, closer)
        tag = _TAGS[(closer.char, use)]
        opener.open_tags.append(tag)
        closer.close_tags.append(tag)
        opener.count -= use
        closer.count -= use
        for k in range(oi + 1, ci):
            delims[k].active = False
        skip_to[ci] = oi
        if opener.count == 0:
            opener.active = False
        if closer.count == 0:
            closer.active = False
            ci += 1


# --------------------------------------------------------------------------- HTML → MD

_VOID_TAGS = frozenset(
    {"br", "hr", "img", "input", "meta", "link", "wbr", "area", "base", "col", "embed",
     "source", "track", "param"}
)


class _Node:
    """Minimal DOM node for html_to_md."""

    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str | None]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    """Lenient HTML → _Node tree (stray end tags ignored, unclosed tags auto-closed)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs))
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def html_to_md(html: str) -> str:
    """Convert Telegram HTML (as from aiogram's ``Message.html_text``) to the dialect.

    Plain text is not escaped: in the bot, text typed by the user is markdown source.
    """
    if not html:
        return ""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return _md_children(builder.root.children, in_link=False).rstrip()


def _md_children(children: list[_Node | str], *, in_link: bool) -> str:
    """Render child nodes, keeping block elements (pre, blockquote) on their own lines."""
    out = ""
    after_block = False
    for child in children:
        if isinstance(child, str):
            text, is_block = (_escape_brackets(child) if in_link else child), False
        else:
            text, is_block = _md_node(child, in_link=in_link)
        if not text:
            continue
        if is_block and out and not out.endswith("\n"):
            out += "\n"
        elif after_block and not text.startswith("\n"):
            out += "\n"
        out += text
        after_block = is_block
    return out


_WRAPPERS = {
    "b": "**", "strong": "**",
    "i": "*", "em": "*",
    "u": "__", "ins": "__",
    "s": "~~", "strike": "~~", "del": "~~",
    "tg-spoiler": "||",
}


def _md_node(node: _Node, *, in_link: bool) -> tuple[str, bool]:
    """Render one element; returns (markdown, is_block)."""
    tag = node.tag
    marker = _WRAPPERS.get(tag)
    if tag == "span" and "tg-spoiler" in (node.attrs.get("class") or "").split():
        marker = "||"
    if marker:
        return _wrap_lines(_md_children(node.children, in_link=in_link), marker, marker), False
    if tag == "code":
        return _md_code(_text_content(node)), False
    if tag == "pre":
        return _md_pre(node), True
    if tag == "blockquote":
        return _md_blockquote(node, in_link=in_link), True
    if tag == "a":
        inner = _md_children(node.children, in_link=True)
        href = node.attrs.get("href")
        if not href or in_link:
            return inner, False
        return _wrap_lines(inner, "[", f"]({_md_url(href)})"), False
    if tag == "tg-emoji":
        alt = _text_content(node)
        emoji_id = node.attrs.get("emoji-id") or ""
        if emoji_id.isdigit() and alt.strip():
            return f"![{_escape_brackets(alt)}](tg://emoji?id={emoji_id})", False
        return alt, False
    if tag == "br":
        return "\n", False
    # Unknown tags (tg-time, span without class, ...): keep the text content.
    return _md_children(node.children, in_link=in_link), False


def _text_content(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append("\n")
        else:
            parts.append(_text_content(child))
    return "".join(parts)


def _wrap_lines(inner: str, left: str, right: str) -> str:
    """Wrap every non-blank line separately, moving edge whitespace outside the markers.

    Emphasis cannot open after / close before whitespace, and inline units end at blank
    lines, so "<b>a </b>" must become "**a** " and multi-line bold is wrapped per line.
    """
    out: list[str] = []
    for line in inner.split("\n"):
        core = line.strip()
        if not core:
            out.append(line)
            continue
        lead = line[: len(line) - len(line.lstrip())]
        trail = line[len(line.rstrip()):]
        if core.endswith("\\"):
            core += "\\"  # a trailing backslash would otherwise escape the marker
        out.append(f"{lead}{left}{core}{right}{trail}")
    return "\n".join(out)


def _escape_brackets(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _md_url(url: str) -> str:
    """Make a URL safe inside ``(...)`` of a markdown link."""
    url = url.replace("\\", "\\\\").replace(" ", "%20")
    depth = 0
    for ch in url:
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if depth < 0:
            break
    if depth != 0:
        url = url.replace("(", "\\(").replace(")", "\\)")
    return url


def _md_code(text: str) -> str:
    """Inline code span per line, with a fence longer than any backtick run inside."""
    out: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        longest = max((len(run) for run in re.findall(r"`+", line)), default=0)
        fence = "`" * (longest + 1)
        needs_pad = (
            line.startswith("`")
            or line.endswith("`")
            or (line.startswith(" ") and line.endswith(" "))
        )
        pad = " " if needs_pad else ""
        out.append(f"{fence}{pad}{line}{pad}{fence}")
    return "\n".join(out)


def _md_pre(node: _Node) -> str:
    """``<pre>`` → fenced block; language from ``class``/``language`` of inner <code>."""
    text = _text_content(node)
    if not text.strip():
        return ""
    lang = ""
    for child in node.children:
        if isinstance(child, _Node) and child.tag == "code":
            value = child.attrs.get("class") or child.attrs.get("language") or ""
            if value.startswith("language-"):
                lang = value[len("language-"):]
    if not _LANG.fullmatch(lang or "-"):
        lang = ""
    longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _md_blockquote(node: _Node, *, in_link: bool) -> str:
    inner = _md_children(node.children, in_link=in_link).strip("\n")
    if not inner.strip():
        return ""
    prefix = ">>" if "expandable" in node.attrs else ">"
    return "\n".join(f"{prefix} {line}" if line.strip() else prefix for line in inner.split("\n"))


# --------------------------------------------------------------------------- utilities


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")


def strip_html(html: str) -> str:
    """Visible text of an HTML fragment: tags removed, entities unescaped."""
    if not html:
        return ""
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return "".join(collector.parts)


def visible_length(html: str) -> int:
    """Length of the visible text in UTF-16 code units (how Telegram counts limits)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in strip_html(html))


_TG_EMOJI_OPEN = re.compile(r"<tg-emoji\b", re.IGNORECASE)
_TG_EMOJI = re.compile(r"<tg-emoji\b[^>]*>(.*?)</tg-emoji\s*>", re.IGNORECASE | re.DOTALL)


def has_custom_emoji(text_md_or_html: str) -> bool:
    """Whether a post text (markdown source or Telegram HTML) contains custom («premium») emoji:
    ``![😀](tg://emoji?id=…)`` in markdown (not inside code) or ``<tg-emoji emoji-id=…>``."""
    if not text_md_or_html:
        return False
    if _TG_EMOJI_OPEN.search(text_md_or_html):
        return True
    if "tg://emoji" not in text_md_or_html.lower():
        return False
    return bool(_TG_EMOJI_OPEN.search(md_to_html(text_md_or_html)))


def strip_custom_emoji_html(html: str) -> str:
    """Telegram HTML with every ``<tg-emoji …>X</tg-emoji>`` replaced by its fallback ``X`` — how
    a post looks when the bot cannot show custom emoji (no Fragment username)."""
    if not html or not _TG_EMOJI_OPEN.search(html):
        return html
    return _TG_EMOJI.sub(lambda m: m.group(1), html)


def excerpt(text_md_or_html: str, limit: int = 60, *, is_html: bool = False) -> str:
    """One-line plain-text preview, cut to ``limit`` characters with "…"."""
    if limit <= 0 or not text_md_or_html:
        return ""
    html = text_md_or_html if is_html else md_to_html(text_md_or_html)
    flat = " ".join(strip_html(html).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- validation

_ALLOWED_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
     "tg-spoiler", "span", "blockquote", "tg-emoji"}
)
_NO_SELF_NESTING = frozenset({"a", "code", "pre", "blockquote", "tg-emoji"})
_ENTITIES = frozenset({"lt", "gt", "amp", "quot"})


def _check_attrs(tag: str, attrs: list[tuple[str, str | None]]) -> None:
    names = {name: value for name, value in attrs}
    if len(names) != len(attrs):
        raise ValueError(f"Повторяющийся атрибут в теге <{tag}>")
    if tag == "a":
        if set(names) != {"href"} or not (names["href"] or "").strip():
            raise ValueError("Тег <a> должен содержать только непустой атрибут href")
    elif tag == "code":
        if names and (set(names) != {"class"} or not re.fullmatch(r"language-\S+", names["class"] or "")):
            raise ValueError('Тег <code> допускает только атрибут class="language-…"')
    elif tag == "span":
        if names != {"class": "tg-spoiler"}:
            raise ValueError('Тег <span> допустим только с class="tg-spoiler"')
    elif tag == "blockquote":
        if set(names) - {"expandable"}:
            raise ValueError("Тег <blockquote> допускает только атрибут expandable")
    elif tag == "tg-emoji":
        if set(names) != {"emoji-id"} or not (names["emoji-id"] or "").isdigit():
            raise ValueError("Тег <tg-emoji> должен содержать числовой атрибут emoji-id")
    elif names:
        raise ValueError(f"Тег <{tag}> не поддерживает атрибуты")


class _Validator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _ALLOWED_TAGS:
            raise ValueError(f"Недопустимый тег <{tag}>")
        _check_attrs(tag, attrs)
        if tag in _NO_SELF_NESTING and tag in self.stack:
            raise ValueError(f"Тег <{tag}> нельзя вкладывать в такой же тег")
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise ValueError(f"Самозакрывающийся тег <{tag}/> не поддерживается")

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            raise ValueError(f"Лишний закрывающий тег </{tag}>")
        if self.stack[-1] != tag:
            raise ValueError(
                f"Неправильная вложенность тегов: ожидался </{self.stack[-1]}>, а найден </{tag}>"
            )
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        for ch in "<>&":
            if ch in data:
                raise ValueError(f"Неэкранированный символ «{ch}» в тексте")

    def handle_entityref(self, name: str) -> None:
        if name not in _ENTITIES:
            raise ValueError(f"Неподдерживаемая HTML-сущность &{name};")

    def handle_charref(self, name: str) -> None:
        try:
            code = int(name[1:], 16) if name[:1] in "xX" else int(name)
        except ValueError:
            raise ValueError(f"Некорректная HTML-сущность &#{name};") from None
        if not 0 < code <= 0x10FFFF:
            raise ValueError(f"Некорректная HTML-сущность &#{name};")

    def _reject(self, *_: object) -> None:
        raise ValueError("HTML-комментарии и служебные конструкции не поддерживаются")

    handle_comment = handle_decl = handle_pi = unknown_decl = _reject


_LEXEME = re.compile(r"<[^<>]*>|[<>]|&[^;\s<>&]*;?")
_TAG_SHAPE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_ENTITY_SHAPE = re.compile(r"&(?:lt|gt|amp|quot|#[0-9]+|#[xX][0-9a-fA-F]+);")


def _check_lexemes(html: str) -> None:
    """Reject stray ``<``/``>``/``&`` and truncated tags (HTMLParser silently drops them)."""
    for match in _LEXEME.finditer(html):
        token = match.group(0)
        if token in ("<", ">"):
            raise ValueError(f"Неэкранированный символ «{token}» в тексте")
        if token.startswith("<"):
            if not _TAG_SHAPE.fullmatch(token):
                raise ValueError(f"Некорректный тег {token[:40]}")
        elif not _ENTITY_SHAPE.fullmatch(token):
            raise ValueError(f"Неподдерживаемая HTML-сущность или символ «&»: {token[:20]}")


def validate_telegram_html(html: str) -> None:
    """Raise ValueError if ``html`` is not valid Telegram HTML (tags, attributes, nesting)."""
    _check_lexemes(html or "")
    validator = _Validator()
    validator.feed(html or "")
    validator.close()
    if validator.stack:
        raise ValueError(f"Незакрытый тег <{validator.stack[-1]}>")
