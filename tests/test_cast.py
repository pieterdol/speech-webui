"""Who speaks which line, and which voice reads it.

The model's half of this can't be asserted — whether it knows that "Then light a candle" is
Marla's is exactly what the pass is for. What is testable is everything around it: the runs code
finds, the answers code settles on its own, and the casting. So the model is replaced by a
canned answer everywhere here, and the tests are about what happens to that answer.
"""
import json

import cast

# Written for the shapes, not the story: a speech split by its tag, a tagged line, an
# unattributed reply, and a paragraph of narration with no speech in it at all.
CHAPTER = """The kitchen was cold enough to see their breath.
“Put the kettle on,” said Marla, “before the pipes freeze.”
Owen looked up from the table. “There is no gas.”
“Then light a candle.”
He counted the coins in his pocket and said nothing at all.
"""


def answers(*speakers):
    """A model reply: one entry per run, in order."""
    return [{"n": i, "speaker": s, "gender": g}
            for i, (s, g) in enumerate(speakers, 1)]


def canned(*speakers):
    reply = answers(*speakers)
    return lambda window, **kw: reply


class TestFindingTheQuotedRuns:
    def test_every_run_in_order(self):
        spans = cast.quote_spans(CHAPTER)
        assert [CHAPTER[a:b] for a, b in spans] == [
            "“Put the kettle on,”", "“before the pipes freeze.”", "“There is no gas.”",
            "“Then light a candle.”"]

    def test_straight_quotes_are_read_too(self):
        """A few of these EPUBs never got typeset with curly marks."""
        text = 'She shrugged. "Suit yourself."'
        assert [text[a:b] for a, b in cast.quote_spans(text)] == ['"Suit yourself."']

    def test_a_run_with_no_closing_mark_stops_at_the_line(self):
        """Speech carried across a paragraph break opens on every paragraph and closes only at
        the end, so a run that waited for the closing mark would swallow the narration between
        them — and read it in the speaker's voice."""
        text = "“It started in the spring.\nThe rain came later.\n“And then it stopped.”"
        assert [text[a:b] for a, b in cast.quote_spans(text)] == [
            "“It started in the spring.", "“And then it stopped.”"]

    def test_no_quotes_is_no_runs(self):
        assert cast.quote_spans("He said nothing for a while.") == []

    def test_marking_numbers_them_for_the_model(self):
        marked = cast.marked(CHAPTER, cast.quote_spans(CHAPTER))
        assert "[1]“Put the kettle on,”" in marked
        assert "[4]“Then light a candle.”" in marked
        assert marked.replace("[1]", "").replace("[2]", "").replace(
            "[3]", "").replace("[4]", "") == CHAPTER


class TestWhatTheChapterSaysOutright:
    def test_a_tag_after_the_run_names_the_speaker(self):
        spans = cast.quote_spans(CHAPTER)
        assert cast.tagged_speaker(CHAPTER, spans[0], ["Marla", "Owen"]) == "Marla"

    def test_the_name_can_come_before_the_verb(self):
        """Modern prose writes "Daniela says" where Austen writes "said Marla"."""
        text = "“The pipes are frozen,” Marla said."
        assert cast.tagged_speaker(text, cast.quote_spans(text)[0], ["Marla"]) == "Marla"

    def test_a_surname_stands_for_the_full_name(self):
        text = "“Later,” said Vance."
        assert cast.tagged_speaker(text, cast.quote_spans(text)[0],
                                  ["Leighton Vance"]) == "Leighton Vance"

    def test_someone_the_model_never_found_is_not_a_speaker(self):
        """"said the man in the mask" would otherwise cast a character called "The"."""
        text = "“Later,” said the man in the mask."
        assert cast.tagged_speaker(text, cast.quote_spans(text)[0], ["Owen"]) is None

    def test_a_name_further_along_the_sentence_is_not_the_speaker(self):
        """The person being spoken about reads as the speaker if the tag isn't anchored."""
        text = "“Later,” he told Marla firmly."
        assert cast.tagged_speaker(text, cast.quote_spans(text)[0], ["Marla"]) is None


