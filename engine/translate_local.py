"""Translate caption lines on our box. No outside scrape."""
from __future__ import annotations

import threading
from typing import Iterable

_lock = threading.Lock()
_ready: set[tuple[str, str]] = set()

_ISO3 = {
    "ara": "ar",
    "ben": "bn",
    "ces": "cs",
    "cmn": "zh",
    "deu": "de",
    "ell": "el",
    "eng": "en",
    "fas": "fa",
    "fin": "fi",
    "fra": "fr",
    "guj": "gu",
    "heb": "he",
    "hin": "hi",
    "hun": "hu",
    "ind": "id",
    "ita": "it",
    "jpn": "ja",
    "kan": "kn",
    "kor": "ko",
    "mal": "ml",
    "mar": "mr",
    "nld": "nl",
    "pan": "pa",
    "pol": "pl",
    "por": "pt",
    "ron": "ro",
    "rus": "ru",
    "spa": "es",
    "swe": "sv",
    "tam": "ta",
    "tel": "te",
    "tha": "th",
    "tur": "tr",
    "ukr": "uk",
    "urd": "ur",
    "vie": "vi",
    "yue": "zh",
    "tlh": "tlh",
    "zho": "zh",
}


def iso2(code: str | None) -> str | None:
    if not code:
        return None
    c = code.strip().lower().replace("_", "-")
    if c in ("", "auto", "same"):
        return None
    if "-" in c:
        c = c.split("-", 1)[0]
    if c in _ISO3:
        return _ISO3[c]
    if len(c) == 2:
        return c
    return None


def _ensure_pair(src: str, dst: str) -> bool:
    key = (src, dst)
    if key in _ready:
        return True
    try:
        import argostranslate.package
        import argostranslate.translate
    except ImportError:
        return False
    with _lock:
        if key in _ready:
            return True
        langs = argostranslate.translate.get_installed_languages()
        from_lang = next((l for l in langs if l.code == src), None)
        to_lang = next((l for l in langs if l.code == dst), None)
        if from_lang and to_lang and from_lang.get_translation(to_lang):
            _ready.add(key)
            return True
        try:
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
            pkg = next(
                (p for p in available if p.from_code == src and p.to_code == dst),
                None,
            )
            if pkg is None:
                return False
            argostranslate.package.install_from_path(pkg.download())
        except Exception:
            return False
        langs = argostranslate.translate.get_installed_languages()
        from_lang = next((l for l in langs if l.code == src), None)
        to_lang = next((l for l in langs if l.code == dst), None)
        ok = bool(from_lang and to_lang and from_lang.get_translation(to_lang))
        if ok:
            _ready.add(key)
        return ok


def translate_lines(texts: Iterable[str], target: str, source: str | None = None) -> list[str]:
    lines = [t or "" for t in texts]
    if not lines:
        return []
    dst = iso2(target) or target
    src = iso2(source)
    if not dst or not src or src == dst:
        return list(lines)
    if not _ensure_pair(src, dst):
        return list(lines)
    import argostranslate.translate

    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            got = argostranslate.translate.translate(line, src, dst)
        except Exception:
            got = line
        out.append(got if (got or "").strip() else line)
    if len(out) != len(lines):
        return list(lines)
    return out
