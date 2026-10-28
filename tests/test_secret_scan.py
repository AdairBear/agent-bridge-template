"""The secret scan must fail on a secret and pass on clean text, in both gitleaks and regex modes."""
import os, pathlib, secrets, shutil, string, subprocess, tempfile, unittest

SCAN = pathlib.Path(__file__).resolve().parent.parent / "tools" / "secret-scan.sh"
# Built at run time so this test file itself holds no secret-shaped string.
FAKE = "AKIA" + "Q7XK2M9PLR4TZW8N"
# gitleaks skips low-entropy samples, so its test uses a random GitHub-token shape.
RANDOM_GH = "gh" + "p_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(36))


class SecretScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.tmp], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def scan(self, text, regex):
        p = pathlib.Path(self.tmp, "f.txt")
        p.write_text(text)
        subprocess.run(["git", "-C", self.tmp, "add", "f.txt"], check=True)
        env = dict(os.environ, BRIDGE_SCAN_REGEX="1" if regex else "")
        return subprocess.run(["sh", str(SCAN), "--staged"], cwd=self.tmp, env=env, capture_output=True).returncode

    def test_regex_fallback(self):
        self.assertEqual(self.scan("hello\n", regex=True), 0)
        self.assertEqual(self.scan(f"aws_key = {FAKE}\n", regex=True), 1)

    @unittest.skipUnless(shutil.which("gitleaks"), "gitleaks not installed")
    def test_gitleaks(self):
        self.assertEqual(self.scan("hello\n", regex=False), 0)
        self.assertEqual(self.scan(f'github_token = "{RANDOM_GH}"\n', regex=False), 1)


if __name__ == "__main__":
    unittest.main()
