import asyncio
import json
import logging
import argparse
from queue import Queue
from websockets import connect
import serial

class TFTAdapter:
    def __init__(self, serial_port, baud_rate, websocket_url):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.websocket_url = websocket_url
        self.serial_connection = None
        self.gcode_queue = Queue()
        self.latest_values = {
            "extruder": {"temperature": 0.0, "target": 0.0},
            "heater_bed": {"temperature": 0.0, "target": 0.0},
        }

    def initialize_serial(self):
        try:
            self.serial_connection = serial.Serial(self.serial_port, self.baud_rate, timeout=0.1)
            logging.info(f"Connected to serial port {self.serial_port} at {self.baud_rate} baud.")
        except Exception as e:
            logging.error(f"Error initializing serial connection: {e}")
            raise

    async def websocket_handler(self):
        async with connect(self.websocket_url) as websocket:
            await self.subscribe_to_printer_objects(websocket)
            while True:
                try:
                    message = await websocket.recv()
                    self.handle_websocket_message(message)
                except Exception as e:
                    logging.error(f"Error in WebSocket handler: {e}")

    async def subscribe_to_printer_objects(self, websocket):
        subscription_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": {
                    "extruder": None,
                    "heater_bed": None,
                    "gcode_move": None
                }
            }
        })
        await websocket.send(subscription_message)
        logging.info("Subscribed to printer object updates.")

    def handle_websocket_message(self, message):
        try:
            data = json.loads(message)
            if "method" in data and data["method"] == "notify_status_update":
                self.update_latest_values(data.get("params", [{}])[0])
                self.send_status_update_to_tft()  # Send status update to TFT
            elif "method" in data and data["method"] == "notify_gcode_response":
                response = self.parse_gcode_response(data["params"][0])
                if response:
                    self.send_to_tft(response)
        except Exception as e:
            logging.error(f"Error processing WebSocket message: {e}")

    def update_latest_values(self, updates):
        for key, values in updates.items():
            if key in self.latest_values:
                self.latest_values[key].update(values)

    def send_status_update_to_tft(self):
        """Format the latest status and send it to the TFT display"""
        extruder = self.latest_values["extruder"]
        heater_bed = self.latest_values["heater_bed"]
        temperature_status = f"ok T:{extruder['temperature']:.2f} /{extruder['target']:.2f} B:{heater_bed['temperature']:.2f} /{heater_bed['target']:.2f} @:0 B@:0"
        self.send_to_tft(temperature_status)

    async def serial_reader(self):
        while True:
            if self.serial_connection.in_waiting > 0:
                gcode = self.serial_connection.readline().decode("utf-8").strip()
                if gcode:
                    logging.info(f"Received G-code from serial: {gcode}")
                    self.gcode_queue.put(gcode)
            await asyncio.sleep(0.1)

    async def process_gcode_queue(self):
        while True:
            if not self.gcode_queue.empty():
                gcode = self.gcode_queue.get()
                logging.info(f"Processing G-code: {gcode}")
                if gcode == "M105":
                    response = self.format_temperature_response()
                    self.send_to_tft(response)
            await asyncio.sleep(0.1)

    def parse_gcode_response(self, gcode_response):
        try:
            components = gcode_response.split()
            BTemp, BTarget, ETemp, ETarget = 0.0, 0.0, 0.0, 0.0
            for i, comp in enumerate(components):
                if comp.startswith("B:"):
                    BTemp = float(comp.split(":")[1])
                    if i + 1 < len(components) and components[i + 1].startswith("/"):
                        BTarget = float(components[i + 1][1:])
                elif comp.startswith("T0:"):
                    ETemp = float(comp.split(":")[1])
                    if i + 1 < len(components) and components[i + 1].startswith("/"):
                        ETarget = float(components[i + 1][1:])
            return f"ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0"
        except Exception as e:
            logging.error(f"Error parsing G-code response: {e}")
            return None

    def format_temperature_response(self):
        extruder = self.latest_values["extruder"]
        heater_bed = self.latest_values["heater_bed"]
        return f"ok T:{extruder['temperature']:.2f} /{extruder['target']:.2f} B:{heater_bed['temperature']:.2f} /{heater_bed['target']:.2f} @:0 B@:0"

    def send_to_tft(self, message):
        """Send a response to the TFT display via serial connection"""
        try:
            self.serial_connection.write((message + "\n").encode("utf-8"))
            logging.info(f"Sent response back to TFT: {message}")
        except Exception as e:
            logging.error(f"Error sending message to TFT: {e}")

    async def run(self):
        self.initialize_serial()
        await asyncio.gather(
            self.serial_reader(),
            self.process_gcode_queue(),
            self.websocket_handler()
        )

def parse_args():
    """Parse command-line arguments with single-letter flags"""
    parser = argparse.ArgumentParser(description="TFT Adapter for serial communication and WebSocket interaction.")
    parser.add_argument('-s', '--serial-port', type=str, default='/dev/ttyS2', help='Serial port for communication')
    parser.add_argument('-b', '--baud-rate', type=int, default=115200, help='Baud rate for serial communication')
    parser.add_argument('-w', '--websocket-url', type=str, default='ws://localhost/websocket', help='WebSocket URL')

    return parser.parse_args()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    # Parse command-line arguments
    args = parse_args()

    # Instantiate the adapter with arguments
    adapter = TFTAdapter(
        serial_port=args.serial_port,
        baud_rate=args.baud_rate,
        websocket_url=args.websocket_url
    )

    try:
        asyncio.run(adapter.run())
    except Exception as e:
        logging.error(f"Fatal error: {e}")
