"""Phase 4 acceptance gate: the browser client, checked without a browser.

There is no DOM here, so these tests pin the things that actually break a client
built in parallel with its server: drift from the wire contract, an accidental
dependency on the network, a host panel that starts disclosing, and the one piece
of timing the whole fairness design rests on — a buzz press that paints before it
awaits anything.

The reducer itself is pure, so it is exercised for real: node replays the recorded
stream through `state.js` and the resulting state is asserted here.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jeopardy import protocol as P  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")

JS_FILES = ["app.js", "board.js", "buzzer.js", "clock.js", "host.js", "state.js",
            "demo.js", os.path.join("fixtures", "demo_stream.js")]
REQUIRED = ["index.html", "style.css", "demo.html"] + JS_FILES

EVENT_NAMES = {v for k, v in vars(P).items() if k.startswith("EVT_")}
COMMAND_NAMES = set(P.COMMAND_SCHEMA)

NODE = shutil.which("node")


def read(*parts):
    with open(os.path.join(STATIC, *parts), encoding="utf-8") as fh:
        return fh.read()


def code_only(src: str) -> str:
    """Source with comments stripped, for assertions about behavior rather than prose."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def all_js():
    return {name: read(name) for name in JS_FILES}


def top_level_keys(literal: str) -> set:
    """Keys of a flat JS object literal, including ES6 shorthand properties."""
    keys, depth, part = set(), 0, ""
    for ch in literal:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            keys.add(part.split(":")[0].strip())
            part = ""
        else:
            part += ch
    if part.strip():
        keys.add(part.split(":")[0].strip())
    return {k for k in keys if k}


def object_literals_after(src: str, opener: str):
    """Every balanced `{...}` body following each occurrence of `opener`."""
    out = []
    for match in re.finditer(re.escape(opener), src):
        i = match.end() - 1
        depth, start = 0, i
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append(src[start + 1:j])
                    break
    return out


class Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key == "id":
                self.ids.add(value)
        self.tags.append(tag)


class FilesTest(unittest.TestCase):

    def test_every_client_file_exists(self):
        for name in REQUIRED:
            self.assertTrue(os.path.isfile(os.path.join(STATIC, name)),
                            f"missing static/{name}")

    @unittest.skipIf(NODE is None, "node is not installed")
    def test_javascript_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, src in all_js().items():
                copy = os.path.join(tmp, os.path.basename(name).replace(".js", ".mjs"))
                with open(copy, "w", encoding="utf-8") as fh:
                    fh.write(src)
                proc = subprocess.run([NODE, "--check", copy],
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"{name}: {proc.stderr}")

    def test_html_parses_and_defines_every_element_the_code_looks_up(self):
        parser = Ids()
        parser.feed(read("index.html"))
        self.assertIn("html", parser.tags)
        wanted = set(re.findall(r"'([a-z0-9-]+)'\]", read("app.js")))
        looked_up = {m for m in wanted if "-" in m}
        missing = looked_up - parser.ids
        self.assertFalse(missing, f"index.html is missing ids: {sorted(missing)}")


class ContractTest(unittest.TestCase):
    """Names crossing the wire must exist in protocol.py, in both directions."""

    def test_subscribed_events_all_exist(self):
        listed = set(re.findall(r"'([a-z_]+)'", read("app.js").split("];")[0]))
        self.assertTrue(listed)
        self.assertEqual(listed - EVENT_NAMES, set())

    def test_every_protocol_event_is_subscribed_and_reduced(self):
        subscribed = set(re.findall(r"'([a-z_]+)'", read("app.js").split("];")[0]))
        reduced = set(re.findall(r"case '([a-z_]+)':", read("state.js")))
        self.assertEqual(EVENT_NAMES - subscribed, set(), "unsubscribed events")
        self.assertEqual(EVENT_NAMES - reduced, set(), "unhandled events")

    def test_commands_match_the_command_schema(self):
        seen = set()
        for name, src in all_js().items():
            for literal in object_literals_after(src, "send({"):
                keys = top_level_keys(literal)
                match = re.search(r"type:\s*'([a-z_]+)'", literal)
                self.assertIsNotNone(match, f"{name}: command without a literal type")
                ctype = match.group(1)
                self.assertIn(ctype, COMMAND_NAMES, f"{name}: unknown command {ctype}")
                self.assertEqual(keys - {"type"}, set(P.COMMAND_SCHEMA[ctype]),
                                 f"{name}: {ctype} field mismatch")
                seen.add(ctype)
        self.assertIn(P.CMD_BUZZ, seen)
        self.assertIn(P.CMD_SELF_REPORT, seen)

    def test_host_commands_are_all_reachable(self):
        src = read("host.js")
        for cmd in P.HOST_COMMANDS:
            self.assertIn(f"'{cmd}'", src, f"host panel cannot issue {cmd}")


