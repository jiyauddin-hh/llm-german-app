"""
Mira's replies mix two languages: English for explanations, German for what is
being taught ("«Guten Tag» means Good day!").

The model wraps every German word or sentence in «…». This module splits a
reply on those marks, so the app can highlight the German and the voice can
speak each part in its own language instead of reading English with a German
accent (or the other way round).
"""

import re

_MARKED = re.compile(r"«([^«»]*)»")
_LETTER = re.compile(r"[^\W\d_]")

# Used only when the model forgot the marks: a rough vote on common words.
_DE_WORDS = {
    "ich", "du", "sie", "er", "es", "wir", "ihr", "bin", "bist", "ist", "sind",
    "und", "nicht", "der", "die", "das", "den", "dem", "ein", "eine", "wie",
    "was", "wo", "woher", "heißt", "heisse", "heiße", "komme", "kommst", "aus",
    "mit", "auf", "zu", "gut", "sehr", "danke", "bitte", "hallo", "guten",
    "tag", "morgen", "abend", "ja", "nein", "mein", "dein", "auch", "noch",
    "jetzt", "schön", "toll", "richtig", "genau", "geht", "dir", "ihnen",
}
_EN_WORDS = {
    "i", "you", "he", "she", "it", "we", "they", "am", "is", "are", "the",
    "a", "an", "and", "not", "what", "where", "how", "means", "say", "can",
    "to", "of", "in", "with", "for", "this", "that", "good", "great", "yes",
    "no", "my", "your", "try", "let's", "word", "now", "it's",
}


def guess_language(text: str) -> str:
    """'de' or 'en' for unmarked text."""
    words = re.findall(r"[\wäöüß']+", text.lower())
    de = sum(w in _DE_WORDS for w in words) + sum(ch in "äöüß" for ch in text.lower())
    en = sum(w in _EN_WORDS for w in words)
    return "de" if de > en else "en"


def segments(reply: str) -> list[dict]:
    """
    [{"lang": "en"|"de", "text": "..."}] in reading order. Joining the texts
    gives the reply without the marks, spacing included.
    """
    reply = reply or ""
    if "«" not in reply:
        text = reply.replace("»", "").strip()
        return [{"lang": guess_language(text), "text": text}] if text else []

    out: list[dict] = []

    def add(lang: str, text: str):
        text = text.replace("«", "").replace("»", "")  # stray, unbalanced marks
        if not text:
            return
        if not text.strip() and out:
            out[-1]["text"] += text  # bare spacing belongs to its neighbour
        elif out and out[-1]["lang"] == lang:
            out[-1]["text"] += text
        else:
            out.append({"lang": lang, "text": text})

    pos = 0
    for m in _MARKED.finditer(reply):
        add("en", reply[pos:m.start()])
        add("de", m.group(1))
        pos = m.end()
    add("en", reply[pos:])
    if out:
        out[0]["text"] = out[0]["text"].lstrip()
        out[-1]["text"] = out[-1]["text"].rstrip()
    return [x for x in out if x["text"]]


def plain(text: str | None) -> str | None:
    """The text without «» marks, for display."""
    if text is None:
        return None
    return text.replace("«", "").replace("»", "")


def speakable(segs: list[dict]) -> list[tuple[str, str]]:
    """(lang, text) pairs worth sending to a voice: skips bare punctuation."""
    return [(s["lang"], s["text"].strip()) for s in segs if _LETTER.search(s["text"])]
