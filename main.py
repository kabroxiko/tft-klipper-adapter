#!/usr/bin/env python3
import tftadapter
import logging

logging.basicConfig(format='%(message)s',level=logging.INFO)

class Printer:
    def __init__(self):
        self.reactor = ''
    def get_reactor(self):
        return self.reactor
    def lookup_object(self, name):
        default = ''
        return default

class PrinterConfig:
    def __init__(self):
        self.printer = Printer()
    def get_printer(self):
        return self.printer
    def get(self, key):
        if key == 'tft_device':
            return '/dev/ttyS2'
        elif key == 'moonraker_url':
            return 'http://127.0.0.1:7125'
        else:
            exit(1)
    def getint(self, key):
        if key == 'tft_baud':
            return 115200

    # def run(self):
config = PrinterConfig()
tftadapter.load_config(config)

# if __name__ == '__main__':
#     Run().run()