class BoardIndexingTest(unittest.TestCase):
    """The client's board lookup must agree with the server about cell coordinates.

    A 0-indexed client against a 1-indexed server renders a board that is internally
    consistent and completely wrong: the first row and first column silently come up
    blank and the top value row falls off the end. Unit tests on either side alone
    cannot see it, so the check has to compare the two.
    """

    def real_cells(self):
        import json as _json
        from jeopardy import redact
        from jeopardy.model import Game
        golden = os.path.join(os.path.dirname(STATIC), "tests", "fixtures",
                              "expected_9537.json")
        with open(golden, encoding="utf-8") as fh:
            game = Game.from_dict(_json.load(fh))
        return redact.public_board(game, "J", [])["cells"]

    def test_server_coordinates_are_one_indexed(self):
        coords = {(c["col"], c["row"]) for c in self.real_cells()}
        self.assertEqual(len(coords), 30)
        self.assertIn((1, 1), coords)
        self.assertIn((6, 5), coords)
        self.assertNotIn((0, 0), coords)

    def test_the_demo_fixture_uses_the_same_coordinates_as_the_server(self):
        src = read("fixtures/demo_stream.js")
        fixture = set(re.findall(r"for \(let (?:col|row) = (\d+)", src))
        self.assertEqual(fixture, {"1"},
                         "the demo fixture must be 1-indexed like the server")

    def test_board_iterates_from_one(self):
        src = read("board.js")
        starts = set(re.findall(r"for \(let (?:row|col) = (\d+)", src))
        self.assertEqual(starts, {"1"},
                         "board.js must look cells up with 1-indexed coordinates")


class OfflineTest(unittest.TestCase):

    def test_no_external_references(self):
        for name in REQUIRED:
            src = read(name)
            hits = [u for u in re.findall(r"https?://[^\s\"')]+", src)
                    if not u.startswith("http://www.w3.org/")]
            # w3.org URLs are XML namespace identifiers, never fetched; anything
            # else in a static file means the app stops working without internet.
            self.assertEqual(hits, [], f"static/{name} reaches outside: {hits}")

    def test_no_bundler_or_package_manager_artifacts(self):
        for name in ("package.json", "node_modules", "package-lock.json"):
            self.assertFalse(os.path.exists(os.path.join(STATIC, name)))


class HostPanelTest(unittest.TestCase):
    """The host plays, so the panel has nothing to disclose and must say nothing."""

    def test_host_panel_never_mentions_a_response(self):
        src = read("host.js").lower()
        for word in ("answer", "clue_resolved", "final_reveal", "daily", "is_daily_double"):
            self.assertNotIn(word, src, f"host.js references {word!r}")

    def test_host_markup_states_the_symmetry(self):
        self.assertIn("host-panel", read("index.html"))


class BuzzTimingTest(unittest.TestCase):
    """The press path is the one place where an await would be a fairness bug."""

    def _press_body(self):
        src = read("buzzer.js")
        start = src.index("\n  press()")
        end = src.index("\n  }\n", start)
        return src[start:end]

    def test_press_path_never_awaits(self):
        self.assertNotIn("await", self._press_body())

    def test_press_fires_the_post_before_it_paints(self):
        body = self._press_body()
        self.assertLess(body.index("this.send("), body.index("this._paint("))

    def test_press_timestamp_comes_from_the_monotonic_clock(self):
        body = self._press_body()
        self.assertIn("localNow()", body)
        self.assertNotIn("Date.now()", body)

    def test_unlock_is_converted_into_local_time(self):
        self.assertIn("this.clock.toLocal(clue.unlock_at)", read("buzzer.js"))

    def test_early_press_carries_no_penalty(self):
        src = read("buzzer.js")
        self.assertIn("_flashEarly", src)
        self.assertNotIn("lockout", code_only(src).lower())


class ClockTest(unittest.TestCase):

    def test_offset_uses_the_minimum_rtt_sample(self):
        src = read("clock.js")
        self.assertIn("s.rtt < best.rtt", src)

    def test_measurements_are_anchored_to_performance_now(self):
        src = code_only(read("clock.js"))
        self.assertIn("performance.now()", src)
        self.assertEqual(src.count("Date.now()"), 1, "wall clock read more than once")


