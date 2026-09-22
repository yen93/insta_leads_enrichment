# CLAUDE.md — insta_leads_enrichment

Lead-enrichment for the Supabase `insta_stories` table: look each lead's `org` up
on Hunter.io and populate points of contact.

## What this project is

For every pending row (`org IS NOT NULL AND (enrichment_is_processed IS NULL OR =
false)`), call Hunter.io **Domain Search** on the org name and **expand the row
into one new row per selected contact** — copying the source row's other fields
and filling `poc` / `poc_email` / `poc_position`. Contacts whose Hunter job title
matches one of the **target roles** are preferred (see below); if an org has none,
one fallback "any" contact is kept so it still gets a POC. Then mark the source
row `enrichment_is_processed = true` (even when no contact is found at all). This
is an expansion model, not a single-best-POC pick.

## Target-role filter

Contacts whose Hunter `position` matches one of these roles are **preferred**:
Conference / Events Manager, Marketing Manager, Communication Manager,
Learning & Development Manager, People & Culture Manager, Executive Assistant,
and Human Resources / HR.
Matching is **keyword-based** and case-insensitive (e.g. any "marketing" title,
"events"/"conference", "communication(s)"/"comms", "learning & development"/"L&D",
"people & culture", "executive assistant"/"EA", and HR: standalone "HR",
"human resources", "CHRO"/"chief people", "recruit", "talent",
"people operations"/"people ops"), so title variants like
"Head of Marketing" or "Events Coordinator" are kept.

**Fallback:** if an org has NO target-role contact among Hunter's results, keep
exactly **one** fallback contact (any person at the company — prefer a named
person over a generic mailbox like info@) so every resolvable org gets at least
one POC. Only when Hunter returns zero emails is the org a true no-match. The
source row is marked processed regardless. The preference + fallback logic lives
in `TARGET_ROLES` / `position_matches_target()` / `contacts_from_hunter()` in
`enrich_leads.py` — keep the cloud routine's prompt in sync when it changes. Note
the plan's 10-email cap is applied **before** this selection, so an org may fall
back even when a target-role person exists beyond the first 10.

## Key facts / gotchas

- **Supabase project:** `MAGTestProject`, id `aivitcomiywiysrfwqxt`. Table
  `public.insta_stories`. `id` is a bigint **identity** and `created_at` defaults
  `now()` — never set them on insert.
- **Inserting contact rows:** prefer `INSERT ... SELECT ... FROM insta_stories
  WHERE id = <source_id>` so the long url/description fields are copied by the DB
  and you only inject the clean `poc`/`poc_email`/`poc_position` literals. Every
  inserted row MUST have `enrichment_is_processed = true`, or it will re-enrich
  itself endlessly (it has a non-null org).
- **Hunter Domain Search:** `GET https://api.hunter.io/v2/domain-search?company=
  {org}&limit=10`. The account plan **caps results at 10** — passing more returns
  a 400 `pagination_error`. People are in `data.emails[]` (`value`, `first_name`,
  `last_name`, `position`).
- **Domain mismatch:** Hunter guesses the domain from the org *name* and can
  return an unrelated company for small/ambiguous orgs (seen: "HerTri London" →
  `kerrylondon.co.uk`). Review flagged orgs; the interactive flow asks a human,
  the cloud routine only flags in its summary.
- **Duplicate orgs** (same org across multiple source rows) are enriched
  independently, producing duplicate contact sets — intended per the per-row
  model.

## Credentials (never commit — all gitignored)

- Hunter key: `hunter_io_api_key.txt` or `$HUNTER_API_KEY`.
- Supabase service_role key (only needed to run `enrich_leads.py` standalone):
  `supabase_service_key.txt` or `$SUPABASE_SERVICE_KEY`. Interactive sessions use
  the Supabase MCP instead, which carries its own auth.

## Run

```
python enrich_leads.py            # enrich all pending rows
python enrich_leads.py --dry-run  # Hunter lookups + print, no writes
```

## Cloud routine

`insta_leads_enrichment` (id `trig_01EZuVzMvdW5Gyq5VVyvKjgk`) mirrors the script
as a paused, API-triggered cloud agent (Default environment, where the Hunter key
and `api.hunter.io` egress already live; Supabase connector; tools Bash +
`mcp__Supabase__execute_sql`). Manage via the `schedule` skill / RemoteTrigger, or
at https://claude.ai/code/routines/trig_01EZuVzMvdW5Gyq5VVyvKjgk. It is created
PAUSED — enable it or fire your own trigger to run it.
