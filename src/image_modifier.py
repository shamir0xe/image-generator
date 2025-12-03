from threading import currentThread
from PIL import Image
import operator
import math
import logging
import numpy as np

logger = logging.getLogger(__name__)

from src.utils.terminal_process import TerminalProcess

Image.MAX_IMAGE_PIXELS = 1176120000 + 10


class ImageModifier:
    @staticmethod
    def rgb_to_monochromatic(rgb):
        return (0.2125 * rgb[0]) + (0.7154 * rgb[1]) + (0.0721 * rgb[2])

    @staticmethod
    def open(image_path):
        return Image.open(image_path)

    @staticmethod
    def add_highlights(img1: Image.Image, img2: Image.Image):
        # img1 * alpha + img2 * beta
        alpha, beta = 0.8, 0.6

        mul = lambda tup, t: tuple([k * t for k in tup])

        img2 = img2.resize(mul(img2.size, 15))
        img1 = img1.resize(img2.size)

        img1_np = np.array(img1).astype(np.float32)
        img1.close()
        img2_np = np.array(img2).astype(np.float32)
        img2.close()

        blend_image = img1_np * alpha + img2_np * beta
        blend_image = np.clip(blend_image, 0, 255)

        return Image.fromarray(blend_image.astype(np.uint8))

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
            for j in range(1, i + 1):
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
        image = Image.open(image_path)
        x_len, y_len = image.size

        xx, yy = ImageModifier.find_ratio(x_len, y_len)

        # upscale
        factor = properties["upsample"]
        image = image.resize((x_len * factor, y_len * factor))
        x_len, y_len = image.size
        res_image = Image.new("RGB", image.size)

        logger.info(f"img size {x_len}, {y_len}")
        logger.info("atomic-xy: (%d, %d)", xx, yy)

        estimated_length = round(y_len / properties["box"])

        box = {
            "y": int(math.ceil(1.0 * estimated_length / yy) * yy + 1e-12),
        }
        box["x"] = box["y"] * xx // yy

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
    final_image_height = properties["final_box_height"] * properties["dimensions"]["x"]
    image = image.resize(
        (
            final_image_height,
            int(final_image_height * image.size[1] / image.size[0]),
        )
    )
    alpha, beta = (
        properties["color_mixtures"]["alpha"],
        properties["color_mixtures"]["beta"],
    )
    x_len, y_len = image.size

    canvas_np = np.array(image).astype(np.float32)

    box = {
        "y": math.ceil(y_len / properties["dimensions"]["y"]),
    }
    box["x"] = math.ceil(box["y"] * properties["ratio"])
    count = (properties["dimensions"]["x"], properties["dimensions"]["y"])
    terminal_process = TerminalProcess(count[0] * count[1])
    for i in range(count[0]):
        for j in range(count[1]):
            index = count[0] * j + i
            terminal_process.hit()

            temp_image = Image.open(images[index])
            temp_image = temp_image.resize((math.ceil(box["x"]), math.ceil(box["y"])))
            img_np = np.array(temp_image).astype(np.float32)
            img_np = (img_np * alpha) + (np.array(mean_rgb[j][i]) * beta)

            y_start = math.floor(j * box["y"])
            x_start = math.floor(i * box["x"])

            y_end = min(y_start + box["y"], y_len)
            x_end = min(x_start + box["x"], x_len)

            canvas_np[
                y_start:y_end,
                x_start:x_end,
            ] = img_np[: y_end - y_start, : x_end - x_start]

    canvas_np = canvas_np.clip(0, 255)
    canvas_np = canvas_np.astype(np.uint8)
    return Image.fromarray(canvas_np)
