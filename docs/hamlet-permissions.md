# Claude Code allow list for the hamlet bench

The bench sessions on hamlet stop for an Allow tap on nearly every command. This list lets them
run the K6 / IC-7100 bench tools without asking, inside the bench authorization (2026-11-07):
transmit, receive and flash on 70 cm, 430-450 MHz, CW ID as AG7EW, never airband, never write to
`/dev/ttyUSB2`. Anything outside it still prompts.

The rules are in [`hamlet-claude-settings.json`](hamlet-claude-settings.json). Jeff installs them
by hand. No session changes hamlet's settings.

## Install (Jeff, once)

1. On hamlet, `git fetch origin main && git checkout FETCH_HEAD` in `~/sotto`, so that
   `tools/k6_send.sh` exists.
2. Open `~/.claude/settings.json`, which is where the old `k6_update.py` rule is. Merge in the
   three lists from `docs/hamlet-claude-settings.json`: append each entry to `permissions.allow`,
   `permissions.ask` and `permissions.deny`, creating any list that isn't there yet. Then
   **delete the old rule `Bash(python3 tools/k6_update.py:*)`**. It lets `--trigger` run any
   shell command, and the new flash rule replaces it.
3. Start a fresh Remote Control session, because settings load at start. `/permissions` in that
   session shows the merged rules.

If you'd rather keep the rules to the sotto checkout only, put the same JSON in
`~/sotto/.claude/settings.local.json` instead. Claude Code doesn't share that file; add it to
`.git/info/exclude` so git leaves it alone.

## What runs without a prompt

| Rule | Why it's safe |
|---|---|
| `sh tools/k6_send.sh FILE.wav` | The keying helper (formerly the `send()` shell function), now a wrapper for `k6_bench.py send`. It takes the PTT lock, refuses unless `rigctl f` reads 430.010-449.990 MHz and `rigctl t` reads 0, refuses a WAV over 180 s, sends the CW ID first when the last one is over 8.5 min old, and always unkeys. It keys through rigctld on ttyUSB0 and opens no serial port. |
| `python3 tools/k6_bench.py send FILE.wav ...`, `id ...`, `status`, `paths`, `install`, `wavs` | The bench helpers (see below). `send` has the same checks as `k6_send.sh` (plus `--k6log`/`--freq`); `id` keys only on 70 cm or 2 m. |
| `systemctl --user status k6-idkeeper`, `restart k6-idkeeper`, `journalctl --user -u k6-idkeeper ...` | The CW ID keeper's unit. |
| `sh tools/hamlet_prune_worktrees.sh` | Lists the worktrees it could remove. It removes nothing without `--yes`, which prompts. |
| `rigctl -m 2 f`, `t`, `m`, `T 0` | Read-only queries and unkey. |
| `rigctl -m 2 F 440850000` | The only frequency it can tune to without a prompt. |
| `rigctl -m 2 M PKTFM 0`, `M PKTFM 12000`, `M PKTUSB 0` | The bench's data modes. |
| `python3 tools/k6_upload.py`, `k6_ifcap.py`, `k6_afsweep.py`, `k6_rxmap.py` (any args) | These key the IC-7100 themselves. Each one refuses unless the rig already reads its `--freq`, which defaults to 440.850; `k6_upload.py` accepts 440.850 only. The rig can only be tuned to 440.850 without a prompt. |
| `python3 tools/k6_ft8.py`, `k6_gp0.py`, `k6_pcmhash.py`, `k6_frames.py`, `k6_link_sim.py` | Offline: they make WAVs and decode logs. |
| `python -m modem.k6 tx / test / ctl / mod / cw / reports / rx / cal / fit / bootdump / keyimage` (also `python3 -m`) | These write WAV or image files only. They never key anything. |
| `python -m modem.k6_harness listen --k6 /dev/ttyUSB2 ...` | The logger, which only reads the port. |
| `python3 tools/k6_update.py IMAGE ... --trigger "sh ~/sotto-work/bin/k6-reset-trigger.sh" --window 0.6` | Flashing, only when the last `--trigger` is the reset script (`tools/k6-reset-trigger.sh`, installed there), which sends the reset packet through `k6_bench.py send`: it checks the frequency, takes the PTT lock and sends the CW ID when one is due. The tool writes to the cable only after `K6 RESET`, while the bootloader is listening. |
| `make -C c/k6`, `git fetch origin ...`, `git checkout FETCH_HEAD`, `mkdir -p ~/sotto-work/...`, `unzip -o ~/Downloads/aura-pager-*.zip ...`, `date +%s` | Housekeeping used in the bench keys. |

