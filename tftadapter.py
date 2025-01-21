# TFT LCD display support
#
# Copyright (C) 2020  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import serial
import os
import time
import errno
import logging
import asyncio
import re
from jinja2 import Template
from collections import deque
from ..utils import ServerError
from ..utils import json_wrapper as jsonw

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Deque,
    Any,
    Tuple,
    Optional,
    Dict,
    List,
    Callable,
    Coroutine,
    Union,
)
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .klippy_connection import KlippyConnection
    from .klippy_apis import KlippyAPI as APIComp
    from .file_manager.file_manager import FileManager as FMComp
    FlexCallback = Callable[..., Optional[Coroutine]]

MIN_EST_TIME = 10.
INITIALIZE_TIMEOUT = 10.

class TFTError(ServerError):
    pass


RESTART_GCODES = ["RESTART", "FIRMWARE_RESTART"]

PRINT_STATUS_TEMPLATE = (
    "//action:notification Layer Left {{ (virtual_sdcard.file_position or 0) }}/{{ (virtual_sdcard.file_size or 0) }}"
)

TEMPERATURE_TEMPLATE = (
    "T:{{ extruder.temperature | round(2) }} /{{ extruder.target | round(2) }} "
    "B:{{ heater_bed.temperature | round(2) }} /{{ heater_bed.target | round(2) }} "
    "@:0 B@:0"
)

PROBE_OFFSET_TEMPLATE = (
    "M851 X{{ bltouch.x_offset | float - gcode_move.homing_origin[0] }} "
    "Y{{ bltouch.y_offset | float - gcode_move.homing_origin[1] }} "
    "Z{{ bltouch.z_offset | float - gcode_move.homing_origin[2] }}"
)

REPORT_SETTINGS_TEMPLATE = (
    "M203 X{{ toolhead.max_velocity }} Y{{ toolhead.max_velocity }} "
    "Z{{ printer.max_z_velocity }} E{{ extruder.max_extrude_only_velocity }}\n"
    "M201 X{{ toolhead.max_accel }} Y{{ toolhead.max_accel }} "
    "Z{{ printer.max_z_accel }} E{{ extruder.max_extrude_only_accel }}\n"
    "M206 X{{ gcode_move.homing_origin[0] }} Y{{ gcode_move.homing_origin[1] }} Z{{ gcode_move.homing_origin[2] }}\n"
    f"{PROBE_OFFSET_TEMPLATE}\n"
    "M420 S1 Z{{ bed_mesh.fade_end }}\n"
    "M106 S{{ fan.speed }}"
)

FIRMWARE_INFO_TEMPLATE = (
    "FIRMWARE_NAME:{{ firmware_name }}"
    "SOURCE_CODE_URL:https://github.com/Klipper3d/klipper "
    "PROTOCOL_VERSION:1.0 "
    "MACHINE_TYPE:{{ machine_name }}\n"
    "Cap:EEPROM:1\n"
    "Cap:AUTOREPORT_TEMP:1\n"
    "Cap:AUTOREPORT_POS:1\n"
    "Cap:AUTOLEVEL:1\n"
    "Cap:Z_PROBE:1\n"
    "Cap:LEVELING_DATA:0\n"
    "Cap:SOFTWARE_POWER:0\n"
    "Cap:TOGGLE_LIGHTS:0\n"
    "Cap:CASE_LIGHT_BRIGHTNESS:0\n"
    "Cap:EMERGENCY_PARSER:1\n"
    "Cap:PROMPT_SUPPORT:0\n"
    "Cap:SDCARD:1\n"
    "Cap:MULTI_VOLUME:0\n"
    "Cap:AUTOREPORT_SD_STATUS:1\n"
    "Cap:LONG_FILENAME:1\n"
    "Cap:BABYSTEPPING:1\n"
    "Cap:BUILD_PERCENT:1\n"
    "Cap:CHAMBER_TEMPERATURE:0"
)

SOFTWARE_ENDSTOPS_TEMPLATE = (
    "Soft endstops: {{ state }}"
)

PROBE_TEST_TEMPLATE = (
    "Last query: {{ probe.last_query }}\n"
    "Last Z result: {{ probe.last_z_result }}"
)

POSITION_TEMPLATE = (
    "X:{{ gcode_move.position[0] | round(2) }} "
    "Y:{{ gcode_move.position[1] | round(2) }} "
    "Z:{{ gcode_move.position[2] | round(2) }} "
    "E:{{ gcode_move.position[3] | round(2) }}"
)

FEED_RATE_TEMPLATE = (
    "FR:{{ gcode_move.speed_factor * 100 | int }}%"
)
FLOW_RATE_TEMPLATE = (
    "E0 Flow:{{ gcode_move.extrude_factor * 100 | int }}%"
)

FILE_LIST_TEMPLATE = (
    "Begin file list\n"
    "{% for file, size in files %}{{ file }} {{ size }}\n{% endfor %}"
    "End file list\nok"
)

