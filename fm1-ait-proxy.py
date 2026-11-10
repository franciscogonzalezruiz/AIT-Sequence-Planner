#!/usr/bin/env python3
"""Local-only YouTrack proxy for the FM1 AIT dashboard (FDI + MAIT-style planning).

Runs on 127.0.0.1:8767 (the previous FM1 dashboard keeps 8766, so both can run).
Open http://127.0.0.1:8767/ - the proxy serves fm1-ait-dashboard.html.

Write rules (enforced here, not only in the HTML):
- Only project FDI. Stream and Block issues are never edited or moved.
- Only the fields in WRITABLE can be changed. Issues are never deleted.
  The only thing that can be removed is a "depends on" link, and only when you ask.
- Field edits check that nobody changed the issue since the dashboard loaded it
  (conflict -> HTTP 409, nothing written). Date sync does the same per issue.
- Every write is re-read and verified where possible, and logged:
  %LOCALAPPDATA%\\FM1 AIT Dashboard\\write-log.txt  (field edits / creates)
  fm1-sync-log.jsonl next to this file                   (links, date sync)
- The working calendar (company closures / extra open days) is saved in
  fm1-working-calendar.json next to this file.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST = "127.0.0.1"
PORT = 8767
YOUTRACK_URL = "https://youtrack.exploration.ninja"
PROJECT_KEY = "FDI"
PROJECT_QUERY = "project: FDI"
PAGE_SIZE = 100
PROGRAM_QUERY = 'project: NYXE summary: "MAIT FM Stream"'
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = "fm1-ait-dashboard.html"
VALUE_FIELDS = "name,$type,value(name,login,fullName,presentation,text,minutes)"
LINK_FIELDS = "links(direction,linkType(name,sourceToTarget,targetToSource),issues(idReadable))"
FIELDS = f"idReadable,summary,description,customFields({VALUE_FIELDS}),{LINK_FIELDS}"
PROGRAM_FIELDS = f"idReadable,summary,customFields({VALUE_FIELDS})"
TOKEN_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "FM1 AIT Dashboard")
TOKEN_FILE = os.path.join(TOKEN_DIR, "youtrack-token.txt")      # shared with the previous dashboard
LOG_FILE = os.path.join(TOKEN_DIR, "write-log.txt")
CALENDAR_FILE = os.path.join(HERE, "fm1-working-calendar.json")
SYNC_LOG_FILE = os.path.join(HERE, "fm1-sync-log.jsonl")
CALENDAR_TYPES = {"closed", "open"}
MAX_SYNC_UPDATES = 200
FDI_ID = re.compile(r"FDI-\d+")

# Fields the dashboard may write. Only those that exist in FDI are offered.
WRITABLE = [
    "State", "Task Type", "Team Assigned", "Stream", "Block", "Total People Required",
    "Total Effort (h)", "Duration (wh)", "Start Date and Time", "End Date and Time",
    "Scheduling Rule", "Rescheduling Rule", "Dependency Rules",
    "Techs need (nb of people)", "Assign Team need (nb of people)",
    "Specimen", "Safety Impact", "Critical mean", "NCR Number", "Activity Type",
]
PROTECTED_TASK_TYPES = {"stream", "block"}
DATE_FIELDS = ("Start Date and Time", "End Date and Time")
NUMBER_FIELDS = ("Total People Required", "Total Effort (h)", "Duration (wh)",
                 "Techs need (nb of people)", "Assign Team need (nb of people)")
ISSUE_TYPE_BY_FIELD_TYPE = {
    "enum[1]": "SingleEnumIssueCustomField", "enum[*]": "MultiEnumIssueCustomField",
    "state[1]": "StateIssueCustomField",
    "ownedField[1]": "SingleOwnedIssueCustomField", "ownedField[*]": "MultiOwnedIssueCustomField",
    "user[1]": "SingleUserIssueCustomField", "user[*]": "MultiUserIssueCustomField",
    "version[1]": "SingleVersionIssueCustomField", "version[*]": "MultiVersionIssueCustomField",
    "build[1]": "SingleBuildIssueCustomField", "build[*]": "MultiBuildIssueCustomField",
    "group[1]": "SingleGroupIssueCustomField", "group[*]": "MultiGroupIssueCustomField",
    "date": "DateIssueCustomField", "date and time": "DateIssueCustomField",
    "integer": "SimpleIssueCustomField", "float": "SimpleIssueCustomField", "string": "SimpleIssueCustomField",
    "period": "PeriodIssueCustomField", "text": "TextIssueCustomField",
}
FIELD_KEYS = {
    "State": "state", "Task Type": "taskType", "Team Assigned": "team", "Stream": "stream", "Block": "block",
    "Total People Required": "people", "Total Effort (h)": "effort", "Duration (wh)": "duration",
    "Start Date and Time": "startMs", "End Date and Time": "endMs",
    "Scheduling Rule": "schedulingRule", "Rescheduling Rule": "reschedulingRule",
    "Dependency Rules": "dependencyRules", "Techs need (nb of people)": "techsNeed",
    "Assign Team need (nb of people)": "teamNeed", "Specimen": "specimen", "Safety Impact": "safetyImpact",
    "Critical mean": "criticalMean", "NCR Number": "ncr", "Activity Type": "activityType",
}
_SCHEMA_CACHE = {"at": 0, "token": None, "value": None}


class Conflict(Exception):
    def __init__(self, message, current):
        super().__init__(message)
        self.current = current


# ---------------------------------------------------------------- token
def saved_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def store_token(token):
    os.makedirs(TOKEN_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as handle:
        handle.write(token.strip())


def clear_token():
    try:
        os.remove(TOKEN_FILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- Europe/Berlin without tzdata
def _last_sunday(year, month):
    d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1) if month < 12 else datetime(year, 12, 31, tzinfo=timezone.utc)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def berlin_offset(utc_dt):
    """CET/CEST: +1h, +2h between last Sunday of March 01:00 UTC and last Sunday of October 01:00 UTC."""
    y = utc_dt.year
    start = _last_sunday(y, 3).replace(hour=1)
    end = _last_sunday(y, 10).replace(hour=1)
    return timedelta(hours=2 if start <= utc_dt < end else 1)


def berlin_date(ms):
    utc = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return (utc + berlin_offset(utc)).date().isoformat()


# ---------------------------------------------------------------- normalising
def value_text(value):
    if value is None:
        return None
    if isinstance(value, list):
        values = [value_text(item) for item in value]
        values = [item for item in values if item not in (None, "")]
        return ", ".join(str(item) for item in values) if values else None
    if isinstance(value, dict):
        for key in ("name", "login", "fullName", "presentation", "text", "value"):
            if value.get(key) is not None:
                return value[key]
        if value.get("minutes") is not None:
            return value["minutes"]
        return None
    return value


def to_ms(value):
    """Return epoch ms for a YouTrack date / date-time value, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"-?\d{10,}", text):
        return int(text)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if len(text) <= 10:
                parsed = parsed.replace(hour=12, tzinfo=timezone.utc)
            else:
                raise ValueError("naive date-time")
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def normalise_number(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def link_label(link):
    lt = link.get("linkType") or {}
    direction = str(link.get("direction") or "").upper()
    label = lt.get("targetToSource") if direction == "INWARD" else lt.get("sourceToTarget")
    return " ".join(str(label or lt.get("name") or "").strip().lower().split())


def links_of(issue):
    preds, succs, parents = [], [], []
    for link in issue.get("links") or []:
        label = link_label(link)
        for other in link.get("issues") or []:
            oid = other.get("idReadable")
            if not oid:
                continue
            if label == "depends on":
                preds.append(oid)
            elif label == "is required for":
                succs.append(oid)
            elif label == "subtask of":
                parents.append(oid)
    return sorted(set(preds)), sorted(set(succs)), sorted(set(parents))


def is_protected(issue):
    t = str(issue.get("taskType") or "").strip().lower()
    if t in PROTECTED_TASK_TYPES:
        return True
    return not issue.get("block") and bool(re.match(r"^\s*Stream\s+[A-Z]\b", issue.get("summary") or "", re.I))


def normalise_issue(issue):
    fields, raw = {}, {}
    for custom in issue.get("customFields", []):
        fields[custom.get("name")] = value_text(custom.get("value"))
        raw[custom.get("name")] = custom.get("value")
    issue_id = issue.get("idReadable") or issue.get("id")
    start_ms = to_ms(fields.get("Start Date and Time") if "Start Date and Time" in fields else fields.get("Baseline Start Date"))
    end_ms = to_ms(fields.get("End Date and Time") if "End Date and Time" in fields else fields.get("Baseline End Date"))
    duration = fields.get("Duration (wh)")
    preds, succs, parents = links_of(issue)
    out = {
        "id": issue_id,
        "url": f"{YOUTRACK_URL}/issue/{issue_id}",
        "summary": issue.get("summary") or "Untitled",
        "description": issue.get("description") or "",
        "state": fields.get("State"),
        "taskType": fields.get("Task Type") or "",
        "team": fields.get("Team Assigned"),
        "stream": fields.get("Stream") or "",
        "block": fields.get("Block"),
        "people": normalise_number(fields.get("Total People Required")),
        "effort": normalise_number(fields.get("Total Effort (h)")),
        "duration": normalise_number(duration),
        "startMs": start_ms,
        "endMs": end_ms,
        "start": berlin_date(start_ms) if start_ms is not None else None,
        "end": berlin_date(end_ms) if end_ms is not None else None,
        "schedulingRule": fields.get("Scheduling Rule") or "",
        "reschedulingRule": fields.get("Rescheduling Rule") or "",
        "dependencyRules": fields.get("Dependency Rules") or "",
        "techsNeed": normalise_number(fields.get("Techs need (nb of people)")),
        "teamNeed": normalise_number(fields.get("Assign Team need (nb of people)")),
        "specimen": fields.get("Specimen") or "",
        "safetyImpact": fields.get("Safety Impact") or "",
        "criticalMean": fields.get("Critical mean") or "",
        "ncr": fields.get("NCR Number") or "",
        "activityType": fields.get("Activity Type") or "",
        "assignees": fields.get("Assignees") or "",
        "predecessors": fields.get("Predecessors") or "",
        "dependencies": [p for p in preds],
        "successors": succs,
        "parents": parents,
        "source": "live",
    }
    out["protected"] = is_protected(out)
    return out


def normalise_program(issue):
    fields = {}
    for custom in issue.get("customFields", []):
        fields[custom.get("name")] = value_text(custom.get("value"))
    issue_id = issue.get("idReadable") or issue.get("id")
    s, d = to_ms(fields.get("Start Date")), to_ms(fields.get("Due Date"))
    return {
        "id": issue_id, "url": f"{YOUTRACK_URL}/issue/{issue_id}", "summary": issue.get("summary") or "",
        "type": fields.get("Type") or "", "state": fields.get("State") or "",
        "start": datetime.fromtimestamp(s / 1000, tz=timezone.utc).date().isoformat() if s is not None else None,
        "due": datetime.fromtimestamp(d / 1000, tz=timezone.utc).date().isoformat() if d is not None else None,
    }


# ---------------------------------------------------------------- HTTP to YouTrack
def yt(token, method, path, params=None, body=None):
    url = f"{YOUTRACK_URL}{path}" + (f"?{urlencode(params)}" if params else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Cache-Control": "no-cache"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:600]
        if error.code in (401, 403):
            raise RuntimeError(f"YouTrack refused the request (HTTP {error.code}). Check that your token is valid and has permission in {PROJECT_KEY}. Details: {detail}") from error
        raise RuntimeError(f"YouTrack HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach YouTrack: {error.reason}") from error


def get_all_issues(token, query, fields):
    issues, skip = [], 0
    while True:
        page = yt(token, "GET", "/api/issues", {"query": query, "fields": fields, "$top": str(PAGE_SIZE), "$skip": str(skip)})
        if not isinstance(page, list):
            raise RuntimeError("YouTrack returned an unexpected response")
        issues.extend(page)
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return issues


def write_log(line):
    try:
        os.makedirs(TOKEN_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(datetime.now().isoformat(timespec="seconds") + " " + line + "\n")
    except OSError:
        pass
    print("[write] " + line)
    sys.stdout.flush()


def write_sync_log(entry):
    try:
        with open(SYNC_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- schema
def _norm_field(item):
    pcf = item.get("projectCustomField") or item
    field = pcf.get("field") or {}
    name = field.get("name") or item.get("name")
    ftype = ((field.get("fieldType") or {}).get("id")) or ""
    bundle = pcf.get("bundle") or {}
    values = [v.get("name") for v in (bundle.get("values") or []) if v.get("name") and not v.get("archived")]
    return name, {"type": ftype, "values": values, "canBeEmpty": pcf.get("canBeEmpty", True)}


def get_schema(token):
    now = time.time()
    if _SCHEMA_CACHE["value"] and _SCHEMA_CACHE["token"] == token and now - _SCHEMA_CACHE["at"] < 300:
        return _SCHEMA_CACHE["value"]
    first = yt(token, "GET", "/api/issues", {"query": PROJECT_QUERY, "fields": "idReadable,project(id,shortName)", "$top": "1"})
    if not first:
        raise RuntimeError(f"Project {PROJECT_KEY} has no readable issues for this token")
    project_id = first[0]["project"]["id"]
    sub = "field(name,fieldType(id)),canBeEmpty,bundle(values(name,archived))"
    fields = {}
    try:
        for item in yt(token, "GET", f"/api/admin/projects/{project_id}/customFields", {"fields": sub, "$top": "200"}):
            name, info = _norm_field(item)
            if name:
                fields[name] = info
    except RuntimeError:
        for item in yt(token, "GET", f"/api/issues/{first[0]['idReadable']}/customFields", {"fields": "name,projectCustomField(" + sub + ")"}):
            name, info = _norm_field(item)
            if name:
                fields[name] = info
    try:
        me = yt(token, "GET", "/api/users/me", {"fields": "login,fullName"})
    except RuntimeError:
        me = {}
    value = {"projectId": project_id, "fields": fields, "writable": [f for f in WRITABLE if f in fields],
             "me": me.get("login") or "", "meName": me.get("fullName") or ""}
    _SCHEMA_CACHE.update({"at": now, "token": token, "value": value})
    return value


# ---------------------------------------------------------------- field encoding
def encode_field(name, raw, info):
    ftype = info["type"]
    issue_type = ISSUE_TYPE_BY_FIELD_TYPE.get(ftype)
    if not issue_type:
        raise RuntimeError(f"Field '{name}' has type '{ftype}', which this dashboard cannot write")
    multi = ftype.endswith("[*]")
    if raw is None or raw == "" or raw == []:
        return {"name": name, "$type": issue_type, "value": [] if multi else None}
    if ftype in ("date", "date and time"):
        ms = to_ms(raw)
        if ms is None:
            raise RuntimeError(f"'{name}': '{raw}' is not a valid date/time")
        return {"name": name, "$type": issue_type, "value": ms}
    if ftype in ("integer", "float"):
        num = normalise_number(raw)
        if num is None or num < 0:
            raise RuntimeError(f"'{name}': '{raw}' is not a valid non-negative number")
        return {"name": name, "$type": issue_type, "value": int(round(num)) if ftype == "integer" else num}
    if ftype == "string":
        return {"name": name, "$type": issue_type, "value": str(raw).strip()}
    if ftype == "text":
        return {"name": name, "$type": issue_type, "value": {"text": str(raw)}}
    if ftype == "period":
        num = normalise_number(raw)
        if num is None or num < 0:
            raise RuntimeError(f"'{name}': '{raw}' is not a valid number of hours")
        return {"name": name, "$type": issue_type, "value": {"minutes": int(round(num * 60))}}
    key = "login" if ftype.startswith("user") else "name"
    items = raw if isinstance(raw, list) else ([x.strip() for x in str(raw).split(",") if x.strip()] if multi else [str(raw).strip()])
    allowed = info.get("values") or []
    if allowed and key == "name":
        bad = [x for x in items if x not in allowed]
        if bad:
            raise RuntimeError(f"'{name}': value(s) {bad} do not exist in YouTrack. Allowed: {allowed}")
    values = [{key: x} for x in items]
    return {"name": name, "$type": issue_type, "value": values if multi else values[0]}


def comparable(name, value):
    if name in DATE_FIELDS:
        ms = to_ms(value)
        return "" if ms is None else ms // 60000
    if name in NUMBER_FIELDS:
        num = normalise_number(value)
        return "" if num is None else round(num, 4)
    if isinstance(value, list):
        return ", ".join(sorted(str(v) for v in value))
    text = "" if value is None else str(value)
    return ", ".join(sorted(x.strip() for x in text.split(","))) if "," in text else text.strip()


def read_issue(token, issue_id):
    raw = yt(token, "GET", f"/api/issues/{quote(issue_id)}", {"fields": FIELDS + ",id,project(id,shortName)"})
    return raw, normalise_issue(raw)


def verify(token, issue_id, wanted):
    _, issue = read_issue(token, issue_id)
    mismatches = []
    for name, value in wanted.items():
        key = FIELD_KEYS.get(name)
        if key and comparable(name, issue.get(key)) != comparable(name, value):
            mismatches.append(f"{name}: wanted '{value}', YouTrack has '{issue.get(key)}'")
    return issue, mismatches


def clean_changes(changes, schema):
    out = {}
    for name, value in (changes or {}).items():
        if name not in schema["writable"]:
            raise RuntimeError(f"Field '{name}' cannot be changed from the dashboard")
        if name == "Task Type" and str(value or "").strip().lower() in PROTECTED_TASK_TYPES:
            raise RuntimeError("Stream and Block issues cannot be created from the dashboard")
        out[name] = value
    s, e = to_ms(out.get("Start Date and Time")), to_ms(out.get("End Date and Time"))
    if s is not None and e is not None and e < s:
        raise RuntimeError("End Date and Time is before Start Date and Time")
    return out


def check_fdi(issue_id):
    if not FDI_ID.fullmatch(str(issue_id or "")):
        raise ValueError(f"Only {PROJECT_KEY} issues can be used here (got '{issue_id}')")


def editable_current(token, issue_id):
    check_fdi(issue_id)
    raw, current = read_issue(token, issue_id)
    if (raw.get("project") or {}).get("shortName") != PROJECT_KEY:
        raise RuntimeError(f"{issue_id} is not in project {PROJECT_KEY}")
    if current["protected"]:
        raise RuntimeError(f"{issue_id} is a Stream/Block issue; it is not edited from the dashboard")
    return raw, current


def create_activity(token, body):
    schema = get_schema(token)
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise RuntimeError("Summary is required")
    changes = clean_changes(body.get("fields"), schema)
    custom = [encode_field(n, v, schema["fields"][n]) for n, v in changes.items()]
    payload = {"project": {"id": schema["projectId"]}, "summary": summary, "customFields": custom}
    if body.get("description"):
        payload["description"] = str(body["description"])
    created = yt(token, "POST", "/api/issues", {"fields": "idReadable"}, payload)
    issue_id = created.get("idReadable")
    if not issue_id:
        raise RuntimeError("YouTrack did not return the new issue id")
    write_log(f"CREATE {issue_id} by {schema['me']} summary={summary!r} fields={json.dumps(changes, ensure_ascii=False)}")
    issue, mismatches = verify(token, issue_id, changes)
    if mismatches:
        write_log(f"VERIFY {issue_id} mismatches: {mismatches}")
    return {"issue": issue, "mismatches": mismatches, "by": schema["me"]}


def update_activity(token, body):
    schema = get_schema(token)
    issue_id = str(body.get("id") or "").strip()
    _, current = editable_current(token, issue_id)
    changes = clean_changes(body.get("changes"), schema)
    summary = body.get("summary")
    stale = []
    for name, old in (body.get("expected") or {}).items():
        if name == "summary":
            if str(old or "").strip() != str(current.get("summary") or "").strip():
                stale.append("Summary")
            continue
        key = FIELD_KEYS.get(name)
        if key and comparable(name, current.get(key)) != comparable(name, old):
            stale.append(name)
    if stale:
        raise Conflict(f"{issue_id} was changed in YouTrack since you loaded it ({', '.join(stale)}). Nothing was saved. Refresh and try again.", current)
    payload = {}
    if changes:
        payload["customFields"] = [encode_field(n, v, schema["fields"][n]) for n, v in changes.items()]
    if summary is not None and str(summary).strip() and str(summary).strip() != current.get("summary"):
        payload["summary"] = str(summary).strip()
    if not payload:
        return {"issue": current, "mismatches": [], "by": schema["me"]}
    yt(token, "POST", f"/api/issues/{quote(issue_id)}", {"fields": "idReadable"}, payload)
    write_log(f"UPDATE {issue_id} by {schema['me']} changes={json.dumps(changes, ensure_ascii=False)}" + (f" summary={payload.get('summary')!r}" if payload.get("summary") else ""))
    issue, mismatches = verify(token, issue_id, changes)
    if mismatches:
        write_log(f"VERIFY {issue_id} mismatches: {mismatches}")
    return {"issue": issue, "mismatches": mismatches, "by": schema["me"]}


# ---------------------------------------------------------------- dependencies (MAIT logic)
RULE_ENTRY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*-\d+)\s*:\s*(EB|BB|EE)\s*(?:([+-])\s*(\d+(?:\.\d+)?)\s*h?)?\s*$", re.I)


def format_rule(predecessor, kind, lag_minutes):
    kind = str(kind or "EB").upper()
    if kind not in ("EB", "BB", "EE"):
        raise ValueError("Dependency type must be EB, BB or EE")
    lag_minutes = int(round(float(lag_minutes or 0)))
    if kind == "EB" and lag_minutes == 0:
        return None          # EB without lag is the implicit default: no entry needed
    if not lag_minutes:
        return f"{predecessor}:{kind}"
    hours = abs(lag_minutes) / 60
    text = str(int(hours)) if hours == int(hours) else f"{hours:.2f}".rstrip("0")
    return f"{predecessor}:{kind}{'+' if lag_minutes > 0 else '-'}{text}h"


def merge_rules(current, predecessor, new_entry):
    parts = [p.strip() for p in re.split(r"[;\n]+", str(current or "")) if p.strip()]
    kept = [p for p in parts if not (RULE_ENTRY.match(p) and RULE_ENTRY.match(p).group(1).upper() == predecessor.upper())]
    if new_entry:
        kept.append(new_entry)
    return "; ".join(kept)


def raw_field(raw_issue, name):
    for item in raw_issue.get("customFields") or []:
        if item.get("name") == name:
            return item.get("$type"), item.get("value")
    return None, None


def set_dependency_rule(token, successor, predecessor, kind, lag_minutes):
    raw, _ = read_issue(token, successor)
    ftype, current = raw_field(raw, "Dependency Rules")
    if not ftype:
        raise RuntimeError(f"Field 'Dependency Rules' was not found on {successor}")
    current_text = current if isinstance(current, str) else ((current or {}).get("text") if isinstance(current, dict) else "") or ""
    updated = merge_rules(current_text, predecessor, format_rule(predecessor, kind, lag_minutes))
    if updated == current_text:
        return "unchanged"
    value = ({"text": updated} if ftype == "TextIssueCustomField" else updated) if updated else None
    yt(token, "POST", f"/api/issues/{quote(successor)}", {"fields": "idReadable"}, {"customFields": [{"$type": ftype, "name": "Dependency Rules", "value": value}]})
    return updated or "(cleared)"


def add_depends_on(token, successor, predecessor):
    raw, _ = read_issue(token, successor)
    yt(token, "POST", "/api/commands", None, {"query": f"depends on {predecessor}", "issues": [{"id": raw.get("id")}]})


def remove_depends_on(token, successor, predecessor):
    links = yt(token, "GET", f"/api/issues/{quote(successor)}/links", {"fields": "id,direction,linkType(sourceToTarget,targetToSource),issues(id,idReadable)"})
    for link in links if isinstance(links, list) else []:
        if link_label(link) != "depends on":
            continue
        for related in link.get("issues") or []:
            if str(related.get("idReadable", "")).upper() == predecessor.upper():
                yt(token, "DELETE", f"/api/issues/{quote(successor)}/links/{quote(link['id'])}/issues/{quote(related['id'])}")
                return "removed"
    return "not found"


def check_pair(predecessor, successor):
    check_fdi(predecessor)
    check_fdi(successor)
    if predecessor == successor:
        raise ValueError("A task cannot depend on itself")


def create_link(token, body):
    predecessor, successor = str(body.get("predecessor", "")), str(body.get("successor", ""))
    check_pair(predecessor, successor)
    _, pred_issue = read_issue(token, predecessor)
    if successor in (pred_issue.get("dependencies") or []):
        raise RuntimeError(f"{predecessor} already depends on {successor}: this link would create a cycle. Nothing was written.")
    add_depends_on(token, successor, predecessor)
    steps = [f"{successor} depends on {predecessor}"]
    rule = set_dependency_rule(token, successor, predecessor, body.get("type"), body.get("lagMinutes"))
    steps.append(f"Dependency Rules on {successor}: {rule}")
    _, after = read_issue(token, successor)
    ok = predecessor in after["dependencies"]
    write_sync_log({"at": stamp(), "action": "create_link", "predecessor": predecessor, "successor": successor,
                    "type": body.get("type"), "lagMinutes": body.get("lagMinutes"), "ok": ok})
    if not ok:
        raise RuntimeError(f"YouTrack accepted the command but {successor} does not show 'depends on {predecessor}' when read back")
    return {"ok": True, "steps": steps, "issue": after}


def remove_links(token, body):
    pairs = body.get("links")
    if not isinstance(pairs, list) or not pairs:
        raise RuntimeError("No links to remove were provided")
    if len(pairs) > 50:
        raise RuntimeError("Too many links in one request (maximum 50)")
    results = []
    for item in pairs:
        predecessor, successor = str((item or {}).get("predecessor", "")), str((item or {}).get("successor", ""))
        entry = {"predecessor": predecessor, "successor": successor, "ok": False}
        try:
            check_pair(predecessor, successor)
            entry["link"] = remove_depends_on(token, successor, predecessor)
            try:
                entry["rule"] = set_dependency_rule(token, successor, predecessor, "EB", 0)
            except Exception as error:
                entry["rule"] = f"not updated: {error}"
            entry["ok"] = entry["link"] == "removed"
            if not entry["ok"]:
                entry["error"] = "Link not found in YouTrack"
        except Exception as error:
            entry["error"] = str(error)
        results.append(entry)
        write_sync_log({"at": stamp(), "action": "remove_link", **entry})
    return {"ok": all(r["ok"] for r in results), "results": results}


def create_task(token, body):
    """Create an activity and link it: predecessor -> NEW (-> successor). Optional removal of predecessor -> successor."""
    predecessor = str(body.get("predecessor") or "")
    successor = str(body.get("successor") or "")
    check_fdi(predecessor)
    if successor:
        check_pair(predecessor, successor)
    created = create_activity(token, body)
    new_id = created["issue"]["id"]
    steps, errors = [f"Created {new_id}"], []

    def step(label, fn):
        try:
            result = fn()
            steps.append(label + (f" ({result})" if isinstance(result, str) else ""))
        except Exception as error:
            errors.append(f"{label}: {error}")

    step(f"{new_id} depends on {predecessor}", lambda: add_depends_on(token, new_id, predecessor))
    step(f"Dependency Rules on {new_id}", lambda: set_dependency_rule(token, new_id, predecessor, body.get("inType"), body.get("inLagMinutes")))
    if successor:
        step(f"{successor} depends on {new_id}", lambda: add_depends_on(token, successor, new_id))
        step(f"Dependency Rules on {successor}", lambda: set_dependency_rule(token, successor, new_id, body.get("outType"), body.get("outLagMinutes")))
        if body.get("removeDirect"):
            def drop():
                result = remove_depends_on(token, successor, predecessor)
                set_dependency_rule(token, successor, predecessor, "EB", 0)
                return result
            step(f"Remove direct link {predecessor} -> {successor}", drop)
    write_sync_log({"at": stamp(), "action": "create_task", "id": new_id, "predecessor": predecessor,
                    "successor": successor or None, "ok": not errors, "errors": errors})
    _, issue = read_issue(token, new_id)
    return {"ok": not errors, "id": new_id, "steps": steps, "errors": errors, "issue": issue, "mismatches": created["mismatches"]}


# ---------------------------------------------------------------- date sync (MAIT logic + conflict check)
def sync_dates(token, updates):
    if not isinstance(updates, list) or not updates:
        raise RuntimeError("No date updates were provided")
    if len(updates) > MAX_SYNC_UPDATES:
        raise RuntimeError(f"Too many updates in one request (maximum {MAX_SYNC_UPDATES})")
    results = []
    for item in updates:
        issue_id = str((item or {}).get("id", ""))
        entry = {"id": issue_id, "ok": False}
        try:
            start, end = int(item["start"]), int(item["end"])
            if end < start:
                raise ValueError("End is before start")
            raw, current = editable_current(token, issue_id)
            old_s, old_e = item.get("oldStart"), item.get("oldEnd")
            if comparable("Start Date and Time", current["startMs"]) != comparable("Start Date and Time", old_s) or \
               comparable("End Date and Time", current["endMs"]) != comparable("End Date and Time", old_e):
                raise RuntimeError("changed in YouTrack since the preview; refresh and preview again")
            st, _ = raw_field(raw, "Start Date and Time")
            et, _ = raw_field(raw, "End Date and Time")
            st, et = st or "DateIssueCustomField", et or "DateIssueCustomField"
            yt(token, "POST", f"/api/issues/{quote(issue_id)}", {"fields": "idReadable"},
               {"customFields": [{"$type": st, "name": "Start Date and Time", "value": start},
                                 {"$type": et, "name": "End Date and Time", "value": end}]})
            _, after = read_issue(token, issue_id)
            entry["ok"] = after["startMs"] is not None and after["startMs"] // 60000 == start // 60000 and \
                after["endMs"] is not None and after["endMs"] // 60000 == end // 60000
            entry["issue"] = after
            if not entry["ok"]:
                entry["error"] = "written, but YouTrack shows different dates when read back"
        except (KeyError, TypeError, ValueError) as error:
            entry["error"] = str(error) or "Invalid update"
        except Exception as error:
            entry["error"] = str(error)
        results.append(entry)
        write_sync_log({"at": stamp(), "action": "sync_dates", "id": issue_id, "ok": entry["ok"], "error": entry.get("error"),
                        "oldStart": item.get("oldStart") if isinstance(item, dict) else None,
                        "oldEnd": item.get("oldEnd") if isinstance(item, dict) else None,
                        "newStart": item.get("start") if isinstance(item, dict) else None,
                        "newEnd": item.get("end") if isinstance(item, dict) else None})
    return {"ok": all(r["ok"] for r in results), "results": results}


def get_user_timezone(token):
    payload = yt(token, "GET", "/api/users/me", {"fields": "login,name,profiles(general(timezone(presentation,offset)))"})
    zone = (((payload.get("profiles") or {}).get("general") or {}).get("timezone") or {})
    offset = zone.get("offset")
    return {"login": payload.get("login"), "name": payload.get("name"), "timezone": zone.get("presentation"),
            "offsetMinutes": int(offset) if isinstance(offset, (int, float)) else None}


# ---------------------------------------------------------------- working calendar
def validate_calendar(entries):
    if not isinstance(entries, list) or len(entries) > 2000:
        raise RuntimeError("Calendar must be a list of at most 2000 entries")
    clean = []
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid calendar entry")
        start, end, kind = str(item.get("from", "")), str(item.get("to", "")), str(item.get("type", ""))
        try:
            datetime.strptime(start, "%Y-%m-%d")
            datetime.strptime(end, "%Y-%m-%d")
        except ValueError as error:
            raise RuntimeError(f"Invalid calendar date: {start} / {end}") from error
        if start > end or kind not in CALENDAR_TYPES:
            raise RuntimeError(f"Invalid calendar period: {start} to {end} ({kind})")
        clean.append({"from": start, "to": end, "type": kind, "comment": str(item.get("comment") or "")[:200]})
    return clean


def read_calendar():
    try:
        with open(CALENDAR_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {"exists": True, "entries": validate_calendar(payload.get("entries")), "savedAt": payload.get("savedAt"), "file": CALENDAR_FILE}
    except FileNotFoundError:
        return {"exists": False, "entries": [], "savedAt": None, "file": CALENDAR_FILE}


def save_calendar(entries):
    clean = validate_calendar(entries)
    payload = {"savedAt": stamp(), "entries": clean}
    temp = CALENDAR_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, CALENDAR_FILE)
    write_sync_log({"at": payload["savedAt"], "action": "save_calendar", "entries": len(clean), "ok": True})
    return {"exists": True, "entries": clean, "savedAt": payload["savedAt"], "file": CALENDAR_FILE}


# ---------------------------------------------------------------- HTTP server
def cors(handler):
    origin = handler.headers.get("Origin", "null")
    if origin not in ("null", f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
        origin = "null"
    return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Max-Age": "600", "Vary": "Origin"}


class Handler(BaseHTTPRequestHandler):
    server_version = "FM1AITProxy/1.0"

    def log_message(self, fmt, *args):
        sys.stdout.write("[proxy] " + (fmt % args) + "\n")
        sys.stdout.flush()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in cors(self).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        for key, value in cors(self).items():
            self.send_header(key, value)
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self.send_json(200, {"ok": True, "saved": bool(saved_token()), "project": PROJECT_KEY, "write": True, "planning": True, "version": 1})
        elif path == "/token":
            self.send_json(200, {"saved": bool(saved_token())})
        elif path == "/calendar":
            try:
                self.send_json(200, read_calendar())
            except Exception as error:
                self.send_json(500, {"error": str(error)})
        elif path in ("/", "/index.html", "/" + PAGE_FILE):
            try:
                with open(os.path.join(HERE, PAGE_FILE), "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                self.send_json(404, {"error": f"{PAGE_FILE} must be in the same folder as the proxy"})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self.read_body()
            if path == "/calendar":
                self.send_json(200, save_calendar(body.get("entries")))
                return
            if path != "/youtrack":
                self.send_json(404, {"error": "Not found"})
                return
            action = body.get("action")
            token = str(body.get("token", "")).strip()
            if action == "save_token":
                if not token:
                    raise RuntimeError("No token supplied")
                store_token(token)
                self.send_json(200, {"saved": True})
                return
            if action == "clear_token":
                clear_token()
                self.send_json(200, {"saved": False})
                return
            token = token or saved_token()
            if not token:
                raise RuntimeError("No YouTrack token supplied")
            handlers = {
                "schema": lambda: get_schema(token),
                "create": lambda: create_activity(token, body),
                "update": lambda: update_activity(token, body),
                "create_link": lambda: create_link(token, body),
                "remove_links": lambda: remove_links(token, body),
                "create_task": lambda: create_task(token, body),
                "sync_dates": lambda: sync_dates(token, body.get("updates")),
                "timezone": lambda: get_user_timezone(token),
            }
            if action in handlers:
                self.send_json(200, handlers[action]())
                return
            data = [normalise_issue(i) for i in get_all_issues(token, PROJECT_QUERY, FIELDS)]
            try:
                program = [normalise_program(i) for i in get_all_issues(token, PROGRAM_QUERY, PROGRAM_FIELDS)]
                program_error = None
            except Exception as error:
                program, program_error = [], str(error)
            self.send_json(200, {"project": PROJECT_KEY, "refHours": 8, "count": len(data),
                                 "refreshedAt": datetime.now().isoformat(timespec="seconds"),
                                 "data": data, "program": program, "programError": program_error})
        except Conflict as error:
            self.send_json(409, {"error": str(error), "current": error.current})
        except json.JSONDecodeError:
            self.send_json(400, {"error": "Invalid JSON request"})
        except Exception as error:
            self.send_json(502, {"error": str(error)})


if __name__ == "__main__":
    print(f"FM1 AIT proxy listening on http://{HOST}:{PORT}/  (open this address in your browser)")
    print("Project: FDI | read + safe write | Stream/Block issues are never edited | Ctrl+C to stop")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")
