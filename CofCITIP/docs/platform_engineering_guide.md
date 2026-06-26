# CofCITIP Platform Engineering Guide

> Practical implementation reference for the platform engineering standards defined in the README.  
> Libraries, GitHub repos, patterns, and code examples for each requirement.

---

## 1. Structured Logging

**Why:** `print()` statements can't be filtered, searched, or shipped to a log aggregator. Structured JSON logs can.

**Library:** Python `structlog` — https://github.com/hynek/structlog

```bash
pip install structlog
```

```python
import structlog

log = structlog.get_logger()

# Instead of: print(f"Querying Intune for {filter}")
log.info("intune.query", filter=filter, tool="query_intune_compliance")

# Output (JSON):
# {"event": "intune.query", "filter": "non-compliant", "tool": "query_intune_compliance", "timestamp": "..."}
```

**Pattern for every tool call:**
```python
log.info("tool.start", tool=tool_name, params=params)
try:
    result = call_api(params)
    log.info("tool.success", tool=tool_name, duration_ms=elapsed, result_count=len(result))
    return result
except Exception as e:
    log.error("tool.error", tool=tool_name, error=str(e))
    raise
```

**Log levels to use:**
- `DEBUG` — API request/response bodies (verbose, off by default)
- `INFO` — every tool call start/success
- `WARN` — degraded state, retrying, circuit open
- `ERROR` — tool failed, connector down

---

## 2. Health Check Endpoints

**Why:** Uptime Kuma and the monitoring stack need to check more than "is the process running." A health endpoint verifies dependencies are reachable.

**Library:** `fastapi` (lightweight, async) — https://github.com/tiangolo/fastapi

```bash
pip install fastapi uvicorn
```

```python
from fastapi import FastAPI
import httpx

app = FastAPI()

@app.get("/health")
async def health():
    checks = {}
    
    # Check Ollama
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        checks["ollama"] = "ok" if r.status_code == 200 else "degraded"
    except:
        checks["ollama"] = "down"
    
    # Check ChromaDB
    try:
        import chromadb
        client = chromadb.Client()
        checks["chromadb"] = "ok"
    except:
        checks["chromadb"] = "down"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
```

Point Uptime Kuma at `http://bb-ip:8080/health` — alerts if status is not `ok`.

---

## 3. Retry with Exponential Backoff

**Why:** APIs go temporarily unavailable. Blind retries hammer them. Backoff with jitter is the standard pattern.

**Library:** `tenacity` — https://github.com/jd/tenacity

```bash
pip install tenacity
```

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import httpx

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(httpx.HTTPError)
)
def call_intune_api(endpoint: str, params: dict) -> dict:
    response = httpx.get(endpoint, params=params, timeout=10)
    response.raise_for_status()
    return response.json()
```

**Configuration per connector** (in `config.env`):
```bash
INTUNE_MAX_RETRIES=3
INTUNE_RETRY_BACKOFF=2
JAMF_MAX_RETRIES=3
```

---

## 4. Circuit Breaker

**Why:** A flaky API that keeps timing out will block JARVIS. The circuit breaker trips after N failures, stops trying, and auto-recovers after a cooldown.

**Library:** `pybreaker` — https://github.com/danielfm/pybreaker

```bash
pip install pybreaker
```

```python
import pybreaker

# One breaker per external service
intune_breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60)
jamf_breaker   = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=60)

@intune_breaker
def query_intune(params):
    # ... API call
    pass

# When circuit is OPEN, pybreaker raises CircuitBreakerError immediately
# JARVIS catches it and responds: "Intune is currently unreachable, try again shortly"
```

**Wire up state change alerts:**
```python
class BreakerListener(pybreaker.CircuitBreakerListener):
    def state_change(self, cb, old_state, new_state):
        if new_state.name == "open":
            send_teams_alert(f"⚠️ Circuit OPEN: {cb.name} is down")
        elif new_state.name == "closed":
            send_teams_alert(f"✅ Circuit CLOSED: {cb.name} recovered")
```

---

## 5. Job Queue (Async Write Operations)

**Why:** Write operations (create ticket, deploy package, update device) should never block the voice pipeline. Queue them, execute async, confirm when done.

**Library:** `rq` (Redis Queue) — https://github.com/rq/rq  
**Requires:** Redis (lightweight, self-hosted)

```bash
pip install rq redis
sudo apt install redis-server
```

```python
from redis import Redis
from rq import Queue

redis_conn = Redis()
q = Queue(connection=redis_conn)

