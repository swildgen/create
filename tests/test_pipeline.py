#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymobiledevice3>=11.61,<12", "rich>=13", "xxhash>=3"]
# ///
"""Offline test of copy / verify / skip / delete using a fake phone backed by a temp folder.

Run:  uv run tests/test_pipeline.py
"""

import asyncio
import contextlib
import hashlib
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import offload  # noqa: E402


class FakeAfc:
    """Just enough of pymobiledevice3's AFC client, serving files from a local folder."""

    def __init__(self, root: Path, corrupt: str = "", die_on: str = ""):
        self.root, self.corrupt, self.die_on = root, corrupt, die_on
        self.handles: dict[int, str] = {}

    def _p(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    async def isdir(self, remote):
        return self._p(remote).is_dir()

    async def walk(self, remote):
        for dirpath, dirnames, filenames in os.walk(self._p(remote)):
            yield "/" + str(Path(dirpath).relative_to(self.root)), dirnames, filenames

    async def stat(self, remote):
        st = self._p(remote).stat()
        return {"st_size": st.st_size, "st_mtime": datetime.fromtimestamp(st.st_mtime), "st_ifmt": "S_IFREG"}

    async def fopen(self, remote, mode="r"):
        self.handles[len(self.handles) + 1] = remote
        return len(self.handles)

    async def pread(self, handle, size, offset):
        remote = self.handles[handle]
        if self.die_on and self.die_on in remote and offset > 0:
            raise ConnectionResetError("unplugged")
        with open(self._p(remote), "rb") as f:
            f.seek(offset)
            data = f.read(size)
        if self.corrupt and self.corrupt in remote and offset == 0:
            data = b"\x00" + data[1:]  # what arrives differs from what is on the phone
        return data

    async def fclose(self, handle):
        pass

    async def get_file_hash(self, remote):
        return hashlib.sha1(self._p(remote).read_bytes()).digest()

    async def rm_single(self, remote, force=False):
        self._p(remote).unlink()
        return True


def make_phone(root: Path, cam: str, **afc_kwargs) -> offload.Phone:
    @contextlib.asynccontextmanager
    async def open_afc():
        yield FakeAfc(root, **afc_kwargs)

    return offload.Phone(udid=f"udid-{cam}", name=f"Phone {cam}", cam=cam, mapped=True, open_afc=open_afc)


def fill(root: Path, files: dict[str, int]) -> None:
    for rel, size in files.items():
        p = root / "Documents/Media" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))


def run(dest: Path, phones, *flags) -> int:
    args = offload.parse_args(["--dest", str(dest), "--config", "/nonexistent", *flags])
    cfg = offload.load_config(Path(args.config), args.dest)
    return asyncio.run(offload.run(args, cfg, phones))


def main() -> None:
    offload.CHUNK = 64 * 1024  # many chunks per file so the read window is exercised
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        a, b, dest = tmp / "phoneA", tmp / "phoneB", tmp / "dest"
        fill(a, {"A001_C001.mov": 1_000_003, "Proxy/A001_C001.mov": 70_001, "notes.txt": 10, "empty.mov": 0})
        fill(b, {"B001_C001.mov": 500_000, "B001_C002.mov": 300_000})

        # 1. dry run copies nothing
        assert run(dest, [make_phone(a, "CamA"), make_phone(b, "CamB")], "--dry-run", "-e", "Ep01") == 0
        assert not dest.exists()

        # 2. real copy, two phones in parallel, strongest verification
        assert run(dest, [make_phone(a, "CamA"), make_phone(b, "CamB")], "-e", "Ep01", "--verify", "device") == 0
        for src_root, cam in ((a, "CamA"), (b, "CamB")):
            for src in (src_root / "Documents/Media").rglob("*.mov"):
                copy = dest / "Ep01" / cam / src.relative_to(src_root / "Documents/Media")
                assert copy.read_bytes() == src.read_bytes(), copy
                assert abs(copy.stat().st_mtime - src.stat().st_mtime) < 1
        assert not (dest / "Ep01/CamA/notes.txt").exists()
        assert not list(dest.rglob("*.part"))

        # 3. next episode: old clips are skipped, only the new one is copied
        fill(a, {"A001_C002.mov": 200_000})
        assert run(dest, [make_phone(a, "CamA")], "-e", "Ep02") == 0
        assert sorted(p.name for p in (dest / "Ep02/CamA").iterdir()) == ["A001_C002.mov"]

        # 4. data corrupted in transit is caught and nothing is left at the final name
        fill(b, {"B001_C003.mov": 400_000})
        assert run(dest, [make_phone(b, "CamB", corrupt="C003")], "-e", "Ep02", "--verify", "device") == 1
        assert not (dest / "Ep02/CamB/B001_C003.mov").exists()
        # ...and a plain re-run recovers
        assert run(dest, [make_phone(b, "CamB")], "-e", "Ep02") == 0
        assert (dest / "Ep02/CamB/B001_C003.mov").read_bytes() == (b / "Documents/Media/B001_C003.mov").read_bytes()

        # 5. a phone unplugged mid-copy fails its clips but not the other phone's
        fill(a, {"A001_C003.mov": 300_000})
        fill(b, {"B001_C004.mov": 300_000, "B001_C005.mov": 100_000})
        assert run(dest, [make_phone(a, "CamA"), make_phone(b, "CamB", die_on="C004")], "-e", "Ep03") == 1
        assert (dest / "Ep03/CamA/A001_C003.mov").exists()
        assert not (dest / "Ep03/CamB/B001_C004.mov").exists() and not (dest / "Ep03/CamB/B001_C005.mov").exists()

        # 6. a phone with an error is reported and makes the run fail
        broken = make_phone(b, "CamB")
        broken.error = "The phone is locked."
        assert run(dest, [make_phone(a, "CamA"), broken], "-e", "Ep03") == 1

        # 7. delete-after-verify: needs the typed confirmation, and only removes verified clips
        assert run(dest, [make_phone(b, "CamB")], "-e", "Ep03") == 0  # finish CamB first
        unverified = a / "Documents/Media/A001_C009.mov"
        phones = [make_phone(a, "CamA")]

        class Tty:
            def isatty(self):
                return True

        real_stdin, real_input = sys.stdin, offload.console.input
        sys.stdin = Tty()
        try:
            offload.console.input = lambda *_a, **_k: "yes"  # wrong word -> nothing deleted
            assert run(dest, phones, "-e", "Ep03", "--delete-after-verify") == 0
            assert (a / "Documents/Media/A001_C001.mov").exists()

            # a clip that fails to copy must survive deletion
            fill(a, {"A001_C009.mov": 150_000})
            offload.console.input = lambda *_a, **_k: "delete"
            assert run(dest, [make_phone(a, "CamA", corrupt="C009")], "-e", "Ep03", "--verify", "device", "--delete-after-verify") == 1
            assert unverified.exists(), "unverified clip was deleted!"
            left = sorted(p.name for p in (a / "Documents/Media").rglob("*.mov"))
            assert left == ["A001_C009.mov"], left
            assert (a / "Documents/Media/notes.txt").exists()
            # every deleted clip still exists, intact, at the destination
            assert (dest / "Ep01/CamA/A001_C001.mov").stat().st_size == 1_000_003
        finally:
            sys.stdin, offload.console.input = real_stdin, real_input

    print("\nALL PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
