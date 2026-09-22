#!/usr/bin/env python3
"""
Insta leads enrichment routine.

For every pending row in public.insta_stories (org is not null and
enrichment_is_processed is null/false), look the org up on Hunter.io
(Domain Search) and insert ONE new row per contact found, copying the
source row's other fields and filling poc / poc_email / poc_position.
Contacts whose Hunter position matches one of the TARGET_ROLES are
preferred (keyword match). If an org has NO target-role contact, one
fallback contact (any person at the company) is kept so every resolvable
org still yields at least one POC. Each source row is then tagged
enrichment_is_processed = true (even when no contact is found at all), so
it is never reprocessed.

Credentials
-----------
- Hunter.io API key: read from hunter_io_api_key.txt (next to this file),
  or the HUNTER_API_KEY env var.
- Supabase service_role key (needed to write via PostgREST, bypasses RLS):
  read from the SUPABASE_SERVICE_KEY env var, or supabase_service_key.txt
  (next to this file).

Usage
-----
    python enrich_leads.py            # enrich all pending rows
    python enrich_leads.py --dry-run  # look up + print, write nothing
"""

import os
import re
import sys
import time
import json
from pathlib import Path

import requests

SUPABASE_URL = "https://aivitcomiywiysrfwqxt.supabase.co"
REST = f"{SUPABASE_URL}/rest/v1"
TABLE = "insta_stories"
HUNTER_ENDPOINT = "https://api.hunter.io/v2/domain-search"

HERE = Path(__file__).resolve().parent

# Fields copied verbatim from a source row onto each new contact row.
# NOTE: is_added_to_sheet is deliberately NOT copied — a new contact row must
# start with it empty so the Sheet-sync step (sheet_sync.py) picks it up. If the
# source story was already in the Sheet, copying its true would wrongly skip the
# new contacts from ever being synced.
COPY_FIELDS = [
    "speaker",
    "insta_story_image_url",
    "insta_story_vid_url",
    "status",
    "insta_handle",
    "description",
    "org",
    "event_name",
    "saved_img_link",
]

# Only contacts whose Hunter position matches one of these target roles are kept.
# Matching is case-insensitive and keyword-based, tolerant of title variants like
# "Head of ...", "... Director", "Senior ...", "... Coordinator". Contacts Hunter
# returns with an empty/unknown position are dropped.
TARGET_ROLES = (
    "Conference / Events Manager",
    "Marketing Manager",
    "Communication Manager",
    "Learning & Development Manager",
    "People & Culture Manager",
    "Executive Assistant",
    "Human Resources / HR (incl. recruitment, talent acquisition, people operations)",
)

_ROLE_RE = re.compile(
    r"event|conference"                                        # Conference / Events
    r"|marketing"                                              # Marketing
    r"|communication|\bcomms\b"                                # Communication(s) / comms
    r"|\bl\s*&\s*d\b"                                           # L&D shorthand
    r"|executive\s+assistant|\bexec\.?\s*assistant\b|\bea\b"    # Executive Assistant / EA
    r"|\bhr\b|human\s+resources|\bchro\b|chief\s+people"        # HR / Human Resources
    r"|recruit|talent|people\s+oper|people\s+ops",             # recruitment / talent / people ops
    re.IGNORECASE,
)


def position_matches_target(position) -> bool:
    """True if a Hunter free-text position falls into one of TARGET_ROLES."""
    if not position or not position.strip():
        return False
    pos = position.lower()
    if _ROLE_RE.search(pos):
        return True
    # Two-word roles: require both tokens present, in any order/punctuation.
    if "learning" in pos and "development" in pos:
        return True
    if "people" in pos and "culture" in pos:
        return True
    return False


def _read_secret(env_var: str, filename: str) -> str:
    val = os.environ.get(env_var)
    if val and val.strip():
        return val.strip()
    path = HERE / filename
    if path.exists():
        txt = path.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    raise SystemExit(
        f"Missing credential: set ${env_var} or put it in {path.name} next to this script."
    )


def load_hunter_key() -> str:
    return _read_secret("HUNTER_API_KEY", "hunter_io_api_key.txt")


def load_service_key() -> str:
    return _read_secret("SUPABASE_SERVICE_KEY", "supabase_service_key.txt")


