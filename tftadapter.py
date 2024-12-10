import asyncio
import websockets
import json

# WebSocket URL for Moonraker
MOONRAKER_URL = "ws://localhost/websocket"

async def moonraker_client():
    async with websockets.connect(MOONRAKER_URL) as websocket:
        print("Connected to Moonraker WebSocket")

        # Step 1: Send a subscription request to monitor printer status
        subscription_request = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": {
                    "extruder": None,
                    "heater_bed": None,
                    "gcode_move": None
                }
            },
            "id": 1  # Unique ID for subscription
        }
        await websocket.send(json.dumps(subscription_request))
        print("Subscription request sent: status")

        # Step 2: Send a query request to fetch printer info
        query_request = {
            "jsonrpc": "2.0",
            "method": "printer.info",
            "id": 2  # Unique ID for query
        }
        await websocket.send(json.dumps(query_request))
        print("Query sent: printer.info")

        # Step 3: Continuously listen for responses and updates
        while True:
            try:
                message = await websocket.recv()
                data = json.loads(message)

                # Handle subscription updates
                if "method" in data and data["method"] == "notify_status_update":
                    print(f"Subscription Update: {json.dumps(data)}")

                # Handle query responses
                elif "id" in data:
                    if data["id"] == 1:
                        print(f"Subscription Response: {json.dumps(data)}")
                    elif data["id"] == 2:
                        print(f"Query Response: {json.dumps(data)}")

            except websockets.ConnectionClosed:
                print("WebSocket connection closed")
                break
            except Exception as e:
                print(f"Error: {e}")
                break

# Run the Moonraker client
asyncio.run(moonraker_client())
