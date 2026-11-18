"""Run isolated, authenticated image acceptance with the real Cursor CLI and Provider."""

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

from cursor_plugin.errors import CursorError
from cursor_plugin.images import MAX_IMAGE_BYTES


def make_card(path):
    from PIL import Image, ImageDraw, ImageFont

    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(7))
    with Image.new("RGB", (760, 300), "white") as image:
        canvas = ImageDraw.Draw(image)
        canvas.text(
            (32, 35),
            "Read and reply with this code:",
            fill="black",
            font=ImageFont.load_default(size=35),
        )
        canvas.text((32, 125), code, fill="black", font=ImageFont.load_default(size=100))
        image.save(path)
    return code


async def check(config):
    from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

    from cursor_plugin import runtime
    from cursor_plugin.provider import CursorCLIProvider

    runtime.configure(config)
    if not runtime.settings()["requested_model"]:
        raise CursorError("model")
    await runtime.ensure_model_validated(startup=True)
    model = runtime.settings()["model"]
    root = Path(get_astrbot_temp_path())
    root.mkdir(parents=True, exist_ok=True)
    failures = 0

    provider = CursorCLIProvider({"id": model, "type": "cursor_cli", "model": model}, {})
    try:
        first, second = root / "image-one.png", root / "image-two.png"
        first_code, second_code = make_card(first), make_card(second)
        third, fourth = root / "image-three.jpg", root / "image-four.webp"
        third_code, fourth_code = make_card(third), make_card(fourth)
        invalid, oversized = root / "invalid.png", root / "oversized.png"
        invalid.write_bytes(b"not an image")
        with oversized.open("wb") as file:
            file.truncate(MAX_IMAGE_BYTES + 1)
        cases = [
            ("image-only", None, [first], (first_code,), None),
            (
                "text and image",
                "Read the code in the image and reply with it.",
                [second],
                (second_code,),
                None,
            ),
            (
                "two images",
                "Read both codes in attachment order. Give one code per line.",
                [third, fourth],
                (third_code, fourth_code),
                None,
            ),
            ("invalid image", "Read the image.", [invalid], (), "image_invalid"),
            ("oversized image", "Read the image.", [oversized], (), "image_limit"),
            ("text after images", "Reply with exactly TEXT-OK.", [], ("TEXT-OK",), None),
        ]
        for label, text, paths, codes, expected_error in cases:
            try:
                response = await provider.text_chat(
                    prompt=text,
                    image_urls=[str(path) for path in paths],
                    conversation_id=label,
                    request_max_retries=0,
                )
            except CursorError as exc:
                ok = exc.code == expected_error
                detail = f"code={exc.code}"
            else:
                answer = response.completion_text or ""
                positions = [answer.upper().find(code) for code in codes]
                ok = expected_error is None and all(index >= 0 for index in positions)
                ok = ok and positions == sorted(positions)
                detail = "fixture text matched" if ok else "expected fixture text/error missing"
            print(f"{'PASS' if ok else 'FAIL'} {label} ({detail})", flush=True)
            failures += int(not ok)
    finally:
        await provider.terminate()
        await runtime.stop_validation()
    return int(failures != 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, type=Path, help="test JSON with an isolated runner or login"
    )
    parser.add_argument(
        "--allow-model-requests", action="store_true", help="enable real Cursor requests"
    )
    args = parser.parse_args()
    if not args.allow_model_requests:
        print(
            "SKIP real images (pass --allow-model-requests after completing a separate test login)"
        )
        return 0
    try:
        config = json.loads(args.config.read_text())
        if not isinstance(config, dict):
            print("FAIL configuration (expected a JSON object with an explicit model_id)")
            return 1
        with TemporaryDirectory(prefix="astrbot-cursor-image-acceptance-") as directory:
            os.environ["ASTRBOT_ROOT"] = directory
            return asyncio.run(check(config))
    except CursorError as exc:
        print(f"FAIL live acceptance (code={exc.code})")
    except (ImportError, OSError, ValueError) as exc:
        print(f"FAIL test environment ({type(exc).__name__})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
