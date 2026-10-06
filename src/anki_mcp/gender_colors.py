"""Colour-code German nouns by gender on the cards that show them.

der is blue, die red, das green, and a plural form orange. Seeing a noun in the
same colour every time it appears builds the gender in as part of the word,
instead of as an article memorised beside it -- a lighter cousin of Wyner's
gender imagery, which the "Gender Mnemonic" field still carries.

It lives in the note type's templates rather than in the notes, for two
reasons:

1. Every note gets it, existing and future, with nothing to backfill and no
   field for add_note to fill in.
2. Changing a template's text is NOT a schema change. Adding a field would be,
   and AnkiWeb answers a schema change by demanding a full sync, which
   sync_to_ankiweb refuses.

Nouns are found by spelling, not grammar: German capitalises every noun, so an
article followed directly by a capitalised word is a noun phrase. Only the
"Word" and "Forms" elements are scanned -- an example sentence inflects its
articles (dem, den, des), and colouring by form there would teach the wrong
gender as often as the right one.

Forms mixes a paradigm ("das Heft, die Hefte") with prose, and prose inflects
too: "nördlich der Alpen" is a genitive plural, "der Reihenfolge nach" a
dative feminine. So a noun phrase is coloured only when it is a whole ENTRY --
an article opening a segment (start, or after ; : . , · — = / or an opening
bracket), and the noun closing it (end, punctuation, or a [IPA] / (note)
bracket). The article must be lowercase unless it opens the text, which keeps
out sentences like "Die Zehner enden auf -zig". Every rule here was measured
against the 471 real notes, whose Forms text is what they were written for.

Plural is orange when it can be told: a "die X" after a comma or · where X is
an inflected form of the noun just before it ("das Konto, die Konten" -- not
the synonym list "die Zahlungsbestätigung, die Auftragsbestätigung"); a "-en"
shorthand after a comma; or one marked "(Plural)" / "(Pl.)". A bare "die X"
otherwise cannot be told apart from a feminine, so it stays red unless the note
is tagged `plural`.

The colour only goes where the word is SHOWN: every answer side, plus a front
that renders {{Word}}. The "Wort produzieren" prompt asks for the word,
article included, so colouring it would give the gender away.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from anki.collection import Collection

log = logging.getLogger(__name__)

DEFAULT_NOTE_TYPES = ("German (Fluent Forever)",)

# Delimit what this module owns, so an install replaces its own block and
# leaves every hand-written line of the template alone.
_CSS_START = "/* anki-mcp:gender-colors:start */"
_CSS_END = "/* anki-mcp:gender-colors:end */"
_HTML_START = "<!-- anki-mcp:gender-colors:start -->"
_HTML_END = "<!-- anki-mcp:gender-colors:end -->"

CSS = f"""{_CSS_START}
.gc-m {{ color: #1d4ed8; }}
.gc-f {{ color: #dc2626; }}
.gc-n {{ color: #15803d; }}
.gc-p {{ color: #c2410c; }}
.nightMode .gc-m, .card.nightMode .gc-m {{ color: #60a5fa; }}
.nightMode .gc-f, .card.nightMode .gc-f {{ color: #f87171; }}
.nightMode .gc-n, .card.nightMode .gc-n {{ color: #4ade80; }}
.nightMode .gc-p, .card.nightMode .gc-p {{ color: #fb923c; }}
{_CSS_END}"""

# The matching, with no DOM in it, so tests can run it under plain node.
# Plain ES5: it runs in AnkiDroid's and AnkiMobile's webviews too.
MATCHER = r"""
var GC_GENDER = {der: "gc-m", die: "gc-f", das: "gc-n"};
var GC_NOUN = /(^|[^A-Za-zÄÖÜäöüß])(der|die|das|Der|Die|Das)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]*)/g;
// "der Verlag, -e" / "die Wanderung, -en" / "der Herausgeber, -"
var GC_SHORT_PLURAL = /,\s*(¨?-[a-zäöüß]*)(?=[\s.;,)]|$)/g;
var GC_ENTRY_START = /(^|[;:.,·—–=\/(\[])\s*$/;
var GC_ENTRY_END = /^\s*($|[;:.,·—–=\/()\[\]!?"“„])/;
var GC_MARKED_PLURAL = /^\s*\((nur\s+)?(Plural|Pl\.)/i;

function gcFold(word) {
  return word.toLowerCase().replace(/ä/g, "a").replace(/ö/g, "o").replace(/ü/g, "u");
}

// Heft/Hefte, Rad/Räder, Konto/Konten -- but not Leistung/Leistungsbeschreibung
// (a compound) or Baumpate/Baumpatin (the feminine).
function gcPluralOf(head, word) {
  var h = gcFold(head), w = gcFold(word);
  if (/in$/.test(w) && !/in$/.test(h)) return false;
  return w.indexOf(h.slice(0, -1)) === 0 && w.length >= h.length && w.length - h.length <= 3;
}

// [[start, end, class], ...] for one run of text, in order, not overlapping.
function gcHits(text, inForms, pluralTag) {
  var m, hits = [], head = null, kept = [], last = 0;
  GC_NOUN.lastIndex = 0;
  while ((m = GC_NOUN.exec(text))) {
    var start = m.index + m[1].length;
    var art = m[2].toLowerCase(), noun = m[3], cls = GC_GENDER[art];
    var before = text.slice(0, start), after = text.slice(GC_NOUN.lastIndex);
    if (!GC_ENTRY_START.test(before) || !GC_ENTRY_END.test(after)) continue;
    if (m[2] !== art && !/^\s*$/.test(before)) continue;
    if (art === "die" && (pluralTag || GC_MARKED_PLURAL.test(after) ||
        (head && /[,·]\s*$/.test(before) && gcPluralOf(head, noun)))) {
      cls = "gc-p";
    } else {
      head = noun;
    }
    hits.push([start, GC_NOUN.lastIndex, cls]);
  }
  if (inForms) {
    GC_SHORT_PLURAL.lastIndex = 0;
    while ((m = GC_SHORT_PLURAL.exec(text))) {
      var s0 = m.index + m[0].length - m[1].length;
      hits.push([s0, s0 + m[1].length, "gc-p"]);
    }
  }
  hits.sort(function (a, b) { return a[0] - b[0]; });
  hits.forEach(function (h) {
    if (h[0] >= last) { kept.push(h); last = h[1]; }
  });
  return kept;
}
"""

# Idempotent because an answer side repeats the front through {{FrontSide}},
# so this can run twice over one card.
SCRIPT = r"""
(function () {
""" + MATCHER + r"""
  var tags = document.querySelector(".gc-tags");
  var pluralTag = !!tags && (" " + tags.textContent.toLowerCase() + " ").indexOf(" plural ") !== -1;

  function colour(node, inForms) {
    var text = node.nodeValue, hits = gcHits(text, inForms, pluralTag), last = 0;
    if (!hits.length) return;
    var parent = node.parentNode;
    hits.forEach(function (h) {
      parent.insertBefore(document.createTextNode(text.slice(last, h[0])), node);
      var s = document.createElement("span");
      s.className = "gc " + h[2];
      s.textContent = text.slice(h[0], h[1]);
      parent.insertBefore(s, node);
      last = h[1];
    });
    parent.insertBefore(document.createTextNode(text.slice(last)), node);
    parent.removeChild(node);
  }

  var roots = document.querySelectorAll(".word, .forms");
  for (var i = 0; i < roots.length; i++) {
    var root = roots[i];
    if (root.getAttribute("data-gc")) continue;
    root.setAttribute("data-gc", "1");
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var nodes = [], n;
    while ((n = walker.nextNode())) nodes.push(n);
    var inForms = root.className.indexOf("forms") !== -1;
    nodes.forEach(function (t) { colour(t, inForms); });
  }
})();
"""

HTML = (
    f"{_HTML_START}\n"
    '<span class="gc-tags" hidden>{{Tags}}</span>\n'
    f"<script>{SCRIPT}</script>\n"
    f"{_HTML_END}"
)

_CSS_BLOCK = re.compile(re.escape(_CSS_START) + r".*?" + re.escape(_CSS_END), re.S)
_HTML_BLOCK = re.compile(re.escape(_HTML_START) + r".*?" + re.escape(_HTML_END), re.S)


def _with_block(text: str, pattern: re.Pattern[str], block: str | None) -> str:
    """`text` with this module's block replaced by `block`, or removed if None."""
    stripped = pattern.sub("", text).rstrip()
    return f"{stripped}\n\n{block}\n" if block else f"{stripped}\n"


def shows_word(side: str, is_answer: bool) -> bool:
    """Whether a template side shows the word, and so should be coloured.

    {{type:Word}} is an input box, not the word, so the production prompt
    does not count -- which is what keeps its gender a question.
    """
    return is_answer or "{{Word}}" in _HTML_BLOCK.sub("", side)


def styled(notetype: dict[str, Any]) -> dict[str, Any]:
    """The note type with the colour coding applied. Pure; does not save."""
    notetype = dict(notetype)
    notetype["css"] = _with_block(notetype.get("css", ""), _CSS_BLOCK, CSS)
    tmpls = []
    for tmpl in notetype["tmpls"]:
        tmpl = dict(tmpl)
        for key, is_answer in (("qfmt", False), ("afmt", True)):
            block = HTML if shows_word(tmpl[key], is_answer) else None
            tmpl[key] = _with_block(tmpl[key], _HTML_BLOCK, block)
        tmpls.append(tmpl)
    notetype["tmpls"] = tmpls
    return notetype


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["css"].strip() != b["css"].strip():
        return False
    return all(
        x[k].strip() == y[k].strip()
        for x, y in zip(a["tmpls"], b["tmpls"])
        for k in ("qfmt", "afmt")
    )


def install(col: Collection, names: Iterable[str]) -> list[str]:
    """Apply the colour coding to each named note type that exists.

    Idempotent: a note type already carrying the current version is left
    untouched, so a restart does not bump its modification time and make the
    next sync upload a note type that did not change. Returns the names that
    were actually updated.
    """
    updated = []
    for name in names:
        notetype = col.models.by_name(name)
        if notetype is None:
            continue
        new = styled(notetype)
        if _same(notetype, new):
            continue
        col.models.update_dict(new)
        updated.append(name)
        log.info("applied gender colours to note type %r", name)
    return updated
