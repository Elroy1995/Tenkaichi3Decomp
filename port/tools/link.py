#!/usr/bin/env python3
"""Links the PC / iOS build: every object from undefined.py, plus generated stubs for whatever is still missing.
A stub prints its name and stops the program, so running the result shows what the game needs next.
Usage: port/tools/undefined.py && port/tools/link.py     -> port/build/bt3"""
import os, pathlib, re, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "port/tools"))
from toolchain import ROOT, VARIANT, WIN, IOS, PREFIX, CLANG_TARGET, OBJ, DATA, EXE, sdl3

B = ROOT / "port/build"

def find_mvk_flags():
    mvk_root = ROOT / "mvk"
    for fw in mvk_root.rglob("MoltenVK.framework"):
        if "ios" in str(fw).lower() or not any("macos" in part.lower() for part in fw.parts):
            return ["-F", str(fw.parent), "-framework", "MoltenVK"]
    for lib in mvk_root.rglob("libMoltenVK.a"):
        if "ios" in str(lib).lower() or not any("macos" in part.lower() for part in lib.parts):
            return [str(lib)]
    return []

def get_brew_llvm_bin():
    try:
        prefix = subprocess.check_output(["brew", "--prefix", "llvm"], text=True).strip()
        p = pathlib.Path(prefix) / "bin"
        if p.exists():
            return p
    except Exception:
        pass
    for p in [pathlib.Path("/opt/homebrew/opt/llvm/bin"), pathlib.Path("/usr/local/opt/llvm/bin")]:
        if p.exists():
            return p
    return None

def link(objs, extra):
    if WIN:
        common = ["-Wl,--defsym=D_3BE71C=0x3BE71C", "-Wl,--wrap=Progress_Main", f"-Wl,-Map={EXE}.map"]
        lib = [f"-L{sdl3() / 'lib'}"] if sdl3() else []
        cmd = [PREFIX + "gcc", "-w", "-o", str(EXE)] + extra + objs + lib + \
              ["-lSDL3", "-lm", "-lwinmm", "-lws2_32", "-lwinhttp", "-static-libgcc", "-Wl,-Bstatic", "-lstdc++", "-lwinpthread", "-Wl,-Bdynamic",
               "-Wl,--image-base,0x20000000", "-Wl,--disable-dynamicbase", "-Wl,--disable-reloc-section", "-Wl,--disable-high-entropy-va", "-Wl,--wrap=main",
               "-Wl,--stack,0x1000000", "-mwindows"] + common
        argsfile = B / "link.rsp"
        argsfile.write_text("\n".join('"' + a.replace('\\', '/').replace('"', '\\"') + '"' for a in cmd[1:]))
        return subprocess.run([cmd[0], "@" + str(argsfile)], capture_output=True, text=True)

    if IOS:
        sdk_path = subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
        sdl_dir = ROOT / "ios-deps/sdl3"
        mvk_flags = find_mvk_flags()
        llvm_bin = get_brew_llvm_bin()

        lld_bin = str(llvm_bin / "ld64.lld") if (llvm_bin and (llvm_bin / "ld64.lld").exists()) else shutil.which("ld64.lld")

        # Invoke ld64.lld directly to bypass Apple ld64's relocation restrictions
        cmd = [
            lld_bin,
            "-arch", "arm64",
            "-platform_version", "ios", "15.0", "15.0",
            "-syslibroot", sdk_path,
            "-image_base", "0x20000000",
            "-pagezero_size", "0x10000",
            "-o", str(EXE)
        ] + extra + objs + [
            "-L" + str(sdl_dir / "lib"),
            "-lSDL3",
            "-lSystem",
            "-lc++"
        ] + mvk_flags + [
            "-framework", "Metal",
            "-framework", "Foundation",
            "-framework", "UIKit",
            "-framework", "QuartzCore",
            "-framework", "AudioToolbox",
            "-framework", "CoreGraphics",
            "-framework", "CoreHaptics",
            "-framework", "GameController",
            "-framework", "AVFoundation"
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    # Linux
    m = "-m64" if VARIANT == "clang64" else "-m32"
    base64 = ["-Wl,-Ttext-segment=0x20000000", "-Wl,--wrap=main"] if VARIANT == "clang64" else []
    common = ["-Wl,--defsym=D_3BE71C=0x3BE71C", "-Wl,--wrap=Progress_Main", f"-Wl,-Map={EXE}.map"]
    return subprocess.run(["gcc", m, "-no-pie", "-w", "-o", str(EXE)] + extra + objs +
                          ["-static-libstdc++", "-static-libgcc", "-Wl,-Bstatic", "-lstdc++", "-Wl,-Bdynamic", "-lm", "-lpthread", "-lSDL3", "-rdynamic",
                           "-Wl,-rpath,$ORIGIN/lib"] + common + base64, capture_output=True, text=True)

def generate_mem_file():
    mem_file = B / (EXE.name + ".mem")
    nm_bin = "llvm-nm" if IOS else PREFIX + "nm"
    try:
        out = subprocess.run([nm_bin, "-n", str(EXE)], capture_output=True, text=True, check=True).stdout
        mem_file.write_text(out)
        print(f"Generated memory layout map: {mem_file.name}")
    except Exception as e:
        print(f"Warning: could not generate {mem_file.name}: {e}")

def main():
    objs = [str(o) for o in sorted(OBJ.glob("*.o")) + sorted(DATA.glob("*.o"))]
    r = link(objs, [])

    need = set(re.findall(r"undefined reference to `(\w+)'", r.stderr))
    for m in re.finditer(r'"_(\w+)", referenced from:', r.stderr):
        need.add(m.group(1))
    for m in re.finditer(r"undefined symbol:\s*(?:_)?(\w+)", r.stderr):
        need.add(m.group(1))

    clean_need = sorted(need)

    lines = ['/* Generated by port/tools/link.py: one stub per symbol the build does not provide yet. */',
             'extern int printf(const char *, ...);', 'extern void exit(int);',
             'static void missing(const char *n) { printf("bt3: not implemented yet: %s\\n", n); exit(3); }']
    lines += [f'void {s}(void) {{ missing("{s}"); }}' for s in clean_need]
    (B / "gen").mkdir(parents=True, exist_ok=True)
    (B / "gen/stubs.c").write_text("\n".join(lines) + "\n")
    (B / "stubs.txt").write_text("\n".join(clean_need) + "\n")

    stubs_obj = str(B / "gen/stubs.o")
    if IOS:
        sdk_path = subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
        llvm_bin = get_brew_llvm_bin()
        cc = str(llvm_bin / "clang") if (llvm_bin and (llvm_bin / "clang").exists()) else "clang"
        subprocess.run([cc, "--target=arm64-apple-ios15.0", "-isysroot", sdk_path, "-c", str(B / "gen/stubs.c"), "-o", stubs_obj], check=True)
        r = link(objs, [stubs_obj])
    else:
        r = link(objs, [str(B / "gen/stubs.c")])

    print(f"{len(clean_need)} stubs; link {'OK -> ' + str(EXE.relative_to(ROOT)) if r.returncode == 0 else 'FAILED'}")
    if r.returncode != 0:
        print("--- Linker Errors ---")
        print(r.stderr)
        sys.exit(1)

    generate_mem_file()

if __name__ == "__main__":
    main()