class TestGenderTheChapterStates:
    """The model reads the question as "is this person identified" and answers unknown for anyone
    in a mask — which costs them a voice. A pronoun in the tag settles it in code."""

    def test_a_pronoun_in_the_tag_settles_it(self):
        text = "“Turn around,” he says."
        assert cast.tagged_gender(text, cast.quote_spans(text)[0]) == "male"

    def test_either_order(self):
        text = "“Turn around,” said she."
        assert cast.tagged_gender(text, cast.quote_spans(text)[0]) == "female"

    def test_no_pronoun_is_no_answer(self):
        text = "“Turn around,” said the man in the mask."
        assert cast.tagged_gender(text, cast.quote_spans(text)[0]) is None

    def test_it_overrides_what_the_model_answered(self):
        got = cast.attribute("“Turn around,” he says.\n",
                             ask_fn=canned(("Masked man", "unknown")))
        assert got["speakers"][0]["gender"] == "male"


class TestOneUtteranceInTwoRuns:
    def test_a_tag_holding_a_speech_open_groups_them(self):
        spans = cast.quote_spans(CHAPTER)
        assert (0, 1) in cast.utterance_groups(CHAPTER, spans)

    def test_a_sentence_ending_between_them_keeps_them_apart(self):
        """Two utterances on one line can be two people, and often are."""
        text = "“Yes,” said Owen. “No,” said Marla."
        assert cast.utterance_groups(text, cast.quote_spans(text)) == [(0,), (1,)]

    def test_a_line_break_keeps_them_apart(self):
        text = "“Yes,”\n“No,”"
        assert cast.utterance_groups(text, cast.quote_spans(text)) == [(0,), (1,)]


class TestTheNamesTheModelGivesBack:
    def test_stray_punctuation_comes_off(self):
        assert cast.clean_name("  Mr. Bingley,  ") == "Mr. Bingley"

    def test_a_first_name_folds_into_the_full_one(self):
        """One person answered two ways would otherwise be cast twice, in two voices, in one
        scene."""
        keep = cast.merge_names(["Leighton", "Leighton Vance"])
        assert keep["Leighton"] == "Leighton Vance"

    def test_a_name_two_people_share_is_left_alone(self):
        keep = cast.merge_names(["Bennet", "Mr. Bennet", "Mrs. Bennet"])
        assert keep["Bennet"] == "Bennet"


class TestTheCast:
    def test_in_the_order_they_first_speak(self):
        lines = answers(("Marla", "female"), ("Owen", "male"), ("Marla", "female"))
        assert [s["name"] for s in cast.speakers_in(lines)] == ["Marla", "Owen"]

    def test_counts_the_lines_each_of_them_gets(self):
        lines = answers(("Marla", "female"), ("Owen", "male"), ("Marla", "female"))
        assert [s["lines"] for s in cast.speakers_in(lines)] == [2, 1]

    def test_gender_is_what_most_of_their_lines_say(self):
        """One line answered "unknown" shouldn't cost a character their voice."""
        lines = answers(("Owen", "male"), ("Owen", "unknown"), ("Owen", "male"))
        assert cast.speakers_in(lines)[0]["gender"] == "male"

    def test_captions_and_gaps_are_not_people(self):
        lines = answers(("none", "unknown"), ("unknown", "unknown"), ("Marla", "female"))
        assert [s["name"] for s in cast.speakers_in(lines)] == ["Marla"]


class TestCollatingTheAnswers:
    def test_a_run_the_model_skipped_is_unknown(self):
        """A model can stop early on a long list. The narrator reads what it missed — a gap has
        to be visible, not filled with whoever spoke last."""
        lines = cast.collate([{"n": 1, "speaker": "Marla", "gender": "female"}], 3)
        assert [l["speaker"] for l in lines] == ["Marla", cast.UNKNOWN, cast.UNKNOWN]

    def test_a_run_answered_twice_counts_once(self):
        lines = cast.collate([{"n": 1, "speaker": "Marla", "gender": "female"},
                              {"n": 1, "speaker": "Owen", "gender": "male"}], 1)
        assert [l["speaker"] for l in lines] == ["Marla"]


