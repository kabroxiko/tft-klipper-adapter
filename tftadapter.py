import asyncio
import websockets
import json
import os
import logging

# Set up logging configuration with DEBUG level
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Define Moonraker URI with token (adjust for your setup)
MOONRAKER_URI = f"ws://localhost:7125/websocket?token={os.getenv('MOONRAKER_TOKEN', 'your_token_here')}"

async def send_to_moonraker(websocket, command):
    """Send a Marlin command to Moonraker as an API request."""
    if command == "M105":
        # Use printer.objects.query to get the status of the extruder and heater bed
        message = {
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": ["extruder", "heater_bed"]
            },
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
        logging.error(f"Unknown command: {command}")
        return None

    # Send the message to Moonraker
    await websocket.send(json.dumps(message))
    logging.debug(f"Sent command: {command} to Moonraker - Message: {json.dumps(message)}")

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

async def listen_for_updates(websocket):
    """Listen for updates from Moonraker WebSocket."""
    while True:
        try:
            response = await websocket.recv()  # Wait for a response
            data = json.loads(response)
            logging.debug(f"Received raw response: {response}")

            # Check for specific notification type like 'notify_status_update'
            if 'params' in data and 'notify_status_update' in data['params']:
                status_update = data['params']['notify_status_update']
                logging.debug(f"Processed status update: {status_update}")

                # Extract extruder temperature info
                if "extruder" in status_update:
                    extruder = status_update["extruder"]
                    logging.info(f"Extruder Update: {extruder}")

                # Extract heater bed temperature info
                if "heater_bed" in status_update:
                    heater_bed = status_update["heater_bed"]
                    logging.info(f"Heater Bed Update: {heater_bed}")

                # Extract G-code move info
                if "gcode_move" in status_update:
                    gcode_move = status_update["gcode_move"]
                    logging.info(f"G-code Move Update: {gcode_move}")

        except websockets.exceptions.ConnectionClosed as e:
            logging.error(f"Connection closed: {e}")
            break  # Exit the loop if the WebSocket connection is closed

async def handle_communication():
    async with websockets.connect(MOONRAKER_URI) as websocket:
        # Subscribe to updates (extruder, heater_bed, and gcode_move)
        await subscribe_to_updates(websocket)

        # Example Marlin commands (can be extended)
        marlin_commands = ["M105", "M104 S200", "M106 S255", "G28", "G1 X100 Y100 Z0.2 F1500"]

        # Send all commands in parallel using asyncio.gather
        tasks = [send_to_moonraker(websocket, command) for command in marlin_commands]
        await asyncio.gather(*tasks)  # Execute all commands concurrently

        # Start listening for updates (this will keep running in the background)
        listen_task = asyncio.create_task(listen_for_updates(websocket))

        # Wait for listening task to complete
        await listen_task

# Main entry point to run the communication
async def main():
    """Main entry point for the asyncio event loop."""
    try:
        await handle_communication()
    except Exception as e:
        logging.error(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
