import asyncio
import websockets
import json
import logging
import serial
import os
import argparse

# Global queue for communication between tasks
message_queue = asyncio.Queue()

# Function to parse command-line arguments
def parse_args():
    parser = argparse.ArgumentParser(description="Control a 3D printer using Moonraker API and Marlin commands.")

    # Add argument for Moonraker WebSocket URI
    parser.add_argument('-m', '--moonraker-uri', type=str, default="ws://localhost:7125/websocket",
                        help='URI for the Moonraker WebSocket server (default: ws://localhost:7125/websocket)')

    # Add argument for Moonraker token (if applicable)
    parser.add_argument('-t', '--moonraker-token', type=str, default=os.getenv('MOONRAKER_TOKEN', ''),
                        help='Authentication token for Moonraker (default: environment variable "MOONRAKER_TOKEN")')

    # Add argument for serial port for Marlin communication
    parser.add_argument('-p', '--serial-port', type=str, default='/dev/ttyS2',
                        help='Serial port for Marlin communication (default: /dev/ttyS2)')

    # Add argument for baud rate (optional)
    parser.add_argument('-b', '--baud-rate', type=int, default=115200,
                        help='Baud rate for Marlin communication (default: 115200)')

    # Add argument for log file
    parser.add_argument('-l', '--log-file', type=str, default=None,
                        help='File to log output (default: None, logs only to console)')

    # Add argument for verbose mode (debug logs)
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Enable verbose mode (debug logs)')

    # Parse arguments
    return parser.parse_args()

# Set up logging configuration with DEBUG level
def setup_logging(log_file=None, verbose=False):
    """Sets up logging to a file (if specified) and handles file size check."""
    logger = logging.getLogger()

    # Set log level based on verbose argument
    if verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # If log file is provided, check its size and purge if needed
    if log_file:
        # Check if the file exists and its size is greater than 10MB (10 * 1024 * 1024 bytes)
        if os.path.exists(log_file) and os.path.getsize(log_file) > 10 * 1024 * 1024:
            logging.warning(f"Log file {log_file} is over 10MB. Purging log file.")
            open(log_file, 'w').close()  # Purge the log file (clear content)

        # Only log to file (no console output)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)
    else:
        # Console handler for printing logs to the terminal (if no log file is specified)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(console_handler)

# Function to send a command to Moonraker and wait for a response
async def send_to_moonraker(websocket, command):
    """Send a Marlin command to Moonraker as an API request and wait for the response."""
    message = None

    if command == "M105":
        # Use printer.objects.query to get the status of the extruder, heater bed, and gcode_move
        message = {
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": {
                    "extruder": None,
                    "heater_bed": None,
                    "gcode_move": None
                }
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

    if message:
        # Send the message to Moonraker and await a response
        await websocket.send(json.dumps(message))
        logging.debug(f"Sent command: {command} to Moonraker - Message: {json.dumps(message)}")

        # Wait for the response from Moonraker
        response = await websocket.recv()
        data = json.loads(response)
        logging.debug(f"Received response from Moonraker: {response}")

        # Check if there's an error in the response
        if 'error' in data:
            error_message = data['error']
            logging.error(f"Moonraker returned an error: Code: {error_message.get('code', 'N/A')} - Message: {error_message.get('message', 'No message')}")
            logging.error(f"Request sent: {json.dumps(message)}")  # Log the request that caused the error
            return None  # Returning None or handling error accordingly

        # Process the response here if no error
        return data
    else:
        logging.error(f"No message for command: {command}")
        return None

# Function to subscribe to Moonraker updates (extruder, heater_bed, and gcode_move)
async def subscribe_to_updates(websocket):
    """Subscribe to printer updates for extruder, heater_bed, and gcode_move."""
    subscribe_message = {
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
    await websocket.send(json.dumps(subscribe_message))
    logging.info("Subscribed to printer updates for extruder, heater_bed, and gcode_move.")

# Function to listen for updates from Moonraker (subscription data)
async def listen_for_updates(websocket):
    """Listen for updates on the subscribed objects from Moonraker and push to a queue."""
    while True:
        try:
            # Wait for the next update from Moonraker
            update = await websocket.recv()
            status_update = json.loads(update)
            logging.debug(f"Received status update: {status_update}")

            # Put the update into the message queue for processing by other tasks
            await message_queue.put(status_update)

        except websockets.exceptions.ConnectionClosed as e:
            logging.error(f"Connection closed: {e}")
            break  # Exit the loop if the WebSocket connection is closed

# Function to read commands from Marlin via serial port
def read_from_serial(websocket):
    """Read commands from the serial port and process them."""
    while True:
        if ser.in_waiting > 0:
            command = ser.readline().decode('utf-8').strip()  # Read a line from the serial port
            if command:
                logging.info(f"Received command from Marlin: {command}")
                asyncio.run(handle_command_from_serial(websocket, command))  # Pass websocket to handler

# Function to handle the received command from Marlin
async def handle_command_from_serial(websocket, command):
    """Handle a command received from Marlin, process and send to Moonraker or Marlin."""
    # Send the command to Moonraker if needed
    if command in ["M105", "G28", "M104", "M106", "G1"]:
        await send_to_moonraker(websocket, command)  # Send to Moonraker API
    else:
        logging.warning(f"Unknown command received from Marlin: {command}")

# Function to handle communication with Moonraker and Marlin
async def handle_communication(moonraker_uri, serial_port, baud_rate):
    global ser

    # Initialize the serial connection
    ser = serial.Serial(serial_port, baud_rate)

    async with websockets.connect(moonraker_uri) as websocket:
        # Subscribe to updates (extruder, heater_bed, and gcode_move)
        await subscribe_to_updates(websocket)

        # Listen for updates in a background task
        listen_task = asyncio.create_task(listen_for_updates(websocket))

        # Start reading from serial (handling Marlin commands)
        serial_task = asyncio.create_task(asyncio.to_thread(read_from_serial, websocket))

        # Wait for all tasks to complete (this is an ongoing process)
        await asyncio.gather(listen_task, serial_task)

# Main entry point of the script
if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()

    # Set up logging
    setup_logging(log_file=args.log_file, verbose=args.verbose)

    # Run the asyncio event loop
    asyncio.run(handle_communication(args.moonraker_uri, args.serial_port, args.baud_rate))
