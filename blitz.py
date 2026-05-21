#!/usr/bin/env python3
"""
Blitz Patcher
Downloads, installs, and patches Blitz to remove ads and disable auto-updates.
Patches are defined in the patches/ directory as JSON files.

Usage:
  blitz                        download + install + patch
  blitz install                download + install latest Blitz (no patching)
  blitz patch                  patch existing Blitz installation
  blitz patch <installer>      patch using a local installer file
  blitz update                 update blitz-cli itself
"""

import hashlib, json, os, platform, re, shutil, struct, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "requests"], check=True, capture_output=True)
    import requests


# ─── Paths ────────────────────────────────────────────────────────────────────

SYSTEM      = platform.system()   # "Windows" | "Darwin" | "Linux"
SCRIPT_DIR  = Path(__file__).parent
PATCHES_DIR = SCRIPT_DIR / "patches"

if SYSTEM == "Windows":
    BLITZ_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Blitz"
    APP_ASAR  = BLITZ_DIR / "resources" / "app.asar"
    INSTALLER_EXT = ".exe"
elif SYSTEM == "Darwin":
    BLITZ_DIR = Path("/Applications/Blitz.app")
    APP_ASAR  = BLITZ_DIR / "Contents" / "Resources" / "app.asar"
    INSTALLER_EXT = ".dmg"
else:  # Linux
    BLITZ_DIR = Path("/opt/Blitz")
    APP_ASAR  = BLITZ_DIR / "resources" / "app.asar"
    INSTALLER_EXT = ".deb"


# ─── Download & Install ───────────────────────────────────────────────────────

_UPDATE_BASE   = "https://blitz-main.blitz.gg"
_LATEST_YML    = {"Windows": "latest.yml", "Darwin": "latest-mac.yml"}

def get_installer_url() -> str:
    yml_name = _LATEST_YML.get(SYSTEM)
    if not yml_name:
        raise RuntimeError(
            "Cannot auto-detect the Linux installer URL.\n"
            "Download from https://blitz.gg/download and run:\n"
            "  blitz <path-to-installer>"
        )

    r = requests.get(f"{_UPDATE_BASE}/{yml_name}", timeout=15)
    r.raise_for_status()

    if SYSTEM == "Windows":
        m = re.search(r"^path:\s*(\S+\.exe)", r.text, re.MULTILINE)
    else:  # Darwin — prefer .dmg over .zip
        m = re.search(r"url:\s*(Blitz[^\s]+\.dmg)", r.text)

    if not m:
        raise RuntimeError(
            f"Could not parse installer filename from {yml_name}.\n"
            "Download from https://blitz.gg/download and run:\n"
            "  blitz <path-to-installer>"
        )

    return f"{_UPDATE_BASE}/{m.group(1).strip()}"



def _ok(label: str = "") -> str:
    return f"  [ok]  {label}" if label else "  [ok]"


def _warn(label: str) -> str:
    return f"  [!]  {label}"


def download_file(url: str, dest: Path):
    print(f"Downloading {dest.name} ...")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    print(f"\r  {pct:3d}%", end="", flush=True)
    size_mb = dest.stat().st_size / 1_048_576
    print(f"\r{_ok(f'{size_mb:.0f} MB')}  ")


def _install_windows(exe: Path):
    print("Installing ...", end="", flush=True)
    subprocess.run([str(exe), "/S"], check=True)
    for _ in range(60):
        if APP_ASAR.exists():
            print(_ok())
            return
        time.sleep(2)
    raise RuntimeError("Timed out — app.asar not found after install")


def _install_mac(dmg: Path):
    import plistlib
    print("Installing ...")
    print("  Mounting disk image ...", end="", flush=True)
    result = subprocess.run(
        ["hdiutil", "attach", str(dmg), "-nobrowse", "-noverify", "-plist"],
        capture_output=True, check=True,
    )
    print(_ok())
    # hdiutil may emit non-plist bytes before the XML; find where it starts
    stdout = result.stdout
    xml_start = stdout.find(b"<?xml")
    if xml_start == -1:
        xml_start = stdout.find(b"bplist")
    if xml_start > 0:
        stdout = stdout[xml_start:]
    info = plistlib.loads(stdout)
    mount_point = next(
        e["mount-point"]
        for e in info["system-entities"]
        if "mount-point" in e
    )
    try:
        app_src = Path(mount_point) / "Blitz.app"
        if BLITZ_DIR.exists():
            shutil.rmtree(BLITZ_DIR)
        shutil.copytree(app_src, BLITZ_DIR)
        print(_ok())
    finally:
        subprocess.run(["hdiutil", "detach", mount_point, "-quiet"])


