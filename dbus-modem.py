#! /usr/bin/python -u

from argparse import ArgumentParser
from enum import IntEnum
import os
import sys
import time
import threading
import traceback
import serial
import gobject
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

import logging
log = logging.getLogger()

VERSION = '0.2'

modem_settings = {
    'connect': ['/Settings/Modem/Connect', 1, 0, 1],
    'roaming': ['/Settings/Modem/RoamingPermitted', 0, 0, 1],
    'pin':     ['/Settings/Modem/PIN', '', 0, 0],
    'apn':     ['/Settings/Modem/APN', '', 0, 0],
}

WDOG_GPIO = 44

class XEnum(IntEnum):
    @classmethod
    def get(cls, val):
        return cls(val) if any(val == m.value for m in cls) else val

class REG_STATUS(XEnum):
    # Status codes defined by 3GPP TS 27.007, section 7.2
    NREG                = 0
    HOME                = 1
    SEARCHING           = 2
    DENIED              = 3
    UNKNOWN             = 4
    ROAMING             = 5

class SIM_STATUS(XEnum):
    # Error codes defined by 3GPP TS 27.007, section 9.2
    PH_SIM_PIN      = 5
    PH_FSIM_PIN     = 6
    PH_FSIM_PUK     = 7
    NO_SIM          = 10
    SIM_PIN         = 11
    SIM_PUK         = 12
    SIM_FAIL        = 13
    SIM_BUSY        = 14
    SIM_WRONG       = 15
    BAD_PASSWD      = 16
    SIM_PIN2        = 17
    SIM_PUK2        = 18
    PH_NET_PIN      = 40
    PH_NET_PUK      = 41
    PH_NETSUB_PIN   = 42
    PH_NETSUB_PUK   = 43
    PH_SP_PIN       = 44
    PH_SP_PUK       = 45
    PH_CORP_PIN     = 46
    PH_CORP_PUK     = 47

    # other codes
    READY           = 1000
    ERROR           = 1001

CPIN = {
    'READY':          SIM_STATUS.READY,
    'SIM PIN':        SIM_STATUS.SIM_PIN,
    'SIM PUK':        SIM_STATUS.SIM_PUK,
    'PH-SIM PIN':     SIM_STATUS.PH_SIM_PIN,
    'PH-FSIM PIN':    SIM_STATUS.PH_FSIM_PIN,
    'PH-FSIM PUK':    SIM_STATUS.PH_FSIM_PUK,
    'SIM PIN2':       SIM_STATUS.SIM_PIN2,
    'SIM PUK2':       SIM_STATUS.SIM_PUK2,
    'PH-NET PIN':     SIM_STATUS.PH_NET_PIN,
    'PH-NET PUK':     SIM_STATUS.PH_NET_PUK,
    'PH-NETSUB PIN':  SIM_STATUS.PH_NETSUB_PIN,
    'PH-NETSUB PUK':  SIM_STATUS.PH_NETSUB_PUK,
    'PH-SP PIN':      SIM_STATUS.PH_SP_PIN,
    'PH-SP PUK':      SIM_STATUS.PH_SP_PUK,
    'PH-CORP PIN':    SIM_STATUS.PH_CORP_PIN,
    'PH-CORP PUK':    SIM_STATUS.PH_CORP_PUK
}

