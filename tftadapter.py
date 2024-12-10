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
def read_gcodes_from_serial(serial_conn, gcode_queue, loop):
    while True:
        try:
            line = serial_conn.readline().decode("utf-8").strip()
            if line:
                print(f"Received G-code: {line}")
                # Schedule the queue.put coroutine in the event loop
                asyncio.run_coroutine_threadsafe(gcode_queue.put(line), loop)
        except Exception as e:
            print(f"Error reading serial: {e}")
            break

# Function to handle Moonraker WebSocket communication
async def moonraker_client(gcode_queue):
    async with websockets.connect(MOONRAKER_URL) as websocket:
        print("Connected to Moonraker WebSocket")

        # Continuously handle G-code and subscription responses
        while True:
            try:
                # Check if there are G-codes in the queue
                if not gcode_queue.empty():
                    gcode = gcode_queue.get()

                    # Send the G-code to Moonraker
                    gcode_request = {
                        "jsonrpc": "2.0",
                        "method": "printer.gcode.script",
                        "params": {"script": gcode},
                        "id": 1
                    }
                    await websocket.send(json.dumps(gcode_request))
                    print(f"Sent G-code to Moonraker: {gcode}")

                # Process incoming WebSocket messages
                message = await asyncio.wait_for(websocket.recv(), timeout=0.1)
                data = json.loads(message)

                # Print Moonraker responses
                if "id" in data:
                    print(f"Moonraker Response: {json.dumps(data)}")
                elif "method" in data:
                    print(f"Moonraker Update: {json.dumps(data)}")

            except asyncio.TimeoutError:
                # No message received, continue checking
                pass
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
            args=(serial_conn, gcode_queue, loop),
            daemon=True
        )
        serial_thread.start()

        # Run the Moonraker WebSocket client
        asyncio.run(moonraker_client(gcode_queue))
    except Exception as e:
        print(f"Error: {e}")

# Entry point
if __name__ == "__main__":
    main()