class TestAskingAboutALongChapter:
    def test_windows_cut_on_line_boundaries(self):
        text = "".join(f"line {i} of the chapter\n" for i in range(200))
        cut = cast.windows(text, [], limit=500)
        assert len(cut) > 1
        assert all(w.endswith("\n") for w, _b, _h in cut[:-1])
        assert "".join(w for w, _b, _h in cut) == text

    def test_every_run_lands_in_exactly_one_window(self):
        text = CHAPTER * 20
        spans = cast.quote_spans(text)
        cut = cast.windows(text, spans, limit=400)
        assert sum(h for _w, _b, h in cut) == len(spans)
        assert [b for _w, b, _h in cut] == [0] + [sum(h for _w, _b, h in cut[:i + 1])
                                                  for i in range(len(cut) - 1)]

    def test_it_does_not_ask_the_model_to_think_about_it(self):
        """qwen3 reasons out loud unless told not to. Here that cost a window of a hundred runs
        33,000 tokens of think block and no answer at all — and with the answer capped, an empty
        one. Attribution is a judgement per marker, not a problem to work through."""
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode()))
            raise RuntimeError("the request is what this is about")

        import unittest.mock as mock
        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch("cast.model_thinks", lambda m: True):
            try:
                cast.ask("[1]“One.”", runs=1)
            except RuntimeError:
                pass
        assert sent["think"] is False

    def test_a_model_that_rejects_the_flag_is_not_sent_it(self):
        """qwen2.5-coder refuses the request outright rather than ignoring the flag."""
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode()))
            raise RuntimeError("the request is what this is about")

        import unittest.mock as mock
        with mock.patch("urllib.request.urlopen", fake_urlopen), \
             mock.patch("cast.model_thinks", lambda m: False):
            try:
                cast.ask("[1]“One.”", runs=1)
            except RuntimeError:
                pass
        assert "think" not in sent

    def test_the_answer_is_bounded_by_how_many_runs_there_are(self):
        """Asked for a JSON array a model can carry on emitting entries for ever, and one did:
        33,000 tokens into a chapter of 130 runs, still going. What comes back short is handled —
        the runs it didn't cover are unknown, and the narrator reads them."""
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data.decode()))
            raise RuntimeError("stop here — the request is what this is about")

        import unittest.mock as mock
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            try:
                cast.ask("[1]“One.” [2]“Two.”", runs=2)
            except RuntimeError:
                pass
        assert sent["options"]["num_predict"] == cast.CAST_TOKENS_LEAST      # the floor, for two
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            try:
                cast.ask("…", runs=200)
            except RuntimeError:
                pass
        assert sent["options"]["num_predict"] == 40 * 200

    def test_the_model_is_asked_about_marked_text(self):
        """Without the markers there is nothing for it to answer about, and it doesn't say so —
        it spends minutes writing something else."""
        seen = []

        def ask(window, **kw):
            seen.append(window)
            return answers(("Marla", "female"))

        cast.attribute("“Now.”\n", ask_fn=ask)
        assert seen == ["[1]“Now.”\n"]

    def test_run_numbers_come_back_chapter_wide(self):
        """Each window is asked about on its own and numbers its runs from one, so a chapter of
        three windows would otherwise have three run number ones."""
        text = CHAPTER * 3
        got = cast.attribute(text, ask_fn=lambda window, **kw: [
            {"n": i + 1, "speaker": "Marla", "gender": "female"}
            for i in range(len(cast.quote_spans(window)))])
        assert [l["n"] for l in got["lines"]] == list(range(1, len(got["lines"]) + 1))


class TestAttributingAChapter:
    def test_what_the_chapter_names_beats_what_the_model_guessed(self):
        got = cast.attribute(CHAPTER, ask_fn=canned(
            ("Owen", "male"), ("Owen", "male"), ("Owen", "male"), ("Marla", "female")))
        assert got["lines"][0]["speaker"] == "Marla"        # "said Marla" is not a guess
        assert got["lines"][0]["how"] == "tag"
        assert got["tagged"] == 1

    def test_a_split_speech_comes_out_in_one_voice(self):
        """Both halves of "…,” said Marla, “…" are her, whatever the model said about the
        second."""
        got = cast.attribute(CHAPTER, ask_fn=canned(
            ("Marla", "female"), ("Owen", "male"), ("Owen", "male"), ("Marla", "female")))
        assert [l["speaker"] for l in got["lines"][:2]] == ["Marla", "Marla"]
        assert got["lines"][1]["how"] == "split"

    def test_the_cast_and_the_count_come_with_it(self):
        got = cast.attribute(CHAPTER, ask_fn=canned(
            ("Marla", "female"), ("Marla", "female"), ("Owen", "male"), ("Marla", "female")))
        assert got["quotes"] == 4
        assert {s["name"] for s in got["speakers"]} == {"Marla", "Owen"}


