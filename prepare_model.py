from __future__ import annotations

"""Convert Whisper model weights to local CTranslate2 format.

This is a one-time prep step that downloads model assets and writes a local
directory optimized for `faster-whisper`.
"""

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Whisper V3 Turbo to local CTranslate2 format.")
    parser.add_argument(
        "--model-id",
        default="openai/whisper-large-v3-turbo",
        help="Hugging Face model ID to convert.",
    )
    parser.add_argument(
        "--output-dir",
        default="models/whisper-large-v3-turbo-ct2-int8",
        help="Destination directory for converted model.",
    )
    parser.add_argument(
        "--quantization",
        default="int8",
        choices=("float32", "float16", "int16", "int8", "int8_float16"),
        help="Quantization type used in CTranslate2 conversion.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output directory if it already exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    # Ensure the parent path exists before conversion starts.
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        from ctranslate2.converters import TransformersConverter
    except ImportError:
        print("Missing dependency: ctranslate2")
        print("Install dependencies first: pip install -r requirements.txt")
        return 1

    print(f"Converting {args.model_id} -> {output_dir} ({args.quantization})")
    converter = TransformersConverter(args.model_id)
    converter.convert(
        output_dir=str(output_dir),
        quantization=args.quantization,
        force=args.force,
        # Preserve tokenizer/preprocessing metadata next to model weights so
        # runtime scripts are fully self-contained with local assets.
        copy_files=["tokenizer.json", "preprocessor_config.json"],
    )
    print("Conversion complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
