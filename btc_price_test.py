import time
import requests
from datetime import datetime

URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

print("BTC price collector started.")
print("Press Ctrl+C to stop.\n")

try:
    while True:
        try:
            response = requests.get(URL, timeout=10)
            response.raise_for_status()

            data = response.json()

            price = float(data["price"])
            bid = float(data["bid"])
            ask = float(data["ask"])

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print(
                f"{now} | "
                f"BTC: ${price:,.2f} | "
                f"Bid: ${bid:,.2f} | "
                f"Ask: ${ask:,.2f}"
            )

        except requests.RequestException as e:
            print("Network error:", e)

        time.sleep(5)

except KeyboardInterrupt:
    print("\nBTC price collector stopped.")