class Modem(object):
    def __init__(self, dbussvc, dev, rate):
        self.dbus = dbussvc
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.thread = None
        self.ser = None
        self.dev = dev
        self.rate = rate
        self.cmds = []
        self.lastcmd = None
        self.ready = True
        self.running = None
        self.registered = None
        self.roaming = None
        self.ppp = None
        self.sim_status = None
        self.wdog = 0

    def error(self, msg):
        global mainloop

        log.error('%s, quitting' % msg)

        mainloop.quit()
        self.disconnect(True)

        with self.cv:
            self.running = False
            self.cv.notify()

    def send(self, cmd):
        self.lastcmd = cmd
        self.ready = False

        log.debug('> %s' % cmd)
        try:
            self.ser.write('\r' + cmd + '\r')
        except serial.SerialException:
            self.error('Write error')

    def cmd(self, cmds):
        with self.lock:
            if self.ready and not self.cmds:
                self.send(cmds.pop(0))
            self.cmds += cmds

    def modem_wait(self):
        try:
            self.ser.timeout = 5

            # reset parameters to defaults
            self.send('AT&F')

            while True:
                line = self.ser.readline()

                # startup chatter complete
                if not line and self.ready:
                    break

                # modem not responding, attempt full reset
                if not line:
                    log.error('Timed out, resetting modem')
                    self.send('AT#REBOOT')
                    continue

                line = line.strip()

                log.debug('< %s' % line)

                # reset succeeded
                if line == 'OK' and self.lastcmd == 'AT&F':
                    self.send('ATH')
                    self.ready = True

            self.ser.timeout = None

        except serial.SerialException:
            self.error('Setup error')
            return False

        return True

    def modem_init(self):
        self.cmd([
            'AT+CGMM',
            'AT+CGSN',
            'AT+CGPS=1',
            'AT+CMEE=1',
            'AT+CPIN?',
        ])

    def modem_update(self):
        if self.sim_status != SIM_STATUS.READY:
            return

        self.cmd([
            'AT+CPIN?',
            'AT+CREG?',
            'AT+COPS?',
            'AT*CNTI?',
            'AT+CSQ',
            'AT+CGACT?',
            'AT+CGPADDR',
        ])

    def wdog_init(self):
        self.cmd([
            'AT+CGDRT=%d,1,0' % WDOG_GPIO,
            'AT+CGSETV=%d,1,0' % WDOG_GPIO,
        ])

    def wdog_update(self):
        self.cmd(['AT+CGSETV=%d,%d,0' % (WDOG_GPIO, self.wdog)])
        self.wdog ^= 1

    def handle_resp(self, cmd, resp):
        if cmd == '+CGMM':
            self.dbus['/Model'] = resp
            return

        if cmd == '+CGSN':
            self.dbus['/IMEI'] = resp
            return

        if cmd == '+CPIN':
            prev_status = self.sim_status
            self.sim_status = CPIN.get(resp, SIM_STATUS.ERROR)
            self.dbus['/SimStatus'] = self.sim_status

            if self.sim_status == SIM_STATUS.SIM_PIN:
                if not self.settings['pin']:
                    log.error('SIM PIN required but not configured: %s' % resp)
                    return

                log.info('SIM PIN required, sending')
                pin = self.settings['pin'].encode('ascii', 'ignore')
                self.cmd(['AT+CPIN=%s' % pin])

            elif self.sim_status == SIM_STATUS.READY:
                if self.sim_status != prev_status:
                    apn = self.settings['apn'].encode('ascii', 'ignore')
                    self.cmd(['AT+CGDCONT=1,"IP","%s"' % apn])

            else:
                log.error('Unknown SIM-PIN status: %s' % resp)

            return

        v = resp.split(',')

        if cmd == '*CNTI':
            self.dbus['/NetworkType'] = v[1]
            return

        if cmd == '+CREG':
            stat = REG_STATUS.get(int(v[1]))

            if stat == REG_STATUS.HOME:
                self.registered = True
                self.roaming = False
            elif stat == REG_STATUS.ROAMING:
                self.registered = True
                self.roaming = True
            else:
                self.registered = False
                self.roaming = False

            self.dbus['/RegStatus'] = stat
            self.dbus['/Roaming'] = self.roaming
            self.update_connection()
            return

        if cmd == '+COPS':
            if len(v) < 3:
                return

            net = v[2].strip('"')
            self.dbus['/NetworkName'] = net
            return

        if cmd == '+CSQ':
            self.dbus['/SignalStrength'] = int(v[0])
            return

        if cmd == '+CGACT':
            self.dbus['/Connected'] = int(v[1])
            return

        if cmd == '+CGPADDR':
            ip = v[1].strip('"')
            if ip == '0.0.0.0':
                ip = None
            self.dbus['/IP'] = ip
            return

    def handle_error(self, cmd, err):
        v = err.split(': ', 1)
        if len(v) > 1:
            err = v[1]

        log.error('%s: command failed: %s' % (cmd, err))

        try:
            err = int(err)
        except:
            # some errors are reported as strings, ignore failure
            pass

        if cmd.startswith('+CPIN'):
            self.sim_status = SIM_STATUS.get(err)
            self.dbus['/SimStatus'] = self.sim_status
            # clear stored PIN if incorrect
            if err == SIM_STATUS.BAD_PASSWD:
                log.info('Wrong PIN, clearing stored value')
                self.settings['pin'] = ''

    def run(self):
        if not self.modem_wait():
            return

        self.modem_init()
        self.wdog_init()

        while True:
            with self.cv:
                if self.ready and self.cmds:
                    self.send(self.cmds.pop(0))
                    if not self.cmds:
                        self.running = True
                        self.cv.notify()

            try:
                line = self.ser.readline().strip()
            except serial.SerialException:
                self.error('Read error')
                break

            if not line:
                continue

            log.debug('< %s' % line)

            if line.startswith('AT'):
                if line != self.lastcmd:
                    log.error('Unexpected command echo: %s' % line)
                continue

            if line == 'ERROR' or line.startswith('+CME ERROR:'):
                self.handle_error(self.lastcmd.lstrip('AT'), line)
                self.ready = True
                continue

            if line in ['OK', 'NO CARRIER']:
                self.ready = True
                continue

            p = line.split(': ', 1)

            if len(p) == 1:
                cmd = self.lastcmd.lstrip('AT')
                resp = p[0]
            else:
                cmd = p[0]
                resp = p[1]

            try:
                self.handle_resp(cmd, resp)
            except:
                log.debug(traceback.format_exc())
                pass

    def connect(self):
        if not self.ppp:
            log.debug('Starting pppd')
            os.system('svc -u /service/ppp')
            self.ppp = True

    def disconnect(self, force=False):
        if self.ppp or force:
            log.debug('Stopping pppd')
            os.system('svc -d /service/ppp')
            self.ppp = False

    def update_connection(self):
        connect = False

        if self.registered and self.settings['connect']:
            if not self.roaming or self.settings['roaming']:
                connect = True

        if connect:
            self.connect()
        else:
            self.disconnect()

    def setting_changed(self, setting, old, new):
        if not self.running:
            return

        if setting == 'connect' or setting == 'roaming':
            self.update_connection()
            return

        if setting == 'pin':
            self.cmd(['AT+CPIN?'])
            return

    def start(self):
        # make sure pppd is not running
        self.disconnect(True)

        log.info('Waiting for localsettings')
        self.settings = SettingsDevice(self.dbus.dbusconn, modem_settings,
                                       self.setting_changed, timeout=10)

        self.ser = serial.Serial(self.dev, self.rate)

        self.thread = threading.Thread(target=self.run)
        self.thread.start()

        log.info('Waiting for modem to become ready')
        with self.cv:
            while self.running == None:
                self.cv.wait()

        if self.running:
            log.info('Modem ready')
            self.modem_update()
        else:
            log.error('Modem setup failed')

        return self.running

    def update(self):
        if self.running:
            self.modem_update()
            self.wdog_update()
        return True

