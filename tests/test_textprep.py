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
    """Neither engine has a pronunciation-override syntax, so hard words go in respelled."""

    def test_replaces_a_known_name(self):
        assert textprep.respell("Hello Pieter") == "Hello Peter"

    def test_is_case_insensitive(self):
        assert "Peter" in textprep.respell("hello pieter")

    def test_only_whole_words(self):
        assert textprep.respell("Pieterse") == "Pieterse"

    def test_leaves_everything_else(self):
        assert textprep.respell("nothing to do here") == "nothing to do here"


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
