#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pymobiledevice3>=11.61,<12",
#     "rich>=13",
#     "xxhash>=3",
#     "pywebview>=5",
# ]
# ///
"""Footage Offload — the window front end for offload.py.

    ./gui.py              open the app window
    ./gui.py --browser    serve the same UI at a local URL instead of opening a window
    ./gui.py --demo       three simulated phones and a temp destination (touches nothing real)

All copying, verifying and deleting is done by the functions in offload.py; this file only
drives them and reports their state to the page in ui.html.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import offload  # noqa: E402
from offload import GB  # noqa: E402

APP_NAME = "Footage Offload"
BUSY = ("copying", "deleting")


class GuiProgress:
    """Stands in for rich's Progress so offload.offload_phone can report into the app."""

    def __init__(self, app: "App"):
        self.app = app
        self.tasks: dict[int, dict[str, int]] = {}
        self.console = self

    def add_task(self, description: str, total: int = 0) -> int:
        self.tasks[len(self.tasks)] = {"total": total, "done": 0}
        return len(self.tasks) - 1

    def advance(self, task: int, n: int) -> None:
        self.tasks[task]["done"] += n

    def update(self, task: int, **_: Any) -> None:
        pass

    def print(self, *_: Any, **__: Any) -> None:
        pass

    def clip_event(self, phone, clip, status: str, detail: str) -> None:
        self.app.clip_status[(phone.udid, clip.remote)] = {"status": status, "detail": detail}


