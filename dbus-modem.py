#!/usr/bin/python3 -u

from argparse import ArgumentParser
from enum import IntEnum
import ipaddress
import os
import queue
import signal
import sys
import time
import threading
import traceback
from typing import NamedTuple
import serial
from gi.repository import GLib
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

import logging
log = logging.getLogger()

from datetime import datetime

VERSION = '0.21'

modem_settings = {
    'connect': ['/Settings/Modem/Connect', 1, 0, 1],
    'roaming': ['/Settings/Modem/RoamingPermitted', 0, 0, 1],
    'pin':     ['/Settings/Modem/PIN', '', 0, 0],
    'apn':     ['/Settings/Modem/APN', '', 0, 0],
    'user':    ['/Settings/Modem/User', '', 0, 0],
    'passwd':  ['/Settings/Modem/Password', '', 0, 0],
}

# connection script used by pppd
CHAT_SCRIPT = '/run/ppp/chat'

# file containing user/password for PPP authentication
AUTH_FILE = '/run/ppp/auth'

# time allowed for ppp interface to come up
PPP_TIMEOUT = 60

# max number of commands to queue
CMDQ_MAX = 15

WDOG_GPIO = 44

# models with save flag in gpio commands
GPIO_SAVE = [
    'SIMCOM_SIM5360E',
]

class XEnum(IntEnum):
    @classmethod
    def get(cls, val, default=None):
        if any(val == m.value for m in cls):
            return cls(val)
        elif default != None:
            return default
        return val

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

# Network mode codes returned by AT+CNSMOD
NET_MODE = {
    0:  'NONE',
    1:  'GSM',
    2:  'GPRS',
    3:  'EDGE',
    4:  'UMTS',
    5:  'HSDPA',
    6:  'HSUPA',
    7:  'HSPA',
    8:  'LTE',
    9:  'TDS-CDMA',
    10: 'TDS-HSDPA',
    11: 'TDS-HSUPA',
    12: 'TDS-HSPA',
    13: 'CDMA',
    14: 'EVDO',
    15: 'CDMA/EVDO',
    16: 'CDMA/LTE',
    23: 'eHRPD',
    24: 'CDMA/eHRPD',
    30: 'HSPA+',
}

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

class PPP_STATUS(IntEnum):
    DOWN            = 0
    INIT            = 1
    UP              = 2

def check_route(ifname='ppp0', ipv6=False):
    if ipv6:
        proc = '/proc/net/ipv6_route'
        name = 9
        dest = 0
    else:
        proc = '/proc/net/route'
        name = 0
        dest = 1

    try:
        with open(proc) as f:
            for line in f:
                r = line.split()
                if r[name] == ifname and int(r[dest], 16) == 0:
                    return True
    except:
        log.warning(traceback.format_exc())

    return False

def parse_ip(s):
    try:
        x = bytes(map(int, s.split('.')))
    except:
        return None

    if not any(x):
        return None

    return ipaddress.ip_address(x)

def ppp_service(up):
    flag = '-u' if up else '-d'
    os.system('svc %s /service/ppp /service/ppp/log' % flag)

def make_authfile(name, user, passwd):
    try:
        if not os.access(os.path.dirname(name), os.F_OK):
            os.mkdir(os.path.dirname(name))

        f = open(name, mode='w')
        if user and passwd:
            f.write('user %s\n' % user)
            f.write('password %s\n' % passwd)
        f.close()
    except Exception as e:
        log.error('Error writing auth file %s: %s', name, e)

def make_chatscript(name, pdp):
    try:
        if not os.access(os.path.dirname(name), os.F_OK):
            os.mkdir(os.path.dirname(name))

        f = open(name, mode='w')
        f.write('ABORT   ERROR\n')
        f.write("ABORT   'NO CARRIER'\n")
        f.write("''      ATZ\n")
        f.write('OK      AT+CGDATA="PPP",%d\n' % pdp)
        f.write("CONNECT ''\n")
        f.close()
    except Exception as e:
        log.error('Error writing chat script %s: %s', name, e)

class PDPContext(NamedTuple):
    cid: int
    pdp_type: str
    apn: str
    pdp_addr: str = ''
    d_comp: int = 0
    h_comp: int = 0
    ipv4_ctrl: int = 0
    emergency: int = 0

    @classmethod
    def create(cls, *args):
        return cls(int(args[0]), *args[1:4], *map(int, args[4:]))

    def __str__(self):
        return '{},"{}","{}","{}",{},{},{},{}'.format(*self)