def sb_headers(service_key: str, extra=None) -> dict:
    h = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def get_pending_rows(service_key: str) -> list:
    """Rows with an org that have not been enriched yet."""
    params = {
        "select": "*",
        "org": "not.is.null",
        "or": "(enrichment_is_processed.is.null,enrichment_is_processed.eq.false)",
        "order": "id.asc",
    }
    r = requests.get(f"{REST}/{TABLE}", headers=sb_headers(service_key), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def hunter_domain_search(org: str, hunter_key: str) -> dict:
    """Raw Hunter Domain Search response 'data' block for a company name."""
    # Hunter caps Domain Search results per the account plan (free plans cap at
    # 10). Requesting more than the plan allows returns a 400 pagination_error,
    # so keep the limit at 10.
    params = {"company": org, "api_key": hunter_key, "limit": 10}
    r = requests.get(HUNTER_ENDPOINT, params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Hunter {r.status_code}: {r.text[:300]}")
    return r.json().get("data", {}) or {}


def contacts_from_hunter(data: dict) -> list:
    """Contacts to insert for one org, as de-duplicated dicts (poc/email/position).

    Prefer contacts whose position matches one of the TARGET_ROLES and return all
    of them. If NONE of the returned emails match a target role, fall back to a
    SINGLE "any" contact so every org that resolves to a domain still yields at
    least one POC — preferring a named person over a generic/nameless mailbox.
    Only when Hunter returns no emails at all is the list empty.
    """
    all_contacts = []
    seen = set()
    for e in data.get("emails", []) or []:
        email = (e.get("value") or "").strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        name = " ".join(p for p in [e.get("first_name"), e.get("last_name")] if p).strip()
        position = (e.get("position") or "").strip()
        all_contacts.append(
            {
                "poc": name or None,
                "poc_email": email,
                "poc_position": position or None,
                "_matched": position_matches_target(position),
            }
        )

    matched = [c for c in all_contacts if c["_matched"]]
    if matched:
        chosen = matched
    elif all_contacts:
        # No target-role contact for this org -> keep ONE fallback so it still
        # gets a POC. Prefer an entry with a real person's name.
        chosen = [next((c for c in all_contacts if c["poc"]), all_contacts[0])]
    else:
        chosen = []

    for c in chosen:
        c.pop("_matched", None)
    return chosen


def build_new_row(source: dict, contact: dict) -> dict:
    row = {f: source.get(f) for f in COPY_FIELDS}
    row["poc"] = contact["poc"]
    row["poc_email"] = contact["poc_email"]
    row["poc_position"] = contact["poc_position"]
    row["enrichment_is_processed"] = True  # inserted rows are already enriched
    return row


def insert_rows(service_key: str, rows: list) -> None:
    if not rows:
        return
    r = requests.post(
        f"{REST}/{TABLE}",
        headers=sb_headers(service_key, {"Prefer": "return=minimal"}),
        data=json.dumps(rows),
        timeout=30,
    )
    r.raise_for_status()


def mark_processed(service_key: str, row_id: int) -> None:
    r = requests.patch(
        f"{REST}/{TABLE}",
        headers=sb_headers(service_key, {"Prefer": "return=minimal"}),
        params={"id": f"eq.{row_id}"},
        data=json.dumps({"enrichment_is_processed": True}),
        timeout=30,
    )
    r.raise_for_status()


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    hunter_key = load_hunter_key()
    # A service_role key is needed to read the pending rows and (unless --dry-run)
    # to write results back.
    service_key = load_service_key()

    pending = get_pending_rows(service_key)
    print(f"Pending rows: {len(pending)}")

    total_inserted = 0
    for row in pending:
        org = row.get("org")
        try:
            data = hunter_domain_search(org, hunter_key)
            contacts = contacts_from_hunter(data)
        except Exception as exc:  # network / Hunter error -> leave row for retry
            print(f"  id={row['id']} org={org!r}: Hunter error, skipping -> {exc}")
            continue

        domain = data.get("domain")
        print(f"  id={row['id']} org={org!r} domain={domain} contacts={len(contacts)}")

        new_rows = [build_new_row(row, c) for c in contacts]
        if dry_run:
            for c in contacts:
                print(f"      + {c['poc']!r} <{c['poc_email']}> [{c['poc_position']}]")
        else:
            insert_rows(service_key, new_rows)
            mark_processed(service_key, row["id"])
        total_inserted += len(new_rows)
        time.sleep(1)  # be gentle with Hunter's rate limit

    print(f"Done. Contact rows {'that would be ' if dry_run else ''}inserted: {total_inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