def _install_linux(deb: Path):
    print("Installing ...", end="", flush=True)
    subprocess.run(["sudo", "dpkg", "-i", str(deb)], check=True)
    if not APP_ASAR.exists():
        raise RuntimeError(f"app.asar not found at {APP_ASAR} after install")
    print(_ok())


def install_blitz(installer: Path):
    if SYSTEM == "Windows":
        _install_windows(installer)
    elif SYSTEM == "Darwin":
        _install_mac(installer)
    else:
        _install_linux(installer)


# ─── Asar ─────────────────────────────────────────────────────────────────────

def _require(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name!r} not found. Is Node.js installed and on PATH?")
    return path


def extract_asar(src: Path, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_require("npx"), "--yes", "@electron/asar", "extract", str(src), str(dest)],
        check=True, capture_output=True,
    )


def repack_asar(src: Path, out: Path):
    with tempfile.TemporaryDirectory() as d:
        npm_dir = Path(d)
        (npm_dir / "package.json").write_text("{}")
        subprocess.run(
            [_require("npm"), "install", "@electron/asar"],
            cwd=str(npm_dir), check=True, capture_output=True,
        )
        asar_lib = npm_dir / "node_modules/@electron/asar/lib/asar.js"
        s = str(src).replace("\\", "/")
        o = str(out).replace("\\", "/")
        a = str(asar_lib).replace("\\", "/")
        unpack = "node_modules/**/*.{node,dll}"
        script = (
            f"require('{a}').createPackageWithOptions('{s}','{o}',"
            f"{{unpack:'{unpack}',unpackDir:'node_modules/lzma-native'}}"
            f").then(()=>process.exit(0))"
            f".catch(e=>{{console.error(e);process.exit(1)}});"
        )
        subprocess.run([_require("node"), "-e", script], check=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


_HEX_RE = re.compile(rb' ([0-9a-f]{64})\x00')

def _find_embedded_hash(data: bytes):
    for m in _HEX_RE.finditer(data):
        candidate = m.group(1)
        # Filter out low-entropy false positives (e.g. sequential byte patterns)
        if len(set(candidate)) > 8:
            return candidate
    return None


def patch_index_node(new_hash: str):
    new_b = new_hash.encode("ascii")
    binaries_dir = BLITZ_DIR / "resources" / "binaries"
    primary = binaries_dir / "index.node"
    targets = [primary] if primary.exists() else []

    if SYSTEM == "Windows":
        deps_base = Path(os.environ["APPDATA"]) / "Blitz" / "blitz-deps"
        if deps_base.exists():
            for ver_dir in deps_base.iterdir():
                candidate = ver_dir / "index.node"
                if candidate.exists():
                    targets.append(candidate)

    for target in targets:
        data = target.read_bytes()
        old_b = _find_embedded_hash(data)
        if not old_b:
            print(_warn(f"{target.name}: no embedded hash found — skipping"))
            continue
        if old_b == new_b:
            print(_ok(f"{target.name}: already up to date"))
            continue
        target.write_bytes(data.replace(old_b, new_b, 1))
        print(_ok(target.name))


def _patch_core_data(data: bytearray) -> tuple:
    """
    Locate the VerifyApp function by finding the 'VerifyApp' string literal and
    tracing the RIP-relative LEA instruction that loads it, then walking back to
    the CC-padded function boundary. Returns (changed: bool, message: str).

    This approach is version-stable: the string must exist for the function to
    work, so it survives recompilation even when the surrounding machine code
    changes completely.
    """
    # ── PE header parsing ──────────────────────────────────────────────────────
    if len(data) < 0x40 or data[0:2] != b'MZ':
        return False, "not a PE/MZ file"
    pe_off = struct.unpack_from('<I', data, 0x3c)[0]
    if data[pe_off:pe_off+4] != b'PE\0\0':
        return False, "invalid PE signature"
    coff     = pe_off + 4
    nsec     = struct.unpack_from('<H', data, coff + 2)[0]
    opt_size = struct.unpack_from('<H', data, coff + 16)[0]
    opt      = coff + 20
    if struct.unpack_from('<H', data, opt)[0] != 0x20b:
        return False, "not a PE32+ binary"
    image_base = struct.unpack_from('<Q', data, opt + 24)[0]

    # Section table: each entry is 40 bytes
    # +8 VirtualSize, +12 VirtualAddress (RVA), +16 SizeOfRawData, +20 PointerToRawData
    sec_base = opt + opt_size
    sections = []
    for i in range(nsec):
        s      = sec_base + i * 40
        vsize  = struct.unpack_from('<I', data, s + 8)[0]
        vaddr  = struct.unpack_from('<I', data, s + 12)[0]
        rsize  = struct.unpack_from('<I', data, s + 16)[0]
        roff   = struct.unpack_from('<I', data, s + 20)[0]
        sections.append((vaddr, max(vsize, rsize), roff, rsize))

    def file_to_va(off):
        for vaddr, span, roff, rsize in sections:
            if roff <= off < roff + rsize:
                return image_base + vaddr + (off - roff)
        return None

    # ── Find "VerifyApp" string (ASCII then UTF-16LE) ─────────────────────────
    raw = bytes(data)
    str_off = raw.find(b'VerifyApp\x00')
    if str_off == -1:
        str_off = raw.find('VerifyApp\x00'.encode('utf-16-le'))
    if str_off == -1:
        return False, "VerifyApp string not found in binary"
    str_va = file_to_va(str_off)
    if str_va is None:
        return False, "VerifyApp string not mapped to any PE section"

    # ── Find LEA reg, [RIP+disp32] instructions that reference the string ─────
    # Encoding: 48 8D <ModRM> <disp32>  where ModRM byte 0x05 field = RIP-relative
    # Valid ModRM bytes for RIP-relative: 05,0D,15,1D,25,2D,35,3D
    LEA_MODRM = {0x05, 0x0D, 0x15, 0x1D, 0x25, 0x2D, 0x35, 0x3D}
    func_starts = set()

    for i in range(len(data) - 7):
        if data[i] == 0x48 and data[i+1] == 0x8D and data[i+2] in LEA_MODRM:
            disp = struct.unpack_from('<i', data, i + 3)[0]
            instr_va = file_to_va(i)
            if instr_va is None:
                continue
            # RIP points to the next instruction (7 bytes after LEA start)
            if instr_va + 7 + disp == str_va:
                # Walk backward from the LEA to find the CC-padded function
                # boundary. Require 2+ consecutive CC bytes to distinguish
                # real inter-function padding from lone INT3s in code.
                pos = i - 1
                found = False
                while pos > 1:
                    if data[pos] == 0xCC and data[pos - 1] == 0xCC:
                        found = True
                        break
                    pos -= 1
                if not found:
                    continue
                # Go back to the start of the CC run
                while pos > 0 and data[pos - 1] == 0xCC:
                    pos -= 1
                # Advance past CC bytes — that's the function entry point
                candidate = pos
                while candidate < i and data[candidate] == 0xCC:
                    candidate += 1
                # Sanity-check: common x64 prologue first bytes, or 0xC3 if
                # already patched to RET by a previous run.
                if data[candidate] in (0x40, 0x41, 0x48, 0x53, 0x55, 0x56, 0x57, 0xB8, 0xC3):
                    func_starts.add(candidate)

    if not func_starts:
        return False, "could not locate VerifyApp function via string reference"

    # MOV EAX, 1 (B8 01 00 00 00) + RET (C3) — return "verified ok" so callers
    # don't interpret a false/garbage return as verification failure and disable
    # overlay injection or other features gated on this check.
    STUB = b'\xB8\x01\x00\x00\x00\xC3'

    changed = False
    msg_parts = []
    for fs in func_starts:
        if bytes(data[fs:fs + len(STUB)]) == STUB:
            msg_parts.append(f"already patched at 0x{fs:x}")
            continue
        for k, b in enumerate(STUB):
            data[fs + k] = b
        # NOP the rest of the prologue bytes up to the next CC boundary
        j = fs + len(STUB)
        while j < len(data) and data[j] != 0xCC:
            data[j] = 0x90
            j += 1
            if j - fs >= 32:  # cap at 32 bytes; don't NOP into the whole function
                break
        msg_parts.append(f"patched at 0x{fs:x}")
        changed = True

    return changed, "; ".join(msg_parts)


def patch_blitz_core():
    binaries_dir = BLITZ_DIR / "resources" / "binaries"
    primary = binaries_dir / "blitz_core.node"
    targets = [primary] if primary.exists() else []

    if SYSTEM == "Windows":
        deps_base = Path(os.environ["APPDATA"]) / "Blitz" / "blitz-deps"
        if deps_base.exists():
            for ver_dir in deps_base.iterdir():
                candidate = ver_dir / "blitz_core.node"
                if candidate.exists():
                    targets.append(candidate)

    for target in targets:
        data = bytearray(target.read_bytes())
        changed, msg = _patch_core_data(data)
        if changed:
            target.write_bytes(bytes(data))
            print(_ok(target.name))
        elif "already patched" in msg:
            print(_ok(f"{target.name}: already up to date"))
        else:
            print(_warn(f"{target.name}: {msg}"))


def _resign_mac():
    fw = BLITZ_DIR / "Contents/Frameworks/Electron Framework.framework"

    # Remove the original Apple signature — it's invalidated by the asar repack
    subprocess.run(["codesign", "--remove-signature", str(BLITZ_DIR)], capture_output=True)

    # Sign dylibs inside Electron Framework
    libs = fw / "Versions/A/Libraries"
    for dylib in libs.glob("*.dylib"):
        subprocess.run(["codesign", "--force", "--sign", "-", str(dylib)],
                       check=True, capture_output=True)

    # Sign Helper .app bundles
    helpers_dir = BLITZ_DIR / "Contents/Frameworks"
    for helper in helpers_dir.glob("*.app"):
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(helper)],
                       check=True, capture_output=True)

    # Sign the main bundle last (subcomponents already signed)
    subprocess.run(["codesign", "--force", "--sign", "-", str(BLITZ_DIR)],
                   check=True, capture_output=True)

    # Clear quarantine so Gatekeeper doesn't block first launch
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(BLITZ_DIR)],
                   capture_output=True)


