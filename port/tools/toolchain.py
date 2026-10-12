"""Which build this is and which tools it uses; shared by undefined.py, link.py and strip_data.py.

    BT3_CC=gcc       (default) 32-bit, gcc                      -> port/build/bt3
    BT3_CC=clang     32-bit, the game code through clang        -> port/build/bt3_clang
    BT3_CC=clang64   64-bit Linux                               -> port/build/bt3_64
    BT3_CC=win64     64-bit Windows                             -> port/build/bt3.exe
    BT3_CC=ios64     64-bit iOS (Mach-O arm64)                  -> port/build/bt3_ios
"""
import os, pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
VARIANT = os.environ.get("BT3_CC", "gcc")
WIN = VARIANT == "win64"
IOS = VARIANT == "ios64"
BITS64 = VARIANT in ("clang64", "win64", "ios64")
ON_WINDOWS = os.name == "nt" or sys.platform in ("msys", "cygwin")
PREFIX = "x86_64-w64-mingw32-" if WIN and not ON_WINDOWS else ""

def get_ios_sdk():
    if not IOS:
        return ""
    try:
        return subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
    except Exception:
        return ""

IOS_SDK = get_ios_sdk() if IOS else ""

if WIN:
    CLANG_TARGET = ["--target=x86_64-w64-mingw32"]
elif IOS:
    CLANG_TARGET = ["--target=arm64-apple-ios15.0.0", "-miphoneos-version-min=15.0", "-isysroot", IOS_SDK]
else:
    CLANG_TARGET = []

OBJ = ROOT / ("port/build/obj" if VARIANT == "gcc" else "port/build/obj_" + VARIANT)
DATA = ROOT / ("port/build/obj_data" if not BITS64 else "port/build/obj_data64" if not WIN else "port/build/obj_data_win64")
EXE = ROOT / "port/build" / {"gcc": "bt3", "clang64": "bt3_64", "win64": "bt3.exe", "ios64": "bt3_ios"}.get(VARIANT, "bt3_" + VARIANT)

def sdl3():
    """The folder with SDL3's include/ and lib/."""
    if os.environ.get("BT3_SDL3"):
        return pathlib.Path(os.environ["BT3_SDL3"])
    if WIN:
        found = sorted((ROOT / "port/build/win").glob("SDL3-*/x86_64-w64-mingw32"))
        return found[-1] if found else None
    if IOS:
        ios_sdl = ROOT / "ios-deps/sdl3"
        return ios_sdl if ios_sdl.exists() else None
    return None
