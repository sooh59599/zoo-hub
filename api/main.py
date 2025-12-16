import os, json, uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import psycopg
import aio_pika
from dotenv import load_dotenv

load_dotenv()  # .env 로드

DATABASE_URL = os.getenv("DATABASE_URL", "")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "")

EVENTS_EXCHANGE = os.getenv("EVENTS_EXCHANGE", "zoo.events")
EVENTS_ROUTING_KEY = os.getenv("EVENTS_ROUTING_KEY", "zoo.event.ingested")

app = FastAPI(title="Zoo Hub API", version="0.3")

# ---------- DTO ----------
class Subject(BaseModel):
  kind: str
  id: str

class IngestEventRequest(BaseModel):
  source: str
  type: str
  subject: Subject
  payload: dict = Field(default_factory=dict)
  occurredAt: Optional[str] = None
  idempotencyKey: Optional[str] = None

class RuleMatch(BaseModel):
  source: Optional[str] = None
  type: Optional[str] = None

class RuleAction(BaseModel):
  kind: str  # EMAIL | WEBHOOK
  config: dict = Field(default_factory=dict)
  orderNo: int = 0

class CreateRuleRequest(BaseModel):
  name: str
  enabled: bool = True
  match: RuleMatch = Field(default_factory=RuleMatch)
  actions: list[RuleAction] = Field(default_factory=list)

class UpdateRuleRequest(BaseModel):
  name: Optional[str] = None
  enabled: Optional[bool] = None
  match: Optional[RuleMatch] = None
  actions: Optional[list[RuleAction]] = None

# ---------- DB helpers ----------
def db():
  return psycopg.connect(DATABASE_URL)

def parse_occurred_at(s: Optional[str]) -> datetime:
  if not s:
    return datetime.now(timezone.utc)
  return datetime.fromisoformat(s.replace("Z", "+00:00"))

# ---------- MQ ----------
class MQ:
  def __init__(self):
    self.conn = None
    self.channel = None
    self.exchange = None

  async def connect(self):
    self.conn = await aio_pika.connect_robust(RABBITMQ_URL)
    self.channel = await self.conn.channel()
    self.exchange = await self.channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)

  async def publish_event(self, message: dict):
    body = json.dumps(message).encode()
    msg = aio_pika.Message(body=body, content_type="application/json", delivery_mode=aio_pika.DeliveryMode.PERSISTENT)
    await self.exchange.publish(msg, routing_key=EVENTS_ROUTING_KEY)

  async def close(self):
    if self.conn:
      await self.conn.close()

mq = MQ()

@app.on_event("startup")
async def _startup():
  await mq.connect()

@app.on_event("shutdown")
async def _shutdown():
  await mq.close()

@app.post("/api/v1/events", status_code=202)
async def ingest_event(req: IngestEventRequest):
  occurred_at = parse_occurred_at(req.occurredAt)
  received_at = datetime.now(timezone.utc)
  event_id = str(uuid.uuid4())

  with db() as conn, conn.cursor() as cur:
    if req.idempotencyKey:
      cur.execute("SELECT id FROM events WHERE idempotency_key=%s", (req.idempotencyKey,))
      row = cur.fetchone()
      if row:
        return {"eventId": str(row[0]), "status": "ACCEPTED", "enqueuedJobs": 0}

    try:
      cur.execute(
          """
          INSERT INTO events(id, source, type, subject_kind, subject_id, payload, occurred_at, received_at, idempotency_key, status)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACCEPTED')
          """,
          (event_id, req.source, req.type, req.subject.kind, req.subject.id,
           json.dumps(req.payload, ensure_ascii=False), occurred_at, received_at, req.idempotencyKey)
      )
    except Exception as e:
      raise HTTPException(status_code=409, detail=f"Conflict: {e}")

  msg = {
    "eventId": event_id,
    "source": req.source,
    "type": req.type,
    "subject": {"kind": req.subject.kind, "id": req.subject.id},
    "payload": req.payload,
    "occurredAt": occurred_at.astimezone(timezone.utc).isoformat(),
    "receivedAt": received_at.astimezone(timezone.utc).isoformat(),
  }
  await mq.publish_event(msg)
  return {"eventId": event_id, "status": "ACCEPTED", "enqueuedJobs": 0}

