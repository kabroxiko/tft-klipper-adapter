import asyncio
import json
import logging
import argparse
from queue import Queue
from websockets import connect
import serial
import re
from jinja2 import Template

MACHINE_TYPE = "Artillery Genius Pro"

# Global response formats in Jinja2
TEMPERATURE_TEMPLATE = Template(
    "T:{{ extruder.temperature | round(2) }} /{{ extruder.target | round(2) }} "
    "B:{{ heater_bed.temperature | round(2) }} /{{ heater_bed.target | round(2) }} "
    "@:0 B@:0\n"
    "ok"
)

POSITION_TEMPLATE = Template(
    "X:{{ gcode_move.position[0] | round(2) }} "
    "Y:{{ gcode_move.position[1] | round(2) }} "
    "Z:{{ gcode_move.position[2] | round(2) }} "
    "E:{{ gcode_move.position[3] | round(2) }}\n"
    "ok"
)

FEED_RATE_TEMPLATE = Template(
    "FR:{{ gcode_move.speed_factor * 100 | int }}%\n"
    "ok"
)
FLOW_RATE_TEMPLATE = Template(
    "E0 Flow:{{ gcode_move.extrude_factor * 100 | int }}%\n"
    "ok"
)

REPORT_SETTINGS_TEMPLATE = Template(
    "M203 X{{ toolhead.max_velocity }} Y{{ toolhead.max_velocity }} "
    "Z{{ configfile.settings.printer.max_z_velocity }} E{{ configfile.settings.extruder.max_extrude_only_velocity }}\n"
    "M201 X{{ toolhead.max_accel }} Y{{ toolhead.max_accel }} "
    "Z{{ configfile.settings.printer.max_z_accel }} E{{ configfile.settings.extruder.max_extrude_only_accel }}\n"
    "M206 X{{ gcode_move.homing_origin[0] }} Y{{ gcode_move.homing_origin[1] }} Z{{ gcode_move.homing_origin[2] }}\n"
    "M851 X{{ configfile.settings.bltouch.x_offset }} Y{{ configfile.settings.bltouch.y_offset }} Z{{ configfile.settings.bltouch.z_offset }}\n"
    "M420 S1 Z{{ configfile.settings.bed_mesh.fade_end }}\n"
    "M106 S{{ fan.speed }}\n"
    "ok"
)

FIRMWARE_INFO_TEMPLATE = Template(
    "FIRMWARE_NAME:Klipper {{ mcu.mcu_version }} "
    "SOURCE_CODE_URL:https://github.com/Klipper3d/klipper "
    "PROTOCOL_VERSION:1.0 "
    f"MACHINE_TYPE:{MACHINE_TYPE}\n"
    "Cap:EEPROM:1\n"
    "Cap:AUTOREPORT_TEMP:1\n"
    "Cap:AUTOREPORT_POS:1\n"
    "Cap:AUTOLEVEL:1\n"
    "Cap:Z_PROBE:1\n"
    "Cap:LEVELING_DATA:0\n"
    "Cap:SOFTWARE_POWER:0\n"
    "Cap:TOGGLE_LIGHTS:0\n"
    "Cap:CASE_LIGHT_BRIGHTNESS:0\n"
    "Cap:EMERGENCY_PARSER:1\n"
    "Cap:PROMPT_SUPPORT:0\n"
    "Cap:SDCARD:1\n"
    "Cap:MULTI_VOLUME:0\n"
    "Cap:AUTOREPORT_SD_STATUS:1\n"
    "Cap:LONG_FILENAME:1\n"
    "Cap:BABYSTEPPING:1\n"
    "Cap:BUILD_PERCENT:1\n"
    "Cap:CHAMBER_TEMPERATURE:0\n"
    "ok"
)

SOFTWARE_ENDSTOPS_TEMPLATE = Template(
    "Soft endstops: {{ state }}\n"
    "ok"
)

