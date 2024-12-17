import asyncio
import serial
import json
import logging
import argparse
from asyncio import Queue
import websockets

# Response formats
M105_RESPONSE_FORMAT = "ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0"
M114_RESPONSE_FORMAT = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok"
M220_RESPONSE_FORMAT = "FR:{fr:}%\nok"
M221_RESPONSE_FORMAT = "E0 Flow: {er:}%\nok"
M503_RESPONSE_FORMAT = (
    "M203 X{printer.toolhead.max_velocity} Y{printer.toolhead.max_velocity} "
    "Z{printer.configfile.settings.printer.max_z_velocity} "
    "E{printer.configfile.settings.extruder.max_extrude_only_velocity}\n"
    "M201 X{printer.toolhead.max_accel} Y{printer.toolhead.max_accel} "
    "Z{printer.configfile.settings.printer.max_z_accel} "
    "E{printer.configfile.settings.extruder.max_extrude_only_accel}\n"
    "M206 X{printer.gcode_move.homing_origin.x} "
    "Y{printer.gcode_move.homing_origin.y} "
    "Z{printer.gcode_move.homing_origin.z}\n"
    "_PROBE_OFFSET_REPORT\n"
    "M420 S1 Z{printer.configfile.settings.bed_mesh.fade_end | default(0)}\n"
    "M106 S{(printer.fan.speed | float * 255.0) | float}\n"
)

M92_RESPONSE_FORMAT = "M92 X{x:.2f} Y{y:.2f} Z{z:.2f} E{e:.2f}\nok"
M211_RESPONSE_FORMAT = "Soft Endstops: {state}\nok"
G28_RESPONSE_FORMAT = "ok Homing completed"

# White-listed G-codes
MARLIN_COMPATIBLE_GCODES = {"M105", "M114", "M220", "M221", "M503", "M92", "M211", "G28"}


class SerialHandler:
    def __init__(self, port, baud_rate):
        self.port = port
        self.baud_rate = baud_rate
        self.serial_conn = None

    def initialize(self):
        self.serial_conn = serial.Serial(self.port, self.baud_rate, timeout=0.1)
        logging.info(f"Initialized serial connection on {self.port} at {self.baud_rate} baud.")

    def read_line(self):
        if self.serial_conn and self.serial_conn.in_waiting > 0:
            return self.serial_conn.readline().decode("utf-8").strip()
        return None

    def write_line(self, line):
        if self.serial_conn:
            self.serial_conn.write((line + "\n").encode("utf-8"))
            logging.info(f"Sent response back to TFT: {line}")


class WebSocketHandler:
    def __init__(self, websocket_url, message_queue):
        self.websocket_url = websocket_url
        self.message_queue = message_queue
        self.latest_values = {}

    async def handler(self):
        async with websockets.connect(self.websocket_url) as websocket:
            await self.initialize_latest_values(websocket)
            subscription_request = {
                "jsonrpc": "2.0",
                "method": "printer.objects.subscribe",
                "params": {
                    "objects": {
                        "extruder": None,
                        "heater_bed": None,
                        "gcode_move": None,
                        "toolhead": None,
                        "fan": None,
                        "configfile": None,
                    }
                },
                "id": 5432,
            }
            await websocket.send(json.dumps(subscription_request))
            while True:
                response = await websocket.recv()
                await self.message_queue.put(response)

    async def initialize_latest_values(self, websocket):
        query_request = {
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": {
                    "extruder": None,
                    "heater_bed": None,
                    "gcode_move": None,
                    "toolhead": None,
                    "fan": None,
                    "configfile": None,
                }
            },
            "id": 1234,
        }
        await websocket.send(json.dumps(query_request))
        response = await websocket.recv()
        self.latest_values = json.loads(response).get("result", {}).get("status", {})
        logging.info(f"Initialized latest values: {self.latest_values}")


class TFTAdapter:
    def __init__(self, serial_handler, websocket_handler):
        self.serial_handler = serial_handler
        self.websocket_handler = websocket_handler

    async def serial_reader(self):
        while True:
            gcode = self.serial_handler.read_line()
            if gcode:
                logging.info(f"Received G-code from serial: {gcode}")
                await self.process_gcode(gcode)
            await asyncio.sleep(0.1)

    async def process_gcode(self, gcode):
        if gcode in MARLIN_COMPATIBLE_GCODES:
            if gcode == "M105":
                response = self.format_m105_response()
            elif gcode == "M114":
                response = self.format_m114_response()
            elif gcode == "M220":
                response = self.format_m220_response()
            elif gcode == "M221":
                response = self.format_m221_response()
            elif gcode == "M503":
                response = self.format_m503_response()
            elif gcode == "M92":
                response = self.format_m92_response()
            elif gcode == "M211":
                response = self.format_m211_response()
            elif gcode == "G28":
                response = self.process_g28_command()
            else:
                response = "ok"
            self.serial_handler.write_line(response)
        else:
            logging.warning(f"Unsupported G-code: {gcode}")

    def format_m105_response(self):
        extruder = self.websocket_handler.latest_values["extruder"]
        heater_bed = self.websocket_handler.latest_values["heater_bed"]
        return M105_RESPONSE_FORMAT.format(
            ETemp=extruder["temperature"],
            ETarget=extruder["target"],
            BTemp=heater_bed["temperature"],
            BTarget=heater_bed["target"],
        )

    def format_m503_response(self):
        return M503_RESPONSE_FORMAT.format(**self.websocket_handler.latest_values)

    def format_m114_response(self):
        gcode_move = self.websocket_handler.latest_values["gcode_move"]
        return M114_RESPONSE_FORMAT.format(
            x=gcode_move["position"]["x"],
            y=gcode_move["position"]["y"],
            z=gcode_move["position"]["z"],
            e=gcode_move["position"]["e"],
        )

    def format_m220_response(self):
        toolhead = self.websocket_handler.latest_values["toolhead"]
        return M220_RESPONSE_FORMAT.format(fr=toolhead["speed_factor"])

    def format_m221_response(self):
        extruder = self.websocket_handler.latest_values["extruder"]
        return M221_RESPONSE_FORMAT.format(er=extruder["flow_factor"])

    def format_m92_response(self):
        steps = self.websocket_handler.latest_values["configfile"]["settings"]["stepper"]
        return M92_RESPONSE_FORMAT.format(
            x=steps["x"],
            y=steps["y"],
            z=steps["z"],
            e=steps["e"],
        )

    def format_m211_response(self):
        return M211_RESPONSE_FORMAT.format(state="On" if self.websocket_handler.latest_values.get("soft_endstops", True) else "Off")

    def process_g28_command(self):
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
    )

if __name__ == "__main__":
    asyncio.run(main())
