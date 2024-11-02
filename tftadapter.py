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

        query_url = "%s/printer/objects/query" % (moonraker_url)
        self.temp_url = "%s?extruder=target,temperature&heater_bed=target,temperature" % (query_url)
        self.position_url = "%s?gcode_move=gcode_position" % (query_url)
        self.speed_factor_url = "%s?gcode_move=speed_factor" % (query_url)
        self.extrude_factor_url = "%s?gcode_move=extrude_factor" % (query_url)
        self.gcode_url_template = "%s/printer/gcode/script?script={g:s}" % (moonraker_url)

        self.temp_template = "ok T:{ETemp:.2f} /{ETarget:.2f} B:{BTemp:.2f} /{BTarget:.2f} @:0 B@:0\n"
        self.position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
        self.feed_rate_template = "FR:{fr:}%\nok\n"
        self.flow_rate_template = "E0 Flow: {er:}%\nok\n"

        self.tftSerial = serial.Serial(tft_device, tft_baud)

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
                logging.info("response: %s" % data)
            finally:
                self.lock.release()
        else:
            logging.info("serial port is not open")

    def get_status(self):
        logging.debug(self.temp_url)
        r = requests.get(self.temp_url)
        logging.debug(r)
        logging.debug(r.status_code)
        logging.debug(r.json())
        results = r.json().get("result")
        logging.debug(results)
        status = results.get("status")
        logging.debug(status)
        statusExtruder = status.get("extruder")
        logging.debug(statusExtruder)
        statusBed = status.get("heater_bed")
        logging.debug(statusBed)
        return self.temp_template.format(ETemp = statusExtruder.get("temperature"), ETarget= statusExtruder.get("target"), BTemp=statusBed.get("temperature"), BTarget = statusBed.get("target"))

    def get_current_position(self):
        r = requests.get(self.position_url)
        logging.debug(r.json())
        position = r.json().get("result").get("status").get("gcode_move").get("gcode_position")
        logging.debug(position)
        return self.position_template.format(x=position[0],y=position[1],z=position[2],e=position[3])

    def get_speed_factor(self):
        r = requests.get(self.speed_factor_url)
        logging.debug(r.json())
        speed_factor = r.json().get("result").get("status").get("gcode_move").get("speed_factor")
        return self.feed_rate_template.format(fr=speed_factor*100)

    def get_extrude_factor(self):
        r = requests.get(self.extrude_factor_url)
        logging.debug(r.json())
        extrude_factor = r.json().get("result").get("status").get("gcode_move").get("extrude_factor")
        return self.flow_rate_template.format(er=extrude_factor*100)

    #self.auto_satus_repost()
    def send_gcode_to_api(self, gcode):
        r = requests.post(self.gcode_url_template.format(g=gcode))
        logging.debug(r)
        return r.json().get("result")

    def check_is_basic_gcode(self, gcode):
        for g in self.acceptable_gcode:
            if g in gcode.capitalize():
                return True
        return False


    def exit_handler(self):
        if self.tftSerial.is_open:
            self.tftSerial.close()
        logging.debug("Serial closed")

    def start(self):
        while True:
            line = self.tftSerial.readline()
            logging.info("x: '%s'" % line.rstrip())
            gcode = line.rstrip().decode("utf-8")
            logging.info("data from serial: %s" % (gcode))

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
                logging.debug("default response to serial")
                self.write_to_serial(self.get_status())

#
#config loading function of add-on
#
def load_config(config):
    return TFTAdapter(config)
