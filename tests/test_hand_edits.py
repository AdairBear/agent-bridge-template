"""Partner edits bridge files by hand through a git hosting connector. These tests check that the README templates pass
`validate` exactly as written, and that common hand-edit mistakes are caught with a fix hint."""
import contextlib, io, json, pathlib, re, shutil, sys, tempfile, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import bridge  # noqa: E402
from test_bridge import BOARD  # noqa: E402

TEMPLATE_RE = re.compile(r"<!-- template: (\S+) -->\n```\n(.*?)```", re.S)


def readme_templates() -> dict[str, str]:
    return dict(TEMPLATE_RE.findall((REPO / "README.md").read_text()))


class HandEditTest(unittest.TestCase):
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

    def put(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def put_templates(self):
        for rel, text in readme_templates().items():
            self.put(rel, text)

    def errors(self):
        return "\n".join(bridge.validate())

    def test_readme_has_a_template_for_every_hand_written_file(self):
        kinds = {rel.split("/")[0] for rel in readme_templates()}
        self.assertEqual(kinds, {"claims", "inbox-lead", "log", "done", "approvals", "heartbeat"})

    def test_readme_templates_pass_validate_as_written(self):
        self.put_templates()
        self.assertEqual(bridge.validate(), [])

    def test_bridge_reads_hand_written_log_and_approval(self):
        self.put_templates()
        kinds = [e["kind"] for e in bridge.read_log("2026-10-27")]
        self.assertEqual(kinds, ["request", "promised"])
        ap, why = bridge.approval_problem("A-20261027-160000-0C2D", "merge PR 12 in example-org/example-app",
                                          bridge.parse_time("2026-10-27T17:00:00Z"))
        self.assertIsNone(why)

    def test_hand_consumed_approval_is_refused_by_the_tool(self):
        self.put_templates()
        p = self.tmp / "approvals" / "A-20261027-160000-0C2D.approval"
        p.write_text(p.read_text() + 'consumed_by: "Partner"\nconsumed_at: "2026-10-27T16:10:00Z"\n')
        self.assertEqual(bridge.validate(), [])
        _, why = bridge.approval_problem(p.stem, "merge PR 12 in example-org/example-app", bridge.parse_time("2026-10-27T17:00:00Z"))
        self.assertIn("already used by Partner", why)

    def test_malformed_log_lines_are_caught(self):
        self.put("log/2026-10-27/Partner.md", "# Log 2026-10-27, Partner\n\n"
                 "- 4pm | Partner | - | said | no seconds, no Z\n"
                 "- 16:00:00Z | Lead | - | said | wrong file\n"
                 "- 16:00:00Z | Partner | R-1 | said | bad id\n"
                 "- 16:00:00Z | Partner | - | shouted | bad kind\n"
                 "just some prose\n")
        self.put("log/2026-10-27.md", "old flat layout\n")
        self.put("log/2026-10-27/Robot.md", "")
        errs = self.errors()
        for want in (":3: not a log line", ":4: agent 'Lead' writes only to its own file", ":5: bad request ID",
                     ":6: kind 'shouted'", ":7: not a log line", "log/2026-10-27.md: log entries go in",
                     "Robot.md: name must be <agent>.md"):
            self.assertIn(want, errs)

    def test_malformed_approvals_are_caught(self):
        good = readme_templates()["approvals/A-20261027-160000-0C2D.approval"]
        self.put("approvals/A-20261027-160000-0C2D.approval",
                 good.replace('"2026-10-28T16:00:00Z"', '"2026-11-27T16:00:00Z"') + 'consumed_by: "Partner"\n')
        self.put("approvals/A-20261027-170000-0E3F.approval", good)  # id inside does not match the file name
        self.put("approvals/A-20261027-180000-0A4B.approval", 'id: "A-20261027-180000-0A4B"\nscope: "x"\n')
        self.put("approvals/note.txt", "hi\n")
        errs = self.errors()
        for want in ("at most 7 days later", "consumed_by and consumed_at go together", "does not match the file name",
                     "missing 'granted_at'", "only the Owner grants", "note.txt: name must be <A-ID>.approval"):
            self.assertIn(want, errs)

    def test_malformed_heartbeats_and_misfiled_notes_are_caught(self):
        self.put("heartbeat/Partner.json", '{"agent": "Lead", "at": "2099-01-01T00:00:00Z", "every_hours": "six"}')
        self.put("heartbeat/Lead.json", "{not json")
        note = readme_templates()["inbox-lead/2026-10-27-EXAMPLE-TASK.md"]
        self.put("inbox-partner/2026-10-27-EXAMPLE-TASK.md", note)
        errs = self.errors()
        for want in ("'agent' must be 'Partner'", "is in the future", "'every_hours' must be a number",
                     "Lead.json: not a valid heartbeat", "'to' is Lead but the note sits in inbox-partner/"):
            self.assertIn(want, errs)

    def test_heartbeat_every_hours_sets_the_silence_threshold(self):
        self.put_templates()  # Partner's heartbeat: 16:00Z, every_hours 6
        at = bridge.parse_time("2026-10-27T21:00:00Z")
        self.assertEqual(bridge.silent_agents(at), [])
        self.assertEqual([x["agent"] for x in bridge.silent_agents(bridge.parse_time("2026-10-27T22:30:00Z"))], ["Partner"])
        self.assertEqual([x["agent"] for x in bridge.silent_agents(at, hours=2)], ["Partner"])  # CLI override

    def test_fix_report_gives_a_hint_per_problem_and_changes_nothing(self):
        p = self.put("log/2026-10-27/Partner.md", "- 4pm | Partner | - | said | x\n")
        before = p.read_text()
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            bridge.main(["validate", "--fix-report"])
        self.assertEqual(cm.exception.code, 1)
        report = out.getvalue()
        self.assertIn("# Validate report", report)
        self.assertIn("`log/2026-10-27/Partner.md:1`", report)
        self.assertIn("**Fix:** write `- HH:MM:SSZ", report)
        self.assertEqual(p.read_text(), before)

    def test_fix_report_on_clean_repo(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            bridge.main(["validate", "--fix-report"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("All bridge files are valid.", out.getvalue())

    def test_tool_written_files_match_the_hand_layout(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.main(["log", "said", "hello", "--agent", "Lead"])
            bridge.main(["heartbeat", "--agent", "Partner", "--every-hours", "6"])
        day = bridge.now().date().isoformat()
        self.assertTrue((self.tmp / "log" / day / "Lead.md").exists())
        self.assertEqual(json.loads((self.tmp / "heartbeat" / "Partner.json").read_text())["every_hours"], 6.0)
        self.assertEqual(bridge.validate(), [])


if __name__ == "__main__":
    unittest.main()
