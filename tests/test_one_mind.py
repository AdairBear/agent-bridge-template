"""Tests for the one-mind commands in tools/bridge.py: log, digest, request, approve, heartbeat, sync."""
import contextlib, datetime as dt, io, json, pathlib, shutil, subprocess, sys, tempfile, time, unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import bridge  # noqa: E402
from test_bridge import BOARD  # noqa: E402


class OneMindTest(unittest.TestCase):
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

    def exit_code(self, *argv):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            bridge.main(list(argv))
        return cm.exception.code, out.getvalue()

    def today(self):
        return bridge.now().date().isoformat()

    # ---- log and digest
    def test_log_appends_one_attributed_line(self):
        self.run_cli("log", "said", "first | line\nwith newline", "--agent", "Lead")
        self.run_cli("log", "decided", "use git as transport", "--agent", "Partner")
        entries = bridge.read_log(self.today())
        self.assertEqual([(e["agent"], e["kind"]) for e in entries], [("Lead", "said"), ("Partner", "decided")])
        self.assertEqual(entries[0]["text"], "first / line with newline")  # one line, no stray pipes

    def test_log_refuses_bad_request_id_and_kind(self):
        with self.assertRaises(SystemExit):
            self.run_cli("log", "said", "x", "--agent", "Partner", "--req", "R-nope")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("log", "shouted", "x", "--agent", "Partner")

    def test_digest_keeps_decisions_and_drops_chat(self):
        req = self.run_cli("request", "new", "audit example-app", "--agent", "Lead").strip()
        self.run_cli("log", "said", "chatty line", "--agent", "Lead", "--req", req)
        self.run_cli("log", "promised", "finish plan by Friday", "--agent", "Partner", "--req", req)
        path = self.tmp / self.run_cli("digest").strip()
        text = path.read_text()
        self.assertIn("finish plan by Friday", text)
        self.assertIn("audit example-app", text)
        self.assertNotIn("chatty line", text)
        self.assertIn("1 'said' lines left out", text)
        self.assertTrue(bridge.log_path(self.today()).exists())  # the full log is never trimmed

    # ---- requests
    def test_request_ids_are_unique_and_seen_flags_duplicates(self):
        a = self.run_cli("request", "new", "Push the CI trim for six repos", "--agent", "Lead").strip()
        self.assertRegex(a, bridge.REQ_RE)
        code, out = self.exit_code("request", "seen", "push the ci trim for the six repos")
        self.assertEqual(code, 1)
        self.assertIn(a, out)
        self.assertIn("not acted on yet", out)
        self.run_cli("log", "decided", "pushed all six", "--agent", "Partner", "--req", a)
        self.assertIn("last: decided by Partner", self.exit_code("request", "seen", "push the ci trim for six repos")[1])
        self.assertEqual(self.exit_code("request", "seen", "book a table for dinner")[0], 0)

    def test_seen_looks_back_only_the_given_days(self):
        self.run_cli("request", "new", "hash the shared drive", "--agent", "Partner")
        later = bridge.iso(bridge.now() + dt.timedelta(days=9))
        self.assertEqual(self.exit_code("request", "seen", "hash the shared drive", "--now", later)[0], 0)

    # ---- approvals
    def approve(self, scope="push example-app main", expires="2h"):
        return self.run_cli("approve", "--scope", scope, "--expires", expires, "--agent", "Lead",
                            "--quote", "yes, push it").strip()

    def test_approval_is_single_use_and_exact_scope(self):
        aid = self.approve()
        self.assertEqual(self.exit_code("approval", "check", aid, "--scope", "push  example-app main")[0], 0)
        self.assertIn("scope differs", self.exit_code("approval", "check", aid, "--scope", "push every repo")[1])
        self.run_cli("approval", "consume", aid, "--scope", "push example-app main", "--agent", "Partner")
        code, out = self.exit_code("approval", "check", aid, "--scope", "push example-app main")
        self.assertEqual(code, 1)
        self.assertIn("already used by Partner", out)
        with self.assertRaises(SystemExit):
            self.run_cli("approval", "consume", aid, "--scope", "push example-app main", "--agent", "Lead")
        self.assertTrue(bridge.approval_path(aid).exists())  # consumed, never deleted
        self.assertEqual([e["kind"] for e in bridge.read_log(self.today())], ["approval", "consumed"])

    def test_approval_expires(self):
        aid = self.approve(expires="30m")
        later = bridge.iso(bridge.now() + dt.timedelta(minutes=31))
        code, out = self.exit_code("approval", "check", aid, "--scope", "push example-app main", "--now", later)
        self.assertEqual(code, 1)
        self.assertIn("expired", out)

    def test_approval_refuses_standing_or_past_expiry_and_unknown_ids(self):
        for bad in ("8d", "2000-01-01T00:00:00Z"):
            with self.assertRaises(SystemExit):
                self.approve(expires=bad)
        self.assertIn("no approval", self.exit_code("approval", "check", "A-x", "--scope", "y")[1])

    def test_validate_flags_approval_not_granted_by_owner(self):
        aid = self.approve()
        p = bridge.approval_path(aid)
        p.write_text(p.read_text().replace('granted_by: "Owner"', 'granted_by: "Partner"'))
        self.assertIn("only the Owner grants approvals", "\n".join(bridge.validate()))

    # ---- heartbeat
    def test_heartbeat_and_silent_agents(self):
        self.run_cli("heartbeat", "--agent", "Partner", "--doing", "TASK-1")
        at = bridge.parse_time(json.loads((self.tmp / "heartbeat" / "Partner.json").read_text())["at"])
        self.assertEqual(bridge.silent_agents(at + dt.timedelta(hours=1.9)), [])
        found = json.loads(self.run_cli("stale", "--json", "--now", bridge.iso(at + dt.timedelta(hours=2.5))))
        self.assertEqual([(x["agent"], x["doing"]) for x in found], [("Partner", "TASK-1")])
        self.assertIn("Partner: silent for 2.5h", self.run_cli("stale", "--now", bridge.iso(at + dt.timedelta(hours=2.5))))
        self.assertEqual(bridge.validate(), [])