class TestCastingTheVoices:
    ROSTER = ["af_bella", "af_nova", "am_adam", "am_eric", "bf_emma", "bm_george", "af_heart"]

    def test_the_narrators_own_accent_and_never_their_own_voice(self):
        pool = cast.voice_pool("af_heart", self.ROSTER)
        assert pool == {"female": ["af_bella", "af_nova"], "male": ["am_adam", "am_eric"]}

    def test_a_piper_voice_has_no_pool(self):
        """Dutch has one or two voices installed at all, so a Dutch book stays single-voiced.
        Two characters sharing a voice is worse than a narrator reading both."""
        assert cast.voice_pool("nl_NL-ronnie-medium", self.ROSTER) == {"female": [], "male": []}

    def test_a_voice_each_matching_gender(self):
        speakers = [{"name": "Marla", "gender": "female", "lines": 3},
                    {"name": "Owen", "gender": "male", "lines": 2}]
        voices = cast.assign_voices(speakers, "af_heart", self.ROSTER)
        assert voices["Marla"].startswith("af_")
        assert voices["Owen"].startswith("am_")

    def test_nobody_shares_a_voice(self):
        speakers = [{"name": n, "gender": "female", "lines": 3} for n in ("A", "B", "C")]
        voices = cast.assign_voices(speakers, "af_heart", self.ROSTER)
        assert len(set(voices.values())) == len(voices)

    def test_the_ones_who_speak_most_are_cast_first(self):
        """When the voices run out it's the passers-by that go without."""
        speakers = [{"name": "Passer", "gender": "female", "lines": 1},
                    {"name": "Lead", "gender": "female", "lines": 90},
                    {"name": "Second", "gender": "female", "lines": 40}]
        voices = cast.assign_voices(speakers, "af_heart", ["af_bella", "af_heart"])
        assert voices == {"Lead": "af_bella"}

    def test_someone_already_cast_keeps_their_voice(self):
        """Attributing chapter two must not re-cast the people you heard in chapter one."""
        speakers = [{"name": "Marla", "gender": "female", "lines": 3},
                    {"name": "Owen", "gender": "male", "lines": 2}]
        voices = cast.assign_voices(speakers, "af_heart", self.ROSTER, {"Marla": "af_nova"})
        assert voices["Marla"] == "af_nova"

    def test_a_gender_the_chapter_never_showed_gets_the_narrator(self):
        """Guessing is wrong half the time, and a man read in a woman's voice is worse than the
        narrator reading his line."""
        speakers = [{"name": "A voice on the radio", "gender": "unknown", "lines": 2}]
        assert cast.assign_voices(speakers, "af_heart", self.ROSTER) == {}


