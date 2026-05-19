# blitz-cli

Patches the [Blitz](https://blitz.gg) desktop client to remove ads and unlock pro features.

## Install

**Windows**
```powershell
iwr -useb https://raw.githubusercontent.com/carlelieser/blitz-cli/main/install.ps1 | iex
```

**macOS / Linux**
```bash
curl -fsSL https://raw.githubusercontent.com/carlelieser/blitz-cli/main/install.sh | bash
```

## Usage

```
blitz                  download, install, and patch Blitz
blitz --patch-only     patch an existing Blitz installation
blitz --update         update blitz-cli itself
blitz <installer>      use a local installer file
```

## Requirements

- Python 3.8+
- Node.js (for asar repacking)

The install scripts will set these up automatically if missing.
