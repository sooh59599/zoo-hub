import os, json, asyncio
from datetime import datetime, timezone, timedelta

import psycopg
from dotenv import load_dotenv

from mq import MQClient
from rule_engine import match_rule, render_template
from webhook_client import call_webhook, WebhookCallError

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
MAX_ATTEMPTS_DEFAULT = int(os.getenv("MAX_ATTEMPTS_DEFAULT", "3"))
RETRY_BACKOFF_SECONDS = int(os.getenv("RETRY_BACKOFF_SECONDS", "5"))
RETRY_SCAN_INTERVAL_SECONDS = int(os.getenv("RETRY_SCAN_INTERVAL_SECONDS", "5"))

mq = MQClient()

def db():
  return psycopg.connect(DATABASE_URL)

async def main():
  await mq.connect()
  print("Worker started (Rule + Executor + RetryScanner)")

  await asyncio.gather(
      consume_events(),
      consume_jobs(),
      retry_scanner_loop()
  )

async def consume_events():
  await mq.events_q.consume(on_event)

async def consume_jobs():
  await mq.jobs_q.consume(on_job)

async def on_event(message):
  async with message.process(requeue=False):
    event = json.loads(message.body.decode())
    created = create_jobs_for_event(event)
    for job_id in created:
      await mq.publish_job(job_id)

def create_jobs_for_event(event: dict) -> list[str]:
  ctx = {
    "eventId": event["eventId"],
    "source": event["source"],
    "type": event["type"],
    "subject": event["subject"],
    "payload": event["payload"],
    "occurredAt": event.get("occurredAt"),
  }

  created: list[str] = []
  with db() as conn, conn.cursor() as cur:
    cur.execute("UPDATE events SET status='PROCESSING' WHERE id=%s", (event["eventId"],))

    cur.execute("SELECT id, enabled, match_source, match_type FROM rules WHERE enabled=true")
    rules = cur.fetchall()

    cur.execute("""
                SELECT id, rule_id, kind, config, order_no
                FROM rule_actions
                ORDER BY rule_id, order_no
                """)
    actions = cur.fetchall()
    actions_by_rule = {}
    for aid, rid, kind, config, order_no in actions:
      actions_by_rule.setdefault(str(rid), []).append((aid, kind, config, order_no))

    for rid, enabled, ms, mt in rules:
      rule = {"enabled": enabled, "match_source": ms, "match_type": mt}
      if not match_rule(rule, event):
        continue

      for aid, kind, config, order_no in actions_by_rule.get(str(rid), []):
        payload = render_template(config, ctx)
        cur.execute(
            """
            INSERT INTO jobs(event_id, rule_id, action_id, kind, status, attempts, max_attempts, payload, created_at, updated_at)
            VALUES (%s,%s,%s,%s,'QUEUED',0,%s,%s,now(),now())
                RETURNING id
            """,
            (event["eventId"], str(rid), str(aid), kind, MAX_ATTEMPTS_DEFAULT, json.dumps(payload, ensure_ascii=False))
        )
        created.append(str(cur.fetchone()[0]))

  return created

async def on_job(message):
  async with message.process(requeue=False):
    job_id = json.loads(message.body.decode())["jobId"]
    run_job(job_id)

def run_job(job_id: str):
  with db() as conn, conn.cursor() as cur:
    cur.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE SKIP LOCKED", (job_id,))
    job = cur.fetchone()
    if not job:
      return

    cols = [d.name for d in cur.description]
    job = dict(zip(cols, job))

    if job["status"] not in ("QUEUED", "FAILED"):
      return

    if job["next_run_at"] and job["next_run_at"] > datetime.now(timezone.utc):
      return

    cur.execute("UPDATE jobs SET status='PROCESSING', updated_at=now() WHERE id=%s", (job_id,))
    conn.commit()

  # 실행은 트랜잭션 밖에서
  try:
    result_obj = execute(job_id, job)
    record_success(job_id, job, result_obj)
    finalize_event(job["event_id"])
  except Exception as e:
    result = None
    if isinstance(e, WebhookCallError):
      result = {"kind": "WEBHOOK", "status": e.status_code, "response": e.response_text}
    fail_job(job_id, job, str(e), result)