Reading files (`grep`, `tail`, `cat`, `head`) is already allowed by Claude Code itself.

## What still prompts

- **Ask:** `tools/k6_rxmap.py cal` (it tunes to 2 m points) and anything with
  `--south-of-line-a`. Both would transmit outside 430-450 MHz.
- **Blocked:** writes or redirects to `/dev/ttyUSB2`, any `--port /dev/ttyUSB2`, and `stty`,
  `screen`, `minicom`, `picocom`, `socat` or `cu` on ttyUSB2.
- **Not listed, so it prompts:** `modem.k6 send` (it keys over CI-V on any `--port`, with no
  frequency check), `modem.k6_harness sweep`, a bare `rigctl ... T 1` or `F` to anything else,
  `paplay`, `python3 -c ...`, `modem.k6 modkey` (it would overwrite the module key),
  `k6_bench.py idkeeper` by hand (the unit runs it), `install --unit`, and any old scratchpad
  helper.

`rigctl` takes several commands on one line (`rigctl -m 2 M PKTFM 0 T 1`), so its rules are
exact, with no `*`. A trailing `*` would let a mode change carry a key-up with it.

## Where bench files go on hamlet

Everything a bench session makes goes under `~/sotto-work/`, not loose in `~`. The checkout itself
stays at `~/sotto`, which is the folder Claude Code's project and Remote Control session use.

| Folder | What's in it |
|---|---|
| `~/sotto-work/images/k6-<sha>/` | Firmware images unpacked from a CI artifact |
| `~/sotto-work/runs/k6-prNN/`, `k6-prNNw/`, `k6-prNN.log` | One bench run: its images, WAVs and serial log |
| `~/sotto-work/logs/` | Serial logs and result notes that aren't tied to one run |
| `~/sotto-work/clips/`, `mods/`, `ci/`, `refdocs/` | Test audio, module WAVs, CI downloads, chip datasheets |
| `~/sotto-work/worktrees/sotto-<sha>/` | Extra checkouts pinned to an old commit |
| `~/sotto-work/aura/` | The Aura checkout with `tools/flash.py` (`k6_update.py --aura` default) |
| `~/sotto-work/bin/` | `k6_bench.py` and `k6-reset-trigger.sh`, copied from `tools/` by `k6_bench.py install` |
| `~/sotto-work/state/` | The PTT lock and the CW ID keeper's state (`ptt.lock`, `ptt.lock.last_tx`, `ptt.lock.last_id`, `txlog.txt`), `cwid.wav`, `reset.wav` |
| `~/sotto-work/private/` | The K6 boot ROM dump. Never copy it into a git checkout. |

The CW ID log is `~/sotto-work/logs/k6-cwid.txt`. `~/k6-cwid.txt` is a symlink to it, kept for the
old scratchpad drivers; the new helpers write the real path.

`SOTTO_WORK` names this folder for every tool (`k6_bench.py`, `k6-reset-trigger.sh`,
`k6_update.py --aura`); unset, it is `~/sotto-work`. The allow rules spell out `~/sotto-work`, so
keep the default on hamlet.

## The bench helpers: PTT lock and CW ID keeper

Until 2026-11-07 the CW ID keeper, `tx_test.py`, `cwid.py` and the PTT lock lived in one old Claude
session's scratchpad under `/tmp`, so a reboot would have broken flashing (the reset trigger ran
`tx_test.py` from there) and the station ID. They are now `tools/k6_bench.py`:

- **One PTT lock**, `~/sotto-work/state/ptt.lock` (`flock`). `k6_send.sh`, `k6_bench.py send/id`,
  the keeper, the reset trigger, `k6_upload.py`, `k6_ifcap.py`, `k6_afsweep.py` and `k6_rxmap.py`
  all key through it, so a keying tool waits out the keeper's ID instead of failing on `t=1`.
  Before, none of the repo tools took the lock.
- **Every key-up is recorded**: a data send sets `ptt.lock.last_tx` and adds a `txlog.txt` line, a
  CW ID sets `ptt.lock.last_id` and adds a `k6-cwid.txt` line. That's all the keeper reads.
