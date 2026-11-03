"""Icom CI-V helpers for keying the IC-7100 (default CI-V address 0x88) and the live send.

Frames are FE FE <to> <from> <cmd> [<sub/data>...] FD. The ones used here:

  1C 00 01 / 1C 00 00   transmit / receive (PTT)
  06 05 01              mode FM, filter 1 (05 = FM)
  1A 06 01 01           data mode on, filter 1 (FM -> FM-D)   (check against your manual)

pyserial is only needed for a real port; frame building works without it.
"""
import time

IC7100 = 0x88
CONTROLLER = 0xE0


def frame(cmd, data=b"", to=IC7100, frm=CONTROLLER):
    return bytes([0xFE, 0xFE, to, frm, cmd]) + bytes(data) + b"\xFD"


def ptt(on, to=IC7100):
    return frame(0x1C, [0x00, 0x01 if on else 0x00], to)


def mode_fm(to=IC7100):
    return frame(0x06, [0x05, 0x01], to)


def data_mode(on=True, to=IC7100):
    return frame(0x1A, [0x06, 0x01 if on else 0x00, 0x01 if on else 0x00], to)


class Keyer:
    """PTT over a serial port: method 'civ' (CI-V frames), 'rts' or 'dtr' (line high = TX,
    as with the IC-7100's USB SEND setting or a Digirig on the DATA2 socket), or 'none'."""

    def __init__(self, method="civ", port=None, baud=19200, addr=IC7100):
        self.method, self.addr, self.ser = method, addr, None
        if method != "none":
            import serial              # pyserial
            self.ser = serial.Serial(port, baud, timeout=0.2)
            if method in ("rts", "dtr"):
                self._line(False)

    def _line(self, on):
        if self.method == "rts":
            self.ser.rts = on
        else:
            self.ser.dtr = on

    def send(self, b):
        self.ser.write(b)
        self.ser.flush()
        time.sleep(0.05)
        self.ser.reset_input_buffer()   # the radio echoes CI-V frames and answers FB / FA

    def key(self, on):
        if self.method == "civ":
            self.send(ptt(on, self.addr))
        elif self.method in ("rts", "dtr"):
            self._line(on)

    def setup_fm_data(self):
        if self.method == "civ":
            self.send(mode_fm(self.addr))
            self.send(data_mode(True, self.addr))

    def close(self):
        if self.ser is not None:
            try:
                self.key(False)
            finally:
                self.ser.close()
