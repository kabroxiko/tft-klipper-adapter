import atexit
import sys
import json
import time
import threading
import logging
import socket
import errno
import websockets
import asyncio
import serial
import traceback
import argparse

class TFTAdapter:
    def __init__(self):
        self.heater_bed = {"temperature": 0, "target": 0}
        self.extruder = {"temperature": 0, "target": 0}
        self.gcode_position = [0, 0, 0, 0]
        self.extrude_factor = 0
        self.speed_factor = 0

        self.temperature_tmpl = "ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0\n"
        self.position_tmpl = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
        self.feed_rate_tmpl = "FR:{fr:}%\nok\n"
        self.flow_rate_tmpl = "E0 Flow: {er:}%\nok\n"
        self.firmware_info = ""

        self.tft_serial = serial.Serial(args.tft_device, args.tft_baud)
        self.autotemperature = "off"

        self.lock = threading.Lock()

        self.standard_gcodes = [
            "M104", # Set Hotend Temperature
            "M140", # Set Bed Temperature
            "M106", # Set Fan Speed
            "M84",  # Disable steppers
            "G90",  # Absolute Positioning
            "G91",  # Relative Positioning
            "G0"    # Linear Move
        ]
        atexit.register(self.exit_handler)

        self.ws_uri = "%s/websocket?token=" % (args.moonraker_uri)

        #
        #create and start threads
        #
        threading.Thread(target=self.start_socket).start()
        threading.Thread(target=self.start_serial).start()

    def write_to_serial(self, message):
        if self.tft_serial.is_open:
            self.lock.acquire()
            try:
                for data in message.splitlines():
                    logging.info("message: %s" % data)
                self.tft_serial.write(bytes(message, encoding='utf-8'))
            finally:
                self.lock.release()
        else:
            logging.error("serial port is not open")

    def _on_close(self, ws, close_status, close_msg):
        logging.info("Reconnecting websocket")
        time.sleep(10)
        self.start_socket()

    def _on_error(self, ws, error):
        traceback.print_exc()

    def _on_message(self, message):
        status = None
        logging.debug("message: %s" % message)
        if "method" in message and message['method'] == "notify_status_update":
            status = message["params"][0]
            self.set_status(status)

    def set_status(self, status):
        if 'heater_bed' in status:
            if "temperature" in status['heater_bed']:
                self.heater_bed["temperature"] = status['heater_bed']["temperature"]
            if "target" in status['heater_bed']:
                self.heater_bed["target"] = status['heater_bed']["target"]
        if 'extruder' in status:
            if "temperature" in status['extruder']:
                self.extruder["temperature"] = status['extruder']["temperature"]
            if "target" in status['extruder']:
                self.extruder["target"] = status['extruder']["target"]
        if 'gcode_move' in status:
            gcode_move = status['gcode_move']
            if "gcode_position" in status['gcode_move']:
                self.gcode_position = gcode_move['gcode_position']
            if "speed_factor" in status['gcode_move']:
                self.speed_factor = gcode_move['speed_factor']
            if "extrude_factor" in status['gcode_move']:
                self.extrude_factor = gcode_move['extrude_factor']

        logging.debug("heater_bed: %s" % self.heater_bed)
        logging.debug("extruder: %s" % self.extruder)
        logging.debug("gcode_position: %s" % self.gcode_position)
        logging.debug("speed_factor: %s" % self.speed_factor)
        logging.debug("extrude_factor: %s" % self.extrude_factor)

    def start_socket(self):
        self.query_status()
        self.auto_get_temperature()
        self.query_firmware_info()
        # self.get_subscriptions()
        asyncio.run(self.listen())

    def websocket_send(self, query):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(self.send_receive_message(self.ws_uri, query))
        loop.close()
        logging.debug("result: %s" % result)
        return result

    def _unsubscribe_all(self):
        data = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": { },
            },
            "id": 4654
        }
        self.ws.send(json.dumps(data))

    # def _add_subscription(self):
    #     data = {
    #         "jsonrpc": "2.0",
    #         "method": "printer.objects.subscribe",
    #         "params": {
    #             "objects": {
    #                 "extruder": None,
    #                 "heater_bed": None,
    #                 "gcode_move": None
    #             }
    #         },
    #         "id": 5434
    #     }
    #     self.ws.send(json.dumps(data))

    def query_status(self):
        # Query Status
        query = {
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": {
                    "extruder": None,
                    "heater_bed": None,
                    "gcode_move": None
                }
            },
            "id": 1234
        }
        response = self.websocket_send(query)
        logging.debug("response: %s" % response)
        status = response["status"]
        self.set_status(status)

    def get_temperature(self):
        message = self.temperature_tmpl.format(
            ETemp   = self.extruder['temperature'],
            ETarget = self.extruder['target'],
            BTemp   = self.heater_bed['temperature'],
            BTarget = self.heater_bed['target']
        )
        self.write_to_serial(message)

    def auto_get_temperature(self):
        self.autotemperature = "on"
        refresh_time = 3
        threading.Timer(refresh_time, self.auto_get_temperature).start()
        self.get_temperature()

    def get_firmware_info(self, software_version):
        message = "FIRMWARE_NAME:Klipper %s" % (software_version)
        message = "%s SOURCE_CODE_URL:https://github.com/Klipper3d/klipper" % (message)
        message = "%s PROTOCOL_VERSION:1.0" % (message)
        message = "%s MACHINE_TYPE:Artillery Genius Pro\n" % (message)
        message = "%sCap:EEPROM:1\n" % (message)
        message = "%sCap:AUTOREPORT_TEMP:1\n" % (message)
        message = "%sCap:AUTOREPORT_POS:1\n" % (message)
        message = "%sCap:AUTOLEVEL:1\n" % (message)
        message = "%sCap:Z_PROBE:1\n" % (message)
        message = "%sCap:LEVELING_DATA:0\n" % (message)
        message = "%sCap:SOFTWARE_POWER:0\n" % (message)
        message = "%sCap:TOGGLE_LIGHTS:0\n" % (message)
        message = "%sCap:CASE_LIGHT_BRIGHTNESS:0\n" % (message)
        message = "%sCap:EMERGENCY_PARSER:1\n" % (message)
        message = "%sCap:PROMPT_SUPPORT:0\n" % (message)
        message = "%sCap:SDCARD:1\n" % (message)
        message = "%sCap:MULTI_VOLUME:0\n" % (message)
        message = "%sCap:AUTOREPORT_SD_STATUS:1\n" % (message)
        message = "%sCap:LONG_FILENAME:1\n" % (message)
        message = "%sCap:BABYSTEPPING:1\n" % (message)
        message = "%sCap:BUILD_PERCENT:1\n" % (message)  # M73 support
        message = "%sCap:CHAMBER_TEMPERATURE:0\n" % (message)
        return message

    def query_firmware_info(self):
        # Query firmware Info
        id = 5153
        query = {
            "jsonrpc": "2.0",
            "method": "printer.info",
            "id": id
        }
        response = self.websocket_send(query)
        self.firmware_info = self.get_firmware_info(response["software_version"])

    def get_report_settings(self, status):
        bltouch = None
        settings = status["configfile"]["settings"]
        toolhead = status["toolhead"]
        gcode_move = status["gcode_move"]
        extruder = settings["extruder"]
        printer = settings["printer"]
        bltouch = settings["bltouch"]
        # Max feedrates (units/s):
        message = "M203 X%s Y%s Z%s E%s\n" % (
            toolhead["max_velocity"],
            toolhead["max_velocity"],
            printer["max_z_velocity"],
            extruder["max_extrude_only_velocity"]
        )
        # Max Acceleration (units/s2):
        message = "%sM201 X%s Y%s Z%s E%s\n" % (
            message,
            toolhead["max_accel"],
            toolhead["max_accel"],
            printer["max_z_accel"],
            extruder["max_extrude_only_accel"]
        )
        # Home offset
        message = "%sM206 X%s Y%s Z%s\n" % (
            message,
            gcode_move["homing_origin"][0],
            gcode_move["homing_origin"][1],
            gcode_move["homing_origin"][2]
        )
        # Z-Probe Offset
        if bltouch is not None:
            message = "%sM851 X%s Y%s Z%s\n" % (
                message,
                bltouch["x_offset"],
                bltouch["y_offset"],
                bltouch["z_offset"]
            )
        # TODO: Respuesta en caso de no tener bltouch
        # else:
        #     message = "%sM851 X%s Y%s Z%s\n" % (
        #         message,
        #         probe["x_offset"],
        #         probe["y_offset"],
        #         probe["z_offset"]
        #     )
        # Auto Bed Leveling
        message = "%sM420 S1 Z%s\n" % (message, settings["bed_mesh"]["fade_end"])
        # Fan Speed
        message = "%sM106 S%s\n" % (message, status["fan"]["speed"])
        logging.info("message: %s" % message)
        return message

    def query_report_settings(self):
        # Query Report Settings
        id = 6726
        query = {
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": {
                    "configfile": ["settings"],
                    "toolhead": None,
                    "gcode_move": ["homing_origin"],
                    "fan": ["speed"]
                }
            },
            "id": id
        }
        response = self.websocket_send(query)
        report_settings = self.get_report_settings(response["status"])
        self.write_to_serial(report_settings)

    def send_current_position(self):
        message = self.position_tmpl.format(
            x=self.gcode_position[0],
            y=self.gcode_position[1],
            z=self.gcode_position[2],
            e=self.gcode_position[3]
        )
        self.write_to_serial(message)

    def send_speed_factor(self):
        message = self.feed_rate_tmpl.format(fr=self.speed_factor*100)
        self.write_to_serial(message)

    def send_extrude_factor(self):
        message = self.flow_rate_tmpl.format(er=self.extrude_factor*100)
        self.write_to_serial(message)

    def send_gcode_to_api(self, gcode):
        query = {
            "jsonrpc": "2.0",
            "method": "printer.gcode.script",
            "params": {
                "script": gcode
            },
            "id": 4758
        }
        response = self.websocket_send(query)
        logging.info("Response to gcode %s: %s" % (gcode.replace('\n',''), response))
        return response

    def get_sd_files(self):
        query = {
            "jsonrpc": "2.0",
            "method": "server.files.list",
            "params": {
                "root": "gcodes"
            },
            "id": 4644
        }
        response = self.websocket_send(query)
        message = "Begin file list\n"
        message = "%scase_1.gcode 4139710\n" % message
        message = "%sEnd file list\n" % message
        message = "%sok\n" % message
        logging.info("Response: %s" % message)
        return message

    # def get_subscriptions(self):
    #     query = {
    #         "jsonrpc": "2.0",
    #         "method": "server.announcements.list",
    #         "params": {
    #             "include_dismissed": "false"
    #         },
    #         "id": 4654
    #     }
    #     response = self.websocket_send(query)
    #     logging.info("Response: %s" % response)

    def get_file_metadata(self, filename):
        logging.info("filename: <%s>" % filename)
        query = {
            "jsonrpc": "2.0",
            "method": "server.files.metadata",
            "params": {
                "filename": filename
            },
            "id": 3545
        }
        response = self.websocket_send(query)
        logging.info("Response: %s" % response)
        message = "File opened:%s Size:%s\n" % (response["filename"], response["size"])
        message = "%sFile selected\n" % message
        message = "%sok\n" % message
        logging.info("Response: %s" % message)
        return message

    def is_standard_gcode(self, gcode):
        for g in self.standard_gcodes:
            if g in gcode.capitalize():
                return True
        return False

    def exit_handler(self):
        if self.tft_serial.is_open:
            self.tft_serial.close()
        logging.debug("Serial closed")

    async def listen(self):
        async with websockets.connect(self.ws_uri) as webservice:
            try:
                data = {
                    "jsonrpc": "2.0",
                    "method": "printer.objects.subscribe",
                    "params": {
                        "objects": {
                            "extruder": None,
                            "heater_bed": None,
                            "gcode_move": None
                        }
                    },
                    "id": 5434
                }
                await webservice.send(json.dumps(data))
            except ConnectionResetError:
                logging.error("ConnectionResetError, reconnecting...")

            while True:
                msg = await webservice.recv()
                msg = json.loads(msg)
                self._on_message(msg)

    async def send_receive_message(self, uri, query):
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps(query))
            response = None
            result = None
            while response is None or response.get("id", -1) != query["id"]:
                reply = await websocket.recv()
                logging.debug("reply: %s" % reply)
                response = json.loads(reply)

            if response.get("result") is not None:
                result = response.get("result")
            elif response.get("error") is not None:
                result = "Error:%s\n" % response.get("error").get("message").replace("\n", " ")

            logging.debug("result: %s" % result)
            return result

    def start_serial(self):
        while True:
            try:
                gcode = self.tft_serial.readline().decode("utf-8")
                logging.info("gcode: %s" % gcode.replace('\n',''))
                if self.is_standard_gcode(gcode):
                    # Standard Mxxx gcode
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M105" in gcode.capitalize():
                    # Report Temperatures
                    if self.autotemperature != "on":
                        self.get_temperature()
                elif "M92" in gcode.capitalize():
                    # Set Axis Steps-per-unit (not implemented)
                    self.write_to_serial("ok\n")
                elif "M82" in gcode.capitalize():
                    # E Absolute
                    pass
                elif "M211" in gcode.capitalize():
                    # Software Endstops
                    self.write_to_serial("ok\n")
                elif "M154" in gcode.capitalize():
                    # Position Auto-Report
                    pass
                elif "M420" in gcode.capitalize():
                    # Bed Leveling State
                    pass
                elif "M503" in gcode.capitalize():
                    # Report Settings
                    self.query_report_settings()
                elif "M155" in gcode.capitalize():
                    if self.autotemperature != "on":
                        self.auto_get_temperature()
                    self.write_to_serial("ok\n")
                elif "M115" in gcode.capitalize():
                    # Firmware Info
                    self.write_to_serial(self.firmware_info)
                elif "M114" in gcode.capitalize():
                    # Get Current Position:
                    self.send_current_position()
                elif "M21" in gcode.capitalize():
                    # Init SD card
                    self.send_gcode_to_api(gcode)
                    self.write_to_serial("SD card ok\n")
                elif "M20" in gcode.upper():
                    # List SD Card
                    response = self.get_sd_files()
                    self.write_to_serial(response)
                elif "M33" in gcode.capitalize():
                    # Get Long Path
                    filename = gcode.split(" ")[1]
                    self.write_to_serial("%s\n" % filename)
                elif "M23" in gcode.capitalize():
                    # Select SD file
                    self.send_gcode_to_api(gcode)
                    filename = gcode.split("/")[1]
                    response = self.get_file_metadata(filename.replace('\n',''))
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M27" in gcode.capitalize():
                    # Report SD print status
                    # TODO: agregar manejo de M27 S3
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M24" in gcode.capitalize():
                    # Start or Resume SD print
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M25" in gcode.capitalize():
                    # Pause SD print
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M524" in gcode.capitalize():
                    # Abort SD print
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M118 P0 A1 action:cancel" in gcode.capitalize():
                    # Serial print
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "M108" in gcode.capitalize():
                    # Break and Continue
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "G28" in gcode.capitalize():
                    # Auto Home
                    response = self.send_gcode_to_api(gcode)
                    logging.debug("response: %s" % response)
                    self.write_to_serial(response)
                elif "G1" in gcode.capitalize():
                    # Linear Move
                    self.send_gcode_to_api("G91")
                    self.send_gcode_to_api(gcode)
                    self.send_gcode_to_api("G90")
                    self.write_to_serial("ok\n")
                    # self.write_to_serial(self.send_current_position())
                elif "M220" in gcode.capitalize():
                    if "M220 S" in gcode.upper():
                        # Set Feedrate Percentage
                        self.send_gcode_to_api(gcode)
                        self.write_to_serial("ok\n")
                    else:
                        # Get Feedrate Percentage
                        self.send_speed_factor()
                elif "M221" in gcode.capitalize():
                    if "M221 S" in gcode.upper():
                        # Set Flow Percentage
                        self.send_gcode_to_api(gcode)
                        self.write_to_serial("ok\n")
                    else:
                        # Get Flow Percentage
                        self.send_extrude_factor()
                else:
                    logging.warning("unknown command")
            except Exception as ex:
                # traceback.print_exc()
                logging.error("Serial Error: %s" % ex)


parser = argparse.ArgumentParser()
parser.add_argument('-t', '--tft_device', help='tty device', required=True)
parser.add_argument('-b', '--tft_baud', help='bauds', default="115200")
parser.add_argument('-u', '--moonraker_uri', help='moonraket api uri', default="ws://127.0.0.1:7125")
parser.add_argument('-l', '--logfile', help='write log to file instead of stderr')
parser.add_argument('-v', '--verbose', help='debug mode', action="store_true")
args = parser.parse_args()

handler = logging.FileHandler('my_log_info.log')

logging.basicConfig(handlers = [
                            logging.FileHandler(args.logfile)
                                if args.logfile
                                else logging.StreamHandler(stream=sys.stdout)
                        ],
                    encoding='utf-8',
                    format='%(message)s',
                    level=logging.DEBUG if args.verbose else logging.INFO)

TFTAdapter()
