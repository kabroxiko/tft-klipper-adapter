import asyncio
import json
import logging
import argparse
from queue import Queue
from websockets import connect
import serial
import re
import os
from jinja2 import Template

MACHINE_TYPE = "Artillery Genius Pro"

# Global response formats in Jinja2
# {Error:{{ error }}
# echo:{{ message }}
# //action:{{ action }}
# {{ command }}

PRINT_STATUS_TEMPLATE = Template(
    "//action:notification Layer Left {{ (virtual_sdcard.file_position or 0) }}/{{ (virtual_sdcard.file_size or 0) }}"
)

TEMPERATURE_TEMPLATE = Template(
    "T:{{ extruder.temperature | round(2) }} /{{ extruder.target | round(2) }} "
    "B:{{ heater_bed.temperature | round(2) }} /{{ heater_bed.target | round(2) }} "
    "@:0 B@:0"
)

POSITION_TEMPLATE = Template(
    "X:{{ gcode_move.position[0] | round(2) }} "
    "Y:{{ gcode_move.position[1] | round(2) }} "
    "Z:{{ gcode_move.position[2] | round(2) }} "
    "E:{{ gcode_move.position[3] | round(2) }}"
)

FEED_RATE_TEMPLATE = Template(
    "FR:{{ gcode_move.speed_factor * 100 | int }}%"
)
FLOW_RATE_TEMPLATE = Template(
    "E0 Flow:{{ gcode_move.extrude_factor * 100 | int }}%"
)

REPORT_SETTINGS_TEMPLATE = Template(
    "M203 X{{ toolhead.max_velocity }} Y{{ toolhead.max_velocity }} "
    "Z{{ configfile.settings.printer.max_z_velocity }} E{{ configfile.settings.extruder.max_extrude_only_velocity }}\n"
    "M201 X{{ toolhead.max_accel }} Y{{ toolhead.max_accel }} "
    "Z{{ configfile.settings.printer.max_z_accel }} E{{ configfile.settings.extruder.max_extrude_only_accel }}\n"
    "M206 X{{ gcode_move.homing_origin[0] }} Y{{ gcode_move.homing_origin[1] }} Z{{ gcode_move.homing_origin[2] }}\n"
    "M851 X{{ configfile.settings.bltouch.x_offset }} Y{{ configfile.settings.bltouch.y_offset }} Z{{ configfile.settings.bltouch.z_offset }}\n"
    "M420 S1 Z{{ configfile.settings.bed_mesh.fade_end }}\n"
    "M106 S{{ fan.speed }}"
)

PROBE_OFFSET_TEMPLATE = Template(
    "M851 X{{ configfile.settings.bltouch.x_offset - gcode_move.homing_origin[0] }} "
    "Y{{ configfile.settings.bltouch.y_offset - gcode_move.homing_origin[1] }} "
    "Z{{ configfile.settings.bltouch.z_offset - gcode_move.homing_origin[2] }}"
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
    "Cap:CHAMBER_TEMPERATURE:0"
)

SOFTWARE_ENDSTOPS_TEMPLATE = Template(
    "Soft endstops: {{ state }}"
)

PROBE_TEST_TEMPLATE = Template(
    "echo:Last query: {{ probe.last_query }} Last Z result: {{ probe.last_z_result }}"
)

FILE_LIST_TEMPLATE = Template(
    "Begin file list\n"
    "{% for path, details in file_list.items() %}{{ path }} {{ details.size }}\n{% endfor %}"
    "End file list"
)

