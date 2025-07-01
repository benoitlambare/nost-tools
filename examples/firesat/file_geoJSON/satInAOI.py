import asyncio
import json
from dotenv import dotenv_values
import aio_pika
from transform_in_geoJSON import convert_location_to_geojson, append_feature_to_file

detected_by = None
detected_time = None


async def handle_message(message: aio_pika.IncomingMessage):
    global detected_by, detected_time

    async with message.process():
        topic = message.routing_key
        payload = json.loads(message.body.decode())

        if topic == "greenfield.constellation.detected":
            detected_by = payload.get("detected_by")
            detected_time = payload.get("detected")
            # print(f"✅ Detected fire by NORAD ID {detected_by} at {detected_time}")
            # print(json.dumps(payload, indent=2))

        elif (
            topic == "greenfield.constellation.location"
            and detected_by is not None
            and detected_time is not None
        ):
            norad_id = payload.get("noradId")
            time = payload.get("time")

            if detected_by == norad_id and detected_time == time:
                print("📍 Matching location found:")
                print(json.dumps(payload, indent=2))

                # Convertir et stocker le feature
                feature = convert_location_to_geojson(payload)
                append_feature_to_file(feature)

                # Reset after match
                detected_by = None
                detected_time = None


async def setup_broker_connection():
    credentials = dotenv_values(".env")
    HOST = credentials["HOST"]
    PORT = 5672  # Default RabbitMQ port
    USERNAME = credentials["USERNAME"]
    PASSWORD = credentials["PASSWORD"]
    USE_TLS = credentials.get("USE_TLS", "false").lower() == "true"

    protocol = "amqps" if USE_TLS else "amqp"
    url = f"{protocol}://{USERNAME}:{PASSWORD}@{HOST}:{PORT}/"
    print(f"🔌 Connecting to broker at {url}")

    try:
        connection = await aio_pika.connect_robust(url)

        async with connection:
            channel = await connection.channel()

            exchange = await channel.declare_exchange(
                "greenfield",
                aio_pika.ExchangeType.TOPIC,
                durable=True,
                auto_delete=True,
            )

            queue = await channel.declare_queue("", durable=False, auto_delete=True)

            # 🔁 Lier plusieurs topics à la même queue
            await queue.bind(exchange, routing_key="greenfield.constellation.detected")
            await queue.bind(exchange, routing_key="greenfield.constellation.location")

            await queue.consume(handle_message, no_ack=False)

            print("📡 Waiting for messages...")
            await asyncio.Future()

    except Exception as e:
        print(f"❌ Failed to connect or consume messages: {e}")


if __name__ == "__main__":
    asyncio.run(setup_broker_connection())
