"""Create auto-updater (same design as Create's).

How an update travels:

  1. check     — read the latest release from GitHub (swildgen/create).
  2. download  — fetch the release zip once into ~/.create/updates, resuming a
                 partial file when the server allows it, then check its size
                 and its SHA-256 (GitHub publishes one per release asset).
  3. stage     — unpack it right away, in the background, to
                 ~/.create/updates/staged/<version>/Create.app and confirm the
                 bundle really is that version. From here installing is just a
                 folder swap.
  4. install   — a small detached script waits for Create to exit, swaps the
                 bundles, and (when asked) reopens the app. The old bundle is
                 kept until the new one has started; if it never starts, the
                 script puts the old one back.

Installing happens on "Restart Now", when Create quits, or at the next launch
if a staged update is waiting — whichever comes first.

Everything is logged to ~/.create/updates/update.log.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("create.updater")

GITHUB_REPO = "swildgen/create"
FEED_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
EXECUTABLE = "create"

UPDATES_DIR = Path(os.environ.get("CREATE_UPDATES_DIR") or (Path.home() / ".create" / "updates"))
STATE_FILE = UPDATES_DIR / "state.json"
STAGED_DIR = UPDATES_DIR / "staged"
LOG_FILE = UPDATES_DIR / "update.log"
SWAP_SCRIPT = UPDATES_DIR / "swap.sh"

# After this many failed installs of one version, stop trying it automatically
# so a bad release cannot trap the app in a restart loop.
MAX_INSTALL_ATTEMPTS = 2

_prepare_lock = threading.Lock()
_log_ready = False


def _setup_log():
    global _log_ready
    if _log_ready:
        return
    try:
        UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=256_000, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        _log_ready = True
    except Exception:
        pass


# ------------------------------------------------------------------ versions

def _version_tuple(v: str) -> tuple:
    """Parse 'v2.1.0' or '2.1.0' into a comparable tuple of ints."""
    parts = []
    for seg in (v or "").lstrip("vV").strip().split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return bool(candidate) and _version_tuple(candidate) > _version_tuple(current)


def bundle_version(app: Path) -> str:
    """The version a Create.app bundle reports (its bundled VERSION file)."""
    try:
        return (app / "Contents" / "Resources" / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ------------------------------------------------------------------ state

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def staged_update(current_version: str) -> Optional[dict]:
    """The update that is unpacked and ready to install, if any:
    {version, release_notes, app}. None when nothing newer is staged, the
    staged bundle is missing or wrong, or it has already failed to install."""
    staged = _load_state().get("staged") or {}
    version = staged.get("version", "")
    app = Path(staged.get("app", "") or "/nonexistent")
    if not is_newer(version, current_version):
        return None
    if not (app / "Contents" / "MacOS" / EXECUTABLE).exists() or bundle_version(app) != version:
        return None
    if int(staged.get("attempts", 0)) >= MAX_INSTALL_ATTEMPTS:
        return None
    return {"version": version, "release_notes": staged.get("release_notes", ""), "app": str(app)}


# ------------------------------------------------------------------ check

def check_for_update(current_version: str) -> Optional[dict]:
    """Ask GitHub for the latest release. Returns None if up to date or
    unreachable, else {version, download_url, release_notes, sha256, size}."""
    # CREATE_UPDATE_FEED points the check at another URL returning the same
    # JSON, for rehearsing a release against a local server before publishing.
    url = os.environ.get("CREATE_UPDATE_FEED") or FEED_URL
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "create-updater",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            row = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("update check failed: %s", e)
        return None

    if not isinstance(row, dict) or row.get("draft") or row.get("prerelease"):
        return None
    latest = (row.get("tag_name") or "").lstrip("vV")
    asset = next((a for a in row.get("assets") or [] if str(a.get("name", "")).endswith(".zip")), None)
    if not latest or not asset or not is_newer(latest, current_version):
        return None
    digest = str(asset.get("digest") or "")
    return {
        "version": latest,
        "download_url": asset.get("browser_download_url", ""),
        "release_notes": row.get("body", "") or "",
        "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
        "size": int(asset.get("size") or 0),
    }


# ------------------------------------------------------------------ download

def _archive_kind(path: Path) -> Optional[str]:
    """Sniff the file's magic bytes. Returns 'zip', 'dmg', or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
            if head[:2] == b"PK":
                return "zip"
            # UDIF (.dmg) images end with a 'koly' trailer.
            f.seek(-512, os.SEEK_END)
            if f.read(4) == b"koly":
                return "dmg"
    except Exception:
        return None
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_update(info: dict, progress_cb: Optional[Callable] = None) -> Optional[Path]:
    """Download the release archive for `info` (from check_for_update).

    The file is written as Create-<version>.part and only renamed once it is
    complete and verified, so a finished archive on disk is always a good one
    and is reused instead of downloaded again. A partial file is resumed when
    the server honours Range requests.

    progress_cb(downloaded_mb, total_mb, pct) is called about once per MB.
    Returns the archive path, or None on failure.
    """
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    version = info["version"]
    final = UPDATES_DIR / f"Create-{version}.archive"
    part = UPDATES_DIR / f"Create-{version}.part"

    if final.exists() and _verify_archive(final, info):
        logger.info("download: reusing verified archive %s", final.name)
        return final
    final.unlink(missing_ok=True)

    # Older releases, and any archive for a different version, are dead weight.
    for stale in UPDATES_DIR.glob("Create-*"):
        if stale not in (final, part) and stale.is_file():
            stale.unlink(missing_ok=True)

    url = info["download_url"]
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        logger.warning("download: refusing non-https url %s", url)
        return None
    have = part.stat().st_size if part.exists() else 0
    headers = {"Accept": "application/octet-stream", "User-Agent": "create-updater"}
    if have:
        headers["Range"] = f"bytes={have}-"

    started = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            resumed = have > 0 and resp.status == 206
            if not resumed:
                have = 0
            total = have + int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = have
            last_report = downloaded
            with open(part, "ab" if resumed else "wb") as f:
                while True:
                    chunk = resp.read(512 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and downloaded - last_report >= 1_000_000:
                        last_report = downloaded
                        progress_cb(downloaded / 1048576, total / 1048576 if total else 0,
                                    int(downloaded * 100 / total) if total else 0)
    except urllib.error.HTTPError as e:
        if e.code == 416:      # our partial file is not a prefix of what the server has now
            part.unlink(missing_ok=True)
        logger.warning("download failed: HTTP %s", e.code)
        return None
    except Exception as e:
        # Keep the partial file: the next attempt resumes from it if it can.
        logger.warning("download interrupted after %s bytes: %s", part.stat().st_size if part.exists() else 0, e)
        return None

    if total and downloaded != total:
        logger.warning("download incomplete: got %s of %s bytes", downloaded, total)
        return None
    if not _verify_archive(part, info):
        part.unlink(missing_ok=True)
        return None

    part.replace(final)
    if progress_cb:
        mb = downloaded / 1048576
        progress_cb(mb, mb, 100)
    logger.info("download: v%s complete, %.0f MB in %.0fs%s", version, downloaded / 1048576,
                time.time() - started, " (resumed)" if resumed else "")
    return final


def _verify_archive(path: Path, info: dict) -> bool:
    size = path.stat().st_size
    if _archive_kind(path) is None:
        logger.warning("verify: %s is not a zip or dmg (%s bytes) — probably an error page", path.name, size)
        return False
    if info.get("size") and size != info["size"]:
        logger.warning("verify: %s is %s bytes, release says %s", path.name, size, info["size"])
        return False
    if info.get("sha256"):
        actual = _sha256(path)
        if actual != info["sha256"]:
            logger.warning("verify: %s checksum mismatch (%s)", path.name, actual[:12])
            return False
    return True


# ------------------------------------------------------------------ stage

def _unpack(archive: Path, dest: Path) -> Optional[str]:
    """Unpack a zip or dmg into `dest`. Returns an error string, or None."""
    if _archive_kind(archive) == "dmg":
        mount = UPDATES_DIR / "mnt"
        shutil.rmtree(mount, ignore_errors=True)
        mount.mkdir(parents=True)
        attach = subprocess.run(["hdiutil", "attach", str(archive), "-nobrowse", "-readonly",
                                 "-mountpoint", str(mount)], capture_output=True, text=True)
        if attach.returncode != 0:
            return f"could not mount disk image: {attach.stderr.strip()[:200]}"
        try:
            src = next((p for p in mount.iterdir() if p.suffix == ".app" and p.is_dir()), None)
            if not src:
                return "no .app inside the disk image"
            copy = subprocess.run(["ditto", str(src), str(dest / src.name)], capture_output=True, text=True)
            if copy.returncode != 0:
                return f"could not copy app out of disk image: {copy.stderr.strip()[:200]}"
        finally:
            subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)
            shutil.rmtree(mount, ignore_errors=True)
        return None

    # ditto keeps executable bits and symlinks, which Python's zipfile drops.
    unzip = subprocess.run(["ditto", "-x", "-k", str(archive), str(dest)], capture_output=True, text=True)
    if unzip.returncode != 0:
        return f"could not unzip: {unzip.stderr.strip()[:200]}"
    return None