class TestReadingItBackOut:
    def test_narration_and_speech_alternate(self):
        lines = cast.attribute(CHAPTER, ask_fn=canned(
            ("Marla", "female"), ("Marla", "female"), ("Owen", "male"),
            ("Marla", "female")))["lines"]
        runs, at = cast.voiced_runs(CHAPTER, lines, 0, "af_heart",
                                    {"Marla": "af_bella", "Owen": "am_adam"})
        assert at == 4
        assert [v for _t, v in runs] == ["af_heart", "af_bella", "af_heart", "af_bella",
                                         "af_heart", "am_adam", "af_bella", "af_heart"]
        # every word, in order: the whitespace between two runs goes, since each run is a TTS
        # call of its own and a call saying only a newline is silence with a load time
        assert " ".join(t.strip() for t, _v in runs).split() == CHAPTER.split()

    def test_a_speaker_without_a_voice_is_read_by_the_narrator(self):
        lines = cast.collate(answers(("Marla", "female"), ("Marla", "female"),
                                     ("Owen", "male"), ("unknown", "unknown")), 4)
        runs, _at = cast.voiced_runs(CHAPTER, lines, 0, "af_heart", {"Marla": "af_bella"})
        assert set(v for _t, v in runs) == {"af_heart", "af_bella"}

    def test_a_segment_carries_on_where_the_last_one_stopped(self):
        """A chapter is rendered a segment at a time and the attribution is one list for all of
        it."""
        lines = cast.collate(answers(("Marla", "female"), ("Owen", "male")), 2)
        one = "“First.”\n"
        two = "“Second.”\n"
        runs_one, at = cast.voiced_runs(one, lines, 0, "af_heart",
                                        {"Marla": "af_bella", "Owen": "am_adam"})
        runs_two, at = cast.voiced_runs(two, lines, at, "af_heart",
                                        {"Marla": "af_bella", "Owen": "am_adam"})
        assert [v for _t, v in runs_one] == ["af_bella"]
        assert [v for _t, v in runs_two] == ["am_adam"]
        assert at == 2

    def test_running_past_the_attribution_raises(self):
        """An attribution made before the chapter was re-scanned would otherwise shift every
        voice after the change by one, which sounds like a broken cast rather than a stale
        file."""
        lines = cast.collate(answers(("Marla", "female")), 1)
        try:
            cast.voiced_runs(CHAPTER, lines, 0, "af_heart", {"Marla": "af_bella"})
        except ValueError:
            return
        raise AssertionError("it read past the end of the attribution without complaining")


class TestARepliedThatRanOffTheEnd:
    """A model asked for a JSON array can carry on emitting entries instead of finishing one. The
    token cap turns that into a truncated reply, and a truncated array is not a document json.loads
    will look at — so the answers are read out of the text, and a window that came back mostly
    unanswered is asked again in halves."""

    def test_entries_before_the_cut_are_kept(self):
        cut_off = ('{"quotes": [{"n": 1, "speaker": "Marla", "gender": "female"}, '
                   '{"n": 2, "speaker": "Owen", "gender": "male"}, {"n": 3, "speak')
        assert [a["n"] for a in cast.answers_in(cut_off)] == [1, 2]

    def test_a_whole_reply_reads_the_same_way(self):
        whole = '{"quotes": [{"n": 1, "speaker": "Marla", "gender": "female"}]}'
        assert cast.answers_in(whole) == [{"n": 1, "speaker": "Marla", "gender": "female"}]

    def test_nothing_at_all_is_no_answers_rather_than_a_crash(self):
        """What a reply cut off before its first entry looks like."""
        assert cast.answers_in("") == [] and cast.answers_in(None) == []

    def test_a_window_that_came_back_short_is_asked_again_in_halves(self):
        """And the halves are numbered so their answers still land on the right runs."""
        long_chapter = CHAPTER * 300                  # two windows, plenty of runs
        asked = []

        def loops_on_a_big_window(window, **kw):
            # On run count, not on length: what the model is given is the *marked* text, which is
            # longer than the window attribute measures when it decides to split.
            runs = len(cast.quote_spans(window))
            asked.append(runs)
            # 120 runs is about what a window is down to once attribute stops halving it
            answered = runs if runs <= 120 else max(1, runs // 8)   # ran off the end on a big one
            return [{"n": i + 1, "speaker": "Marla", "gender": "female"} for i in range(answered)]

        got = cast.attribute(long_chapter, ask_fn=loops_on_a_big_window)
        assert len(asked) > len(cast.windows(long_chapter, cast.quote_spans(long_chapter)))
        assert min(asked) <= 120                      # it did split, down to windows it answers
        assert [l["n"] for l in got["lines"]] == list(range(1, got["quotes"] + 1))
        # every run the halves answered is attributed, none of them shifted
        assert all(l["speaker"] == "Marla" for l in got["lines"])

    def test_it_stops_splitting_rather_than_going_forever(self):
        """A window nothing can be got out of is accepted as it is: the runs it didn't cover are
        unknown, which the narrator reads."""
        asked = []

        def answers_nothing(window, **kw):
            asked.append(len(window))
            return []

        got = cast.attribute(CHAPTER * 300, ask_fn=answers_nothing)
        assert len(asked) < 100                       # bounded, not recursing on every half
        assert all(l["speaker"] == cast.UNKNOWN for l in got["lines"])