class App:
    def __init__(self, config_path: Path, dest_override: Optional[str], demo: bool):
        self.config_path = config_path
        self.demo = demo
        self.window = None
        self.demo_dir: Optional[tempfile.TemporaryDirectory] = None
        if demo:
            self._setup_demo()
        else:
            if not config_path.exists() and not dest_override:
                example = HERE / "config.example.toml"
                config_path.write_text(example.read_text() if example.exists() else 'destination = "~/Movies/Footage"\n')
            self.cfg = offload.load_config(config_path, dest_override)

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.phase = "scanning"
        self.phones: list[offload.Phone] = []
        self.ledger: Optional[offload.Ledger] = None
        self.episode = self.suggest_episode()
        self.episode_touched = False
        self.clip_status: dict[tuple[str, str], dict[str, str]] = {}
        self.progress: Optional[GuiProgress] = None
        self.total_task: Optional[int] = None
        self.samples: dict[Any, deque] = {}
        self.started_at = 0.0
        self.task: Optional[asyncio.Task] = None
        self.summary: Optional[dict[str, Any]] = None
        self.notice = ""  # one-line problem to show (bad ledger, not enough space, ...)
        self.delete_plan: list[tuple[offload.Phone, list[offload.Clip]]] = []
        self.call(self.refresh(), wait=False)
        if not demo:
            self.call(self.watch(), wait=False)

    # ------------------------------------------------------------------ plumbing

    def call(self, coro, wait: bool = True):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(60) if wait else None

    def _setup_demo(self) -> None:
        sys.path.insert(0, str(HERE / "tests"))
        from test_pipeline import fill, make_phone

        self.demo_dir = tempfile.TemporaryDirectory(prefix="offload-demo-")
        root = Path(self.demo_dir.name)
        self.config_path = root / "config.toml"
        self.cfg = offload.Config(destination=root / "Footage")
        self.cfg.cameras_raw = {"udid-CamA": "CamA", "udid-CamB": "CamB"}
        self.cfg.cameras = {offload.norm(k): v for k, v in self.cfg.cameras_raw.items()}
        self._demo_phones = []
        for i, cam in enumerate(("CamA", "CamB", "CamC")):
            files = {
                f"{cam[-1]}001_11211659_C001.mov": 260_000_000 + 40_000_000 * i,
                f"{cam[-1]}001_11211716_C002.mov": 380_000_000 - 50_000_000 * i,
                f"Proxy/{cam[-1]}001_11211659_C001.mov": 6_000_000,
                f"Proxy/{cam[-1]}001_11211716_C002.mov": 9_000_000,
            }
            fill(root / cam, files)
            phone = make_phone(root / cam, cam, delay=0.45)
            phone.name, phone.model = f"Studio iPhone {i + 1}", "iPhone 18 Pro Max"
            phone.bundle_id = "com.blackmagic-design.DaVinciCamera"
            self._demo_phones.append(phone)

    async def connect(self) -> list[offload.Phone]:
        if not self.demo:
            return await offload.connect_phones(self.cfg)
        for p in self._demo_phones:
            cam = self.cfg.cameras.get(offload.norm(p.udid))
            p.mapped, p.cam = cam is not None, offload.safe_name(cam or p.name)
        return sorted(self._demo_phones, key=lambda p: p.cam.casefold())

    def suggest_episode(self) -> str:
        """Next EpNN if the destination already has numbered episodes, else today's date."""
        best: Optional[tuple[int, str, int]] = None
        with contextlib.suppress(OSError):
            for d in self.cfg.destination.iterdir():
                m = re.fullmatch(r"(.*?)(\d+)", d.name)
                if d.is_dir() and m and not d.name[0].isdigit() and (best is None or int(m[2]) > best[0]):
                    best = (int(m[2]), m[1], len(m[2]))
        return f"{best[1]}{best[0] + 1:0{best[2]}d}" if best else date.today().isoformat()

    def args(self) -> SimpleNamespace:
        return SimpleNamespace(match=None, all=False, verify=self.cfg.verify)

    def episode_dir(self) -> Path:
        return self.cfg.destination / offload.safe_name(self.episode)

    def load_ledger(self) -> bool:
        try:
            self.ledger = offload.Ledger(self.cfg.destination)
            return True
        except SystemExit as e:
            self.ledger, self.notice = None, str(e)
            return False

    def reclassify(self) -> None:
        if not self.ledger:
            return
        for p in self.phones:
            if not p.error:
                offload.classify(p, p.clips, self.ledger, self.episode_dir(), self.args())

    # ------------------------------------------------------------------ actions (run on the loop)

    async def refresh(self) -> None:
        if self.phase in BUSY:
            return
        self.phase = "scanning"
        self.notice = ""
        try:
            old = self.phones
            self.phones = await self.connect()
            for p in old:
                if p.lockdown is not None and all(p.lockdown is not q.lockdown for q in self.phones):
                    with contextlib.suppress(Exception):
                        await p.lockdown.close()
            if self.load_ledger():
                await asyncio.gather(
                    *(offload.scan_phone(p, self.cfg, self.ledger, self.episode_dir(), self.args()) for p in self.phones)
                )
            if not self.episode_touched:
                self.episode = self.suggest_episode()
                self.reclassify()
        except SystemExit as e:
            self.notice = str(e)
        except Exception as e:  # noqa: BLE001
            self.notice = offload.explain(e)
        finally:
            self.phase = "ready"

    async def watch(self) -> None:
        """Rescan when a phone is plugged in or unplugged."""
        from pymobiledevice3.usbmux import list_devices

        while True:
            await asyncio.sleep(3)
            if self.phase != "ready":
                continue
            with contextlib.suppress(Exception):
                now = {d.serial for d in await list_devices() if d.connection_type == "USB"}
                if now != {p.udid for p in self.phones}:
                    await self.refresh()

    async def set_episode(self, name: str) -> None:
        if self.phase in BUSY:
            return
        self.episode, self.episode_touched = name.strip() or self.suggest_episode(), bool(name.strip())
        self.reclassify()

    async def set_config(self, body: dict[str, Any]) -> None:
        if self.phase in BUSY:
            return
        rescan = False
        if body.get("verify") in offload.VERIFY_MODES:
            self.cfg.verify = body["verify"]
        if "include_proxies" in body:
            self.cfg.include_proxies, rescan = bool(body["include_proxies"]), True
        if body.get("destination"):
            self.cfg.destination, rescan = Path(body["destination"]).expanduser(), True
        if "camera" in body:
            udid, cam = body["camera"]["udid"], offload.safe_name(body["camera"]["name"])
            phone = next((p for p in self.phones if p.udid == udid), None)
            drop = {offload.norm(udid)} | ({offload.norm(phone.name)} if phone else set())
            self.cfg.cameras_raw = {k: v for k, v in self.cfg.cameras_raw.items() if offload.norm(k) not in drop}
            self.cfg.cameras_raw[udid] = cam
            self.cfg.cameras = {offload.norm(k): v for k, v in self.cfg.cameras_raw.items()}
            rescan = True
        offload.save_config(self.config_path, self.cfg)
        if rescan:
            await self.refresh()

    async def choose_destination(self) -> None:
        if self.phase in BUSY:
            return
        if self.window is not None:  # native folder picker attached to the app window
            import webview

            picked = await asyncio.to_thread(self.window.create_file_dialog, webview.FOLDER_DIALOG)
            if picked:
                await self.set_config({"destination": str(picked[0] if isinstance(picked, (list, tuple)) else picked)})
            return
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", 'POSIX path of (choose folder with prompt "Where should episode folders be created?")',
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0 and out.strip():
            await self.set_config({"destination": out.decode().strip().rstrip("/")})

    def start(self) -> str:
        if self.phase != "ready":
            return "Busy."
        self.reclassify()
        usable = [p for p in self.phones if not p.error and p.new]
        if not self.ledger:
            return self.notice or "The offload ledger could not be read."
        if not usable:
            return "Nothing new to copy."
        cams = [p.cam.casefold() for p in self.phones if not p.error]
        if len(set(cams)) != len(cams):
            return "Two phones have the same camera name. Give each phone its own name first."
        need = sum(c.size for p in usable for c in p.new)
        free = offload.free_space(self.cfg.destination)
        if need > free:
            return f"Not enough space at the destination: {need / GB:.1f} GB needed, {free / GB:.1f} GB free."
        try:
            self.lock = offload.destination_lock(self.cfg.destination)
            self.lock.__enter__()
        except RuntimeError as e:
            return str(e)
        self.phase, self.summary, self.notice = "copying", None, ""
        self.clip_status.clear()
        self.task = self.loop.create_task(self.copy(usable, need))
        return ""

    async def copy(self, usable: list[offload.Phone], need: int) -> None:
        episode, episode_dir = offload.safe_name(self.episode), self.episode_dir()
        self.progress = GuiProgress(self)
        self.total_task = self.progress.add_task("total", total=need)
        self.samples.clear()
        self.started_at = time.monotonic()
        stopped = False
        results: list[offload.Result] = []
        try:
            self.cfg.destination.mkdir(parents=True, exist_ok=True)
            for group in await asyncio.gather(
                *(offload.offload_phone(p, episode, episode_dir, self.ledger, self.args(), self.progress, self.total_task) for p in usable)
            ):
                results += group
        except asyncio.CancelledError:
            stopped = True
        except Exception as e:  # noqa: BLE001
            self.notice = offload.explain(e)
        finally:
            ok = [r for r in results if r.ok]
            bad = [r for r in results if not r.ok]
            broken = [p for p in self.phones if p.error]
            for key, st in self.clip_status.items():
                if st["status"] in ("copying", "verifying"):
                    st.update(status="failed", detail="stopped before it finished")
            self.summary = {
                "ok": not stopped and not bad and not broken and not self.notice,
                "stopped": stopped,
                "copied": len(ok),
                "copied_bytes": sum(r.clip.size for r in ok),
                "seconds": round(time.monotonic() - self.started_at),
                "verify": self.cfg.verify,
                "episode_dir": str(episode_dir),
                "failures": [{"cam": r.phone.cam, "clip": r.clip.rel, "detail": r.detail} for r in bad]
                + [{"cam": p.cam, "clip": "", "detail": p.error} for p in broken],
                "warnings": [{"cam": r.phone.cam, "clip": r.clip.rel, "detail": r.detail} for r in ok if r.detail],
            }
            self.lock.__exit__(None, None, None)
            self.reclassify()
            self.phase = "ready"

    async def stop(self) -> None:
        if self.phase == "copying" and self.task:
            self.task.cancel()

    async def plan_delete(self) -> None:
        if self.phase != "ready" or not self.ledger:
            return
        self.reclassify()
        self.delete_plan = offload.deletion_plan(self.phones, self.ledger)

    async def confirm_delete(self, word: str) -> None:
        plan, self.delete_plan = self.delete_plan, []
        if word != "delete" or not plan or self.phase != "ready":
            return
        self.phase = "deleting"
        try:
            done = await offload.perform_delete(plan)
            errors = [f"{p.cam}: {e}" for p, _, _, errs in done for e in errs]
            self.summary = {
                "deleted": sum(r for _, r, _, _ in done),
                "deleted_bytes": sum(c.size for _, clips in plan for c in clips) if not errors else None,
                "ok": not errors,
                "failures": [{"cam": "", "clip": "", "detail": e} for e in errors],
                "warnings": [],
            }
        finally:
            self.phase = "ready"
        await self.refresh()

    async def reveal(self) -> None:
        target = self.episode_dir() if self.episode_dir().exists() else self.cfg.destination
        if target.exists():
            subprocess.Popen(["open", str(target)])

    # ------------------------------------------------------------------ state for the page

    def rate(self, key: Any, done: int) -> float:
        q = self.samples.setdefault(key, deque(maxlen=12))
        now = time.monotonic()
        if not q or now - q[-1][0] >= 0.4:
            q.append((now, done))
        if len(q) < 2 or q[-1][0] == q[0][0]:
            return 0.0
        return max(0.0, (q[-1][1] - q[0][1]) / (q[-1][0] - q[0][0]))

    async def state(self) -> dict[str, Any]:
        copying = self.phase == "copying" and self.progress
        plan_ids = {(p.udid, c.remote) for p, clips in (offload.deletion_plan(self.phones, self.ledger) if self.ledger and self.phase == "ready" else []) for c in clips}
        phones = []
        for p in self.phones:
            skipped = {c.remote: why for c, why in p.skipped}
            clips = []
            for c in p.clips:
                st = self.clip_status.get((p.udid, c.remote))
                if st:
                    status, detail = st["status"], st["detail"]
                elif c.remote in skipped:
                    status, detail = "offloaded", skipped[c.remote]
                else:
                    status, detail = "new", ""
                clips.append({"rel": c.rel, "size": c.size, "mtime": c.mtime, "status": status, "detail": detail,
                              "proxy": c.rel.casefold().startswith("proxy/")})
            entry: dict[str, Any] = {
                "udid": p.udid, "name": p.name, "model": p.model, "cam": p.cam, "mapped": p.mapped, "error": p.error,
                "clips": clips, "new_count": len(p.new), "new_bytes": sum(c.size for c in p.new),
                "skipped_count": len(p.skipped), "ignored": p.ignored,
                "deletable": sum(1 for c in p.clips if (p.udid, c.remote) in plan_ids),
            }
            if self.progress and p.progress_task in self.progress.tasks and any((p.udid, c.remote) in self.clip_status for c in p.clips):
                t = self.progress.tasks[p.progress_task]
                entry["progress"] = {"done": t["done"], "total": t["total"],
                                     "rate": self.rate(p.udid, t["done"]) if copying else 0}
            phones.append(entry)
        connected = {offload.norm(p.udid) for p in self.phones} | {offload.norm(p.name) for p in self.phones}
        absent = sorted({v for k, v in self.cfg.cameras_raw.items() if offload.norm(k) not in connected}
                        - {p.cam for p in self.phones})
        total = None
        if self.progress and self.total_task is not None and (copying or self.summary):
            t = self.progress.tasks[self.total_task]
            rate = self.rate("total", t["done"]) if copying else 0
            total = {"done": t["done"], "total": t["total"], "rate": rate,
                     "eta": (t["total"] - t["done"]) / rate if rate > 0 else None,
                     "elapsed": time.monotonic() - self.started_at if copying else None}
        usable = [p for p in self.phones if not p.error]
        episodes = []
        with contextlib.suppress(OSError):
            episodes = sorted(d.name for d in self.cfg.destination.iterdir() if d.is_dir() and not d.name.startswith("."))
        return {
            "phase": self.phase, "demo": self.demo, "notice": self.notice,
            "episode": self.episode, "episodes": episodes,
            "config": {"destination": str(self.cfg.destination), "verify": self.cfg.verify,
                       "include_proxies": self.cfg.include_proxies},
            "free": offload.free_space(self.cfg.destination),
            "need": sum(c.size for p in usable for c in p.new),
            "need_count": sum(len(p.new) for p in usable),
            "phones": phones, "absent": absent, "total": total, "summary": self.summary,
            "delete_plan": [{"cam": p.cam, "name": p.name, "count": len(clips), "bytes": sum(c.size for c in clips)}
                            for p, clips in self.delete_plan],
        }


