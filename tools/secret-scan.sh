#!/bin/sh
# Secret scan. Exit 1 when it finds a secret.
#   secret-scan.sh            scan staged changes (the pre-commit hook runs this)
#   secret-scan.sh --history  scan the whole git history (run before every push)
# Uses gitleaks when installed; otherwise a regex fallback. BRIDGE_SCAN_REGEX=1 forces the fallback (for tests).
mode=${1:---staged}
if [ -z "$BRIDGE_SCAN_REGEX" ] && command -v gitleaks >/dev/null 2>&1; then
  if [ "$mode" = "--history" ]; then
    gitleaks git . --redact --no-banner || { echo "secret-scan: gitleaks found a secret" >&2; exit 1; }
  else
    gitleaks git --pre-commit --staged . --redact --no-banner || { echo "secret-scan: gitleaks found a secret" >&2; exit 1; }
  fi
  exit 0
fi
# Fallback: common key shapes plus generic key/token/password assignments with a long value.
PATTERN='(AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(api[_-]?key|secret|token|passw(or)?d)["'\'' ]*[:=][ ]*["'\'']?[A-Za-z0-9/+_=-]{16,})'
if [ "$mode" = "--history" ]; then
  added=$(git log -p --all --no-color | grep '^+' | grep -v '^+++' || true)
else
  added=$(git diff --cached --no-color -U0 | grep '^+' | grep -v '^+++' || true)
fi
hits=$(printf '%s\n' "$added" | grep -Ein "$PATTERN" | grep -v 'secret-scan: allow' || true)
if [ -n "$hits" ]; then
  echo "secret-scan: possible secret in $(printf '%s\n' "$hits" | wc -l | tr -d ' ') added line(s); values not printed" >&2
  exit 1
fi
exit 0