TRACKED_OBJECTS = {
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
    "gcode_move": ["position", "homing_origin", "speed_factor", "extrude_factor"],
    "toolhead": ["max_velocity", "max_accel"],
    "mcu": ["mcu_version"],
    "configfile": ["settings"],
    "fan": ["speed"],
    "virtual_sdcard": ["file_position", "file_size"],
    "print_stats": ["state"],
    "probe": ["last_query", "last_z_result"],
    "filament_switch_sensor filament_sensor": ["enabled"]
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

    def read(self):
        if self.connection.in_waiting > 0:
            return self.connection.readline().decode("utf-8").strip()
        return None

    def write(self, message):
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

    async def call_moonraker_script(self, scripts):
        """Send a single or multiple G-codes to Moonraker and wait for the responses."""
        try:
            if isinstance(scripts, str):
                scripts = [scripts]
            elif isinstance(scripts, list):
                scripts = scripts
            else:
                raise ValueError("Invalid script format. Must be a string or a list of strings.")

            responses = []
            async with connect(self.websocket_url) as websocket:
                for script in scripts:
                    message_id = id(script)  # Generate a unique ID for each request
                    logging.info(f"Call Moonraker script: {script}")
                    gcode_message = json.dumps({
                        "jsonrpc": "2.0",
                        "method": "printer.gcode.script",
                        "params": {"script": script},
                        "id": message_id
                    })
                    await websocket.send(gcode_message)

                    # Wait for a response with matching ID
                    response_message = ""
                    while True:
                        response = json.loads(await websocket.recv())
                        logging.debug(f"Response: {response}")
                        if response.get("method") == "notify_gcode_response":
                            response_message += f"{response['params'][0]}\n"
                        elif response.get("id") == message_id:
                            response_message += f"{response.get('result', '')}\n"
                            break
                    responses.append(response_message.strip())

            return "\n".join(responses)

        except Exception as e:
            logging.error(f"Error sending G-code(s) to Moonraker: {e}")
            return None

    def handle_message(self, message):
        logging.debug(f"Processing WebSocket message: {message}")
        try:
            data = json.loads(message)
            if data is not None:
                if data.get("method") == "notify_status_update":
                    logging.debug(f"Update: {data.get('params')[0]}")
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

    async def send_moonraker_request(self, method, params=None, request_id=None):
        """Send a JSON-RPC request and return the response."""
        try:
            async with connect(self.websocket_url) as websocket:
                request_id = request_id or int.from_bytes(os.urandom(2), "big")  # Generate a unique ID
                request = {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                    "id": request_id,
                }
                await websocket.send(json.dumps(request))

                while True:
                    response = json.loads(await websocket.recv())
                    if response.get("id") == request_id:
                        return response.get("result", {})
        except Exception as e:
            logging.error(f"JSON-RPC call error: {method}, {e}")
            return None

    async def initialize_values(self, websocket):
        """Initialize the latest values and file list from the printer."""
        result = await self.send_moonraker_request("printer.objects.query", {"objects": TRACKED_OBJECTS})
        logging.info(f"result: {result}")
        self.latest_values = result.get("status")
        logging.info("Initialized latest values from printer.")

    async def query_file_list(self):
        """Get the list of files."""
        return await self.send_moonraker_request("server.files.list", {"path": ""})

    async def start_print(self, filename):
        """Start a print."""
        return await self.send_moonraker_request("printer.print.start", {"filename": filename})

    async def pause_print(self):
        """Pause the current print."""
        return await self.send_moonraker_request("printer.print.pause")

    async def resume_print(self):
        """Pause the current print."""
        return await self.send_moonraker_request("printer.print.resume")

    async def cancel_print(self):
        """Cancel the current print."""
        return await self.send_moonraker_request("printer.print.cancel")

class TFTAdapter:
    def __init__(self, serial_handler, websocket_handler):
        self.serial_handler = serial_handler
        self.websocket_handler = websocket_handler
        self.gcode_queue = Queue()
        self.auto_report_temperature = 0
        self.auto_report_position = 0
        self.auto_report_print_status = 0
        self.selected_file = 0

    async def serial_reader(self):
        while True:
            gcode = self.serial_handler.read()
            if gcode:
                logging.debug(f"Received G-code from serial: {gcode}")
                self.gcode_queue.put(gcode)
            await asyncio.sleep(0.1)

    async def process_gcode_queue(self):
        while True:
            if not self.gcode_queue.empty():
                gcode = self.gcode_queue.get()
                logging.info(f"Processing G-code: {gcode}")
                response = await self.handle_gcode(gcode)
                if response and response != "":
                    logging.info(f"G-code response: {repr(response)}")
                    messages = response.split("\n")
                    for message in messages:
                        if message.startswith("!!"):
                            message = f"{{Error:{message}"

                        if message.strip():  # Avoid sending empty lines
                            self.serial_handler.write(message)

            await asyncio.sleep(0.1)

    async def periodic_update_report(self, auto_report_interval, template, prefix_ok=False):
        while True:
            interval = getattr(self, auto_report_interval, 0)
            if interval > 0:
                try:
                    report = template.render(**self.websocket_handler.latest_values)
                    if prefix_ok:
                        report = f"ok {report}"
                    self.serial_handler.write(report)
                except:
                    pass
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(1)

    async def handle_gcode(self, request):
        gcode, *parameters = request.split(maxsplit=1)
        parameters = parameters[0] if parameters else None
        params_dict = {
            key: value for key, value in re.findall(r"([A-Z])(-?\d+\.?\d*)", parameters)
        } if parameters else {}

        # Predefined G-code handlers
        GCODE_TEMPLATES = {
            "M105": TEMPERATURE_TEMPLATE,       # Report current temperatures
            "M114": POSITION_TEMPLATE,          # Report current position
            "M220": FEED_RATE_TEMPLATE,         # Set or report feed rate percentage
            "M221": FLOW_RATE_TEMPLATE,         # Set or report flow rate percentage
            "M503": REPORT_SETTINGS_TEMPLATE,   # Report current settings
            "M115": FIRMWARE_INFO_TEMPLATE,     # Report firmware information
            "M211": SOFTWARE_ENDSTOPS_TEMPLATE, # Enable/disable software endstops
            "M20":  FILE_LIST_TEMPLATE,         # List files on the SD card
        }
        gcode_handlers = {
            "M524": lambda _: self.websocket_handler.cancel_print()
        }
        handler = gcode_handlers.get(gcode, None)

        # Direct handlers
        if gcode in GCODE_TEMPLATES:
            template = GCODE_TEMPLATES[gcode]
            if gcode == "M211":  # Special case for software endstops
                state = {
                    "state": "On" if self.websocket_handler.latest_values["filament_switch_sensor filament_sensor"]["enabled"] else "Off"
                }
                return f"{template.render(**state)}\nok"
            elif gcode == "M20":  # List the files stored on the SD card
                result = await self.websocket_handler.query_file_list()
                file_list = {file["path"]: {"size": file["size"]} for file in result}
                return f"{template.render(file_list=file_list)}\nok"
            elif gcode in ("M220", "M221") and params_dict:
                return await self.websocket_handler.call_moonraker_script(request)
            return f"{template.render(**self.websocket_handler.latest_values)}\nok"

        elif handler:
            return await handler(parameters)

        # Auto-report G-codes
        elif gcode == "M154":  # Enable/disable auto-reporting of position
            self.auto_report_position = int(params_dict.get("S", 0))
        elif gcode == "M155":  # Enable/disable auto-reporting of temperature
            self.auto_report_temperature = int(params_dict.get("S", 0))
        elif gcode == "M27":  # Enable/disable auto-report print status
            self.auto_report_print_status = int(params_dict.get("S", 0))

        elif gcode == "M33":  # Mock response for SD card operations
            return f"{parameters}\nok"

        elif gcode == "G29":  # Bed Leveling (Unified)
            return await self.websocket_handler.call_moonraker_script(
                ["BED_MESH_CLEAR", f"BED_MESH_CALIBRATE {params_dict if params_dict else ''}"]
            )
        elif gcode == "M112":  # Bed Leveling (Unified)
            await self.websocket_handler.call_moonraker_script("M112")
            return f"{{Error:Emergency Stop"

        # Special G-codes with parameter-specific behavior
        elif gcode in ("M201", "M203", "M206"):
            if gcode == "M201" and any(key in params_dict for key in ("X", "Y")):  # Set maximum acceleration
                acceleration = int(params_dict.get('X', params_dict.get('Y', 0)))
                command = f"SET_VELOCITY_LIMIT ACCEL={acceleration} ACCEL_TO_DECEL={acceleration / 2}"
            elif gcode == "M203" and any(key in params_dict for key in ("X", "Y")):  # Set maximum feedrate
                velocity = int(params_dict.get('X', params_dict.get('Y', 0)))
                command = f"SET_VELOCITY_LIMIT VELOCITY={velocity}"
            elif gcode == "M206" and any(key in params_dict for key in ("X", "Y", "Z", "E")):  # Set home offset
                offsets = " ".join(f"{axis}={value}" for axis, value in params_dict.items() if axis in ("X", "Y", "Z", "E"))
                command = f"SET_GCODE_OFFSET {offsets}"
            return await self.websocket_handler.call_moonraker_script(command)

        elif gcode == "M150":  # Set LED color
            return await self.set_led_color(params_dict)
        elif gcode in ("M701", "M702"):  # Filament load/unload
            length = 25
            params_dict = {
                'L': length if gcode == "M701" else -1 * length,
                'T': params_dict.get("T", 0),
                'Z': params_dict.get("Z", 0)
            }
            return await self.handle_filament(params_dict)
        elif gcode == "M118":  # Display message
            # TODO: Complete rest of options
            if request == "M118 P0 A1 action:cancel":
                return "//action:cancel"
            else:
                return await self.websocket_handler.call_moonraker_script(request)

        elif gcode == "M280":  # Servo Position
            servo_id = int(params_dict.get('P', 0))  # Default to 0 if 'P' is missing
            position = int(params_dict.get('S', 0))  # Default to 0 if 'S' is missing
            bltouch = True # TODO: if configfile.settings.bltouch
            # TODO: // Unknown command:"M48"

            if position == 120: # Test
                await self.websocket_handler.call_moonraker_script("QUERY_PROBE")
                return f"{PROBE_TEST_TEMPLATE.render(**self.websocket_handler.latest_values)}\nok"
            else:
                if bltouch:
                    if position == 10: # Deploy
                        command = "BLTOUCH_DEBUG COMMAND=pin_down"
                    elif position == 90: # Stow
                        command = "BLTOUCH_DEBUG COMMAND=pin_up"
                    elif position == 160:  # Reset
                        command = "BLTOUCH_DEBUG COMMAND=reset"
                else:
                    if position == 10: # Deploy
                        command = "SET_PIN PIN=_probe_enable VALUE=1"
                    if position in (90, 160):
                        command = "SET_PIN PIN=_probe_enable VALUE=0"
            return await self.websocket_handler.call_moonraker_script(command)

        elif gcode == "M290":  # Babystep
            Z = params_dict.get('Z')
            return await self.websocket_handler.call_moonraker_script(
                f"SET_GCODE_OFFSET Z_ADJUST={Z}"
            )
        elif gcode == "M851":  # XYZ Probe Offset
            return f"{PROBE_OFFSET_TEMPLATE.render(**self.websocket_handler.latest_values)}\nok"

        elif gcode == "M108":  # Special empty response
            return ""
        elif gcode == "M23":   # Select an SD card file for printing
            self.selected_file = parameters
            return await self.websocket_handler.call_moonraker_script(request)
        elif gcode == "M24":   # Start/resume SD card print
            if self.websocket_handler.latest_values.get("print_stats").get("state") == "paused":
                return await self.websocket_handler.resume_print()
            elif self.websocket_handler.latest_values.get("print_stats").get("state") != "printing":
                await self.websocket_handler.call_moonraker_script('CLEAR_PAUSE')
                return await self.websocket_handler.start_print(self.selected_file)
        elif gcode == "M25":   # Pause SD card print
            if self.websocket_handler.latest_values.get("print_stats").get("state") != "printing":
                return await self.websocket_handler.pause_print()
        elif gcode == "M524":  # Cancel current print
            return await self.websocket_handler.cancel_print()
        elif gcode in {"M82"}:  # Set extruder to absolute mode
            await self.websocket_handler.call_moonraker_script(request)
            return "ok"

        # Commands with no action or immediate acknowledgment
        elif gcode in {"M22", "M92", "T0"}:  # Acknowledge with "ok"
            # M22: Release the SD card
            # M92: Set axis steps per unit
            # T0: Select tool 0
            return "ok"
        elif gcode in {"G28", "G0", "G1", "M420", "M21", "M84", "G90", "G91", "M106", "M104", "M140", "M48"}:  # Send directly to Moonraker
            # M21: Initialize the SD card
            # G90: Set to absolute positioning
            return await self.websocket_handler.call_moonraker_script(request)

        # Fallback for unknown commands
        else:
            logging.warning(f"Unknown gcode: {request}")
            return None

    async def handle_filament(self, params_dict):
        length = params_dict.get("L")
        extruder = params_dict.get("T")
        zmove = params_dict.get("Z")
        command = [
            "G91",               # Relative Positioning
            f"G92 E{extruder}",  # Reset Extruder
            f"G1 Z{zmove} E{length} F{3*60}",  # Extrude or Retract
            "G92 E0"              # Reset Extruder
        ]
        return await self.websocket_handler.call_moonraker_script(command)

    async def set_led_color(self, params_dict):
        gcode = (
            f"SET_LED LED=statusled "
            f"RED={(int(params_dict.get('R', 0)) / 255 ) * (int(params_dict.get('P', 0)) / 255 ):.3f} "
            f"GREEN={(int(params_dict.get('U', 0)) / 255 ) * (int(params_dict.get('P', 0)) / 255 ):.3f} "
            f"BLUE={(int(params_dict.get('B', 0)) / 255 ) * (int(params_dict.get('P', 0)) / 255 ):.3f} "
            f"WHITE={(int(params_dict.get('W', 0)) / 255 ) * (int(params_dict.get('P', 0)) / 255 ):.3f} "
            "TRANSMIT=1 SYNC=1" # [P=<index>]
        )
        return await self.websocket_handler.call_moonraker_script(gcode)

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
        tft_adapter.periodic_update_report(
            "auto_report_position", POSITION_TEMPLATE
        ),
        tft_adapter.periodic_update_report(
            "auto_report_temperature", TEMPERATURE_TEMPLATE, prefix_ok=True
        ),
        tft_adapter.periodic_update_report(
            "auto_report_print_status", PRINT_STATUS_TEMPLATE
        )
    )

if __name__ == "__main__":
    asyncio.run(main())
