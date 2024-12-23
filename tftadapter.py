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
G28_RESPONSE_FORMAT = "ok Homing done\n"

# Whitelist of Marlin-compatible G-codes
MOONRAKER_COMPATIBLE_GCODES = {"M105", "M114", "M220", "M221", "M503", "M92", "M211", "G28"}

class SerialHandler:
    def __init__(self, serial_port, baud_rate):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.connection = None

    def initialize(self):
        try:
            self.connection = serial.Serial(self.serial_port, self.baud_rate, timeout=0.1)
            logging.info(f"Connected to serial port {self.serial_port} at {self.baud_rate} baud.")
        except Exception as e:
            logging.error(f"Error initializing serial connection: {e}")
            raise

    def read_gcode(self):
        if self.connection.in_waiting > 0:
            return self.connection.readline().decode("utf-8").strip()
        return None

    def write_response(self, message):
        try:
            self.connection.write((message + "\n").encode("utf-8"))
            logging.info(f"Sent response back to TFT: {message}")
        except Exception as e:
            logging.error(f"Error sending message to TFT: {e}")


class WebSocketHandler:
    def __init__(self, websocket_url, message_queue):
        self.websocket_url = websocket_url
        self.message_queue = message_queue
        self.latest_values = {
            "extruder": {"temperature": 0.0, "target": 0.0},
            "heater_bed": {"temperature": 0.0, "target": 0.0},
            "gcode_move": {"position": {"x": 0.0, "y": 0.0, "z": 0.0, "e": 0.0}},
            "motion": {"speed_factor": 100.0, "extrude_factor": 100.0},
            "steps": {"x": 80.0, "y": 80.0, "z": 400.0, "e": 93.0},
            "feedrates": {"x": 3000.0, "y": 3000.0, "z": 5.0, "e": 25.0},
            "acceleration": 500.0
        }

    async def handler(self):
        async with connect(self.websocket_url) as websocket:
            await self.query_printer_objects(websocket)
            await self.subscribe_to_printer_objects(websocket)
            while True:
                try:
                    message = await websocket.recv()
                    self.handle_message(message)
                except Exception as e:
                    logging.error(f"Error in WebSocket handler: {e}")

    async def query_printer_objects(self, websocket):
        query_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": {
                    "extruder": ["temperature", "target"],
                    "heater_bed": ["temperature", "target"],
                    "gcode_move": ["position"],
                    "motion": ["speed_factor", "extrude_factor"],
                    "toolhead": ["homed_axes"],
                    "stepper": ["steps"]
                }
            },
            "id": 1
        })
        await websocket.send(query_message)
        response = await websocket.recv()
        self.update_latest_values(json.loads(response).get("result", {}))
        logging.info("Queried initial printer object values.")

    async def subscribe_to_printer_objects(self, websocket):
        subscription_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": {
                    "extruder": None,
                    "heater_bed": None,
                    "gcode_move": None,
                    "motion": None,
                    "toolhead": None
                }
            }
        })
        await websocket.send(subscription_message)
        logging.info("Subscribed to printer object updates.")

    async def send_gcode_and_wait(self, gcode):
        """Send a G-code to Moonraker and wait for the response."""
        # try:
        message_id = 100  # You can implement an incrementing ID for unique requests
        gcode_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.gcode.script",
            "params": {"script": gcode},
            "id": message_id
        })
        # Send the G-code
        async with connect(self.websocket_url) as websocket:
            await websocket.send(gcode_message)

            # Wait for a response with matching ID
            while True:
                response = await websocket.recv()
                response_data = json.loads(response)
                if response_data.get("id") == message_id:
                    return response_data.get("result", {})
        # except Exception as e:
        #     logging.error(f"Error sending G-code to Moonraker: {e}")
        #     return None

    def handle_message(self, message):
        try:
            data = json.loads(message)
            if "method" in data and data["method"] == "notify_status_update":
                self.update_latest_values(data.get("params", [{}])[0])
            elif "method" in data and data["method"] == "notify_gcode_response":
                self.message_queue.put(data["params"][0])
        except Exception as e:
            logging.error(f"Error processing WebSocket message: {e}")

    def update_latest_values(self, updates):
        for key, values in updates.items():
            if key in self.latest_values:
                self.latest_values[key].update(values)


