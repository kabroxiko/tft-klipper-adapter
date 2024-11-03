import atexit
import sys
import json
import time
import threading
import logging
import socket
import errno
import websocket
import serial
import traceback

class TFTAdapter:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        tft_device = config.get('tft_device')
        tft_baud = config.getint('tft_baud')
        moonraker_url = config.get('moonraker_url')
        self.moonraker_socket_path = "/home/pi/printer_data/comms/moonraker.sock"

        # self.gcode_url_template = moonraker_url+"/printer/gcode/script?script={g:s}"

        self.heater_bed = {"temperature": 0, "target": 0}
        self.extruder = {"temperature": 0, "target": 0}
        self.gcode_position = [0, 0, 0, 0]
        self.extrude_factor = 0
        self.speed_factor = 0

        self.temp_template = "ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0\n"
        self.position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
        self.feed_rate_template = "FR:{fr:}%\nok\n"
        self.flow_rate_template = "E0 Flow: {er:}%\nok\n"

        self.tft_serial = serial.Serial(tft_device, tft_baud)

        self.lock = threading.Lock()

        self.acceptable_gcode = ["M104", "M140", "M106", "M84"]

        atexit.register(self.exit_handler)

        self.ws_url = "%s/websocket?token=" % (moonraker_url)

        #
        #create and start threads
        #
        threading.Thread(target=self.start_socket).start()
        threading.Thread(target=self.start_serial).start()

    def _on_close(self, ws, close_status, close_msg):
        logging.info("Reconnecting websocket")
        time.sleep(10)
        self.start_socket()

    def _on_error(self, ws, error):
        traceback.print_exc()

    def _on_message(self, ws, msg):
        response = json.loads(msg)
        status = None
        if 'id' in response:
            if response["id"] == 5153:
                logging.debug(response)
                self.send_firmware_info(response["result"]["software_version"])
            elif response["id"] == 6726:
                self.send_report_settings(response)
            elif response["id"] == 1234:
                status = response["result"]["status"]
                self.add_subscription()

        elif "method" in response and response['method'] == "notify_status_update":
            status = response["params"][0]

        if status is not None:
            if 'heater_bed' in status:
                self.heater_bed["temperature"] = status['heater_bed']["temperature"]
                if "target" in status['heater_bed']:
                    self.heater_bed["target"] = status['heater_bed']["target"]
            if 'extruder' in status:
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

    def _on_open(self, ws):
        self.query_status()
        self.query_firmware_info()

    def unsubscribe_all(self):
        data = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {
                "objects": { },
            },
            "id": 4654
        }
        self.ws.send(json.dumps(data))

    def add_subscription(self):
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
        self.ws.send(json.dumps(data))

    def query_status(self):
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
        self.ws.send(json.dumps(query))

    def write_to_serial(self, data):
        if self.tft_serial.is_open:
            self.lock.acquire()
            try:
                self.tft_serial.write(bytes(data, encoding='utf-8'))
            finally:
                self.lock.release()
        else:
            logging.error("serial port is not open")

    def get_temperature(self):
        message = self.temp_template.format(
            ETemp   = self.extruder['temperature'],
            ETarget = self.extruder['target'],
            BTemp   = self.heater_bed['temperature'],
            BTarget = self.heater_bed['target']
        )
        logging.debug(message)
        return message

    def query_firmware_info(self):
        id = 5153
        query = {
            "jsonrpc": "2.0",
            "method": "printer.info",
            "id": id
        }
        self.ws.send(json.dumps(query))

    def send_firmware_info(self, software_version):
        message = "FIRMWARE_NAME:Klipper %s" % (software_version)
        message = "%s SOURCE_CODE_URL:https://github.com/Klipper3d/klipper" % (message)
        message = "%s PROTOCOL_VERSION:1.0" % (message)
        message = "%s MACHINE_TYPE:Artillery Genius Pro\n" % (message)
        message = "%sCap:EEPROM:1\n" % (message)
        message = "%sCap:AUTOREPORT_TEMP:0\n" % (message)
        message = "%sCap:AUTOREPORT_POS:0\n" % (message)
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
        self.write_to_serial(message)

    def query_report_settings(self):
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
        self.ws.send(json.dumps(query))

    def send_report_settings(self, response):
        bltouch = None
        status = response["result"]["status"]
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
        logging.info(message)
        self.write_to_serial(message)

    def get_current_position(self):
        message = self.position_template.format(
            x=self.gcode_position[0],
            y=self.gcode_position[1],
            z=self.gcode_position[2],
            e=self.gcode_position[3]
        )
        logging.debug(message)
        return message

    def get_speed_factor(self):
        message = self.feed_rate_template.format(fr=self.speed_factor*100)
        logging.debug(message)
        return message

    def get_extrude_factor(self):
        message = self.flow_rate_template.format(er=self.extrude_factor*100)
        logging.debug(message)
        return message

    def send_gcode_to_api(self, gcode):
        id = 4758
        gcode_request = {
            "jsonrpc": "2.0",
            "method": "printer.gcode.script",
            "params": {
                "script": gcode
            },
            "id": id
        }
        response = self.request_from_unixsocket(json.dumps(gcode_request), id)
        logging.debug("response: %s" % response)
        return response

    def check_is_basic_gcode(self, gcode):
        for g in self.acceptable_gcode:
            if g in gcode.capitalize():
                return True
        return False

    def exit_handler(self):
        if self.tft_serial.is_open:
            self.tft_serial.close()
        logging.debug("Serial closed")

    def start_socket(self):
        self.ws = websocket.WebSocketApp(url=self.ws_url,
                                         on_close=self._on_close,
                                         on_error=self._on_error,
                                         on_message=self._on_message,
                                         on_open=self._on_open)
        self.ws.run_forever()

    def start_serial(self):
        while True:
            gcode = self.tft_serial.readline().decode("utf-8")
            logging.info("gcode: %s" % gcode)
            if self.check_is_basic_gcode(gcode):
                self.send_gcode_to_api(gcode)
                self.write_to_serial("ok\n")
            # Report Temperatures
            elif "M105" in gcode.capitalize():
                self.write_to_serial(self.get_temperature())
            elif "M92" in gcode.capitalize():
                pass
            elif "M90" in gcode.capitalize():
                pass
            elif "M82" in gcode.capitalize():
                pass
            elif "M211" in gcode.capitalize():
                pass
            elif "M503" in gcode.capitalize():
                self.query_report_settings()
            elif "M503 S0" in gcode.capitalize():
                self.query_report_settings()
            elif "M155" in gcode.capitalize():
                # TODO: deshabilitar en firmware
                logging.debug("enable temperature autoreport")
                self.write_to_serial("ok\n")
            elif "M115" in gcode.capitalize():
                self.query_firmware_info()
            elif "M114" in gcode.capitalize():
                self.write_to_serial(self.get_current_position())
            elif "G28" in gcode.capitalize():
                self.send_gcode_to_api(gcode)
                self.write_to_serial("ok\n")
                self.write_to_serial(self.get_current_position())
            elif "G1" in gcode.capitalize():
                self.send_gcode_to_api("G91")
                self.send_gcode_to_api(gcode)
                self.send_gcode_to_api("G90")
                self.write_to_serial("ok\n")
                # self.write_to_serial(self.get_current_position())
            elif "M220" in gcode.capitalize():
                if "M220 S" in gcode.upper():
                    self.send_gcode_to_api(gcode)
                    self.write_to_serial("ok\n")
                else:
                    self.write_to_serial(self.get_speed_factor())
            elif "M221" in gcode.capitalize():
                if "M221 S" in gcode.upper():
                    self.send_gcode_to_api(gcode)
                    self.write_to_serial("ok\n")
                else:
                    self.write_to_serial(self.get_extrude_factor())
            else:
                logging.warn("unknown command")

#
#config loading function of add-on
#
def load_config(config):
    return TFTAdapter(config)
