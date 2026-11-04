#!/usr/bin/env python3
"""Stack frame sizes of a Cortex-M0 (thumbv6m) ELF, from its disassembly: each function's
prologue (push, sub sp, #n, and ldr rX, =-n; add sp, rX), largest first. For the K6 pager,
whose stack ran out on the radio: shows which of Aura's and the decoder's functions hold it.

  llvm-objdump -d -C --no-show-raw-insn aura.elf | python3 tools/k6_frames.py [N]
  python3 tools/k6_frames.py --sizes aura.elf      the App's and its parts' sizes (K6_SIZES)

Only the prologue (the first 24 instructions) is read, so alloca-style growth later in a
function is missed; thumbv6m code from rustc and clang doesn't do that. SPDX-License-Identifier: MIT
"""
import re
import sys

FUNC = re.compile(r"^([0-9a-f]+) <(.+)>:$")
INSN = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)$")
WORD = re.compile(r"^\s*([0-9a-f]+):.*\.word\s+0x([0-9a-f]+)")
LDR_PC = re.compile(r"(r\d+), \[pc, #(-?0x[0-9a-f]+|-?\d+)\]")


def parse(lines):
    words, funcs, cur = {}, [], None
    for ln in lines:
        m = WORD.match(ln)
        if m:
            words[int(m.group(1), 16)] = int(m.group(2), 16)
            continue
        m = FUNC.match(ln.strip())
        if m:
            cur = [m.group(2), int(m.group(1), 16), []]
            funcs.append(cur)
            continue
        m = INSN.match(ln)
        if m and cur is not None and len(cur[2]) < 24:
            cur[2].append((int(m.group(1), 16), m.group(2), m.group(3).split("@")[0].strip()))
    return words, funcs


def frame(words, insns):
    size, lit = 0, {}
    for addr, op, args in insns:
        if op == "push":
            size += 4 * len(re.findall(r"r\d+|lr", args))
        elif op == "sub" and args.startswith("sp, #"):
            size += int(args.split("#")[1], 0)
        elif op == "ldr":
            m = LDR_PC.match(args)
            if m:
                # thumb: literal at Align(pc + 4, 4) + imm
                tgt = ((addr + 4) & ~3) + int(m.group(2), 0)
                if tgt in words:
                    lit[m.group(1)] = words[tgt]
        elif op == "add" and args.startswith("sp, r"):
            r = args.split(",")[1].strip()
            if r in lit:
                v = lit[r] - (1 << 32) if lit[r] & 0x80000000 else lit[r]
                if v < 0:
                    size += -v
        elif op in ("bl", "blx", "b", "bx", "pop"):
            break
    return size


def sizes(elf):
    """The image's K6_SIZES words (app/pager.rs): App, PagerState, Settings, Storage, Radio, Keypad."""
    import subprocess
    nm = subprocess.run(["llvm-nm", elf], capture_output=True, text=True).stdout
    addr = [int(ln.split()[0], 16) for ln in nm.splitlines() if ln.endswith(" K6_SIZES")]
    if not addr:
        return None
    a = addr[0]
    dump = subprocess.run(["llvm-objdump", "-s", f"--start-address={a:#x}", f"--stop-address={a + 24:#x}", elf],
                          capture_output=True, text=True).stdout
    data = b""
    for ln in dump.splitlines():
        parts = ln.split()
        if len(parts) > 1 and re.fullmatch(r"[0-9a-f]+", parts[0]) and int(parts[0], 16) >= a - 16:
            start = int(parts[0], 16)
            hexs = "".join(p for p in parts[1:5] if re.fullmatch(r"[0-9a-f]{2,8}", p))
            chunk = bytes.fromhex(hexs)
            data += chunk[max(0, a - start):] if start < a else chunk
    w = [int.from_bytes(data[i:i + 4], "little") for i in range(0, 24, 4)]
    return dict(zip(["App", "PagerState", "Settings", "Storage", "Radio", "Keypad"], w))


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--sizes":
        print("sizes:", sizes(sys.argv[2]))
        return
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    words, funcs = parse(sys.stdin)
    rows = sorted(((frame(words, f[2]), f[0]) for f in funcs), reverse=True)
    print(f"{'bytes':>6}  function (prologue only)")
    for size, name in rows[:n]:
        print(f"{size:6d}  {name[:110]}")


if __name__ == "__main__":
    main()
