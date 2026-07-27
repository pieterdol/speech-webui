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


class TestABooksOwnRespellings:
    """The names in a novel are nobody else's problem, so a book carries its own map on top of
    the global one. Everything outside book narration passes no map at all, which has to mean
    exactly what it meant before."""

    def test_a_books_word_is_respelled_too(self):
        assert textprep.respell("about Vermeer", {"Vermeer": "Vermayr"}) == "about Vermayr"

    def test_the_global_map_still_applies(self):
        assert textprep.respell("Pieter likes Vermeer", {"Vermeer": "Vermayr"}) \
            == "Peter likes Vermayr"

    def test_a_book_wins_where_both_name_the_word(self):
        assert textprep.respell("Pieter", {"Pieter": "Peeter"}) == "Peeter"

    def test_no_map_is_the_global_map(self):
        assert textprep.respell("Hello Pieter", None) == textprep.respell("Hello Pieter")

    def test_case_insensitive_and_whole_words_like_the_rest(self):
        m = {"Vermeer": "Vermayr"}
        assert textprep.respell("vermeer", m) == "Vermayr"
        assert textprep.respell("Vermeerkade", m) == "Vermeerkade"

    def test_a_replacement_is_never_read_as_a_backreference(self):
        """A reader types what they hear. re.sub's template syntax would make "\\1" a group
        reference and raise re.error deep inside a render thread."""
        assert textprep.respell("play AC", {"AC": r"\1"}) == r"play \1"
        assert textprep.respell("play AC", {"AC": "AC\\DC"}) == "play AC\\DC"

    def test_an_empty_replacement_takes_the_word_out(self):
        """For a footnote marker or a symbol that shouldn't be spoken at all."""
        assert textprep.respell("said 1 there", {"1": ""}).split() == ["said", "there"]

    def test_it_fires_on_what_an_earlier_rule_produced(self):
        """Deterministic and worth knowing: the maps are merged, so a book key that matches a
        global rule's output still applies to it."""
        assert textprep.respell("movies", {"movees": "MOOVEES"}) == "MOOVEES"


class TestCleaningABooksMap:
    """It's typed in on a phone, and every entry is a regex pass over every chunk of every
    render."""

    def test_whitespace_comes_off(self):
        assert textprep.clean_respell({"  Vermeer  ": " Vermayr "}) == {"Vermeer": "Vermayr"}

    def test_an_empty_key_is_dropped(self):
        assert textprep.clean_respell({"": "x", "   ": "y"}) == {}

    def test_an_empty_replacement_is_kept(self):
        """It means "don't say this", which is a real thing to want."""
        assert textprep.clean_respell({"†": ""}) == {"†": ""}

    def test_the_same_word_twice_is_one_rule(self):
        """The match ignores case, so two casings would be two rules on the same word — the
        second one running over the first one's output."""
        assert list(textprep.clean_respell({"Vermeer": "a", "vermeer": "b"})) == ["Vermeer"]

    def test_junk_is_ignored_rather_than_fatal(self):
        assert textprep.clean_respell({"ok": "fine", 3: "x", "y": None}) == {"ok": "fine"}
        assert textprep.clean_respell("not a map") == {}
        assert textprep.clean_respell(None) == {}

    def test_both_sides_are_capped(self):
        long = "x" * (textprep.RESPELL_CHARS + 40)
        got = textprep.clean_respell({long: long})
        assert list(got) == [long[:textprep.RESPELL_CHARS]]
        assert got[long[:textprep.RESPELL_CHARS]] == long[:textprep.RESPELL_CHARS]

    def test_and_so_is_the_count(self):
        many = {f"word{i}": "x" for i in range(textprep.RESPELL_MAX + 50)}
        assert len(textprep.clean_respell(many)) == textprep.RESPELL_MAX


class TestWhatChangedInAMap:
    """Which entries moved decides whether there's any audio to re-make at all."""

    def test_added_edited_and_removed(self):
        old = {"a": "1", "b": "2", "c": "3"}
        new = {"a": "1", "b": "changed", "d": "4"}
        assert textprep.respell_diff(old, new) == (["d"], ["b"], ["c"])

    def test_recasing_a_key_is_not_a_change(self):
        """The match ignores case, so it's the same rule and the same audio."""
        assert textprep.respell_diff({"Vermeer": "x"}, {"vermeer": "x"}) == ([], [], [])

    def test_reordering_is_not_a_change(self):
        assert textprep.respell_diff({"a": "1", "b": "2"}, {"b": "2", "a": "1"}) == ([], [], [])

    def test_identical_maps_and_empty_ones(self):
        assert textprep.respell_diff({"a": "1"}, {"a": "1"}) == ([], [], [])
        assert textprep.respell_diff(None, None) == ([], [], [])
        assert textprep.respell_diff({}, {"a": "1"}) == (["a"], [], [])