def execute(job_id: str, job: dict) -> dict:
  payload = job["payload"]
  kind = job["kind"]

  if kind == "EMAIL":
    print(f"[EMAIL] to={payload.get('to')} template={payload.get('template')}")
    return {"kind": "EMAIL", "to": payload.get("to"), "template": payload.get("template")}

  if kind == "WEBHOOK":
    idem = f"{job['event_id']}:{job_id}:{job['attempts'] + 1}"
    status, resp = call_webhook(
        method=payload.get("method", "POST"),
        url=payload.get("url"),
        body=payload.get("body"),
        headers=payload.get("headers"),
        idempotency_key=idem
    )
    print(f"[WEBHOOK OK] status={status}")
    return {"kind": "WEBHOOK", "status": status, "response": resp}

  raise RuntimeError(f"Unknown kind: {kind}")

def record_success(job_id: str, job: dict, result_obj: dict):
  with db() as conn, conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO job_attempts(job_id, attempt_no, status, result, started_at, finished_at)
        VALUES (%s,%s,'SUCCEEDED',%s,now(),now())
        """,
        (job_id, job["attempts"] + 1, json.dumps(result_obj, ensure_ascii=False))
    )
    cur.execute("UPDATE jobs SET status='SUCCEEDED', updated_at=now() WHERE id=%s", (job_id,))
    conn.commit()

def fail_job(job_id: str, job: dict, error: str, result_obj: dict | None):
  next_attempt = job["attempts"] + 1
  is_dead = next_attempt >= job["max_attempts"]
  next_run = datetime.now(timezone.utc) + timedelta(seconds=RETRY_BACKOFF_SECONDS)

  with db() as conn, conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO job_attempts(job_id, attempt_no, status, error, result, started_at, finished_at)
        VALUES (%s,%s,'FAILED',%s,%s,now(),now())
        """,
        (job_id, next_attempt, error, json.dumps(result_obj or {}, ensure_ascii=False))
    )
    cur.execute(
        """
        UPDATE jobs
        SET status=%s, attempts=%s, last_error=%s, next_run_at=%s, updated_at=now()
        WHERE id=%s
        """,
        ("DEAD" if is_dead else "FAILED", next_attempt, error, None if is_dead else next_run, job_id)
    )
    conn.commit()

  print(f"[JOB FAILED] job_id={job_id} attempts={next_attempt}/{job['max_attempts']} dead={is_dead} err={error}")

def finalize_event(event_id: str):
  with db() as conn, conn.cursor() as cur:
    cur.execute("""
                UPDATE events e
                SET status = CASE
                                 WHEN EXISTS (SELECT 1 FROM jobs j WHERE j.event_id=e.id AND j.status='DEAD') THEN 'FAILED'
                                 WHEN EXISTS (SELECT 1 FROM jobs j WHERE j.event_id=e.id AND j.status IN ('QUEUED','PROCESSING','FAILED')) THEN e.status
                                 ELSE 'DONE'
                    END
                WHERE e.id=%s
                """, (event_id,))
    conn.commit()

async def retry_scanner_loop():
  while True:
    await asyncio.sleep(RETRY_SCAN_INTERVAL_SECONDS)
    try:
      await scan_and_enqueue()
    except Exception as e:
      print("retry_scanner error:", e)

async def scan_and_enqueue():
  with db() as conn, conn.cursor() as cur:
    cur.execute("""
                SELECT id
                FROM jobs
                WHERE status='FAILED'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= now()
                ORDER BY next_run_at ASC
                    LIMIT 50
                """)
    rows = [str(r[0]) for r in cur.fetchall()]
    if not rows:
      return
    # 간단 중복 방지: next_run_at 미래로 밀기
    cur.executemany(
        "UPDATE jobs SET next_run_at = now() + interval '60 seconds', updated_at=now() WHERE id=%s AND status='FAILED'",
        [(r,) for r in rows]
    )
    conn.commit()

  for job_id in rows:
    await mq.publish_job(job_id)
  print(f"[RETRY SCAN] enqueued={len(rows)}")

if __name__ == "__main__":
  asyncio.run(main())
