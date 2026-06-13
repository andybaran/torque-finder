"""Pure unit tests for the deterministic product/brand matcher (#32, shared with #28).

No DB, no network, no model — just the string-matching truth table that the
out-of-corpus abstention gate (and #28's product_family facet) hang on.
"""

from __future__ import annotations

import pytest

from parts_lookup.domain.models import ProductScope
from parts_lookup.retrieval.product_match import (
    derive_facet,
    extract_query_scope,
    normalize_product,
    scope_matches,
)


class TestNormalizeProduct:
    @pytest.mark.parametrize(
        ("title", "family", "brand"),
        [
            ("2014-2017-pike-service-manual.pdf", "pike", None),
            ("2021-2022-zeb-service-manual.pdf", "zeb", None),
            ("2016-2017-lyrik-service-manual.pdf", "lyrik", None),
            ("2012-avid-code-and-code-r-service-manual.pdf", "code", "avid"),
            (
                "2017-2021-level-ultimate-tlm-tl-service-manual-english.pdf",
                "level",
                None,
            ),
            ("2011-vivid-air-service-manual.pdf", "vivid", None),
            # The literal "serivce" typo in the corpus must not break derivation.
            ("2014-2022-vivid-air-serivce-manual.pdf", "vivid", None),
            ("2013-2018-reverb-stealth-a2-and-b1-service-manual.pdf", "reverb", None),
            ("2015-2018-boxxer-rc-service-manual.pdf", "boxxer", None),
            ("2018-sram-code-rsc-r-and-code-stealth.pdf", "code", "sram"),
        ],
    )
    def test_known_titles_derive_family(
        self, title: str, family: str, brand: str | None
    ) -> None:
        scope = normalize_product(title)
        assert scope.family == family
        assert scope.brand == brand
        assert scope.confidence > 0.0

    def test_generic_title_is_brand_only_no_family(self) -> None:
        """A generic multi-product manual yields brand-only (family=None) so
        scope_matches treats it as non-blocking, not a false mismatch."""
        scope = normalize_product("2010-sram-technical-manual.pdf")
        assert scope.family is None
        assert scope.brand == "sram"

    def test_fail_safe_returns_unidentified_when_nothing_recognized(self) -> None:
        """FAIL-SAFE: an unrecognizable title returns family=None/brand=None and
        confidence 0.0 rather than guessing a wrong family."""
        scope = normalize_product("some-completely-unknown-doc.pdf")
        assert scope == ProductScope()
        assert not scope.is_identified

    def test_empty_string_is_unidentified(self) -> None:
        assert normalize_product("") == ProductScope()


class TestExtractQueryScope:
    @pytest.mark.parametrize(
        ("query", "expect_brand", "expect_identified"),
        [
            # Out-of-corpus brands → recognized (so the gate engages), brand set.
            ("Box Three rear derailleur b-bolt torque?", "box", True),
            ("Hayes Dominion A4 banjo bolt torque?", "hayes", True),
            ("What torque for the Shimano XT M8100 lockring?", "shimano", True),
            ("Fox 36 Float fork lower leg pinch-bolt torque?", "fox", True),
            ("What's the torque for a DT Swiss 240 hub end cap?", "dt-swiss", True),
            ("Race Face Aeffect crank pinch bolt torque?", "race-face", True),
            ("Chris King headset top cap preload torque?", "chris-king", True),
            # In-corpus product family named.
            ("What torque for the Pike lower-leg bolts?", None, True),
            ("Avid BB7 caliper mounting bolts are 40 N·m, confirm?", "avid", True),
        ],
    )
    def test_recognized_products_are_identified(
        self, query: str, expect_brand: str | None, expect_identified: bool
    ) -> None:
        scope = extract_query_scope(query)
        assert scope.is_identified is expect_identified
        if expect_brand is not None:
            assert scope.brand == expect_brand

    def test_zeb_family_is_identified_even_when_part_fabricated(self) -> None:
        """Acknowledged limit: 'Zeb plasma damper' names the IN-CORPUS family
        ZEB, so the scope IS identified and a ZEB title would match → the
        deterministic gate cannot catch this part-level fabrication (it's #28 /
        prompt territory). Pin the behavior so the limit is explicit."""
        scope = extract_query_scope("What's the torque for the Zeb's plasma damper bleed bolt?")
        assert scope.family == "zeb"
        assert scope.is_identified

    def test_no_product_query_is_unidentified_no_op(self) -> None:
        """Degrade-safe: a question naming no product/brand is unidentified, so
        the gate is a no-op (never over-abstains)."""
        scope = extract_query_scope("What tool do I use for the top cap bolt?")
        assert not scope.is_identified

    def test_empty_query_is_unidentified(self) -> None:
        assert extract_query_scope("") == ProductScope()