def stage_update(archive: Path, info: dict) -> Optional[Path]:
    """Unpack `archive` to staged/<version>/Create.app and confirm it is the
    version it claims to be. Returns the staged bundle, or None."""
    version = info["version"]
    dest = STAGED_DIR / version
    started = time.time()
    shutil.rmtree(STAGED_DIR, ignore_errors=True)
    dest.mkdir(parents=True)

    err = _unpack(archive, dest)
    app = None
    if not err:
        app = next((p for p in dest.iterdir() if p.suffix == ".app" and p.is_dir()), None) \
            or next((p for p in dest.rglob("*.app") if p.is_dir()), None)
        if not app:
            err = "no .app in the update package"
        elif not os.access(app / "Contents" / "MacOS" / EXECUTABLE, os.X_OK):
            err = "the update's app has no runnable executable"
        elif bundle_version(app) != version:
            err = f"the update's app says it is v{bundle_version(app) or '?'}, expected v{version}"
    if err:
        logger.warning("stage: v%s failed: %s", version, err)
        shutil.rmtree(STAGED_DIR, ignore_errors=True)
        archive.unlink(missing_ok=True)   # do not keep reusing an archive that cannot be staged
        return None

    subprocess.run(["xattr", "-rd", "com.apple.quarantine", str(app)], capture_output=True)
    state = _load_state()
    state["staged"] = {"version": version, "app": str(app),
                       "release_notes": info.get("release_notes", ""), "attempts": 0}
    _save_state(state)
    archive.unlink(missing_ok=True)       # the staged bundle is all that is needed from here
    logger.info("stage: v%s ready at %s (%.0fs)", version, app, time.time() - started)
    return app


