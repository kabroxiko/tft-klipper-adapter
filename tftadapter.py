import atexit
import threading
import logging
import requests
import serial

logging.basicConfig(level=logging.INFO)

# This should be moved to config
ADDRESS = "http://127.0.0.1/"
PORT = '/dev/ttyS2'


class moonrakerapiclient:

    def __init__(self,  address):
        self.TEMP_URL = address+"printer/objects/query?extruder=target,temperature&heater_bed=target,temperature"
        self.POSITION_URL = address+"printer/objects/query?gcode_move=gcode_position"
        self.SPEED_FACTOR_URL = address+"printer/objects/query?gcode_move=speed_factor"
        self.EXTRUDE_FACTOR_URL = address+"printer/objects/query?gcode_move=extrude_factor"
        self.gcode_url_template = address+"printer/gcode/script?script={g:s}"

    def __make_get_request(self, url):
        r = requests.get(url)
        if(r.status_code == 200):
            logging.info("Response (GET):{}".format(r.json()))
            return r.json()
        else:
            raise Exception("Can not get valid response from monraker (GET)")

    def __make_post_request(self, url):
        r = requests.post(url)
        if(r.status_code == 200):
            logging.info("Response (POST):{}".format(r.json()))
            return r.json()
        elif(r.status_code==400):
            message = r.json().get("error").get("message")
            # do not if 400, handle exception on bigger layer
            if message != "":
                logging.error(message)
                return None
            else:
                logging.error(r)
                raise Exception("Can not get valid response from monraker (POST)")

    def get_status(self):
        logging.debug("get status called")
        return self.__make_get_request(self.TEMP_URL)

    def get_current_position(self):
        logging.debug("get current position called")
        return self.__make_get_request(self.POSITION_URL)

    def get_speed_factor(self):
        logging.debug("get speed factor called")
        return self.__make_get_request(self.SPEED_FACTOR_URL)

    def get_extrude_factor(self):
        logging.debug("get exrude factor called")
        return self.__make_get_request(self.EXTRUDE_FACTOR_URL)

    def send_gcode_to_api(self, gcode):
        logging.debug("send gcode to API called")
        url = self.gcode_url_template.format(g=gcode);
        logging.info("URL:{}".format(url))
        r = self.__make_post_request(url)
        if(r != None):
            return r.get("result")
        else:
            return r


class tftserial:

    def __init__(self, port, baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self.lock = threading.Lock()
        self.tft_serial= None

    def __is_serial_ready(self):
        if(self.tft_serial != None and self.tft_serial.is_open):
                return True
        return False

    def open(self):
        self.tft_serial = serial.Serial(self.port, self.baudrate)
        logging.info("Opening serial port: {} with baudrate: {}".format(self.port, self.baudrate))

    def read(self):
            if(self.__is_serial_ready):
                data = self.tft_serial.readline().decode("utf-8")
                logging.info("Data from serial: {}".format(data.replace("\n", "")))
                return data
            else:
                raise Exception("Can not read from serial port")

    def write(self, data):
        if self.__is_serial_ready():
            self.lock.acquire()
            try:
                self.tft_serial.write(bytes(data, encoding='utf-8'))
            finally:
                self.lock.release()
        else:
            logging.warning("Serial port is not opened")

    def send_ok(self):
        self.write("ok\n")

    def close(self):
        if self.tft_serial.is_open:
            self.tft_serial.close()


client = moonrakerapiclient(ADDRESS)
tft_serial = tftserial(PORT)
tft_serial.open()


temp_template = "ok T:{ETemp:.4f} /{ETarget:.4f} B:{BTemp:.4f} /{BTarget:.4f} P:0 /0.0000 @:0 B@:0\n"
position_template = "X:{x:.2f} Y:{y:.2f} Z:{z:.2f} E:{e:.2f} \nok\n"
feed_rate_template = "FR:{fr:}%\nok\n"
flow_rate_template = "E0 Flow: {er:}%\nok\n"

acceptable_gcode = ["M104", "M140", "M106", "M84"]


# display is asking by M105 for reporting temps
def auto_satus_repost():
    threading.Timer(5.0, auto_satus_repost).start()
    tft_serial.write(get_status())

def get_status():
    r = client.get_status()
    status = r.get("result").get("status")
    statusExtruder = status.get("extruder")
    statusBed = status.get("heater_bed")
    return temp_template.format(ETemp=statusExtruder.get("temperature"), ETarget=statusExtruder.get("target"), BTemp=statusBed.get("temperature"), BTarget=statusBed.get("target"))


def get_current_position():
    r = client.get_current_position()
    position = r.get("result").get("status").get("gcode_move").get("gcode_position")
    logging.info("Position:{}".format(position))
    return position_template.format(x=position[0], y=position[1], z=position[2], e=position[3])


def get_speed_factor():
    r = client.get_speed_factor()
    speed_factor = r.get("result").get("status").get("gcode_move").get("speed_factor")
    return feed_rate_template.format(fr=speed_factor*100)


def get_extrude_factor():
    r = client.get_extrude_factor()
    extrude_factor = r.get("result").get("status").get("gcode_move").get("extrude_factor")
    return flow_rate_template.format(er=extrude_factor*100)

def check_is_basic_gcode(gcode):
    for g in acceptable_gcode:
        if g in gcode.capitalize():
            return True
    return False

def exit_handler():
    tft_serial.close()
    logging.info("Serial closed")

atexit.register(exit_handler)

while True:

    gcode = tft_serial.read()

    if check_is_basic_gcode(gcode):
        client.send_gcode_to_api(gcode)
        tft_serial.send_ok()
    elif "M105" in gcode.capitalize():
        tft_serial.write(get_status())
    elif "M114" in gcode.capitalize():
        tft_serial.write(get_current_position())
    elif "G28" in gcode.capitalize():
        client.send_gcode_to_api(gcode)
        tft_serial.send_ok()
        tft_serial.write(get_current_position())
    elif "G1" in gcode.capitalize():
        client.send_gcode_to_api("G91")
        client.send_gcode_to_api(gcode)
        client.send_gcode_to_api("G90")
        tft_serial.send_ok()
        # write_to_serial(get_current_position())
    elif "M220" in gcode.capitalize():
        if "M220 S" in gcode.upper():
            client.send_gcode_to_api(gcode)
            tft_serial.send_ok()
        else:
            tft_serial.write(get_speed_factor())
    elif "M221" in gcode.capitalize():
        if "M221 S" in gcode.upper():
            client.send_gcode_to_api(gcode)
            tft_serial.send_ok()
        else:
            tft_serial.write(get_extrude_factor())
    else:
        logging.info("Default response to serial for gcode {}".format(gcode))
        tft_serial.write(get_status())