PROBE_ACCURACY_TEMPLATE = (
    "Mean: {{ avg_val }} Min: {{ min_val }} Max: {{ max_val }} Range: {{ range_val }}\n"
    "Standard Deviation: {{ stddev_val }}\n"
    "ok"
)

class SerialConnection:
    def __init__(self,
                 config: ConfigHelper,
                 tft: TFT
                 ) -> None:
        self.event_loop = config.get_server().get_event_loop()
        self.tft = tft
        self.port: str = config.get('serial')
        self.baud = config.getint('baud', 57600)
        self.partial_input: bytes = b""
        self.ser: Optional[serial.Serial] = None
        self.fd: Optional[int] = None
        self.connected: bool = False
        self.send_busy: bool = False
        self.send_buffer: bytes = b""
        self.attempting_connect: bool = True

    def disconnect(self, reconnect: bool = False) -> None:
        if self.connected:
            if self.fd is not None:
                self.event_loop.remove_reader(self.fd)
                self.fd = None
            self.connected = False
            if self.ser is not None:
                self.ser.close()
            self.ser = None
            self.partial_input = b""
            self.send_buffer = b""
            self.tft.initialized = False
            logging.info("TFT Disconnected")
        if reconnect and not self.attempting_connect:
            self.attempting_connect = True
            self.event_loop.delay_callback(1., self.connect)

    async def connect(self) -> None:
        self.attempting_connect = True
        start_time = connect_time = time.time()
        while not self.connected:
            if connect_time > start_time + 30.:
                logging.info("Unable to connect, aborting")
                break
            logging.info(f"Attempting to connect to: {self.port}")
            try:
                # XXX - sometimes the port cannot be exclusively locked, this
                # would likely be due to a restart where the serial port was
                # not correctly closed.  Maybe don't use exclusive mode?
                self.ser = serial.Serial(
                    self.port, self.baud, timeout=0, exclusive=True)
            except (OSError, IOError, serial.SerialException):
                logging.exception(f"Unable to open port: {self.port}")
                await asyncio.sleep(2.)
                connect_time += time.time()
                continue
            self.fd = self.ser.fileno()
            fd = self.fd = self.ser.fileno()
            os.set_blocking(fd, False)
            self.event_loop.add_reader(fd, self._handle_incoming)
            self.connected = True
            logging.info("TFT Connected")
        self.attempting_connect = False

    def _handle_incoming(self) -> None:
        # Process incoming data using same method as gcode.py
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, 4096)
        except os.error:
            return

        if not data:
            # possibly an error, disconnect
            self.disconnect(reconnect=True)
            logging.info("serial_display: No data received, disconnecting")
            return

        # Remove null bytes, separate into lines
        data = data.strip(b'\x00')
        lines = data.split(b'\n')
        lines[0] = self.partial_input + lines[0]
        self.partial_input = lines.pop()
        for line in lines:
            try:
                decoded_line = line.strip().decode('utf-8', 'ignore')
                self.tft.process_line(decoded_line)
            except ServerError:
                logging.exception(
                    f"GCode Processing Error: {decoded_line}")
                self.tft.handle_gcode_response(
                    f"!! GCode Processing Error: {decoded_line}")
            except Exception:
                logging.exception("Error during gcode processing")

    def send(self, data: bytes) -> None:
        self.send_buffer += data
        if not self.send_busy:
            self.send_busy = True
            self.event_loop.register_callback(self._do_send)

    async def _do_send(self) -> None:
        assert self.fd is not None
        while self.send_buffer:
            if not self.connected:
                break
            try:
                sent = os.write(self.fd, self.send_buffer)
            except os.error as e:
                if e.errno == errno.EBADF or e.errno == errno.EPIPE:
                    sent = 0
                else:
                    await asyncio.sleep(.001)
                    continue
            if sent:
                self.send_buffer = self.send_buffer[sent:]
            else:
                logging.exception(
                    "Error writing data, closing serial connection")
                self.disconnect(reconnect=True)
                return
        self.send_busy = False

