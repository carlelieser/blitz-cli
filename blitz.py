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

ORIG_ASAR = APP_ASAR.parent / (APP_ASAR.name + ".orig")


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
    subprocess.run([str(exe), "/S"], check=True)
    for _ in range(60):
        if APP_ASAR.exists():
            return
        time.sleep(2)
    raise RuntimeError("Timed out — app.asar not found after install")


def _install_mac(dmg: Path):
    import plistlib
    print("  Mounting disk image ...", end="", flush=True)
    result = subprocess.run(
        ["hdiutil", "attach", str(dmg), "-nobrowse", "-noverify", "-plist"],
        capture_output=True, check=True,
    )
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
        print(" [ok]")
    finally:
        subprocess.run(["hdiutil", "detach", mount_point, "-quiet"])


def _install_linux(deb: Path):
    subprocess.run(["sudo", "dpkg", "-i", str(deb)], check=True)
    if not APP_ASAR.exists():
        raise RuntimeError(f"app.asar not found at {APP_ASAR} after install")


def _kill_blitz():
    """Terminate any running Blitz processes so they cannot write stale file
    offsets to index.node between our install and our asar repack."""
    if SYSTEM == "Windows":
        subprocess.run(
            ["taskkill", "/F", "/IM", "Blitz.exe", "/T"],
            capture_output=True,
        )
    elif SYSTEM == "Darwin":
        subprocess.run(["pkill", "-f", "Blitz.app"], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "blitz"], capture_output=True)


def install_blitz(installer: Path):
    _kill_blitz()
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


def _asar_paths(asar_path: Path) -> tuple:
    """
    Parse the asar header and return (all_files: set, unpacked_files: set)
    where each element is a forward-slash relative path.
    """
    with open(asar_path, "rb") as f:
        data = f.read(1024 * 1024)
    json_len = struct.unpack_from("<I", data, 12)[0]
    header = json.loads(data[16 : 16 + json_len])

    all_files: set = set()
    unpacked: set = set()

    def walk(node, prefix=""):
        for name, entry in node.get("files", {}).items():
            rel = f"{prefix}/{name}" if prefix else name
            if "files" in entry:
                walk(entry, rel)
            else:
                all_files.add(rel)
                if entry.get("unpacked"):
                    unpacked.add(rel)

    walk(header)
    return all_files, unpacked


def _unpack_glob_from_paths(all_files: set, unpacked: set) -> str:
    """
    Derive minimal **/dir/** glob patterns for the originally-unpacked files.

    @electron/asar calls minimatch(absoluteFilePath, pattern, {matchBase:true})
    so patterns must start with '**/' to match anywhere in the full path.

    We only mark a directory as "fully unpacked" when EVERY file under it in
    the asar (packed + unpacked) is in the unpacked set, preventing the common
    mistake of marking 'node_modules' as fully unpacked when only two of its
    many subdirectories are unpacked.
    """
    from collections import defaultdict

    # Build children maps from the FULL file list (not just unpacked)
    all_dir_children: dict = defaultdict(set)
    for p in all_files:
        parts = p.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            child  = "/".join(parts[:depth + 1])
            all_dir_children[parent].add(child)

    fully_unpacked: set = set()

    def is_full(node: str) -> bool:
        if node in fully_unpacked:
            return True
        children = all_dir_children.get(node)
        if not children:
            return node in unpacked
        result = all(is_full(c) for c in children)
        if result:
            fully_unpacked.add(node)
        return result

    for d in list(all_dir_children):
        is_full(d)

    # Minimal set: fully-unpacked dirs whose parent is not fully unpacked
    minimal: list = []
    for d in sorted(fully_unpacked):
        parts = d.split("/")
        parent = "/".join(parts[:-1])
        if parent not in fully_unpacked:
            minimal.append(f"**/{d}/**")

    # Add individual unpacked files not already covered by a directory glob
    covered: set = set()
    for g in minimal:
        bare = g.removeprefix("**/")[:-3]
        for p in unpacked:
            if p == bare or p.startswith(bare + "/"):
                covered.add(p)
    for p in unpacked:
        if p not in covered:
            minimal.append(f"**/{p}")

    if not minimal:
        return ""
    if len(minimal) == 1:
        return minimal[0]
    return "{" + ",".join(minimal) + "}"


