import os
import shutil
import random
import re
from typing import Annotated, Any
from dotenv import load_dotenv
import logging
import csv
from PIL import Image

from pathlib import Path
from pylib_0xe.file.file import File
import typer
from src.image_modifier import ImageModifier, construct_box
from src.movie_sampler import MovieSampler
from src.movie import Movie
from src.min_cost_matcher import MinCostMatcher
from src.utils.terminal_process import TerminalProcess

load_dotenv()
logger = logging.getLogger(__name__)


def read_envs() -> dict[str, Any]:
    return {
        "movie_path": os.getenv("movie_path"),
        "image_path": os.getenv("image_path"),
        "movie_frames_path": os.getenv("movie_frames_path"),
        "box": int(os.getenv("box", 0)),
        "frame_count_per_box": int(os.getenv("frame_count_per_box", 0)),
        "final_box_height": int(os.getenv("final_box_height", 0)),
        "ratio": float(os.getenv("ratio", 0)),
        "alpha": float(os.getenv("alpha", 0)),
        "beta": float(os.getenv("beta", 0)),
        "crop_box": (int(os.getenv("crop_box_x", 0)), int(os.getenv("crop_box_y", 0))),
        "upsample": int(os.getenv("upsample", 2))
    }


app = typer.Typer(help="Image generator")


def movie_standard_name(movie_name: str) -> str:
    out = movie_name.lower()
    pattern = r"[\(\)\[\]\{\}\<\>\|\+\*\?\\\_\s]"
    out = re.sub(pattern, " ", out)
    out = re.sub(r"\s+", " ", out)
    out = out.strip()
    return "-".join(out.split())


def our_filename(filename: str, sname: str):
    pattern = rf".*{sname}-.*"
    return re.match(pattern, filename) is not None


@app.command(name="clear-cache")
def clear_cache(
        movie_name: Annotated[
            str, typer.Option(help="Name of the movie you want to remove")
        ],
):
    envs = read_envs()
    sname = movie_standard_name(movie_name)

    frames_path = os.path.join(envs["movie_frames_path"], sname)
    if os.path.isdir(frames_path):
        shutil.rmtree(frames_path)
        logger.info("Removed frames folder")

    filenames = File.get_all_files("assets", ".csv")
    for filename in filenames:
        if our_filename(filename, sname):
            os.remove(os.path.join("assets", filename))
            logger.info(f"Removed {filename}")


@app.command(name="modify")
def post_modification(
        movie_name: Annotated[
            str, typer.Option(help="The movie name available in the path")
        ],
        use_gpu: Annotated[bool, typer.Option(help="Force to use GPU")] = False,
):
    st_name = "-".join(movie_name.lower().split())
    origin_img = ImageModifier.open(f"assets/{st_name}.png")
    gen_img = ImageModifier.open(f"assets/{st_name}-o1.png")

    if use_gpu:
        modified_img = ImageModifier.add_highlights_mac_gpu(gen_img, origin_img)
    else:
        modified_img = ImageModifier.add_highlights(gen_img, origin_img)
    modified_img.save(f"assets/{st_name}-o2.png")


def get_movie_frames(standard_name: str, envs: dict, generate_frames: bool):
    if generate_frames:
        logger.info("Generating frames...")
        # First clear already cache
        clear_cache(standard_name)

        # Create folders
        if not os.path.isdir(envs["movie_frames_path"]):
            os.makedirs(envs["movie_frames_path"])

        # Generates frame
        movie = Movie(
            standard_name,
            envs["movie_path"],
            {"cache_path": envs["movie_frames_path"]},
        )
        movie_frames = MovieSampler.get_random_frames(
            movie,
            {
                "total_frames": envs["frame_count_per_box"],
                "crop_box": envs["crop_box"],
            },
        )
        movie_frames.sort()
        movie.close()
    else:
        logger.info("Already have the frames, retrieving...")
        # Already have the frames
        movie_frames = File.get_all_files(envs["movie_frames_path"], ext=".jpg")
        movie_frames = [
            os.path.join(envs["movie_frames_path"], movie_frames[i])
            for i in range(len(movie_frames))
        ]
        random.shuffle(movie_frames)
        movie_frames = movie_frames[: envs["frame_count_per_box"]]

    return movie_frames


