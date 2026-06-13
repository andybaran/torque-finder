"""Deterministic product/brand matching — the shared core for #32 and #28.

This module answers one question, three ways, with **no model in the loop**:

* ``normalize_product(text)`` — derive a :class:`ProductScope` (family + brand)
  from a document title / filename / model string.
* ``extract_query_scope(query)`` — derive the product/brand a mechanic *asked*
  about from a free-text natural-language question.
* ``scope_matches(asked, candidate)`` — the predicate both issues gate on:
  does a retrieved candidate's product identity satisfy the asked product?

#32 (out-of-corpus abstention) consumes this LIVE against each retrieved hit's
already-hydrated ``document_title``: if the query names a recognizable product
and NO retrieved title matches it, abstain. #28 will build its persisted
``product_family`` facet column on the *same* ``normalize_product`` — one
normalizer, one ``scope_matches``, no fork.

Why deterministic and not Claude's self-report: the issue-#32 evidence shows the
model maps a fabricated "Zeb plasma damper" onto the real ZEB damper at 0.90+
confidence, 3/3. Asking that same model "is this the asked product?" launders
the hallucination. A string match over the title cannot launder anything — it
never asks the model — so it is the safety gate's *independent* backstop.

Honest scope: a title check catches BRAND-mismatch hallucinations (Box, Hayes —
brands absent from the SRAM/RockShox/Avid/Zipp/Quarq corpus). It structurally
CANNOT catch an in-corpus-family-but-fabricated-part hallucination ("Zeb plasma
damper" — ZEB legitimately matches a real title); those remain prompt-dependent
and are #28 / #34-adversarial territory, not this gate.

Vocabulary is hand-curated from the actual ~260-manual corpus titles. It is the
single point of maintenance when the corpus grows; tests pin it against the
evidence filenames and the adversarial probe questions.
"""

from __future__ import annotations

import re

from parts_lookup.domain.models import ProductScope

# --- Vocabulary -------------------------------------------------------------
# Brands the corpus actually contains. A query naming one of these is "in our
# universe"; the family/title check then decides whether the specific product
# is present.
_IN_CORPUS_BRANDS: frozenset[str] = frozenset(
    {"rockshox", "sram", "avid", "zipp", "quarq"}
)

# Brands we recognize as real bicycle-component brands that are NOT in the
# corpus. Recognizing them is what lets the gate fire: the query names a known
# product universe we simply do not carry, so no retrieved title can match it.
# (The list is illustrative, not exhaustive — an unknown brand falls through to
# family/token matching, and the degrade-safe no-op covers the rest.)
_OUT_OF_CORPUS_BRANDS: frozenset[str] = frozenset(
    {
        "shimano",
        "fox",
        "campagnolo",
        "magura",
        "hope",
        "crankbrothers",
        "trp",
        "box",
        "hayes",
        "tektro",
        "formula",
        "praxis",
        "easton",
        "enve",
        "industry-nine",
        "onyx",
        "wolftooth",
        "wolf-tooth",
    }
)

# Multi-word brands → their normalized single token. Matched as phrases against
# the query so "DT Swiss" / "Race Face" / "Chris King" / "Cane Creek" are
# recognized as out-of-corpus brands (the Box/Hayes class extends to these).
_BRAND_PHRASES: dict[str, str] = {
    "dt swiss": "dt-swiss",
    "race face": "race-face",
    "chris king": "chris-king",
    "cane creek": "cane-creek",
    "wolf tooth": "wolf-tooth",
}