def prepare_update(current_version: str, progress_cb: Optional[Callable] = None,
                   on_found: Optional[Callable] = None) -> Optional[dict]:
    """Check, download and stage in one go. Safe to call from several threads
    or on a timer: only one runs at a time and finished work is never redone.

    Returns the staged update ({version, release_notes, app}) or None.
    `on_found(info)` fires once a newer version is known and has to be fetched.
    """
    _setup_log()
    if not _prepare_lock.acquire(blocking=False):
        return None
    try:
        info = check_for_update(current_version)
        ready = staged_update(current_version)
        if not info:
            return ready
        if ready and ready["version"] == info["version"]:
            return ready
        failed = (_load_state().get("staged") or {})
        if failed.get("version") == info["version"] and int(failed.get("attempts", 0)) >= MAX_INSTALL_ATTEMPTS:
            logger.info("prepare: v%s already failed to install %s times; waiting for a newer release",
                        info["version"], failed["attempts"])
            return None

        logger.info("prepare: v%s available (running v%s)", info["version"], current_version)
        if on_found:
            on_found(info)
        archive = download_update(info, progress_cb)
        if not archive:
            return None
        if not stage_update(archive, info):
            return None
        return staged_update(current_version)
    except Exception as e:
        logger.exception("prepare: unexpected error: %s", e)
        return None
    finally:
        _prepare_lock.release()


# ------------------------------------------------------------------ install

def _find_app_bundle() -> Optional[Path]:
    """Walk up from the running executable to find the .app bundle."""
    if not getattr(sys, "frozen", False):
        return None
    current = Path(sys.executable).resolve()
    for _ in range(10):
        if current.suffix == ".app":
            return current
        if current == current.parent:
            break
        current = current.parent
    return None