FILE_LIST_TEMPLATE = Template(
    "Begin file list\n"
    "{% for file in file_list %}{{ file.path }} {{ file.size }}\n{% endfor %}"
    "End file list\n"
    "ok"
)

TRACKED_OBJECTS = {
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
    "gcode_move": ["position", "homing_origin", "speed_factor", "extrude_factor"],
    "toolhead": ["max_velocity", "max_accel"],
    "mcu": ["mcu_version"],
    "configfile": ["settings"],
    "fan": ["speed"],
    "filament_switch_sensor filament_sensor": None
}

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
            logging.info(f"Sent to TFT: {message}")
        except Exception as e:
            logging.error(f"Error sending message to TFT: {e}")


class WebSocketHandler:
    def __init__(self, websocket_url, message_queue):
        self.websocket_url = websocket_url
        self.message_queue = message_queue
        self.latest_values = {}
        self.file_list = []  # Cache of the file list
        self.retry_delay = 1

    async def handler(self):
        """Handle WebSocket messages and ensure reconnection."""
        while True:
            try:
                async with connect(self.websocket_url) as websocket:
                    logging.info("Connected to WebSocket.")
                    await self.initialize_values(websocket)
                    await self.subscribe_to_printer_objects(websocket)
                    await self.listen_to_websocket(websocket)
            except Exception as e:
                logging.error(f"WebSocket connection error: {e}. Retrying in {self.retry_delay} seconds...")
                await asyncio.sleep(self.retry_delay)
                self.retry_delay = min(self.retry_delay * 2, 60)  # Exponential backoff, max 60 seconds

    async def listen_to_websocket(self, websocket):
        """Listen to WebSocket messages and process them."""
        try:
            while True:
                message = await websocket.recv()
                self.handle_message(message)
        except Exception as e:
            logging.error(f"Error in WebSocket listener: {e}")
            raise  # Trigger reconnection

    async def initialize_values(self, websocket):
        """Initialize the latest values and file list from the printer."""
        # Query printer objects
        query_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": TRACKED_OBJECTS
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

        # Query file list
        await self.query_file_list(websocket)

    async def subscribe_to_printer_objects(self, websocket):
        subscription_message = json.dumps({
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": TRACKED_OBJECTS
            }
        })
        await websocket.send(subscription_message)
        logging.info("Subscribed to printer object updates.")

    async def send_gcode_and_wait(self, gcode):
        """Send a G-code to Moonraker and wait for the response."""
        try:
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
                message = ""
                while True:
                    response = json.loads(await websocket.recv())
                    logging.debug(f"Response: {response}")

                    if response.get("method") == "notify_gcode_response":
                        message += f"{response['params'][0]}\n"
                    elif response.get("id") == message_id:
                        message += f"{response.get('result')}\n"
                        logging.debug(f"Message: {message.strip()}")
                        return message.strip()

        except Exception as e:
            logging.error(f"Error sending G-code to Moonraker: {e}")
            return None

    def handle_message(self, message):
        logging.debug(f"Processing WebSocket message: {message}")
        try:
            data = json.loads(message)
            if data is not None:
                if data.get("method") == "notify_status_update":
                    self.update_latest_values(data.get("params")[0])
                elif data.get("method") == "notify_gcode_response":
                    self.message_queue.put(data["params"][0])
                elif data.get("method") == "notify_filelist_changed":
                    file_path = data["params"][0].get("item").get("path")
                    if file_path.endswith(".gcode"):
                        self.update_file_list(data["params"][0])
        except Exception as e:
            logging.error(f"Error processing WebSocket message: {e}")

    def update_file_list(self, params):
        item = params.get("item")
        action = params.get("action")
        file_path = item.get("path")
        file_size = item.get("size", 0)
        if action == "create_file":
            self.file_list.insert(0, {"path": file_path, "size": file_size})
            logging.info(f"Added new file to file_list: {file_path}")
        elif action == "modify_file":
            for i, file in enumerate(self.file_list):
                if file.get("path") == file_path:
                    self.file_list[i]["size"] = file_size
                    logging.info(f"Updated size for {file_path}: {file_size}")
                    break
        elif action == "delete_file":
            self.file_list = [file for file in self.file_list if file.get("path") != file_path]
            logging.info(f"File {file_path} removed from file list.")
        logging.info(f"self.file_list: {self.file_list}")

    async def query_file_list(self, websocket):
        """Query the list of files from Moonraker."""
        try:
            file_query_message = json.dumps({
                "jsonrpc": "2.0",
                "method": "server.files.list",
                "params": {
                    "path": ""  # Query root directory or adjust as needed
                },
                "id": 2
            })
            await websocket.send(file_query_message)
            while True:
                response = json.loads(await websocket.recv())
                if response.get("id") == 2:
                    logging.info(f"response: {response}")
                    self.file_list = response.get("result")
                    logging.info(f"Updated file list: {self.file_list}")
                    break
        except Exception as e:
            logging.error(f"Error querying file list: {e}")

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
        self.auto_report_temperature = None
        self.auto_report_position = None

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
                response = await self.handle_gcode(gcode)
                if response != "":
                    self.serial_handler.write_response("ok" if f"{response}" == "None" else response)
            await asyncio.sleep(0.1)

    async def periodic_position_update(self):
        while True:
            if self.auto_report_position and self.auto_report_position > 0:
                self.serial_handler.write_response(f"{self.get_position()}")
                await asyncio.sleep(self.auto_report_position)
            else:
                await asyncio.sleep(1)

    async def periodic_temperature_update(self):
        while True:
            if self.auto_report_temperature and self.auto_report_temperature > 0:
                self.serial_handler.write_response(f"ok {self.get_temperature()}")
                await asyncio.sleep(self.auto_report_temperature)
            else:
                await asyncio.sleep(1)

    async def handle_gcode(self, request):
        gcode, *parameters = request.split(maxsplit=1)
        parameters = parameters[0] if parameters else None

        # Predefined G-code handlers
        gcode_handlers = {
            "M211": lambda: f"{self.get_software_endstops()}",
            "M115": lambda: f"{self.get_firmware_info()}",
            "M503": lambda: f"{self.get_report_settings()}",
            "M105": lambda: f"{self.get_temperature()}",
            "M114": lambda: f"{self.get_position()}",
            "M220": lambda: f"{self.get_feed_rate(gcode)}",
            "M221": lambda: f"{self.get_flow_rate(gcode)}",
            "M20":  lambda: f"{self.get_file_list(gcode)}",
        }

        # Direct handlers
        if gcode in gcode_handlers:
            return gcode_handlers[gcode]()

        # Auto-report G-codes
        if gcode == "M154":
            self.auto_report_position = int(parameters[1:]) if parameters else None
        elif gcode == "M155":
            self.auto_report_temperature = int(parameters[1:]) if parameters else None
        elif gcode == "M33":
            return f"{parameters}\nok"

        # Special G-codes with parameter-specific behavior
        elif gcode == "M201" and parameters:
            if parameters.startswith(("X", "Y")):
                max_acceleration = parameters[1:]
                return await self.websocket_handler.send_gcode_and_wait(
                    f"SET_VELOCITY_LIMIT ACCEL={max_acceleration} ACCEL_TO_DECEL={int(max_acceleration) / 2}"
                )
        elif gcode == "M203" and parameters.startswith(("X", "Y")):
            max_velocity = parameters[1:]
            return await self.websocket_handler.send_gcode_and_wait(
                f"SET_VELOCITY_LIMIT VELOCITY={max_velocity}"
            )
        elif gcode == "M206" and parameters.startswith(("X", "Y", "Z", "E")):
            return await self.websocket_handler.send_gcode_and_wait(
                f"SET_GCODE_OFFSET {parameters[:1]}={parameters[1:]}"
            )
        elif gcode == "M150":
            return await self.set_led_color(parameters)
        elif gcode == "M524":
            return await self.websocket_handler.send_gcode_and_wait("CANCEL_PRINT")
        elif gcode in ("M701", "M702"):
            action = "load" if gcode == "M701" else "unload"
            return await self.handle_filament(parameters, action=action)
        elif gcode == "M118":
            return await self.websocket_handler.send_gcode_and_wait(request)

        # Commands with no action or immediate acknowledgment
        elif gcode in {"M851", "M420", "M22", "M92", "T0"}:
            return "ok"
        elif gcode == "M108":
            return ""
        elif gcode in {"M21", "G90", "M82"}:
            return await self.websocket_handler.send_gcode_and_wait(request)

        # Fallback for unknown commands
        else:
            logging.warning(f"Unknown gcode: {request}")
            return None

    async def handle_filament(self, parameters="L25 T0 Z0", action="load"):
        pattern = re.compile(r'([LTZ])(\d+)')
        params = {match[0]: int(match[1]) for match in pattern.findall(parameters)}

        length = params.get("L", 25)
        extruder = params.get("T", 0)
        zmove = params.get("Z", 0)
        direction = 1 if action == "load" else -1
        command = (
            "G91\n"               # Relative Positioning
            f"G92 E{extruder}\n"  # Reset Extruder
            f"G1 Z{zmove} E{direction * length} F{3*60}\n"  # Extrude or Retract
            "G92 E0\n"            # Reset Extruder
        )
        return await self.websocket_handler.send_gcode_and_wait(command)

    async def load_filament(self, parameters="L25 T0 Z0"):
        return await self.handle_filament(parameters, direction=1)

    async def unload_filament(self, parameters="L25 T0 Z0"):
        return await self.handle_filament(parameters, direction=-1)

    async def set_led_color(self, parameters):
        # Use regex to extract key-value pairs like 'R255', 'U0', etc.
        pattern = re.compile(r'([RUBWPI])(\d+)')
        params = {match[0]: int(match[1]) for match in pattern.findall(parameters)}

        # Get the RGB, White, Brightness, and Intensity values, defaulting to 0 if not provided
        red = params.get("R", 0) / 255.0
        green = params.get("U", 0) / 255.0
        blue = params.get("B", 0) / 255.0
        white = params.get("W", 0) / 255.0
        brightness = params.get("P", 0) / 255.0
        intensity = params.get("I", 0) / 255.0  # Optional if needed

        return await self.websocket_handler.send_gcode_and_wait(
            f"SET_LED LED=statusled RED={red:.3f} GREEN={green:.3f} "
            f"BLUE={blue:.3f} WHITE={white:.3f} BRIGHTNESS={brightness:.3f}"
        )

    def render_template(self, template):
        return template.render(**self.websocket_handler.latest_values)

    def get_temperature(self):
        return self.render_template(TEMPERATURE_TEMPLATE)

    def get_position(self):
        return self.render_template(POSITION_TEMPLATE)

    def get_feed_rate(self, gcode):
        return self.render_template(FEED_RATE_TEMPLATE)

    def get_flow_rate(self, gcode):
        return self.render_template(FLOW_RATE_TEMPLATE)

    def get_report_settings(self):
        return self.render_template(REPORT_SETTINGS_TEMPLATE)

    def get_firmware_info(self):
        return self.render_template(FIRMWARE_INFO_TEMPLATE)

    def get_software_endstops(self):
        state = {"state": "On" if self.websocket_handler.latest_values["filament_switch_sensor filament_sensor"]["enabled"] else "Off"}
        return SOFTWARE_ENDSTOPS_TEMPLATE.render(**state)

    def get_file_list(self):
        """Format the file list as required for M20 using Jinja2."""
        return FILE_LIST_TEMPLATE.render(file_list=self.websocket_handler.file_list)

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
        tft_adapter.periodic_position_update(),
        tft_adapter.periodic_temperature_update()
    )

if __name__ == "__main__":
    asyncio.run(main())
