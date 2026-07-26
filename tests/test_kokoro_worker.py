"""Cutting phonemes into pieces the model will read.

The worker runs under Kokoro's own interpreter, but this is string work — the ONNX imports
happen inside main(), so this imports fine in the app's venv and never loads a model.
"""
import kokoro_worker
from kokoro_worker import MAX_PHONEMES, phoneme_batches


class TestPhonemeBatches:
    def test_short_input_is_left_whole(self):
        assert phoneme_batches("hˈɛloʊ ðˈɛɹ") == ["hˈɛloʊ ðˈɛɹ"]

    def test_nothing_comes_back_from_nothing(self):
        assert phoneme_batches("   ") == []

    def test_every_piece_fits_the_model(self):
        """The one that crashed: a page of book titles has no sentence punctuation at all, so
        kokoro-onnx's own splitter handed the model a batch of 510 and it read past the end of
        the voice's style table."""
        run = " ".join(["bˈʊk"] * 400)
        assert len(run) > MAX_PHONEMES
        assert all(len(b) <= MAX_PHONEMES for b in phoneme_batches(run))

    def test_a_run_with_no_boundary_at_all_is_still_cut(self):
        assert all(len(b) <= MAX_PHONEMES for b in phoneme_batches("a" * 1200))

    def test_the_cut_lands_after_punctuation(self):
        """Where a voice would draw breath anyway, rather than mid-phrase."""
        head = "sˈɛntəns wˈʌn." + " ɐ" * 300
        batches = phoneme_batches(head + " sˈɛntəns tˈuː", limit=40)
        assert batches[0] == "sˈɛntəns wˈʌn."

    def test_no_phonemes_are_lost(self):
        run = " ".join(f"wˈɜːd{i}" for i in range(400))
        assert " ".join(phoneme_batches(run)) == run

    def test_the_protocol_stream_is_not_claimed_on_import(self):
        """main() dups stdout aside; doing it at import would redirect the whole process —
        including a test run — the moment the module was loaded."""
        assert kokoro_worker._proto is None
