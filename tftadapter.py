import serial
import atexit
import sys
import json
import time
import threading
import logging
import socket
import errno

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

        self.temp_request = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "extruder": ["target", "temperature"],
                        "heater_bed": ["target", "temperature"]
                    }
                },
                "id": 1
            }
        )
        self.position_request = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "gcode_move": ["gcode_move"]
                    }
                },
                "id": 1
            }
        )
        self.speed_factor_request = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "gcode_move": ["speed_factor"]
                    }
                },
                "id": 1
            }
        )
        self.extrude_factor_request = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "gcode_move": ["extrude_factor"]
                    }
                },
                "id": 1
            }
        )
        self.position_request = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "printer.objects.query",
                "params": {
                    "objects": {
                        "gcode_move": ["gcode_position"]
                    }
                },
                "id": 1
            }
        )

        # self.gcode_url_template = moonraker_url+"/printer/gcode/script?script={g:s}"

        self.temp_template = "ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0\n"
        self.position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
        self.feed_rate_template = "FR:{fr:}%\nok\n"
        self.flow_rate_template = "E0 Flow: {er:}%\nok\n"

        self.tftSerial = serial.Serial(tft_device, tft_baud)

        self.lock = threading.Lock()

        self.acceptable_gcode = ["M104", "M140", "M106", "M84"]

        atexit.register(self.exit_handler)

        self.auto_status_repost()
        self.start()

    def sock_error_exit(self, msg):
        sys.stderr.write(msg + "\n")
        sys.exit(-1)

    def process_message(self, msg):
        try:
            resp = json.loads(msg)
        except json.JSONDecodeError:
            return None
        if resp.get("id", -1) != 1:
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
        # send mesh query
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
                        self.sock_error_exit("Socket closed before mesh received")
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

    # display is asking by M105 for reporting temps
    def auto_status_repost(self):
        logging.info("start auto_status_repost")
        threading.Timer(5.0, self.auto_status_repost).start()
        self.write_to_serial(self.get_status())
        logging.info("end auto_status_repost")

    def write_to_serial(self, data):
        if self.tftSerial.is_open:
            self.lock.acquire()
            try:
                self.tftSerial.write(bytes(data, encoding='utf-8'))
            finally:
                self.lock.release()
        else:
            logging.info("serial port is not open")

    def get_status(self):
        logging.debug("temp_request: %s" % self.temp_request)
        response = self.request_from_unixsocket(self.temp_request)
        logging.debug("response: %s" % response)
        message = self.temp_template.format(
            ETemp   = response['status']['extruder']['temperature'],
            ETarget = response['status']['extruder']['target'],
            BTemp   = response['status']['heater_bed']['temperature'],
            BTarget = response['status']['heater_bed']['target']
        )
        logging.debug(message)
        return message

    def get_current_position(self):
        response = self.request_from_unixsocket(self.position_request)
        logging.debug("response: %s" % response)
        position = response['status']['gcode_move']['gcode_position']
        return self.position_template.format(x=position[0],y=position[1],z=position[2],e=position[3])

    def get_speed_factor(self):
        response = self.request_from_unixsocket(self.speed_factor_request)
        logging.debug("response: %s" % response)
        speed_factor = response['status']['gcode_move']['speed_factor']
        return self.feed_rate_template.format(fr=speed_factor*100)

    def get_extrude_factor(self):
        response = self.request_from_unixsocket(self.extrude_factor_request)
        logging.debug("response: %s" % response)
        extrude_factor = response['status']['gcode_move']['extrude_factor']
        return self.flow_rate_template.format(er=extrude_factor*100)

    def send_gcode_to_api(self, gcode):
        gcode_request = json.dumps({"jsonrpc": "2.0", "method": "printer.gcode.script", "params": {"script": gcode}, "id": 1})
        response = self.request_from_unixsocket(gcode_request)
        logging.debug("response: %s" % response)
        return response

    def check_is_basic_gcode(self, gcode):
        for g in self.acceptable_gcode:
            if g in gcode.capitalize():
                return True
        return False

    def exit_handler(self):
        if self.tftSerial.is_open:
            self.tftSerial.close()
        logging.info("Serial closed")

    def start(self):
        while True:
            gcode = self.tftSerial.readline().decode("utf-8")
            logging.info("data from serial: %s" % gcode)

            if self.check_is_basic_gcode(gcode):
                self.send_gcode_to_api(gcode)
                self.write_to_serial("ok\n")
            elif "M105" in gcode.capitalize():
                self.write_to_serial(self.get_status())
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
                logging.info("unknown command")
                # self.write_to_serial(self.get_status())

#
#config loading function of add-on
#
def load_config(config):
    return TFTAdapter(config)
