# Insta Leads Enrichment

Enriches `public.insta_stories` rows (Supabase project `MAGTestProject`,
`aivitcomiywiysrfwqxt`) with points of contact looked up on **Hunter.io**.

## What it does

For every pending row (`org` is not null AND `enrichment_is_processed` is
null/false):

1. Calls Hunter.io **Domain Search** on the `org` name.
2. Inserts **one new row per contact** found, copying the source row's other
   fields (speaker, image/video urls, status, insta_handle, description, org,
   event_name, saved_img_link, is_added_to_sheet) and filling
   `poc` / `poc_email` / `poc_position`. New rows are inserted with
   `enrichment_is_processed = true` so they are never re-enriched.
3. Marks the source row `enrichment_is_processed = true` — even when no contact
   is found.

## Setup

- **Hunter.io key** — already present in `hunter_io_api_key.txt` (or set
  `HUNTER_API_KEY`).
- **Supabase service_role key** — required for the script to write via
  PostgREST. Put it in `supabase_service_key.txt` (next to the script) or set
  `SUPABASE_SERVICE_KEY`. Get it from Supabase → Project Settings → API →
  `service_role` secret. **This file is a secret — do not commit it.**
- Python 3 with `requests` (`pip install requests`).

## Run

```
python enrich_leads.py            # enrich all pending rows
python enrich_leads.py --dry-run  # look up + print, write nothing
```

## Caveats

- **Hunter's plan caps Domain Search at 10 emails per org** (the script requests
  `limit=10`).
- **Company-name → domain resolution can be wrong.** Hunter guesses the domain
  from the org name; for small/ambiguous orgs it may return an unrelated
  company's contacts (e.g. "HerTri London" resolved to `kerrylondon.co.uk`).
  Review new rows for orgs whose domain looks off. To be safe you can enrich by
  an exact domain instead of a name by editing the Hunter call to pass
  `domain=` instead of `company=`.
- **Duplicate source rows for the same org are enriched independently**, so the
  same contacts are inserted once per source row.
