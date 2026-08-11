import duckdb
import os
import shutil
import zipfile
from pathlib import Path

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = Path(BASE) / "options_data"

SCHEMA = """
    CREATE TABLE IF NOT EXISTS options_trades (
        product_symbol VARCHAR,
        option_type VARCHAR,
        strike DOUBLE,
        expiry DATE,
        price DOUBLE,
        size DOUBLE,
        timestamp TIMESTAMP,
        buyer_role VARCHAR
    );
    CREATE TABLE IF NOT EXISTS loaded_files (
        file_name VARCHAR PRIMARY KEY
    );
"""


def underlying_of(zip_name):
    # options-trades-monthly-BTC-2025-09.csv.zip -> BTC
    # options-trades-monthly-BTC-2026-04.csv (1).zip -> BTC
    return zip_name.split("-")[3]


def load_zip(con, zip_path):
    name = zip_path.name
    if con.execute("SELECT 1 FROM loaded_files WHERE file_name = ?", [name]).fetchone():
        print(f"  [skip] {name}")
        return
    with zipfile.ZipFile(zip_path) as z:
        members = [n for n in z.namelist() if not n.endswith('/')]
        if not members:
            raise RuntimeError(f"no file member in {zip_path}")
        csv_name = members[0]
        z.extract(csv_name, BASE)
    csv_path = os.path.join(BASE, csv_name).replace("\\", "/")
    # If the zip wrapped the csv in a directory, that dir lives under BASE too.
    top = os.path.join(BASE, csv_name.split("/")[0])
    try:
        con.execute(f"""
            INSERT INTO options_trades
            SELECT
                product_symbol::VARCHAR,
                parts[1]::VARCHAR AS option_type,
                parts[3]::DOUBLE AS strike,
                strptime(parts[4], '%d%m%y')::DATE AS expiry,
                price::DOUBLE,
                size::DOUBLE,
                timestamp::TIMESTAMP,
                buyer_role::VARCHAR
            FROM (
                SELECT *, string_split(product_symbol, '-') AS parts
                FROM read_csv_auto('{csv_path}')
            )
        """)
        con.execute("INSERT INTO loaded_files VALUES (?)", [name])
        print(f"  [ok  ] {name}")
    finally:
        if os.path.isdir(top):
            shutil.rmtree(top, ignore_errors=True)
        else:
            try:
                os.remove(top)
            except OSError:
                pass


def main():
    zips = sorted(DATA_DIR.rglob("*.zip"))
    if not zips:
        print(f"No .zip files under {DATA_DIR}")
        return

    by_underlying = {}
    for z in zips:
        by_underlying.setdefault(underlying_of(z.name), []).append(z)

    for underlying in sorted(by_underlying):
        files = by_underlying[underlying]
        db_path = os.path.join(BASE, f"{underlying.lower()}_options.duckdb")
        print(f"\n=== {underlying} ({len(files)} files) -> {os.path.basename(db_path)} ===")
        con = duckdb.connect(db_path)
        try:
            con.execute(SCHEMA)
            for z in files:
                try:
                    load_zip(con, z)
                except Exception as e:
                    print(f"  [err ] {z.name}: {e}")
            count = con.execute("SELECT COUNT(*) FROM options_trades").fetchone()[0]
            print(f"  total rows: {count}")
        finally:
            con.close()


if __name__ == "__main__":
    main()
