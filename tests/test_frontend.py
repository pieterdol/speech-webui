"""index.html is one file with no build step, so nothing catches a broken reference before
the phone does. These are the checks that kept finding real breakage by hand.

They're deliberately structural rather than behavioural — there's no DOM here. What they
catch is the failure mode of editing a 2,000-line page: renaming or removing an element and
leaving something pointing at it.
"""
import os
import re
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(HERE, "index.html")

VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "base", "col",
        "embed", "param", "track", "wbr"}


@pytest.fixture(scope="module")
def page():
    with open(PAGE) as f:
        return f.read()


@pytest.fixture(scope="module")
def script(page):
    return "\n".join(re.findall(r"<script>(.*?)</script>", page, re.S))


@pytest.fixture(scope="module")
def markup(page):
    return page[:page.index("<script>")]


def test_every_selector_resolves(script, markup):
    """$("#thing") for an element that isn't there is the refactor bug this page invites."""
    ids = set(re.findall(r'\bid="([^"]+)"', markup))
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', script))
    used |= set(re.findall(r'getElementById\("([^"]+)"\)', script))
    assert sorted(used - ids) == []


def test_no_element_is_left_unreferenced(script, markup):
    """The other direction: markup nobody talks to is usually the leftover half of an edit."""
    ids = set(re.findall(r'\bid="([^"]+)"', markup))
    dead = sorted(i for i in ids if i not in script)
    assert dead == []


def test_tags_balance(page):
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            elif tag in self.stack:
                self.bad.append(f"</{tag}> closes across {self.stack[-1]}")
                while self.stack and self.stack.pop() != tag:
                    pass
            else:
                self.bad.append(f"stray </{tag}>")

    p = P()
    p.feed(page)
    assert p.bad == []
    assert p.stack == []


def test_ids_are_unique(markup):
    ids = re.findall(r'\bid="([^"]+)"', markup)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert dupes == []


# esprima predates ES2020, and it's the fallback when there's no node — so the few later forms
# the page uses are rewritten to their older equivalents before it sees them. Both are syntax
# swaps for the purpose at hand: whether the script hangs together, not what it evaluates to.
LATER_JS = [("??", "||"), ("?.", ".")]


def parse_error(script, tmp_path):
    """None if the script parses, else what the checker said about it.

    node is the real thing and is what CI has. A machine without it — a desktop rather than a
    build box — falls back to esprima, which still finds the stray brace or the unclosed template
    literal this is for, and is the difference between the check running where the page is
    actually edited and not running at all.
    """
    if shutil.which("node"):
        f = tmp_path / "page.js"
        f.write_text(script)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True,
                           timeout=60)
        return None if r.returncode == 0 else r.stderr
    esprima = pytest.importorskip("esprima", reason="neither node nor esprima installed")
    older = script
    for form, plain in LATER_JS:
        older = older.replace(form, plain)
    try:
        esprima.parseScript(older)
        return None
    except esprima.Error as e:                     # its message carries the line number
        return f"esprima (no node installed): {e}"


def test_the_script_parses(script, tmp_path):
    """A syntax error here takes the whole page down, and there's no bundler to catch it."""
    assert parse_error(script, tmp_path) is None


def test_the_parse_check_can_fail(tmp_path):
    """A checker that passes everything is worse than no checker, and there are two of them
    here — so whichever one is in use has to reject a script that doesn't parse."""
    assert parse_error("function f(){ if(true){ return 1; }", tmp_path)


def test_the_player_lives_outside_every_view(page):
    """Switching mode or leaving the reader hides a whole <main>. The player has to keep
    playing and stay reachable through that, which it can't do from inside one."""
    assert page.index('id="miniPlayer"') > page.rindex("</main>")


def test_the_audio_element_is_in_the_player(page):
    """And the <audio> has to be inside it, or hiding a view would still take the sound."""
    player = page.index('id="miniPlayer"')
    assert player < page.index('id="bookAudio"') < page.index("<script>")
