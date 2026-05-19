# Designservice server-side files (snapshots)

Snapshots of files living on https://designservice.group/public_html/.
Kept here for version control / rollback.

| File | Endpoint |
|---|---|
| `_bot_api.php` | https://designservice.group/_bot_api.php |

## Actions in _bot_api.php

| Action | Method | Description |
|---|---|---|
| `get_planned` | GET | Today's planned (limit/sort) |
| `get_article` | GET | Single article |
| `stats` | GET | Counts by status |
| `mark_status` | POST | Atomic status update |
| `get_next_for_pipeline` | GET | Next planned with service rotation + backfill (PR 22, 23) |
| `recent_published` | GET | Last N published, server-side scan (PR 24) |

## Deploy procedure

```bash
FILE=docs/designservice_server/_bot_api.php
base64 -w0 "$FILE" | curl -X POST --data-binary @- \
  "https://designservice.group/_receiver.php?k=wr_pj_2026_v1&p=_bot_api.php"
```
