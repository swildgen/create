#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymobiledevice3>=11.61,<12",
#     "rich>=13",
#     "xxhash>=3",
# ]
# ///
"""Offload Blackmagic Camera clips from USB-connected iPhones into an episode folder.

    ./offload.py --list-devices          show connected phones and their camera mapping
    ./offload.py --dry-run               list what would be copied, and total size
    ./offload.py --episode Ep05          copy + verify
    ./offload.py --episode Ep05 --delete-after-verify

Nothing is ever deleted from a phone unless --delete-after-verify is passed AND confirmed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import xxhash
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

CHUNK = 4 * 1024**2  # AFC's maximum single read
WINDOW = 8  # reads kept in flight per phone
READ_TIMEOUT = 120  # seconds without data before a phone is considered stalled
LEDGER_NAME = ".offload-ledger.json"
GB = 1000**3
VERIFY_MODES = ("size", "readback", "device")

console = Console(highlight=False)


# ----------------------------------------------------------------------------- config


@dataclass
class Config:
    destination: Path
    cameras: dict[str, str] = field(default_factory=dict)
    app_name_match: str = "blackmagic"
    bundle_id: Optional[str] = None
    source_dir: str = "/Documents/Media"
    include_proxies: bool = True
    extensions: tuple[str, ...] = (".mov", ".mp4", ".m4v", ".braw")
    verify: str = "readback"
    cameras_raw: dict[str, str] = field(default_factory=dict)  # keys as written in config.toml


def norm(s: str) -> str:
    """Normalise a device name / UDID for matching (curly quotes, case, whitespace)."""
    return s.replace("’", "'").replace("‘", "'").strip().casefold()


def load_config(path: Path, dest_override: Optional[str]) -> Config:
    raw: dict[str, Any] = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    elif not dest_override:
        sys.exit(
            f"No config file at {path}.\n"
            "Copy config.example.toml to config.toml and set `destination`, or pass --dest."
        )
    destination = dest_override or raw.get("destination")
    if not destination:
        sys.exit(f"`destination` is not set in {path} (or pass --dest).")
    cfg = Config(destination=Path(destination).expanduser())
    cfg.cameras_raw = {str(k): str(v) for k, v in raw.get("cameras", {}).items()}
    cfg.cameras = {norm(k): v for k, v in cfg.cameras_raw.items()}
    if raw.get("verify") in VERIFY_MODES:
        cfg.verify = raw["verify"]
    for key in ("app_name_match", "bundle_id", "source_dir", "include_proxies"):
        if key in raw:
            setattr(cfg, key, raw[key])
    if "extensions" in raw:
        cfg.extensions = tuple(e.lower() if e.startswith(".") else f".{e.lower()}" for e in raw["extensions"])
    return cfg


def save_config(path: Path, cfg: Config) -> None:
    """Write config.toml back out (used by the app when presets are changed)."""
    q = json.dumps  # JSON strings are valid TOML basic strings
    dest = str(cfg.destination)
    home = str(Path.home())
    if dest.startswith(home + "/"):
        dest = "~" + dest[len(home) :]
    default = Config(destination=cfg.destination)
    lines = [
        "# Episode folders are created inside this folder: <destination>/Ep05/CamA/...",
        f"destination = {q(dest, ensure_ascii=False)}",
        "",
        "# size | readback | device  (see README: Verification)",
        f"verify = {q(cfg.verify)}",
        "",
        "# Also copy Media/Proxy/* into CamX/Proxy/",
        f"include_proxies = {'true' if cfg.include_proxies else 'false'}",
    ]
    if cfg.extensions != default.extensions:
        lines.append(f"extensions = [{', '.join(q(e) for e in cfg.extensions)}]")
    for key in ("source_dir", "app_name_match", "bundle_id"):
        if getattr(cfg, key) != getattr(default, key):
            lines.append(f"{key} = {q(getattr(cfg, key), ensure_ascii=False)}")
    lines += ["", "# Map each phone (UDID or device name) to a camera folder.", "[cameras]"]
    lines += [f"{q(k, ensure_ascii=False)} = {q(v, ensure_ascii=False)}" for k, v in cfg.cameras_raw.items()]
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def safe_name(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "-", s).strip().strip(".")
    return s or "unnamed"


# ----------------------------------------------------------------------------- ledger


class Ledger:
    """Record of every clip already offloaded, stored next to the footage.

    This is what makes "new clips only" work across episodes while clips stay on the phones.
    """

    def __init__(self, root: Path):
        self.root = root
        self.path = root / LEDGER_NAME
        self.data: dict[str, Any] = {"version": 1, "devices": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                sys.exit(f"Could not read the offload ledger {self.path}: {e}\nFix or move that file, then re-run.")

    def lookup(self, udid: str, clip: "Clip") -> Optional[dict[str, Any]]:
        entry = self.data["devices"].get(udid, {}).get("files", {}).get(clip.remote)
        if entry and entry["size"] == clip.size and abs(entry["mtime"] - clip.mtime) < 2:
            return entry
        return None

    def record(self, phone: "Phone", clip: "Clip", dest: Path, episode: str, digest: Optional[str], verify: str):
        dev = self.data["devices"].setdefault(phone.udid, {"name": phone.name, "files": {}})
        dev["name"] = phone.name
        dev["files"][clip.remote] = {
            "size": clip.size,
            "mtime": clip.mtime,
            "episode": episode,
            "dest": str(dest.relative_to(self.root)),
            "xxh3_128": digest,
            "verify": verify,
            "offloaded_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)


@contextlib.contextmanager
def destination_lock(dest: Path):
    """Allow only one offload (app or command line) into a destination at a time."""
    import fcntl

    dest.mkdir(parents=True, exist_ok=True)
    f = open(dest / ".offload.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError("Another offload into this destination is already running. Wait for it to finish.") from None
    try:
        yield
    finally:
        f.close()


# ----------------------------------------------------------------------------- model


@dataclass
class Clip:
    remote: str  # path on the phone, e.g. /Documents/Media/A001_C001.mov
    rel: str  # path under the camera folder, e.g. A001_C001.mov or Proxy/A001_C001.mov
    size: int
    mtime: float


@dataclass
class Phone:
    udid: str
    name: str
    cam: str
    mapped: bool
    model: str = ""
    open_afc: Optional[Callable[[], Any]] = None  # -> async context manager yielding an AFC client
    bundle_id: str = ""
    error: Optional[str] = None
    new: list[Clip] = field(default_factory=list)
    skipped: list[tuple[Clip, str]] = field(default_factory=list)
    ignored: int = 0  # non-video files left alone
    lockdown: Any = None
    clips: list[Clip] = field(default_factory=list)  # everything found at the last scan
    progress_task: Any = None


@dataclass
class Result:
    phone: Phone
    clip: Clip
    ok: bool
    detail: str = ""
    dest: Optional[Path] = None


class PhoneLost(Exception):
    """The connection to a phone died; stop working on that phone."""


# ----------------------------------------------------------------------------- devices


def explain(e: BaseException) -> str:
    """Turn a pymobiledevice3 error into something actionable."""
    from pymobiledevice3 import exceptions as ex

    if isinstance(e, ex.UserDeniedPairingError):
        return "You tapped “Don’t Trust”. Unplug the phone, plug it back in, tap “Trust”, then re-run."
    if isinstance(e, (ex.NotPairedError, ex.PairingDialogResponsePendingError, ex.NotTrustedError)):
        return "Not trusted yet. Unlock the phone, tap “Trust This Computer”, enter the passcode, then re-run."
    if isinstance(e, (ex.PasswordRequiredError, ex.PasscodeRequiredError)):
        return "The phone is locked. Unlock it (keep it unlocked until the copy starts), then re-run."
    if isinstance(e, ex.AppNotInstalledError):
        return "Blackmagic Camera is not installed on this phone."
    if isinstance(e, (ex.ConnectionTerminatedError, ex.ConnectionFailedError, ConnectionError, asyncio.TimeoutError)):
        return "Lost the USB connection. Check the cable, unlock the phone, then re-run."
    return f"{type(e).__name__}: {e}"


def is_connection_error(e: BaseException) -> bool:
    from pymobiledevice3 import exceptions as ex

    if isinstance(e, ex.AfcException):
        return False
    return isinstance(
        e, (ex.ConnectionTerminatedError, ex.MuxException, ConnectionError, asyncio.TimeoutError, EOFError, BrokenPipeError)
    )


async def find_bundle_id(lockdown, cfg: Config) -> str:
    if cfg.bundle_id:
        return cfg.bundle_id
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    async with InstallationProxyService(lockdown) as proxy:
        apps = await proxy.get_apps(application_type="User")
    want = cfg.app_name_match.casefold()
    hits = []
    for bundle, info in apps.items():
        names = f"{info.get('CFBundleDisplayName', '')} {info.get('CFBundleName', '')}".casefold()
        if want in names or want in bundle.casefold():
            hits.append((bundle, info))
    # Blackmagic ships several apps; the camera is the one with "cam" in its name that shares files.
    cams = [h for h in hits if "cam" in f"{h[1].get('CFBundleDisplayName', '')} {h[0]}".casefold()] or hits
    cams = [h for h in cams if h[1].get("UIFileSharingEnabled")] or cams
    if not cams:
        raise LookupError(f"No app matching “{cfg.app_name_match}” is installed on this phone.")
    if len(cams) > 1:
        found = ", ".join(f"{i.get('CFBundleDisplayName')} ({b})" for b, i in cams)
        raise LookupError(f"Several apps match “{cfg.app_name_match}”: {found}. Set bundle_id in config.toml.")
    return cams[0][0]


async def connect_phones(cfg: Config) -> list[Phone]:
    from pymobiledevice3 import exceptions as ex
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService
    from pymobiledevice3.usbmux import list_devices

    try:
        mux_devices = await list_devices()
    except Exception as e:
        sys.exit(f"Could not talk to usbmuxd (the macOS iPhone USB service): {e}")
    serials = sorted({d.serial for d in mux_devices if d.connection_type == "USB"})

    async def connect(serial: str) -> Phone:
        phone = Phone(udid=serial, name=serial, cam=safe_name(serial), mapped=False)
        try:
            try:
                ld = await create_using_usbmux(serial=serial, autopair=False)
            except ex.NotPairedError:
                console.print(f"[yellow]{serial}: not trusted yet — unlock it and tap “Trust” now (waiting 60s)…[/]")
                ld = await create_using_usbmux(serial=serial, autopair=True, pair_timeout=60)
            phone.lockdown = ld
            phone.name = ld.all_values.get("DeviceName") or serial
            phone.model = ld.display_name or ld.product_type or ""
            cam = cfg.cameras.get(norm(serial)) or cfg.cameras.get(norm(phone.name))
            phone.mapped = cam is not None
            phone.cam = safe_name(cam or phone.name)
            phone.bundle_id = await find_bundle_id(ld, cfg)

            @contextlib.asynccontextmanager
            async def open_afc():
                async with await HouseArrestService.create(ld, phone.bundle_id, documents_only=True) as afc:
                    yield afc

            phone.open_afc = open_afc
        except LookupError as e:
            phone.error = str(e)
        except Exception as e:  # noqa: BLE001 - every failure becomes a per-phone message
            phone.error = explain(e)
        return phone

    phones = list(await asyncio.gather(*(connect(s) for s in serials)))
    phones.sort(key=lambda p: p.cam.casefold())
    return phones


# ----------------------------------------------------------------------------- scan


async def scan_clips(afc, cfg: Config) -> tuple[list[Clip], int]:
    """Return (video clips, count of other files ignored) from the app's storage."""
    root = cfg.source_dir.rstrip("/") or "/"
    try:
        if not await afc.isdir(root):
            root = "/Documents"
    except Exception:  # noqa: BLE001
        root = "/Documents"
    clips: list[Clip] = []
    ignored = 0
    async for dirpath, dirnames, filenames in afc.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = dirpath[len(root) :].strip("/")
        if not cfg.include_proxies and rel_dir.split("/")[0].casefold() == "proxy":
            continue
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if not name.lower().endswith(cfg.extensions):
                ignored += 1
                continue
            remote = f"{dirpath.rstrip('/')}/{name}"
            st = await afc.stat(remote)
            if st.get("st_ifmt") != "S_IFREG":
                continue
            clips.append(
                Clip(remote=remote, rel=f"{rel_dir}/{name}".lstrip("/"), size=st["st_size"], mtime=st["st_mtime"].timestamp())
            )
    return clips, ignored


