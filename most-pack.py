#!/usr/bin/env python3
"""
most-pack.py — Build, pack & upload MOST localization artifacts.

Reads most_locales.json and locales/<lang_code>/{app,mods}/ folders,
then for each language:
  1. Reads Korabli.Most.exe ProductVersion → supported_most_version
  2. Gets file date → app_version / mods_version
  3. Creates most_l10n_app.7z and most_l10n_mods.7z
  4. Uploads to Cloudflare R2
Finally updates metadata/l10n.json with all language entries.
"""

import json
import os
import subprocess
import sys
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────
LOCALES_FILE = "most_locales.json"
LOCALES_DIR = Path("locales")
METADATA_DIR = Path("metadata")
METADATA_FILE = METADATA_DIR / "l10n.json"
R2_BASE_URL = os.environ.get("R2_PUBLIC_URL", "")
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_PREFIX = "lateral/most"


def log_info(msg):
    print(f"[INFO] {msg}")


def log_error(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)


def run(cmd, **kwargs):
    """Run a shell command and return output."""
    log_info(f"Running: {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        log_error(f"Command failed (exit {result.returncode}):\n{result.stderr}")
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}")
    return result.stdout.strip()


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)


def get_file_version_from_pe(exe_path: Path) -> str:
    """Extract ProductVersion from a PE executable using pefile."""
    import pefile
    pe = pefile.PE(str(exe_path))

    # Navigate the version info structure
    if hasattr(pe, "FileInfo") and pe.FileInfo:
        for file_info in pe.FileInfo:
            if hasattr(file_info, "StringTable"):
                for st in file_info.StringTable:
                    if hasattr(st, "entries"):
                        for key_bytes, value_bytes in st.entries.items():
                            key = key_bytes.decode("utf-8", errors="replace") if isinstance(key_bytes, bytes) else key_bytes
                            if key == "ProductVersion":
                                return value_bytes.decode("utf-8", errors="replace") if isinstance(value_bytes, bytes) else value_bytes

    raise ValueError(f"Cannot read ProductVersion from {exe_path}")


def get_file_version_from_exiftool(exe_path: Path) -> str:
    """Fallback: use exiftool to get ProductVersion."""
    output = run(["exiftool", "-ProductVersion", "-s3", str(exe_path)])
    return output if output else "0.0.0.0"


def get_product_version(exe_path: Path) -> str:
    """Get ProductVersion, trying pefile first then exiftool."""
    try:
        return get_file_version_from_pe(exe_path)
    except Exception as e:
        log_info(f"pefile failed for {exe_path.name}: {e}, trying exiftool...")
        try:
            return get_file_version_from_exiftool(exe_path)
        except Exception as e2:
            log_error(f"Cannot read version from {exe_path}: {e2}")
            return "0.0.0.0"


def get_date_version(file_path: Path) -> str:
    """Get file modification date formatted as YY.M.D (e.g. '26.5.19')."""
    mtime = os.path.getmtime(file_path)
    dt = datetime.fromtimestamp(mtime)
    return f"{dt.year % 100}.{dt.month}.{dt.day}"


def create_7z(source_dir: Path, output_path: Path):
    """Create a 7z archive from a directory's contents."""
    if not source_dir.exists() or not any(source_dir.iterdir()):
        log_info(f"  ⚠️  {source_dir.name} is empty or missing, skipping")
        return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # We want the contents of the folder, not the folder itself
    run(["7z", "a", "-mx=9", "-t7z", str(output_path), "."], cwd=str(source_dir))
    log_info(f"  ✅ Created {output_path.name} ({output_path.stat().st_size / 1024 / 1024:.1f} MB)")
    return output_path


def upload_to_r2(local_path: Path, remote_key: str):
    """Upload a file to Cloudflare R2."""
    if R2_ENDPOINT and R2_BUCKET:
        run([
            "aws", "s3", "cp", str(local_path),
            f"s3://{R2_BUCKET}/{remote_key}",
            "--endpoint-url", R2_ENDPOINT,
            "--region", "auto"
        ])
        log_info(f"  ☁️  Uploaded to s3://{R2_BUCKET}/{remote_key}")
    else:
        log_info(f"  📦  Skipping upload (no R2 config)")


def build_metadata_entry(lang_code: str, lang_name: str, app_archive: str,
                         app_version: str, supported_most_version: str,
                         mods_archive: str, mods_version: str) -> dict:
    """Build a metadata entry for one language."""
    entry = {
        "id": lang_code,
        "name": lang_name,
        "l10n_app": {
            "path": f"{R2_BASE_URL}/{R2_PREFIX}/{lang_code}/{app_archive}",
            "version": app_version,
            "supported_most_version": supported_most_version
        },
        "l10n_mods": {
            "path": f"{R2_BASE_URL}/{R2_PREFIX}/{lang_code}/{mods_archive}",
            "version": mods_version
        }
    }
    return entry


