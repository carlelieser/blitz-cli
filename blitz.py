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

import json, os, platform, re, shutil, subprocess, sys, tempfile, time, zipfile
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
    print(f"\r✓ {Path(url).name} ({dest.stat().st_size / 1_048_576:.0f} MB)")


def _install_windows(exe: Path):
    subprocess.run([str(exe), "/S"], check=True)
    for _ in range(60):
        if APP_ASAR.exists():
            return
        time.sleep(2)
    raise RuntimeError("Timed out — app.asar not found after install")


def _install_mac(dmg: Path):
    import plistlib
    print("Mounting disk image ...")
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
    finally:
        subprocess.run(["hdiutil", "detach", mount_point, "-quiet"])


def _install_linux(deb: Path):
    subprocess.run(["sudo", "dpkg", "-i", str(deb)], check=True)
    if not APP_ASAR.exists():
        raise RuntimeError(f"app.asar not found at {APP_ASAR} after install")


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
        unpack = "**/{*.node,liblzma.dll}" if SYSTEM == "Windows" else "**/*.node"
        script = (
            f"require('{a}').createPackageWithOptions('{s}','{o}',"
            f"{{unpack:'{unpack}'}}"
            f").then(()=>process.exit(0))"
            f".catch(e=>{{console.error(e);process.exit(1)}});"
        )
        subprocess.run([_require("node"), "-e", script], check=True)


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
        print("No patches found — skipping")
        return

    patches = [(json.loads(pf.read_text("utf-8")), pf) for pf in patch_files]
    patches.sort(key=lambda x: x[0].get("priority", 0))

    for patch, _ in patches:
        apply_patch(src, patch)
        print(f"  ✓ {patch['description']}")


# ─── Self-update ──────────────────────────────────────────────────────────────

def self_update():
    install_dir = Path(__file__).parent
    url = "https://github.com/carlelieser/blitz-cli/archive/refs/heads/main.zip"

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        zip_path = tmp / "blitz-cli.zip"

        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)

        extract_dir = tmp / "extract"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        src = extract_dir / "blitz-cli-main"
        shims = {"blitz.cmd", "blitz"}  # never overwrite platform shims
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

    print("✓ Updated")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    command = args[0] if args else None

    if command == "update":
        self_update()
        return

    if command == "install":
        url = get_installer_url()
        tmp = Path(tempfile.mkdtemp())
        installer = tmp / Path(url).name
        download_file(url, installer)
        install_blitz(installer)
        print("✓ Installed")
        return

    patch_only = command == "patch"
    installer = None

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
            tmp = Path(tempfile.mkdtemp())
            installer = tmp / Path(url).name
            download_file(url, installer)

        install_blitz(installer)
        print("✓ Installed")

    if not APP_ASAR.exists():
        sys.exit(f"app.asar not found at {APP_ASAR}")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "asar-src"
        extract_asar(APP_ASAR, src)
        apply_all_patches(src)
        repack_asar(src, APP_ASAR)

    if SYSTEM == "Darwin":
        _resign_mac()

    print("✓ Patched")


if __name__ == "__main__":
    main()
