import asyncio
import json
from dotenv import dotenv_values
import aio_pika


async def handle_message(message: aio_pika.IncomingMessage):
    async with message.process():
        topic = message.routing_key
        payload = json.loads(message.body.decode())
        print(f"Received message on topic '{topic}': {payload}")


async def setup_broker_connection():
    credentials = dotenv_values(".env")
    HOST = credentials["HOST"]
    PORT = 5672  # Default RabbitMQ port
    USERNAME = credentials["USERNAME"]
    PASSWORD = credentials["PASSWORD"]
    USE_TLS = credentials.get("USE_TLS", "false").lower() == "true"

    protocol = "amqps" if USE_TLS else "amqp"
    url = f"{protocol}://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/"
    print(f"Connecting to broker at {url}")

    try:
        # Connexion au broker AMQP (RabbitMQ)
        connection = await aio_pika.connect_robust(url)

        async with connection:
            channel = await connection.channel()

            # Déclare l'exchange topic
            exchange = await channel.declare_exchange(
                "greenfield",
                aio_pika.ExchangeType.TOPIC,
                durable=True,
                auto_delete=True,
            )

            # Déclare une queue avec un nom temporaire (ou fixe)
            queue = await channel.declare_queue("", durable=False, auto_delete=True)

            # Lie la queue à l'exchange avec le routing key pattern
            await queue.bind(exchange, routing_key="greenfield.constellation.detected")

            # Consomme les messages
            await queue.consume(handle_message, no_ack=False)

            print("Waiting for messages... ")
            # Garde la connexion ouverte
            await asyncio.Future()

    except Exception as e:
        print(f"Failed to connect or consume messages: {e}")


if __name__ == "__main__":
    asyncio.run(setup_broker_connection())
