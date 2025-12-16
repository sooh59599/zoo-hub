# 🐾 Zoo Stack Toy Project
## 동물 로고 스택만 사용해서, 파이썬을 모르는 개발자가 LLM으로 만드는 토이 프로젝트

이 프로젝트는 **기능을 많이 만들기 위한 프로젝트가 아닙니다.**  
아래 **두 가지 제약**을 걸고 시작한 실험입니다.

---

## 🎯 프로젝트 제약

### 1️⃣ Zoo Stack 제약
> **공식 로고가 ‘동물’인 기술만 사용한다**

그래서 선택된 스택은 딱 이 네 가지입니다.

- 🐳 **Docker** — 실행 환경
- 🐍 **Python (FastAPI)** — API + Worker
- 🐘 **PostgreSQL** — 데이터 저장
- 🐰 **RabbitMQ** — 비동기 큐

다른 기술은 의도적으로 사용하지 않습니다.

---

### 2️⃣ 개발자 제약
> **나는 파이썬을 거의 모른다**

- 파이썬 문법에 익숙하지 않음
- 대신 LLM(ChatGPT)을 사용해서
  - 설계
  - 코드 작성
  - 오류 수정
  - 실행까지 전부 진행

즉, 이 프로젝트는  
👉 **“LLM을 활용해 낯선 언어로 끝까지 만들어보는 실험”** 입니다.

---

## ❓ 이 프로젝트는 뭐하는 건가요?

한 문장으로 설명하면:

> **“조건문(if)을 서버로 옮겨서 대신 실행해주는 프로그램”**

---

## 🧠 핵심 개념

### 이벤트(Event)
> “무슨 일이 일어났는지 알려주는 메시지”

예:
- 결제 실패
- 주문 완료

---

### 룰(Rule)
> **조건문(if)**

```text
IF 이런 이벤트가 오면
THEN 이런 행동을 해라
````

예:

* IF 결제 실패
* THEN 웹훅 호출

---

### 워커(Worker)

> 룰을 보고 실제로 행동하는 프로그램

---

## 🔁 전체 흐름 (아주 단순)

1. 이벤트를 보낸다
2. 서버는 이벤트를 저장만 한다
3. 워커가 룰(if)을 확인한다
4. 조건이 맞으면 행동을 실행한다

API는 **절대 직접 실행하지 않는다**는 게 포인트입니다.

---

## 🛠️ 사용 기술

* 🐍 Python + FastAPI
* 🐰 RabbitMQ
* 🐘 PostgreSQL
* 🐳 Docker / Docker Compose

---

# 🚀 실행 방법

## 0️⃣ 준비물

* Docker / Docker Compose
* Python 3.12+
* 터미널 2개

---

## 1️⃣ DB + RabbitMQ 실행

```bash
docker compose up -d
```

---

## 2️⃣ API 서버 실행

```bash
pip install -r api/requirements.txt
uvicorn api.main:app --reload --port 8000
```

---

## 3️⃣ 워커 실행 (다른 터미널)

```bash
pip install -r worker/requirements.txt
python worker/worker.py
```

---

# 🧪 1분 데모: 조건문 하나 만들어보기

## ① 룰 등록 (if 문 작성)

아래 룰의 의미:

> IF 결제 실패 이벤트가 오면
> THEN 웹훅을 호출한다

```bash
curl -X POST http://localhost:8000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{
    "name": "결제 실패 → 웹훅",
    "enabled": true,
    "match": {
      "source": "billing-service",
      "type": "PAYMENT_FAILED"
    },
    "actions": [
      {
        "kind": "WEBHOOK",
        "orderNo": 0,
        "config": {
          "url": "https://httpbin.org/post",
          "method": "POST",
          "body": {
            "eventId": "{{eventId}}",
            "message": "결제 실패 발생"
          }
        }
      }
    ]
  }'
```

---

## ② 이벤트 보내기

```bash
curl -X POST http://localhost:8000/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
    "source": "billing-service",
    "type": "PAYMENT_FAILED",
    "subject": {
      "kind": "order",
      "id": "order_123"
    },
    "payload": {
      "amount": 30000
    }
  }'
```

---

## ③ 결과 확인

워커 터미널에 아래 로그가 나오면 성공입니다.

```text
[WEBHOOK OK] status=200
```

---

## 📌 이 프로젝트의 의도

* 이건 서비스가 아님
* 이건 CRUD 연습이 아님
* 이건 **조건문(if)을 서버로 옮긴 실험**

---

## ✨ 이 프로젝트의 재미 포인트

* 동물 로고 스택만 쓰는 명확한 컨셉
* 파이썬을 몰라도 LLM으로 끝까지 완주
* 비동기 / 큐 / 워커 구조를 직접 체험

---

### 한 줄 요약

> **“동물 로고 기술만 써서,
> 파이썬 모르는 개발자가
> LLM으로 만든 조건문 서버”**
