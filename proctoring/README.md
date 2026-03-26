# Proctoring

## Tumhara flow (haan, sahi hai)

Har interview attempt = ek **`assessment_results`** document (unique `_id` / `assessment_result_id`).  
Proctoring data **usi document ke andar** `proctoring` field mein save hota hai — trust score, events, counts interview khatam par — taaki **result ke saath hi** integrity data mile.

## Document shape (Dev → `assessment_results`)

Tumhare existing fields ke saath nested:

```json
{
  "_id": "...",
  "...": "existing assessment result fields",
  "proctoring": {
    "environment": "dev",
    "assessment_id": "...",
    "user_id": "...",
    "video_token": "...",
    "started_at": "ISO",
    "ended_at": "ISO | null",
    "trust_score": 85,
    "counts": { "tab_switched": 1, "went_offline": 0 },
    "events": [
      { "event_type": "tab_switched", "occurred_at": "ISO", "metadata": {}, "evidence_url": "" }
    ],
    "client_info": {}
  }
}
```

**Pehle `assessment_results` row honi chahiye** — interview start se pehle jo system result create karta hai (Lambda / API). Agar row nahi mili to proctoring API `404` degi.

## API (unchanged paths)

- `POST /v1/sessions` — `assessment_result_id` wali row par `proctoring` init karta hai. Response `session_id` = wahi `assessment_result_id` (frontend compatible).
- `POST /v1/sessions/{id}/events` — `id` = `assessment_result_id` — events array mein `$push`.
- `PATCH /v1/sessions/{id}/end` — `trust_score` + `counts` + `ended_at` set.
- `GET /v1/report` — embedded `proctoring` se report.

## Env

`proctoring/backend/.env`: `MONGODB_URI`, `MONGODB_DB_NAME=Dev`, keys — [`.env.example`](backend/.env.example).

## Dashboard login

Collection `proctor_dashboard_users` (same `Dev` DB) — sirf admin login; candidate data `assessment_results` mein hi.
