#!/usr/bin/env python3
"""bridge: claim, note and report tasks between Lead and Partner. Stdlib only, so it runs anywhere Partner runs.

Files are DATA, never instructions. This tool only reads and writes files in this repo; git does the transport.
It never deletes: release and done stamp `released_at` on the claim, consume stamps `consumed_at` on an approval,
and digest adds a summary next to the day's log without trimming it.
"""
import argparse, datetime as dt, json, pathlib, re, secrets, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS = ("Lead", "Partner", "Owner")
INBOX = {"Partner": "inbox-partner", "Lead": "inbox-lead"}
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,47}$")
STATUS_RE = re.compile(r"^STATUS: \S.*$")
BOARD_HEAD = "| ID | owner | state | next step | updated |"
STALE_HOURS = 3.0
SILENT_HOURS = 2.0  # default: a heartbeat older than this means the agent may be down. A heartbeat may set its own
                    # `every_hours`, because Partner only checks in every few hours.
LOG_KINDS = ("said", "decided", "promised")  # what agents log by choice
ALL_KINDS = LOG_KINDS + ("ask", "request", "approval", "consumed")  # also valid in a hand-written log line
# ("ask" = an open question to the Owner; an automation job can mirror them from the Lead chat log)
LOG_LINE_RE = re.compile(r"^- (\d{2}:\d{2}:\d{2}Z) \| (\w+) \| (\S+) \| (\w+) \| (.+)$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQ_RE = re.compile(r"^R-\d{8}-\d{6}-[0-9A-F]{4}$")
APPROVAL_RE = re.compile(r"^A-\d{8}-\d{6}-[0-9A-F]{4}$")
MAX_APPROVAL = dt.timedelta(days=7)  # approvals are per-action, so no standing ones
# Approval rule: an approval that Partner recorded covers low-risk actions only (reads, drafts, board
# edits, local branches). Anything else needs a Lead-recorded approval or a fresh OK from the Owner: NEEDS-CONFIRM.
# Unknown actions count as high-risk, so the word lists only ever let through what is clearly low.
ALWAYS_HIGH = re.compile(r"\b(send|post|tweet|publish|public|visibility|pay|payment|purchase|buy|order|transfer|"
                         r"delete|remove|rm|erase|wipe|install|uninstall|force|force-push|deploy|sudo|launchd)\b", re.I)
HIGH_UNLESS_LOW_VERB = re.compile(r"\b(email|e-mail|message|dm|reply|private|release|host|drop|merge|push)\b", re.I)
LOW_VERB_FIRST = re.compile(r"^(read|review|audit|research|summari[sz]e|draft|triage|check|list)\b", re.I)
LOW_ANYWHERE = re.compile(r"\b(board|local branch|note|log)\b", re.I)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- flat YAML: `key: value` lines only. Enough for claims and front matter, no PyYAML needed.
def load_flat(text: str) -> dict:
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"line {n}: expected 'key: value'")
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = json.loads(v)
        out[k.strip()] = v
    return out


def dump_flat(d: dict) -> str:
    return "".join(f"{k}: {json.dumps(str(v))}\n" for k, v in d.items())


