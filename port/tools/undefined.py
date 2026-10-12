#!/usr/bin/env python3
"""Compile the game sources to 32-bit host objects and list what a link would still need, by kind.
Usage: port/tools/undefined.py [-v]   (objects go to port/build/obj)"""
import collections, concurrent.futures, os, pathlib, re, shutil, subprocess, sys
import portsrc
import eeconst

ROOT = pathlib.Path(__file__).resolve().parents[2]
from toolchain import VARIANT, WIN, IOS, BITS64, PREFIX, CLANG_TARGET, OBJ, DATA, sdl3

def variant(cmd):
    """The command for this build variant: clang for everything built with software float, 64-bit or Windows flags."""
    if BITS64 and not IOS:
        cmd = ["-m64" if a == "-m32" else a for a in cmd if a != "-malign-double"]
    if WIN:
        cmd = [a for a in cmd if a != "-m64"]
        inc = [f"-I{sdl3() / 'include'}"] if sdl3() else []
        if cmd[0] in ("gcc", "g++") and "-msoft-float" not in cmd:
            return [PREFIX + cmd[0]] + cmd[1:] + inc
    if IOS:
        strip_flags = {"-m32", "-m64", "-msoft-float", "-mno-sse", "-mno-mmx", "-malign-double", "-msse2", "-mfpmath=sse", "-fno-pic"}
        cmd = [a for a in cmd if a not in strip_flags]
        inc = [f"-I{sdl3() / 'include'}"] if sdl3() else []
        c0 = cmd[0]
        compiler = "clang++" if c0 in ("g++", "clang++") else "clang"
        return [compiler] + CLANG_TARGET + inc + cmd[1:] + ["-Wno-error=return-mismatch", "-Wno-return-type", "-fms-extensions"]
    if VARIANT == "gcc" or "-msoft-float" not in cmd:
        return cmd
    extra = ["-fms-extensions"] if BITS64 else []
    if WIN:
        extra += ["-mno-ms-bitfields", "-D__CRT__NO_INLINE"]
    return ["clang"] + CLANG_TARGET + cmd[1:] + ["-mno-x87", "-Wno-error=return-mismatch"] + extra

def run(cmd, **kw):
    return subprocess.run(variant(cmd), **kw)

CC = ["gcc", "-m32", "-std=gnu89", "-c", "-O2", "-g", "-fno-strict-aliasing", "-ffp-contract=off", "-fcommon", "-w", "-fno-pic", "-fno-stack-protector", "-fno-builtin", "-msoft-float", "-mno-sse", "-mno-mmx", "-malign-double", "-include", "port_libm.h",
      "-Iinclude", "-Iport/include", "-include", "port_compat.h"]
TEXT_END, GAME_END = 0x2BF6B0, 0x273CF0