# In jarvis_core.py — queue a write operation
def create_ticket_async(title: str, description: str, priority: str):
    job = q.enqueue(
        tools.servicenow.create_ticket,
        title=title,
        description=description,
        priority=priority,
        job_timeout=30
    )
    return f"Ticket creation queued. Job ID: {job.id}"

# Check status later
def check_job(job_id: str):
    from rq.job import Job
    job = Job.fetch(job_id, connection=redis_conn)
    return {"status": job.get_status(), "result": job.result}
```

**Alternative (no Redis):** `dramatiq` with a SQLite backend for simpler single-node setup — https://github.com/Bogdanp/dramatiq

---

## 6. Audit Log

**Why:** Every write action JARVIS takes needs an immutable record. Compliance, debugging, accountability.

**Approach:** Append-only SQLite via `sqlite3` (no extra dependencies). Shipped to Node 2 nightly.

```python
import sqlite3
import json
from datetime import datetime

AUDIT_DB = "/var/lib/cofc-itip/audit.db"

def init_audit_log():
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action    TEXT NOT NULL,
            tool      TEXT NOT NULL,
            params    TEXT NOT NULL,
            result    TEXT,
            confirmed_by TEXT DEFAULT 'human'
        )
    """)
    conn.commit()

def audit(action: str, tool: str, params: dict, result: dict = None):
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute(
        "INSERT INTO audit_log (timestamp, action, tool, params, result) VALUES (?,?,?,?,?)",
        (datetime.utcnow().isoformat(), action, tool, json.dumps(params), json.dumps(result))
    )
    conn.commit()
```

**Usage — always called after confirmed write operations:**
```python
# Human confirms → action executes → audit fires
audit("create_ticket", "servicenow.create_ticket", params, result)
```

---

## 7. Input Validation (Tool Schema Enforcement)

**Why:** Bad inputs to API calls cause confusing downstream errors. Validate at the tool boundary before touching any external API.

**Library:** `pydantic` — https://github.com/pydantic/pydantic (v2)

```bash
pip install pydantic
```

```python
from pydantic import BaseModel, field_validator
from typing import Literal, Optional

class IntuneComplianceParams(BaseModel):
    filter: Literal["compliant", "non-compliant", "all"] = "all"
    platform: Optional[str] = None

class CreateTicketParams(BaseModel):
    title: str
    description: str
    priority: Literal["1", "2", "3", "4"] = "3"
    
    @field_validator("title")
    def title_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Ticket title cannot be empty")
        return v

# In tool function:
def query_intune_compliance(raw_params: dict) -> dict:
    params = IntuneComplianceParams(**raw_params)  # raises ValidationError with clear message if invalid
    # ... proceed with clean, typed params
```

---

## 8. Mock Mode

**Why:** You need to develop and test tools without live API credentials or risk hitting production systems.

**Pattern:** Environment flag + mock data module per connector.

```python
import os

MOCK_MODE = os.getenv("JARVIS_MOCK", "false").lower() == "true"

def query_intune_compliance(params: dict) -> dict:
    if MOCK_MODE:
        return mock_data.intune_compliance(params)
    return _live_intune_query(params)
```

```bash
# Run in mock mode
JARVIS_MOCK=true python jarvis_core.py
```

**Mock data module** (`tools/mock_data.py`) — realistic-shaped data matching production API responses. Already partially exists in the project.

---

## 9. Rate Limiting (Outbound API calls)

**Why:** Graph API, Jamf, and ServiceNow all have rate limits. Exceeding them causes throttling or bans.

**Library:** `ratelimit` — https://github.com/tomasbasham/ratelimit

```bash
pip install ratelimit
```

```python
from ratelimit import limits, sleep_and_retry

GRAPH_API_CALLS_PER_SECOND = 10

@sleep_and_retry
@limits(calls=GRAPH_API_CALLS_PER_SECOND, period=1)
def call_graph_api(endpoint: str, params: dict):
    # ... httpx call
    pass
```

**Per-connector limits (approximate):**
| API | Limit |
|-----|-------|
| Microsoft Graph | 10 req/sec per app |
| Jamf Pro | 100 req/min |
| ServiceNow | Varies by instance; start conservative at 20 req/min |
| Taegis GraphQL | TBD — confirm with Alejandro |

---

## 10. Tool Test Harness

**Why:** Run any tool in isolation without spinning up the full JARVIS stack.

**Pattern:** `__main__` block in every tool file + a shared test runner.

```python
# tools/intune.py — bottom of file
if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser()
    parser.add_argument("--function", required=True)
    parser.add_argument("--params", default="{}")
    args = parser.parse_args()
    
    params = json.loads(args.params)
    fn = globals()[args.function]
    result = fn(params)
    print(json.dumps(result, indent=2))
```

```bash
# Test a tool directly
python -m tools.intune --function query_intune_compliance --params '{"filter": "non-compliant"}'

# With mock data
JARVIS_MOCK=true python -m tools.intune --function query_intune_compliance --params '{}'
```

---

## 11. Graceful Degradation

**Why:** If one connector is unavailable, JARVIS should still function for everything else.

**Pattern:** Per-connector availability state + aggregator-level try/except.

```python
CONNECTOR_STATUS = {
    "intune": True,
    "jamf": True,
    "taegis": True,
    "servicenow": True,
}

def morning_briefing() -> str:
    sections = []
    
    for name, fn in [("Intune", get_intune_summary), ("Jamf", get_jamf_summary)]:
        try:
            sections.append(fn())
        except pybreaker.CircuitBreakerError:
            sections.append(f"⚠️ {name}: currently unreachable")
        except Exception as e:
            log.error("briefing.section_failed", source=name, error=str(e))
            sections.append(f"⚠️ {name}: data unavailable")
    
    return "\n\n".join(sections)
```

JARVIS never returns a silent failure — always tells you what it couldn't reach.

---

## 12. Idempotency (Safe Retries on Write Operations)

**Why:** Network blips can cause retries. Without idempotency, you get duplicate tickets, double-deployed packages, or conflicting updates.

**Pattern:** Idempotency key on all write operations.

```python
import hashlib

def create_ticket(title: str, description: str, priority: str) -> dict:
    # Idempotency key = hash of the content
    idem_key = hashlib.sha256(f"{title}{description}{priority}".encode()).hexdigest()[:16]
    
    # Check if we already created this ticket
    existing = check_recent_tickets(idem_key)
    if existing:
        log.warn("ticket.duplicate_prevented", idem_key=idem_key, existing_id=existing["id"])
        return existing
    
    # Create with key stored
    result = _sn_create_ticket(title, description, priority, idem_key=idem_key)
    return result
```

---

## 13. Useful GitHub Repos to Watch

| Repo | What it is | Why relevant |
|------|-----------|-------------|
| [ollama/ollama](https://github.com/ollama/ollama) | LLM runtime | Core of the stack — watch for model updates and API changes |
| [chroma-core/chroma](https://github.com/chroma-core/chroma) | Vector DB | JARVIS memory layer |
| [hynek/structlog](https://github.com/hynek/structlog) | Structured logging | Platform observability |
| [jd/tenacity](https://github.com/jd/tenacity) | Retry library | Resilient API calls |
| [danielfm/pybreaker](https://github.com/danielfm/pybreaker) | Circuit breaker | Connector resilience |
| [rq/rq](https://github.com/rq/rq) | Job queue | Async write operations |
| [pydantic/pydantic](https://github.com/pydantic/pydantic) | Data validation | Tool input validation |
| [tiangolo/fastapi](https://github.com/tiangolo/fastapi) | API framework | Health endpoints |
| [louislam/uptime-kuma](https://github.com/louislam/uptime-kuma) | Service monitoring | Watches JARVIS itself |
| [grafana/grafana](https://github.com/grafana/grafana) | Dashboards | Fleet + platform visibility |
| [prometheus/prometheus](https://github.com/prometheus/prometheus) | Metrics | Platform + GPU metrics |

---

## Implementation Priority

Build these in order — each layer supports the next:

1. **Structured logging** — foundational, add to every tool from day one
2. **Mock mode** — enables all development without live credentials
3. **Input validation (pydantic)** — add alongside each tool as it's built
4. **Retry + backoff (tenacity)** — add to every external API call
5. **Health endpoints (fastapi)** — wire up once first tools are working
6. **Circuit breaker (pybreaker)** — add after retry logic is stable
7. **Audit log (sqlite)** — required before any write operations go live
8. **Job queue (rq)** — add before first write operation (ticket creation)
9. **Idempotency** — implement alongside each write tool
10. **Graceful degradation** — integrate into briefing and aggregation tools
11. **Tool test harness** — build once, reuse for every tool

---

*Reference: `CofCITIP_README.md` — Platform Engineering Standards section*  
*Repo: `cofc-it-ops/CofCITIP/docs/platform_engineering_guide.md`*
