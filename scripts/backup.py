"""Back up the database and the site's code and configuration to one
encrypted file (README.md -> Backups).

    BACKUP_PASSPHRASE=... python scripts/backup.py --out backups

Reads the Turso database when TURSO_DATABASE_URL/TURSO_AUTH_TOKEN are set,
the local SQLite file (data/mangawhere.db) otherwise. The archive holds:

    database.sql   every table as SQL, minus ones that only hold throwaway
                   data (cached scrapes, captchas)
    code.tar       the code and config at this commit (git archive), or the
                   files below when this isn't a git checkout
    manifest.json  when, from where, which commit, row counts

It's encrypted with AES-256 by openssl (the same command decrypts it, see
README.md), so it can sit on any cloud storage. Old backups beyond --keep
in the output folder are removed. Runs daily from
.github/workflows/backup.yml; run it from cron anywhere else.
"""
import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Rebuilt by the app on its own; not worth carrying around.
SKIPPED_TABLES = {"scrape_cache", "captchas"}
# What "the code" means when there's no git to ask.
CODE_PATHS = ("main.py", "scrapers", "server", "index.html", "config.js", "sw.js",
              "ocr-worker.js", "manifest.json", "icons", "requirements.txt", "render.yaml",
              "wrangler.jsonc", "cloudflare", "narrator", ".github", "docs", "scripts", "README.md")
PREFIX = "mangawhere-"
SUFFIX = ".tar.gz.enc"
PLAIN_SUFFIX = ".tar.gz"
OPENSSL_ARGS = ["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt"]


def _database_file(tmp: Path) -> Path:
    """A consistent copy of the live database, as a plain SQLite file."""
    from server import db

    copy = tmp / "copy.db"
    if db.TURSO_DATABASE_URL:
        import libsql

        conn = libsql.connect(str(copy), sync_url=db.TURSO_DATABASE_URL,
                              auth_token=db.TURSO_AUTH_TOKEN)
        conn.sync()
        conn.close()
        return copy
    if not db.DB_PATH.exists():
        raise SystemExit(f"No database at {db.DB_PATH} and TURSO_DATABASE_URL isn't set.")
    # The backup API copies a database that's in use (WAL and all) safely.
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return copy


def dump_database(path: Path):
    """(SQL text, {table: rows}) for every table but SKIPPED_TABLES."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        counts = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in tables if t not in SKIPPED_TABLES}
        # The skipped tables keep their (empty) schema, so a restore still
        # has every table the app expects.
        skipped_rows = tuple(f'INSERT INTO "{t}"' for t in SKIPPED_TABLES)
        lines = [s for s in conn.iterdump() if not s.startswith(skipped_rows)]
    finally:
        conn.close()
    return "\n".join(lines) + "\n", counts


def _code_tar() -> bytes:
    try:
        return subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT,
                              check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name in CODE_PATHS:
                if (ROOT / name).exists():
                    tar.add(ROOT / name, arcname=name,
                            filter=lambda i: None if "__pycache__" in i.name else i)
        return buf.getvalue()


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _add(tar: tarfile.TarFile, name: str, data: bytes, when: float) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(when)
    tar.addfile(info, io.BytesIO(data))


def build_archive(db_file: Path, when: float) -> bytes:
    sql, counts = dump_database(db_file)
    from server import db

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
        "database": "turso" if db.TURSO_DATABASE_URL else "sqlite",
        "commit": _commit(),
        "rows": counts,
        "skipped_tables": sorted(SKIPPED_TABLES),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "database.sql", sql.encode(), when)
        _add(tar, "code.tar", _code_tar(), when)
        _add(tar, "manifest.json", json.dumps(manifest, indent=2).encode(), when)
    return buf.getvalue()


def encrypt(data: bytes, passphrase: str) -> bytes:
    env = dict(os.environ, BACKUP_PASSPHRASE=passphrase)
    return subprocess.run(["openssl", *OPENSSL_ARGS, "-pass", "env:BACKUP_PASSPHRASE"],
                          input=data, env=env, check=True, capture_output=True).stdout


def decrypt(data: bytes, passphrase: str) -> bytes:
    env = dict(os.environ, BACKUP_PASSPHRASE=passphrase)
    return subprocess.run(["openssl", *OPENSSL_ARGS, "-d", "-pass", "env:BACKUP_PASSPHRASE"],
                          input=data, env=env, check=True, capture_output=True).stdout


def prune(out: Path, keep: int) -> list:
    """Remove all but the newest `keep` backups in `out`."""
    backups = sorted(p for p in out.iterdir()
                     if p.name.startswith(PREFIX) and p.name.endswith((SUFFIX, PLAIN_SUFFIX)))
    removed = backups[:-keep] if keep > 0 else []
    for p in removed:
        p.unlink()
    return removed


def main(argv=None) -> Path:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "backups"), help="folder to write to")
    parser.add_argument("--keep", type=int, default=14, help="newest backups to keep (0: all)")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="write a plain .tar.gz (only for storage you trust)")
    args = parser.parse_args(argv)

    passphrase = os.environ.get("BACKUP_PASSPHRASE", "")
    if not passphrase and not args.no_encrypt:
        raise SystemExit("Set BACKUP_PASSPHRASE (or pass --no-encrypt to write it unencrypted).")

    when = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        archive = build_archive(_database_file(Path(tmp)), when)
    if not args.no_encrypt:
        archive = encrypt(archive, passphrase)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(when))
    path = out / f"{PREFIX}{stamp}{PLAIN_SUFFIX if args.no_encrypt else SUFFIX}"
    path.write_bytes(archive)
    for old in prune(out, args.keep):
        print(f"removed {old.name}")
    print(f"wrote {path} ({len(archive) / 1024:.0f} KB)")
    return path


if __name__ == "__main__":
    main()
