"""
Russian typo corrector using SymSpell with phonetic fallback (Metaphone-RU).

Dictionary is loaded lazily from app/data/nlp/ru_dict.txt on first use.
If the file or symspellpy are unavailable the corrector returns the original
tokens unchanged (silent degradation).
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict

log = logging.getLogger(__name__)

_symspell = None
_phonetic_index: dict[str, str] = {}  # phonetic_key → best word
_ready = False

# Bounded LRU of domain words injected at request time (column labels, etc.).
# Without a cap these accumulate in the global SymSpell dictionary forever —
# a slow memory leak and a source of cross-request correction pollution.
_injected_words: "OrderedDict[str, None]" = OrderedDict()
_MAX_INJECTED_WORDS = 2000


def _load(dict_path: str | None = None) -> None:
    global _symspell, _phonetic_index, _ready
    if _ready:
        return

    if dict_path is None:
        base = os.path.dirname(__file__)
        dict_path = os.path.join(base, "..", "..", "data", "nlp", "ru_dict.txt")
        dict_path = os.path.normpath(dict_path)

    try:
        from symspellpy import SymSpell  # type: ignore
        ss = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
        if os.path.exists(dict_path):
            ss.load_dictionary(dict_path, term_index=0, count_index=1)
            _symspell = ss
            log.info("typo_corrector: SymSpell loaded from %s", dict_path)
        else:
            log.warning("typo_corrector: dictionary not found at %s", dict_path)
    except Exception as exc:
        log.warning("typo_corrector: symspellpy unavailable (%s)", exc)

    # Build phonetic index from dictionary words if available
    if _symspell is not None:
        from app.services.nlp.metaphone_ru import phonetic_key
        try:
            words = list(_symspell.words.keys())
            for w in words:
                pk = phonetic_key(w)
                if pk not in _phonetic_index:
                    _phonetic_index[pk] = w
        except Exception as exc:
            log.debug("typo_corrector: phonetic index build failed: %s", exc)

    _ready = True


def _correct_token(token: str) -> str:
    if _symspell is None:
        return token
    suggestions = _symspell.lookup(token, verbosity=0, max_edit_distance=2)
    if suggestions:
        return suggestions[0].term
    # Phonetic fallback
    if _phonetic_index:
        from app.services.nlp.metaphone_ru import phonetic_key
        pk = phonetic_key(token)
        if pk in _phonetic_index:
            return _phonetic_index[pk]
    return token


def add_domain_words(words: list[str]) -> None:
    """Inject context-specific words (e.g. current column labels) into the lookup.

    Deduplicated and bounded by an LRU cap so repeated requests with varied
    column labels don't grow the SymSpell dictionary without limit.
    """
    if _symspell is None:
        return
    from app.services.nlp.metaphone_ru import phonetic_key
    for w in words:
        wl = w.lower()
        if not wl:
            continue
        if wl in _injected_words:
            _injected_words.move_to_end(wl)
            continue
        _symspell.create_dictionary_entry(wl, 10_000)
        pk = phonetic_key(wl)
        if pk not in _phonetic_index:
            _phonetic_index[pk] = wl
        _injected_words[wl] = None
        while len(_injected_words) > _MAX_INJECTED_WORDS:
            old, _ = _injected_words.popitem(last=False)
            try:
                _symspell.delete_dictionary_entry(old)
            except Exception:
                pass  # older symspellpy without delete API — entry stays, bounded churn


def correct(tokens: list[str], dict_path: str | None = None) -> list[str]:
    """Return typo-corrected tokens. Fails silently → returns originals."""
    try:
        _load(dict_path)
        return [_correct_token(t) for t in tokens]
    except Exception as exc:
        log.debug("typo_corrector.correct failed: %s", exc)
        return tokens
