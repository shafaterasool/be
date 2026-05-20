# Bot v10 PRO — Architecture Upgrade

## Key Changes (v9 → v10)

### ✅ #11 — Global Page Ownership (DATABASE FIX — MOST IMPORTANT)

**Problem (v9):**
```
fb_pages: UNIQUE(account_id, page_id)
→ Same page appear karta tha 5 accounts mein = 5 rows
→ User ko lagta tha: duplicate entries hain
→ Token conflicts: system nahi jaanta konsa token valid hai
```

**Solution (v10):**
```
pages:         UNIQUE(page_id)  ← globally one row per page
account_pages: account_id + page_id bridge table (many-to-many)
```

**Architecture:**
```
fb_accounts ──┐
              ├── account_pages ──→ pages (1 row per page globally)
fb_accounts ──┘
```

**Token Conflict Resolution:**
- Jab naya account same page add kare → conflict detect hota hai
- Newest token automatically primary banta hai
- `pages.primary_token` = always the best valid token
- Dashboard mein: page sirf ek baar dikhta hai, primary owner ke saath

---

### ✅ #12 — Structured Logging

New `structured_logs` table:
```json
{
  "ts":          "2026-01-01T12:00:00+00:00",
  "level":       "ERROR",
  "request_id":  "A1B2C3D4",
  "channel_id":  5,
  "message":     "FB upload failed",
  "stack_trace": "Traceback (most recent call last):\n...",
  "attempt":     2,
  "max_attempts": 3
}
```

Log levels: `DEBUG / INFO / WARNING / ERROR / CRITICAL / RETRY / REQUEST / TOKEN_CHECK / OWNERSHIP`

**Access:**
```
GET /api/structured_logs?channel_id=5&level=ERROR&limit=50
```

---

### ✅ #13 — PostgreSQL Support

```bash
# SQLite (default, no changes needed):
python app.py

# PostgreSQL:
DATABASE_URL=postgresql://user:pass@localhost:5432/botdb python app.py
```

**Why PostgreSQL for scale:**
- 1000+ accounts → SQLite bottleneck hoga
- Multiple workers → PostgreSQL handles concurrent writes better
- Connection pooling → 50 connections max pool

---

### ✅ #14 — Token Conflict Prevention

```
Old: Same page ke 3 tokens → random kaunsa use ho?
New: 
  → validate_page_token(): FB API se check karo
  → get_best_token_for_page(): sab tokens try karo, valid wala promote karo
  → Auto-promotion: agar primary token invalid ho, working token primary banta hai
```

**APIs:**
```
POST /api/pages/{page_id}/token_check   → validate current token
POST /api/pages/{page_id}/best_token    → resolve best valid token
GET  /api/pages/conflicts               → show multi-account pages
```

---

### ✅ #15 — Ownership Transfer on Disconnect

```
Old: Account disconnect → pages orphaned (no owner, no token)
New: Account disconnect → pages auto-transferred to next best active account
```

---

## Migration Steps

```bash
# 1. Backup existing database
cp data.db data_backup_$(date +%Y%m%d).db

# 2. Run migration (safe, idempotent)
python migrate.py

# 3. Install new dependencies
pip install -r requirements.txt

# 4. Start bot
python app.py
```

---

## New API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/pages` | GET | All unique pages (deduplicated) |
| `/api/pages/{id}` | GET | Single page info + owner |
| `/api/pages/{id}/token_check` | POST | Validate token via FB API |
| `/api/pages/{id}/best_token` | POST | Resolve best valid token |
| `/api/pages/conflicts` | GET | Multi-account token conflicts |
| `/api/structured_logs` | GET | Structured logs with request IDs |

---

## File Changes

| File | Status | Changes |
|------|--------|---------|
| `db.py` | 🆕 NEW | SQLite + PostgreSQL abstraction |
| `structured_logger.py` | 🆕 NEW | Request IDs, stack traces, retry logs |
| `fb_oauth.py` | 🔄 REWRITTEN | New schema + ownership logic |
| `bot_worker.py` | 🔄 UPGRADED | New tables + structured logging |
| `app.py` | 🔄 UPGRADED | New API endpoints |
| `migrate.py` | 🆕 NEW | v9 → v10 migration script |
| `requirements.txt` | 🔄 UPDATED | Added psycopg2-binary |
| `page_monitor.py` | ✅ UNCHANGED | |
| `templates/index.html` | ✅ UNCHANGED | |
