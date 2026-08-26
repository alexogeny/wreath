"""What the search index knows about a section, beyond its first two sentences.

The index used to carry 280 characters per section — the snippet — and match
against that. On wreath's own corpus that indexed 287 KB of 1.27 MB of prose:
**84% of sections were truncated**, so anything explained below the opening of a
section could not be found at all. A reader searching "query parameters" was
told the docs did not discuss them.

So each section also carries a *word set*: every distinct word in the whole
section, stemmed, minus the ones the snippet already answers for. It is a set,
not text, so it costs a fraction of the prose it stands for; it cannot produce a
snippet, which is why the snippet stays.

`stem` is deliberately blunt — plurals only. It exists because `"parameters"`
contains `"param"` but not `"params"`, and a reader who types the plural of a
word should not be told it does not appear. Anything cleverer (a real Porter
stemmer, prefix expansion) starts merging words a reader can tell apart, and a
docs search that quietly widens the question is worse than one that misses.

**`stem` has a twin in `scripts.py`** — the query side runs in the browser. The
two must agree exactly or a term is stemmed on one side only, which is a miss
that looks like an empty corpus. `tests/test_docs_ssg.py` checks them against
the same table.
"""

from __future__ import annotations

from wreath._native import _docs as _native_docs

#: Words that carry no topic. Kept small on purpose: this list is only here to
#: stop the word set filling with `the`, not to model English.
STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be but by can for from has have how if in into is it its may
    not of on or that the their then there these this to was were what when which who
    will with you your""".split())

_STOPWORD_TAPE = tuple(sorted(STOPWORDS))


def stem(word: str) -> str:
    """Strip a plural, and only a plural. Twin of `stem()` in `scripts.py`.

    The length floor is what keeps `cors` from becoming `cor` and `jobs` from
    becoming `job`: below five characters the suffix is more likely to be part
    of the word than a plural of it.
    """
    if len(word) <= 4:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if word.endswith("es"):
        return word[:-1]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def word_set(text: str, covered: str) -> str:
    """The stems in `text` that a substring search of `covered` would not find.

    `covered` is the snippet and heading already in the record. Leaving their
    words out of the set is not an optimisation for its own sake — it is what
    keeps the set to the part of the section nothing else can answer for, and it
    halves the index on a corpus whose sections open with their own topic.
    """
    return _native_docs.word_set(text, covered, _STOPWORD_TAPE)


__all__ = ["STOPWORDS", "stem", "word_set"]
