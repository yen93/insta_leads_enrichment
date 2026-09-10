#!/usr/bin/env python3
"""
Insta leads enrichment routine.

For every pending row in public.insta_stories (org is not null and
enrichment_is_processed is null/false), look the org up on Hunter.io
(Domain Search) and insert ONE new row per contact found, copying the
source row's other fields and filling poc / poc_email / poc_position.
Each source row is then tagged enrichment_is_processed = true (even when
no contact is found), so it is never reprocessed.

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
    "is_added_to_sheet",
]


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
    """Turn Hunter emails into de-duplicated contact dicts (poc/email/position)."""
    contacts = []
    seen = set()
    for e in data.get("emails", []) or []:
        email = (e.get("value") or "").strip()
        if not email or email.lower() in seen:
            continue
        seen.add(email.lower())
        name = " ".join(p for p in [e.get("first_name"), e.get("last_name")] if p).strip()
        contacts.append(
            {
                "poc": name or None,
                "poc_email": email,
                "poc_position": (e.get("position") or None),
            }
        )
    return contacts


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