# Product families that appear in corpus titles. Each maps a normalized family
# key to the set of surface tokens that identify it. Order does not matter; the
# matcher takes the family whose token appears in the text. Hand-curated from
# the corpus filename survey — extend here when the corpus grows.
_FAMILY_TOKENS: dict[str, frozenset[str]] = {
    "pike": frozenset({"pike"}),
    "lyrik": frozenset({"lyrik"}),
    "zeb": frozenset({"zeb"}),
    "yari": frozenset({"yari"}),
    "sid": frozenset({"sid"}),
    "sidluxe": frozenset({"sidluxe"}),
    "revelation": frozenset({"revelation"}),
    "reba": frozenset({"reba"}),
    "recon": frozenset({"recon"}),
    "sektor": frozenset({"sektor"}),
    "domain": frozenset({"domain"}),
    "boxxer": frozenset({"boxxer"}),
    "judy": frozenset({"judy"}),
    "tora": frozenset({"tora"}),
    "argyle": frozenset({"argyle"}),
    "dart": frozenset({"dart"}),
    "bluto": frozenset({"bluto"}),
    "vivid": frozenset({"vivid"}),
    "monarch": frozenset({"monarch"}),
    "deluxe": frozenset({"deluxe"}),
    "kage": frozenset({"kage"}),
    "reverb": frozenset({"reverb"}),
    "code": frozenset({"code"}),
    "guide": frozenset({"guide"}),
    "level": frozenset({"level"}),
    "bb7": frozenset({"bb7"}),
    "db": frozenset({"db5", "db8"}),
    "reaktiv": frozenset({"reaktiv"}),
    # SRAM AXS / Eagle / Reverb-AXS ecosystem families that appear in HTML
    # publications and a couple of PDF titles.
    "eagle": frozenset({"eagle"}),
    "axs": frozenset({"axs"}),
    "rs1": frozenset({"rs-1", "rs1"}),
    "predictive-steering": frozenset({"predictive"}),
}

# Generic tokens in titles that are NOT a product family — never derive a
# family from these (a title that is only generic → family=None, brand-only).
_GENERIC_TITLE_TOKENS: frozenset[str] = frozenset(
    {
        "technical",
        "front",
        "suspension",
        "spare",
        "part",
        "catalog",
        "road",
        "frame",
        "fit",
        "specifications",
        "safety",
        "instructions",
        "user",
        "udh",
        "hose",
        "bleed",
        "procedure",
        "adjustment",
    }
)

