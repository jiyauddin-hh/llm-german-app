"""
Turn a lesson from the backend into something a tutor can teach from.

The backend stores lessons as app questions (mcq, sentence_translation,
speak_question, learn_word, swipe_card, ...), with type-specific fields inside
`content`. The tutor needs the substance, not the widgets: which words, which
sentences, which grammar points. This module pulls those out.

Lesson text comes from the content team, not the learner, but it still goes
into the prompt as quoted material and is length-capped.
"""

from dataclasses import dataclass, field

MAX_ITEMS = 40
MAX_FIELD = 200


def _s(value) -> str:
    """A short single-line string, or '' for anything that is not text."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_FIELD]


@dataclass
class LessonBrief:
    chapter: int
    lesson: int
    title: str = ""
    description: str = ""
    vocabulary: list[str] = field(default_factory=list)   # "Hallo = Hello"
    sentences: list[str] = field(default_factory=list)    # "Hallo, mein Name ist X. (Hello, ...)"
    grammar: list[str] = field(default_factory=list)      # swipe-card explanations
    words: set[str] = field(default_factory=set)          # German words, for scoring

    @property
    def found(self) -> bool:
        return bool(self.title or self.vocabulary or self.sentences)

    def as_text(self) -> str:
        lines = [f"Chapter {self.chapter}, lesson {self.lesson}: {self.title or 'untitled'}"]
        if self.description:
            lines.append(f"Goal: {self.description}")
        if self.vocabulary:
            lines.append("Vocabulary:")
            lines += [f"  - {v}" for v in self.vocabulary[:MAX_ITEMS]]
        if self.sentences:
            lines.append("Key sentences:")
            lines += [f"  - {s}" for s in self.sentences[:MAX_ITEMS]]
        if self.grammar:
            lines.append("Grammar points:")
            lines += [f"  - {g}" for g in self.grammar[:MAX_ITEMS]]
        return "\n".join(lines)


def _add_words(brief: LessonBrief, german: str):
    for w in german.replace(",", " ").replace(".", " ").replace("?", " ").replace("!", " ").split():
        w = w.strip("_-—'\"()").lower()
        if len(w) > 1:
            brief.words.add(w)


def build(data: dict, chapter: int, lesson_no: int) -> LessonBrief:
    brief = LessonBrief(
        chapter=chapter,
        lesson=lesson_no,
        title=_s(data.get("lesson_title")),
        description=_s(data.get("lesson_description")),
    )
    seen = set()

    def add(bucket: list, text: str):
        if text and text not in seen:
            seen.add(text)
            bucket.append(text)

    for q in data.get("questions") or []:
        if not isinstance(q, dict):
            continue
        qtype = q.get("type")
        prompt = _s(q.get("prompt_text"))
        prompt_lang = (q.get("prompt_language") or "").lower()
        c = q.get("content") if isinstance(q.get("content"), dict) else {}

        if qtype == "mcq":
            correct = next(
                (_s(o.get("text")) for o in c.get("options") or []
                 if isinstance(o, dict) and o.get("isCorrect")),
                "",
            )
            if correct and prompt:
                if prompt_lang == "english":
                    add(brief.vocabulary, f"{correct} = {prompt}")
                    _add_words(brief, correct)
                else:
                    add(brief.vocabulary, f"{prompt} = {correct}")
                    _add_words(brief, prompt)

        elif qtype == "sentence_translation":
            target = c.get("targetSentence")
            german = _s(" ".join(t for t in target if isinstance(t, str))) if isinstance(target, list) else ""
            if german:
                add(brief.sentences, f"{german} ({prompt})" if prompt else german)
                _add_words(brief, german)

        elif qtype == "speak_question":
            if prompt:
                hint = _s(c.get("subHint"))
                add(brief.sentences, f"{prompt} ({hint})" if hint else prompt)
                _add_words(brief, prompt)

        elif qtype == "learn_word":
            if prompt:
                meaning = _s(c.get("subHint")).strip("()") or _s(c.get("meaningTranslation")).strip("()")
                add(brief.vocabulary, f"{prompt} = {meaning}" if meaning else prompt)
                _add_words(brief, prompt)
            example = _s(c.get("meaning"))
            if example:
                tr = _s(c.get("meaningTranslation"))
                add(brief.sentences, f"{example} {tr}".strip())

        elif qtype == "swipe_card":
            for card in c.get("swipeCards") or []:
                if not isinstance(card, dict):
                    continue
                side = card.get(card.get("correct")) if card.get("correct") in ("left", "right") else None
                word = _s(side.get("word")) if isinstance(side, dict) else ""
                german = _s(card.get("german"))
                if german and word:
                    filled = german.replace("____", word)
                    eng = _s(card.get("english"))
                    add(brief.sentences, f"{filled} ({eng})" if eng else filled)
                    _add_words(brief, filled)
                expl = _s(card.get("explanation"))
                if expl:
                    add(brief.grammar, expl)

        else:
            # Unknown or future question types: keep German prompts as material.
            if prompt and prompt_lang == "german":
                add(brief.sentences, prompt)
                _add_words(brief, prompt)
            dialogue = _s(c.get("dialogueText"))
            if dialogue:
                add(brief.sentences, dialogue)
                _add_words(brief, dialogue)

    return brief