def compile_ee(cmd, src, o):
    """Compiles src with the PS2 compiler's float constants: preprocess, eeconst.transform, compile the result."""
    cmd = variant(cmd)
    pre = [a for a in cmd if a != "-c"] + ["-E", str(src)]
    r = subprocess.run(pre, cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        return r
    i = pathlib.Path(str(o)[:-2] + ".i")
    i.write_text(eeconst.transform(r.stdout))
    # Only rewrite game files (src/ and include/), never host/port files
    is_game_file = not o.name.startswith(("nl_", "pc_", "gs_", "cxx_"))
    if BITS64 and cmd[0] in ("clang", "clang++") and is_game_file:
        import ptr32
        std = next((a for a in cmd if a.startswith("-std=")), "-std=gnu89")
        target_flags = CLANG_TARGET if IOS else (CLANG_TARGET or ["-m64"])
        try:
            text = ptr32.transform(str(i), ["-x", "cpp-output"] + target_flags + [std, "-fms-extensions", "-w", "-Wno-error=return-mismatch", "-Wno-return-type"] +
                                   (["-mno-ms-bitfields"] if WIN else []))
        except RuntimeError as e:
            return subprocess.CompletedProcess(cmd, 1, "", f"ptr32: {e}\n")
        i.write_text(text, encoding="latin-1")
    if cmd[0] in ("clang", "clang++"):
        c2, skip = [], False
        for a in cmd:
            if skip:
                skip = False
            elif a == "-include":
                skip = True
            else:
                c2.append(a)
        if BITS64 and is_game_file:
            import irfix
            ll = pathlib.Path(str(o)[:-2] + ".ll")
            r = subprocess.run(c2 + ["-S", "-emit-llvm", "-x", "cpp-output", str(i), "-o", str(ll)], cwd=ROOT, capture_output=True, text=True)
            if r.returncode:
                return r
            text, _ = irfix.fix(ll.read_text())
            ll.write_text(text)
            llc_args = ["llc", "-O2", "-filetype=obj"]
            if IOS:
                llc_args += ["-mtriple=arm64-apple-ios15.0", "-relocation-model=pic"]
            else:
                llc_args += ["-relocation-model=static"]
            return subprocess.run(llc_args + [str(ll), "-o", str(o)], cwd=ROOT, capture_output=True, text=True)
        return subprocess.run(c2 + ["-x", "cpp-output", str(i), "-o", str(o)], cwd=ROOT, capture_output=True, text=True)
    return subprocess.run(cmd + ["-fpreprocessed", str(i), "-o", str(o)], cwd=ROOT, capture_output=True, text=True)

def cc(f):
    o = OBJ / (str(f.relative_to(ROOT / "src")).replace("/", "_")[:-2] + ".o")
    src, done, missing = portsrc.prepare(f)
    r = compile_ee(CC + [f"-I{f.parent}"], src, o)
    if missing:
        print("no C for:", f.name, " ".join(missing))
    return o if r.returncode == 0 else None, f, r.stderr

def macho_convert_asm(text):
    """Converts GNU/ELF .section directives to Mach-O directives for Apple Clang as."""
    out = []
    for line in text.splitlines():
        l = line.strip()
        if l.startswith(".section"):
            if ".rodata" in l or ".lit4" in l:
                out.append("\t.const_data")
            elif ".sbss" in l or ".bss" in l:
                out.append("\t.data")
            elif ".sdata" in l or ".data" in l:
                out.append("\t.data")
            else:
                out.append("\t.data")
        else:
            out.append(line)
    return "\n".join(out) + "\n"

def main():
    OBJ.mkdir(parents=True, exist_ok=True)
    fs = portsrc.sources()
    with concurrent.futures.ProcessPoolExecutor(16) as ex:
        res = list(ex.map(cc, fs))
    bad = [(f, e) for o, f, e in res if o is None]
    for f, e in bad:
        print("FAILED", f.name, next((l for l in e.splitlines() if "rror" in l or l.startswith("ptr32")), e.strip()[:150])[:200])
    objs = [str(o) for o, _, _ in res if o]
    if BITS64:
        real = sorted((ROOT / "port/build/gen/data").glob("*.s"))
        blank = os.environ.get("BT3_SKELETON") == "1" or not real
        DATA.mkdir(exist_ok=True)
        for old in DATA.glob("*.o"):
            old.unlink()
        (DATA / "SKELETON").unlink(missing_ok=True)
        if blank:
            (DATA / "SKELETON").write_text("built from port/data: finish with make_dat.py\n")
        for f in (sorted((ROOT / "port/data").glob("*.s")) if blank else real):
            asm_file = f
            if IOS:
                converted_s = DATA / f.name
                converted_s.write_text(macho_convert_asm(f.read_text()))
                asm_file = converted_s
                as_cmd = ["clang"] + CLANG_TARGET + ["-c", str(asm_file), "-o", str(DATA / (f.name[:-2] + ".o"))]
            else:
                as_cmd = [PREFIX + "as"] + ([] if WIN else ["--64"]) + [str(f), "-o", str(DATA / (f.name[:-2] + ".o"))]
            r = subprocess.run(as_cmd, capture_output=True, text=True)
            if r.returncode:
                print("FAILED", f.name, r.stderr.splitlines()[0][:150] if r.stderr else "assembly error")
        objs += [str(o) for o in sorted(DATA.glob("*.o"))]
    else:
        objs += [str(o) for o in sorted((ROOT / "port/build/obj_data").glob("*.o"))]

    # Math library
    for f in sorted((ROOT / "port/third_party/newlib_libm").glob("*.c")):
        o = OBJ / ("nl_" + f.stem + ".o")
        r = run(["gcc", "-m32", "-std=gnu89", "-c", "-O2", "-g", "-fno-pic", "-msoft-float", "-mno-sse", "-mno-mmx",
                 "-malign-double", "-fno-strict-aliasing", "-ffp-contract=off", "-fno-builtin", "-w",
                 "-Iport/include", f"-I{f.parent}", "-include", "newlib_shim.h", str(f), "-o", str(o)], cwd=ROOT, capture_output=True, text=True)
        if r.returncode:
            print("FAILED", f.name, "\n".join(l for l in r.stderr.splitlines() if "error" in l)[:400] if r.stderr else "build error")
        else:
            objs.append(str(o))

    # Renderer shaders
    gen = ROOT / "port/build/gen/gs"
    gen.mkdir(parents=True, exist_ok=True)
    arrays = []
    nz = gen / "gsnz.frag"
    nz_text = (ROOT / "port/src/gs/shaders/gs.frag").read_text().replace("\n", "\n#define NO_FRAG_DEPTH\n", 1)
    if not nz.exists() or nz.read_text() != nz_text:
        nz.write_text(nz_text)
    shader_sources = sorted((ROOT / "port/src/gs/shaders").glob("*.*")) + [nz]
    for src in shader_sources:
        stage = src.suffix[1:]
        name = "k" + src.stem.capitalize() + stage.capitalize() + "Spv"
        spv = gen / (src.name + ".spv")
        if shutil.which("glslc") is None and spv.exists() and spv.stat().st_mtime >= src.stat().st_mtime:
            pass
        else:
            if shutil.which("glslc") is not None:
                r = subprocess.run(["glslc", f"-fshader-stage={stage}", str(src), "-o", str(spv)], capture_output=True, text=True)
            else:
                r = subprocess.run(["glslangValidator", "-V", "-S", stage, str(src), "-o", str(spv)], capture_output=True, text=True)
            if r.returncode:
                print("FAILED shader", src.name, r.stderr[:300])
                continue
        arrays.append(f"static const unsigned char {name}[] = {{" + ",".join(str(b) for b in spv.read_bytes()) + "};\n")

    for src in shader_sources:
        if src.suffix not in (".vert", ".frag"):
            continue
        stage = src.suffix[1:]
        name = "k" + src.stem.capitalize() + stage.capitalize() + "Glsl"
        data = src.read_bytes() + b"\0"
        arrays.append(f"static const unsigned char {name}[] = {{" + ",".join(str(b) for b in data) + "};\n")

    bars = "".join(f"static const unsigned char kBars{n.capitalize()}Png[] = {{" + ",".join(str(b) for b in (ROOT / "port/assets/bars" / (n + ".png")).read_bytes()) + "};\n"
                   for n in ("trim", "fill"))
    (gen / "bars_png.h").write_text("/* generated from port/assets/bars by port/tools/undefined.py */\n" + bars)
    (gen / "shaders.h").write_text("/* generated from port/src/gs/shaders by port/tools/undefined.py */\n" + "".join(arrays))

    for f in sorted((ROOT / "port/src/gs").glob("*.c")):
        o = OBJ / ("gs_" + f.stem + ".o")
        r = run(["gcc", "-m32", "-std=gnu99", "-c", "-O2", "-g", "-fno-pic", "-msse2", "-mfpmath=sse", "-fno-strict-aliasing",
                 "-Wall", "-Wno-unused", "-Wno-misleading-indentation", f"-I{gen}", str(f), "-o", str(o)], cwd=ROOT,
                capture_output=True, text=True)
        if r.returncode:
            print("FAILED", f.name, "\n".join(l for l in r.stderr.splitlines() if "error" in l)[:400])
        else:
            objs.append(str(o))

    imgui = ROOT / "port/third_party/imgui"
    for f in sorted(imgui.glob("*.cpp")) + sorted((ROOT / "port/src/gs").glob("*.cpp")):
        o = OBJ / ("cxx_" + f.stem + ".o")
        if f.parent == imgui and o.exists() and o.stat().st_mtime > f.stat().st_mtime:
            objs.append(str(o))
            continue
        r = run(["g++", "-m32", "-std=c++17", "-c", "-O2", "-g", "-fno-pic", "-msse2", "-mfpmath=sse", "-fno-exceptions",
                 "-fno-rtti", "-w", f"-I{imgui}", f"-I{ROOT / 'port/src/gs'}", f"-I{gen}", str(f), "-o", str(o)], cwd=ROOT,
                capture_output=True, text=True)
        if r.returncode:
            print("FAILED", f.name, "\n".join(l for l in r.stderr.splitlines() if "error" in l)[:600])
        else:
            objs.append(str(o))

    # Host platform code (port/src)
    for f in [ROOT / "src/port/vu0_a.c", ROOT / "src/port/vu0_b.c"] + sorted((ROOT / "port/src").glob("*.c")):
        o = OBJ / ("pc_" + f.stem + ".o")
        names = ["-include", "vu0_names.h", "-DREF_VU0_EXTERN_ARITH"] if f.parent.name == "port" and f.parent.parent.name == "src" else []
        soft = [] if f.name in ("plat_libm.c", "plat_fastvec.c") else ["-msoft-float", "-mno-sse", "-mno-mmx", "-DSF_FAST_CALLS", "-include", "math.h", "-include", "stdlib.h", "-include", "port_libm.h"]
        cmd = ["gcc", "-m32", "-std=gnu99", "-c", "-O2", "-g", "-fno-pic", "-malign-double", "-fno-strict-aliasing",
               "-ffp-contract=off", "-fno-builtin", "-w", "-Iinclude", "-Iport/include", "-Iport/src"] + soft + names
        if os.environ.get("BT3_PORT_CFLAGS"):
            cmd += os.environ["BT3_PORT_CFLAGS"].split()
        r = run(cmd + [str(f), "-o", str(o)], cwd=ROOT, capture_output=True, text=True)
        if r.returncode:
            print("FAILED", f.name, "\n".join(l for l in r.stderr.splitlines() if "error" in l)[:300] if r.stderr else r.stderr[:200])
        else:
            objs.append(str(o))

    defined, undef = set(), collections.Counter()
    argsfile = ROOT / "port/build/nm-objects.rsp"
    argsfile.write_text("\n".join('"' + p.replace('\\', '/') + '"' for p in objs))
    nm_bin = "llvm-nm" if IOS else PREFIX + "nm"
    out = subprocess.run([nm_bin, "-A", "@" + str(argsfile)], capture_output=True, text=True, check=True).stdout
    for l in out.splitlines():
        m = re.match(r"\S+:\s*([0-9a-f]*)\s+(\w)\s+(\S+)$", l)
        if not m:
            continue
        if m.group(2) == "U":
            undef[m.group(3)] += 1
        elif m.group(2) not in "tdbr":
            defined.add(m.group(3))
    addr = {}
    for p in (ROOT / "config/symbols").glob("*.txt"):
        if p.name.startswith("menu"):
            continue
        for l in p.read_text().splitlines():
            m = re.match(r"(\w+)\s*=\s*0x([0-9A-Fa-f]+)", l)
            if m:
                addr[m.group(1)] = int(m.group(2), 16)
    kinds = collections.defaultdict(list)
    for s in undef:
        if s in defined:
            continue
        a = addr.get(s)
        if a is None:
            m = re.match(r"(?:func|D|jtbl)_([0-9A-F]{8})$", s)
            a = int(m.group(1), 16) if m else None
        k = ("no address (host library or compiler helper)" if a is None else "game function" if a < GAME_END
             else "library function (SDK / CRI / libc)" if a < TEXT_END else "data")
        kinds[k].append(s)
    print(f"{len(objs)} objects, {len(defined)} global symbols defined")
    for k, v in sorted(kinds.items()):
        print(f"{len(v):5d}  {k}")
        if "-v" in sys.argv:
            print("       " + " ".join(sorted(v)))
    (ROOT / "port/build/undefined.txt").write_text(
        "".join(f"{k}\t{s}\n" for k, v in sorted(kinds.items()) for s in sorted(v)))

if __name__ == "__main__":
    main()