class Modem(object):
    def __init__(self, dev, rate, debug=0):
        self.debug = debug
        self.thread = None
        self.ser = None
        self.dev = dev
        self.rate = rate
        self.line = None
        self.cmds = queue.Queue()
        self.lastcmd = None
        self.ready = False
        self.running = None
        self.registered = None
        self.roaming = None
        self.ppp = None
        self.ppp_time = None
        self.sim_status = None
        self.wdog = 0
        self.gpio_save = ''
        self.pdp = []
        self.pdp_cid = None
        self.pdp_act = []

    def error(self, msg):
        global mainloop

        log.error('%s, quitting' % msg)

        try:
            mainloop.quit()
            self.disconnect(True)

            self.running = False
            self.cmds.shutdown(True)
        except:
            quit(1)

    def readline(self):
        if self.line is None:
            self.line = bytearray()

        while True:
            c = self.ser.read()
            if c == b'\n':
                break
            elif c:
                self.line += c
            else:
                return None

        r = self.line.strip().decode()
        self.line = None

        return r

    def send(self, cmd):
        self.lastcmd = cmd
        self.ready = False

        log.debug('> %s' % cmd)
        try:
            self.ser.write(b'\r' + cmd.encode() + b'\r')
        except serial.SerialException:
            self.error('Write error')

    def cmd(self, cmds, limit=False):
        if limit and self.cmds.qsize() > CMDQ_MAX:
            return

        try:
            for c in cmds:
                self.cmds.put(c)
        except queue.ShutDown:
            pass

        self.ser.cancel_read()

    def modem_wait(self):
        try:
            self.ser.timeout = 10

            while True:
                if not self.ready:
                    self.send('AT')

                line = self.ser.readline().decode()

                # startup chatter complete
                if not line and self.ready:
                    break

                # modem not responding, keep trying
                if not line:
                    log.error('Timed out waiting for response')
                    continue

                line = line.strip()

                log.debug('< %s' % line)

                # command succeeded
                if line == 'OK':
                    self.ser.timeout = 5
                    self.ready = True

            self.ser.timeout = None

        except serial.SerialException:
            self.error('Setup error')
            return False

        return True

    def modem_init(self):
        self.cmd([
            'ATH',
            'AT+CGMM',
            'AT+CGSN',
            'AT+CMEE=1',
            'AT+CPIN?',
        ])

    def modem_update(self):
        self.cmd([
            'AT+CPIN?',
            'AT+CGPS?',
        ], limit=True)

        if self.sim_status != SIM_STATUS.READY:
            return

        self.cmd([
            'AT+COPS?',
            'AT+CNSMOD?',
            'AT+CSQ',
            'AT+CGACT?',
            'AT+CGATT?',
            'AT+CREG?',
            'AT+CGPADDR',
        ], limit=True)

    def wdog_init(self):
        self.cmd([
            'AT+CGDRT=%d,1' % WDOG_GPIO,
            'AT+CGSETV=%d,1' % WDOG_GPIO,
        ])

    def wdog_update(self):
        self.cmd(['AT+CGSETV=%d,%d%s' % (WDOG_GPIO, self.wdog, self.gpio_save)],
                 limit=True)
        self.wdog ^= 1

    def select_pdp(self):
        self.disconnect()
        self.pdp_cid = None
        self.cmd([
            'AT+CGATT=0',
            'AT+CGACT?',
            'AT+CGDCONT?',
        ])

    def find_pdp(self, types, apn):
        cl = []

        for i in range(len(self.pdp)):
            ctx = self.pdp[i]
            act = ctx.cid in self.pdp_act

            if ctx.emergency:
                continue

            try:
                pref = types.index(ctx.pdp_type)
            except ValueError:
                pref = 1000

            cl.append((not act, pref, ctx.apn != apn, i, ctx))

        return min(cl)[-1] if cl else None

    def update_pdp(self):
        defpdp = False
        apn = self.settings['apn']
        types = ['IP', 'IPV4V6', 'IPV6']
        ctx = self.find_pdp(types, apn)

        if not ctx:
            ctx = PDPContext.create(1, types[0], apn)
            defpdp = True

        if apn and apn != ctx.apn:
            apn = apn.strip()
            log.info('Overriding APN: "%s" -> "%s"', ctx.apn, apn)
            ctx = ctx._replace(apn=apn)
            defpdp = True

        if defpdp:
            log.info('Defining PDP context: %s', ctx)
            self.cmd(['AT+CGDCONT=%s' % str(ctx)])

        log.info('Using PDP context %d', ctx.cid)
        self.pdp_cid = ctx.cid
        self.cmd(['AT+CGATT=1'])

    def handle_echo(self, cmd):
        if cmd == '+CGACT?':
            self.pdp_act = []
            return

        if cmd == '+CGDCONT?':
            self.pdp = []
            return

    def handle_ok(self, cmd):
        if cmd == '+CGDCONT?':
            self.update_pdp()
            return

    def handle_resp(self, cmd, resp):
        if cmd == '+CGMM':
            self.dbus['/Model'] = resp
            if resp in GPIO_SAVE:
                self.gpio_save = ',0'
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
                pin = self.settings['pin']
                self.cmd(['AT+CPIN=%s' % pin])

            elif self.sim_status == SIM_STATUS.READY:
                if self.sim_status != prev_status:
                    if prev_status is not None:
                        log.info('SIM PIN accepted')
                    else:
                        log.info('SIM PIN not required')

            else:
                log.error('Unknown SIM-PIN status: %s' % resp)

            return

        v = list(map(lambda x: x.strip('"'), resp.split(',')))

        if cmd == '+CNSMOD':
            self.dbus['/NetworkType'] = NET_MODE[int(v[1])]
            return

        if cmd == '+CREG':
            prev = self.registered
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

            if self.registered and not prev:
                self.select_pdp()

            self.dbus['/RegStatus'] = stat
            self.dbus['/Roaming'] = self.roaming
            return

        if cmd == '+COPS':
            if len(v) < 3:
                return

            net = v[2]
            self.dbus['/NetworkName'] = net
            return

        if cmd == '+CSQ':
            self.dbus['/SignalStrength'] = int(v[0])
            return

        if cmd == '+CGACT':
            cid = int(v[0])
            act = int(v[1])

            if act:
                self.pdp_act.append(cid)

                if self.pdp_cid is not None and cid != self.pdp_cid:
                    self.cmd(['AT+CGACT=0,%d' % cid])

            return

        if cmd == '+CGATT':
            att = int(v[0])

            if att and self.pdp_cid is not None:
                if self.pdp_cid not in self.pdp_act:
                    self.cmd(['AT+CGACT=1,%d' % self.pdp_cid])
                self.update_connection()

            return

        if cmd == '+CGDCONT':
            ctx = PDPContext.create(*v)
            self.pdp.append(ctx)
            log.info('PDP context %s', ctx)
            return

        if cmd == '+CGPADDR':
            if int(v[0]) == self.pdp_cid:
                ip = parse_ip(v[1])

                if ip is not None:
                    ip = str(ip)

                self.dbus['/IP'] = ip

            return

        if cmd == '+CGPS':
            if int(v[0]) != 1:
                self.cmd(['AT+CGPS=1'])
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
            self.sim_status = SIM_STATUS.get(err, SIM_STATUS.BAD_PASSWD)
            self.dbus['/SimStatus'] = self.sim_status
            # clear stored PIN if incorrect
            if self.sim_status == SIM_STATUS.BAD_PASSWD:
                log.info('Wrong PIN, clearing stored value')
                self.settings['pin'] = ''

    def drain_resp(self):
        try:
            self.ser.timeout = 1

            while True:
                line = self.ser.readline().strip().decode()
                if not line:
                    break
                log.debug('< %s', line)
        except:
            self.error()
        finally:
            self.ser.timeout = None

    def run(self):
        if not self.modem_wait():
            return

        while True:
            if self.ready:
                try:
                    self.send(self.cmds.get(False))

                    if self.cmds.empty() and self.running is None:
                        self.running = True

                    self.cmds.task_done()
                except queue.Empty:
                    pass
                except queue.ShutDown:
                    break

            try:
                line = self.readline()
            except serial.SerialException:
                self.error('Read error')
                break

            if not line:
                continue

            log.debug('< %s' % line)

            if line.startswith('AT'):
                if line != self.lastcmd:
                    log.error('Unexpected command echo: %s' % line)
                    log.error('Last command was: %s' % self.lastcmd)
                    self.drain_resp()
                    self.ready = True
                    continue

            if line == 'ERROR' or line.startswith('+CME ERROR:'):
                self.handle_error(self.lastcmd.lstrip('AT'), line)
                self.ready = True
                continue

            if line == 'NO CARRIER' or line.startswith('+PPPD:'):
                continue

            p = line.split(': ', 1)

            if len(p) == 1:
                cmd = self.lastcmd.lstrip('AT')
                resp = p[0]
            else:
                cmd = p[0]
                resp = p[1]

            try:
                if line == 'OK':
                    self.handle_ok(cmd)
                elif line.startswith('AT'):
                    self.handle_echo(cmd)
                else:
                    self.handle_resp(cmd, resp)
            except:
                log.warning(traceback.format_exc())

            if line == 'OK':
                self.ready = True

    def connect(self):
        if not self.ppp:
            log.info('Starting pppd')
            make_authfile(AUTH_FILE,
                          self.settings['user'],
                          self.settings['passwd'])
            make_chatscript(CHAT_SCRIPT, self.pdp_cid)
            ppp_service(True)
            self.ppp = True
            self.ppp_time = time.time()

    def disconnect(self, force=False):
        if self.ppp or force:
            log.info('Stopping pppd')
            ppp_service(False)
            self.ppp = False
            self.ppp_time = None

    def connect_allowed(self):
        if self.settings['connect']:
            if self.roaming == False or self.settings['roaming']:
                return True

        return False

    def update_connection(self):
        connect = False

        if self.registered:
            connect = self.connect_allowed()

        if connect:
            self.connect()
        else:
            self.disconnect()

    def ppp_status(self):
        if not self.ppp:
            return PPP_STATUS.DOWN

        if check_route(ipv6=False):
            return PPP_STATUS.UP

        if check_route(ipv6=True):
            return PPP_STATUS.UP

        return PPP_STATUS.INIT

    def check_ppp(self):
        st = self.ppp_status()
        self.dbus['/PPPStatus'] = st
        self.dbus['/Connected'] = int(st == PPP_STATUS.UP)

        if self.ppp_time is not None and st != PPP_STATUS.UP:
            if time.time() - self.ppp_time > PPP_TIMEOUT:
                self.error('Timeout waiting for ppp')

    def setting_changed(self, setting, old, new):
        if not self.running:
            return

        if setting == 'connect' or setting == 'roaming':
            self.update_connection()
            return

        if setting == 'pin':
            self.cmd(['AT+CPIN?'])
            return

        if setting == 'apn':
            self.select_pdp()
            return

        if setting == 'user' or setting == 'passwd':
            self.disconnect()
            self.update_connection()
            return

    def set_debug(self, path, val):
        try:
            self.debug = int(val)
        except:
            return False

        log.setLevel(logging.DEBUG if self.debug else logging.INFO)

        return True

    def start(self):
        # make sure pppd is not running
        self.disconnect(True)

        self.dbus = VeDbusService('com.victronenergy.modem', register=False)
        self.dbus.add_path('/Model', None)
        self.dbus.add_path('/IMEI', None)
        self.dbus.add_path('/NetworkName', None)
        self.dbus.add_path('/NetworkType', None)
        self.dbus.add_path('/SignalStrength', None)
        self.dbus.add_path('/Roaming', None)
        self.dbus.add_path('/Connected', None)
        self.dbus.add_path('/IP', None)
        self.dbus.add_path('/SimStatus', None)
        self.dbus.add_path('/RegStatus', None)
        self.dbus.add_path('/PPPStatus', None)
        self.dbus.add_path('/Debug', self.debug, writeable=True,
                           onchangecallback=self.set_debug)
        self.dbus.register()

        log.info('Waiting for localsettings')
        self.settings = SettingsDevice(self.dbus.dbusconn, modem_settings,
                                       self.setting_changed, timeout=10)

        self.ser = serial.Serial(self.dev, self.rate)

        self.thread = threading.Thread(target=self.run)
        self.thread.start()

        self.modem_init()
        self.wdog_init()

        log.info('Waiting for modem to become ready')
        self.cmds.join()

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
            self.check_ppp()
        return True

def quit(n):
    global start
    log.info('End. Run time %s' % str(datetime.now() - start))
    ppp_service(False)
    os._exit(n)

def sigterm(s, f):
    global mainloop
    log.info('Signal received, terminating')
    mainloop.quit()

def main():
    global mainloop
    global start

    start = datetime.now()

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

    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    mainloop = GLib.MainLoop()

    signal.signal(signal.SIGINT, sigterm)
    signal.signal(signal.SIGTERM, sigterm)

    modem = Modem(args.serial, rate, args.debug)
    if not modem.start():
        return

    GLib.timeout_add(5000, modem.update)
    mainloop.run()

    quit(1)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        quit(1)