# Arguments: <pid to wait for> <installed bundle> <staged bundle> <version>
#            <relaunch: 1|0> <updates dir> [seconds to wait for the new app]
# Paths arrive as arguments, never pasted into the script text, so spaces and
# quotes in them are harmless.
_SWAP_SCRIPT = r"""#!/bin/bash
PID="$1"; APP="$2"; NEW="$3"; VERSION="$4"; RELAUNCH="$5"; DIR="$6"; WAIT="${7:-90}"
OLD="$APP.previous"
MARK="$DIR/launched-$VERSION"
exec >> "$DIR/update.log" 2>&1
say() { echo "$(date '+%Y-%m-%d %H:%M:%S') SWAP $*"; }

say "installing v$VERSION over $APP (waiting for pid $PID)"
for _ in $(seq 1 150); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.2
done
if kill -0 "$PID" 2>/dev/null; then
    say "Create did not exit within 30s; leaving everything as it was"
    exit 1
fi

rm -rf "$OLD" "$MARK"
if ! mv "$APP" "$OLD"; then
    say "could not move the current app aside; nothing changed"
    [ "$RELAUNCH" = "1" ] && open "$APP"
    exit 1
fi
if ! mv "$NEW" "$APP"; then
    say "could not move the new app into place; restoring the previous one"
    mv "$OLD" "$APP"
    [ "$RELAUNCH" = "1" ] && open "$APP"
    exit 1
fi
xattr -rd com.apple.quarantine "$APP" 2>/dev/null
say "v$VERSION is in place"

if [ "$RELAUNCH" != "1" ]; then
    # Installed on quit: the next launch confirms it and removes the previous copy.
    exit 0
fi

open "$APP" || say "open failed"
# The new app writes $MARK once it is up. Without it, treat the update as broken.
for _ in $(seq 1 "$WAIT"); do
    [ -e "$MARK" ] && break
    sleep 1
done
if [ -e "$MARK" ]; then
    say "v$VERSION started; removing the previous version"
    rm -rf "$OLD"
    exit 0
fi

say "v$VERSION never reported in after ${WAIT}s; rolling back"
pkill -f "$APP/Contents/MacOS/" 2>/dev/null
sleep 1
rm -rf "$APP"
mv "$OLD" "$APP" && say "previous version restored" || say "ROLLBACK FAILED"
open "$APP"
exit 1
"""


def install_staged(current_version: str, relaunch: bool, app_bundle: Optional[Path] = None) -> dict:
    """Hand the staged update to the swap script. The caller must exit right
    after this returns {"ok": True}: the script waits for this process to end.

    Returns {"error": reason} when there is nothing to install or it cannot be
    installed here.
    """
    _setup_log()
    ready = staged_update(current_version)
    if not ready:
        return {"error": "No update is ready to install"}

    app_bundle = app_bundle or _find_app_bundle()
    if not app_bundle:
        return {"error": "Not running from Create.app"}
    # e.g. launched straight from the disk image, or from a read-only location.
    if not os.access(app_bundle.parent, os.W_OK):
        msg = f"Cannot write to {app_bundle.parent} — move Create to your Applications folder"
        logger.warning("install: %s", msg)
        return {"error": msg}

    state = _load_state()
    state["staged"]["attempts"] = int(state["staged"].get("attempts", 0)) + 1
    state["installing"] = {"version": ready["version"], "from": current_version}
    _save_state(state)

    SWAP_SCRIPT.write_text(_SWAP_SCRIPT, encoding="utf-8")
    SWAP_SCRIPT.chmod(0o755)
    subprocess.Popen(
        ["/bin/bash", str(SWAP_SCRIPT), str(os.getpid()), str(app_bundle), ready["app"],
         ready["version"], "1" if relaunch else "0", str(UPDATES_DIR),
         os.environ.get("CREATE_SWAP_WAIT", "90")],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("install: v%s handed to swap script (relaunch=%s)", ready["version"], relaunch)
    return {"ok": True, "version": ready["version"]}


def confirm_launch(current_version: str, app_bundle: Optional[Path] = None):
    """Call once at startup. Tells a waiting swap script this version is up,
    and clears out whatever the last update left behind."""
    _setup_log()
    try:
        UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        for old in UPDATES_DIR.glob("launched-*"):
            old.unlink(missing_ok=True)
        (UPDATES_DIR / f"launched-{current_version}").write_text(str(os.getpid()))

        state = _load_state()
        installing = state.pop("installing", None)
        staged = state.get("staged") or {}
        if installing:
            if installing.get("version") == current_version:
                logger.info("launch: now running v%s (was v%s)", current_version, installing.get("from"))
            else:
                logger.warning("launch: install of v%s did not take; still on v%s",
                               installing.get("version"), current_version)
        # A staged copy of this version or older has served its purpose.
        if staged and not is_newer(staged.get("version", ""), current_version):
            state.pop("staged", None)
            shutil.rmtree(STAGED_DIR, ignore_errors=True)
        _save_state(state)

        # An update installed on quit leaves the previous bundle for us to remove.
        bundle = app_bundle or _find_app_bundle()
        if bundle:
            for suffix in (".previous", ".old"):
                leftover = Path(str(bundle) + suffix)
                if leftover.exists():
                    shutil.rmtree(leftover, ignore_errors=True)
    except Exception as e:
        logger.warning("launch: cleanup failed: %s", e)
