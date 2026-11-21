# iPhone footage offload

Copies Blackmagic Camera clips off every iPhone plugged in over USB, into
`<destination>/<episode>/<CamA|CamB|CamC>/`, verifies each file, and never deletes
anything from a phone unless you explicitly ask and confirm.

## The app: Create

**Create.app** is the normal way to use this. Install it once (download
`Create-x.y.z.zip` from the [latest release](https://github.com/swildgen/create/releases/latest),
unzip, drag to `/Applications`) and it keeps itself up to date.

- One lane per camera. Phones appear on their own when plugged in; type in a lane's
  title to name that camera.
- Episode name, destination, verification mode and proxies along the top. These are
  your presets: they load from `~/.create/config.toml` and changes are saved back.
- **Offload** copies all phones in parallel with live progress, then reports
  "All files copied and verified" or lists what failed, per clip.
- **Delete … from phones** only appears when verified copies exist, lists exactly what
  would be removed, and needs the word `delete` typed before it does anything.

Before first use, plug each phone in once, unlock it and tap **Trust This Computer**.

### Updates

The app checks GitHub for a newer release at launch and every few hours, downloads it in
the background, verifies its SHA-256, and shows **Restart Now** when it is ready. If you
ignore that, the update installs when you quit, or at the next launch. It never restarts
during an offload. The previous version is kept until the new one has started, and is
put back if it does not. Log: `~/.create/updates/update.log`.

### Shipping a new version

```bash
./release.sh 1.0.1 "What changed"
```

builds `dist/Create.app`, zips it, commits, pushes, and publishes a GitHub release.
Every installed copy then updates itself. `./build.sh` alone just builds the app.

## Command line

The same engine without the window. Needs [uv](https://docs.astral.sh/uv/)
(`brew install uv`); the script installs its own dependencies on first run. It shares
`~/.create/config.toml` and the ledger with the app (see `config.example.toml`).
`./offload.py --list-devices` prints ready-to-paste `[cameras]` lines.


```bash
./offload.py --dry-run            # what would be copied, total size, free space
./offload.py --episode Ep05       # copy + verify (omit --episode to be prompted; default is today's date)
```

Keep the phones unlocked until the copy has started. All phones copy in parallel.
If anything fails or you interrupt it, run the same command again: finished clips
are skipped and only the rest is copied.

Result:

```
<destination>/
  Ep05/
    CamA/A001_11211659_C001.mov
    CamA/Proxy/A001_11211659_C001.mov
    CamB/…
    CamC/…
  .offload-ledger.json
```

## Options

| Flag | What it does |
| --- | --- |
| `--dry-run`, `-n` | List clips and sizes per phone; copy nothing. |
| `--episode NAME`, `-e` | Episode folder name. |
| `--verify size\|readback\|device` | See below. Default `readback`. |
| `--delete-after-verify` | After copying, offer to delete verified clips from the phones. |
| `--match GLOB` | Only some clips, e.g. `--match 'Proxy/*'` or `--match '*C002*'`. |
| `--all` | Ignore the ledger and re-copy clips offloaded in earlier episodes. |
| `--sequential` | One phone at a time. |
| `--dest PATH` | Override `destination` for this run. |
| `--force` | Continue even if free space looks insufficient. |

## How "new clips" works

Every verified clip is recorded in `<destination>/.offload-ledger.json` (phone UDID,
path, size, timestamp, checksum, where it went). A clip already in the ledger is skipped
on later runs, so clips left on the phones are not copied again into the next episode.
Use `--all` to override. Don't delete the ledger unless you want everything re-copied.

## Verification

Each clip is written to `name.mov.part` and only renamed to its final name after it
passes, so a file with a normal name is always a complete, verified copy.

- `size`: byte count on disk equals the phone's.
- `readback` (default): the above, plus a checksum (xxh3) of the data received from the
  phone is compared with the file re-read from disk. Catches disk/write errors. Adds
  roughly 10% to the run.
- `device`: the above, plus the phone computes its own SHA-1 of the clip and it must
  match. This is a true end-to-end check but the phone has to re-read every file, so it
  takes noticeably longer on 100 GB clips.

## Deleting from the phones

`--delete-after-verify` lists, per phone, the clips whose verified copy is on disk right
now (same size, recorded in the ledger), and deletes them only after you type `delete`.
Clips that failed, were skipped for a size conflict, or whose copy has since been moved
are left on the phone. It refuses to run without an interactive terminal.

## Troubleshooting

- **"Not trusted yet"**: unlock the phone, tap Trust, enter the passcode, re-run.
- **"The phone is locked"**: unlock it and re-run.
- **"No iPhones found over USB"**: check the cable (use a USB 3 / 10 Gb/s cable, the
  charging cable in the box is USB 2 and about 10x slower), and that the phone shows up
  in Finder.
- **A different file is already at …**: something with the same name but another size is
  in the episode folder. Move it aside and re-run.
- Clips live in the app's `Media` folder; LUTs and presets are ignored. Set
  `include_proxies = false` in `config.toml` to skip `Media/Proxy`.

## Tests

`./gui.py --demo` opens the window from source with three simulated phones and a
temporary destination. `uv run tests/test_pipeline.py` exercises copy, verify, skip, failure and delete paths
against a fake phone (a temp folder); it needs no devices.
