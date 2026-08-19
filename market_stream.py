import asyncio
import json
from datetime import datetime

import websockets


WS_URL = "wss://ws-feed.exchange.coinbase.com"

PRODUCTS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "XRP-USD",
    "DOGE-USD",
]

latest_data = {}


async def websocket_listener():
    subscribe_message = {
        "type": "subscribe",
        "product_ids": PRODUCTS,
        "channels": ["ticker"],
    }

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:

                await websocket.send(json.dumps(subscribe_message))
                print("Connected to Coinbase WebSocket.\n")

                async for message in websocket:
                    data = json.loads(message)

                    if data.get("type") != "ticker":
                        continue

                    symbol = data["product_id"]

                    price = float(data["price"])
                    bid = float(data["best_bid"])
                    ask = float(data["best_ask"])
                    spread = ask - bid

                    latest_data[symbol] = {
                        "price": price,
                        "bid": bid,
                        "ask": ask,
                        "spread": spread,
                    }

        except Exception as error:
            print(f"Connection error: {error}")
            print("Reconnecting in 3 seconds...\n")
            await asyncio.sleep(3)


async def display_loop():
    while True:
        await asyncio.sleep(1)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print(f"\n{now}")
        print("-" * 75)

        for symbol in PRODUCTS:
            data = latest_data.get(symbol)

            if data is None:
                print(f"{symbol:<8} | Waiting for data...")
                continue

            print(
                f"{symbol:<8} | "
                f"Price ${data['price']:>12,.6f} | "
                f"Bid ${data['bid']:>12,.6f} | "
                f"Ask ${data['ask']:>12,.6f} | "
                f"Spread {data['spread']:.6f}"
            )


async def main():
    print("LIVE 15-MIN market data collector")
    print("--------------------------------")
    print("Monitoring:", ", ".join(PRODUCTS))
    print("Press Ctrl+C to stop.\n")

    await asyncio.gather(
        websocket_listener(),
        display_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMarket data collector stopped.")