#!/usr/bin/env python3
"""
audit.py — st-architecture auto-audit script

Collects live system state from authoritative sources and patches index.html
with any values that have drifted, then bumps the version and prepends a
changelog entry.

Usage:
    python audit.py [--dry-run] [--minor] [--no-commit]

    --dry-run    collect + diff only; do NOT write index.html or commit
    --minor      bump minor version (v3.4 -> v3.5) instead of patch (v3.4 -> v3.4.1)
    --no-commit  patch index.html but skip git commit

Environment variables required for Supabase queries:
    SUPABASE_URL  e.g. https://cnplogkxbjecdeeritdl.supabase.co
    SUPABASE_KEY  anon key

External dependency:
    requests  (pip install requests)

Python hard rules applied (smokin-knowledge/python/AGENTS.md):
  Rule 1: UTF-8 reconfigure for Win11 at module entry — see top of file.
  Rule 2: subprocess uses arg-list + shell=False throughout.
  Rule 3: No unsafe deserialization; json only, no pickle/yaml.load.
  Rule 4: No blocking I/O in async (script is synchronous — N/A).
  Rule 5: MCP stdio reserved for JSON-RPC (script is CLI — N/A).
"""

import sys
import os

# Rule 1 — UTF-8 on Win11 entry point (before any print)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import argparse
import datetime
import glob
import json
import re
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_DIR      = Path(__file__).parent.resolve()
INDEX_HTML    = REPO_DIR / "index.html"

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "https://cnplogkxbjecdeeritdl.supabase.co")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "")

HOOK_DIR      = Path.home() / ".claude" / "hooks"
CATALOG_FILE  = Path.home() / ".claude" / "references" / "catalog" / "catalog.json"
# SETTINGS_FILE not used: MCP count is cloud-managed, not in local settings.json

# Scoring engine versions — update manually when an engine version ships
KNOWN_VERSIONS = {
    "tys":          "v7.6_threshold_recal",
    "chps":         "v1.1",
    "bhps":         "v1.0",
    "psps":         "v1.4",
    "rms":          "v1",
    "golden_record": "v2",
    "ssl":          "v1",
}


# ---------------------------------------------------------------------------
# Phase A: Collect live state
# ---------------------------------------------------------------------------

def _supabase_head_count(session, table, extra_params=None):
    """Return exact row count for a Supabase table via HEAD + Prefer:count=exact."""
    params = {"select": "*"}
    if extra_params:
        params.update(extra_params)
    resp = session.head(
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=params,
        headers={"Prefer": "count=exact"},
        timeout=15,
    )
    resp.raise_for_status()
    # Content-Range: 0-N/TOTAL  or  */TOTAL
    cr = resp.headers.get("Content-Range", "*/0")
    return int(cr.split("/")[-1])