# ----------------------------------------------------------------------------- http


def make_handler(app: App, token: str):
    page = (HERE / "ui.html").read_text().replace("__TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def authorised(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def do_GET(self):
            if self.path == "/":
                return self.send(200, page.encode(), "text/html; charset=utf-8")
            if self.path == "/api/state" and self.authorised():
                return self.send(200, json.dumps(app.call(app.state())).encode())
            self.send(404, b"{}")

        def do_POST(self):
            if not self.authorised():
                return self.send(403, b"{}")
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            error = ""
            match self.path:
                case "/api/refresh":
                    app.call(app.refresh(), wait=False)
                case "/api/episode":
                    app.call(app.set_episode(str(body.get("episode", ""))))
                case "/api/config":
                    app.call(app.set_config(body), wait=False)
                case "/api/choose-destination":
                    app.call(app.choose_destination(), wait=False)
                case "/api/start":
                    async def go():
                        return app.start()
                    error = app.call(go())
                case "/api/stop":
                    app.call(app.stop())
                case "/api/delete-plan":
                    app.call(app.plan_delete())
                case "/api/delete":
                    app.call(app.confirm_delete(str(body.get("confirm", ""))), wait=False)
                case "/api/reveal":
                    app.call(app.reveal())
                case _:
                    return self.send(404, b"{}")
            self.send(200, json.dumps({"error": error}).encode())

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} window")
    ap.add_argument("--config", default=str(HERE / "config.toml"))
    ap.add_argument("--dest", help="override the destination folder for this session")
    ap.add_argument("--demo", action="store_true", help="simulated phones and a temporary destination")
    ap.add_argument("--browser", action="store_true", help="serve the UI at a local URL instead of opening a window")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    app = App(Path(args.config), args.dest, args.demo)
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app, token))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if args.browser:
        print(url, flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            threading.Event().wait()
        return

    import webview

    with contextlib.suppress(Exception):  # show the app's own name and icon instead of Python's
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
        icon = NSImage.alloc().initWithContentsOfFile_(str(HERE / "assets" / "icon.icns"))
        if icon is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(icon)
    app.window = webview.create_window(APP_NAME, url, width=1180, height=780, min_size=(860, 560), background_color="#131314")
    webview.start()
    os._exit(0)


if __name__ == "__main__":
    main()
