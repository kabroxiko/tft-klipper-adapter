import asyncio
import json
import logging
import argparse
from queue import Queue
from websockets import connect
import serial

# Global response formats
TEMPERATURE_RESPONSE_FORMAT = "ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0"
POSITION_RESPONSE_FORMAT = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok"
FEED_RATE_RESPONSE_FORMAT = "FR:{fr:.2f}%\nok"
FLOW_RATE_RESPONSE_FORMAT = "E0 Flow: {er:.2f}%\nok"
M503_RESPONSE_FORMAT = "Steps per unit: X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f}\nMax feedrates: X:{x_feed:.2f} Y:{y_feed:.2f} Z:{z_feed:.2f} E:{e_feed:.2f}\nAcceleration: {acc:.2f}\nok"
M92_RESPONSE_FORMAT = "ok Steps per unit: X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f}"
M211_RESPONSE_FORMAT = "Soft endstops: {state}\nok"

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
            "gcode_move": {"position": {"x": 0.0, "y": 0.0, "z": 0.0, "e": 0.0}},
            "motion": {"speed_factor": 100.0, "extrude_factor": 100.0},
            "steps": {"x": 80.0, "y": 80.0, "z": 400.0, "e": 93.0},
            "feedrates": {"x": 3000.0, "y": 3000.0, "z": 5.0, "e": 25.0},
            "acceleration": 500.0,
            "soft_endstops": "enabled"
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
                    "gcode_move": None,
                    "motion": None
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
                self.send_status_update_to_tft()
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
        extruder = self.latest_values["extruder"]
        heater_bed = self.latest_values["heater_bed"]
        temperature_status = TEMPERATURE_RESPONSE_FORMAT.format(
            ETemp=extruder['temperature'],
            ETarget=extruder['target'],
            BTemp=heater_bed['temperature'],
            BTarget=heater_bed['target']
        )
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
                elif gcode == "M114":
                    response = self.format_position_response()
                    self.send_to_tft(response)
                elif gcode.startswith("M220"):
                    self.process_feed_rate_command(gcode)
                elif gcode.startswith("M221"):
                    self.process_flow_rate_command(gcode)
                elif gcode == "M503":
                    response = self.format_m503_response()
                    self.send_to_tft(response)
                elif gcode.startswith("M92"):
                    self.process_m92_command(gcode)
                elif gcode == "M211":
                    response = self.format_m211_response()
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
            return TEMPERATURE_RESPONSE_FORMAT.format(
                ETemp=ETemp,
                ETarget=ETarget,
                BTemp=BTemp,
                BTarget=BTarget
            )
        except Exception as e:
            logging.error(f"Error parsing G-code response: {e}")
            return None

    def format_temperature_response(self):
        extruder = self.latest_values["extruder"]
        heater_bed = self.latest_values["heater_bed"]
        return TEMPERATURE_RESPONSE_FORMAT.format(
            ETemp=extruder['temperature'],
            ETarget=extruder['target'],
            BTemp=heater_bed['temperature'],
            BTarget=heater_bed['target']
        )

    def format_position_response(self):
        position = self.latest_values["gcode_move"]["position"]
        return POSITION_RESPONSE_FORMAT.format(
            x=position['x'],
            y=position['y'],
            z=position['z'],
            e=position['e']
        )

    def process_feed_rate_command(self, gcode):
        try:
            parts = gcode.split()
            feed_rate = 100.0
            for part in parts:
                if part.startswith("S"):
                    feed_rate = float(part[1:])
            self.latest_values["motion"]["speed_factor"] = feed_rate
            self.send_to_tft(FEED_RATE_RESPONSE_FORMAT.format(fr=feed_rate))
        except Exception as e:
            logging.error(f"Error processing M220 command: {e}")

    def process_flow_rate_command(self, gcode):
        try:
            parts = gcode.split()
            flow_rate = 100.0
            for part in parts:
                if part.startswith("S"):
                    flow_rate = float(part[1:])
            self.latest_values["motion"]["extrude_factor"] = flow_rate
            self.send_to_tft(FLOW_RATE_RESPONSE_FORMAT.format(er=flow_rate))
        except Exception as e:
            logging.error(f"Error processing M221 command: {e}")

    def process_m92_command(self, gcode):
        try:
            parts = gcode.split()
            for part in parts:
                if part.startswith("X"):
                    self.latest_values["steps"]["x"] = float(part[1:])
                elif part.startswith("Y"):
                    self.latest_values["steps"]["y"] = float(part[1:])
                elif part.startswith("Z"):
                    self.latest_values["steps"]["z"] = float(part[1:])
                elif part.startswith("E"):
                    self.latest_values["steps"]["e"] = float(part[1:])
            response = M92_RESPONSE_FORMAT.format(
                x=self.latest_values["steps"]["x"],
                y=self.latest_values["steps"]["y"],
                z=self.latest_values["steps"]["z"],
                e=self.latest_values["steps"]["e"]
            )
            self.send_to_tft(response)
        except Exception as e:
            logging.error(f"Error processing M92 command: {e}")

    def format_m503_response(self):
        steps = self.latest_values["steps"]
        feedrates = self.latest_values["feedrates"]
        acceleration = self.latest_values["acceleration"]
        return M503_RESPONSE_FORMAT.format(
            x=steps['x'],
            y=steps['y'],
            z=steps['z'],
            e=steps['e'],
            x_feed=feedrates['x'],
            y_feed=feedrates['y'],
            z_feed=feedrates['z'],
            e_feed=feedrates['e'],
            acc=acceleration
        )

    def format_m211_response(self):
        return M211_RESPONSE_FORMAT.format(
            state=self.latest_values["soft_endstops"]
        )

    def send_to_tft(self, message):
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
    parser = argparse.ArgumentParser(description="TFT Adapter for serial communication and WebSocket interaction.")
    parser.add_argument('-s', '--serial-port', type=str, default='/dev/ttyS2', help='Serial port to use.')
    parser.add_argument('-b', '--baud-rate', type=int, default=115200, help='Baud rate for serial communication.')
    parser.add_argument('-w', '--websocket-url', type=str, default='ws://localhost/websocket', help='WebSocket URL for printer connection.')
    parser.add_argument('-l', '--log-file', type=str, default=None, help='Log file location. If not specified, logs to stdout.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging.')
    return parser.parse_args()

def setup_logging(log_file, verbose):
    log_level = logging.DEBUG if verbose else logging.INFO
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    if log_file:
        logging.basicConfig(filename=log_file, level=log_level, format=log_format)
    else:
        logging.basicConfig(level=log_level, format=log_format)

if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_file, args.verbose)

    adapter = TFTAdapter(
        serial_port=args.serial_port,
        baud_rate=args.baud_rate,
        websocket_url=args.websocket_url
    )

    try:
        asyncio.run(adapter.run())
    except Exception as e:
        logging.error(f"Fatal error: {e}")
