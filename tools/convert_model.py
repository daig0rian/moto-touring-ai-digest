#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "ctranslate2",
#   "transformers",
#   "torch",
# ]
# ///
"""
HuggingFace Whisperモデル → CTranslate2形式 変換スクリプト
実行: uv run tools/convert_model.py

faster-whisperはCTranslate2形式（model.bin）が必要。
kotoba-whisperはtransformers形式で公開されているため要変換。
変換は1回だけ実行すればOK。
"""

import time
from pathlib import Path
from ctranslate2.converters import TransformersConverter

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SRC_MODEL = "kotoba-tech/kotoba-whisper-v2.1"
OUT_DIR   = PROJECT_ROOT / "models/kotoba-whisper-v2.1-ct2"
QUANT     = "float16"   # GPU用。CPUのみなら "int8"

def main():
    if OUT_DIR.exists() and (OUT_DIR / "model.bin").exists():
        print(f"変換済みモデルが既に存在します: {OUT_DIR}")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"変換元: {SRC_MODEL}")
    print(f"変換先: {OUT_DIR}")
    print(f"量子化: {QUANT}")
    print()

    print("ダウンロード＆変換中（初回のみ、数分かかります）...")
    t0 = time.time()
    converter = TransformersConverter(SRC_MODEL)
    converter.convert(str(OUT_DIR), quantization=QUANT, force=True)

    elapsed = time.time() - t0
    size_mb = sum(f.stat().st_size for f in OUT_DIR.rglob("*") if f.is_file()) / 1e6
    print(f"✓ 変換完了  ({elapsed:.0f}秒, {size_mb:.0f} MB)")
    print(f"  {OUT_DIR}")
    print()
    print("transcribe.py の WHISPER_MODEL をこのパスに変更してください:")
    print(f'  WHISPER_MODEL = r"{OUT_DIR}"')

if __name__ == "__main__":
    main()