def classify(phone: Phone, clips: list[Clip], ledger: Ledger, episode_dir: Path, args) -> None:
    """Split a phone's clips into new vs. already offloaded."""
    phone.new, phone.skipped = [], []
    for clip in clips:
        if args.match and not fnmatch.fnmatch(clip.rel, args.match):
            continue
        entry = None if args.all else ledger.lookup(phone.udid, clip)
        dest = episode_dir / phone.cam / clip.rel
        if entry:
            phone.skipped.append((clip, f"already offloaded → {entry['dest']}"))
        elif dest.exists() and dest.stat().st_size == clip.size:
            phone.skipped.append((clip, "already in this episode folder (same size)"))
        else:
            phone.new.append(clip)


async def scan_phone(phone: Phone, cfg: Config, ledger: Ledger, episode_dir: Path, args) -> None:
    if phone.error:
        return
    try:
        async with phone.open_afc() as afc:
            phone.clips, phone.ignored = await scan_clips(afc, cfg)
        classify(phone, phone.clips, ledger, episode_dir, args)
    except Exception as e:  # noqa: BLE001
        phone.error = explain(e)


# ----------------------------------------------------------------------------- copy + verify


def _sink(f, hashers, chunk: bytes) -> None:
    f.write(chunk)
    for h in hashers:
        h.update(chunk)


