from threading import currentThread
from PIL import Image
import operator
import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

from src.utils.terminal_process import TerminalProcess

Image.MAX_IMAGE_PIXELS = 2557302128 + 10


class ImageModifier:
    @staticmethod
    def rgb_to_monochromatic(rgb):
        return (0.2125 * rgb[0]) + (0.7154 * rgb[1]) + (0.0721 * rgb[2])

    @staticmethod
    def open(image_path):
        return Image.open(image_path)

    @staticmethod
    def find_ratio(x: int, y: int) -> tuple[int, int]:
        ratio = -1
        res = (-1, -1)
        target = x * 1.0 / y
        eps = 1e-9
        logger.info("%d/%d = %f", x, y, target)

        def distance(current: float) -> float:
            return math.fabs(current - target)

        for i in range(1, 20):
            for j in range(1, 20):
                current_ratio = i / j
                if (
                    ratio == -1
                    or distance(ratio) - eps > distance(current_ratio)
                    or math.fabs(distance(ratio) - distance(current_ratio)) < eps
                    and i + j <= res[0] + res[1]
                ):
                    ratio = current_ratio
                    res = (i, j)
        return res

    @staticmethod
    def tile_target(image_path, properties):
        """Sample the target's mean color under each tile placement.

        Walks `properties["placements"]` (from a tiling strategy) in order and,
        for each, averages the target pixels over the tile's *rotated* footprint
        -- the same rectangle (size and angle) that `construct_box` later fills
        -- so the sampled color matches what gets placed there. Returns a blocky
        preview (with the tiles drawn rotated, mirroring the final render) and a
        *flat* list of per-placement mean RGBs in placement order, ready to feed
        straight into the matcher.
        """
        image = image_path if isinstance(image_path, Image.Image) else Image.open(image_path)
        x_len, y_len = image.size

        # upscale
        factor = properties["upsample"]
        image = image.resize((x_len * factor, y_len * factor))
        x_len, y_len = image.size

        logger.info(f"img size {x_len}, {y_len}")

        rows = max(1, properties["box"])
        ratio = properties["ratio"]
        placements = properties["placements"]

        # Uniform tile size in pixels: height drives it, width follows the ratio.
        th = y_len / rows
        tw = th * ratio
        box_w, box_h = max(1, round(tw)), max(1, round(th))
        # Bleed for rotated preview blocks so they overlap rather than leave
        # seams -- mirrors the same trick in construct_box.
        bleed = max(1, round(box_h * 0.06))
        # A square crop big enough to hold the tile at any rotation.
        diag = int(math.ceil(math.hypot(box_w, box_h)))
        c = diag / 2.0
        cx0, cy0 = round(c - box_w / 2), round(c - box_h / 2)

        # Reusable 255 image to mark which cropped pixels are inside the target
        # (so rotated/edge tiles don't average in the black padding).
        valid_full = Image.new("L", (x_len, y_len), 255)

        preview = Image.new("RGB", (x_len, y_len))
        mean_rgb: list[tuple[int, int, int]] = []
        terminal_process = TerminalProcess(len(placements))
        for p in placements:
            terminal_process.hit()
            # u runs 0..aspect, v runs 0..1, both scaled by the height in px.
            cx, cy = p.u * y_len, p.v * y_len
            left, top = round(cx - c), round(cy - c)
            rotated = p.angle % 360 != 0

            # Crop a square around the center, rotate the tile upright, then
            # take the central box_w x box_h -- the tile's true footprint.
            patch = image.crop((left, top, left + diag, top + diag))
            if rotated:
                patch = patch.rotate(-p.angle, resample=Image.BILINEAR)
            tile = patch.crop((cx0, cy0, cx0 + box_w, cy0 + box_h))
            arr = np.asarray(tile, dtype=np.float64).reshape(-1, 3)

            if left < 0 or top < 0 or left + diag > x_len or top + diag > y_len:
                vmask = valid_full.crop((left, top, left + diag, top + diag))
                if rotated:
                    vmask = vmask.rotate(-p.angle, resample=Image.NEAREST)
                inside = np.asarray(vmask.crop((cx0, cy0, cx0 + box_w, cy0 + box_h))).reshape(-1) > 127
                rgb = tuple(int(v) for v in arr[inside].mean(axis=0)) if inside.any() else (0, 0, 0)
            else:
                rgb = tuple(int(v) for v in arr.mean(axis=0))
            mean_rgb.append(rgb)  # pyright: ignore

            # Draw the cell into the preview as a (possibly rotated) solid block
            # so the preview mirrors the final mosaic's layout. Tiles get a bleed
            # so abutting blocks overlap instead of leaving seams -- a wide one
            # for rotated tiles, 1px for upright ones (covers rounding seams).
            b = bleed if rotated else 1
            bw, bh = box_w + 2 * b, box_h + 2 * b
            block = Image.new("RGB", (bw, bh), rgb)
            if rotated:
                bmask = Image.new("L", (bw, bh), 255)
                block = block.rotate(p.angle, expand=True, resample=Image.BILINEAR)
                bmask = bmask.rotate(p.angle, expand=True, resample=Image.BILINEAR)
            else:
                bmask = None
            preview.paste(
                block,
                (round(cx - block.size[0] / 2), round(cy - block.size[1] / 2)),
                bmask,
            )

        return preview, mean_rgb

    @staticmethod
    def get_mean_rgb(image):
        data = list(image.resize((100, 100)).getdata())
        rgb = (0, 0, 0)
        for i in range(len(data)):
            rgb = tuple(map(operator.add, rgb, data[i]))
        total = len(data)
        rgb = tuple(map(operator.mul, rgb, (1 / total, 1 / total, 1 / total)))
        rgb = tuple(map(math.floor, rgb))
        return rgb


