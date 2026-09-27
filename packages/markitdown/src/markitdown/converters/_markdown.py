import functools
import re
from typing import Any, BinaryIO, Literal, Optional, Union
from urllib.parse import quote, urlparse, urlunparse

import turbohtml
from turbohtml import Document, Element, Markdown

from .._stream_info import StreamInfo

_PERCENT_ENCODED_OCTET = re.compile(r"%[0-9A-Fa-f]{2}")

# The heading styles markdownify accepted, mapped onto turbohtml's names;
# markdownify treated any other value as ATX
_HEADING_STYLES: dict[str, Literal["atx", "atx_closed", "setext"]] = {
    "atx_closed": "atx_closed",
    "underlined": "setext",
}


def _quote_path_preserving_percent_encoded_octets(path: str) -> str:
    """Quote a URL path while preserving existing %HH byte encodings."""
    parts: list[str] = []
    last_end = 0

    for match in _PERCENT_ENCODED_OCTET.finditer(path):
        parts.append(quote(path[last_end : match.start()]))
        parts.append(match.group(0))
        last_end = match.end()

    parts.append(quote(path[last_end:]))
    return "".join(parts)


def _parse_html(file_stream: BinaryIO, stream_info: StreamInfo) -> Document:
    """Parse an HTML stream, decoding it with the charset the stream declares."""
    data = file_stream.read()
    try:
        return turbohtml.parse(data.decode(stream_info.charset or "utf-8"))
    except (LookupError, UnicodeDecodeError):
        # The declared charset is unknown or wrong, so sniff the bytes instead
        return turbohtml.parse(data, detect_encoding=True)


def _document_title(doc: Union[Document, Element]) -> Optional[str]:
    title = doc.select_one("title")
    return None if title is None else title.text or None


