# Redis Scale-Out — Distributed Live-State Cutover

**Status:** design / ready to execute against a real Redis instance.
**Goal:** run JustHereToListen.io across **multiple API/worker processes** so live
bot state is shared and `MAX_CONCURRENT_BOTS` is enforced *cluster-wide*, instead
of being pinned to a single process.

> This document is the executable plan for the one substantial reliability item
> left after the v2.64–v2.65 hardening pass. The contract groundwork is already
> merged; what remains needs a running Redis instance to validate (fakeredis
> cannot exercise cross-process coordination), so it is intentionally **not**
> bundled into a CI-only PR.

---

## 1. Why this matters

Today the process holds three things in memory that block horizontal scaling:

| State | Where | Problem when >1 worker |
|---|---|---|
| Live bot sessions | `app.store.store` (in-memory `Store` singleton) | A bot created on worker A is invisible to worker B → 404s, lost polling, missing webhooks under a non-sticky load balancer |
| Run queue + running tasks | `_bot_queue`, `_running_tasks`, `_queue_event` in `app/api/bots.py` | Each worker counts only its own bots, so the global `MAX_CONCURRENT_BOTS` cap is violated N× across N workers → resource exhaustion |
| Runtime handles (Playwright `page`, asyncio tasks, PulseAudio) | `BotSession.runtime`, `_running_tasks` | **Inherently process-local** — cannot be serialized. Only the worker actually driving the browser can `/say`, `/leave`, etc. |

The first two are solvable with Redis. The third dictates the architecture: **a
bot's browser runs on exactly one worker, and control operations must be routed
to that worker.**

---

## 2. What's already in place (merged)

- **`store_interface.BotStateStore`** — explicit `Protocol` for the live-state
  surface: `create_bot`, `get_bot`, `update_bot`, `get_bot_by_share_hash`,
  `list_bots`, `delete_bot`, `list_live_bots`. `@runtime_checkable`.
- **`redis_store.RedisBotStateStore`** — full implementation of that protocol
  against Redis (JSON value per bot + a `created_at`-scored sorted-set index +
  a share-hash hash). Covered by `tests/test_redis_store.py` (fakeredis),
  including a `test_conforms_to_protocol` guard that **forces parity** whenever
  the protocol grows.
- **`store.get_bot_state_store()`** — returns the in-memory `Store` by default,
  or `RedisBotStateStore` when `BOT_STATE_BACKEND=redis` **and** `REDIS_URL` set.
- Config flags: `BOT_STATE_BACKEND` (`memory` | `redis`), `REDIS_URL`.

**Default remains `memory` — nothing in this plan changes single-process behaviour
until the flag is flipped.**

---

## 3. Target architecture

```
            ┌───────────── Redis ─────────────┐
            │  live bot state (BotStateStore) │
            │  run queue + slot counter       │
            │  control-channel pub/sub        │
            └─────────────────────────────────┘
              ▲          ▲             ▲
   ┌──────────┴┐  ┌──────┴─────┐  ┌────┴───────┐
   │  Worker A │  │  Worker B  │  │  Worker C  │   (each: API + bot runner)
   │ runs bots │  │ runs bots  │  │ runs bots  │
   └───────────┘  └────────────┘  └────────────┘
```

- **Live state**: all reads/writes of `BotSession` go through
  `get_bot_state_store()`. The DB (`BotSnapshot`, accounts, webhooks) is already
  shared and unchanged.
- **Queue**: a Redis list/stream holds queued bot IDs; an atomic Redis counter
  (or a `SETNX`-per-slot scheme) enforces the global concurrency cap. Any worker
  can dequeue and run a bot.
- **Runtime/control**: the worker that wins a bot records `runner_id =
  <worker-id>` on the bot. Control endpoints (`/say`, `/chat`, `/leave`,
  `/cancel`) publish a command on a per-bot Redis pub/sub channel; the owning
  worker subscribes and applies it to its local Playwright `page`.

---

## 4. The three hard parts

### 4.1 `mark_terminal` must become backend-aware
Currently `Store.mark_terminal` does **two** things: (a) mutate in-memory live
state + set TTL, and (b) persist the terminal `BotSnapshot` to the DB. In
distributed mode (a) must target the **shared** live store. Plan:

- Move the terminal transition into a small module-level helper
  `finalize_bot(bot_id, status, **kwargs)` that:
  1. `await get_bot_state_store().update_bot(...)` (or delete from live set),
  2. writes the `BotSnapshot` to the DB (shared infra — unchanged),
  3. handles TTL/expiry.
- Keep `Store.mark_terminal` as a thin wrapper for the memory path so existing
  call sites keep working during migration.
- Extend the `BotStateStore` protocol with the minimal terminal primitive the
  helper needs (e.g. `set_terminal(bot_id, status, expires_at, **kwargs)`), and
  implement it on `RedisBotStateStore` (the conformance test will enforce parity).
