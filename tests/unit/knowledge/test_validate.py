"""The citation validator: a rationale may cite only what retrieval returned.

Pure, exhaustive, offline. The invented-citation rejection is the step-8 exit
condition the whole explanation layer leans on: the model's prose cannot add
a source the pipeline never saw.
"""

from __future__ import annotations

from candlegraph.knowledge.validate import citation_ids, validate_rationale


def test_markers_are_parsed_in_every_form_a_model_writes() -> None:
    assert citation_ids("A [1]. B [2][3]. C [4, 5] and [6,7].") == [1, 2, 3, 4, 5, 6, 7]
    assert citation_ids("no markers here") == []


def test_a_clean_rationale_passes_through_unchanged() -> None:
    text = "WETH rose 6% in three hours [1]. Two documents discuss an unlock [1][2]."

    result = validate_rationale(text, [1, 2])

    assert result.clean
    assert result.rationale == text
    assert result.cited == (1, 2)
    assert result.rejected == () and result.stripped == ()


def test_a_sentence_citing_an_id_outside_the_retrieved_set_is_stripped_and_flagged() -> None:
    """The exit criterion: an invented citation is rejected, not kept, not repaired."""
    text = "The price moved 6% [1]. An exchange hack was reported [3]. WBTC was flat [2]."

    result = validate_rationale(text, [1, 2])

    assert not result.clean
    assert result.rationale == "The price moved 6% [1]. WBTC was flat [2]."
    assert result.rejected == (3,)
    assert result.stripped == ("An exchange hack was reported [3].",)
    assert result.cited == (1, 2)


def test_a_sentence_mixing_valid_and_invalid_ids_goes_as_a_whole() -> None:
    """Rewriting the sentence to keep [1] would be the validator inventing prose."""
    result = validate_rationale("Both say so [1][9].", [1])

    assert result.rationale == ""
    assert result.rejected == (9,)
    assert result.cited == (), "the valid id in a stripped sentence is not counted as cited"


def test_zero_and_an_unretrieved_number_are_both_rejected() -> None:
    result = validate_rationale("See [0]. See [2].", [1])

    assert result.rejected == (0, 2)
    assert result.rationale == ""


def test_uncited_prose_is_kept() -> None:
    """Prose without markers is fine: the numbers it reads are in the context."""
    result = validate_rationale("The move was idiosyncratic. Volume was thin.", [1])

    assert result.clean
    assert result.rationale == "The move was idiosyncratic. Volume was thin."
    assert result.cited == ()


def test_an_empty_rationale_is_clean_and_empty() -> None:
    result = validate_rationale("", [1, 2])

    assert result.clean and result.rationale == "" and result.cited == ()


def test_paragraph_breaks_collapse_to_one_paragraph_and_each_sentence_is_judged_alone() -> None:
    text = "First [1].\n\nSecond [7].\nThird."

    result = validate_rationale(text, [1])

    assert result.rationale == "First [1]. Third."
    assert result.stripped == ("Second [7].",)


def test_duplicate_rejections_are_reported_once_in_order_of_appearance() -> None:
    result = validate_rationale("A [5]. B [4]. C [5].", [1])

    assert result.rejected == (5, 4)
    assert len(result.stripped) == 3


def test_a_sentence_ending_in_a_quote_or_bracket_is_split_correctly() -> None:
    text = 'He said "sell." [1] Then it fell [2].'

    result = validate_rationale(text, [1])

    assert result.stripped == ("Then it fell [2].",)
    assert result.rationale == 'He said "sell." [1]'
