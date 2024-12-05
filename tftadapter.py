import asyncio
import serial
import logging
import json
import websockets
import os

# Configuration
MOONRAKER_TOKEN = os.getenv("MOONRAKER_TOKEN", "")
SERIAL_PORT = "/dev/ttyS2"  # Replace with your serial port
BAUD_RATE = 115200  # Common baud rate for 3D printers

if MOONRAKER_TOKEN:
    MOONRAKER_WS_URL = f"ws://localhost:7125/websocket?token={MOONRAKER_TOKEN}"
else:
    MOONRAKER_WS_URL = "ws://localhost:7125/websocket"

# Logging setup
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG for detailed logs
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

async def send_gcode_and_receive_response(websocket, gcode, request_id):
    """Send a Marlin G-code command as a JSON-RPC request and wait for the response."""
    request = {
        "jsonrpc": "2.0",
        "method": "printer.gcode.script",
        "params": {"script": gcode},
        "id": request_id
    }
    try:
        # Send the G-code request
        await websocket.send(json.dumps(request))
        logging.debug(f"Sent G-code: {gcode} with ID: {request_id}")
    except Exception as e:
        logging.error(f"Error sending G-code: {e}")
        await reconnect_websocket()

async def listen_websocket_messages(websocket, request_counter):
    """Single routine to listen for both updates and G-code responses."""
    while True:
        try:
            # Wait for a message from the WebSocket
            message = await websocket.recv()
            logging.debug(f"Received WebSocket message: {message}")

            data = json.loads(message)

            # Handle responses to G-code commands (messages with an 'id' field)
            if "id" in data:
                if "result" in data:
                    logging.info(f"Response to request ID {data['id']}: {data['result']}")
                    # Print the response directly
                    print(f"Response to G-code with ID {data['id']}: {data['result']}")
                else:
                    logging.warning(f"Unexpected response for G-code with ID {data['id']}: {data}")

            # Handle status updates (messages with 'method' = 'notify_status_update')
            elif "method" in data and data["method"] == "notify_status_update":
                logging.info(f"Received status update: {data['params']}")
                # Print the status update
                print(f"Status Update: {data['params']}")

            else:
                logging.warning(f"Unhandled message: {data}")

        except websockets.exceptions.ConnectionClosed as e:
            logging.error(f"WebSocket connection closed unexpectedly: {e}")
            await reconnect_websocket()
            break
        except Exception as e:
            logging.error(f"Error processing WebSocket message: {e}")
            break

async def reconnect_websocket():
    """Reconnect to WebSocket if the connection is lost."""
    logging.info("Attempting to reconnect to WebSocket...")
    await asyncio.sleep(1)  # Wait a moment before reconnecting
    return await websockets.connect(MOONRAKER_WS_URL)

async def read_serial_and_forward(websocket, request_counter):
    """Read G-codes from the serial port and forward them to Moonraker."""
    try:
        with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1) as ser:
            logging.info(f"Listening on {SERIAL_PORT} at {BAUD_RATE} baud.")
            while True:
                # Read a line of G-code from the serial port
                if ser.in_waiting > 0:
                    line = ser.readline().decode('utf-8').strip()
                    if line:
                        logging.debug(f"Received from serial: {line}")
                        # Increment request counter and send G-code
                        request_id = next(request_counter)
                        await send_gcode_and_receive_response(websocket, line, request_id)
    except Exception as e:
        logging.error(f"Error with serial port: {e}")

async def moonraker_client():
    """Connect to Moonraker, read serial data, and handle WebSocket messages."""
    request_counter = iter(range(1, 10**6))  # Unique request ID generator
    try:
        logging.info(f"Connecting to Moonraker WebSocket: {MOONRAKER_WS_URL}")
        async with websockets.connect(MOONRAKER_WS_URL) as websocket:
            logging.info("Connected to Moonraker")

            # Send a subscription request for objects
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
                "id": next(request_counter)
            }
            logging.debug(f"Sending subscription request: {json.dumps(subscription_request)}")
            await websocket.send(json.dumps(subscription_request))
            logging.info("Subscription request sent. Waiting for messages...")

            # Handle incoming WebSocket messages and serial input in parallel
            await asyncio.gather(
                read_serial_and_forward(websocket, request_counter),
                listen_websocket_messages(websocket, request_counter)
            )
    except Exception as e:
        logging.error(f"Error in WebSocket connection: {e}")
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(moonraker_client())