@app.post("/api/v1/rules", status_code=201)
def create_rule(req: CreateRuleRequest):
  with db() as conn, conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO rules(name, enabled, match_source, match_type, created_at, updated_at)
        VALUES (%s,%s,%s,%s,now(),now())
            RETURNING id
        """,
        (req.name, req.enabled, req.match.source, req.match.type)
    )
    rule_id = str(cur.fetchone()[0])

    for a in req.actions:
      cur.execute(
          """
          INSERT INTO rule_actions(rule_id, kind, config, order_no)
          VALUES (%s,%s,%s,%s)
          """,
          (rule_id, a.kind, json.dumps(a.config, ensure_ascii=False), a.orderNo)
      )

  return {"ruleId": rule_id, "enabled": req.enabled}

@app.get("/api/v1/rules")
def list_rules(enabled: Optional[bool] = None):
  with db() as conn, conn.cursor() as cur:
    if enabled is None:
      cur.execute("SELECT id,name,enabled,match_source,match_type FROM rules ORDER BY created_at DESC")
    else:
      cur.execute("SELECT id,name,enabled,match_source,match_type FROM rules WHERE enabled=%s ORDER BY created_at DESC", (enabled,))
    rules = cur.fetchall()

    rule_ids = [r[0] for r in rules]
    actions_by_rule = {str(rid): [] for rid in rule_ids}
    if rule_ids:
      cur.execute(
          """
          SELECT rule_id, kind, config, order_no
          FROM rule_actions
          WHERE rule_id = ANY(%s)
          ORDER BY rule_id, order_no
          """,
          (rule_ids,)
      )
      for rule_id, kind, config, order_no in cur.fetchall():
        actions_by_rule[str(rule_id)].append({"kind": kind, "config": config, "orderNo": order_no})

  items = []
  for rid, name, en, ms, mt in rules:
    items.append({
      "ruleId": str(rid),
      "name": name,
      "enabled": en,
      "match": {"source": ms, "type": mt},
      "actions": actions_by_rule.get(str(rid), [])
    })
  return {"items": items}

@app.patch("/api/v1/rules/{rule_id}")
def update_rule(rule_id: str, req: UpdateRuleRequest):
  with db() as conn, conn.cursor() as cur:
    cur.execute("SELECT name, enabled, match_source, match_type FROM rules WHERE id=%s", (rule_id,))
    row = cur.fetchone()
    if not row:
      raise HTTPException(status_code=404, detail="Rule not found")

    name, enabled, ms, mt = row
    if req.name is not None: name = req.name
    if req.enabled is not None: enabled = req.enabled
    if req.match is not None:
      if req.match.source is not None: ms = req.match.source
      if req.match.type is not None: mt = req.match.type

    cur.execute(
        "UPDATE rules SET name=%s, enabled=%s, match_source=%s, match_type=%s, updated_at=now() WHERE id=%s",
        (name, enabled, ms, mt, rule_id)
    )

    if req.actions is not None:
      cur.execute("DELETE FROM rule_actions WHERE rule_id=%s", (rule_id,))
      for a in req.actions:
        cur.execute(
            "INSERT INTO rule_actions(rule_id, kind, config, order_no) VALUES (%s,%s,%s,%s)",
            (rule_id, a.kind, json.dumps(a.config, ensure_ascii=False), a.orderNo)
        )
  return {"ruleId": rule_id, "enabled": enabled}

@app.get("/api/v1/admin/circuit")
def list_circuit(state: Optional[str] = None):
  q = "SELECT key,state,failure_count,opened_at,last_failure_at,updated_at FROM webhook_circuit"
  params = ()
  if state:
    q += " WHERE state=%s"
    params = (state,)
  q += " ORDER BY updated_at DESC LIMIT 200"

  with db() as conn, conn.cursor() as cur:
    cur.execute(q, params)
    rows = cur.fetchall()

  def iso(x): return x.astimezone(timezone.utc).isoformat() if x else None
  return {"items": [
    {"key": k, "state": s, "failureCount": fc, "openedAt": iso(oa), "lastFailureAt": iso(lf), "updatedAt": iso(ua)}
    for (k, s, fc, oa, lf, ua) in rows
  ]}

@app.post("/api/v1/admin/circuit/{key}/reset")
def reset_circuit(key: str):
  with db() as conn, conn.cursor() as cur:
    cur.execute(
        """
        UPDATE webhook_circuit
        SET state='CLOSED', failure_count=0, opened_at=NULL, last_failure_at=NULL, updated_at=now()
        WHERE key=%s
        """,
        (key,)
    )
    if cur.rowcount == 0:
      raise HTTPException(status_code=404, detail="Circuit key not found")
  return {"key": key, "state": "CLOSED"}
