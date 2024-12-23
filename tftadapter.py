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
M503_M203_FORMAT = "M203 X{toolhead_max_velocity} Y{toolhead_max_velocity} Z{max_z_velocity} E{max_extrude_only_velocity}"
M503_M201_FORMAT = "M201 X{toolhead_max_accel} Y{toolhead_max_accel} Z{max_z_accel} E{max_extrude_only_accel}"
M503_M206_FORMAT = "M206 X{homing_origin_x} Y{homing_origin_y} Z{homing_origin_z}"
M503_M851_FORMAT = "M851 X{bltouch_x_offset} Y{bltouch_y_offset} Z{bltouch_x_offset}"
M503_M420_FORMAT = "M420 S1 Z{bed_mesh_fade_end}"
M503_M106_FORMAT = "M106 S{fan_speed}"
M211_RESPONSE_FORMAT = "Soft endstops: {state}\nok"
G28_RESPONSE_FORMAT = "ok Homing done\n"

OBJECTS = {
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
    "gcode_move": ["position", "homing_origin", "speed_factor", "extrude_factor"],
    "toolhead": ["max_velocity", "max_accel"],
    "mcu": ["mcu_version"],
    "configfile": ["settings"],
    "fan": ["speed"]
}

# Whitelist of Marlin-compatible G-codes
MOONRAKER_COMPATIBLE_GCODES = {"M105", "M114", "M115", "M220", "M221", "M503", "M92", "M211", "G28", "G90", "M82"}

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
        self.latest_values = {}  # Start with an empty dictionary

    async def handler(self, websocket):
        await self.subscribe_to_printer_objects(websocket)
        while True:
            try:
                message = await websocket.recv()
                self.handle_message(message)
            except Exception as e:
                logging.error(f"Error in WebSocket handler: {e}")

    async def initialize_values(self, websocket):
        query_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": OBJECTS
            },
            "id": 1
        })
        await websocket.send(query_message)
        result = None
        while result is None:
            response = await websocket.recv()
            result = json.loads(response).get("result")
        logging.info(f"result: {result}")
        self.latest_values = result.get("status")
        logging.info("Initialized latest values from printer.")

    async def subscribe_to_printer_objects(self, websocket):
        subscription_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": OBJECTS
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
            if data is not None:
                if data.get("method") == "notify_status_update":
                    self.update_latest_values(data.get("params")[0])
                elif data.get("method") == "notify_gcode_response":
                    self.message_queue.put(data["params"][0])
        except Exception as e:
            logging.error(f"Error processing WebSocket message: {e}")

    def update_latest_values(self, updates):
        logging.debug(f"Update latest_values: {updates}")
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
            return "ok"
        elif gcode == "M211":
            return self.format_m211_response()
        elif gcode == "M115":
            return self.format_m115_response()
        elif gcode.startswith("G28") or gcode.startswith("G90") or gcode.startswith("M82"):
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
            x=position[0],
            y=position[1],
            z=position[2],
            e=position[3]
        )

    def process_feed_rate_command(self, gcode):
        return FEED_RATE_RESPONSE_FORMAT.format(fr=self.websocket_handler.latest_values["gcode_move"]["speed_factor"] * 100)

    def process_flow_rate_command(self, gcode):
        return FLOW_RATE_RESPONSE_FORMAT.format(er=self.websocket_handler.latest_values["gcode_move"]["extrude_factor"] * 100)

    def format_m503_response(self):
        # Access the latest values from the WebSocket
        toolhead = self.websocket_handler.latest_values["toolhead"]
        config = self.websocket_handler.latest_values["configfile"]["settings"]
        gcode_move = self.websocket_handler.latest_values["gcode_move"]
        fan = self.websocket_handler.latest_values["fan"]

        # Format the M503 response using the global variables
        return M503_M203_FORMAT.format(
            toolhead_max_velocity=toolhead["max_velocity"],
            max_z_velocity=config["printer"]["max_z_velocity"],
            max_extrude_only_velocity=config["extruder"]["max_extrude_only_velocity"]
        ) + "\n" + M503_M201_FORMAT.format(
            toolhead_max_accel=toolhead["max_accel"],
            max_z_accel=config["printer"]["max_z_accel"],
            max_extrude_only_accel=config["extruder"]["max_extrude_only_accel"]
        ) + "\n" + M503_M206_FORMAT.format(
            homing_origin_x=gcode_move["homing_origin"][0],
            homing_origin_y=gcode_move["homing_origin"][1],
            homing_origin_z=gcode_move["homing_origin"][2]
        ) + "\n" + M503_M851_FORMAT.format(
            bltouch_x_offset=config["bltouch"]["x_offset"],
            bltouch_y_offset=config["bltouch"]["y_offset"]
        ) + "\n" + M503_M420_FORMAT.format(
            bed_mesh_fade_end=config["bed_mesh"]["fade_end"]
        ) + "\n" + M503_M106_FORMAT.format(
            fan_speed=fan["speed"] * 255.0  # Convert fan speed to PWM value
        ) + "\nok"

    def format_m115_response(self):
        mcu_version = self.websocket_handler.latest_values["mcu"]["mcu_version"]
        return f"FIRMWARE_NAME:Klipper {mcu_version} SOURCE_CODE_URL:https://github.com/Klipper3d/klipper PROTOCOL_VERSION:1.0 MACHINE_TYPE:Sidewinder X2\nok"

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

    async with connect(args.websocket_url) as websocket:
        await websocket_handler.initialize_values(websocket)
        await asyncio.gather(
            websocket_handler.handler(websocket),
            tft_adapter.serial_reader(),
            tft_adapter.process_gcode_queue(),
            tft_adapter.periodic_temperature_update()
        )

if __name__ == "__main__":
    asyncio.run(main())