class TestScopeMatches:
    def test_unidentified_asked_never_matches(self) -> None:
        # Caller short-circuits this as a no-op; the predicate itself is False.
        assert not scope_matches(
            ProductScope(), normalize_product("2014-2017-pike-service-manual.pdf")
        )

    def test_same_family_matches(self) -> None:
        asked = extract_query_scope("What torque for the Pike lower-leg bolts?")
        candidate = normalize_product("2014-2017-pike-service-manual.pdf")
        assert scope_matches(asked, candidate)

    def test_different_family_does_not_match(self) -> None:
        asked = extract_query_scope("What torque for the Pike lower-leg bolts?")
        candidate = normalize_product("2021-2022-zeb-service-manual.pdf")
        assert not scope_matches(asked, candidate)

    def test_out_of_corpus_brand_matches_no_corpus_title(self) -> None:
        """The Box/Hayes win: an out-of-corpus brand matches no in-corpus
        product title (different brand, no family overlap)."""
        asked = extract_query_scope("Box Three rear derailleur b-bolt torque?")
        for title in (
            "2014-2017-pike-service-manual.pdf",
            "2012-avid-code-and-code-r-service-manual.pdf",
            "2010-sram-technical-manual.pdf",
        ):
            assert not scope_matches(asked, normalize_product(title))

    def test_generic_title_is_non_blocking(self) -> None:
        """A family query whose answer lives in a generic brand-only manual must
        not be a false mismatch — the generic title is treated as non-blocking
        so we don't over-abstain (review pressure-test (a))."""
        asked = extract_query_scope("What torque for the Pike lower-leg bolts?")
        generic = normalize_product("2010-sram-technical-manual.pdf")  # family=None
        assert scope_matches(asked, generic)

    def test_brand_query_matches_same_brand_title(self) -> None:
        """A brand-only asked scope (out-of-corpus brand absent here) — an
        in-corpus brand query without a family token still matches a same-brand
        title and does not falsely abstain."""
        asked = ProductScope(brand="avid", confidence=0.7)
        candidate = normalize_product(
            "2012-avid-code-and-code-r-service-manual.pdf"
        )
        assert scope_matches(asked, candidate)

    def test_fully_generic_candidate_with_no_brand_is_non_blocking(self) -> None:
        asked = extract_query_scope("What torque for the Pike lower-leg bolts?")
        assert scope_matches(asked, ProductScope())  # candidate fully generic


