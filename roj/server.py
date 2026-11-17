"""`roj serve`: the one long-running process of a server installation.

Every tick it processes owner clicks from Telegram, resumes parked work (so an approval
continues its task within seconds), starts due schedules and delivers notifications. The
panel runs in the same process on localhost. State is durable: a restart during a deploy
loses nothing, the next tick resumes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .util import day_key, utcnow_iso

if TYPE_CHECKING:
    from .project import Installation

log = logging.getLogger("roj.serve")


def backup(inst: "Installation", *, with_cas: bool = False) -> Path:
    """Consistent snapshot of every database plus settings, Playbook and reports as one tar.gz.

    Databases are copied with SQLite's online backup API, so this is safe while roj runs. The
    archive is written locally and then copied to [storage] backup_dir (e.g. a mounted bucket):
    live SQLite files must never sit on a network or FUSE mount themselves.
    """
    home, cfg = inst.home, inst.settings.storage
    dest = Path(cfg.backup_dir).expanduser() if cfg.backup_dir else home / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    name = f"roj-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "roj-home"
        for db in [home / "roj.db", *sorted(home.glob("projects/*/state.db"))]:
            out = stage / db.relative_to(home)
            out.parent.mkdir(parents=True, exist_ok=True)
            src, dst = sqlite3.connect(db), sqlite3.connect(out)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        keep = ["roj.toml", "projects/*/project.toml", "projects/*/reports/*", "projects/*/blueprint*", "projects/*/adr/*",
                "playbook/*.yaml", "playbook/versions/*", "playbook/models/*", "pool/**/*"] + (["projects/*/cas/**/*"] if with_cas else [])
        for pattern in keep:
            for f in home.glob(pattern):
                if f.is_file():
                    out = stage / f.relative_to(home)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)
        local = Path(tmp) / name
        with tarfile.open(local, "w:gz") as tar:
            tar.add(stage, arcname="roj-home")
        shutil.copyfile(local, dest / name)
    old = sorted(dest.glob("roj-backup-*.tar.gz"))[: -max(1, cfg.backup_keep)]
    for f in old:
        f.unlink(missing_ok=True)
    inst.events.append("backup.created", {"file": str(dest / name), "kb": round((dest / name).stat().st_size / 1024), "with_cas": with_cas},
                       actor="serve")
    return dest / name


async def serve_forever(inst: "Installation", *, host: str = "127.0.0.1", port: int = 8765, panel: bool = True,
                        interval_s: float = 30.0, allow_remote: bool = False, ticks: int | None = None) -> None:
    from .scheduler import tick

    server = None
    if panel:
        from .panel.server import serve

        server, _, _ = serve(inst.home, host, port, allow_remote=allow_remote)
        threading.Thread(target=server.serve_forever, daemon=True, name="roj-panel").start()
        print(f"roj panel on http://{host}:{server.server_address[1]}/ (token in {inst.home / 'panel_token'})", flush=True)
    from . import __version__

    row = inst.store.one("SELECT value FROM kv WHERE key = 'last_version'")
    if row and row["value"] != __version__:
        try:  # a new image started: keep a snapshot of the data as it was under the old version
            path = await asyncio.to_thread(backup, inst)
            log.info("upgrade %s -> %s: backup %s", row["value"], __version__, path)
            print(f"upgrade {row['value']} -> {__version__}: backup {path}", flush=True)
        except Exception:  # noqa: BLE001
            log.exception("pre-upgrade backup failed")
    if not row or row["value"] != __version__:
        inst.store.run("INSERT OR REPLACE INTO kv(key, value, updated) VALUES('last_version', ?, ?)", (__version__, utcnow_iso()))
        inst.events.append("serve.version", {"version": __version__, "previous": row["value"] if row else None}, actor="serve")
    n = 0
    try:
        while ticks is None or n < ticks:
            n += 1
            t0 = time.monotonic()
            (inst.home / "serve.heartbeat").write_text(utcnow_iso(), encoding="utf-8")  # alive even if this tick runs a long task
            try:
                report = await tick(inst)
                if any(k not in ("outbox",) for r in report for k in r):
                    log.info("tick: %s", report)
            except Exception:  # noqa: BLE001 - one bad tick must not stop the service
                log.exception("tick failed")
            try:
                today = day_key(inst.settings.timezone)
                row = inst.store.one("SELECT value FROM kv WHERE key = 'last_backup_day'")
                if not row or row["value"] != today:  # daily, to [storage] backup_dir or <home>/backups
                    path = await asyncio.to_thread(backup, inst)
                    inst.store.run("INSERT OR REPLACE INTO kv(key, value, updated) VALUES('last_backup_day', ?, ?)", (today, utcnow_iso()))
                    log.info("daily backup: %s", path)
            except Exception:  # noqa: BLE001
                log.exception("backup failed")
            (inst.home / "serve.heartbeat").write_text(utcnow_iso(), encoding="utf-8")
            if ticks is None or n < ticks:
                await asyncio.sleep(max(1.0, interval_s - (time.monotonic() - t0)))
    finally:
        if server:
            server.shutdown()
            server.server_close()
