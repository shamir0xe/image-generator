# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

A CLI that turns a movie file into a photo-mosaic poster: it samples frames from the movie, computes each frame's mean RGB, then solves a min-cost matching so that every "box" (cell) of a blurred target image is filled with the movie frame whose average color is closest. The result is a large poster of the target image rendered entirely out of movie frames.

## Commands

Install (uses a virtualenv and a git submodule):

```bash
git submodule update --init --recursive   # pulls libs/PythonLibrary (pylib_0xe)
pip install -r requirements.txt
```

Run the CLI (entry point is `main.py`, built with Typer):

```bash
# Full pipeline: sample frames (first run only) + build poster
python main.py gen --movie-name "Paris Texas" --movie-format "mkv" --generate-frames
# Subsequent runs reuse cached frames — drop --generate-frames
python main.py gen --movie-name "Paris Texas" --movie-format "mkv"

# Other subcommands
python main.py sampling   --movie-name "Paris Texas"   # only extract frames
python main.py clear-cache --movie-name "Paris Texas"  # delete frames + rgb csv cache
python main.py modify     --movie-name "paris texas"   # post-process highlights pass
python main.py crop       --image-path path/to/img.jpg # crop to A3 ratio + downscale
```

There is no test suite, linter config, or build step — this is a single-entry Typer CLI.

## Configuration (`.env`)

Copy `sample.env` → `.env`. All tuning happens through env vars, read once in `read_envs()` (`src/cmds/run_bot.py`). Key knobs:

- `box` — number of frame cells stacked vertically per column (controls mosaic resolution).
- `frame_count_per_box` — size of the frame pool sampled from the movie; also the suffix of the rgb cache file (`assets/{name}-{count}.csv`).
- `final_box_height` — pixel height of each frame cell in the output.
- `alpha`/`beta` — blend weights: `output = frame*alpha + target_mean_color*beta`. Higher beta makes the mosaic fade toward the target image.
- `ratio`, `crop_box_x/y`, `upsample` — frame aspect ratio / crop / target upscale factor. Note `gen` overrides `ratio` from the actual target image dimensions.

## Pipeline architecture (`gen` command in `src/cmds/run_bot.py`)

The `gen` command (`main()`) orchestrates everything; understanding it explains the whole project:

1. **Blur the target** — `ImageModifier.get_blured()` divides the upscaled target image into a grid of boxes and returns both a blocky preview image and a 2D `mean_rgb[y][x]` grid. Box dimensions are derived from `find_ratio()`, which approximates the image's aspect ratio as a small integer fraction so boxes tile cleanly.
2. **Get frames** — `get_movie_frames()` either samples new frames (`MovieSampler` + `Movie`, OpenCV grabs one frame per second of runtime, shuffles, keeps `frame_count_per_box`) or loads cached `.jpg`s from `movie_frames_path/{standard_name}/`.
3. **Compute frame colors** — `calculate_movie_rgbs()` builds/reads `assets/{name}-{count}.csv` mapping each frame filename → mean RGB.
4. **Match** — `MinCostMatcher` (`src/min_cost_matcher.py`) builds a min-cost-flow graph (via `MinCostFlow` wrapping OR-Tools, `src/utils/min_cost_flow.py`): source → each target box (cap 1) → each frame (cost = Euclidean RGB distance) → sink (cap `max_same_picture`). `best_match()` binary-searches the per-frame reuse capacity to find the cheapest assignment.
5. **Render** — `construct_box()` (`src/image_modifier.py`) pastes the chosen frame into each cell, blended with that cell's target color via alpha/beta, on a NumPy canvas.
6. **Save + crop** — `save_out_image()` auto-increments `outputs/{name}-o{i}.jpg`, then `crop_image()` produces an A3-ratio version and `final_job()` downscales it in-process with Pillow.

## Naming conventions that matter

- `movie_standard_name()` lowercases and replaces punctuation/space runs with single hyphens. This standard name is the key for **everything**: frame folder (`movie_frames_path/{standard_name}/`), rgb cache (`assets/{standard_name}-{count}.csv`), and output names. Cache invalidation in `clear_cache()` matches files by this name.
- If you add/remove frames in a movie's frame folder by hand, you must delete the matching `assets/{name}.csv` so the rgb cache is rebuilt (see README).

## Notes

- `src/movie.py` extracts frames by seeking to whole-second timestamps with `cv2.CAP_PROP_POS_MSEC`; `get_duration()` derives the count from FPS × frame count.
- `pylib_0xe` (used for `File.get_all_files`) comes from the `libs/PythonLibrary` git submodule — without it, imports fail.
- The repo's tracked `assets/` already contains many sample `.png` targets and `.csv` rgb caches; movies themselves live under `assets/movies/` (gitignored).
