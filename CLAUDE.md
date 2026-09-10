# CLAUDE.md — insta_leads_enrichment

Lead-enrichment for the Supabase `insta_stories` table: look each lead's `org` up
on Hunter.io and populate points of contact.

## What this project is

For every pending row (`org IS NOT NULL AND (enrichment_is_processed IS NULL OR =
false)`), call Hunter.io **Domain Search** on the org name and **expand the row
into one new row per contact** — copying the source row's other fields and filling
`poc` / `poc_email` / `poc_position`. Then mark the source row
`enrichment_is_processed = true` (even when no contact is found). This is an
expansion model, not a single-best-POC pick.

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
