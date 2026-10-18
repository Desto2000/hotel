"""Calculus theme -> Textual Theme + Rich styles for lobby highlighting.

The Zed theme JSON is read at runtime (hotel/themes/calculus.json), so the
file you attached stays the single source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources

from rich.style import Style
from textual.theme import Theme

THEME_FILE = "calculus.json"


def _hex(c: str | None, drop_alpha: bool = True) -> str | None:
    if not c:
        return None
    c = c.strip()
    if len(c) == 9:  # #RRGGBBAA
        if drop_alpha or c[7:].upper() == "FF":
            return c[:7]
    return c


def _blend(c: str | None, bg: str) -> str | None:
    """Composite #RRGGBBAA over an opaque background (Rich has no alpha)."""
    if not c or len(c) != 9:
        return _hex(c)
    a = int(c[7:9], 16) / 255
    f = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * a + y * (1 - a)):02x}" for x, y in zip(f, b))


def load_zed() -> dict:
    return json.loads(resources.files("hotel.themes").joinpath(THEME_FILE).read_text(encoding="utf-8"))


@dataclass
class Palette:
    name: str
    dark: bool
    s: dict  # zed style map
    syn: dict  # syntax colors

    def c(self, key: str, fallback: str = "#C7C7C7") -> str:
        return _hex(self.s.get(key)) or fallback

    def sy(self, key: str, fallback: str = "#C7C7C7") -> str:
        v = self.syn.get(key)
        return _hex(v["color"]) if v and v.get("color") else fallback


def palettes() -> dict[str, Palette]:
    z = load_zed()
    out = {}
    for t in z["themes"]:
        dark = t["appearance"] == "dark"
        name = "calculus-dark" if dark else "calculus-light"
        out[name] = Palette(name, dark, t["style"], t["style"].get("syntax", {}))
    return out


def textual_theme(p: Palette) -> Theme:
    s = p.s
    players0 = s.get("players", [{}])[0]
    return Theme(
        name=p.name,
        dark=p.dark,
        primary=p.c("text.accent"),
        secondary=p.sy("function"),
        accent=p.sy("keyword"),
        foreground=p.c("text"),
        background=p.c("background"),
        surface=p.c("element.background"),
        panel=p.c("panel.background"),
        boost=_hex(s.get("element.hover")),
        warning=p.c("warning"),
        error=p.c("error"),
        success=p.c("success"),
        variables={
            "border": p.c("pane.focused_border"),
            "border-blurred": p.c("border"),
            "scrollbar": p.c("scrollbar.thumb.border"),
            "scrollbar-hover": p.c("element.hover"),
            "scrollbar-active": p.c("element.active"),
            "scrollbar-background": p.c("scrollbar.track.background"),
            "scrollbar-background-hover": p.c("scrollbar.track.background"),
            "scrollbar-background-active": p.c("scrollbar.track.background"),
            "scrollbar-corner-color": p.c("background"),
            "block-cursor-background": p.c("element.selected"),
            "block-cursor-foreground": p.c("editor.active_line_number"),
            "block-cursor-text-style": "none",
            "block-cursor-blurred-background": p.c("element.background"),
            "block-hover-background": p.c("element.hover"),
            "input-cursor-background": _hex(players0.get("cursor")) or p.c("text"),
            "input-selection-background": _hex(players0.get("selection"), drop_alpha=False) or p.c("element.selected"),
            "footer-background": p.c("status_bar.background"),
            "footer-key-foreground": p.c("text.accent"),
            "footer-description-foreground": p.c("text.muted"),
            "link-color": p.sy("link_uri"),
            "link-color-hover": p.c("link_text.hover"),
            # hotel-specific variables used in app CSS
            "hotel-muted": p.c("text.muted"),
            "hotel-placeholder": p.c("text.placeholder"),
            "hotel-element": p.c("element.background"),
            "hotel-active-line": p.c("editor.active_line.background"),
            "hotel-title": p.sy("title"),
            "hotel-line-number": p.c("editor.line_number"),
            "hotel-hint": p.c("hint"),
            "hotel-hint-bg": p.c("hint.background"),
            "hotel-info-bg": p.c("info.background"),
            "hotel-warning-bg": p.c("warning.background"),
            "hotel-error-bg": p.c("error.background"),
            "hotel-success-bg": p.c("success.background"),
        },
    )


def lobby_styles(p: Palette) -> dict[str, Style]:
    S = Style.parse
    return {
        "header": S(p.sy("preproc")),
        "room_punct": S(p.sy("punctuation.special")),
        "room_kw": S(p.sy("keyword")),
        "room_name": Style(color=p.sy("title"), bold=True),
        "id": S(p.sy("constant")),
        "at": S(p.sy("operator")),
        "bracket": S(p.sy("punctuation.bracket")),
        "sig_open": Style(color=p.c("modified"), bold=True),
        "sig_closed": Style(color=p.c("created"), bold=True),
        "sig_superseded": S(p.c("hidden")),
        "sig_contested": Style(color=p.c("conflict"), bold=True),
        "sig_static": S(p.c("text.muted")),
        "type": S(p.sy("type")),
        "key": S(p.sy("attribute")),
        "origin_user": S(p.sy("variable")),
        "origin_approved": S(p.sy("label")),
        "origin_resident": S(p.sy("hint")),
        "ts": S(p.sy("comment")),
        "pipe": S(p.sy("punctuation")),
        "body": S(p.c("editor.foreground")),
        "dead": S(p.c("text.disabled")),
        "plus": S(p.sy("operator")),
        "meta_key": S(p.sy("property")),
        "meta_ref": S(p.sy("link_uri")),
        "meta_tag": S(p.sy("string.special.symbol")),
        "meta_note": S(p.sy("comment.doc")),
        "meta_value": S(p.sy("string")),
        "error": S(p.c("error")),
        "muted": S(p.c("text.muted")),
        "accent": S(p.c("text.accent")),
        "success": S(p.c("success")),
        "warning": S(p.c("warning")),
        "created": S(p.c("created")),
        "deleted": S(p.c("deleted")),
        "modified": S(p.c("modified")),
        "hint": S(p.c("hint")),
        "number": S(p.sy("number")),
        "string": S(p.sy("string")),
        "function": S(p.sy("function")),
        "match": Style(bgcolor=_blend(p.s.get("search.match_background"), p.c("background")), color=p.c("text")),
        "active_line": Style(bgcolor=_blend(p.s.get("editor.active_line.background"), p.c("background"))),
        "gutter": S(p.c("editor.line_number")),
        "gutter_active": S(p.c("editor.active_line_number")),
        "selection": Style(bgcolor=_blend(p.s.get("players", [{}])[0].get("selection"), p.c("background"))),
    }