- Update `reap_stuck_bots` to list/terminate via `get_bot_state_store()` so the
  reaper works in both modes (today it is correct only for memory).

### 4.2 Distributed queue + global concurrency cap
Replace the per-process structures in `app/api/bots.py`:

- `_bot_queue` (deque) → Redis list `jhtl:queue` (LPUSH/BRPOP) or a stream.
- `_running_count` → atomic Redis counter `jhtl:running` with **lease keys**
  (`jhtl:slot:<bot_id>` with a TTL heartbeat) so a crashed worker's slot is
  auto-reclaimed when its lease expires — critical to avoid permanently leaking
  capacity.
- `_queue_event` → `BRPOP` blocking pop (no event needed across processes).
- `_queue_processor` runs on every worker; each pops work only if it can acquire
  a slot lease.
- `_running_tasks` stays **process-local** (it tracks asyncio tasks for bots this
  worker is actually running) — it is the local view, not the global truth.

### 4.3 Routing control ops to the owning worker
- On win, set `bot.runner_id` + subscribe the worker to `jhtl:ctl:<bot_id>`.
- `/say`, `/chat`, `/leave`, `/cancel`: if `bot.runner_id == self`, apply
  locally (current behaviour); else `PUBLISH` the command and return 202.
- Heartbeat `runner_id`'s lease; on expiry the bot is considered dead → reaper
  finalizes it.

---

## 5. Call-site migration (ordered, behind the flag)

16 files import the singleton (`from app.store import store`). Migrate in
dependency order, each as its own commit, verifying memory-mode tests stay green:

1. **Read-only API paths** first (lowest risk): `api/analytics.py`,
   `api/exports.py`, `api/ui.py` read views → `get_bot_state_store()`.
2. `api/bots.py` read paths (`get`, `list`), then create/queue.
3. `services/bot_service.py` lifecycle reads/writes + the `finalize_bot` helper.
4. `services/webhook_service.py`, `services/calendar_service.py`,
   `services/memory_service.py`, `services/consent_service.py`.
5. `api/webhooks.py`, `api/admin.py`, `api/auth.py`.
6. `services/browser_bot.py`, `services/transcription_service.py`,
   `services/mcp_service.py` — these mostly touch `runtime` (process-local) and
   need the least change.

Webhook persistence, the share index, snapshot writes, and `cleanup_expired`
stay on `Store`/DB (already shared) — out of scope for the live-state cutover.

---

## 6. Config & deploy

```bash
BOT_STATE_BACKEND=redis
REDIS_URL=redis://<host>:6379/0
MAX_CONCURRENT_BOTS=<per-cluster total, not per-worker>
# Optional new knobs introduced by this work:
BOT_SLOT_LEASE_SECONDS=60        # slot/runner lease TTL (heartbeated)
```

- Railway: add a Redis plugin; set the env vars on the service.
- `main.py` lifespan: when `redis`, connect + ping on startup, fail fast if
  unreachable; ensure the queue processor and reaper use the shared store.

---

## 7. Rollout / fallback

1. Ship the code with `BOT_STATE_BACKEND=memory` (default) — no behaviour change.
2. Provision Redis; enable on **one** worker (still effectively single-process)
   to validate read/write/terminal parity against real Redis.
3. Scale to 2 workers behind the LB; run the multi-worker test matrix (§8).
4. Remove LB sticky-session pinning once verified.
5. **Fallback:** flip `BOT_STATE_BACKEND=memory` + scale to 1 worker. Because the
   accessor is read at call time, this is a config rollback, not a code revert.
   (In-flight bots' live state in Redis is lost on fallback — drain first.)

---

## 8. Testing strategy

- **Unit (CI, fakeredis):** extend `tests/test_redis_store.py` for the new
  terminal primitive + queue helpers. The protocol-conformance test continues to
  enforce parity automatically.
- **Integration (real Redis, not in default CI):** a `docker-compose` Redis +
  two uvicorn workers; assert: create on A → visible on B; global cap honoured
  across workers; control op on B routed to A; crashed-worker slot reclaimed
  after lease TTL; reaper finalizes a dead bot.
- **Soak:** N concurrent bots across 3 workers for an extended run; watch slot
  counter never exceeds the cap and never permanently leaks capacity.

---

## 9. Risks / open questions

- **Slot leak on crash** — mitigated by lease TTLs (§4.2); pick a TTL safely
  above the heartbeat interval.
- **Control-op latency** — pub/sub adds a hop; acceptable for `/say`/`/leave`.
- **Split brain** — two workers both think they own a bot. Guard with a single
  atomic `SET runner NX` on win; loser backs off.
- **Serialization drift** — `BotSession.to_state_dict`/`from_state_dict` already
  back the Redis store; keep them the single source of truth and round-trip-test.
- **Partial migration window** — while some call sites use the singleton and
  others the accessor, run `memory` mode only. Do **not** enable `redis` until
  every live-state call site is migrated.
