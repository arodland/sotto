"""tools/k6_bench.py: the PTT lock, the CW ID keeper's rules and the state every keyer shares,
against a fake rig (no rigctld, no sound card, no serial port)."""
import fcntl
import importlib.util
import os
import subprocess
import sys
import threading
import time

import pytest

TOOLS = os.path.join(os.path.dirname(__file__), "..", "tools")
spec = importlib.util.spec_from_file_location("k6_bench", os.path.join(TOOLS, "k6_bench.py"))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


@pytest.fixture(autouse=True)
def work(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_WORK", str(tmp_path))
    monkeypatch.setattr(b, "play", lambda wav, device=None, dry_run=False, max_s=None: None)
    return tmp_path


class Rig:
    """rigctl as the bench sees it; shift/split model the IC-7100's DUP and split (stuck: R ignored)"""

    def __init__(self, hz=440850000, shift="None", split="0", stuck=False):
        self.hz, self.ptt, self.calls = hz, 0, []
        self.shift, self.split, self.stuck = shift, split, stuck

    def __call__(self, *args):
        self.calls.append(" ".join(str(v) for v in args))
        if args[0] == "f":
            return str(self.hz)
        if args[0] == "t":
            return str(self.ptt)
        if args[0] == "T":
            self.ptt = int(args[1])
        if args[0] == "R" and not self.stuck:
            self.shift = {"+": "+", "-": "-"}.get(str(args[1]), "None")
        if args[0] == "S":
            self.split = str(args[1])
        if args[0] == "r":
            return self.shift
        if args[0] == "s":
            return f"{self.split}\nVFOA"
        return ""


KEY = ["t", "V VFOA", "S 0 VFOA", "R 0", "r", "s", "T 1", "T 0"]


def test_paths_follow_sotto_work(work):
    assert b.work() == str(work)
    assert b.path("lock") == os.path.join(str(work), "state", "ptt.lock")
    assert b.path("cwid_log") == os.path.join(str(work), "logs", "k6-cwid.txt")


def test_default_is_sotto_work_in_home(monkeypatch):
    monkeypatch.delenv("SOTTO_WORK")
    assert b.work() == os.path.expanduser("~/sotto-work")


def test_key_and_play_records_data_and_id(work):
    r = Rig()
    b.key_and_play(r, "ctl.wav", what="ctl.wav")
    assert r.calls == KEY and r.ptt == 0
    assert b._read("last_tx") > 0 and b._read("last_id") == 0
    assert open(b.path("txlog")).read().split()[2] == "ctl.wav"
    b.key_and_play(r, "id.wav", kind="id", what="end of run", text="AG7EW")
    assert b._read("last_id") >= b._read("last_tx")
    assert "CW ID 'AG7EW' (end of run)" in open(b.path("cwid_log")).read()


def test_unkeys_when_play_fails(work, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "paplay")
    monkeypatch.setattr(b, "play", boom)
    r = Rig()
    with pytest.raises(subprocess.CalledProcessError):
        b.key_and_play(r, "x.wav")
    assert r.ptt == 0 and r.calls[-1] == "T 0"


def test_clears_repeater_shift_before_keying(work):
    r = Rig(shift="+", split="1")
    b.key_and_play(r, "x.wav")
    assert r.calls == KEY and r.shift == "None" and r.split == "0"


def test_refuses_when_shift_will_not_clear(work):
    r = Rig(shift="+", stuck=True)
    with pytest.raises(SystemExit):
        b.key_and_play(r, "x.wav")
    assert "T 1" not in r.calls and r.ptt == 0


def test_refuses_when_already_keyed():
    r = Rig()
    r.ptt = 1
    with pytest.raises(SystemExit):
        b.key_and_play(r, "x.wav")
    assert "T 1" not in r.calls


def test_dry_run_takes_no_lock_and_writes_no_state(work):
    b.key_and_play(Rig(), "x.wav", dry_run=True)
    assert not os.path.exists(os.path.join(str(work), "state"))


def test_lock_is_reentrant_and_shuts_out_other_processes(work):
    with b.ptt_lock():
        with b.ptt_lock():              # send's ID-first path nests the lock
            pass
        assert not b.lock_free()
        other = open(b.path("lock"), "a")    # another open file description: as another process
        with pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        other.close()
    assert b.lock_free()


def test_lock_wait_gives_up(work):
    os.makedirs(os.path.dirname(b.path("lock")), exist_ok=True)
    holder = open(b.path("lock"), "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    t = time.monotonic()
    with pytest.raises(SystemExit):
        with b.ptt_lock(wait=0.3):
            pass
    assert time.monotonic() - t < 2
    holder.close()


def test_keyer_waits_for_the_lock(work):
    os.makedirs(os.path.dirname(b.path("lock")), exist_ok=True)
    holder = open(b.path("lock"), "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    r, done = Rig(), []
    th = threading.Thread(target=lambda: (b.key_and_play(r, "x.wav"), done.append(1)))
    th.start()
    time.sleep(0.3)
    assert not done and r.calls == []       # held off while the keeper's ID is on the air
    holder.close()
    th.join(5)
    assert done and r.calls == KEY


@pytest.mark.parametrize("since_tx, since_id, why", [
    (30, 600, "9-min periodic"),          # active, last ID 10 min ago
    (30, 300, None),                      # active, ID 5 min ago
    (130, 400, "end of session"),         # quiet 2 min, no ID since the last TX
    (60, 400, None),                      # quiet 1 min: not yet
    (130, 100, None),                     # an ID came after the last TX
    (5000, 9000, None),                   # long idle
])
def test_keeper_rules(since_tx, since_id, why):
    now = 1.8e9
    assert b.keeper_due(now, now - since_tx, now - since_id) == why


def test_never_keyed_means_no_id():
    assert b.keeper_due(1.8e9, 0.0, 0.0) is None


def test_bands():
    assert b.in_band(440850000) and b.in_band(430010000)
    assert not b.in_band(425000000) and not b.in_band(146520000)     # south of Line A; 2 m
    assert b.in_band(146520000, b.BANDS_ID)
    assert not b.in_band(121500000, b.BANDS_ID)                      # airband: never


def test_k6_cfg_freq(tmp_path):
    log = tmp_path / "k6.log"
    log.write_text("1.0\tK6 CFG why=boot freq=440850000 phy=F\n2.0\tK6 PKT n=1\n"
                   "3.0\tK6 CFG why=key freq=440875000\n")
    assert b.k6_cfg_hz(str(log)) == 440875000
    log.write_text("K6 PKT n=1\n")
    assert b.k6_cfg_hz(str(log)) is None


def _send(work, monkeypatch, r, *args):
    wav = os.path.join(str(work), "s.wav")
    b.write_wav(wav, [0.0] * 4800)
    monkeypatch.setattr(b, "rigctl", lambda rigctld=None: r)
    monkeypatch.setattr(b, "cwid_wav", lambda: "cwid.wav")
    return b.main(["send", wav, *args])


def test_send_ids_first_when_due(work, monkeypatch):
    r = Rig()
    _send(work, monkeypatch, r)
    assert r.calls.count("T 1") == 2                  # the CW ID, then the data
    assert "before a data send" in open(b.path("cwid_log")).read()
    r.calls.clear()
    _send(work, monkeypatch, r)
    assert r.calls.count("T 1") == 1                  # ID just went: data only


@pytest.mark.parametrize("hz, args", [(425000000, []), (146520000, []), (440875000, ["--freq", "440850000"])])
def test_send_refuses_off_frequency(work, monkeypatch, hz, args):
    r = Rig(hz)
    with pytest.raises(SystemExit):
        _send(work, monkeypatch, r, *args)
    assert "T 1" not in r.calls


def test_send_checks_the_k6_log(work, monkeypatch, tmp_path):
    log = tmp_path / "k6.log"
    log.write_text("K6 CFG freq=440875000\n")
    r = Rig()
    with pytest.raises(SystemExit):
        _send(work, monkeypatch, r, "--k6log", str(log))
    log.write_text("K6 CFG freq=440850000\n")
    _send(work, monkeypatch, r, "--k6log", str(log), "--no-id")
    assert r.calls.count("T 1") == 1


def test_send_refuses_a_long_wav(work, monkeypatch):
    with pytest.raises(SystemExit):
        _send(work, monkeypatch, Rig(), "--max-s", "0.05")


def test_installed_copy_needs_only_the_standard_library():
    src = open(os.path.join(TOOLS, "k6_bench.py")).read()
    top = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert all(m.split()[1].split(".")[0] in sys.stdlib_module_names for m in top), top


def test_unit_runs_the_installed_copy(work):
    u = b.unit_text()
    assert f"ExecStart=/usr/bin/python3 {work}/bin/k6_bench.py idkeeper" in u
    assert f"Environment=SOTTO_WORK={work}" in u and "Restart=always" in u


def test_tools_key_through_the_bench():
    """every tool that keys the IC-7100 takes the shared lock; none keys on its own"""
    for f in ("k6_upload.py", "k6_ifcap.py", "k6_afsweep.py", "k6_rxmap.py"):
        s = open(os.path.join(TOOLS, f)).read()
        assert "bench.key" in s, f
        assert '"T", 1)' not in s, f