class _CustomMarkdown:
    """
    Renders parsed HTML as Markdown. Changes from turbohtml's defaults include:

    - Accepting the markdownify options markitdown has always forwarded.
    - Removing javascript hyperlinks.
    - Truncating images with large data:uri sources.
    - Ensuring URIs are properly escaped, and do not conflict with Markdown syntax
    """

    def __init__(self, **options: Any):
        self._keep_data_uris = options.get("keep_data_uris", False)
        self._autolinks = options.get("autolinks", True)
        self._default_title = options.get("default_title", False)
        strong_em_symbol = options.get("strong_em_symbol", "*")
        converters = {
            "a": self._convert_a,
            "img": self._convert_img,
            "input": self._convert_input,
            "kbd": self._convert_code,
            "samp": self._convert_code,
            "u": self._convert_u,
        }
        for tag in ("sub", "sup"):
            if symbol := options.get(f"{tag}_symbol"):
                converters[tag] = functools.partial(self._convert_script, symbol)
        # Tags that are stripped, or not converted, render as plain text, so
        # none of the converters above may apply to them
        strip = options.get("strip")
        convert = options.get("convert")
        if strip is not None:
            converters = {k: v for k, v in converters.items() if k not in strip}
        elif convert is not None:
            converters = {k: v for k, v in converters.items() if k in convert}
        self._markdown = Markdown(
            headings=Markdown.Headings(
                style=_HEADING_STYLES.get(
                    options.get("heading_style", "atx").lower(), "atx"
                )
            ),
            lists=Markdown.Lists(bullets=options.get("bullets", "*+-")),
            inline=Markdown.Inline(
                strong=strong_em_symbol * 2, emphasis=strong_em_symbol
            ),
            code=Markdown.Code(language=options.get("code_language", "")),
            tables=Markdown.Tables(
                header="first" if options.get("table_infer_header") else "detect",
                cell_blocks="text",
            ),
            escaping=Markdown.Escaping(
                mode="all" if options.get("escape_misc") else "minimal",
                asterisks=options.get("escape_asterisks", True),
                underscores=options.get("escape_underscores", True),
            ),
            wrapping=Markdown.Wrapping(
                width=options.get("wrap_width", 80) if options.get("wrap") else 0
            ),
            document=Markdown.Document(
                line_break=(
                    "backslash"
                    if options.get("newline_style", "spaces").lower() == "backslash"
                    else "spaces"
                ),
                trim=options.get("strip_document", "strip") or "none",
            ),
            strip=strip,
            convert=convert,
            converters=converters,
        )

    def convert(self, node: Union[Document, Element]) -> str:
        # An underline around nothing but whitespace or a line break would
        # render as nothing at all, so let its content render in its place
        for underline in node.select("u"):
            if not underline.text.strip():
                underline.unwrap()
        return node.to_markdown(self._markdown)

    def _convert_a(self, el: Element, text: str) -> str:
        """Same as usual converter, but removes JavaScript links and escapes URIs."""
        if not text:
            return ""
        prefix = " " if el.text[:1].isspace() else ""
        suffix = " " if el.text[-1:].isspace() else ""

        href = el.attr("href")
        title = el.attr("title")

        # Escape URIs and skip non-http or file schemes
        if href:
            try:
                parsed_url = urlparse(href)
                if parsed_url.scheme and parsed_url.scheme.lower() not in [
                    "http",
                    "https",
                    "file",
                ]:
                    return prefix + text + suffix
                href = urlunparse(
                    parsed_url._replace(
                        path=_quote_path_preserving_percent_encoded_octets(
                            parsed_url.path
                        )
                    )
                )
            except ValueError:  # It's not clear if this ever gets thrown
                return prefix + text + suffix

        # For the replacement see #29: text nodes underscores are escaped
        if (
            self._autolinks
            and text.replace(r"\_", "_") == href
            and not title
            and not self._default_title
        ):
            # Shortcut syntax
            return "%s<%s>%s" % (prefix, href, suffix)
        if self._default_title and not title:
            title = href
        title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
        return (
            "%s[%s](%s%s)%s" % (prefix, text, href, title_part, suffix)
            if href
            else text
        )

    def _convert_img(self, el: Element, text: str) -> str:
        """Same as usual converter, but removes data URIs"""

        alt = el.attr("alt") or ""
        src = el.attr("src") or ""
        data_src = el.attr("data-src") or ""
        # Lazy-loading libraries commonly leave a tiny placeholder data URI in
        # src and put the real image in data-src. Prefer data-src when src
        # isn't a usable URL, so the placeholder doesn't win over actual
        # content. When keep_data_uris is set the caller explicitly wants the
        # embedded bytes, so a data URI in src is left alone.
        if data_src and (
            not src or (src[:5].lower() == "data:" and not self._keep_data_uris)
        ):
            src = data_src
        title = el.attr("title") or ""
        title_part = ' "%s"' % title.replace('"', r"\"") if title else ""
        # Remove all line breaks from alt
        alt = alt.replace("\n", " ")

        # Remove dataURIs
        if src[:5].lower() == "data:" and not self._keep_data_uris:
            src = src.split(",")[0] + "..."

        return "![%s](%s%s)" % (alt, src, title_part)

    def _convert_input(self, el: Element, text: str) -> str:
        """Convert checkboxes to Markdown [x]/[ ] syntax."""

        if el.attr("type") == "checkbox":
            return "[x] " if "checked" in el.attrs else "[ ] "
        return ""

    def _convert_code(self, el: Element, text: str) -> str:
        """Render keyboard input and sample output as inline code."""
        return f"`{el.text}`" if el.text.strip() else ""

    def _convert_script(self, symbol: str, el: Element, text: str) -> str:
        """Wrap sub/superscript text, closing an HTML tag symbol with its end tag."""
        closing = (
            "</" + symbol[1:] if symbol[:1] == "<" and symbol[-1:] == ">" else symbol
        )
        return f"{symbol}{text}{closing}"

    def _convert_u(self, el: Element, text: str) -> str:
        prefix = " " if el.text[:1].isspace() else ""
        suffix = " " if el.text[-1:].isspace() else ""
        return f"{prefix}<u>{text}</u>{suffix}"
