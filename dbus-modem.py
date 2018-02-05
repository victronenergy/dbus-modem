#! /usr/bin/python

import os
import sys
import threading
import serial
import gobject
import dbus
import dbus.mainloop.glib
from vedbus import VeDbusService

class Modem(threading.Thread):
    def __init__(self, dbussvc, dev, rate):
        threading.Thread.__init__(self)
        self.dbus = dbussvc
        self.lock = threading.Lock()
        self.ser = serial.Serial(dev, rate)
        self.cmds = []
        self.lastcmd = None
        self.ready = True
        self.roaming = False

    def send(self, cmd):
        self.lastcmd = cmd
        self.ready = False
        self.ser.write('\r' + cmd + '\r')

    def cmd(self, cmd):
        self.lock.acquire()
        if self.ready:
            self.send(cmd)
        else:
            self.cmds.append(cmd)
        self.lock.release()

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
            self.dbus['/Connected'] = int(v[1])
            return

        if cmd == '+CGPADDR':
            ip = v[1]
            if ip == '0.0.0.0':
                ip = None
            self.dbus['/IP'] = ip
            return

    def run(self):
        self.cmd('ATE0')
        self.cmd('AT+CGMM')
        self.cmd('AT+CGSN')
        self.cmd('AT+CGPS=1')

        while True:
            self.lock.acquire()
            if self.ready and self.cmds:
                self.send(self.cmds.pop(0))
            self.lock.release()

            line = self.ser.readline().strip()
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

    def update_status(self):
        self.cmd('AT+CREG?')
        self.cmd('AT+COPS?')
        self.cmd('AT*CNTI?')
        self.cmd('AT+CSQ')
        self.cmd('AT+CGACT?')
        self.cmd('AT+CGPADDR')
        return True

class ModemControl(dbus.service.Object):
    def __init__(self, modem, dbussvc, path):
        dbus.service.Object.__init__(self, dbussvc.dbusconn, path)
        self.modem = modem
        self.dbus = dbussvc

    @dbus.service.method('com.victronenergy.ModemControl', out_signature='b')
    def Connect(self):
        if self.modem.roaming and not self.dbus['/RoamingPermitted']:
            return False

        err = os.system('pon')

        return err == 0

    @dbus.service.method('com.victronenergy.ModemControl')
    def Disconnect(self):
        os.system('poff')

def main(argv):
    if len(argv) != 1:
        exit(1)

    gobject.threads_init()
    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    svc = VeDbusService('com.victronenergy.modem')

    # status
    svc.add_path('/Model', None)
    svc.add_path('/IMEI', None)
    svc.add_path('/NetworkName', None)
    svc.add_path('/NetworkType', None)
    svc.add_path('/SignalStrength', None)
    svc.add_path('/Roaming', None)
    svc.add_path('/Connected', None)
    svc.add_path('/IP', None)

    # settings
    svc.add_path('/RoamingPermitted', False, writeable=True)

    modem = Modem(svc, argv[0], 115200)
    modem.daemon = True
    modem.start()

    gobject.timeout_add(5000, modem.update_status)

    ModemControl(modem, svc, '/Control')

    mainloop = gobject.MainLoop()
    mainloop.run()

main(sys.argv[1:])