def collect_live_state():
    """Query all authoritative sources. Returns dict of current live values."""
    state = {}

    # --- Supabase counts ---
    if not SUPABASE_KEY:
        print("WARNING: SUPABASE_KEY not set — Supabase counts skipped", file=sys.stderr)
        for k in ("accounts", "contacts", "parent_orgs", "warn_events",
                  "account_artifacts", "deliverables", "hac_measures"):
            state[k] = None
    else:
        import requests  # only imported when actually needed
        s = requests.Session()
        s.headers.update({
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        state["accounts"]          = _supabase_head_count(s, "accounts")
        state["contacts"]          = _supabase_head_count(s, "contacts")
        state["warn_events"]       = _supabase_head_count(s, "warn_events")
        state["account_artifacts"] = _supabase_head_count(s, "account_artifacts")
        state["deliverables"]      = _supabase_head_count(s, "deliverables")
        state["hac_measures"]      = _supabase_head_count(s, "hac_measures")

        # COUNT(DISTINCT ultimate_parent_duns) — fetch all values, count unique in Python
        resp = s.get(
            f"{SUPABASE_URL}/rest/v1/accounts",
            params={"select": "ultimate_parent_duns",
                    "ultimate_parent_duns": "not.is.null",
                    "limit": "5000"},
            timeout=30,
        )
        resp.raise_for_status()
        state["parent_orgs"] = len({r["ultimate_parent_duns"] for r in resp.json()})

    # --- Hooks ---
    sh_files = glob.glob(str(HOOK_DIR / "*.sh"))
    state["hook_count"]    = len(sh_files)

    # --- Skills ---
    with open(CATALOG_FILE, encoding="utf-8") as f:
        catalog = json.load(f)                        # Rule 3: json only
    state["total_skills"]  = catalog.get("total_skills") or len(catalog["skills"])
    state["custom_skills"] = len([
        sk for sk in catalog["skills"]
        if sk.get("tier", "").startswith("custom")
    ])

    # MCP count is NOT auto-collected: MCPs are connected via claude.ai cloud interface,
    # not in local settings.json. Update mcp_count manually when servers are added/removed.

    return state


# ---------------------------------------------------------------------------
# Phase B+C: Build replacement list and compute delta
# ---------------------------------------------------------------------------

# Each entry in AUDIT_POINTS drives both the delta report and the patching.
#
# Structure:
#   name        — human-readable label
#   live_key    — key in state dict
#   fmt         — optional lambda to format the live value (default: str)
#   locations   — list of (description, extract_re, replacement_fn) triples
#                   extract_re  : captures the ENTIRE match to be replaced (group 0)
#                                 + the numeric value to compare (group 1)
#                   replacement_fn : (match, new_str) -> replacement string

def _locs_for_hooks():
    """Return location descriptors for hook_count."""
    return [
        (
            "stat-card value",
            # matches e.g.  AI Hooks</div>\n        <div class="value">20</div>
            re.compile(
                r'(AI Hooks</div>\s*\n\s*<div class="value">)(\d+)(</div>)',
                re.MULTILINE
            ),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "SVG Hooks box title",
            re.compile(r'(>Hooks \()(\d+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "section heading",
            re.compile(r'(Enforcement Hooks \()(\d+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "SVG claude-config sub-line",
            re.compile(r'(>)(\d+)( hooks · make install)'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "nav sidebar",
            re.compile(r'(href="#hooks">Hooks \()(\d+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
    ]


def _locs_for_skills():
    return [
        (
            "stat-card value",
            re.compile(
                r'(Skills \(cataloged\)</div>\s*\n\s*<div class="value">)([0-9,]+)(</div>)',
                re.MULTILINE
            ),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "SVG Skills box title",
            re.compile(r'(>Skills \()([0-9,]+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "section heading",
            re.compile(r'(Skills \()([0-9,]+)( cataloged\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "prose totalling",
            re.compile(r'(totalling )([0-9,]+)'),
            lambda m, v: m.group(1) + v,
        ),
    ]


def _locs_for_custom_skills():
    return [
        (
            "SVG sub-line",
            # "89 custom + plugin marketplaces"  — exclude "89 → 81" in changelog
            re.compile(r'(\b)(\d+)( custom \+ plugin marketplaces)'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "prose custom skills",
            # "81 custom skills"  — but NOT the "89 → 81" changelog note
            re.compile(r'(\b)(\d+)( custom skills)'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
    ]


def _locs_for_mcp():
    return [
        (
            "stat-card value",
            re.compile(
                r'(MCP Servers</div>\s*\n\s*<div class="value">)([0-9+]+)(</div>)',
                re.MULTILINE
            ),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "SVG MCP box title",
            re.compile(r'(>MCP Servers \()([0-9+]+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "section heading",
            re.compile(r'(<h3 id="mcps">MCP Servers \()([0-9+]+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "nav sidebar",
            re.compile(r'(href="#mcps">MCP Servers \()([0-9+]+)(\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
    ]


def _locs_for_accounts():
    return [
        (
            "stat-card value",
            re.compile(
                r'(Accounts</div>\s*\n\s*<div class="value">)(\d+)(</div>)',
                re.MULTILINE
            ),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "table row accounts",
            re.compile(r'(<code>accounts</code></td>\s*<td>)(\d+)'),
            lambda m, v: m.group(1) + v,
        ),
        (
            "table row scoring",
            re.compile(r'(<code>scoring</code></td>\s*<td>)(\d+)'),
            lambda m, v: m.group(1) + v,
        ),
        (
            "boundaries card Accounts",
            re.compile(r'(>Accounts</td><td[^>]*>)(\d+)'),
            lambda m, v: m.group(1) + v,
        ),
    ]


def _locs_for_parent_orgs():
    return [
        (
            "stat-card sub",
            re.compile(r'(\b)(\d+)( unique parent orgs \(by ultimate_parent_duns\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "boundaries card parent orgs",
            re.compile(r'(\b)(\d+)( parent orgs\))'),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
    ]


def _locs_for_contacts():
    return [
        (
            "stat-card value",
            re.compile(
                r'(Contacts</div>\s*\n\s*<div class="value">)(\d+)(</div>)',
                re.MULTILINE
            ),
            lambda m, v: m.group(1) + v + m.group(3),
        ),
        (
            "table row contacts",
            re.compile(r'(<code>contacts</code></td>\s*<td>)(\d+)'),
            lambda m, v: m.group(1) + v,
        ),
    ]


AUDIT_POINTS = [
    {"name": "hook_count",    "live_key": "hook_count",    "fmt": str,
     "locs_fn": _locs_for_hooks},
    {"name": "total_skills",  "live_key": "total_skills",
     "fmt": lambda v: f"{int(v):,}",
     "locs_fn": _locs_for_skills},
    {"name": "custom_skills", "live_key": "custom_skills", "fmt": str,
     "locs_fn": _locs_for_custom_skills},
    # mcp_count intentionally omitted — MCPs are cloud-managed; update manually
    {"name": "accounts",      "live_key": "accounts",      "fmt": str,
     "locs_fn": _locs_for_accounts},
    {"name": "parent_orgs",   "live_key": "parent_orgs",   "fmt": str,
     "locs_fn": _locs_for_parent_orgs},
    {"name": "contacts",      "live_key": "contacts",      "fmt": str,
     "locs_fn": _locs_for_contacts},
]


def compute_delta_and_patches(html, state):
    """
    Returns:
        rows   — list of dicts for printing the delta table
        ops    — list of (location_desc, match_obj, new_str) pending substitutions
    """
    rows = []
    ops  = []

    for pt in AUDIT_POINTS:
        live_raw = state.get(pt["live_key"])
        if live_raw is None:
            continue
        live_str = pt["fmt"](live_raw)
        locs     = pt["locs_fn"]()
        found    = []
        changed  = False

        for loc_desc, pattern, repl_fn in locs:
            for m in pattern.finditer(html):
                cur = m.group(2)
                found.append(cur)
                if cur != live_str:
                    changed = True
                    ops.append((pt["name"], loc_desc, m, repl_fn, live_str))

        # Collect unique current values for display
        unique_cur = sorted(set(found), key=lambda x: found.index(x))
        status = "DRIFT" if changed else ("OK" if found else "NOT FOUND")

        rows.append({
            "name":    pt["name"],
            "current": unique_cur,
            "live":    live_str,
            "hits":    len(found),
            "status":  status,
        })

    return rows, ops


# ---------------------------------------------------------------------------
# Phase D: Apply patches
# ---------------------------------------------------------------------------

def apply_patches(html, ops):
    """Apply all pending substitutions. Returns patched HTML."""
    # Work backwards by match start position so offsets stay valid
    ops_sorted = sorted(ops, key=lambda x: x[2].start(), reverse=True)
    for _name, _desc, m, repl_fn, live_str in ops_sorted:
        replacement = repl_fn(m, live_str)
        html = html[:m.start()] + replacement + html[m.end():]
    return html


def bump_version(html, minor=False):
    """
    Find the CURRENT version badge string 'vX.Y' or 'vX.Y.Z' and bump it.
    Returns (patched_html, new_version_string).
    Only updates the badge span and footer line — not changelog historical entries.
    """
    today = datetime.date.today().strftime("%Y-%m-%d")

    # Find the badge that has the version + date together
    badge_re = re.compile(
        r'(>)(v\d+\.\d+(?:\.\d+)?)( · )(\d{4}-\d{2}-\d{2})(<)'
    )
    m = badge_re.search(html)
    if not m:
        return html, "v?.?"

    cur_ver = m.group(2)
    parts   = cur_ver.lstrip("v").split(".")
    if minor:
        parts[1] = str(int(parts[1]) + 1)
        if len(parts) > 2:
            parts[2] = "0"
    else:
        if len(parts) == 2:
            parts.append("1")
        else:
            parts[2] = str(int(parts[2]) + 1)
    new_ver = "v" + ".".join(parts)

    # Replace only the badge span (not changelog historical notes)
    html = badge_re.sub(
        lambda bm: bm.group(1) + new_ver + bm.group(3) + today + bm.group(5),
        html,
        count=1
    )

    # Update footer "Generated YYYY-MM-DD" and version mention
    html = re.sub(
        r'(Generated )\d{4}-\d{2}-\d{2}',
        rf'\g<1>{today}',
        html
    )
    html = re.sub(
        r'(SmokinTerritory Architecture <strong>)' + re.escape(cur_ver) + r'(</strong>)',
        rf'\g<1>{new_ver}\2',
        html
    )
    # Title tag only — do NOT do a global replace (would corrupt changelog history entries)
    html = re.sub(
        r'(<title>[^<]*?)' + re.escape(cur_ver) + r'([^<]*?</title>)',
        rf'\g<1>{new_ver}\2',
        html,
        count=1
    )

    return html, new_ver


def prepend_changelog(html, new_ver, delta_summary, today):
    """Prepend a new <li> as the first item in the first changelog <ul>."""
    li = (
        f'<li><strong>+ auto-audit {new_ver}</strong> (<code>{today}</code>) '
        f'— {delta_summary}</li>'
    )
    # The first <ul> inside the changelog div
    html = re.sub(
        r'(<div class="changelog">.*?<ul>)(\s*)',
        rf'\g<1>\n      {li}\n      ',
        html,
        count=1,
        flags=re.DOTALL
    )
    return html


# ---------------------------------------------------------------------------
# Phase E: Commit
# ---------------------------------------------------------------------------

def git_commit(new_ver, delta_summary):
    msg = f"docs: auto-audit {new_ver} — {delta_summary}"
    # Rule 2: arg-list + shell=False
    subprocess.run(
        ["git", "-C", str(REPO_DIR), "add", "index.html"],
        check=True, shell=False
    )
    subprocess.run(
        ["git", "-C", str(REPO_DIR), "commit", "-m", msg],
        check=True, shell=False
    )
    return msg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Audit and patch st-architecture/index.html"
    )
    parser.add_argument("--dry-run",   action="store_true",
                        help="Show delta only — do NOT write index.html or commit")
    parser.add_argument("--minor",     action="store_true",
                        help="Bump minor version (v3.4 -> v3.5) instead of patch")
    parser.add_argument("--no-commit", action="store_true",
                        help="Patch index.html but skip git commit")
    args = parser.parse_args()

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"=== st-architecture audit ({mode}) ===\n")

    # --- Collect ---
    print("Collecting live state...")
    state = collect_live_state()
    for k, v in state.items():
        print(f"  {k:<25} {v}")
    print()

    # --- Read HTML ---
    html = INDEX_HTML.read_text(encoding="utf-8")

    # --- Delta ---
    rows, ops = compute_delta_and_patches(html, state)

    col = f"{'Data Point':<25}  {'Current (unique)':<28}  {'Live':<15}  {'Hits':>4}  Status"
    print(col)
    print("-" * len(col))
    drifted = []
    for r in rows:
        cur_str = ", ".join(r["current"]) if r["current"] else "(no match)"
        print(f"  {r['name']:<23}  {cur_str:<28}  {r['live']:<15}  {r['hits']:>4}  {r['status']}")
        if r["status"] in ("DRIFT", "NOT FOUND"):
            drifted.append(r["name"])
    print()

    if not drifted:
        print("Nothing to patch — all values current.")
        return

    if args.dry_run:
        print(f"DRY-RUN: {len(ops)} substitution(s) identified. No files written.")
        return

    # --- Patch ---
    print(f"Applying {len(ops)} substitution(s)...")
    patched = apply_patches(html, ops)

    today   = datetime.date.today().strftime("%Y-%m-%d")
    patched, new_ver = bump_version(patched, minor=args.minor)

    n    = len(drifted)
    summary_names = ", ".join(drifted[:4]) + (" ..." if n > 4 else "")
    delta_summary = f"{n} data point(s) updated: {summary_names}"
    patched = prepend_changelog(patched, new_ver, delta_summary, today)

    INDEX_HTML.write_text(patched, encoding="utf-8")
    print(f"  Written: {INDEX_HTML}")
    print(f"  Version: {new_ver}   Date: {today}")
    print()

    if not args.no_commit:
        msg = git_commit(new_ver, delta_summary)
        print(f"  Committed: {msg}")
    else:
        print("  --no-commit: skipped git commit")

    print("\nDone.")


if __name__ == "__main__":
    main()