def construct_box(image, images, mean_rgb, placements, properties):
    """Render the mosaic by dropping each matched frame onto its placement.

    Walks `placements` in lockstep with `images`/`mean_rgb` (cell `i` of each),
    tints the frame toward the cell's target color via alpha/beta, rotates it by
    the placement's angle, and pastes it at the placement center. Rotated tiles
    keep a mask so their corners stay transparent and the preview shows through
    the seams (e.g. between circular rings).
    """
    alpha, beta = (
        properties["color_mixtures"]["alpha"],
        properties["color_mixtures"]["beta"],
    )
    ratio = properties["ratio"]
    rows = max(1, properties["rows"])

    # Tile pixel size. `final_box_height` sets the tile width (kept for
    # backwards compatibility -- it has always driven the larger dimension);
    # the height follows from the frame aspect ratio.
    box_w = properties["final_box_height"]
    box_h = max(1, round(box_w / ratio))
    # Rotated tiles meet along feathered, rounded edges, so abutting bricks can
    # leave a hair-thin seam of background showing. Grow rotated tiles by a small
    # bleed so neighbours overlap and cover the seam. Upright tiles (crossboard,
    # brick) abut exactly and stay untouched.
    bleed = max(1, round(box_h * 0.06))

    # Canvas: `rows` tiles tall, same aspect as the (preview) target image.
    aspect = image.size[0] / image.size[1]
    y_len = rows * box_h
    x_len = max(1, round(aspect * y_len))
    canvas = image.resize((x_len, y_len)).convert("RGB")

    terminal_process = TerminalProcess(len(placements))
    for index, p in enumerate(placements):
        terminal_process.hit()

        # Upright tiles still get a 1px bleed: column spacing is a float while
        # the block width is rounded, so without overlap the rounding can leave
        # hair-thin seams (vertical/horizontal black lines) between tiles.
        rotated = p.angle % 360 != 0
        b = bleed if rotated else 1
        tw, th = box_w + 2 * b, box_h + 2 * b
        temp_image = Image.open(images[index]).resize((tw, th)).convert("RGB")
        img_np = np.asarray(temp_image, dtype=np.float32)
        img_np = (img_np * alpha) + (np.asarray(mean_rgb[index], dtype=np.float32) * beta)
        tile = Image.fromarray(np.clip(img_np, 0, 255).astype(np.uint8))
        temp_image.close()

        if rotated:
            mask = Image.new("L", (tw, th), 255)
            tile = tile.rotate(p.angle, expand=True, resample=Image.BILINEAR)
            mask = mask.rotate(p.angle, expand=True, resample=Image.BILINEAR)
        else:
            mask = None

        cx, cy = p.u * y_len, p.v * y_len
        x = round(cx - tile.size[0] / 2)
        y = round(cy - tile.size[1] / 2)
        canvas.paste(tile, (x, y), mask)

    return canvas