# ─── Patch engine ─────────────────────────────────────────────────────────────

def apply_patch(src: Path, patch: dict):
    path = src / patch["file"]
    text = path.read_text("utf-8")
    kind = patch["type"]

    if kind == "replace":
        old = patch["find"]
        new = patch["replace"]
        if old not in text:
            raise RuntimeError(f"find string not found in {patch['file']}")
        text = text.replace(old, new, 1)

    elif kind == "insert_after_regex":
        m = re.search(patch["pattern"], text, re.DOTALL)
        if not m:
            raise RuntimeError(f"pattern not matched in {patch['file']}")
        pos = m.end()
        text = text[:pos] + patch["insert"] + text[pos:]

    else:
        raise RuntimeError(f"Unknown patch type: {kind!r}")

    path.write_text(text, "utf-8")


def apply_all_patches(src: Path):
    patch_files = list(PATCHES_DIR.glob("*.json"))
    if not patch_files:
        return

    patches = [(json.loads(pf.read_text("utf-8")), pf) for pf in patch_files]
    patches.sort(key=lambda x: x[0].get("priority", 0))

    for patch, pf in patches:
        try:
            apply_patch(src, patch)
            print(_ok(pf.stem))
        except Exception as e:
            print(_warn(f"{pf.stem}: {e}"))


