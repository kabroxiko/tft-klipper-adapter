import asyncio
import json
import logging
import argparse
from queue import Queue
from websockets import connect
import serial
import re

# Global response formats
MACHINE_TYPE = "Artillery Genius Pro"
TEMPERATURE_FORMAT = (
    "T:{extruder_temperature:.2f} /{extruder_target:.2f} "
    "B:{bed_temperature:.2f} /{bed_target:.2f} "
    "@:0 B@:0"
)
POSITION_FORMAT = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f}"
FEED_RATE_FORMAT = "FR:{fr:.2f}%"
FLOW_RATE_FORMAT = "E0 Flow: {er:.2f}%"
REPORT_SETTINGS_FORMAT = (
    "M203 X{toolhead_max_velocity} Y{toolhead_max_velocity} Z{max_z_velocity} E{max_extrude_only_velocity}\n"
    "M201 X{toolhead_max_accel} Y{toolhead_max_accel} Z{max_z_accel} E{max_extrude_only_accel}\n"
    "M206 X{homing_origin_x} Y{homing_origin_y} Z{homing_origin_z}\n"
    "M851 X{x_offset} Y{y_offset} Z{z_offset}\n"
    "M420 S1 Z{bed_mesh_fade_end}\n"
    "M106 S{fan_speed}"
)
SOFTWARE_ENDSTOPS_FORMAT = "Soft endstops: {state}"
FIRMWARE_INFO_FORMAT = (
    "FIRMWARE_NAME:Klipper {mcu_version} "
    "SOURCE_CODE_URL:https://github.com/Klipper3d/klipper "
    "PROTOCOL_VERSION:1.0 "
    "MACHINE_TYPE: {machine_type}\n"
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

TRACKED_OBJECTS = {
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
    "gcode_move": ["position", "homing_origin", "speed_factor", "extrude_factor"],
    "toolhead": ["max_velocity", "max_accel"],
    "mcu": ["mcu_version"],
    "configfile": ["settings"],
    "fan": ["speed"]
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
        self.latest_values = {}  # Start with an empty dictionary
        self.file_list = []  # Cache of the file list

    async def handler(self, websocket):
        await self.subscribe_to_printer_objects(websocket)
        while True:
            try:
                message = await websocket.recv()
                self.handle_message(message)
            except Exception as e:
                logging.error(f"Error in WebSocket handler: {e}")

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
                "objects": OBJECTS
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
                if response != "";
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
        parts = request.split()
        gcode = parts[0]
        parameters = " ".join(request.split()[1:]) if len(parts) > 1 else None

        if gcode == "M211":
            return f"{self.get_software_endstops()}\nok"
        elif gcode == "M115":
            return f"{self.get_firmware_info()}\nok"
        elif gcode == "M503":
            return f"{self.get_report_settings()}\nok"
        elif gcode == "M154":
            parts = request.split()
            self.auto_report_position = int(parameters[1:]) if len(parts) > 1 else None
        elif gcode == "M155":
            parts = request.split()
            self.auto_report_temperature = int(parameters[1:]) if len(parts) > 1 else None
        elif gcode == "M105":
            return f"{self.get_temperature()}\nok"
        elif gcode == "M114":
            return f"{self.get_position()}\nok"
        elif request == "M220":
            return f"{self.get_feed_rate(gcode)}\nok"
        elif request == "M221":
            return f"{self.get_flow_rate(gcode)}\nok"
        elif gcode == "M20":
            return self.get_file_list()
        elif gcode == "M33":
            return f"{request.split(' ')[1]}\nok"
        elif gcode == "M201":
            if parameters.startswith(("X", "Y")):
                max_acceleration = parameters[1:]
                return await self.websocket_handler.send_gcode_and_wait(
                    f"SET_VELOCITY_LIMIT "
                    f"ACCEL={max_acceleration} "
                    f"ACCEL_TO_DECEL={int(max_acceleration) / 2}"
                )
            elif parameters.startswith(("Z", "E")):
                pass # Can't be configured dynamically
        elif gcode == "M203":
            if parameters.startswith(("X", "Y")):
                max_velocity = parameters[1:]
                return await self.websocket_handler.send_gcode_and_wait(
                    f"SET_VELOCITY_LIMIT VELOCITY={max_velocity}"
                )
            elif parameters.startswith(("Z", "E")):
                pass # Can't be configured dynamically
        elif gcode == "M206":
            if parameters.startswith(("X", "Y", "Z", "E")):
                return await self.websocket_handler.send_gcode_and_wait(
                    f"SET_GCODE_OFFSET {parameters[:1]}={parameters[1:]}"
                )
        elif gcode == "M150":
            return await self.set_led_color(parameters)
        elif gcode == "M524":
            return await self.websocket_handler.send_gcode_and_wait("CANCEL_PRINT")
        elif gcode == "M701":
            return await self.load()
        elif gcode == "M702":
            return await self.websocket_handler.send_gcode_and_wait("CANCEL_PRINT")
        elif gcode == "M118":
            return ""
        #     # TODO
        #     parts = parameters.split()

        #     return await self.websocket_handler.send_gcode_and_wait("CANCEL_PRINT")
        #     return await self.websocket_handler.send_gcode_and_wait(request)
        elif gcode in ("M851", "M420", "M22"):
            # Unknown commands
            return "ok"
        elif gcode in ("M108"):
            # Ignored command
            return ""
        elif gcode in ("M92", "T0"):
            return "ok"
        else:
            return await self.websocket_handler.send_gcode_and_wait(request)
        return None

    async def load(self, parameters="L25 T0 Z0"):
        pattern = re.compile(r'([LTZ])(\d+)')
        params = {match[0]: int(match[1]) for match in pattern.findall(parameters)}

        length = params.get("L")
        extruder = params.get("T")
        zmove = params.get("Z")
        command = (
            "G91\n"
            f"G92 E{extruder}\n"
            f"G1 Z{zmove} E{length} F{3*60}\n"
            "G92 E0\n"
        )
        print(command)
        return await self.websocket_handler.send_gcode_and_wait(command)

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

    def get_temperature(self):
        extruder = self.websocket_handler.latest_values["extruder"]
        heater_bed = self.websocket_handler.latest_values["heater_bed"]
        return TEMPERATURE_FORMAT.format(
            extruder_temperature=extruder['temperature'],
            extruder_target=extruder['target'],
            bed_temperature=heater_bed['temperature'],
            bed_target=heater_bed['target']
        )

    def get_position(self):
        position = self.websocket_handler.latest_values["gcode_move"]["position"]
        return POSITION_FORMAT.format(
            x=position[0],
            y=position[1],
            z=position[2],
            e=position[3]
        )

    def get_feed_rate(self, gcode):
        flow_rate = self.websocket_handler.latest_values["gcode_move"]["speed_factor"] * 100
        return FEED_RATE_FORMAT.format(fr=flow_rate)

    def get_flow_rate(self, gcode):
        extrude_rate = self.websocket_handler.latest_values["gcode_move"]["extrude_factor"] * 100
        return FLOW_RATE_FORMAT.format(er=extrude_rate)

    def get_report_settings(self):
        # Access the latest values from the WebSocket
        toolhead = self.websocket_handler.latest_values["toolhead"]
        config = self.websocket_handler.latest_values["configfile"]["settings"]
        gcode_move = self.websocket_handler.latest_values["gcode_move"]
        fan = self.websocket_handler.latest_values["fan"]

        # Format the M503 response using the global variables
        return REPORT_SETTINGS_FORMAT.format(
            toolhead_max_velocity=toolhead["max_velocity"],
            max_z_velocity=config["printer"]["max_z_velocity"],
            max_extrude_only_velocity=config["extruder"]["max_extrude_only_velocity"],
            toolhead_max_accel=toolhead["max_accel"],
            max_z_accel=config["printer"]["max_z_accel"],
            max_extrude_only_accel=config["extruder"]["max_extrude_only_accel"],
            homing_origin_x=gcode_move["homing_origin"][0],
            homing_origin_y=gcode_move["homing_origin"][1],
            homing_origin_z=gcode_move["homing_origin"][2],
            x_offset=config.get('bltouch', config.get('probe'))["x_offset"],
            y_offset=config.get('bltouch', config.get('probe'))["y_offset"],
            z_offset=config.get('bltouch', config.get('probe'))["z_offset"],
            bed_mesh_fade_end=config["bed_mesh"]["fade_end"],
            fan_speed=fan["speed"] * 255.0  # Convert fan speed to PWM value
        )

    def get_firmware_info(self):
        mcu_version = self.websocket_handler.latest_values["mcu"]["mcu_version"]
        return FIRMWARE_INFO_FORMAT.format(
            mcu_version=mcu_version,
            machine_type=MACHINE_TYPE
        )

    def get_software_endstops(self):
        state = "On"  # Replace with actual logic to determine soft endstops state
        return SOFTWARE_ENDSTOPS_FORMAT.format(state=state)

    def get_file_list(self):
        """Format the file list as required for M20."""
        file_list_str = "Begin file list\n"
        for file in self.websocket_handler.file_list:
            filename = file.get("path")
            size = file.get("size", 0)
            file_list_str += f"{filename} {size}\n"
        file_list_str += "End file list\nok"
        return file_list_str

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
            tft_adapter.periodic_temperature_update(),
            tft_adapter.periodic_position_update()
        )

if __name__ == "__main__":
    asyncio.run(main())