- **The keeper** (FCC 97.119): while a data TX ended under 10 min ago, it IDs 9 min after the last
  ID; 2 min after the last data TX it sends a final ID unless one already followed it (not for a
  TX over 30 min old, so a restart doesn't ID for a run from before a reboot). It keys only on
  70 cm or 2 m. It runs as the systemd user unit `k6-idkeeper` from `~/sotto-work/bin/k6_bench.py`,
  so it survives reboots and no session's timeout ends it (one did on 2026-11-05).
- `k6_bench.py` uses only the standard library, so the copy in `~/sotto-work/bin` keeps working
  whatever commit `~/sotto` has checked out. Run `install` again after the helper changes.

The old scratchpad helpers map to:

| Old (`/tmp/.../dee6ae60.../scratchpad`) | Now |
|---|---|
| `idkeeper.py` | the `k6-idkeeper` unit (`k6_bench.py idkeeper`) |
| `tx_test.py F 1800 0.3 WAV` (`K6LOG=...`) | `python3 tools/k6_bench.py send WAV --k6log LOG` |
| `tx_test.py PHY BAUD LEVEL` (test message) | `python -m modem.k6 test ...` to a WAV, then `send` |
| `cwid.py WHY` | `python3 tools/k6_bench.py id WHY` |
| `ptt.lock`, `ptt.lock.last_*`, `txlog.txt`, `cwid.wav` | the same names in `~/sotto-work/state/` |
| `~/sotto-work/bin/k6-reset-trigger.sh` | the same path, now installed from `tools/k6-reset-trigger.sh` |

`wait_send.py`, `auto_sweep.py` and the `drive*.py` run drivers were one run's tools and aren't
ported. Their default logs (`~/k6-live*.txt`) are gone since the tidy-up anyway.

### Switching over (once, with the bench idle)

The old keeper and the old lock stay in use until this is done, and an old scratchpad script
uses the old lock, so stop using them once the new keeper runs.

1. In `~/sotto`: `git fetch origin main && git checkout FETCH_HEAD`.
2. `python3 tools/k6_bench.py install --unit`: copies `k6_bench.py` and `k6-reset-trigger.sh` to
   `~/sotto-work/bin`, makes `state/cwid.wav` and `state/reset.wav`, writes
   `~/.config/systemd/user/k6-idkeeper.service`.
3. Carry the keeper's state over, so it knows when the last ID went:
   `cp /tmp/claude-1000/-home-baitisj-sotto/dee6ae60-19a7-5c92-9ca8-1466cff6d19d/scratchpad/ptt.lock.last_* ~/sotto-work/state/`
4. Stop the old keeper by its pid (`pgrep -af scratchpad/idkeeper.py`, then `kill PID`; `pkill -f`
   can match its own shell).
5. `systemctl --user daemon-reload && systemctl --user enable --now k6-idkeeper`, then
   `python3 tools/k6_bench.py status` should say `keeper running`.
6. For the keeper (and `rigctld.service`) to start at boot before anyone logs in:
   `loginctl show-user baitisj -p Linger`; if it says `no`, `sudo loginctl enable-linger baitisj`.
7. Optional: `sh tools/hamlet_prune_worktrees.sh` lists the extra worktrees that are clean and
   fully on GitHub; `--yes` removes them. It leaves any under a `/tmp` scratchpad that a process
   still runs from.

## Notes for the sessions on hamlet

- Run from `~/sotto` and write commands literally, one per call: `python3 tools/k6_rxmap.py map A
  --bin ~/sotto-work/runs/k6-rxmap/mod-rxmap.bin --key-file ~/.config/sotto/k6-mod.key`, not `$R map A ...`.
  Claude Code matches the command text, and a leading `KEY=...` assignment stops a rule from
  matching.
- Use `sh tools/k6_send.sh ctl.wav` wherever the bench keys say `send ctl.wav`. A shell function
  doesn't survive from one command to the next anyway.
- `a && b` runs without a prompt only when every part matches a rule. For example,
  `python -m modem.k6 ctl ctl.wav ... && sh tools/k6_send.sh ctl.wav` works.
- These rules aren't a sandbox. A Python tool can still open any port. They keep the prompts to the
  commands that leave 70 cm or touch the PTT line.
