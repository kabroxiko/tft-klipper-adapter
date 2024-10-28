import serial  # instalation python3 -m pip install pyserial
import logging
import threading

class TFTSerial:

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