# "guide" is genuinely ambiguous in the real corpus: it is BOTH the SRAM Guide
# brake family AND the English noun (a "tuning guide", a "theory guide", an
# "identification guide"). The DB title survey for #28 found 10 titles with
# "guide"; 5 are the brake product and 5 are the genre word. Deriving
# family="guide" on a genre title is a FALSE POSITIVE — worse than NULL, since
# it would boost the wrong product (#28) and let a Guide-brake query falsely
# match a "suspension-theory-guide" title (#32's live gate). So "guide" only
# counts as the Guide BRAKE family when context confirms the product, not the
# genre (verified against all 10 corpus titles + the two "Guide brake" probes).
#
# * a Guide BRAKE variant token immediately follows "guide" (guide-rs / -rsc /
#   -re / -r / -t / -ultimate), OR
# * "guide" is immediately followed by "brake" (the query form), OR
# * "guide" is NOT immediately preceded by a genre context word and NOT a
#   trailing "...-guide" suffix (i.e. it leads a product reference).
_GUIDE_PRODUCT_VARIANTS: frozenset[str] = frozenset(
    {"rs", "rsc", "re", "r", "t", "ultimate"}
)
_GUIDE_GENRE_CONTEXT: frozenset[str] = frozenset(
    {"tuning", "theory", "service", "identification", "start", "quick"}
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics. Keeps ordering for phrase checks."""
    return _TOKEN_RE.findall(text.lower())


def _guide_is_product(tokens: list[str]) -> bool:
    """Disambiguate "guide" (SRAM Guide brake) from the genre word "guide".

    FAIL-SAFE: returns ``True`` (→ family="guide") ONLY when a "guide"
    occurrence is positively a product reference; otherwise the genre reading
    wins and the family is suppressed. Operates on the ordered token list so the
    preceding/following word can be inspected. See ``_GUIDE_GENRE_CONTEXT``.
    """
    for i, tok in enumerate(tokens):
        if tok != "guide":
            continue
        prev_tok = tokens[i - 1] if i > 0 else None
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
        if next_tok == "brake":  # "Guide brake" — the query form
            return True
        if next_tok in _GUIDE_PRODUCT_VARIANTS:  # guide-rs / -rsc / -re / -t / …
            return True
        if prev_tok in _GUIDE_GENRE_CONTEXT:  # "...-tuning-guide", "service-guide"
            continue
        if next_tok is None:  # trailing "...-guide" suffix → genre
            continue
        # "guide" leads a reference and isn't a known genre context → product.
    return False


def _detect_brand(lowered: str, tokens: set[str]) -> tuple[str | None, bool]:
    """Return ``(brand, recognized)``.

    ``recognized`` is True when the brand is a known bicycle brand (in OR out of
    corpus) — that recognition is what lets the gate decide the query named a
    real product universe. Multi-word brand phrases are matched first.
    """
    for phrase, normalized in _BRAND_PHRASES.items():
        if phrase in lowered:
            return normalized, True
    for token in tokens:
        if token in _IN_CORPUS_BRANDS or token in _OUT_OF_CORPUS_BRANDS:
            return token, True
    return None, False


def _detect_family(tokens: set[str], ordered: list[str]) -> str | None:
    """First family whose identifying token appears. ``None`` if none match.

    ``ordered`` is the positional token list; it is only consulted for the
    context-sensitive "guide" disambiguation (genre word vs. Guide brake) — all
    other families are unambiguous single tokens.
    """
    for family, surface in _FAMILY_TOKENS.items():
        if not (surface & tokens):
            continue
        if family == "guide" and not _guide_is_product(ordered):
            # "guide" matched as the genre word, not the Guide brake → suppress
            # so a generic guide title falls through to brand-only / NULL.
            continue
        return family
    return None


def normalize_product(text: str) -> ProductScope:
    """Derive a product/brand scope from a document title / filename / model string.

    FAIL-SAFE: if neither a known brand nor a known family is found, returns an
    unidentified scope (``family=None, brand=None, confidence=0.0``) rather than
    guessing — a mis-derived family would actively *boost the wrong product* and
    trip the gate on correct answers, so "don't know" is the safe default. A
    generic multi-product title (e.g. ``2010-sram-technical-manual.pdf``) yields
    brand-only (``family=None``) on purpose, so ``scope_matches`` treats it as a
    non-blocking generic hit rather than a false mismatch.
    """
    if not text:
        return ProductScope()

    lowered = text.lower()
    token_list = _tokens(lowered)
    token_set = set(token_list)

    brand, _recognized = _detect_brand(lowered, token_set)
    # Generic tokens never count as a family.
    family = _detect_family(token_set - _GENERIC_TITLE_TOKENS, token_list)

    if family is None and brand is None:
        return ProductScope()

    # Family identification is the strong signal; brand-only is weaker.
    confidence = 0.9 if family is not None else 0.6
    return ProductScope(family=family, brand=brand, confidence=confidence)


# Confidence bar for PERSISTING a derived family to the #28 facet column. A
# title that derives only a brand (confidence ~0.6) is below the bar: we keep
# the brand but persist family=None, never a guessed family. The query-time
# live gate (#32) uses the looser bar in `scope_matches`; persisting is stricter
# because a wrong stored family is sticky and would mis-boost every later query.
_FAMILY_PERSIST_CONFIDENCE: float = 0.9


def derive_facet(title: str) -> tuple[str | None, str | None]:
    """Derive the ``(product_family, brand)`` to PERSIST for a document title.

    The #28 facet derivation. FAIL-SAFE by construction (reviewer-required):

    * It delegates to the same :func:`normalize_product` the #32 live gate uses
      — one normalizer, no fork.
    * A ``family`` is persisted ONLY when the derivation cleared the strong
      confidence bar (a positively identified family token, incl. the "guide"
      genre/brake disambiguation). Anything weaker → ``family=None``: we never
      guess a family, because a WRONG stored family is worse than NULL (it would
      boost the wrong product and trip the contamination guard on correct
      answers). ``brand`` is persisted when recognized even without a family
      (generic multi-product manuals keep their brand, family stays NULL).

    Returns ``(None, None)`` for an unrecognizable title — the document simply
    stays product-blind and retrieval degrades to today's behaviour for it.
    """
    scope = normalize_product(title)
    family = scope.family if scope.confidence >= _FAMILY_PERSIST_CONFIDENCE else None
    return family, scope.brand


def extract_query_scope(query: str) -> ProductScope:
    """Derive the product/brand a mechanic asked about from a free-text question.

    Same vocabulary as :func:`normalize_product`, but here recognizing an
    *out-of-corpus* brand (Box, Hayes, Shimano, …) is the point: the returned
    scope is "identified" (``brand`` set), so the gate engages and — finding no
    retrieved title with that brand — abstains. If nothing is recognized the
    scope is unidentified and the gate is a no-op (degrade-safe: never
    over-abstain on an under-specified query).
    """
    if not query:
        return ProductScope()

    lowered = query.lower()
    token_list = _tokens(lowered)
    token_set = set(token_list)

    brand, recognized_brand = _detect_brand(lowered, token_set)
    family = _detect_family(token_set - _GENERIC_TITLE_TOKENS, token_list)

    if family is None and not recognized_brand:
        return ProductScope()

    # A recognized brand alone (e.g. "Box", "Hayes") is enough to engage the
    # gate; a family raises confidence.
    confidence = 0.9 if family is not None else 0.7
    return ProductScope(family=family, brand=brand, confidence=confidence)


def scope_matches(asked: ProductScope, candidate: ProductScope) -> bool:
    """Does ``candidate``'s product identity satisfy the ``asked`` product?

    The predicate the abstention gate (and #28's facet) hangs on. Semantics:

    * **Unidentified asked scope** → ``False``. The caller treats an
      unidentified *asked* scope as "no gate" (degrade-safe no-op) BEFORE
      calling this — by the time we are here the query named something. A
      defensive ``False`` here can never *cause* an abstention on its own
      because the no-op short-circuits first; it only matters in the truth table.
    * **Family match** → ``True`` when both name the same family. The strongest,
      most specific signal.
    * **Brand-only candidate (generic title, ``family=None``)** → treated as
      NON-BLOCKING: if the candidate carries the asked brand (or carries no
      brand at all, i.e. a fully generic title), it counts as a match so a
      generic multi-product manual (``*-sram-technical-manual.pdf``) never
      causes a false mismatch / over-abstention. This is the brand-only /
      generic-title fallback the review required.
    * **Brand mismatch** → ``False``. Asked RockShox/SRAM/…, candidate is a
      different brand (or vice-versa) and families don't match → not a match.
      This is the Box/Hayes win: the asked out-of-corpus brand matches no
      in-corpus title's brand.
    """
    if not asked.is_identified:
        return False

    # Family is the specific signal: if both name a family, they must agree.
    if asked.family is not None and candidate.family is not None:
        return asked.family == candidate.family

    # The candidate is a generic/brand-only title (no family derived). Don't let
    # it cause a false mismatch: it matches if it shares the asked brand or
    # carries no brand at all (fully generic). This keeps generic multi-product
    # manuals non-blocking.
    if candidate.family is None:
        if candidate.brand is None:
            return True
        if asked.brand is not None:
            return asked.brand == candidate.brand
        # Asked named only a family (no brand) and candidate is brand-only with a
        # different... we can't confirm the family is present, so non-blocking.
        return True

    # Asked named only a brand (no specific family), candidate names a family.
    # Match on brand when the candidate carries one; otherwise non-blocking.
    if candidate.brand is not None and asked.brand is not None:
        return asked.brand == candidate.brand
    return False