@unittest.skipIf(NODE is None, "node is not installed")
class ReplayTest(unittest.TestCase):
    """Replay the recorded stream through the real reducer."""

    HARNESS = """
    import {{ initialState, reduce }} from '{state}';
    import {{ RECORDED_STREAM }} from '{stream}';
    let s = initialState();
    const trace = [];
    RECORDED_STREAM.forEach(([type, data], i) => {{
      s = reduce(s, type, data);
      const cell = (s.board.cells || []).find((c) => c.clue_id === data.clue_id);
      trace.push({{
        i, type, answer: s.clue ? s.clue.answer : null, on_screen: !!s.clue,
        cell_available: cell ? cell.available : null,
      }});
    }});
    console.log(JSON.stringify({{
      phase: s.phase, winner: s.winner, isHost: s.isHost, you: s.you,
      players: s.players, trace,
    }}));
    """

    @classmethod
    def setUpClass(cls):
        code = cls.HARNESS.format(
            state="file://" + os.path.join(STATIC, "state.js"),
            stream="file://" + os.path.join(STATIC, "fixtures", "demo_stream.js"),
        )
        proc = subprocess.run([NODE, "--input-type=module", "-e", code],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(proc.stderr)
        cls.result = json.loads(proc.stdout)

    def test_final_scores_match_the_stream(self):
        scores = {p["id"]: p["score"] for p in self.result["players"]}
        self.assertEqual(scores, {"p1": 0, "p2": 0, "p3": 800})
        self.assertEqual(self.result["phase"], P.PHASE_GAME_OVER)
        self.assertEqual(self.result["winner"], "p3")

    def test_the_answer_screen_holds_until_dismissed_then_spends_the_cell(self):
        trace = self.result["trace"]
        for i, row in enumerate(trace):
            if row["type"] != P.EVT_CLUE_RESOLVED:
                continue
            self.assertTrue(row["on_screen"], "a resolved clue must stay on screen")
            self.assertFalse(row["cell_available"], "a resolved cell must be spent")
            dismissed = trace[i + 1]
            self.assertEqual(dismissed["type"], P.EVT_CLUE_DISMISSED)
            self.assertFalse(dismissed["on_screen"])

    def test_a_clue_response_is_held_until_its_clue_resolves(self):
        for row in self.result["trace"]:
            if row["type"] != P.EVT_CLUE_RESOLVED and row["answer"] is not None:
                seen_resolve = any(
                    t["type"] == P.EVT_CLUE_RESOLVED and t["i"] < row["i"]
                    and t["answer"] == row["answer"]
                    for t in self.result["trace"])
                self.assertTrue(seen_resolve,
                                f"response surfaced at step {row['i']} on {row['type']}")


@unittest.skipIf(NODE is None, "node is not installed")
class ReboundTest(unittest.TestCase):
    """After a miss reopens the clue, only the player who missed stays locked out."""

    HARNESS = """
    import {{ initialState, reduce, buzzLockedOut }} from '{state}';
    const steps = [
      ['clue_selected', {{ clue_id: 'J_1_1', category: 'C', value: 200, text: 't',
                           media: [], unlock_at: 0, selected_by: 'p1' }}],
      ['buzzers_open', {{ clue_id: 'J_1_1' }}],
      ['buzz_result', {{ clue_id: 'J_1_1', order: [{{ player_id: 'p1' }}],
                         rejected: [] }}],
      ['answering', {{ clue_id: 'J_1_1', player_id: 'p1', deadline: 1 }}],
      ['answer_result', {{ clue_id: 'J_1_1', player_id: 'p1', correct: false,
                           score_delta: -200 }}],
      ['buzzers_open', {{ clue_id: 'J_1_1' }}],
    ];
    const out = {{}};
    ['p1', 'p2'].forEach((you) => {{
      let s = {{ ...initialState(), you }};
      steps.forEach(([t, d]) => {{ s = reduce(s, t, d); }});
      out[you] = {{ locked: buzzLockedOut(s), step: s.clue.step,
                   order: s.buzz.order.length }};
    }});
    console.log(JSON.stringify(out));
    """

    def test_only_the_player_who_missed_is_locked_out(self):
        code = self.HARNESS.format(state="file://" + os.path.join(STATIC, "state.js"))
        proc = subprocess.run([NODE, "--input-type=module", "-e", code],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["p1"]["locked"])
        self.assertFalse(out["p2"]["locked"])
        self.assertEqual(out["p2"]["step"], "open")
        self.assertEqual(out["p2"]["order"], 0, "a reopen must clear the last race")


if __name__ == "__main__":
    unittest.main()
