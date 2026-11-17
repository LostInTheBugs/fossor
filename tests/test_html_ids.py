"""Un ``id`` HTML est unique par page — et un élément n'a qu'un gestionnaire.

Un doublon ne casse rien visiblement : le second ``getElementById``/``querySelector``
retombe sur le premier élément du document, donc le gestionnaire affecté en dernier
écrase silencieusement le précédent.  C'est exactement ce qui rendait le bouton
« Mettre à jour » inopérant (2026.11.003-c3) : ``#applyBtn`` désignait à la fois le
bouton de mise à jour et celui de l'import de configuration, et le toast
« Aperçu requis avant d'appliquer » répondait à un clic de mise à jour.

Le test parcourt **toutes** les pages de ``static/`` et compte aussi les ids injectés
par ``nav.js`` (bandeau de simulation), puisqu'ils vivent dans le même document.
"""

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"

#: Sélecteurs JS qui affectent un gestionnaire de clic à un id.
CLICK_HANDLER_RE = re.compile(
    r"""\$\("#?([\w-]+)"\)\.onclick"""
    r"""|getElementById\(["']([\w-]+)["']\)\.onclick"""
)
NAV_JS_ID_RE = re.compile(r"""\.id\s*=\s*["']([\w-]+)["']""")
NAV_JS_SET_ID_RE = re.compile(r"""setAttribute\(\s*["']id["']\s*,\s*["']([\w-]+)["']""")


class _PageIds(HTMLParser):
    """Collecte les ``id`` du HTML et les ``onclick`` posés en attribut."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.inline_handlers: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D102 - HTMLParser API
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.append(element_id)
            if values.get("onclick"):
                self.inline_handlers.append(element_id)

    def handle_startendtag(self, tag, attrs):  # noqa: D102 - HTMLParser API
        self.handle_starttag(tag, attrs)


def _pages() -> list[Path]:
    pages = sorted(STATIC.glob("*.html"))
    assert len(pages) >= 7, f"pages manquantes dans {STATIC} : {[p.name for p in pages]}"
    return pages


def _nav_ids() -> list[str]:
    """Ids que nav.js crée lui-même (le bandeau de simulation)."""
    source = (STATIC / "nav.js").read_text(encoding="utf-8")
    return NAV_JS_ID_RE.findall(source) + NAV_JS_SET_ID_RE.findall(source)


def _ids_of(page: Path) -> tuple[list[str], list[str]]:
    parser = _PageIds()
    parser.feed(page.read_text(encoding="utf-8"))
    ids = list(parser.ids)
    # nav.js sort immédiatement sans <nav id="nav"> : ses ids ne concernent que
    # les pages qui montent la navigation.
    if "nav" in ids:
        ids.extend(_nav_ids())
    return ids, parser.inline_handlers


def test_no_duplicate_id_on_any_page():
    problems: list[str] = []
    for page in _pages():
        ids, _ = _ids_of(page)
        duplicates = {value: n for value, n in Counter(ids).items() if n > 1}
        if duplicates:
            problems.append(f"{page.name}: {duplicates}")
    assert problems == [], "id(s) en double sur une page :\n" + "\n".join(problems)


def test_no_element_has_two_click_handlers():
    problems: list[str] = []
    for page in _pages():
        text = page.read_text(encoding="utf-8")
        targets = [left or right for left, right in CLICK_HANDLER_RE.findall(text)]
        _, inline_handlers = _ids_of(page)
        targets.extend(inline_handlers)
        duplicates = {value: n for value, n in Counter(targets).items() if n > 1}
        if duplicates:
            problems.append(f"{page.name}: {duplicates}")
    assert problems == [], "gestionnaire de clic affecté deux fois :\n" + "\n".join(problems)