def main():
    global mainloop

    parser = ArgumentParser(description='dbus-modem', add_help=True)
    parser.add_argument('-d', '--debug', help='enable debug logging',
                        action='store_true')
    parser.add_argument('-s', '--serial', help='tty')

    args = parser.parse_args()

    logging.basicConfig(format='%(levelname)-8s %(message)s',
                        level=(logging.DEBUG if args.debug else logging.INFO))

    logLevel = {
        0:  'NOTSET',
        10: 'DEBUG',
        20: 'INFO',
        30: 'WARNING',
        40: 'ERROR',
    }
    log.info('Loglevel set to ' + logLevel[log.getEffectiveLevel()])

    if not args.serial:
        log.error('No serial port specified, see -h')
        exit(1)

    rate = 115200

    log.info('Starting dbus-modem %s on %s at %d bps' %
             (VERSION, args.serial, rate))

    gobject.threads_init()
    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    mainloop = gobject.MainLoop()

    svc = VeDbusService('com.victronenergy.modem')

    svc.add_path('/Model', None)
    svc.add_path('/IMEI', None)
    svc.add_path('/NetworkName', None)
    svc.add_path('/NetworkType', None)
    svc.add_path('/SignalStrength', None)
    svc.add_path('/Roaming', None)
    svc.add_path('/Connected', None)
    svc.add_path('/IP', None)
    svc.add_path('/SimStatus', None)
    svc.add_path('/RegStatus', None)

    modem = Modem(svc, args.serial, rate)
    if not modem.start():
        return

    gobject.timeout_add(5000, modem.update)
    mainloop.run()

try:
    main()
except KeyboardInterrupt:
    os._exit(1)
