import os, json, aio_pika
from dotenv import load_dotenv

load_dotenv()

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "")
EVENTS_EXCHANGE = os.getenv("EVENTS_EXCHANGE", "zoo.events")
EVENTS_ROUTING_KEY = os.getenv("EVENTS_ROUTING_KEY", "zoo.event.ingested")
EVENTS_QUEUE = os.getenv("EVENTS_QUEUE", "zoo.events.q")

JOBS_EXCHANGE = os.getenv("JOBS_EXCHANGE", "zoo.jobs")
JOBS_ROUTING_KEY = os.getenv("JOBS_ROUTING_KEY", "zoo.job.execute")
JOBS_QUEUE = os.getenv("JOBS_QUEUE", "zoo.jobs.q")

class MQClient:
  async def connect(self):
    self.conn = await aio_pika.connect_robust(RABBITMQ_URL)
    self.channel = await self.conn.channel()
    await self.channel.set_qos(prefetch_count=50)

    self.events_exchange = await self.channel.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    self.jobs_exchange = await self.channel.declare_exchange(JOBS_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)

    self.events_q = await self.channel.declare_queue(EVENTS_QUEUE, durable=True)
    await self.events_q.bind(self.events_exchange, routing_key=EVENTS_ROUTING_KEY)

    self.jobs_q = await self.channel.declare_queue(JOBS_QUEUE, durable=True)
    await self.jobs_q.bind(self.jobs_exchange, routing_key=JOBS_ROUTING_KEY)

  async def publish_job(self, job_id: str):
    msg = aio_pika.Message(
        body=json.dumps({"jobId": job_id}).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )
    await self.jobs_exchange.publish(msg, routing_key=JOBS_ROUTING_KEY)

  async def close(self):
    await self.conn.close()
