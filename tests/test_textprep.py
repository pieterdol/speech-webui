"""Turning written text into something worth hearing.

A chat reply arrives as markdown and gets read out loud. Every asterisk and backtick left in
it is a noise the voice makes, so this is about what gets stripped and what survives.
"""
import pytest

import textprep


class TestSpeechText:
    @pytest.mark.parametrize("written,spoken", [
        ("**bold** words", "bold words"),
        ("*italic* words", "italic words"),
        ("`code` words", "code words"),
        ("# A heading", "A heading"),
        ("## Another", "Another"),
        ("- a bullet", "a bullet"),
        ("1. numbered", "numbered"),
    ])
    def test_markup_is_not_read_aloud(self, written, spoken):
        assert textprep.speech_text(written) == spoken

    def test_a_link_keeps_its_words_not_its_url(self):
        out = textprep.speech_text("see [the docs](https://example.com/a/b) for more")
        assert "the docs" in out
        assert "example.com" not in out

    def test_a_fenced_block_does_not_get_read(self):
        out = textprep.speech_text("Here:\n```python\nx = 1\n```\nthat's it")
        assert "```" not in out
        assert "python" not in out

    def test_prose_is_left_alone(self):
        text = "It was a bright cold day in April, and the clocks were striking thirteen."
        assert textprep.speech_text(text) == text

    def test_empty(self):
        assert textprep.speech_text("") == ""


class TestRespell:
    """Neither engine takes a pronunciation override, so hard words go in respelled."""

    def test_replaces_a_known_name(self):
        assert textprep.respell("Hello Pieter") == "Hello Peter"

    def test_is_case_insensitive(self):
        assert "Peter" in textprep.respell("hello pieter")

    def test_only_whole_words(self):
        assert textprep.respell("Pieterse") == "Pieterse"

    def test_leaves_everything_else(self):
        assert textprep.respell("nothing to do here") == "nothing to do here"

    def test_a_word_espeak_gets_wrong(self):
        """espeak clips the -ies and says "movis"."""
        assert textprep.respell("I love watching movies.") == "I love watching movees."


class TestSpokenAbbreviations:
    """"Mr." is read as "mister" and then a sentence break, so the name lands after a pause
    the text never asked for. Writing the word out takes the full stop with it."""

    @pytest.mark.parametrize("written,spoken", [
        ("Mr. Halloway looked up.", "Mister Halloway looked up."),
        ("Mrs. Ashgrove arrived.", "Missus Ashgrove arrived."),
        ("Dr. Evans agreed.", "Doctor Evans agreed."),
        ("Ms. Brown spoke.", "Miz Brown spoke."),
        ("Prof. Hall nodded.", "Professor Hall nodded."),
        ("MR. HALLOWAY", "Mister HALLOWAY"),
    ])
    def test_titles_always_lose_the_full_stop(self, written, spoken):
        """A title sits in front of a name, so its stop is never a sentence end."""
        assert textprep.respell(written) == spoken

    @pytest.mark.parametrize("written,spoken", [
        ("Comparing this vs. that.", "Comparing this versus that."),
        ("Cats, e.g. tabbies, and dogs.", "Cats, for example tabbies, and dogs."),
        ("Dogs, i.e. hounds, bark.", "Dogs, that is hounds, bark."),
        ("It was approx. ten miles.", "It was approximately ten miles."),
        ("Sammy Jr. and Sammy Sr. went.", "Sammy Junior and Sammy Senior went."),
    ])
    def test_mid_sentence_the_stop_comes_off(self, written, spoken):
        assert textprep.respell(written) == spoken

    @pytest.mark.parametrize("written,spoken", [
        ("There was Sammy Jr. Then he left.", "There was Sammy Junior. Then he left."),
        ("He listed them, etc. Then he stopped.",
         "He listed them, et cetera. Then he stopped."),
        ("Cats and dogs, etc.", "Cats and dogs, et cetera."),
        ('He said "etc." Then left.', 'He said "et cetera." Then left.'),
    ])
    def test_at_the_end_of_a_sentence_the_stop_stays(self, written, spoken):
        """Otherwise the sentence runs straight into the next one."""
        assert textprep.respell(written) == spoken

    def test_no_is_a_number_only_before_one(self):
        assert textprep.respell("Take No. 5 first.") == "Take number 5 first."
        assert textprep.respell("No, I don't think so.") == "No, I don't think so."

    @pytest.mark.parametrize("text", [
        "They walked down Main St. to the corner.",   # Saint or Street — unknowable here
        "See fig. 4 and Smith et al.",
        "It took 300 ms to load.",                    # milliseconds, not an honorific
        "Nothing to expand in this one.",
    ])
    def test_left_alone(self, text):
        assert textprep.respell(text) == text

    def test_mrs_is_not_read_as_mr(self):
        """Whichever order the patterns are tried in."""
        assert textprep.respell("Mrs. Smith") == "Missus Smith"
        assert "Misters" not in textprep.respell("Mrs. Smith")

    def test_expansion_happens_after_sentence_splitting(self):
        """cut_sentences must still see "Mr." — it's what stops it cutting there. By the time
        respell runs, the chunk boundaries are already decided."""
        chunks = textprep.cut_sentences("Mr. Halloway looked up. Then he left.", 0)[0]
        assert chunks == ["Mr. Halloway looked up."]
        assert textprep.respell(chunks[0]) == "Mister Halloway looked up."

    def test_every_expandable_abbreviation_is_also_a_known_one(self):
        """An abbreviation this expands but _ABBREV doesn't know would get a sentence cut in
        the middle of it before the expansion ever ran."""
        expandable = set(textprep.HONORIFICS) | set(textprep.SPOKEN_ABBREV)
        assert {a.lower() for a in expandable} <= textprep._ABBREV


