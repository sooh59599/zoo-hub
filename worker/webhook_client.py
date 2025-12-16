import os, time, json, hmac, hashlib
from urllib.parse import urlparse
import httpx
import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "3"))
MAX_RETRIES = int(os.getenv("WEBHOOK_MAX_RETRIES", "3"))
BACKOFF_BASE = float(os.getenv("WEBHOOK_RETRY_BACKOFF_BASE", "0.5"))

SIGNING_SECRET = os.getenv("WEBHOOK_SIGNING_SECRET", "")
SIG_HEADER = os.getenv("WEBHOOK_SIGNATURE_HEADER", "X-Zoo-Signature")
TS_HEADER = os.getenv("WEBHOOK_TIMESTAMP_HEADER", "X-Zoo-Timestamp")
SIG_ALG = os.getenv("WEBHOOK_SIGNATURE_ALG", "sha256").lower()

CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "3"))
CB_OPEN_SECONDS = int(os.getenv("CB_OPEN_SECONDS", "30"))

class WebhookCallError(RuntimeError):
  def __init__(self, message: str, status_code=None, response_text=None):
    super().__init__(message)
    self.status_code = status_code
    self.response_text = response_text

def _db():
  return psycopg.connect(DATABASE_URL)

def _cb_key_from_url(url: str) -> str:
  return urlparse(url).netloc or url

def _sign_payload(timestamp: str, body: dict | None) -> str:
  if not SIGNING_SECRET:
    return ""
  if SIG_ALG != "sha256":
    raise RuntimeError(f"Unsupported signature alg: {SIG_ALG}")
  canonical = "" if body is None else json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
  msg = f"{timestamp}.{canonical}".encode("utf-8")
  return hmac.new(SIGNING_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()

def call_webhook(method: str, url: str, body: dict | None, headers: dict | None = None, idempotency_key: str | None = None):
  key = _cb_key_from_url(url)
  headers = headers or {}
  headers.setdefault("Content-Type", "application/json")
  if idempotency_key:
    headers.setdefault("Idempotency-Key", idempotency_key)

  ts = str(int(time.time()))
  sig = _sign_payload(ts, body)
  if sig:
    headers.setdefault(TS_HEADER, ts)
    headers.setdefault(SIG_HEADER, f"{SIG_ALG}={sig}")

  with _db() as conn, conn.cursor() as cur:
    cur.execute(
        """
        INSERT INTO webhook_circuit(key, state, failure_count, updated_at)
        VALUES (%s,'CLOSED',0,now())
            ON CONFLICT (key) DO NOTHING
        """,
        (key,)
    )
    cur.execute("SELECT state, opened_at FROM webhook_circuit WHERE key=%s", (key,))
    state, opened_at = cur.fetchone()

    if state == "OPEN":
      # OPEN 시간 지난 경우 HALF_OPEN으로 넘어가는 단순 모델은 생략(토이)
      # 운영 포인트: 일정 시간 차단
      raise RuntimeError(f"CIRCUIT_OPEN for {key}")

  last_err = None
  for attempt in range(1, MAX_RETRIES + 1):
    try:
      with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.request(method.upper(), url, json=body, headers=headers)
        if 200 <= resp.status_code < 300:
          _on_success(key)
          return resp.status_code, resp.text
        raise WebhookCallError(f"HTTP {resp.status_code}", resp.status_code, resp.text)
    except Exception as e:
      last_err = e
      if attempt < MAX_RETRIES:
        time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
      else:
        _on_failure(key)
        if isinstance(e, WebhookCallError):
          raise e
        raise WebhookCallError(str(e))

def _on_success(key: str):
  with _db() as conn, conn.cursor() as cur:
    cur.execute(
        "UPDATE webhook_circuit SET state='CLOSED', failure_count=0, opened_at=NULL, last_failure_at=NULL, updated_at=now() WHERE key=%s",
        (key,)
    )

def _on_failure(key: str):
  with _db() as conn, conn.cursor() as cur:
    cur.execute("SELECT failure_count FROM webhook_circuit WHERE key=%s", (key,))
    fc = cur.fetchone()[0] + 1
    if fc >= CB_FAILURE_THRESHOLD:
      cur.execute(
          "UPDATE webhook_circuit SET state='OPEN', failure_count=%s, opened_at=now(), last_failure_at=now(), updated_at=now() WHERE key=%s",
          (fc, key)
      )
    else:
      cur.execute(
          "UPDATE webhook_circuit SET failure_count=%s, last_failure_at=now(), updated_at=now() WHERE key=%s",
          (fc, key)
      )
