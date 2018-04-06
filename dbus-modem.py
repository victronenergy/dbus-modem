#! /usr/bin/python -u

import os
import sys
import threading
import serial
import gobject
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService
from settingsdevice import SettingsDevice

modem_settings = {
    'connect': ['/Settings/Modem/Connect', 1, 0, 1],
    'roaming': ['/Settings/Modem/RoamingPermitted', 0, 0, 1],
}

WDOG_GPIO = 44

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
        self.roaming = None
        self.connected = False
        self.wdog = 0

    def error(self, msg):
        global mainloop

        print('%s, quitting' % msg)

        mainloop.quit()
        self.disconnect()

        with self.cv:
            self.running = False
            self.cv.notify()

    def send(self, cmd):
        self.lastcmd = cmd
        self.ready = False

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

            while True:
                line = self.ser.readline()
                if not line:
                    break
                line = line.strip()
                if line == 'PB DONE':
                    break

            self.ser.timeout = None

        except serial.SerialException:
            self.error('Setup error')
            return False

        return True


    def modem_init(self):
        self.cmd([
            'ATE0',
            'AT+CGMM',
            'AT+CGSN',
            'AT+CGPS=1',
        ])

    def modem_update(self):
        self.cmd([
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

        v = resp.split(',')

        if cmd == '*CNTI':
            self.dbus['/NetworkType'] = v[1]
            return

        if cmd == '+CREG':
            self.roaming = int(v[1]) == 5
            self.dbus['/Roaming'] = self.roaming
            return

        if cmd == '+COPS':
            net = v[2].strip('"')
            self.dbus['/NetworkName'] = net
            return

        if cmd == '+CSQ':
            self.dbus['/SignalStrength'] = int(v[0])
            return

        if cmd == '+CGACT':
            self.connected = int(v[1])
            self.dbus['/Connected'] = self.connected
            return

        if cmd == '+CGPADDR':
            ip = v[1]
            if ip == '0.0.0.0':
                ip = None
            self.dbus['/IP'] = ip
            return

    def run(self):
        if not self.modem_wait():
            return

        self.modem_init()
        self.modem_update()
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

            if line in ['OK', 'ERROR', 'NO CARRIER']:
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
                pass

    def connect(self):
        if not self.roaming or self.settings['roaming']:
            os.system('svc -u /service/ppp')

    def disconnect(self):
        os.system('svc -d /service/ppp')

    def setting_changed(self, setting, old, new):
        if not self.running:
            return

        if setting == 'connect':
            if new:
                self.connect()
            else:
                self.disconnect()
            return

        if setting == 'roaming':
            if self.connected and not new:
                self.disconnect()
            return

    def start(self):
        self.ser = serial.Serial(self.dev, self.rate)

        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.thread.start()

        print('Waiting for localsettings')
        self.settings = SettingsDevice(self.dbus.dbusconn, modem_settings,
                                       self.setting_changed, timeout=10)

        print('Waiting for modem to become ready')
        with self.cv:
            while self.running == None:
                self.cv.wait()

        if self.running:
            print('Modem ready')
            if self.settings['connect']:
                self.connect()
        else:
            print('Modem setup failed')

        return self.running

    def update(self):
        if self.running:
            self.modem_update()
            self.wdog_update()
        return True

def main(argv):
    global mainloop

    if len(argv) != 1:
        exit(1)

    tty = argv[0]
    rate = 115200

    print('Starting dbus-modem on %s at %d bps' % (tty, rate))

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

    modem = Modem(svc, tty, rate)
    if not modem.start():
        return

    gobject.timeout_add(5000, modem.update)
    mainloop.run()

main(sys.argv[1:])