class TestSpokenAbbreviations:
    """"Mr." is read as "mister" and then a sentence break, so the name lands after a pause
    the text never asked for. Writing the word out takes the full stop with it."""

    @pytest.mark.parametrize("written,spoken", [
        ("Mr. Halloway looked up.", "Mister Halloway looked up."),
        ("Mrs. Ashgrove arrived.", "Missus Ashgrove arrived."),
        ("Dr. Everly agreed.", "Doctor Everly agreed."),
        ("Ms. Brand spoke.", "Miz Brand spoke."),
        ("Prof. Linden nodded.", "Professor Linden nodded."),
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

    @pytest.mark.parametrize("text", [
        "my contacts in the DR, and elsewhere",                 # the Dominican Republic
        "like some MS-13 lookout",                              # a gang
        "the intersection of SR 92 and the old highway",         # a state route
        "where the road reverted to SR 92 again",
        "a phone booth on SR 109, half a mile along",
    ])
    def test_an_all_caps_initialism_is_not_a_title(self, text):
        """Every one of these forms turned up in a real book and every one was being expanded
        — "the Doctor", "Miz-13", "Senior 92" — until the all-caps spelling was made to carry
        its full stop. Two capitals without one is an initialism far more often than a
        title."""
        assert textprep.respell(text) == text

    @pytest.mark.parametrize("text,spoken", [
        ("and Mrs Ashgrove steering", "and Missus Ashgrove steering"),
        ("the old Spy vs Spy cartoons", "the old Spy versus Spy cartoons"),
    ])
    def test_but_the_stop_is_not_required_otherwise(self, text, spoken):
        """Both forms also occur in real books. British style drops the stop after a title,
        and "vs" between two nouns wants reading out in full."""
        assert textprep.respell(text) == spoken

    def test_an_all_caps_heading_still_expands(self):
        """Because it does carry the stop."""
        assert textprep.respell("MR. HALLOWAY") == "Mister HALLOWAY"

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


class TestNumberWords:
    """Only words-to-digits is needed: a chapter heading can spell its number out, but digits
    handed to an engine are read in whatever language it speaks."""

    def test_the_reverse_lookup_can_be_built_from_the_tables(self):
        """books.py reads "Chapter Twenty-One", and builds that from these."""
        assert textprep.ONES[11] == "eleven" and textprep.TENS[6] == "sixty"


class TestSlashNumbers:
    """A slash between numbers is a writing convention, not a sound: "11/22/63" read as written
    comes out "eleven slash twenty-two slash sixty-three". Only the slash is dealt with here —
    the digits go to the engine, which reads them in its own language."""

    @pytest.mark.parametrize("written,spoken", [
        ("11/22/63", "11, 22, 63"),
        ("11/22/63: A Novel", "11, 22, 63: A Novel"),
        ("10/7/58", "10, 7, 58"),
        ("9/30/1958", "9, 30, 1958"),
        ("9/11", "9, 11"),
        ("20/20", "20, 20"),
        ("24/7", "24, 7"),
        ("The card was dated 11/22/63, in pencil.",
         "The card was dated 11, 22, 63, in pencil."),
        ("11/21/63 and 11/22/63", "11, 21, 63 and 11, 22, 63"),
    ])
    def test_the_slash_becomes_the_beat_between_the_groups(self, written, spoken):
        assert textprep.respell(written) == spoken

    @pytest.mark.parametrize("written,spoken", [
        ("10/02/1986", "10, 2, 1986"),
        ("01/02/03", "1, 2, 3"),
        ("09/11", "9, 11"),
    ])
    def test_a_leading_zero_comes_off(self, written, spoken):
        """It's the one thing an engine gets wrong by itself: "02" is read "zero two"."""
        assert textprep.respell(written) == spoken

    @pytest.mark.parametrize("text", [
        "he ate 1/2 of it",                 # a fraction, not a date — "one two" is worse
        "a 3/4 majority",
        "and/or",
        "Jake/George",
        "see example.com/5/8 for that",     # a path
        "version 1.2/3 of it",
    ])
    def test_left_alone(self, text):
        assert textprep.respell(text) == text

    @pytest.mark.parametrize("written,spoken", [
        ("10-02-1986", "10, 2, 1986"),          # day first, as written here
        ("1986-02-10", "1986, 2, 10"),          # and ISO
        ("2/10/1986", "2, 10, 1986"),
    ])
    def test_a_hyphen_carries_a_date_too(self, written, spoken):
        assert textprep.respell(written) == spoken

    @pytest.mark.parametrize("text", [
        "the 1914-1918 war",              # a range, not a date
        "see pages 10-20 for that",
        "it took 10-15 minutes",
        "the 2020-21 season",
        "like some MS-13 lookout",
        "ISBN 978-0-7432-7356-5",
        "a 3-2-1 countdown",              # three groups, but no year in it
    ])
    def test_a_hyphen_between_numbers_is_usually_a_range(self, text):
        """Which is why only the two forms carrying a four-digit year are read as a date."""
        assert textprep.respell(text) == text

    def test_the_digits_are_left_for_the_engine(self):
        """Spelling them out here would mean spelling them out in one language: espeak says
        "elf, tweeentwintig" for a Dutch voice and "eleven, twenty-two" for an English one, and
        reads a four-digit group as a year in both."""
        assert textprep.respell("1/2/1986") == "1, 2, 1986"


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