class TestCutSentences:
    """Chat speaks a reply while it's still being written, so this has to decide what's a
    finished sentence and what's still arriving."""

    def test_splits_on_sentence_ends(self):
        got, rest = textprep.cut_sentences("One. Two. Three still coming", 0)
        assert got == ["One.", "Two."]
        assert rest.strip() == "Three still coming"

    def test_holds_back_an_unfinished_sentence(self):
        got, rest = textprep.cut_sentences("Still typing", 0)
        assert got == []
        assert rest == "Still typing"

    def test_flush_sends_whatever_is_left(self):
        got, rest = textprep.cut_sentences("Still typing", 0, flush=True)
        assert got == ["Still typing"]
        assert rest == ""

    def test_short_pieces_grow_into_the_next(self):
        """Below about half a second of audio, generating the next chunk takes longer than
        playing this one and the speech develops gaps."""
        got, _rest = textprep.cut_sentences("A. B. C. " + "x" * 200 + ".", 100)
        assert all(len(c) >= 50 for c in got)

    @pytest.mark.parametrize("text,first", [
        ("Ask Dr. Jones about it. Then go.", "Ask Dr. Jones about it."),
        ("See fig. 4 for this. Then go.", "See fig. 4 for this."),
        ("Smith et al. said so. Then go.", "Smith et al. said so."),
        ("Take No. 5 first. Then go.", "Take No. 5 first."),
        ("J. Smith wrote it. Then go.", "J. Smith wrote it."),   # a lone initial
    ])
    def test_an_abbreviation_is_not_a_sentence_end(self, text, first):
        got, _rest = textprep.cut_sentences(text, 0)
        assert got[0] == first

    @pytest.mark.parametrize("text", [
        "She said 'yes.' Then left.",
        "A quote ends here.” Then more.",
        "In brackets (like this.) Then more.",
    ])
    def test_closing_punctuation_belongs_to_the_sentence(self, text):
        """The bracket or quote closes this sentence; it isn't the start of the next."""
        got, rest = textprep.cut_sentences(text, 0)
        assert got and got[0][-1] in "'”)"
        assert rest.startswith("Then")

    def test_an_open_code_fence_waits(self):
        """Half a fence read out loud is worse than a pause."""
        got, rest = textprep.cut_sentences("Here it is:\n```python\nx = 1", 0)
        assert got == []
        assert "```" in rest

    def test_a_closed_fence_can_go(self):
        got, _rest = textprep.cut_sentences("Here:\n```\nx = 1\n```\nDone.", 0, flush=True)
        assert got

    def test_nothing_is_dropped(self):
        text = " ".join(f"Sentence {i}." for i in range(30))
        got, rest = textprep.cut_sentences(text, 0, flush=True)
        assert " ".join(got).split() == text.split()
        assert rest == ""
