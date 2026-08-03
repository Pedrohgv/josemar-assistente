"""Operator helper for printing the immutable Mnemosyne eval identity."""

from __future__ import annotations

import argparse
from pathlib import Path

from .schema import dataset_fingerprint


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the Mnemosyne dataset fingerprint.")
    parser.add_argument("dataset_dir", type=Path)
    args = parser.parse_args()
    print(dataset_fingerprint(args.dataset_dir))
    print("Review instructions: inspect >=50 labels, with >=10 reviewed queries in each difficulty slice; record the timestamp, method, counts, and this fingerprint in review.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