class SyncTest(unittest.TestCase):
    """sync runs a real `git pull --rebase` against a throwaway bare remote."""

    def git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                        "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
                       cwd=cwd, check=True, capture_output=True)

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", "remote.git")
        self.git(self.tmp, "clone", "-q", "remote.git", "a")
        a = self.tmp / "a"
        for d in ("claims", "inbox-partner", "inbox-lead", "done"):
            (a / d).mkdir()
            (a / d / ".keep").write_text("")
        shutil.copytree(REPO / "schema", a / "schema")
        (a / "BOARD.md").write_text(BOARD)
        self.git(a, "add", "-A"); self.git(a, "commit", "-qm", "seed"); self.git(a, "push", "-q", "origin", "main")
        self.git(self.tmp, "clone", "-q", "remote.git", "b")
        self._root, bridge.ROOT = bridge.ROOT, self.tmp / "b"

    def tearDown(self):
        bridge.ROOT = self._root
        for _ in range(5):  # git on macOS can still be writing inside .git for a moment; retry the temp cleanup
            try:
                shutil.rmtree(self.tmp)
                return
            except OSError:
                time.sleep(0.2)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_pulls_other_agents_work_then_validates(self):
        a = self.tmp / "a"
        (a / "heartbeat").mkdir()
        (a / "heartbeat" / "Partner.json").write_text('{"agent": "Partner", "at": "2026-10-27T10:00:00Z"}\n')
        self.git(a, "add", "-A"); self.git(a, "commit", "-qm", "hb"); self.git(a, "push", "-q", "origin", "main")
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            bridge.main(["sync"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("synced and valid", out.getvalue())
        self.assertTrue((self.tmp / "b" / "heartbeat" / "Partner.json").exists())

    def test_sync_fails_loudly_without_a_remote(self):
        bridge.ROOT = self.tmp  # not a git repo
        with self.assertRaises(SystemExit) as cm:
            bridge.main(["sync"])
        self.assertIn("git pull --rebase failed", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
