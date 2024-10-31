import serial
import atexit
import requests
import threading
import logging
# from websocket import create_connection


class TFTAdapter:
    def __init__(self, config):
        self.printer = config.get_printer()
        tft_device = config.get('tft_device')
        tft_baud = config.getint('tft_baud')
        moonraker_url = config.get('moonraker_url')

        query = "query?extruder=target,temperature"

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }
        query_url = "%sprinter/objects/query" % (moonraker_url)
        self.temp_url = "%s?extruder=target,temperature&heater_bed=target,temperature" % (query_url)
        self.POSITION_URL = moonraker_url+"printer/objects/query?gcode_move=gcode_position"
        self.SPEED_FACTOR_URL = moonraker_url+"printer/objects/query?gcode_move=speed_factor"
        self.EXTRUDE_FACTOR_URL = moonraker_url+"printer/objects/query?gcode_move=extrude_factor"
        self.TEMP_URL = moonraker_url+"printer/objects/query"
        self.gcode_url_template = moonraker_url+"printer/gcode/script?script={g:s}"
        # self.temp_url = "%s/printer/objects/%s" % (moonraker_url, temp_query)

        self.temp_template = "ok T:{ETemp:.4f} /{ETarget:.4f} B:{BTemp:.4f} /{BTarget:.4f} P:0 /0.0000 @:0 B@:0\n"
        self.position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
        self.feed_rate_template = "FR:{fr:}%\nok\n"
        self.flow_rate_template = "E0 Flow: {er:}%\nok\n"

        self.tftSerial = serial.Serial(tft_device, tft_baud)  # open serial port
        # self.ws = create_connection("wss://ws.dogechain.info/inv")

        logging.info(self.tftSerial.name)

        self.lock = threading.Lock()

        self.acceptable_gcode = ["M104", "M140", "M106", "M84"]

        atexit.register(self.exit_handler)

        self.start()

    # display is asking by M105 for reporting temps
    def auto_satus_repost(self):
        threading.Timer(5.0, auto_satus_repost).start()
        self.write_to_serial(self.get_status())

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
        logging.info(self.temp_url)
        r = requests.get(self.temp_url)
        logging.info(r)
        logging.info(r.status_code)
        logging.info(r.json())
        results = r.json().get("result")
        logging.info(results)
        status = results.get("status")
        logging.info(status)
        statusExtruder = status.get("extruder")
        logging.info(statusExtruder)
        statusBed = status.get("heater_bed")
        logging.info(statusBed)
        return self.temp_template.format(ETemp = statusExtruder.get("temperature"), ETarget= statusExtruder.get("target"), BTemp=statusBed.get("temperature"), BTarget = statusBed.get("target"))

    def get_current_position(self):
        r = requests.get(self.POSITION_URL)
        logging.info(r.json())
        position = r.json().get("result").get("status").get("gcode_move").get("gcode_position")
        logging.info(position)
        return self.position_template.format(x=position[0],y=position[1],z=position[2],e=position[3])

    def get_speed_factor(self):
        r = requests.get(self.SPEED_FACTOR_URL)
        logging.info(r.json())
        speed_factor = r.json().get("result").get("status").get("gcode_move").get("speed_factor")
        return self.feed_rate_template.format(fr=speed_factor*100)

    def get_extrude_factor(self):
        r = requests.get(self.EXTRUDE_FACTOR_URL)
        logging.info(r.json())
        extrude_factor = r.json().get("result").get("status").get("gcode_move").get("extrude_factor")
        return self.flow_rate_template.format(er=extrude_factor*100)

    #self.auto_satus_repost()
    def send_gcode_to_api(self, gcode):
        r = requests.post(self.gcode_url_template.format(g=gcode))
        logging.info(r)
        return r.json().get("result")

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
        a = 0
        while a <= 10:
            gcode = self.tftSerial.readline().decode("utf-8")
            logging.info("data from serial:\n")
            logging.info(gcode)

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
                logging.info("default response to serial")
                self.write_to_serial(self.get_status())
            a += 1

#
#config loading function of add-on
#
def load_config(config):
    return TFTAdapter(config)
