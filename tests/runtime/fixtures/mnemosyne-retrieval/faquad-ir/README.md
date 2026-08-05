# Public FaQuAD-IR Activation Fixture

This is the complete `MTEB-BR/faquad-ir` test split, deterministically converted from revision `c081a26d706764f1d09de17792f5eb995f51b124`. It contains 244 corpus passages, 900 queries, and all 900 positive qrels.

The benchmark is licensed CC-BY-4.0. See `manifest.json` and `LICENSE.txt` for source URLs, hashes, attribution, and citation. Query difficulty values are a deterministic word-count proxy only, not human difficulty, and are not used for activation thresholds. No `review.json` is required for this official immutable benchmark.

Regenerate with `scripts/mnemosyne_retrieval_eval/vendor_faquad_ir.py` using pinned `pyarrow==25.0.0` in a disposable environment under `dump_folder/`, an explicit retrieval date, and the downloaded CC-BY-4.0 legal code.
