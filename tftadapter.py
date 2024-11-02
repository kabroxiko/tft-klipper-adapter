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

class TFTAdapter:
    def __init__(self, config):
        logging.getLogger().setLevel(logging.INFO)
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
        printer_ip = "localhost"
        api_port = 7125

        self.ws_url = "ws://%s:%s/websocket?token=" % ( printer_ip, str(api_port))

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
        logging.error("Websocket error: %s" % error)

    def _on_message(self, ws, msg):

        response = json.loads(msg)
        status = None
        # logging.debug(response)
        if 'id' in response and response["id"] == 1234:
            status = response["result"]["status"]
            self.add_subscription()
            # logging.debug(status)
        if "method" in response and response['method'] == "notify_status_update":
            status = response["params"][0]
            # logging.debug(status)
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
        self.query_data(ws)


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
                    "extruder": [ "temperature", "target"],
                    "heater_bed": ["temperature", "target"],
                    "gcode_move": ["gcode_position", "speed_factor", "extrude_factor"]
                }
            },
            "id": 5434
        }
        self.ws.send(json.dumps(data))

    def query_data(self, ws):
        query_req = {
            "jsonrpc": "2.0",
            "method": "printer.objects.query",
            "params": {
                "objects": {
                    "extruder": [ "temperature", "target"],
                    "heater_bed": ["temperature", "target"],
                    "gcode_move": ["gcode_position", "speed_factor", "extrude_factor"]
                }
            },
            "id": 1234
        }
        ws.send(json.dumps(query_req))

    def sock_error_exit(self, msg):
        sys.stderr.write(msg + "\n")
        sys.exit(-1)

    def process_message(self, msg):
        try:
            resp = json.loads(msg)
        except json.JSONDecodeError:
            return None
        if resp.get("id", -1) != 4758:
            return None
        if "error" in resp:
            err = resp["error"].get("message", "Unknown")
            self.sock_error_exit(
                "Error: %s" % (err,)
            )
        return resp["result"]

    def webhook_socket_create(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        while 1:
            try:
                sock.connect(self.moonraker_socket_path)
            except socket.error as e:
                if e.errno == errno.ECONNREFUSED:
                    time.sleep(0.1)
                    continue
                self.sock_error_exit(
                    "Unable to connect socket %s [%d,%s]"
                    % (self.moonraker_socket_path, e.errno, errno.errorcode[e.errno])
                )
            break
        logging.debug("Connected")
        return sock

    def request_from_unixsocket(self, request):
        whsock = self.webhook_socket_create()
        whsock.settimeout(1.)
        whsock.send(request.encode() + b"\x03")
        sock_data = b""
        end_time = time.monotonic() + 30.0
        try:
            while time.monotonic() < end_time:
                try:
                    data = whsock.recv(4096)
                except TimeoutError:
                    pass
                else:
                    if not data:
                        self.sock_error_exit("Socket closed before response received")
                    parts = data.split(b"\x03")
                    parts[0] = sock_data + parts[0]
                    sock_data = parts.pop()
                    for msg in parts:
                        result = self.process_message(msg)
                        if result is not None:
                            return result
                time.sleep(.1)
        finally:
            whsock.close()
        self.sock_error_exit("request timed out")

    def write_to_serial(self, data):
        if self.tft_serial.is_open:
            self.lock.acquire()
            try:
                self.tft_serial.write(bytes(data, encoding='utf-8'))
            finally:
                self.lock.release()
        else:
            logging.debug("serial port is not open")

    def get_status(self):
        message = self.temp_template.format(
            ETemp   = self.extruder['temperature'],
            ETarget = self.extruder['target'],
            BTemp   = self.heater_bed['temperature'],
            BTarget = self.heater_bed['target']
        )
        logging.debug(message)
        return message

    def get_settings(self):
        message = "FIRMWARE_NAME:Klipper {printer.mcu.mcu_version}"
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
        gcode_request = {
            "jsonrpc": "2.0",
            "method": "printer.gcode.script",
            "params": {
                "script": gcode
            },
            "id": 4758
        }
        response = self.request_from_unixsocket(json.dumps(gcode_request))
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
            logging.debug("data from serial: %s" % gcode)
            if self.check_is_basic_gcode(gcode):
                self.send_gcode_to_api(gcode)
                self.write_to_serial("ok\n")
            elif "M105" in gcode.capitalize():
                self.write_to_serial(self.get_status())
            elif "M115" in gcode.capitalize():
                self.write_to_serial(self.get_settings())
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
                logging.debug("unknown command")
                # self.write_to_serial(self.get_status())

#
#config loading function of add-on
#
def load_config(config):
    return TFTAdapter(config)
