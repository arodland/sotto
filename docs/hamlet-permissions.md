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
| `sh tools/k6_send.sh FILE.wav` | The keying helper (formerly the `send()` shell function). It refuses unless `rigctl f` reads 430.010-449.990 MHz and `rigctl t` reads 0, caps play at 180 s, and always unkeys. It keys through rigctld on ttyUSB0 and opens no serial port. |
| `rigctl -m 2 f`, `t`, `m`, `T 0` | Read-only queries and unkey. |
| `rigctl -m 2 F 440850000` | The only frequency it can tune to without a prompt. |
| `rigctl -m 2 M PKTFM 0`, `M PKTFM 12000`, `M PKTUSB 0` | The bench's data modes. |
| `python3 tools/k6_upload.py`, `k6_ifcap.py`, `k6_afsweep.py`, `k6_rxmap.py` (any args) | These key the IC-7100 themselves. Each one refuses unless the rig already reads its `--freq`, which defaults to 440.850; `k6_upload.py` accepts 440.850 only. The rig can only be tuned to 440.850 without a prompt. |
| `python3 tools/k6_ft8.py`, `k6_gp0.py`, `k6_pcmhash.py`, `k6_frames.py`, `k6_link_sim.py` | Offline: they make WAVs and decode logs. |
| `python -m modem.k6 tx / test / ctl / mod / cw / reports / rx / cal / fit / bootdump / keyimage` (also `python3 -m`) | These write WAV or image files only. They never key anything. |
| `python -m modem.k6_harness listen --k6 /dev/ttyUSB2 ...` | The logger, which only reads the port. |
| `python3 tools/k6_update.py IMAGE ... --trigger "sh ~/sotto-work/bin/k6-reset-trigger.sh" --window 0.6` | Flashing, only when the last `--trigger` is the reset script, which checks the frequency and sends the CW ID. The tool writes to the cable only after `K6 RESET`, while the bootloader is listening. |
| `make -C c/k6`, `git fetch origin ...`, `git checkout FETCH_HEAD`, `mkdir -p ~/sotto-work/...`, `unzip -o ~/Downloads/aura-pager-*.zip ...`, `date +%s` | Housekeeping used in the bench keys. |

Reading files (`grep`, `tail`, `cat`, `head`) is already allowed by Claude Code itself.

## What still prompts

- **Ask:** `tools/k6_rxmap.py cal` (it tunes to 2 m points) and anything with
  `--south-of-line-a`. Both would transmit outside 430-450 MHz.
- **Blocked:** writes or redirects to `/dev/ttyUSB2`, any `--port /dev/ttyUSB2`, and `stty`,
  `screen`, `minicom`, `picocom`, `socat` or `cu` on ttyUSB2.
- **Not listed, so it prompts:** `modem.k6 send` (it keys over CI-V on any `--port`, with no
  frequency check), `modem.k6_harness sweep`, a bare `rigctl ... T 1` or `F` to anything else,
  `paplay`, `python3 -c ...`, `modem.k6 modkey` (it would overwrite the module key), and the
  scratchpad helpers such as the CW ID keeper. The keeper prompts once when it starts, which is
  fine for a long-running job.

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
| `~/sotto-work/bin/` | `k6-reset-trigger.sh` and the helpers it needs |
| `~/sotto-work/private/` | The K6 boot ROM dump. Never copy it into a git checkout. |

The CW ID log stays reachable at `~/k6-cwid.txt` (a symlink into `logs/`), because the CW ID keeper
and the run drivers append to that path.

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
