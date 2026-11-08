"""
00_download_data.py
Downloads the source dataset (V1 file, 5,000,000 rows, ~969 MB) from Hugging Face
into data/raw/ and verifies its SHA-256 checksum.

Source : https://huggingface.co/datasets/electricsheepafrica/Nigerian-Financial-Transactions-and-Fraud-Detection-Dataset
File   : V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv
Licence: GPL (as declared on the dataset card)

The raw file is git-ignored because it exceeds GitHub's 100 MB file limit.
Run    : python scripts/00_download_data.py
"""
import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = ("https://huggingface.co/datasets/electricsheepafrica/"
       "Nigerian-Financial-Transactions-and-Fraud-Detection-Dataset/resolve/main/"
       "V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv")
DEST = ROOT / "data" / "raw" / "V1-nigerian-financial-transactions-and-fraud-detection-dataset.csv"
EXPECTED_SHA256 = "ed7753fbd7da6d775ce1cd0a339c15abdcc304dae6f668643d15f7e9427cbe09"
EXPECTED_BYTES = 968_831_912


def main() -> None:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if not DEST.exists() or DEST.stat().st_size != EXPECTED_BYTES:
        print(f"Downloading {URL}")
        urllib.request.urlretrieve(URL, DEST)
    h = hashlib.sha256()
    with open(DEST, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    ok = h.hexdigest() == EXPECTED_SHA256
    print(f"{DEST.name}: {DEST.stat().st_size:,} bytes, sha256 {'OK' if ok else 'MISMATCH ' + h.hexdigest()}")


if __name__ == "__main__":
    main()
