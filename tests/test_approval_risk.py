"""Approval rule: a Partner-recorded approval covers low-risk actions only, and must carry
the Owner's chat time. High-risk or unclear actions get NEEDS-CONFIRM (exit 2) unless Lead recorded the approval."""
import contextlib, io, pathlib, shutil, subprocess, sys, tempfile, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import bridge  # noqa: E402
from test_bridge import BOARD  # noqa: E402

LOW = ["read the example-app README", "draft an email to the venue", "review PR 12 in example-org/example-app",
       "update the board row for TASK-1", "commit to a local branch in example-app", "summarise the CI logs"]
HIGH = ["send the draft email to the venue", "post the release notes", "pay the example invoice",
        "delete the old claims", "make example-org/example-app public", "change repo visibility to private",
        "install example-tool", "force-push main in example-app", "draft and send the reply",
        "restart launchd jobs on the host", "merge PR 12 in example-org/example-app", "push main to origin",
        "rename the shared folders on the host", "something nobody listed"]


class RiskTest(unittest.TestCase):
    def test_low_and_high_scopes(self):
        for s in LOW:
            self.assertEqual(bridge.risk_of(s)[0], "low", s)
        for s in HIGH:
            self.assertEqual(bridge.risk_of(s)[0], "high", s)


class ApprovalRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        for d in ("claims", "inbox-partner", "inbox-lead", "done"):
            (self.tmp / d).mkdir()
        shutil.copytree(REPO / "schema", self.tmp / "schema")
        (self.tmp / "BOARD.md").write_text(BOARD)
        self._root, bridge.ROOT = bridge.ROOT, self.tmp

    def tearDown(self):
        bridge.ROOT = self._root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *argv):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            bridge.main(list(argv))
        return cm.exception.code, out.getvalue()

    def approve(self, scope, agent, chat_time="2026-10-27T15:58:00Z"):
        argv = ["approve", "--scope", scope, "--expires", "2h", "--agent", agent, "--quote", "yes"]
        if chat_time:
            argv += ["--chat-time", chat_time]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bridge.main(argv)
        return out.getvalue().strip()

    def test_partner_low_risk_is_valid_and_consumable(self):
        aid = self.approve("draft an email to the venue", "Partner")
        self.assertEqual(self.cli("approval", "check", aid, "--scope", "draft an email to the venue")[0], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.main(["approval", "consume", aid, "--scope", "draft an email to the venue", "--agent", "Partner"])
        self.assertIn("consumed_at", bridge.approval_path(aid).read_text())

    def test_partner_high_risk_needs_confirm_and_is_not_consumed(self):
        aid = self.approve("send the draft email to the venue", "Partner")
        code, out = self.cli("approval", "check", aid, "--scope", "send the draft email to the venue")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("NEEDS-CONFIRM"), out)
        with self.assertRaises(SystemExit) as cm:
            bridge.main(["approval", "consume", aid, "--scope", "send the draft email to the venue", "--agent", "Lead"])
        self.assertIn("NEEDS-CONFIRM", str(cm.exception.code))
        self.assertNotIn("consumed_at", bridge.approval_path(aid).read_text())

    def test_lead_recorded_high_risk_is_valid(self):
        aid = self.approve("send the draft email to the venue", "Lead", chat_time=None)
        self.assertEqual(self.cli("approval", "check", aid, "--scope", "send the draft email to the venue")[0], 0)

    def test_partner_must_give_chat_time(self):
        with self.assertRaises(SystemExit):
            self.approve("read the README", "Partner", chat_time=None)
        aid = self.approve("read the README", "Lead", chat_time=None)
        p = bridge.approval_path(aid)
        p.write_text(p.read_text().replace('recorded_by: "Lead"', 'recorded_by: "Partner"'))  # hand edit, no chat_time
        self.assertIn("needs chat_time", "\n".join(bridge.validate()))
        code, out = self.cli("approval", "check", aid, "--scope", "read the README")
        self.assertEqual(code, 2)
        self.assertIn("without the Owner's chat_time", out)

    def test_expired_or_used_still_beats_risk(self):
        aid = self.approve("send it", "Partner")
        code, out = self.cli("approval", "check", aid, "--scope", "send it", "--now", "2099-01-01T00:00:00Z")
        self.assertEqual((code, out.strip()[:7]), (1, "expired"))


class StagedValidateTest(unittest.TestCase):
    """The pre-commit hook validates only staged files, so one writer's bad file can't block another's commit."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        for d in ("claims", "inbox-partner", "inbox-lead", "done"):
            (self.tmp / d).mkdir()
        shutil.copytree(REPO / "schema", self.tmp / "schema")
        (self.tmp / "BOARD.md").write_text(BOARD)
        self._root, bridge.ROOT = bridge.ROOT, self.tmp

    def tearDown(self):
        bridge.ROOT = self._root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def staged_code(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            bridge.main(["validate", "--staged"])
        return cm.exception.code, out.getvalue()

    def test_only_staged_errors_count(self):
        (self.tmp / "claims" / "BAD.claim").write_text('owner: "Robot"\n')  # someone else's bad, unstaged file
        good = self.tmp / "log" / "2026-10-27"
        good.mkdir(parents=True)
        (good / "Lead.md").write_text("- 16:00:00Z | Lead | - | decided | fine\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "log"], check=True)
        self.assertEqual(self.staged_code()[0], 0)
        self.assertTrue(bridge.validate())  # the full check still sees the bad claim
        subprocess.run(["git", "-C", str(self.tmp), "add", "claims"], check=True)
        code, out = self.staged_code()
        self.assertEqual(code, 1)
        self.assertIn("claims/BAD.claim", out)


if __name__ == "__main__":
    unittest.main()