def calculate_movie_rgbs(
        standard_name: str, movie_frames: list[str], envs: dict
) -> list[tuple[int, int, int]]:
    frame_rgbs_file = f"assets/{standard_name}-{envs['frame_count_per_box']}.csv"

    def resolve():
        movie_rgbs = []
        rgbs = {}

        with open(frame_rgbs_file, "r") as f:
            reader = csv.reader(f)
            for row in reader:
                filename, r, g, b = row
                rgbs[filename] = tuple(map(int, (r, g, b)))

        for frame_path in movie_frames:
            pattern = r".*\/(.+).jpg$"
            match = re.search(pattern, frame_path)
            if match:
                filename = match.groups()[0]
                movie_rgbs.append(rgbs[filename])
            else:
                raise Exception("invalid time!")

        return movie_rgbs

    if not os.path.isfile(frame_rgbs_file):
        logger.info("We dont have any cache for movie rgbs!")

        all_frames = File.get_all_files(envs["movie_frames_path"], ext=".jpg")
        terminal_process = TerminalProcess(len(all_frames))

        with open(frame_rgbs_file, "w") as f:
            writer = csv.writer(f)
            for frame in all_frames:
                terminal_process.hit()
                rgb = ImageModifier.get_mean_rgb(
                    ImageModifier.open(os.path.join(envs["movie_frames_path"], frame))
                )
                pattern = r"(.+).jpg$"
                match = re.search(pattern, frame)
                if not match:
                    raise Exception("invalid frame")
                filename = match.groups()[0]
                writer.writerow([filename, *rgb])

    return resolve()


def save_out_image(final_image: Image.Image, name: str, annotate: bool = True):
    """Returns the final path as the result"""

    output_dir = Path("outputs")
    if not Path.is_dir(output_dir):
        Path.mkdir(output_dir)

    i = 1
    while True:
        if annotate:
            path = output_dir / f"{name}-o{i}.jpg"
            if Path.is_file(path):
                i += 1
                continue
        else:
            path = output_dir / f"{name}.jpg"
        break
    final_image.save(path)

    return path.stem


def crop_image(img: Image.Image) -> Image.Image:
    # A3
    target_width: int = 4962
    target_height: int = 3507
    target_ratio: float = target_width / target_height

    img_width, img_height = img.size

    if (1 - img_width / img_height) * (1 - target_ratio) < 1e-9:
        target_width, target_height = target_height, target_width
        target_ratio = target_width / target_height

    if img_width / img_height > target_ratio:
        delta_h = 0
        delta_w = (img_width - img_height * target_ratio) // 2
    else:
        delta_h = (img_height - img_width / target_ratio) // 2
        delta_w = 0

    cropped_img = img.crop(
        (delta_w, delta_h, img_width - delta_w, img_height - delta_h)
    )

    return cropped_img


def final_job(img: Image.Image, name: str, scale: float = 0.4) -> None:
    logger.info("Resizing the final image...")
    w, h = img.size
    resized = img.resize(
        (max(1, round(w * scale)), max(1, round(h * scale))),
        Image.LANCZOS,
    )
    resized.save(Path("outputs") / f"{name}-final.jpg")


@app.command(name="sampling")
def sample_frames(movie_name: Annotated[str, typer.Option(help="The movie name available in the path")]):
    envs = read_envs()
    standard_name = movie_standard_name(movie_name)
    movie_suffix = ""
    for path in Path(envs["movie_path"]).iterdir():
        if path.is_file() and movie_standard_name(path.stem) == standard_name:
            movie_name = path.stem
            movie_suffix = path.suffix
    if movie_suffix == "":
        raise Exception("Movie not found!")
    logger.info(f"movie: {movie_name}{movie_suffix}")
    envs["movie_path"] += f"{movie_name}{movie_suffix}"
    envs["movie_frames_path"] = Path(envs["movie_frames_path"]) / standard_name
    get_movie_frames(standard_name, envs, True)


@app.command(name="crop")
def crop(image_path: Annotated[str, typer.Option(help="The image path")]):
    img = Image.open(image_path)
    img = crop_image(img)
    image_name = Path(image_path).stem + "-a3"
    img.save(Path("outputs") / f"{image_name}.jpg")
    final_job(img, image_name)


