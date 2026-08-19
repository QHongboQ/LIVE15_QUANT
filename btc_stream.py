import asyncio
import json
from datetime import datetime

import websockets


WS_URL = "wss://ws-feed.exchange.coinbase.com"


async def stream_btc():
    subscribe_message = {
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channels": ["ticker"]
    }

    print("BTC WebSocket collector started.")
    print("Press Ctrl+C to stop.\n")

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20
            ) as websocket:

                await websocket.send(json.dumps(subscribe_message))

                print("Connected to Coinbase WebSocket.\n")

                async for message in websocket:
                    data = json.loads(message)

                    if data.get("type") == "ticker":
                        price = float(data["price"])

                        best_bid = data.get("best_bid")
                        best_ask = data.get("best_ask")

                        now = datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S.%f"
                        )[:-3]

                        output = (
                            f"{now} | "
                            f"BTC: ${price:,.2f}"
                        )

                        if best_bid and best_ask:
                            output += (
                                f" | Bid: ${float(best_bid):,.2f}"
                                f" | Ask: ${float(best_ask):,.2f}"
                            )

                        print(output)

        except Exception as e:
            print(f"\nConnection error: {e}")
            print("Reconnecting in 3 seconds...\n")
            await asyncio.sleep(3)


async def main():
    try:
        await stream_btc()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBTC WebSocket collector stopped.")