def repack_asar(src: Path, out: Path, unpack_glob: str):
    # Preserve the installer-placed app.asar.unpacked/ files.  @electron/asar
    # overwrites them with fresh temp-dir copies that Windows may treat as less
    # trusted (different security attributes), causing blitz_core.node to crash
    # when it loads the DLLs.  We back up the originals, let the repack run
    # (so the new asar header correctly marks files as unpacked), then restore.
    unpacked_dir = out.parent / (out.name + ".unpacked")
    backup_dir = out.parent / (out.name + ".unpacked.bak")
    if unpacked_dir.exists():
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        shutil.copytree(unpacked_dir, backup_dir)

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
        # JSON-encode the glob so special chars are safe inside the JS string literal
        glob_json = json.dumps(unpack_glob)
        script = (
            f"require('{a}').createPackageWithOptions('{s}','{o}',"
            f"{{unpack:{glob_json}}}"
            f").then(()=>process.exit(0))"
            f".catch(e=>{{console.error(e);process.exit(1)}});"
        )
        subprocess.run([_require("node"), "-e", script], check=True)

    # Restore the original installer-placed unpacked files
    if backup_dir.exists():
        if unpacked_dir.exists():
            shutil.rmtree(unpacked_dir)
        shutil.copytree(backup_dir, unpacked_dir)
        shutil.rmtree(backup_dir)


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
    targets = [binaries_dir / "index.node"]

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
    Bypass the E6 integrity check inside VerifyApp in blitz_core.node.

    VerifyApp runs a ~8 KB security block that both verifies the asar hash
    AND initializes the overlay renderer.  An older approach replaced the
    conditional branch at the block entry with an unconditional JMP, which
    skipped the block entirely — preventing overlay initialization.

    The correct fix is narrower: a specific sub-check inside VerifyApp
    compares an API return value with 0x6D and crashes with "E6 Error" when
    it matches.  We patch that conditional JNZ to an unconditional JMP so
    the E6 path is never taken while the rest of the block (including overlay
    setup) still runs normally.

    Secondary: revert any old entry-stub (MOV EAX,0; RET) that a previous
    version of blitz-cli may have written, since the stub skips all of init.
    """
    raw = bytes(data)
    changed = False
    msgs = []

    # ── Primary: bypass E6 sub-check inside VerifyApp ────────────────────────
    # CMP EAX, 0x6D (83 F8 6D) + JNZ rel32 (0F 85 [disp4])
    # → replace JNZ with unconditional JMP (E9 [disp+1]) + NOP pad
    # (JMP is 5 bytes vs JNZ 6 bytes, so displacement increases by 1)
    E6_CMP    = bytes([0x83, 0xF8, 0x6D, 0x0F, 0x85])  # CMP EAX,0x6D + JNZ prefix
    E6_BYPASS = bytes([0x83, 0xF8, 0x6D, 0xE9])         # CMP EAX,0x6D + JMP prefix

    idx = 0
    e6_found = 0
    while True:
        pos = raw.find(E6_CMP, idx)
        if pos == -1:
            break
        old_disp = int.from_bytes(raw[pos + 5:pos + 9], "little", signed=True)
        new_disp = (old_disp + 1).to_bytes(4, "little", signed=True)
        data[pos + 3] = 0xE9
        data[pos + 4:pos + 8] = new_disp
        data[pos + 8] = 0x90
        raw = bytes(data)
        changed = True
        e6_found += 1
        msgs.append(f"bypassed E6 check at 0x{pos:x}")
        idx = pos + 9
    if e6_found == 0:
        if raw.find(E6_BYPASS) != -1:
            msgs.append("E6 bypass already applied")
        else:
            msgs.append("E6 check pattern not found — skipping")

    # ── Secondary: revert old entry stub so VerifyApp can actually run ────────
    OLD_STUB      = bytes([0xB8, 0x00, 0x00, 0x00, 0x00, 0xC3])
    ORIG_PROLOGUE = bytes([0x40, 0x55, 0x53, 0x56, 0x57, 0x41])
    idx = 0
    while True:
        pos = raw.find(OLD_STUB, idx)
        if pos == -1:
            break
        if pos >= 2 and raw[pos - 1] == 0xCC and raw[pos - 2] == 0xCC:
            for k, b in enumerate(ORIG_PROLOGUE):
                data[pos + k] = b
            raw = bytes(data)
            changed = True
            msgs.append(f"reverted old stub at 0x{pos:x}")
        idx = pos + 1

    return changed, "; ".join(msgs) if msgs else "no changes"


def restore_blitz_core_to_deps():
    """
    After a fresh Blitz install the binaries/ dir has a clean blitz_core.node.
    Copy it into every blitz-deps version directory so that any copy previously
    modified by an older run of this script is replaced with the signed original.
    """
    if SYSTEM != "Windows":
        return
    src = BLITZ_DIR / "resources" / "binaries" / "blitz_core.node"
    if not src.exists():
        return
    deps_base = Path(os.environ["APPDATA"]) / "Blitz" / "blitz-deps"
    if not deps_base.exists():
        return
    for ver_dir in deps_base.iterdir():
        dest = ver_dir / "blitz_core.node"
        if dest.exists():
            try:
                shutil.copy2(src, dest)
                print(_ok(f"restored blitz_core.node to {ver_dir.name}"))
            except PermissionError:
                print(_warn(f"blitz_core.node in {ver_dir.name} is locked -- close Blitz first"))


def patch_blitz_core():
    binaries_dir = BLITZ_DIR / "resources" / "binaries"
    targets = [binaries_dir / "blitz_core.node"]

    if SYSTEM == "Windows":
        deps_base = Path(os.environ["APPDATA"]) / "Blitz" / "blitz-deps"
        if deps_base.exists():
            for ver_dir in deps_base.iterdir():
                candidate = ver_dir / "blitz_core.node"
                if candidate.exists():
                    targets.append(candidate)

    for target in targets:
        try:
            data = bytearray(target.read_bytes())
            changed, msg = _patch_core_data(data)
            if changed:
                target.write_bytes(bytes(data))
                print(_ok(target.name))
            elif "already patched" in msg:
                print(_ok(f"{target.name}: already up to date"))
            else:
                print(_warn(f"{target.name}: {msg}"))
        except PermissionError:
            print(_warn(f"{target.name}: file is locked -- close Blitz and re-run"))


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


def _save_orig_asar():
    """Save a pristine copy of app.asar before the first patch run."""
    if not ORIG_ASAR.exists():
        shutil.copy2(APP_ASAR, ORIG_ASAR)
        print(_ok("saved original app.asar"))


def _restore_orig_asar():
    """
    Always patch from the pristine original so patch runs are idempotent.
    If no backup exists yet, treat the current app.asar as original and save it.
    """
    if not ORIG_ASAR.exists():
        shutil.copy2(APP_ASAR, ORIG_ASAR)
        print(_ok("saved original app.asar"))
    else:
        shutil.copy2(ORIG_ASAR, APP_ASAR)


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
        print(f"Downloading {installer.name} ...")
        download_file(url, installer)
        print("Installing ...", end="", flush=True)
        install_blitz(installer)
        print(" [ok]")
        ORIG_ASAR.unlink(missing_ok=True)
        _save_orig_asar()
        return

    patch_only = command == "patch"

    if patch_only:
        if len(args) > 1:
            installer = Path(args[1])
            if not installer.exists():
                sys.exit(f"File not found: {installer}")
            print("Installing ...", end="", flush=True)
            install_blitz(installer)
            print(" [ok]")
            ORIG_ASAR.unlink(missing_ok=True)
    else:
        if command:
            installer = Path(command)
            if not installer.exists():
                sys.exit(f"File not found: {installer}")
        else:
            url = get_installer_url()
            installer = Path(tempfile.mkdtemp()) / Path(url).name
            print(f"Downloading {installer.name} ...")
            download_file(url, installer)

        print("Installing ...", end="", flush=True)
        install_blitz(installer)
        print(" [ok]")
        ORIG_ASAR.unlink(missing_ok=True)

    if not APP_ASAR.exists():
        sys.exit(f"app.asar not found at {APP_ASAR}")

    # Kill any Blitz that auto-started after install before we touch its files.
    _kill_blitz()

    patch_files = list(PATCHES_DIR.glob("*.json"))

    if patch_files:
        _restore_orig_asar()

        print("Extracting app.asar ...", end="", flush=True)
        all_files, unpacked = _asar_paths(APP_ASAR)
        unpack_glob = _unpack_glob_from_paths(all_files, unpacked)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "asar-src"
            extract_asar(APP_ASAR, src)
            print(_ok())

            print("Applying patches ...")
            apply_all_patches(src)

            print("Repacking app.asar ...", end="", flush=True)
            repack_asar(src, APP_ASAR, unpack_glob)
            print(_ok())

    print("Patching binaries ...")
    new_hash = _sha256_file(APP_ASAR)
    patch_index_node(new_hash)
    patch_blitz_core()

    if SYSTEM == "Darwin":
        print("Re-signing app bundle ...", end="", flush=True)
        _resign_mac()
        print(" [ok]")

    print("Done.")


if __name__ == "__main__":
    main()

