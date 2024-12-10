import asyncio
import websockets
import json
import serial
import threading

# Moonraker WebSocket URL and serial device configuration
MOONRAKER_URL = "ws://localhost/websocket"
SERIAL_PORT = "/dev/ttyS2"
BAUD_RATE = 115200  # Adjust based on your device's configuration

# Function to read G-codes from the serial device
def read_gcodes_from_serial(serial_conn, gcode_queue):
    while True:
        try:
            line = serial_conn.readline().decode("utf-8").strip()
            if line:
                print(f"Received G-code from serial: {line}")
                # Add G-code to the queue (using asyncio thread-safe method)
                gcode_queue.put_nowait(line)  # Directly add to the queue without asyncio.run_coroutine_threadsafe
                print("G-code added to queue")
            else:
                print("No data received from serial.")
        except Exception as e:
            print(f"Error reading serial: {e}")
            break

# Function to handle Moonraker WebSocket communication
async def moonraker_client(gcode_queue, serial_conn):
    async with websockets.connect(MOONRAKER_URL) as websocket:
        print("Connected to Moonraker WebSocket")

        # Step 1: Subscribe to specific objects (extruder, heater_bed, gcode_move)
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
        print("Subscription request sent for extruder, heater_bed, and gcode_move")

        # Step 2: Continuously handle G-code and subscription responses
        while True:
            try:
                # Check if the queue is not empty
                queue_size = gcode_queue.qsize()  # Check the queue size
                print(f"Checking queue size: {queue_size}")  # Debugging the queue size

                if queue_size > 0:
                    # If the queue is not empty, get the G-code from it
                    gcode = await gcode_queue.get()
                    print(f"Processing G-code: {gcode}")  # Debugging: should print G-code

                    # Send the G-code to Moonraker
                    gcode_request = {
                        "jsonrpc": "2.0",
                        "method": "printer.gcode.script",
                        "params": {"script": gcode},
                        "id": 2  # Unique ID for G-code commands
                    }
                    await websocket.send(json.dumps(gcode_request))
                    print(f"Sent G-code to Moonraker: {gcode}")
                else:
                    print("Queue is empty, waiting for G-code...")

                # Process WebSocket messages
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                    data = json.loads(message)

                    # Handle subscription updates
                    if "method" in data and data["method"] == "notify_status_update":
                        print(f"Subscription Update: {data}")

                    # Handle G-code responses
                    elif "id" in data and data["id"] == 2:
                        print(f"G-code Response: {data}")

                        # Convert response to Marlin format
                        if "result" in data and data["result"] == "ok":
                            response_message = "ok\n"  # Marlin expects 'ok' on success
                        else:
                            # If an error occurred, return the error message
                            error_message = data.get("error", {}).get("message", "Unknown error")
                            response_message = f"Error: {error_message}\n"

                        # Send the response back to Artillery TFT in Marlin format
                        serial_conn.write(response_message.encode())  # Send response to TFT
                        print(f"Sent Marlin response back to TFT: {response_message}")

                except asyncio.TimeoutError:
                    # No message received, continue
                    pass

                await asyncio.sleep(0.1)  # Sleep to prevent blocking the event loop

            except websockets.ConnectionClosed:
                print("WebSocket connection closed")
                break
            except Exception as e:
                print(f"Error: {e}")
                break

# Main function to integrate serial and WebSocket
def main():
    try:
        # Open the serial connection
        serial_conn = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        print(f"Connected to serial device at {SERIAL_PORT} with baud rate {BAUD_RATE}")

        # Create a thread-safe asyncio queue
        gcode_queue = asyncio.Queue()

        # Get the current event loop
        loop = asyncio.get_event_loop()

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
        print(f"Error: {e}")

# Entry point
if __name__ == "__main__":
    main()