class TestGuideDisambiguation:
    """\"guide\" is BOTH the SRAM Guide brake AND the genre word. Deriving
    family=\"guide\" on a genre title (a tuning/service/theory GUIDE) is a FALSE
    POSITIVE — worse than NULL (#28). Pinned against the real corpus titles."""

    @pytest.mark.parametrize(
        "title",
        [
            "2016-2019-guide-ultimate-service-manual-english.pdf",
            "2017-2019-guide-rs-and-r-service-manual.pdf",
            "2017-2019-guide-rsc-service-manual-english.pdf",
            "gen0000000005632-rev-c-guide-t-english.pdf",
            "service-manual-2017-2019-guide-re-english.pdf",
        ],
    )
    def test_guide_brake_titles_derive_guide_family(self, title: str) -> None:
        assert normalize_product(title).family == "guide"

    @pytest.mark.parametrize(
        ("title", "expect_family"),
        [
            # Genre "guide" — must NOT derive the Guide brake family.
            ("2001-2012-dual-air-tuning-guide.pdf", None),
            ("2006-rockshox-front-suspension-dual-air-service-guide.pdf", None),
            ("rockshox-rear-shock-piston-tuning-guide.pdf", None),
            ("sram-avid-disc-brake-pad-identification-guide.pdf", None),
            ("suspension-theory-guide.pdf", None),
            # "guide" is genre here, but boxxer is the real product → boxxer wins.
            ("boxxer-gen-d-fork-installation---quick-start-guide.pdf", "boxxer"),
        ],
    )
    def test_genre_guide_titles_do_not_derive_guide(
        self, title: str, expect_family: str | None
    ) -> None:
        assert normalize_product(title).family == expect_family

    def test_guide_brake_query_still_identifies_guide(self) -> None:
        """The query form \"Guide brake\" must still resolve to family=guide so
        #32's gate treats it as the in-corpus Guide brake (the probe set's
        'Guide brake hydro-phase equalizer' is part-level, not product-level)."""
        scope = extract_query_scope(
            "What torque for the Guide brake's hydro-phase equalizer screw?"
        )
        assert scope.family == "guide"


class TestDeriveFacet:
    """The #28 persistence helper: derive (product_family, brand) to STORE.

    FAIL-SAFE (reviewer-required, precision over recall): a confidently-identified
    product persists a family; anything weaker persists family=None — never a
    guess. A WRONG stored family is worse than NULL (mis-boosts retrieval + trips
    the contamination guard on correct answers)."""

    @pytest.mark.parametrize(
        ("title", "family", "brand"),
        [
            # Clean, product-bearing titles → correct family persisted.
            ("2014-2017-pike-service-manual.pdf", "pike", None),
            ("2021-2022-zeb-service-manual.pdf", "zeb", None),
            ("2012-avid-code-and-code-r-service-manual.pdf", "code", "avid"),
            # The literal "serivce" typo must NOT break clean derivation.
            ("2014-2022-vivid-air-serivce-manual.pdf", "vivid", None),
            ("bb7-user-manual.pdf", "bb7", None),
        ],
    )
    def test_clean_title_persists_correct_family(
        self, title: str, family: str, brand: str | None
    ) -> None:
        assert derive_facet(title) == (family, brand)

    @pytest.mark.parametrize(
        "title",
        [
            # Unrecognizable / hash-only / generic spec sheets → NO family.
            "some-completely-unknown-doc.pdf",
            "pdf.pdf",
            "2024-mtb-frame-fit-specifications.pdf",
            "2015-front-suspension-oil-air-spring-chart.pdf",
            "component-serial-number-locator.pdf",
            # Genre "guide" titles → must NOT persist a guessed family.
            "suspension-theory-guide.pdf",
            "rockshox-rear-shock-piston-tuning-guide.pdf",
        ],
    )
    def test_low_confidence_or_generic_title_persists_null_family(
        self, title: str
    ) -> None:
        family, _brand = derive_facet(title)
        assert family is None

    def test_generic_brand_title_keeps_brand_drops_family(self) -> None:
        """A generic multi-product manual keeps its brand but persists no family
        (so the boost can still brand-match it without a false product claim)."""
        assert derive_facet("2010-sram-technical-manual.pdf") == (None, "sram")

    def test_typo_serivce_does_not_corrupt_family(self) -> None:
        """The known corpus typo 'serivce' (in 2014-2022-vivid-air-serivce-...) is
        ignored — it is not a product token, so the real family (vivid) wins and
        the typo never produces a wrong family."""
        assert derive_facet("2014-2022-vivid-air-serivce-manual.pdf") == ("vivid", None)

    def test_empty_title_is_null(self) -> None:
        assert derive_facet("") == (None, None)
