import asyncio
import websockets
import json
import os
<<<<<<< HEAD
import logging
import serial  # For serial communication with Marlin

# Set up logging configuration with DEBUG level
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
=======
>>>>>>> 3fed63f (ahhhh)

# Define Moonraker URI with token (adjust for your setup)
MOONRAKER_URI = f"ws://localhost:7125/websocket?token={os.getenv('MOONRAKER_TOKEN', 'your_token_here')}"

# Serial connection to Marlin (adjust to your setup)
SERIAL_PORT = '/dev/ttyS2'  # Or the appropriate serial port for your setup
BAUD_RATE = 115200  # Marlin's default baud rate

# Create a global serial connection object
ser = serial.Serial(SERIAL_PORT, BAUD_RATE)

async def send_to_moonraker(websocket, command):
    """Send a Marlin command to Moonraker as an API request."""
    if command == "M105":
        # Get temperature (equivalent to M105)
        message = {
            "jsonrpc": "2.0",
            "method": "printer.objects.temperature",
            "params": {},
            "id": 1
        }
    elif command.startswith("M104"):
        # Set extruder temperature (equivalent to M104)
        temp = int(command.split('S')[1])
        message = {
            "jsonrpc": "2.0",
            "method": "printer.objects.temperature.set_temperature",
            "params": {
                "tool0": temp
            },
            "id": 2
        }
    elif command.startswith("M106"):
        # Set fan speed (equivalent to M106)
        speed = int(command.split('S')[1])
        message = {
            "jsonrpc": "2.0",
            "method": "printer.objects.fan.set_speed",
            "params": {
                "fan0": speed
            },
            "id": 3
        }
    elif command == "G28":
        # Home all axes (equivalent to G28)
        message = {
            "jsonrpc": "2.0",
            "method": "printer.gcode.script",
            "params": {
                "script": ["G28"]
            },
            "id": 4
        }
    elif command.startswith("G1"):
        # Move command (equivalent to G1)
        gcode = command
        message = {
            "jsonrpc": "2.0",
            "method": "printer.gcode.script",
            "params": {
                "script": [gcode]
            },
            "id": 5
        }
    else:
        print(f"Unknown command: {command}")
        return None

    # Send the message to Moonraker
    await websocket.send(json.dumps(message))
<<<<<<< HEAD
    logging.debug(f"Sent command: {command} to Moonraker - Message: {json.dumps(message)}")

async def send_response_to_marlin(response):
    """Send the response back to Marlin through the serial connection."""
    if response:
        logging.info(f"Sending response to Marlin: {response}")
        # Write to the serial port (Marlin will process this)
        ser.write(f"{response}\n".encode('utf-8'))
    else:
        logging.error("No valid response to send back to Marlin.")

async def subscribe_to_updates(websocket):
    """Subscribe to extruder, heater_bed, and gcode_move updates from Moonraker."""
    # Subscription message for extruder, heater_bed, and gcode_move
    subscription_message = {
        "jsonrpc": "2.0",
        "method": "printer.objects.subscribe",
        "params": {
            "objects": {
                "extruder": None,
                "heater_bed": None,
                "gcode_move": None
            }
        },
        "id": 10
    }

    # Send the subscription request
    await websocket.send(json.dumps(subscription_message))
    logging.debug(f"Subscription message sent: {json.dumps(subscription_message)}")
=======
    print(f"Sent command: {command} to Moonraker")
>>>>>>> 3fed63f (ahhhh)

async def listen_for_updates(websocket):
    """Listen for updates from Moonraker WebSocket."""
    while True:
        try:
            response = await websocket.recv()  # Wait for a response
            data = json.loads(response)

            # Check for specific notification type like 'notify_status_update'
            if 'params' in data and 'notify_status_update' in data['params']:
                status_update = data['params']['notify_status_update']
                print(f"Received status update: {status_update}")

<<<<<<< HEAD
                # Extract extruder temperature info
                if "extruder" in status_update:
                    extruder = status_update["extruder"]
                    logging.info(f"Extruder Update: {extruder}")
                    # Send temperature info to Marlin
                    await send_response_to_marlin(f"Extruder Temp: {extruder['temperature']}")

                # Extract heater bed temperature info
                if "heater_bed" in status_update:
                    heater_bed = status_update["heater_bed"]
                    logging.info(f"Heater Bed Update: {heater_bed}")
                    # Send temperature info to Marlin
                    await send_response_to_marlin(f"Heater Bed Temp: {heater_bed['temperature']}")

                # Extract G-code move info
                if "gcode_move" in status_update:
                    gcode_move = status_update["gcode_move"]
                    logging.info(f"G-code Move Update: {gcode_move}")
=======
                # Process the status update (e.g., printing status, temperature, etc.)
                if status_update.get("status") == "idle":
                    print("Printer is idle.")
                elif status_update.get("status") == "printing":
                    print("Printer is printing a job.")
                else:
                    print(f"Printer status: {status_update.get('status')}")
>>>>>>> 3fed63f (ahhhh)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed: {e}")
            break  # Exit the loop if the WebSocket connection is closed

async def handle_communication():
    async with websockets.connect(MOONRAKER_URI) as websocket:
        # Example Marlin commands (can be extended)
        marlin_commands = ["M105", "M104 S200", "M106 S255", "G28", "G1 X100 Y100 Z0.2 F1500"]

        # Start listening for updates (this will keep running in the background)
        listen_task = asyncio.create_task(listen_for_updates(websocket))

        # Send Marlin commands to Moonraker and wait for the status updates
        for command in marlin_commands:
            await send_to_moonraker(websocket, command)
            await asyncio.sleep(1)  # Pause between commands for demo

        # Let the listening task keep running and handling updates
        await listen_task

# Main entry point to run the communication
async def main():
    """Main entry point for the asyncio event loop."""
    try:
        await handle_communication()
    except Exception as e:
<<<<<<< HEAD
        logging.error(f"An error occurred: {e}")
    finally:
        # Close the serial connection when done
        if ser.is_open:
            ser.close()
            logging.info("Serial connection closed.")
=======
        print(f"An error occurred: {e}")
>>>>>>> 3fed63f (ahhhh)

if __name__ == "__main__":
    asyncio.run(main())