# ─── Self-update ──────────────────────────────────────────────────────────────

def self_update():
    install_dir = Path(__file__).parent
    url = "https://github.com/carlelieser/blitz-cli/archive/refs/heads/main.zip"

    print("Downloading update ...")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zip_path = tmp / "blitz-cli.zip"

        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        print(" [ok]")

        print("Installing update ...", end="", flush=True)
        extract_dir = tmp / "extract"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        src = extract_dir / "blitz-cli-main"
        shims = {"blitz.cmd", "blitz"}  # never overwrite platform shims
        upstream_names = {item.name for item in src.iterdir()}
        for item in src.iterdir():
            dest = install_dir / item.name
            if item.name in shims:
                continue
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        # Remove local items that no longer exist upstream
        for item in install_dir.iterdir():
            if item.name in shims:
                continue
            if item.name not in upstream_names:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        print(" [ok]")


def patch_app():
    patch_files = list(PATCHES_DIR.glob("*.json"))

    if patch_files:
        print("Extracting app.asar ...", end="", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "asar-src"
            extract_asar(APP_ASAR, src)
            print(_ok())

            print("Applying patches ...")
            apply_all_patches(src)

            print("Repacking app.asar ...", end="", flush=True)
            repack_asar(src, APP_ASAR)
            print(_ok())

    print("Patching binaries ...")
    new_hash = _sha256_file(APP_ASAR)
    patch_index_node(new_hash)
    patch_blitz_core()

    if SYSTEM == "Darwin":
        print("Re-signing app bundle ...", end="", flush=True)
        _resign_mac()
        print(_ok())

    print("Done.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    command = args[0] if args else None

    if command == "update":
        self_update()
        return

    if command == "install":
        url = get_installer_url()
        installer = Path(tempfile.mkdtemp()) / Path(url).name
        download_file(url, installer)
        install_blitz(installer)
        return

    patch_only = command == "patch"

    if patch_only:
        if len(args) > 1:
            installer = Path(args[1])
            if not installer.exists():
                sys.exit(f"File not found: {installer}")
            install_blitz(installer)
    else:
        if command:
            installer = Path(command)
            if not installer.exists():
                sys.exit(f"File not found: {installer}")
        else:
            url = get_installer_url()
            installer = Path(tempfile.mkdtemp()) / Path(url).name
            download_file(url, installer)

        install_blitz(installer)

    if not APP_ASAR.exists():
        sys.exit(f"app.asar not found at {APP_ASAR}")

    patch_app()


if __name__ == "__main__":
    main()