class TFTAdapter:
    def __init__(self, serial_handler, websocket_handler):
        self.serial_handler = serial_handler
        self.websocket_handler = websocket_handler
        self.gcode_queue = Queue()

    async def serial_reader(self):
        while True:
            gcode = self.serial_handler.read_gcode()
            if gcode:
                logging.info(f"Received G-code from serial: {gcode}")
                self.gcode_queue.put(gcode)
            await asyncio.sleep(0.1)

    async def process_gcode_queue(self):
        while True:
            if not self.gcode_queue.empty():
                gcode = self.gcode_queue.get()
                logging.info(f"Processing G-code: {gcode}")
                if gcode.split()[0] in MOONRAKER_COMPATIBLE_GCODES:
                    response = await self.handle_gcode(gcode)
                    if response:
                        self.serial_handler.write_response(response)
                else:
                    logging.warning(f"G-code {gcode} is not in the whitelist and will not be processed.")
            await asyncio.sleep(0.1)

    async def periodic_temperature_update(self):
        while True:
            response = self.format_temperature_response()
            if response:
                self.serial_handler.write_response(response)
            await asyncio.sleep(3)

    async def handle_gcode(self, gcode):
        if gcode == "M105":
            return self.format_temperature_response()
        elif gcode == "M114":
            return self.format_position_response()
        elif gcode.startswith("M220"):
            return self.process_feed_rate_command(gcode)
        elif gcode.startswith("M221"):
            return self.process_flow_rate_command(gcode)
        elif gcode == "M503":
            return self.format_m503_response()
        elif gcode.startswith("M92"):
            return self.process_m92_command(gcode)
        elif gcode == "M211":
            return self.format_m211_response()
        elif gcode == "G28":
            return await self.websocket_handler.send_gcode_and_wait(gcode)
        return None

    def format_temperature_response(self):
        extruder = self.websocket_handler.latest_values["extruder"]
        heater_bed = self.websocket_handler.latest_values["heater_bed"]
        return TEMPERATURE_RESPONSE_FORMAT.format(
            ETemp=extruder['temperature'],
            ETarget=extruder['target'],
            BTemp=heater_bed['temperature'],
            BTarget=heater_bed['target']
        )

    def format_position_response(self):
        position = self.websocket_handler.latest_values["gcode_move"]["position"]
        return POSITION_RESPONSE_FORMAT.format(
            x=position['x'],
            y=position['y'],
            z=position['z'],
            e=position['e']
        )

    def process_feed_rate_command(self, gcode):
        parts = gcode.split()
        feed_rate = 100.0
        for part in parts:
            if part.startswith("S"):
                feed_rate = float(part[1:])
        self.websocket_handler.latest_values["motion"]["speed_factor"] = feed_rate
        return FEED_RATE_RESPONSE_FORMAT.format(fr=feed_rate)

    def process_flow_rate_command(self, gcode):
        parts = gcode.split()
        flow_rate = 100.0
        for part in parts:
            if part.startswith("S"):
                flow_rate = float(part[1:])
        self.websocket_handler.latest_values["motion"]["extrude_factor"] = flow_rate
        return FLOW_RATE_RESPONSE_FORMAT.format(er=flow_rate)

    def format_m503_response(self):
        steps = self.websocket_handler.latest_values["steps"]
        feedrates = self.websocket_handler.latest_values["feedrates"]
        acceleration = self.websocket_handler.latest_values["acceleration"]

        return M503_RESPONSE_FORMAT.format(
            x=steps["x"],
            y=steps["y"],
            z=steps["z"],
            e=steps["e"],
            x_feed=feedrates["x"],
            y_feed=feedrates["y"],
            z_feed=feedrates["z"],
            e_feed=feedrates["e"],
            acc=acceleration
        )

    def process_m92_command(self, gcode):
        steps = self.websocket_handler.latest_values["steps"]
        return M92_RESPONSE_FORMAT.format(
            x=steps["x"],
            y=steps["y"],
            z=steps["z"],
            e=steps["e"]
        )

    def format_m211_response(self):
        state = "On"  # Replace with actual logic to determine soft endstops state
        return M211_RESPONSE_FORMAT.format(state=state)

    def process_g28_command(self):
        # Simulate homing process
        return G28_RESPONSE_FORMAT

async def main():
    parser = argparse.ArgumentParser(description="TFT Adapter for Moonraker and Artillery TFT.")
    parser.add_argument("-p", "--serial_port", type=str, default="/dev/ttyS2", help="Serial port for TFT communication.")
    parser.add_argument("-b", "--baud_rate", type=int, default=115200, help="Baud rate for serial communication.")
    parser.add_argument("-w", "--websocket_url", type=str, default="ws://127.0.0.1:7125/websocket", help="WebSocket URL for Moonraker.")
    parser.add_argument("-l", "--log_file", type=str, default=None, help="Log file location. If not specified, logs to stdout.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s - %(levelname)s - %(message)s",
                        filename=args.log_file, filemode='a' if args.log_file else None)

    message_queue = Queue()
    serial_handler = SerialHandler(args.serial_port, args.baud_rate)
    websocket_handler = WebSocketHandler(args.websocket_url, message_queue)
    tft_adapter = TFTAdapter(serial_handler, websocket_handler)

    serial_handler.initialize()

    await asyncio.gather(
        websocket_handler.handler(),
        tft_adapter.serial_reader(),
        tft_adapter.process_gcode_queue(),
        tft_adapter.periodic_temperature_update()
    )

if __name__ == "__main__":
    asyncio.run(main())
