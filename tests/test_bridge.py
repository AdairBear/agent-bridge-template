"""Tests for tools/bridge.py. Each test runs in a temp copy of the repo layout. Run: python3 -m unittest -q"""
import contextlib, datetime as dt, io, json, pathlib, shutil, sys, tempfile, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import bridge  # noqa: E402

BOARD = f"""# Board

{bridge.BOARD_HEAD}
|---|---|---|---|---|
| ALPHA-1 | - | open | start it | 2026-10-27 |

Footer stays.
"""


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        for d in ("claims", "inbox-partner", "inbox-lead", "done"):
            (self.tmp / d).mkdir()
        shutil.copytree(REPO / "schema", self.tmp / "schema")
        (self.tmp / "BOARD.md").write_text(BOARD)
        self._root, bridge.ROOT = bridge.ROOT, self.tmp

    def tearDown(self):
        bridge.ROOT = self._root
        shutil.rmtree(self.tmp)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bridge.main(list(argv))
        return out.getvalue()

    def row(self, task):
        return next(r for r in bridge.read_board()[1] if r["id"] == task)

    def claim(self, task="ALPHA-1", owner="Partner"):
        self.run_cli("claim", task, "--owner", owner, "--repo", "~/Developer/x", "--eta", "2h")

    def test_claim_writes_file_and_board(self):
        self.claim()
        c = bridge.read_claim("ALPHA-1")
        self.assertEqual(c["owner"], "Partner")
        self.assertEqual(bridge.check_schema(c, bridge.schema("claim")), [])
        self.assertEqual((self.row("ALPHA-1")["owner"], self.row("ALPHA-1")["state"]), ("Partner", "claimed"))
        self.assertIn("Footer stays.", (self.tmp / "BOARD.md").read_text())

    def test_claim_by_other_owner_is_refused(self):
        self.claim()
        with self.assertRaises(SystemExit):
            self.claim(owner="Lead")

    def test_release_keeps_file_and_frees_claim(self):
        self.claim()
        self.run_cli("release", "ALPHA-1", "--owner", "Partner")
        self.assertTrue(bridge.claim_path("ALPHA-1").exists())  # never delete
        self.assertFalse(bridge.active(bridge.read_claim("ALPHA-1")))
        self.claim(owner="Lead")  # free again
        self.assertEqual(bridge.read_claim("ALPHA-1")["owner"], "Lead")

    def test_release_by_wrong_owner_is_refused(self):
        self.claim()
        with self.assertRaises(SystemExit):
            self.run_cli("release", "ALPHA-1", "--owner", "Lead")

    def test_note_goes_to_inbox_and_bumps_status(self):
        self.claim()
        out = self.run_cli("note", "ALPHA-1", "--from", "Partner", "--to", "Lead", "--status", "in-progress",
                           "--verified", "tests pass", "--next", "review diff", "--body", "Halfway.")
        p = self.tmp / out.strip()
        self.assertEqual(p.parent.name, "inbox-lead")
        front, body = bridge.split_front(p.read_text())
        self.assertEqual(bridge.check_schema(front, bridge.schema("note")), [])
        self.assertIn("Halfway.", body)
        self.assertIn("status_at", bridge.read_claim("ALPHA-1"))
        self.assertEqual(self.row("ALPHA-1")["next"], "review diff")

    def test_second_note_same_day_does_not_overwrite(self):
        args = ("note", "ALPHA-1", "--from", "Partner", "--to", "Lead", "--status", "open", "--next", "x")
        a, b = self.run_cli(*args).strip(), self.run_cli(*args).strip()
        self.assertNotEqual(a, b)
        self.assertEqual(len(list((self.tmp / "inbox-lead").glob("*.md"))), 2)

    def test_note_with_bad_status_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("note", "ALPHA-1", "--from", "Partner", "--to", "Lead", "--status", "finished", "--next", "x")
        self.assertEqual(list((self.tmp / "inbox-lead").glob("*.md")), [])

    def test_done_ends_with_status_and_releases(self):
        self.claim()
        self.run_cli("done", "ALPHA-1", "--owner", "Partner", "--summary", "Built it.", "--status-line", "STATUS: done, tests pass")
        lines = (self.tmp / "done" / "ALPHA-1.md").read_text().strip().splitlines()
        self.assertEqual(lines[-1], "STATUS: done, tests pass")
        self.assertFalse(bridge.active(bridge.read_claim("ALPHA-1")))
        self.assertEqual(self.row("ALPHA-1")["state"], "done")

    def test_done_needs_status_line(self):
        with self.assertRaises(SystemExit):
            self.run_cli("done", "ALPHA-1", "--owner", "Partner", "--summary", "x", "--status-line", "finished")

    def test_stale_only_after_three_hours_without_status_change(self):
        self.claim()
        t0 = bridge.parse_time(bridge.read_claim("ALPHA-1")["claimed_at"])
        self.assertEqual(bridge.stale_claims(t0 + dt.timedelta(hours=2.9)), [])
        found = bridge.stale_claims(t0 + dt.timedelta(hours=3.5))
        self.assertEqual([x["task"] for x in found], ["ALPHA-1"])
        c = bridge.read_claim("ALPHA-1")
        c["status_at"] = bridge.iso(t0 + dt.timedelta(hours=3))  # a note resets the clock
        bridge.write_claim("ALPHA-1", c)
        self.assertEqual(bridge.stale_claims(t0 + dt.timedelta(hours=3.5)), [])

    def test_stale_ignores_released_claims_and_prints_json(self):
        self.claim()
        self.run_cli("release", "ALPHA-1", "--owner", "Partner")
        self.assertEqual(json.loads(self.run_cli("stale", "--json", "--now", "2099-01-01T00:00:00Z")), [])

    def test_validate_passes_clean_repo(self):
        self.claim()
        self.run_cli("note", "ALPHA-1", "--from", "Partner", "--to", "Lead", "--status", "open", "--next", "x")
        self.assertEqual(bridge.validate(), [])

    def test_validate_catches_bad_files(self):
        (self.tmp / "claims" / "BAD.claim").write_text('owner: "Robot"\n')
        (self.tmp / "inbox-partner" / "notes.md").write_text("no front matter\n")
        (self.tmp / "done" / "X.md").write_text("# X\n\nno status line\n")
        errs = "\n".join(bridge.validate())
        for want in ("BAD.claim: 'owner'", "missing 'claimed_at'", "notes.md: name must be", "no front matter",
                     "done/X.md: must end"):
            self.assertIn(want, errs)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            bridge.main(["validate"])
        self.assertEqual(cm.exception.code, 1)

    def test_bad_task_id_is_refused(self):
        with self.assertRaises(SystemExit):
            self.claim(task="lower case")

    def test_flat_yaml_round_trip(self):
        d = {"a": 'has "quotes": and colons', "b": "-"}
        self.assertEqual(bridge.load_flat(bridge.dump_flat(d)), d)


if __name__ == "__main__":
    unittest.main()