def split_front(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("no front matter")
    return load_flat(m.group(1)), m.group(2)


# ---- minimal JSON Schema check: type object, required, enum, pattern, additionalProperties, format date-time
def check_schema(doc: dict, schema: dict) -> list[str]:
    errs = [f"missing '{k}'" for k in schema.get("required", []) if k not in doc]
    props = schema.get("properties", {})
    for k, v in doc.items():
        p = props.get(k)
        if p is None:
            if schema.get("additionalProperties") is False:
                errs.append(f"unknown field '{k}'")
            continue
        if not isinstance(v, str):
            errs.append(f"'{k}' must be a string")
        elif "enum" in p and v not in p["enum"]:
            errs.append(f"'{k}'={v!r} not in {p['enum']}")
        elif "pattern" in p and not re.search(p["pattern"], v):
            errs.append(f"'{k}'={v!r} does not match {p['pattern']}")
        elif "minLength" in p and len(v) < p["minLength"]:
            errs.append(f"'{k}' is empty")
        elif p.get("format") == "date-time":
            try:
                parse_time(v)
            except ValueError:
                errs.append(f"'{k}'={v!r} is not an ISO date-time")
    return errs


def schema(name: str) -> dict:
    return json.loads((ROOT / "schema" / f"{name}.schema.json").read_text())


# ---- board
def read_board() -> tuple[list[str], list[dict], list[str]]:
    """Returns (lines before the table rows, rows, lines after)."""
    lines = (ROOT / "BOARD.md").read_text().splitlines()
    i = lines.index(BOARD_HEAD) + 2  # skip header + separator
    j = i
    while j < len(lines) and lines[j].startswith("|"):
        j += 1
    rows = []
    for line in lines[i:j]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 5:
            raise ValueError(f"board row needs 5 cells: {line}")
        rows.append(dict(zip(("id", "owner", "state", "next", "updated"), cells)))
    return lines[:i], rows, lines[j:]


def write_board(head, rows, tail) -> None:
    body = [f"| {r['id']} | {r['owner']} | {r['state']} | {r['next'].replace('|', '/')} | {r['updated']} |" for r in rows]
    (ROOT / "BOARD.md").write_text("\n".join(head + body + tail) + "\n")


def update_row(task: str, **changes) -> None:
    head, rows, tail = read_board()
    row = next((r for r in rows if r["id"] == task), None)
    if row is None:
        row = {"id": task, "owner": "-", "state": "open", "next": "-", "updated": ""}
        rows.append(row)
    row.update({k: v for k, v in changes.items() if v is not None})
    row["updated"] = now().date().isoformat()
    write_board(head, rows, tail)


# ---- claims
def claim_path(task: str) -> pathlib.Path:
    return ROOT / "claims" / f"{task}.claim"


def read_claim(task: str) -> dict | None:
    p = claim_path(task)
    return load_flat(p.read_text()) if p.exists() else None


def active(c: dict | None) -> bool:
    return bool(c) and "released_at" not in c


def write_claim(task: str, c: dict) -> None:
    claim_path(task).write_text(dump_flat(c))


def need_id(task: str) -> str:
    if not ID_RE.match(task):
        sys.exit(f"bad task ID {task!r}: use capitals, digits and dashes")
    return task


def cmd_claim(a) -> None:
    c = read_claim(need_id(a.id))
    if active(c) and c["owner"] != a.owner:
        sys.exit(f"{a.id} is already claimed by {c['owner']} since {c['claimed_at']}")
    write_claim(a.id, {"owner": a.owner, "claimed_at": iso(now()), "eta": a.eta, "repo": a.repo})
    update_row(a.id, owner=a.owner, state="claimed")
    print(f"claimed {a.id} for {a.owner}")


def release(task: str, owner: str) -> None:
    c = read_claim(task)
    if not active(c):
        sys.exit(f"{task} has no active claim")
    if c["owner"] != owner:
        sys.exit(f"{task} is claimed by {c['owner']}, not {owner}")
    c["released_at"] = iso(now())
    write_claim(task, c)


def cmd_release(a) -> None:
    release(need_id(a.id), a.owner)
    update_row(a.id, owner="-", state="open")
    print(f"released {a.id}")


def cmd_note(a) -> None:
    need_id(a.id)
    if a.to not in INBOX:
        sys.exit(f"--to must be one of {list(INBOX)}")
    day = now().date().isoformat()
    box = ROOT / INBOX[a.to]
    p, n = box / f"{day}-{a.id}.md", 2
    while p.exists():  # one note per handoff; a second one the same day gets a suffix
        p, n = box / f"{day}-{a.id}-{n}.md", n + 1
    front = {"from": a.sender, "to": a.to, "task": a.id, "status": a.status,
             "verified": a.verified, "unverified": a.unverified, "next_step": a.next}
    errs = check_schema(front, schema("note"))
    if errs:
        sys.exit("note not written: " + "; ".join(errs))
    p.write_text(f"---\n{dump_flat(front)}---\n\n{a.body.strip()}\n")
    c = read_claim(a.id)
    if active(c):
        c["status_at"] = iso(now())  # a note counts as a STATUS change for `stale`
        write_claim(a.id, c)
    update_row(a.id, state=a.status, next=a.next)
    print(p.relative_to(ROOT))


def cmd_done(a) -> None:
    need_id(a.id)
    if not STATUS_RE.match(a.status_line):
        sys.exit("--status-line must start with 'STATUS: '")
    p = ROOT / "done" / f"{a.id}.md"
    if p.exists():
        sys.exit(f"{p.relative_to(ROOT)} already exists; send a note instead")
    p.write_text(f"# {a.id}: done\n\nOwner: {a.owner}\nFinished: {iso(now())}\n\n{a.summary.strip()}\n\n{a.status_line}\n")
    if active(read_claim(a.id)):
        release(a.id, a.owner)
    update_row(a.id, owner=a.owner, state="done", next="-")
    print(p.relative_to(ROOT))


def stale_claims(at: dt.datetime, hours: float = STALE_HOURS) -> list[dict]:
    out = []
    for p in sorted((ROOT / "claims").glob("*.claim")):
        c = load_flat(p.read_text())
        if not active(c):
            continue
        last = max(parse_time(c["claimed_at"]), parse_time(c.get("status_at", c["claimed_at"])))
        age = (at - last).total_seconds() / 3600
        if age > hours:
            out.append({"task": p.stem, "owner": c["owner"], "repo": c["repo"], "hours": round(age, 1)})
    return out


def silent_agents(at: dt.datetime, hours: float | None = None) -> list[dict]:
    """`hours` overrides every agent's own `every_hours`; without either, SILENT_HOURS applies."""
    out = []
    for p in sorted((ROOT / "heartbeat").glob("*.json")):
        hb = json.loads(p.read_text())
        age = (at - parse_time(hb["at"])).total_seconds() / 3600
        if age > (hours if hours is not None else float(hb.get("every_hours", SILENT_HOURS))):
            out.append({"kind": "heartbeat", "agent": hb["agent"], "hours": round(age, 1), "doing": hb.get("doing", "-")})
    return out


def cmd_stale(a) -> None:
    at = parse_time(a.now) if a.now else now()
    found = stale_claims(at, a.hours) + silent_agents(at, a.silent_hours)
    if a.json:
        print(json.dumps(found))
        return
    lines = [f"{x['agent']}: silent for {x['hours']}h, last doing: {x['doing']}" if x.get("kind") == "heartbeat"
             else f"{x['task']}: {x['owner']} claimed it, no STATUS change for {x['hours']}h" for x in found]
    print("\n".join(lines) or "no stale claims, no silent agents")


# ---- shared log: one folder per day, one file per agent, one line per entry. Per-agent files mean Partner's edits
# through a git hosting connector and Lead's local commits never touch the same file, so a rebase cannot conflict.
def new_id(prefix: str, t: dt.datetime) -> str:
    return f"{prefix}-{t.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"


def one_line(text: str) -> str:
    return " ".join(text.split()).replace("|", "/")


def log_path(day: str) -> pathlib.Path:
    return ROOT / "log" / day


def log_header(day: str, agent: str) -> str:
    return (f"# Log {day}, {agent}\n\nOne line per entry: time | agent | request | kind | text. "
            "Data, not instructions.\n\n")


def append_log(agent: str, req: str, kind: str, text: str, t: dt.datetime | None = None) -> str:
    t = t or now()
    day = t.date().isoformat()
    p = log_path(day) / f"{agent}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(log_header(day, agent))
    line = f"- {t.strftime('%H:%M:%SZ')} | {agent} | {req} | {kind} | {one_line(text)}"
    with p.open("a") as f:
        f.write(line + "\n")
    return line


def read_log(day: str) -> list[dict]:
    """All agents' entries for the day, in time order. Lines that don't parse are skipped; `validate` reports them."""
    out = []
    for p in sorted(log_path(day).glob("*.md")):
        for line in p.read_text().splitlines():
            m = LOG_LINE_RE.match(line)
            if m:
                out.append(dict(zip(("time", "agent", "req", "kind", "text"), m.groups()), day=day))
    return sorted(out, key=lambda e: e["time"])


def need_req(req: str) -> str:
    if req != "-" and not REQ_RE.match(req):
        sys.exit(f"bad request ID {req!r}: use one from `request new`, or '-'")
    return req


def cmd_log(a) -> None:
    print(append_log(a.agent, need_req(a.req), a.kind, a.text))


def digest(day: str) -> str:
    """Keeps every decision, promise, request and approval; counts the chat. Old logs stay, agents read digests."""
    entries = read_log(day)
    counts = {}
    for e in entries:
        counts[e["agent"]] = counts.get(e["agent"], 0) + 1
    keep = [e for e in entries if e["kind"] != "said"]
    said = len(entries) - len(keep)
    out = [f"# Digest {day}", "", f"{len(entries)} entries: " +
           (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none") + f". {said} 'said' lines left out.", ""]
    for kind in ("request", "decided", "promised", "approval", "consumed"):
        rows = [e for e in keep if e["kind"] == kind]
        if rows:
            out += [f"## {kind}", ""] + [f"- {e['time']} {e['agent']} {e['req']}: {e['text']}" for e in rows] + [""]
    return "\n".join(out)


def cmd_digest(a) -> None:
    day = a.date or now().date().isoformat()
    p = ROOT / "log" / "digest" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(digest(day) + "\n")
    print(p.relative_to(ROOT))


# ---- requests: every request from the Owner gets an ID, so neither agent acts on it twice
def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def similar_requests(text: str, at: dt.datetime, days: int = 7, cutoff: float = 0.5) -> list[dict]:
    want, out = words(text), []
    for n in range(days + 1):
        day = (at.date() - dt.timedelta(days=n)).isoformat()
        entries = read_log(day)
        for e in (e for e in entries if e["kind"] == "request"):
            have = words(e["text"])
            score = len(want & have) / len(want | have) if want | have else 0.0
            if score >= cutoff:
                last = [x for x in entries if x["req"] == e["req"] and x["kind"] != "request"][-1:]
                out.append({**e, "score": round(score, 2), "last": last[0] if last else None})
    return sorted(out, key=lambda e: -e["score"])


def show_matches(found: list[dict]) -> None:
    for e in found:
        last = f"; last: {e['last']['kind']} by {e['last']['agent']}: {e['last']['text']}" if e["last"] else "; not acted on yet"
        print(f"likely duplicate of {e['req']} ({e['agent']}, {e['day']} {e['time']}, {e['score']}): {e['text']}{last}")


def cmd_request_new(a) -> None:
    t = now()
    found = similar_requests(a.text, t)
    if found:  # warn only: the Owner may repeat a request on purpose
        show_matches(found)
    req = new_id("R", t)
    append_log(a.agent, req, "request", a.text, t)
    print(req)


def cmd_request_seen(a) -> None:
    found = similar_requests(a.text, parse_time(a.now) if a.now else now(), a.days)
    show_matches(found)
    if not found:
        print("no match")
    sys.exit(1 if found else 0)


# ---- approvals: the Owner's OK, logged with exact scope and expiry; either agent may use it once
def approval_path(aid: str) -> pathlib.Path:
    return ROOT / "approvals" / f"{aid}.approval"


def parse_expiry(s: str, t: dt.datetime) -> dt.datetime:
    m = re.fullmatch(r"(\d+)([mhd])", s)
    if m:
        unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return t + dt.timedelta(**{unit: int(m.group(1))})
    return parse_time(s)


def scope_key(s: str) -> str:
    return " ".join(s.split())


def risk_of(scope: str) -> tuple[str, str]:
    """('high' or 'low', why)."""
    m = ALWAYS_HIGH.search(scope)
    if m:
        return "high", f"high-risk action ({m.group(0)!r})"
    if LOW_VERB_FIRST.match(scope.strip()):
        return "low", "starts with a low-risk verb"
    m = HIGH_UNLESS_LOW_VERB.search(scope)
    if m:
        return "high", f"high-risk action ({m.group(0)!r})"
    if LOW_ANYWHERE.search(scope):
        return "low", "board, note, log or local branch"
    return "high", "not clearly low-risk (reads, drafts, board edits, local branches)"


def approval_risk(ap: dict, scope: str) -> str | None:
    """None if the approval may be used for this scope; else the NEEDS-CONFIRM reason."""
    if ap.get("recorded_by") != "Partner":
        return None
    if "chat_time" not in ap:
        return "recorded by Partner without the Owner's chat_time"
    level, why = risk_of(scope)
    return f"{why}, recorded by Partner" if level == "high" else None


def cmd_approve(a) -> None:
    t = now()
    exp = parse_expiry(a.expires, t)
    if not t < exp <= t + MAX_APPROVAL:
        sys.exit(f"expiry must be in the future and at most {MAX_APPROVAL.days} days away")
    if not a.quote.strip():
        sys.exit("--quote must hold the Owner's own words")
    if a.agent == "Partner" and not a.chat_time:
        sys.exit("an approval recorded by Partner needs --chat-time: when the Owner said it, in their chat")
    aid = new_id("A", t)
    approval_path(aid).parent.mkdir(exist_ok=True)
    approval_path(aid).write_text(dump_flat({
        "id": aid, "scope": scope_key(a.scope), "granted_by": "Owner", "recorded_by": a.agent,
        "granted_at": iso(t), "expires_at": iso(exp), "single_use": "true", "request": need_req(a.req),
        "quote": one_line(a.quote), **({"chat_time": iso(parse_time(a.chat_time))} if a.chat_time else {})}))
    append_log(a.agent, a.req, "approval", f"{aid} scope={scope_key(a.scope)!r} expires {iso(exp)}", t)
    print(aid)


def approval_problem(aid: str, scope: str, at: dt.datetime) -> tuple[dict | None, str | None]:
    if not APPROVAL_RE.match(aid) or not approval_path(aid).exists():
        return None, f"no approval {aid}"
    ap = load_flat(approval_path(aid).read_text())
    if "consumed_at" in ap:
        return ap, f"already used by {ap['consumed_by']} at {ap['consumed_at']}"
    if at >= parse_time(ap["expires_at"]):
        return ap, f"expired at {ap['expires_at']}"
    if scope_key(scope) != ap["scope"]:
        return ap, f"scope differs: approved {ap['scope']!r}, asked {scope_key(scope)!r}"
    return ap, None


def cmd_approval_check(a) -> None:
    """Exit 0 valid, 1 not valid, 2 NEEDS-CONFIRM (valid record, but the Owner must confirm again or Lead record it)."""
    ap, why = approval_problem(a.id, a.scope, parse_time(a.now) if a.now else now())
    if why:
        print(why)
        sys.exit(1)
    risk = approval_risk(ap, a.scope)
    if risk:
        print(f"NEEDS-CONFIRM: {risk}. Get a fresh OK from the Owner, or an approval that Lead recorded.")
        sys.exit(2)
    print(f"valid: {a.id} covers {scope_key(a.scope)!r} once")
    sys.exit(0)


def cmd_approval_consume(a) -> None:
    t = now()
    ap, why = approval_problem(a.id, a.scope, t)
    if why:
        sys.exit(f"not consumed: {why}")
    risk = approval_risk(ap, a.scope)
    if risk:
        sys.exit(f"not consumed: NEEDS-CONFIRM: {risk}")
    ap.update(consumed_by=a.agent, consumed_at=iso(t))
    approval_path(a.id).write_text(dump_flat(ap))
    append_log(a.agent, ap["request"], "consumed", f"{a.id} scope={ap['scope']!r}", t)
    print(f"consumed {a.id}: commit and push this before you act, so the other agent cannot use it too")


# ---- heartbeat: each agent stamps its own file; `stale` reports the silent ones
def cmd_heartbeat(a) -> None:
    p = ROOT / "heartbeat" / f"{a.agent}.json"
    p.parent.mkdir(exist_ok=True)
    hb = {"agent": a.agent, "at": iso(now()), "doing": one_line(a.doing)}
    if a.every_hours:
        hb["every_hours"] = a.every_hours
    p.write_text(json.dumps(hb, indent=2) + "\n")
    print(p.relative_to(ROOT))


def cmd_sync(a) -> None:
    r = subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase"], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"git pull --rebase failed, fix it before you reply:\n{r.stderr.strip()}")
    errs = validate()
    print("\n".join(errs) or "synced and valid")
    sys.exit(1 if errs else 0)


def validate() -> list[str]:
    errs = []
    cs, ns = schema("claim"), schema("note")
    for p in sorted((ROOT / "claims").glob("*.claim")):
        try:
            errs += [f"claims/{p.name}: {e}" for e in check_schema(load_flat(p.read_text()), cs)]
        except ValueError as e:
            errs.append(f"claims/{p.name}: {e}")
    for box in INBOX.values():
        for p in sorted((ROOT / box).glob("*.md")):
            if not re.match(r"^\d{4}-\d{2}-\d{2}-[A-Z0-9-]+\.md$", p.name):
                errs.append(f"{box}/{p.name}: name must be <date>-<ID>.md")
            try:
                front, _ = split_front(p.read_text())
                errs += [f"{box}/{p.name}: {e}" for e in check_schema(front, ns)]
                if INBOX.get(front.get("to")) not in (None, box):
                    errs.append(f"{box}/{p.name}: 'to' is {front['to']} but the note sits in {box}/")
            except ValueError as e:
                errs.append(f"{box}/{p.name}: {e}")
    for p in sorted((ROOT / "done").glob("*.md")):
        last = [l for l in p.read_text().splitlines() if l.strip()][-1:]
        if not last or not STATUS_RE.match(last[0]):
            errs.append(f"done/{p.name}: must end with a 'STATUS: ' line")
    errs += validate_approvals() + validate_heartbeats() + validate_logs()
    try:
        _, rows, _ = read_board()
        for r in rows:
            if not ID_RE.match(r["id"]):
                errs.append(f"BOARD.md: bad ID {r['id']!r}")
    except ValueError as e:
        errs.append(f"BOARD.md: {e}")
    return errs


def validate_approvals() -> list[str]:
    errs, sch = [], schema("approval")
    for p in sorted((ROOT / "approvals").glob("*")):
        where = f"approvals/{p.name}"
        if p.suffix != ".approval":
            errs.append(f"{where}: name must be <A-ID>.approval")
            continue
        try:
            ap = load_flat(p.read_text())
        except ValueError as e:
            errs.append(f"{where}: {e}")
            continue
        found = check_schema(ap, sch)
        errs += [f"{where}: {e}" for e in found]
        if ap.get("granted_by") != "Owner":
            errs.append(f"{where}: only the Owner grants approvals")
        if ap.get("id") and ap["id"] != p.stem:
            errs.append(f"{where}: id {ap['id']!r} does not match the file name")
        if ap.get("recorded_by") == "Partner" and "chat_time" not in ap:
            errs.append(f"{where}: recorded by Partner, so it needs chat_time (when the Owner said it)")
        if ("consumed_by" in ap) != ("consumed_at" in ap):
            errs.append(f"{where}: consumed_by and consumed_at go together")
        if not found:
            granted, expires = parse_time(ap["granted_at"]), parse_time(ap["expires_at"])
            if not granted < expires <= granted + MAX_APPROVAL:
                errs.append(f"{where}: expires_at must be after granted_at and at most {MAX_APPROVAL.days} days later")
    return errs


def validate_heartbeats() -> list[str]:
    errs = []
    for p in sorted((ROOT / "heartbeat").glob("*")):
        where = f"heartbeat/{p.name}"
        try:
            hb = json.loads(p.read_text())
            if p.suffix != ".json" or p.stem not in AGENTS:
                errs.append(f"{where}: name must be <agent>.json, agent one of {list(AGENTS)}")
            if hb.get("agent") != p.stem:
                errs.append(f"{where}: 'agent' must be {p.stem!r}")
            if parse_time(hb["at"]) > now() + dt.timedelta(minutes=10):
                errs.append(f"{where}: 'at' is in the future; use UTC with a Z")
            every = hb.get("every_hours", SILENT_HOURS)
            if isinstance(every, bool) or not isinstance(every, (int, float)) or not 0 < every <= 24:
                errs.append(f"{where}: 'every_hours' must be a number from 0 to 24")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            errs.append(f"{where}: not a valid heartbeat ({type(e).__name__}: {e})")
    return errs


def validate_logs() -> list[str]:
    errs = []
    for d in sorted((ROOT / "log").glob("*")):
        if d.name == "digest":
            continue
        if not d.is_dir() or not DAY_RE.match(d.name):
            errs.append(f"log/{d.name}: log entries go in log/<YYYY-MM-DD>/<agent>.md")
            continue
        for p in sorted(d.iterdir()):
            where = f"log/{d.name}/{p.name}"
            if p.suffix != ".md" or p.stem not in AGENTS:
                errs.append(f"{where}: name must be <agent>.md, agent one of {list(AGENTS)}")
                continue
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if not line.strip() or line.startswith("# Log ") or line.startswith("One line per entry"):
                    continue
                m = LOG_LINE_RE.match(line)
                if not m:
                    errs.append(f"{where}:{n}: not a log line: {line[:60]!r}")
                    continue
                _, agent, req, kind, _ = m.groups()
                if agent != p.stem:
                    errs.append(f"{where}:{n}: agent {agent!r} writes only to its own file")
                if req != "-" and not REQ_RE.match(req):
                    errs.append(f"{where}:{n}: bad request ID {req!r}")
                if kind not in ALL_KINDS:
                    errs.append(f"{where}:{n}: kind {kind!r} not in {list(ALL_KINDS)}")
    return errs


# Hints for `validate --fix-report`, matched against each error. The README has the full templates.
FIX_HINTS = (
    ("not a log line", "write `- HH:MM:SSZ | <agent> | <R-ID or -> | <kind> | <text>`, one entry per line"),
    ("writes only to its own file", "move the line to log/<date>/<that agent>.md"),
    ("log entries go in", "make the folder log/<YYYY-MM-DD>/ and one file per agent inside it"),
    ("bad request ID", "use R-YYYYMMDD-HHMMSS-XXXX (4 capital hex digits) or '-'"),
    ("kind ", "use one of: " + ", ".join(ALL_KINDS)),
    ("only the Owner grants", "set granted_by: \"Owner\"; only record an OK that the Owner gave in their own chat"),
    ("does not match the file name", "rename the file to <id>.approval or fix the id line"),
    ("go together", "add both consumed_by and consumed_at, or neither"),
    ("needs chat_time", "add chat_time: the UTC time of the Owner's message, like 2026-10-27T16:00:00Z"),
    ("expires_at must be", "set expires_at after granted_at and at most 7 days later"),
    ("is in the future", "write times in UTC, like 2026-10-27T16:00:00Z"),
    ("every_hours", "write a plain number, for example 6"),
    ("not a valid heartbeat", "use {\"agent\": ..., \"at\": \"<UTC time>Z\", \"doing\": ..., \"every_hours\": 6}"),
    ("but the note sits in", "move the note to the inbox of its 'to' agent"),
    ("name must be <date>-<ID>.md", "rename to YYYY-MM-DD-<TASK-ID>.md"),
    ("no front matter", "start the file with the --- front matter block from the README"),
    ("missing '", "add the missing field; see the template in the README"),
    ("not in [", "use one of the listed values"),
    ("is not an ISO date-time", "write times in UTC, like 2026-10-27T16:00:00Z"),
    ("must end with a 'STATUS: ' line", "add a last line: STATUS: <state>, <what is next>"),
)


def fix_report(errs: list[str]) -> str:
    if not errs:
        return "# Validate report\n\nAll bridge files are valid."
    out = ["# Validate report", "", f"{len(errs)} problem(s). Nothing was changed; the author fixes their own file.", ""]
    for e in errs:
        hint = next((h for key, h in FIX_HINTS if key in e), "compare the file with its template in the README")
        out.append(f"- `{e.split(': ', 1)[0]}`: {e.split(': ', 1)[-1]}. **Fix:** {hint}.")
    return "\n".join(out)


def staged_paths() -> set[str]:
    r = subprocess.run(["git", "-C", str(ROOT), "diff", "--cached", "--name-only"], capture_output=True, text=True)
    return set(r.stdout.split())


def error_path(err: str) -> str:
    return re.match(r"^[^:]+", err).group(0)


def cmd_validate(a) -> None:
    errs = validate()
    if a.staged:  # the pre-commit hook: one writer's bad file must not block another writer's good commit
        staged = staged_paths()
        errs = [e for e in errs if error_path(e) in staged]
    print(fix_report(errs) if a.fix_report else "\n".join(errs) or "valid")
    sys.exit(1 if errs else 0)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="bridge", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("claim"); s.add_argument("id"); s.add_argument("--owner", required=True, choices=AGENTS)
    s.add_argument("--repo", required=True); s.add_argument("--eta", required=True); s.set_defaults(f=cmd_claim)
    s = sub.add_parser("release"); s.add_argument("id"); s.add_argument("--owner", required=True, choices=AGENTS)
    s.set_defaults(f=cmd_release)
    s = sub.add_parser("note"); s.add_argument("id"); s.add_argument("--from", dest="sender", required=True, choices=AGENTS)
    s.add_argument("--to", required=True, choices=list(INBOX)); s.add_argument("--status", required=True)
    s.add_argument("--verified", default="-"); s.add_argument("--unverified", default="-")
    s.add_argument("--next", required=True); s.add_argument("--body", default=""); s.set_defaults(f=cmd_note)
    s = sub.add_parser("done"); s.add_argument("id"); s.add_argument("--owner", required=True, choices=AGENTS)
    s.add_argument("--summary", required=True); s.add_argument("--status-line", required=True); s.set_defaults(f=cmd_done)
    sub.add_parser("board").set_defaults(f=lambda a: print((ROOT / "BOARD.md").read_text(), end=""))
    s = sub.add_parser("stale"); s.add_argument("--hours", type=float, default=STALE_HOURS)
    s.add_argument("--silent-hours", type=float, help="override each heartbeat's own every_hours")
    s.add_argument("--now", help="ISO time, for tests"); s.add_argument("--json", action="store_true")
    s.set_defaults(f=cmd_stale)
    s = sub.add_parser("validate"); s.add_argument("--fix-report", action="store_true",
                                                   help="Markdown report with a fix hint per problem")
    s.add_argument("--staged", action="store_true", help="only report files staged for commit (pre-commit hook)")
    s.set_defaults(f=cmd_validate)
    sub.add_parser("sync", help="git pull --rebase, then validate").set_defaults(f=cmd_sync)
    s = sub.add_parser("log"); s.add_argument("kind", choices=LOG_KINDS); s.add_argument("text")
    s.add_argument("--agent", required=True, choices=AGENTS); s.add_argument("--req", default="-")
    s.set_defaults(f=cmd_log)
    s = sub.add_parser("digest"); s.add_argument("--date", help="YYYY-MM-DD, default today"); s.set_defaults(f=cmd_digest)
    req = sub.add_parser("request").add_subparsers(dest="rcmd", required=True)
    s = req.add_parser("new"); s.add_argument("text"); s.add_argument("--agent", required=True, choices=AGENTS)
    s.set_defaults(f=cmd_request_new)
    s = req.add_parser("seen"); s.add_argument("text"); s.add_argument("--days", type=int, default=7)
    s.add_argument("--now", help="ISO time, for tests"); s.set_defaults(f=cmd_request_seen)
    s = sub.add_parser("approve"); s.add_argument("--scope", required=True); s.add_argument("--expires", required=True,
                                                                                         help="30m, 2h, 1d or ISO")
    s.add_argument("--quote", required=True, help="the Owner's own words"); s.add_argument("--req", default="-")
    s.add_argument("--chat-time", help="UTC time of the Owner's message; required when Partner records it")
    s.add_argument("--agent", required=True, choices=AGENTS); s.set_defaults(f=cmd_approve)
    apr = sub.add_parser("approval").add_subparsers(dest="acmd", required=True)
    s = apr.add_parser("check"); s.add_argument("id"); s.add_argument("--scope", required=True)
    s.add_argument("--now", help="ISO time, for tests"); s.set_defaults(f=cmd_approval_check)
    s = apr.add_parser("consume"); s.add_argument("id"); s.add_argument("--scope", required=True)
    s.add_argument("--agent", required=True, choices=AGENTS); s.set_defaults(f=cmd_approval_consume)
    s = sub.add_parser("heartbeat"); s.add_argument("--agent", required=True, choices=AGENTS)
    s.add_argument("--doing", default="-"); s.add_argument("--every-hours", type=float,
                                                        help="how often this agent checks in; Partner: 6")
    s.set_defaults(f=cmd_heartbeat)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