def merge_metadata(existing: list, new_entries: list) -> list:
    """Merge new entries into existing metadata list, updating matching entries."""
    existing_map = {entry["id"]: entry for entry in existing}
    for entry in new_entries:
        existing_map[entry["id"]] = entry
    return list(existing_map.values())


def main():
    # ─── Load locales mapping ────────────────────────────────
    if not os.path.exists(LOCALES_FILE):
        log_error(f"{LOCALES_FILE} not found. Run from project root.")
        sys.exit(1)

    with open(LOCALES_FILE, encoding="utf-8") as f:
        locales = json.load(f)

    if not isinstance(locales, dict):
        log_error(f"{LOCALES_FILE} should be a dictionary (lang_code -> lang_name)")
        sys.exit(1)

    log_info(f"Loaded {len(locales)} locales: {', '.join(locales.keys())}")

    # ─── Check tools ─────────────────────────────────────────
    try:
        run(["7z"], check=False)
    except Exception:
        log_info("Installing p7zip...")
        run(["sudo", "apt-get", "update", "-qq"])
        run(["sudo", "apt-get", "install", "-y", "-qq", "p7zip-full"])

    # ─── Process each language ───────────────────────────────
    new_entries = []
    temp_dir = Path(tempfile.mkdtemp(prefix="most-pack-"))
    archives_dir = temp_dir / "archives"

    try:
        for lang_code, lang_name in sorted(locales.items()):
            lang_dir = LOCALES_DIR / lang_code
            if not lang_dir.exists():
                log_info(f"  ⚠️  Skipping {lang_code}: folder not found")
                continue

            app_dir = lang_dir / "app"
            mods_dir = lang_dir / "mods"
            exe_path = app_dir / "Korabli.Most.exe"

            log_info(f"── Processing: {lang_code} ({lang_name}) ──")

            # Get version info
            if not exe_path.exists():
                log_error(f"  ❌ {exe_path} not found, skipping {lang_code}")
                continue

            supported_most_version = get_product_version(exe_path)
            app_version = get_date_version(exe_path)
            log_info(f"  Supported MOST: {supported_most_version}")
            log_info(f"  App version: {app_version}")

            # Get mods version from first file in mods/
            mods_version = app_version  # fallback
            if mods_dir.exists():
                mod_files = sorted(mods_dir.iterdir())
                if mod_files:
                    mods_version = get_date_version(mod_files[0])
            log_info(f"  Mods version: {mods_version}")

            # Create archives
            lang_archives = archives_dir / lang_code
            ensure_dir(lang_archives)

            app_archive_path = lang_archives / "most_l10n_app.7z"
            mods_archive_path = lang_archives / "most_l10n_mods.7z"

            app_ok = create_7z(app_dir, app_archive_path) is not None
            mods_ok = create_7z(mods_dir, mods_archive_path) is not None

            if not app_ok and not mods_ok:
                log_error(f"  ❌ No archives created for {lang_code}, skipping")
                continue

            # Upload to R2
            if app_ok:
                upload_to_r2(app_archive_path, f"{R2_PREFIX}/{lang_code}/most_l10n_app.7z")
            if mods_ok:
                upload_to_r2(mods_archive_path, f"{R2_PREFIX}/{lang_code}/most_l10n_mods.7z")

            # Build metadata entry
            entry = build_metadata_entry(
                lang_code=lang_code,
                lang_name=lang_name,
                app_archive="most_l10n_app.7z",
                app_version=app_version,
                supported_most_version=supported_most_version,
                mods_archive="most_l10n_mods.7z",
                mods_version=mods_version
            )
            new_entries.append(entry)

    finally:
        # Clean up temp files
        shutil.rmtree(temp_dir, ignore_errors=True)

    # ─── Update metadata/l10n.json ───────────────────────────
    if not new_entries:
        log_error("No entries processed, not updating metadata")
        sys.exit(1)

    ensure_dir(METADATA_DIR)

    existing = []
    if METADATA_FILE.exists():
        with open(METADATA_FILE, encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = []
        log_info(f"Loaded existing metadata with {len(existing)} entries")

    merged = merge_metadata(existing, new_entries)
    # Sort by language code
    merged.sort(key=lambda x: x["id"])

    with open(METADATA_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=4)
    log_info(f"✅ Updated {METADATA_FILE} with {len(merged)} entries")
    print(json.dumps(merged, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
