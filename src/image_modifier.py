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
    def get_blured(image_path, properties):
        image = image_path if isinstance(image_path, Image.Image) else Image.open(image_path)
        x_len, y_len = image.size

        # upscale
        factor = properties["upsample"]
        image = image.resize((x_len * factor, y_len * factor))
        x_len, y_len = image.size
        res_image = Image.new("RGB", image.size)

        logger.info(f"img size {x_len}, {y_len}")

        # Cells take the frame's aspect ratio so whole frames tile the target
        # without cropping. `box` is the number of cells along one axis: by
        # height (default) the cell count follows the height, by width it
        # follows the width -- pick whichever keeps the mosaic dense enough.
        if properties.get("box_axis", "height") == "width":
            box = {"x": max(1, round(x_len / properties["box"]))}
            box["y"] = max(1, round(box["x"] / properties["ratio"]))
        else:
            box = {"y": max(1, round(y_len / properties["box"]))}
            box["x"] = max(1, round(box["y"] * properties["ratio"]))

        logger.info(f"x, y: {box['x']}, {box['y']}")

        count = (math.ceil(x_len / box["x"]), math.ceil(y_len / box["y"]))
        mean_rgb = [[(0, 0, 0) for _ in range(count[0])] for _ in range(count[1])]
        data = list(image.getdata())  # pyright: ignore
        res_data = res_image.load()
        if res_data is None:
            raise Exception("result data is None!")
        terminal_process = TerminalProcess(count[0] * count[1])
        for i in range(count[0]):
            for j in range(count[1]):
                terminal_process.hit()
                rgb = (0, 0, 0)
                total = 0
                ii = 0
                while ii < box["x"] and i * box["x"] + ii < x_len:
                    jj = 0
                    while jj < box["y"] and j * box["y"] + jj < y_len:
                        rgb = tuple(
                            map(
                                operator.add,
                                rgb,
                                data[(j * box["y"] + jj) * x_len + i * box["x"] + ii],
                            )
                        )
                        jj += 1
                        total += 1
                    ii += 1
                rgb = tuple(map(operator.mul, rgb, (1 / total, 1 / total, 1 / total)))
                rgb = tuple(map(math.floor, rgb))
                mean_rgb[j][i] = rgb
                ii = 0
                while ii < box["x"] and i * box["x"] + ii < x_len:
                    jj = 0
                    while jj < box["y"] and j * box["y"] + jj < y_len:
                        res_data[i * box["x"] + ii, j * box["y"] + jj] = rgb
                        jj += 1
                    ii += 1
        return res_image, mean_rgb

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


def construct_box(image, images, mean_rgb, properties):
    alpha, beta = (
        properties["color_mixtures"]["alpha"],
        properties["color_mixtures"]["beta"],
    )
    count = (properties["dimensions"]["x"], properties["dimensions"]["y"])

    # Each tile takes the frame's aspect ratio, so a whole frame drops in with a
    # plain resize -- no cropping, no stretching (only the optional crop_box
    # applied at sampling time changes a frame). The canvas is sized to the tile
    # grid exactly so every cell lands inside it.
    box = {"x": properties["final_box_height"]}
    box["y"] = max(1, round(box["x"] / properties["ratio"]))
    x_len, y_len = box["x"] * count[0], box["y"] * count[1]
    image = image.resize((x_len, y_len))

    canvas_np = np.array(image).astype(np.float32)

    terminal_process = TerminalProcess(count[0] * count[1])
    for i in range(count[0]):
        for j in range(count[1]):
            index = count[0] * j + i
            terminal_process.hit()

            temp_image = Image.open(images[index]).resize((box["x"], box["y"]))
            img_np = np.array(temp_image).astype(np.float32)
            img_np = (img_np * alpha) + (np.array(mean_rgb[j][i]) * beta)

            y_start = j * box["y"]
            x_start = i * box["x"]
            canvas_np[
                y_start : y_start + box["y"],
                x_start : x_start + box["x"],
            ] = img_np

            temp_image.close()

    canvas_np = canvas_np.clip(0, 255)
    canvas_np = canvas_np.astype(np.uint8)
    return Image.fromarray(canvas_np)