@app.command(name="gen")
def main(
        movie_name: Annotated[
            str, typer.Option(help="The movie name available in the path")
        ],
        movie_format: Annotated[str, typer.Option(help="Format of the video")],
        generate_frames: Annotated[
            bool, typer.Option(help="Force to generate frames from the movie")
        ] = False,
        target_img: Annotated[str | None, typer.Option(help="Target image")] = None,
        box: Annotated[
            int | None,
            typer.Option(help="Number of tiles along the chosen axis (see --by-width)"),
        ] = None,
        upsample: Annotated[int | None, typer.Option(help="Upsample factor")] = None,
        capacity: Annotated[int | None, typer.Option(help="Capacity")] = None,
        colored: Annotated[
            bool,
            typer.Option(
                help="Tint frames with the target colors (alpha/beta from .env) "
                "instead of using the raw movie frames (alpha=1, beta=0)"
            ),
        ] = False,
        by_width: Annotated[
            bool,
            typer.Option(
                "--by-width/--by-height",
                help="Count `box` tiles along the image width instead of its "
                "height (denser mosaic when frames are wide)",
            ),
        ] = False,
):
    logging.info(f"Processing {movie_name}.{movie_format}")

    envs = read_envs()
    envs["movie_path"] += f"{movie_name}.{movie_format}"
    envs["image_path"] = target_img if target_img else envs["image_path"]
    standard_name = movie_standard_name(movie_name)
    envs["movie_frames_path"] = os.path.join(envs["movie_frames_path"], standard_name)
    envs["upsample"] = upsample if upsample is not None else envs["upsample"]
    envs["box"] = box if box is not None else envs["box"]
    # Colored mode keeps the alpha/beta loaded from .env; raw mode shows the
    # untinted movie frames.
    if not colored:
        envs["alpha"], envs["beta"] = 1.0, 0.0

    logger.info("Retrieving frames")
    movie_frames = get_movie_frames(standard_name, envs, generate_frames)

    # The frame aspect ratio drives the mosaic tiles so whole frames fit without
    # cropping. Use the crop_box if set in .env, otherwise read it off a frame.
    crop_x, crop_y = envs["crop_box"]
    if crop_x > 0 and crop_y > 0:
        envs["ratio"] = crop_x / crop_y
    else:
        with Image.open(movie_frames[0]) as sample_frame:
            envs["ratio"] = sample_frame.size[0] / sample_frame.size[1]
    logger.info(f"frame ratio = {envs['ratio']:.4f}")

    logger.info("Building blured image...")
    image, mean_rgbs = ImageModifier.get_blured(
        envs["image_path"],
        {
            "box": envs["box"],
            "upsample": envs["upsample"],
            "ratio": envs["ratio"],
            "box_axis": "width" if by_width else "height",
        },
    )
    image.show(movie_name)
    image.save(f"assets/{standard_name}.png")

    dimensions = (len(mean_rgbs[0]), len(mean_rgbs))
    logger.info(f"dimensions = {dimensions}")

    logger.info("Calculating rgbs for frames...")
    movie_rgbs = calculate_movie_rgbs(standard_name, movie_frames, envs)

    logger.info("Running MaxMatcher algorithm...")
    max_matcher = MinCostMatcher(mean_rgbs, movie_rgbs)
    if capacity is not None:
        order = max_matcher.solve(capacity)[0]
    else:
        order = max_matcher.best_match()

    logger.info("Constructing final image...")

    final_image = construct_box(
        image,
        [movie_frames[order[i]] for i in range(len(order))],
        mean_rgbs,
        {
            "dimensions": {"x": dimensions[0], "y": dimensions[1]},
            "ratio": envs["ratio"],
            "color_mixtures": {"alpha": envs["alpha"], "beta": envs["beta"]},
            "final_box_height": envs["final_box_height"],
        },
    )
    final_name = save_out_image(final_image, standard_name)
    cropped_image = crop_image(final_image)
    cropped_name = save_out_image(cropped_image, f"{final_name}-a3", annotate=False)
    final_job(cropped_image, cropped_name)