class TFTAdapter:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.event_loop = self.server.get_event_loop()
        self.file_manager: FMComp = self.server.lookup_component('file_manager')
        self.klippy_apis: APIComp = self.server.lookup_component('klippy_apis')
        self.machine_name = config.get('machine_name', "Klipper")
        self.firmware_name: str = "Klipper"
        self.last_message: Optional[str] = None
        self.current_file: str = ""
        self.file_metadata: Dict[str, Any] = {}
        self.enable_checksum = config.getboolean('enable_checksum', True)
        self.debug_queue: Deque[str] = deque(maxlen=100)
        self.temperature_report_task: Optional[asyncio.Task] = None
        self.position_report_task: Optional[asyncio.Task] = None

        # Initialize tracked state.
        kconn: KlippyConnection = self.server.lookup_component("klippy_connection")
        self.printer_state: Dict[str, Dict[str, Any]] = kconn.get_subscription_cache()
        self.config = {}
        self.extruder_count: int = 0
        self.heaters: List[str] = []
        self.is_ready: bool = False
        self.is_shutdown: bool = False
        self.initialized: bool = False
        self.cq_busy: bool = False
        self.gq_busy: bool = False
        self.queue: List[Union[str, Tuple[FlexCallback, Any, Any]]] = []
        self.last_printer_state: str = 'O'
        self.last_update_time: float = 0.

        # Set up macros
        self.confirmed_gcode: str = ""
        self.mbox_sequence: int = 0
        self.available_macros: Dict[str, str] = {}
        self.confirmed_macros = {
            "RESTART": "RESTART",
            "FIRMWARE_RESTART": "FIRMWARE_RESTART"}
        macros = config.getlist('macros', None)
        if macros is not None:
            # The macro's configuration name is the key, whereas the full
            # command is the value
            self.available_macros = {m.split()[0]: m for m in macros}
        conf_macros = config.getlist('confirmed_macros', None)
        if conf_macros is not None:
            # The macro's configuration name is the key, whereas the full
            # command is the value
            self.confirmed_macros = {m.split()[0]: m for m in conf_macros}
        self.available_macros.update(self.confirmed_macros)
        self.non_trivial_keys = config.getlist('non_trivial_keys', ["Klipper state"])
        self.ser_conn = SerialConnection(config, self)
        logging.info("TFT Configured")

        # Register server events
        self.server.register_event_handler(
            "server:klippy_ready", self._process_klippy_ready
        )
        self.server.register_event_handler(
            "server:klippy_shutdown", self._process_klippy_shutdown
        )
        self.server.register_event_handler(
            "server:klippy_disconnect", self._process_klippy_disconnect
        )
        self.server.register_event_handler(
            "server:gcode_response", self.handle_gcode_response
        )

        # These commands are directly executued on the server and do not to
        # make a request to Klippy
        self.direct_gcodes: Dict[str, FlexCallback] = {
            'G26': self._send_ok_response, # Mesh Validation Pattern (G26 H240 B70 R99)
            'G29': self._send_ok_response, # Bed leveling (G29)
            'G30': self._send_ok_response, # Single Z-Probe (G30 E1 X28 Y207)
            'M0': self._cancel_print,
            'M20': self._list_sd_files,
            'M21': self._init_sd_card,
            'M23': self._select_sd_file,
            'M24': self._start_print,
            'M25': self._pause_print,
            'M27': self._set_print_status_report,
            'M30': self._delete_sd_file,
            'M32': self._print_file,
            'M33': self._get_long_path,
            'M36': self._get_sd_file_info,
            'M48': self._probe_bed,
            'M82': self._send_ok_response,
            'M92': self._send_ok_response,
            'M105': self._report_temperature,
            'M108': self._send_ok_response,
            'M114': self._report_position,
            'M115': self._report_firmware_info,
            'M118': self._handle_m118_command,
            'M120': self._save_gcode_state,
            'M121': self._restore_gcode_state,
            'M150': self._set_led,
            'M154': self._set_position_report,
            'M155': self._set_temperature_report,
            'M201': self._set_acceleration,
            'M203': self._set_velocity,
            'M206': self._set_gcode_offset,
            'M211': self._report_software_endstops,
            'M220': self._set_feed_rate,
            'M221': self._set_flow_rate,
            'M280': self._probe_command,
            'M290': self._set_babystep,
            'M420': self._set_bed_leveling,
            'M500': self._z_offset_apply_probe,
            'M503': self._report_settings,
            'M524': self._cancel_print,
            'M701': self._load_filament,
            'M702': self._unload_filament,
            'M851': self._set_probe_offset,
            'M999': self._firmware_restart,
            'T0': self._send_ok_response,
        }

        self.standard_gcodes: List[str] = {
            'G0',
            'G1',
            'G28',
            'G90',
            'M84',
            'M104',
            'M106',
            'M140',
        }

    async def component_init(self) -> None:
        await self.ser_conn.connect()

    async def _process_klippy_ready(self) -> None:
        # Request "info" and "configfile" status
        retries = 10
        printer_info: Dict[str, Any] = {}
        cfg_status: Dict[str, Any] = {}
        while retries:
            try:
                printer_info = await self.klippy_apis.get_klippy_info()
                cfg_status = await self.klippy_apis.query_objects({'configfile': None})
            except self.server.error:
                logging.exception("TFT initialization request failed")
                retries -= 1
                if not retries:
                    raise
                await asyncio.sleep(1.)
                continue
            break

        self.firmware_name = "Klipper " + printer_info['software_version']
        self.config: Dict[str, Any] = cfg_status.get('configfile', {}).get('config', {})

        logging.info(
            f"TFT Config Received:\n"
            f"Firmware Name: {self.firmware_name}\n"
            f"Printer Config: {self.config}\n")

        # Make subscription request
        sub_args: Dict[str, Optional[List[str]]] = {
            "motion_report": None,
            "gcode_move": None,
            "toolhead": None,
            "virtual_sdcard": None,
            "fan": None,
            "display_status": None,
            "print_stats": None,
            "idle_timeout": None,
            "probe": None,
            "filament_switch_sensor filament_sensor": None
        }
        self.extruder_count = 0
        self.heaters = []
        extruders = []
        for cfg in self.config:
            if cfg.startswith("extruder"):
                self.extruder_count += 1
                extruders.append(cfg)
                sub_args[cfg] = None
            elif cfg == "heater_bed":
                self.heaters.append(cfg)
                sub_args[cfg] = None
        extruders.sort()
        self.heaters.extend(extruders)
        try:
            await self.klippy_apis.subscribe_objects(sub_args)
        except self.server.error:
            logging.exception("Unable to complete subscription request")
        self.is_shutdown = False
        self.is_ready = True
        self.event_loop.create_task(self._monitor_print_status())

    async def _monitor_print_status(self) -> None:
        while True:
            await asyncio.sleep(1)
            print_stats = self.printer_state.get('print_stats', {})
            state = print_stats.get('state', 'standby')
            if state == 'printing' and self.last_printer_state != 'printing':
                self.write_response(action="print_start")
            elif state == 'paused' and self.last_printer_state == 'paused':
                self.write_response(action="pause")
            elif state == 'printing' and self.last_printer_state == 'paused':
                self.write_response(action="resume")
            elif state == 'cancelled' and self.last_printer_state != 'cancelled':
                self.write_response(action="cancel")
            self.last_printer_state = state

    def _process_klippy_shutdown(self) -> None:
        self.is_shutdown = True

    def _process_klippy_disconnect(self) -> None:
        # Tell the PD that the printer is "off"
        self.write_response({'status': 'O'})
        self.last_printer_state = 'O'
        self.is_ready = False
        self.is_shutdown = self.is_shutdown = False

    def process_line(self, line: str) -> None:
        logging.info(f"line: {line}")
        self.debug_queue.append(line)
        # If we find M112 in the line then skip verification
        if "M112" in line.upper():
            self.event_loop.register_callback(self.klippy_apis.emergency_stop)
            return

        script = line
        # Execute the gcode.  Check for special RRF gcodes that
        # require special handling
        parts = script.split()
        gcode = parts[0].strip()
        if gcode in ["M23", "M30", "M32", "M36", "M37"]:
            arg = script[len(gcode):].strip()
            parts = [gcode, arg]

        # Check for commands that query state and require immediate response
        if gcode in self.direct_gcodes or gcode in self.standard_gcodes:
            if gcode in self.standard_gcodes:
                self.queue_task(script)
                self.write_response("ok")
                return
            params: Dict[str, Any] = {}
            for part in parts[1:]:
                logging.info(f"part: {part}")
                if not re.match(r'^-?\d+(?:\.\d+)?$', part[1:]):
                    if not params.get("arg_string"):
                        params["arg_string"] = part
                    else:
                        params["arg_string"] = f'{params["arg_string"]} {part}'
                    continue
                else:
                    arg = part[0].lower()
                    if re.match(r'^-?\d+$', part[1:]):
                        val = int(part[1:])
                    else:
                        val = float(part[1:])
                    params[f"arg_{arg}"] = val
            logging.info(f'params: {params}')
            func = self.direct_gcodes[gcode]
            self.queue_task(func, **params)
            return
        else:
            logging.warning(f"Unregistered command: {line}")
            script = line

        if not script:
            logging.warning(f"No script generated for command: {line}")
            return
        self.queue_task(script)

    def queue_task(self, cmd: FlexCallback, *args, **kwargs) -> None:
        self.queue_task((cmd, args, kwargs))

    def queue_task(self, task: Union[str, List[str], Tuple[FlexCallback, Any, Any]]) -> None:
        if isinstance(task, str):
            self.queue.append(task)
        elif isinstance(task, list):
            self.queue.extend(task)
        elif isinstance(task, tuple) and len(task) == 2:
            self.queue.append((task[0], task[1], {}))
        else:
            self.queue.append(task)
        if not self.gq_busy:
            self.gq_busy = True
            self.event_loop.register_callback(self._process_queue)

    async def _process_queue(self) -> None:
        self.gq_busy = True
        while self.queue:
            item = self.queue.pop(0)
            if isinstance(item, str):
                script = item
                logging.info(f"script: {script}")
                try:
                    if script in RESTART_GCODES:
                        await self.klippy_apis.do_restart(script)
                    else:
                        await self.klippy_apis.run_gcode(script)
                        logging.info(f"end script: {script}")
                except self.server.error:
                    msg = f"Error executing script {script}"
                    self.handle_gcode_response("!! " + msg)
                    logging.exception(msg)
            else:
                cmd, args, kwargs = item
                try:
                    ret = cmd(*args, **kwargs)
                    if ret is not None:
                        await ret
                except Exception:
                    logging.exception("Error processing command")
        self.gq_busy = False

    def _clean_filename(self, filename: str) -> str:
        # Remove quotes and whitespace
        filename.strip(" \"\t\n")
        # Remove drive number
        if filename.startswith("0:/"):
            filename = filename[3:]
        # Remove initial "gcodes" folder.  This is necessary
        # due to the HACK in the tft_M20 gcode.
        if filename.startswith("gcodes/"):
            filename = filename[6:]
        elif filename.startswith("/gcodes/"):
            filename = filename[7:]
        # Start with a "/" so the gcode parser can correctly
        # handle files that begin with digits or special chars
        if filename[0] != "/":
            filename = "/" + filename
        return filename

    def _select_sd_file(self, arg_string: str) -> str:
        self.current_file = self._clean_filename(arg_string)
        self.queue_task(f"M23 {self.current_file}")

    def _start_print(self) -> str:
        if not self.current_file:
            raise TFTError("No file selected to print")
        sd_state = self.printer_state.get("print_stats", {}).get("state", "standby")
        if sd_state == "paused":
            self.queue_task("RESUME")
        elif sd_state in ("standby", "cancelled"):
            self.queue_task(f"SDCARD_PRINT_FILE FILENAME=\"{self.current_file}\"")
        else:
            raise TFTError("Cannot start printing, printer is not in a stopped state")

    def _pause_print(self, arg_p: int) -> str:
        # TODO: handle P1
        sd_state = self.printer_state.get("print_stats", {}).get("state", "standby")
        if sd_state == "printing":
            self.queue_task("PAUSE")
        else:
            raise TFTError("Cannot pause, printer is not printing")

    def _probe_command(self, arg_p: int, arg_s: int) -> None:
        if arg_s == 120:  # Test
            cmd = "QUERY_PROBE"
        else:
            if self.config.get("bltouch"):
                value = {
                    10: "pin_down",
                    90: "pin_up",
                    160: "reset"
                }.get(arg_s)
                cmd = f"BLTOUCH_DEBUG COMMAND={value}"
            else:
                value = {
                    10: "1",
                    90: "0",
                    160: "0"
                }.get(arg_s)
                cmd = f"SET_PIN PIN=_probe_enable VALUE={value}"
        self.queue_task(cmd)
        self.write_response("ok")

    def _print_file(self, args: List[str]) -> str:
        filename = self._clean_filename(args[0])
        # Escape existing double quotes in the file name
        filename = filename.replace("\"", "\\\"")
        self.queue_task(f"SDCARD_PRINT_FILE FILENAME=\"{filename}\"")

    def _set_led(self, args: List[str]) -> str:
        params = {arg[0]: int(arg[1:]) for arg in args}
        red = params.get('R', 0) / 255
        green = params.get('U', 0) / 255
        blue = params.get('B', 0) / 255
        white = params.get('W', 0) / 255
        brightness = params.get('P', 255) / 255
        cmd = (
            f"SET_LED LED=statusled "
            f"RED={red * brightness:.3f} "
            f"GREEN={green * brightness:.3f} "
            f"BLUE={blue * brightness:.3f} "
            f"WHITE={white * brightness:.3f} "
            "TRANSMIT=1 SYNC=1"
        )
        self.queue_task(cmd)
        self.write_response("ok")

    def _set_babystep(self, **args: Dict[float]) -> None:
        offsets = []
        if 'arg_x' in args:
            offsets.append(f"X={args['arg_x']}")
        if 'arg_y' in args:
            offsets.append(f"Y={args['arg_y']}")
        if 'arg_z' in args:
            offsets.append(f"Z={args['arg_z']}")
        offset_str = " ".join(offsets)
        self.queue_task(f"SET_GCODE_OFFSET {offset_str}")
        self.write_response("ok")

    def _set_bed_leveling(self,
                          **args) -> None:
        if args.get('arg_s'):
            if args.get('arg_s') == 0:
                self.queue_task("BED_MESH_CLEAR")
            else:
                self.queue_task("BED_MESH_PROFILE LOAD=default")
        else:
            # TODO: Falta implementar M420 V1 T1 y M420 Zx.xx
            self.write_response("ok")

    def handle_gcode_response(self, response: str) -> None:
        if "// Sending" in response:
            return

        if "Klipper state" in response or response.startswith('!!'):
            self.write_response(action=f"notification {response}")
        elif response.startswith('File opened:') or response.startswith('File selected') or response.startswith('ok'):
            self.write_response(response)
        elif response.startswith('echo: Adjusted Print Time'):
            timeleft = response.split('echo: Adjusted Print Time')[-1].strip()
            hours, minutes = timeleft.split('hr')
            minutes = minutes.strip().replace('min', '')
            formatted_timeleft = f"{hours}h{minutes}m00s"
            self.write_response(action=f"notification Time Left {formatted_timeleft}")
        elif response.startswith('//'):
            message = response[3:]
            if "probe: open" in message:
                response = f"{Template(PROBE_TEST_TEMPLATE).render(**self.printer_state)}\nok"
                self.write_response(response)
            elif "probe accuracy results:" in message:
                parts = message.split(',')
                max_val = parts[0].split()[-1]
                min_val = parts[1].split()[-1]
                range_val = parts[2].split()[-1]
                avg_val = parts[3].split()[-1]
                stddev_val = parts[5].split()[-1]
                marlin_response = Template(PROBE_ACCURACY_TEMPLATE).render(
                    max_val=max_val,
                    min_val=min_val,
                    range_val=range_val,
                    avg_val=avg_val,
                    stddev_val=stddev_val
                )
                self.write_response(marlin_response)
            elif "Unknown command" in message:
                self.write_response(error=message)
        elif "B:" in response and "T0:" in response:
            parts = response.split()
            bed_temp = float(parts[0].split(":")[1])
            bed_target = float(parts[1].split("/")[1])
            extruder_temp = float(parts[2].split(":")[1])
            extruder_target = float(parts[3].split("/")[1])
            temperature_response = Template(TEMPERATURE_TEMPLATE).render(
                extruder={"temperature": extruder_temp, "target": extruder_target},
                heater_bed={"temperature": bed_temp, "target": bed_target}
            )
            self.write_response(f"ok {temperature_response}")
        else:
            logging.info(f"Untreated response: {response}")

    def write_response(self, message=None, command=None, action=None, error=None) -> None:
        if command:
            msg = f'{command}'
        elif action:
            msg = f'//action:{action}'
        elif error:
            msg = f'Error:{error}'
        else:
            msg = f'{message}'

        formatted_msg = msg.replace('\n', '\\n')
        logging.info(f"response: {formatted_msg}")
        byte_resp = (msg + "\n").encode("utf-8")
        self.ser_conn.send(byte_resp)

    async def notify_timeleft(self, timeleft):
        await self.write_response(action=f'notification Time Left {timeleft}')

    async def notify_dataleft(self, current, max_data):
        await self.write_response(action=f'notification Data Left {current}/{max_data}')

    async def report(self, template, interval):
        while self.ser_conn.connected and interval > 0:
            report = Template(template).render(**self.printer_state)
            self.write_response(f"{report}")
            await asyncio.sleep(interval)

    def _init_sd_card(self) -> str:
        self.write_response("SD card ok\nok")

    def _list_sd_files(self, arg_string: Optional[str] = None) -> None:
        response_type = 2
        if response_type != 2:
            logging.info(
                f"Cannot process response type {response_type} in M20")
            return
        path = "/"

        # Strip quotes if they exist
        path = path.strip('\"')

        # Path should come in as "0:/macros, or 0:/<gcode_folder>".  With
        # repetier compatibility enabled, the default folder is root,
        # ie. "0:/"
        if path.startswith("0:/"):
            path = path[2:]
        response: Dict[str, Any] = {'dir': path}
        response['files'] = []

        if path == "/macros":
            response['files'] = [(macro, 0) for macro in self.available_macros.keys()]
        else:
            # HACK: The TFT has a bug where it does not correctly detect
            # subdirectories if we return the root as "/".  Moonraker can
            # support a "gcodes" directory, however we must choose between this
            # support or disabling RRF specific gcodes (this is done by
            # identifying as Repetier).
            # The workaround below converts both "/" and "/gcodes" paths to
            # "gcodes".
            if path == "/":
                response['dir'] = "/gcodes"
                path = "gcodes"
            elif path.startswith("/gcodes"):
                path = path[1:]

            flist = self.file_manager.list_dir(path, simple_format=False)
            if flist:
                response['files'] = [(file['filename'], file['size']) for file in flist.get("files")]

        marlin_response = Template(FILE_LIST_TEMPLATE).render(files=response['files'])
        self.write_response(marlin_response)

    async def _delete_sd_file(self, arg_string: str = "") -> None:
        # Delete a file.  Clean up the file name and make sure
        # it is relative to the "gcodes" root.
        path = arg_string
        path = path.strip('\"')
        if path.startswith("0:/"):
            path = path[3:]
        elif path[0] == "/":
            path[1:]

        if not path.startswith("gcodes/"):
            path = "gcodes/" + path
        await self.file_manager.delete_file(path)

    def _get_sd_file_info(self, arg_string: Optional[str] = None) -> None:
        response: Dict[str, Any] = {}
        filename: Optional[str] = arg_string
        sd_status = self.printer_state.get("virtual_sdcard", {})
        print_stats = self.printer_state.get("print_stats", {})
        if filename is None:
            # TFT is requesting file information on a
            # currently printed file
            active = False
            if sd_status and print_stats:
                filename = print_stats['filename']
                active = sd_status['is_active']
            if not filename or not active:
                # Either no file printing or no virtual_sdcard
                response['err'] = 1
                self.write_response(response)
                return
            else:
                response['fileName'] = filename.split("/")[-1]

        # For consistency make sure that the filename begins with the
        # "gcodes/" root.  The M20 HACK should add this in some cases.
        # Ideally we would add support to the TFT firmware that
        # indicates Moonraker supports a "gcodes" directory.
        if filename[0] == "/":
            filename = filename[1:]
        if not filename.startswith("gcodes/"):
            filename = "gcodes/" + filename

        metadata: Dict[str, Any] = \
            self.file_manager.get_file_metadata(filename)
        if metadata:
            response['err'] = 0
            response['size'] = metadata['size']
            # workaround for TFT replacing the first "T" found
            response['lastModified'] = "T" + time.ctime(metadata['modified'])
            slicer: Optional[str] = metadata.get('slicer')
            if slicer is not None:
                response['generatedBy'] = slicer
            height: Optional[float] = metadata.get('object_height')
            if height is not None:
                response['height'] = round(height, 2)
            layer_height: Optional[float] = metadata.get('layer_height')
            if layer_height is not None:
                response['layerHeight'] = round(layer_height, 2)
            filament: Optional[float] = metadata.get('filament_total')
            if filament is not None:
                response['filament'] = [round(filament, 1)]
            est_time: Optional[float] = metadata.get('estimated_time')
            if est_time is not None:
                response['printTime'] = int(est_time + .5)
        else:
            response['err'] = 1
        self.write_response(response)

    def _get_long_path(self, arg_string: Optional[str] = None) -> None:
        filename: Optional[str] = arg_string
        if filename is None:
            self.write_response(error="Missing filename\nok")
            return

        # Clean up the filename
        filename = filename.strip('\"')
        if filename.startswith("0:/"):
            filename = filename[3:]
        elif filename[0] == "/":
            filename[1:]

        if not filename.startswith("gcodes/"):
            filename = "gcodes/" + filename

        self.write_response(f"{filename}\nok")

    def _set_temperature_report(self, arg_s: int) -> None:
        interval = arg_s
        if interval > 0:
            if self.temperature_report_task:
                self.temperature_report_task.cancel()
            self.temperature_report_task = self.event_loop.create_task(
                self.report(f"ok {TEMPERATURE_TEMPLATE}", interval)
            )
        else:
            if self.temperature_report_task:
                self.temperature_report_task.cancel()
                self.temperature_report_task = None
        self.write_response("ok")

    def _set_position_report(self, arg_s: int) -> None:
        interval = arg_s
        if interval > 0:
            if self.position_report_task:
                self.position_report_task.cancel()
            self.position_report_task = self.event_loop.create_task(
                self.report(POSITION_TEMPLATE, interval)
            )
        else:
            if self.position_report_task:
                self.position_report_task.cancel()
                self.position_report_task = None
            self.write_response("ok")

    def _set_print_status_report(self, arg_s: int) -> None:
        interval = arg_s
        if interval > 0:
            if self.position_report_task:
                self.position_report_task.cancel()
            self.position_report_task = self.event_loop.create_task(
                self.report(PRINT_STATUS_TEMPLATE, interval)
            )
        else:
            if self.position_report_task:
                self.position_report_task.cancel()
                self.position_report_task = None
            self.write_response("ok")

    def _report_software_endstops(self) -> str:
        filament_sensor_enabled = self.printer_state.get("filament_switch_sensor filament_sensor", {}).get("enabled", False)
        logging.info(f"Filament Sensor Enabled: {filament_sensor_enabled}")
        state = {
            "state": "On" if filament_sensor_enabled else "Off"
        }
        self.write_response(f"{Template(SOFTWARE_ENDSTOPS_TEMPLATE).render(**state)}\nok")

    def _report_settings(self, arg_s: Optional[str] = None) -> str:
        report = Template(REPORT_SETTINGS_TEMPLATE).render(
            **(
                self.printer_state |
                self.config
            )
        )
        self.write_response(f"{report}\nok")

    def _send_ok_response(self, **args: Dict[float]) -> str:
        self.write_response("ok")

    def _handle_m118_command(self,
                             arg_p: Optional[int] = None,
                             arg_a: Optional[int] = None,
                             arg_string: Optional[str] = None) -> str:
        if arg_p == 0:
            if arg_a == 1:
                self.write_response(f"// {arg_string}")
            else:
                self.write_response(f"echo:{arg_string}\nok" if arg_string else "ok")

    def _set_acceleration(self, **args: Dict[float]) -> None:
        acceleration = args.get("arg_x") or args.get("arg_y")
        cmd = f"SET_VELOCITY_LIMIT ACCEL={acceleration} ACCEL_TO_DECEL={acceleration / 2}"
        self.queue_task(cmd)

    def _set_velocity(self, **args: Dict[float]) -> None:
        velocity = args.get("arg_x") or args.get("arg_y")
        cmd = f"SET_VELOCITY_LIMIT VELOCITY={velocity}"
        self.queue_task(cmd)

    def _set_gcode_offset(self, **args: Dict[float]) -> None:
        offsets = []
        if 'arg_x' in args:
            offsets.append(f"X={args['arg_x']}")
        if 'arg_y' in args:
            offsets.append(f"Y={args['arg_y']}")
        if 'arg_z' in args:
            offsets.append(f"Z={args['arg_z']}")
        offset_str = " ".join(offsets)
        self.queue_task(f"SET_GCODE_OFFSET {offset_str}")
        self.write_response("ok")

    def _set_probe_offset(self, **args: Dict[float]) -> None:
        if not args:
            response = Template(PROBE_OFFSET_TEMPLATE).render(**(self.printer_state|self.config))
            self.write_response(f"{response}")
        self.write_response("ok")

    def _load_filament(self) -> str:
        params = {
            "length": 25,
            "extruder": 0,
            "zmove": 0
        }
        self.queue_task(self._handle_filament(params))

    def _unload_filament(self) -> str:
        params = {
            "length": -25,
            "extruder": 0,
            "zmove": 0
        }
        self.queue_task(self._handle_filament(params))

    def _handle_filament(self, args: Dict[str, Any]) -> str:
        length = args.get("length")
        extruder = args.get("extruder")
        zmove = args.get("zmove")
        cmd = [
            "G91",               # Relative Positioning
            f"G92 E{extruder}",  # Reset Extruder
            f"G1 Z{zmove} E{length} F{3*60}",  # Extrude or Retract
            "G92 E0"              # Reset Extruder
        ]
        return cmd

    def _probe_bed(self) -> str:
        cmd = "PROBE_ACCURACY"
        self.queue_task(cmd)

    def close(self) -> None:
        self.ser_conn.disconnect()
        if self.temperature_report_task:
            self.temperature_report_task.cancel()
        if self.position_report_task:
            self.position_report_task.cancel()
        msg = "\nTFT GCode Dump:"
        for i, gc in enumerate(self.debug_queue):
            msg += f"\nSequence {i}: {gc}"
        logging.debug(msg)

    def _set_feed_rate(self, arg_s: Optional[int] = None, arg_d: Optional[int] = None) -> None:
        if arg_s is not None:
            self.queue_task(f"M220 S{arg_s}")
        else:
            self.write_response(Template(f"{FEED_RATE_TEMPLATE}\nok").render(**self.printer_state))

    def _set_flow_rate(self, arg_s: Optional[int] = None, arg_d: Optional[int] = None) -> None:
        if arg_s is not None:
            self.queue_task(f"M221 S{arg_s}")
        else:
            self.write_response(Template(f"{FLOW_RATE_TEMPLATE}\nok").render(**self.printer_state))

    def _report_temperature(self) -> None:
        report = Template(TEMPERATURE_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _report_position(self) -> None:
        report = Template(POSITION_TEMPLATE).render(**self.printer_state)
        self.write_response(f"{report}\nok")

    def _report_firmware_info(self) -> None:
        report = Template(FIRMWARE_INFO_TEMPLATE).render(**(
            self.printer_state |
            { "machine_name": self.machine_name } |
            { "firmware_name": self.firmware_name }))
        self.write_response(f"{report}\nok")

    def _report_software_endstops(self) -> None:
        state = {"state": "On" if self.printer_state.get("filament_switch_sensor filament_sensor", {}).get("enabled", False) else "Off"}
        report = Template(SOFTWARE_ENDSTOPS_TEMPLATE).render(**state)
        self.write_response(f"{report}\nok")

    def _cancel_print(self) -> str:
        self.queue_task("CANCEL_PRINT")

    def _save_gcode_state(self) -> str:
        self.queue_task("SAVE_GCODE_STATE STATE=TFT")

    def _restore_gcode_state(self) -> str:
        self.queue_task("RESTORE_GCODE_STATE STATE=TFT")

    def _z_offset_apply_probe(self) -> List[str]:
        sd_state = self.printer_state.get("print_stats", {}).get("state", "standby")
        if sd_state in ("printing", "paused"):
            self.write_response(error="Not saved - Printing")
        else:
            self.queue_task(["Z_OFFSET_APPLY_PROBE", "SAVE_CONFIG"])

    def _firmware_restart(self) -> str:
        self.queue_task("FIRMWARE_RESTART")

def load_component(config: ConfigHelper) -> TFT:
    return TFTAdapter(config)
