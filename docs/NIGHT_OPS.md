# Memory API v2 — Nocne Operacje

## Cel

Automatyczna higiena pamięci w czasie gdy nikt nie pracuje na WSL2.
Hermes cronjob wywołuje endpointy Memory API v2 przez `curl`.

## Harmonogram

### 02:00 — Temporal Decay
**Endpoint:** `POST /memories/decay`
**SQL:** `UPDATE memories SET trust_score = trust_score * POWER(0.5, age_days/365) WHERE trust_score < 0.8 AND archived_at IS NULL`
**Co:** Stare fakty z niskim trust tracą jeszcze więcej. Ważne (trust ≥ 0.8) nie decay-ują.
**Expected response:** `{"status": "ok", "updated_count": 45}`

### 02:15 — Expire Memories
**Endpoint:** `POST /memories/expire`
**SQL:** `UPDATE memories SET archived_at = NOW() WHERE expires_at IS NOT NULL AND expires_at < NOW() AND archived_at IS NULL`
**Co:** Archive memories past their `expires_at` (per-fact TTL set at creation)
**Expected response:** `{"status": "ok", "expired": 3}`

### 02:30 — Contradiction Detection
**Endpoint:** `POST /hygiene/run`
**Co:** Znajduje pary faktów które:
- Mają ≥2 wspólne entities
- Mają content similarity < 0.3
- Oba mają `trust_score > 0.5`
**Action:** Flaguje jako `contradictions JSONB` — nie usuwa, tylko oznacza
**Expected response:** `{"status": "ok", "contradictions_found": 3, "pairs": [...]}`

### 03:00 — Entity Resolution
**Endpoint:** `POST /entities/resolve`
**Co:** Wykrywa duplicate entities:
- `Rafał` vs `rafal-shark` vs `Rafał (szark)` → merge
- Aktualizuje `entity_aliases` i `fact_entity_links`
**Expected response:** `{"status": "ok", "resolved": 5, "merged": 2}`

### 03:30 — Trust Recalculation
**Endpoint:** `POST /hygiene/trust`
**Co:** Recalculates trust na podstawie ostatniego feedback:
- `trust = base + Σ(feedback_delta * 0.5^(days_since/30))`
- Clamp do [0.0, 1.0]
**Expected response:** `{"status": "ok", "updated_count": 120}`

### 04:00 — Archive Low Trust
**Endpoint:** `DELETE /memories/purge`? `max_trust=0.2&min_age_days=90`
**Co:** Archive (soft delete) pamięci z:
- `trust_score < 0.2` AND `age > 90 dni` LUB
- `access_count = 0` AND `age > 180 dni`
**Expected response:** `{"status": "ok", "archived_count": 15}`

### 04:30 — Purge Old Archive (niedziele tylko)
**Endpoint:** `DELETE /memories/purge`? `archived_before_days=30`
**Co:** Finalne usunięcie zarchiwizowanych > 30 dni
**To jest destruckcyjne!** Backup musi być zrobiony before.

### 05:00 — Cache Rebuild
**Endpoint:** `POST /cache/rebuild`
**Co:** Pre-compute embeddingi dla często wyszukiwanych zapytań
**Expected response:** `{"status": "ok", "cached": 25}`

### 05:30 — Stats Snapshot
**Endpoint:** `POST /stats/snapshot`
**Co:** Zapisuje daily stats → `daily_stats` table
**Expected response:** `{"status": "ok", "date": "2026-05-12"}`

## Monitorowanie

### Logi
- Każdy ops loguje do `/tmp/night-ops-YYYY-MM-DD.log`
- Format: `{timestamp, endpoint, status, affected_count, duration_ms}`

### Alerting
- Fail → alert na Telegram (via Hermes)
- Success → cicho (watchdog pattern)

### Rollback
- Każdy ops jest w transakcji
- Przy błędzie → `ROLLBACK`, zero zmian
- Backup jest przed purge (co 6h)

## Cronjob Setup (via Hermes)

Każy cronjob to:
```python
cronjob(action='create',
    schedule='120m',  # 02:00
    prompt='curl -s -X POST http://localhost:8766/memories/decay',
    deliver='local',
    name='memory-decay',
    workdir='/home/ArndtOs/Tools/memory-api-v2')
```

## Przykładowy Log

```
2026-05-22 02:00:00 | POST /memories/decay | status=ok | updated=45 | 120ms
2026-05-22 02:15:00 | POST /memories/expire | status=ok | expired=3 | 30ms
2026-05-13 02:30:00 | POST /hygiene/run | status=ok | contradictions=3 | 450ms
2026-05-13 03:00:00 | POST /entities/resolve | status=ok | resolved=5 | 300ms
2026-05-13 03:30:00 | POST /hygiene/trust | status=ok | updated=120 | 200ms
2026-05-13 04:00:00 | DELETE /memories/purge | status=ok | archived=15 | 150ms
2026-05-13 05:00:00 | POST /cache/rebuild | status=ok | cached=25 | 800ms
2026-05-13 05:30:00 | POST /stats/snapshot | status=ok | date=2026-05-13 | 50ms
```