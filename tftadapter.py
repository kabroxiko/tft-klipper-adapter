import asyncio
import websockets
import json
import serial
import threading
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the logging level to DEBUG
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.FileHandler("tft_adapter.log", mode='a')  # Log to a file
    ]
)

# Moonraker WebSocket URL and serial device configuration
MOONRAKER_URL = "ws://localhost/websocket"
SERIAL_PORT = "/dev/ttyS2"
BAUD_RATE = 115200  # Adjust based on your device's configuration

# Global cache for latest values
latest_values = {
    "extruder": {"temperature": 0.0, "target": 0.0},
    "heater_bed": {"temperature": 0.0, "target": 0.0},
}

def convert_to_marlin(response):
    """
    Convert a Moonraker response to a Marlin-compatible format.
    """
    global latest_values

    try:
        # Handle G-code execution responses
        if "id" in response and response["id"] == 2:
            if "result" in response and response["result"] == "ok":
                return "ok"
            elif "error" in response:
                return f"Error: {response['error']}"

        # Filter out "notify_proc_stat_update"
        if "method" in response and response["method"] == "notify_proc_stat_update":
            logging.debug("Filtered out notify_proc_stat_update")
            return None

        # Handle status update notifications
        if "method" in response and response["method"] == "notify_status_update":
            params = response.get("params", [])
            if isinstance(params, list) and len(params) > 0:
                updates = params[0]  # The first element contains the updates

                # Update cached values with new data
                for key, values in updates.items():
                    if key in latest_values:
                        latest_values[key].update({k: v for k, v in values.items() if v is not None})

                # Format output using latest values
                extruder = latest_values["extruder"]
                heater_bed = latest_values["heater_bed"]

                ETemp = extruder["temperature"]
                ETarget = extruder["target"]
                BTemp = heater_bed["temperature"]
                BTarget = heater_bed["target"]

                return f"ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0"

        # Handle unexpected or unknown structures
        return f"Unknown response: {json.dumps(response)}"

    except Exception as e:
        logging.error(f"Error in response conversion: {e}")
        return f"Error in response conversion: {str(e)}"

def read_gcodes_from_serial(serial_conn, gcode_queue):
    while True:
        try:
            line = serial_conn.readline().decode("utf-8").strip()
            if line:
                logging.debug(f"Received G-code from serial: {line}")
                # Add G-code to the queue (using asyncio thread-safe method)
                gcode_queue.put_nowait(line)
                logging.info("G-code added to queue")
        except Exception as e:
            logging.error(f"Error reading serial: {e}")
            break

async def moonraker_client(gcode_queue, serial_conn):
    async with websockets.connect(MOONRAKER_URL) as websocket:
        logging.info("Connected to Moonraker WebSocket")

        # Subscription request
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
            "id": 1
        }
        await websocket.send(json.dumps(subscription_request))
        logging.info("Subscription request sent for extruder, heater_bed, and gcode_move")

        while True:
            try:
                # Check queue size
                queue_size = gcode_queue.qsize()
                logging.debug(f"Checking queue size: {queue_size}")

                if queue_size > 0:
                    gcode = await gcode_queue.get()
                    logging.debug(f"Processing G-code: {gcode}")

                    # Send G-code to Moonraker
                    gcode_request = {
                        "jsonrpc": "2.0",
                        "method": "printer.gcode.script",
                        "params": {"script": gcode},
                        "id": 2
                    }
                    await websocket.send(json.dumps(gcode_request))
                    logging.debug(f"Sent G-code to Moonraker: {gcode}")

                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                    data = json.loads(message)
                    logging.debug(f"Received from Moonraker: {json.dumps(data)}")

                    # Handle the response and send to TFT
                    marlin_response = convert_to_marlin(data)
                    if marlin_response:
                        serial_conn.write(f"{marlin_response}\n".encode())
                        logging.info(f"Sent response back to TFT: {marlin_response}")

                except asyncio.TimeoutError:
                    pass

                await asyncio.sleep(0.1)

            except websockets.ConnectionClosed:
                logging.warning("WebSocket connection closed")
                break
            except Exception as e:
                logging.error(f"Error: {e}")
                break

def main():
    try:
        # Open the serial connection
        serial_conn = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        logging.info(f"Connected to serial device at {SERIAL_PORT} with baud rate {BAUD_RATE}")

        # Create a thread-safe asyncio queue
        gcode_queue = asyncio.Queue()

        # Start a thread to read from the serial port
        serial_thread = threading.Thread(
            target=read_gcodes_from_serial,
            args=(serial_conn, gcode_queue),
            daemon=True
        )
        serial_thread.start()

        # Run the Moonraker WebSocket client
        asyncio.run(moonraker_client(gcode_queue, serial_conn))
    except Exception as e:
        logging.critical(f"Critical error: {e}")

if __name__ == "__main__":
    main()