def hash_file(path: Path) -> str:
    """xxh3-128 of a file, read back from disk bypassing the page cache where possible."""
    h = xxhash.xxh3_128()
    with open(path, "rb", buffering=0) as f:
        with contextlib.suppress(Exception):
            import fcntl

            fcntl.fcntl(f.fileno(), fcntl.F_NOCACHE, 1)  # macOS: really read the disk
        buf = bytearray(8 * 1024**2)
        view = memoryview(buf)
        while n := f.readinto(buf):
            h.update(view[:n])
    return h.hexdigest()


async def copy_clip(afc, clip: Clip, dest: Path, verify: str, on_bytes: Callable[[int], None], on_stage) -> tuple[str, str, str]:
    """Stream one clip to disk and verify it. Returns (digest, verify method, warning)."""
    if dest.exists():
        raise FileExistsError(
            f"a different file is already at {dest} ({dest.stat().st_size:,} bytes vs {clip.size:,} on the phone); "
            "move it aside and re-run"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    xxh = xxhash.xxh3_128()
    sha1 = hashlib.sha1() if verify == "device" else None
    hashers = [h for h in (xxh, sha1) if h is not None]

    # Keep WINDOW positioned reads in flight at once (about 30% faster than one at a time
    # over USB 3), and write the chunks to disk strictly in order.
    handle = await afc.fopen(clip.remote, "r")
    offsets = iter(range(0, clip.size, CHUNK))
    pending: deque[tuple[int, asyncio.Future]] = deque()

    def launch() -> None:
        offset = next(offsets, None)
        if offset is not None:
            want = min(CHUNK, clip.size - offset)
            pending.append((want, asyncio.ensure_future(afc.pread(handle, want, offset))))

    try:
        with open(part, "wb") as f:
            for _ in range(WINDOW):
                launch()
            while pending:
                want, fut = pending.popleft()
                chunk = await asyncio.wait_for(fut, READ_TIMEOUT)
                if len(chunk) != want:
                    raise OSError(f"short read from the phone ({len(chunk):,} of {want:,} bytes)")
                await asyncio.to_thread(_sink, f, hashers, chunk)
                on_bytes(len(chunk))
                launch()
            f.flush()
            os.fsync(f.fileno())
    finally:
        for _, fut in pending:
            fut.cancel()
        await asyncio.gather(*(fut for _, fut in pending), return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(afc.fclose(handle), 10)

    # 1. size on disk, and the phone's copy has not changed underneath us
    on_stage("verifying")
    on_disk = part.stat().st_size
    if on_disk != clip.size:
        raise OSError(f"size mismatch: {on_disk:,} bytes on disk vs {clip.size:,} on the phone")
    now = await afc.stat(clip.remote)
    if now["st_size"] != clip.size:
        raise OSError("the clip changed on the phone during the copy (still recording?)")

    method, warning = "size", ""
    digest = xxh.hexdigest()
    # 2. read the file back from disk and compare with what the phone sent
    if verify in ("readback", "device"):
        if await asyncio.to_thread(hash_file, part) != digest:
            raise OSError("checksum mismatch: the file on disk differs from what the phone sent")
        method = "readback"
    # 3. optionally compare against a SHA-1 computed by the phone itself
    if verify == "device":
        try:
            device_sha1 = await asyncio.wait_for(afc.get_file_hash(clip.remote), 1800)
        except Exception as e:  # noqa: BLE001
            if is_connection_error(e):
                raise
            warning = f"phone could not hash the file ({type(e).__name__}); read-back checksum only"
        else:
            if device_sha1 != sha1.digest():
                raise OSError("checksum mismatch: the phone's own SHA-1 differs from the copied data")
            method = "device"

    os.utime(part, (clip.mtime, clip.mtime))
    os.replace(part, dest)
    return digest, method, warning


class GBColumn(ProgressColumn):
    def render(self, task) -> Text:
        return Text(f"{task.completed / GB:7.1f} / {(task.total or 0) / GB:.1f} GB", style="cyan")


def make_progress() -> Progress:
    return Progress(
        TextColumn("{task.description}"),
        BarColumn(bar_width=28),
        GBColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
    )


async def offload_phone(phone: Phone, episode: str, episode_dir: Path, ledger: Ledger, args, progress: Progress, total_task) -> list[Result]:
    results: list[Result] = []
    todo = list(phone.new)
    task = phone.progress_task = progress.add_task(f"[bold]{phone.cam}[/] waiting", total=sum(c.size for c in todo))
    notify = getattr(progress, "clip_event", lambda *a: None)  # front ends get (phone, clip, status, detail)

    def advance(n: int) -> None:
        progress.advance(task, n)
        if total_task is not None:
            progress.advance(total_task, n)

    def fail_rest(start: int, why: str) -> None:
        for clip in todo[start:]:
            results.append(Result(phone, clip, False, why))
            notify(phone, clip, "failed", why)
            advance(clip.size)

    try:
        async with phone.open_afc() as afc:
            for i, clip in enumerate(todo):
                label = f"[bold]{phone.cam}[/] {clip.rel} ({i + 1}/{len(todo)})"
                progress.update(task, description=label)
                notify(phone, clip, "copying", "")
                dest = episode_dir / phone.cam / clip.rel
                done = 0

                def on_bytes(n: int) -> None:
                    nonlocal done
                    done += n
                    advance(n)

                started = time.monotonic()
                try:
                    digest, method, warning = await copy_clip(
                        afc, clip, dest, args.verify, on_bytes,
                        lambda stage, label=label, clip=clip: (
                            progress.update(task, description=f"{label} [dim]{stage}…[/]"),
                            notify(phone, clip, stage, ""),
                        ),
                    )
                except Exception as e:  # noqa: BLE001
                    advance(clip.size - done)
                    notify(phone, clip, "failed", explain(e) if is_connection_error(e) else str(e))
                    if is_connection_error(e):
                        results.append(Result(phone, clip, False, explain(e)))
                        progress.console.print(f"[red]✗ {phone.cam}  {clip.rel}  {explain(e)}[/]")
                        fail_rest(i + 1, "not attempted: connection to the phone was lost")
                        break
                    results.append(Result(phone, clip, False, str(e)))
                    progress.console.print(f"[red]✗ {phone.cam}  {clip.rel}  {e}[/]")
                    continue
                ledger.record(phone, clip, dest, episode, digest, method)
                secs = max(time.monotonic() - started, 1e-6)
                note = {"size": "size verified", "readback": "checksum verified", "device": "checksum verified vs phone"}[method]
                results.append(Result(phone, clip, True, warning, dest))
                notify(phone, clip, "done", note + (f" ({warning})" if warning else ""))
                progress.console.print(
                    f"[green]✓[/] {phone.cam}  {clip.rel}  {clip.size / GB:.2f} GB  {note}  "
                    f"[dim]{clip.size / 1e6 / secs:.0f} MB/s incl. verify[/]"
                    + (f"  [yellow]⚠ {warning}[/]" if warning else "")
                )
    except Exception as e:  # noqa: BLE001 - could not even open the app's storage
        fail_rest(len(results), explain(e))
        progress.console.print(f"[red]✗ {phone.cam}  {explain(e)}[/]")
    failed = sum(not r.ok for r in results)
    progress.update(task, description=f"[bold]{phone.cam}[/] " + (f"[red]{failed} failed[/]" if failed else "[green]done[/]"))
    return results


# ----------------------------------------------------------------------------- delete


def deletion_plan(phones: list[Phone], ledger: Ledger) -> list[tuple[Phone, list[Clip]]]:
    """Per phone, the clips whose verified copy is on disk right now. Nothing else may be deleted."""
    plan: list[tuple[Phone, list[Clip]]] = []
    for phone in phones:
        if phone.error:
            continue
        eligible = []
        for clip in [c for c, _ in phone.skipped] + phone.new:
            entry = ledger.lookup(phone.udid, clip)
            if not entry:
                continue
            copy = ledger.root / entry["dest"]
            if copy.is_file() and copy.stat().st_size == clip.size:
                eligible.append(clip)
        if eligible:
            plan.append((phone, eligible))
    return plan


async def perform_delete(plan: list[tuple[Phone, list[Clip]]]) -> list[tuple[Phone, int, int, list[str]]]:
    """Delete the planned clips. Returns (phone, removed, planned, errors) per phone."""
    out = []
    for phone, clips in plan:
        removed, errors = 0, []
        try:
            async with phone.open_afc() as afc:
                for clip in clips:
                    try:
                        await afc.rm_single(clip.remote)
                        removed += 1
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"{clip.rel}: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(explain(e))
        out.append((phone, removed, len(clips), errors))
    return out


async def delete_after_verify(phones: list[Phone], ledger: Ledger) -> None:
    """CLI flow: show the plan, require the typed confirmation, then delete."""
    plan = deletion_plan(phones, ledger)
    if not plan:
        console.print("\n[yellow]--delete-after-verify: nothing eligible for deletion.[/]")
        return
    console.print("\n[bold red]Delete from phones[/] — only clips with a verified copy currently on disk:")
    for phone, clips in plan:
        console.print(f"  {phone.cam} ({phone.name}): {len(clips)} clips, {sum(c.size for c in clips) / GB:.1f} GB")
    if not sys.stdin.isatty():
        console.print("[red]Refusing to delete without an interactive confirmation.[/]")
        return
    if console.input("This cannot be undone. Type [bold]delete[/] to confirm: ").strip() != "delete":
        console.print("Nothing deleted.")
        return
    for phone, removed, planned, errors in await perform_delete(plan):
        console.print(f"  {phone.cam}: deleted {removed}/{planned}" + ("" if not errors else f" [red]({len(errors)} errors)[/]"))
        for err in errors:
            console.print(f"    [red]{err}[/]")


# ----------------------------------------------------------------------------- reporting


def print_devices(phones: list[Phone]) -> None:
    table = Table(title="Connected iPhones (USB)")
    for col in ("Camera", "Device name", "Model", "UDID", "Status"):
        table.add_column(col, overflow="fold")
    for p in phones:
        status = f"[red]{p.error}[/]" if p.error else f"[green]ready[/] ({p.bundle_id})"
        cam = p.cam if p.mapped else f"[yellow]{p.cam} (unmapped)[/]"
        table.add_row(cam, p.name, p.model, p.udid, status)
    console.print(table)
    unmapped = [p for p in phones if not p.mapped]
    if unmapped:
        console.print("\nTo name unmapped phones, add to [bold]config.toml[/] under \\[cameras]:")
        for i, p in enumerate(unmapped):
            console.print(f'  "{p.udid}" = "Cam{chr(65 + i)}"   # {p.name}', markup=False)


def print_plan(phones: list[Phone], episode_dir: Path) -> None:
    for p in phones:
        if p.error:
            console.print(f"\n[bold]{p.cam}[/] ({p.name}): [red]{p.error}[/]")
            continue
        table = Table(title=f"{p.cam} — {p.name}  →  {episode_dir / p.cam}", title_justify="left")
        table.add_column("Clip")
        table.add_column("Size", justify="right")
        table.add_column("Recorded")
        table.add_column("Action")
        for clip in p.new:
            table.add_row(clip.rel, f"{clip.size / GB:.2f} GB", f"{datetime.fromtimestamp(clip.mtime):%Y-%m-%d %H:%M}", "[green]copy[/]")
        for clip, why in p.skipped:
            table.add_row(f"[dim]{clip.rel}[/]", f"[dim]{clip.size / GB:.2f} GB[/]", f"[dim]{datetime.fromtimestamp(clip.mtime):%Y-%m-%d %H:%M}[/]", f"[dim]skip: {why}[/]")
        console.print()
        console.print(table)
        console.print(
            f"  {len(p.new)} to copy ({sum(c.size for c in p.new) / GB:.1f} GB), {len(p.skipped)} skipped"
            + (f", {p.ignored} non-video files ignored" if p.ignored else "")
        )


def free_space(path: Path) -> int:
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


# ----------------------------------------------------------------------------- main


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Offload Blackmagic Camera clips from USB-connected iPhones.")
    ap.add_argument("--episode", "-e", help="episode folder name (default: prompt, falling back to today's date)")
    ap.add_argument("--dry-run", "-n", action="store_true", help="list what would be copied and the total size; copy nothing")
    ap.add_argument("--list-devices", action="store_true", help="show connected phones and camera mapping, then exit")
    ap.add_argument("--verify", choices=VERIFY_MODES, default=None,
                    help="size: size only. readback (default, or `verify` in config.toml): checksum the data from the phone against the file re-read "
                         "from disk. device: additionally compare with a SHA-1 computed by the phone (slow)")
    ap.add_argument("--delete-after-verify", action="store_true",
                    help="after copying, offer to delete verified clips from the phones (asks for confirmation)")
    ap.add_argument("--all", action="store_true", help="ignore the ledger and re-copy clips offloaded in earlier episodes")
    ap.add_argument("--match", metavar="GLOB", help="only clips whose path matches, e.g. 'Proxy/*' or '*C002*'")
    ap.add_argument("--sequential", action="store_true", help="copy one phone at a time instead of all in parallel")
    ap.add_argument("--force", action="store_true", help="continue even if free space looks insufficient")
    ap.add_argument("--dest", help="override the destination folder from config.toml")
    ap.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.toml"), help="path to config.toml")
    return ap.parse_args(argv)


def choose_episode(args) -> str:
    default = date.today().isoformat()
    if args.episode:
        return safe_name(args.episode)
    if args.dry_run or not sys.stdin.isatty():
        return default
    return safe_name(console.input(f"Episode name [bold]\\[{default}][/]: ").strip() or default)


async def run(args, cfg: Config, phones: Optional[list[Phone]] = None) -> int:
    if phones is None:
        phones = await connect_phones(cfg)
    if not phones:
        console.print("[red]No iPhones found over USB.[/] Plug them in, unlock them, and tap “Trust” if asked.")
        return 2
    if args.list_devices:
        print_devices(phones)
        return 0

    cams: dict[str, Phone] = {}
    for p in phones:
        if p.cam.casefold() in cams:
            console.print(f"[red]Two phones map to {p.cam}: {cams[p.cam.casefold()].name} and {p.name}. Fix config.toml.[/]")
            return 2
        cams[p.cam.casefold()] = p
    for p in phones:
        if not p.mapped and not p.error:
            console.print(f"[yellow]{p.name} is not in config.toml — using folder “{p.cam}”. Run --list-devices for the line to add.[/]")

    episode = choose_episode(args)
    episode_dir = cfg.destination / episode
    ledger = Ledger(cfg.destination)

    with console.status("Reading clip lists from phones…"):
        await asyncio.gather(*(scan_phone(p, cfg, ledger, episode_dir, args) for p in phones))
    print_plan(phones, episode_dir)

    usable = [p for p in phones if not p.error]
    broken = [p for p in phones if p.error]
    need = sum(c.size for p in usable for c in p.new)
    count = sum(len(p.new) for p in usable)
    free = free_space(cfg.destination)
    console.print(f"\n[bold]Total: {count} clips, {need / GB:.1f} GB to copy.[/] Free at destination: {free / GB:.1f} GB.")
    short = need > free
    if short:
        console.print(f"[bold red]Not enough space: short by {(need - free) / GB:.1f} GB.[/]")
    elif need and free - need < 0.05 * need:
        console.print("[yellow]Warning: this will leave the destination almost full.[/]")

    if args.dry_run:
        console.print("[dim]Dry run — nothing copied.[/]")
        return 1 if broken or short else 0
    if short and not args.force:
        console.print("Free up space or pass --force to try anyway.")
        return 2

    results: list[Result] = []
    if count:
        try:
            lock = destination_lock(cfg.destination)
            lock.__enter__()
        except RuntimeError as e:
            console.print(f"[red]{e}[/]")
            return 2
        console.print()
        with contextlib.closing(contextlib.ExitStack()) as stack, make_progress() as progress:
            stack.callback(lock.__exit__, None, None, None)
            total_task = progress.add_task("[bold]TOTAL[/]", total=need) if len(usable) > 1 else None
            jobs = [offload_phone(p, episode, episode_dir, ledger, args, progress, total_task) for p in usable if p.new]
            if args.sequential:
                for job in jobs:
                    results += await job
            else:
                for r in await asyncio.gather(*jobs):
                    results += r

    # ---- summary
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    skipped = sum(len(p.skipped) for p in usable)
    console.print()
    for p in usable:
        mine = [r for r in results if r.phone is p]
        console.print(
            f"  {p.cam}: {sum(r.ok for r in mine)} copied "
            f"({sum(r.clip.size for r in mine if r.ok) / GB:.1f} GB), {len(p.skipped)} skipped, "
            f"{sum(not r.ok for r in mine)} failed"
        )
    warnings = [r for r in ok if r.detail]
    if bad or broken:
        console.print(f"\n[bold red]❌ Not everything was offloaded.[/] {len(ok)} copied and verified, {skipped} skipped.")
        for p in broken:
            console.print(f"  [red]{p.cam} ({p.name}): {p.error}[/]")
        for r in bad:
            console.print(f"  [red]{r.phone.cam}  {r.clip.rel}: {r.detail}[/]")
        console.print("Re-run the same command to retry — finished clips are skipped.")
    elif not ok and not skipped:
        console.print("\n[yellow]No clips found on the phones.[/]")
    else:
        how = {"size": "size-checked", "readback": "checksum-verified", "device": "checksum-verified against the phones"}[args.verify]
        console.print(
            f"\n[bold green]All files copied and verified ✅[/]  {len(ok)} copied ({sum(r.clip.size for r in ok) / GB:.1f} GB, {how}), "
            f"{skipped} already offloaded.  → {episode_dir}"
        )
    for r in warnings:
        console.print(f"  [yellow]⚠ {r.phone.cam}  {r.clip.rel}: {r.detail}[/]")

    if args.delete_after_verify:
        await delete_after_verify(phones, ledger)
    return 1 if bad or broken else 0


def main() -> None:
    args = parse_args()
    cfg = load_config(Path(args.config), args.dest)
    args.verify = args.verify or cfg.verify
    try:
        code = asyncio.run(run(args, cfg))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Re-run the same command to continue — finished clips are skipped.[/]")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
