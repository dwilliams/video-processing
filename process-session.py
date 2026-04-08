#!/usr/bin/env python3

### IMPORTS ###
import argparse
import json
import logging
import pathlib
import shutil
import sys

import ffmpeg
import jinja2

from PIL import Image, ImageDraw, ImageFont

### GLOBALS ###
CMD_FFMPEG = ['ffmpeg', '-threads', '6']

### FUNCTIONS ###
def create_title_overlay_image(session_dir, temp_file_prefix, width, height, spec):
    output_path = pathlib.Path(session_dir, "{}_title.png".format(temp_file_prefix))
    # Create a transparent image object
    img = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    # Overlay the title text
    title_text = "{}\n{}\n{} - {}\nSession {} - {}".format(
        spec["race"]["long"],
        spec["race"]["track"],
        spec["event"]["date"],
        spec["event"]["name"],
        spec["event"]["session_number"],
        spec["driver"]
    )
    font = ImageFont.truetype(font = "DejaVuSans-Bold.ttf", size = 96)
    draw = ImageDraw.Draw(img)
    draw.text(
        xy = (width / 2, height / 2),
        text = title_text,
        fill = (255, 255, 255, 255),
        font = font,
        anchor = "mm",
        spacing = 30,
        align = "center",
        stroke_width = 1,
        stroke_fill = (0, 0, 0, 255)
    )
    img.save(output_path)
    return output_path

def create_intermediate_inputs(session_dir, temp_file_prefix, input_videos_paths):
    # Transcode each of the inputs in case (usually) the container format is MP4
    # Per: https://trac.ffmpeg.org/wiki/Concatenate#Usingintermediatefiles
    # NOTE: This didn't exactly work, so have to re-encode the hard way before
    #       concatenation.
    main_videos = []
    for i in range(len(input_videos_paths)):
        mvid_path = input_videos_paths[i]
        tmp_output_path = pathlib.Path(session_dir, "{}_input{}.ts".format(temp_file_prefix, i))
        tmp_input = ffmpeg.input(mvid_path)
        tmp_output = ffmpeg.output(tmp_input, str(tmp_output_path), c="copy").overwrite_output()
        output_args = ffmpeg.compile(tmp_output, cmd = CMD_FFMPEG)
        logging.debug("tmp_input: %s", tmp_input)
        logging.debug("tmp_output: %s", tmp_output)
        logging.debug("output_args: %s", output_args)
        ffmpeg.run(tmp_output, cmd = CMD_FFMPEG)
        main_videos.append(tmp_output_path)
    return main_videos

def create_concat_video(session_dir, temp_file_prefix, input_videos_paths):
    # NOTE: Have to use this version of concat (demuxer as part of the input)
    #       It's the only one that seems to work with the MP4s from the Garmin
    tmp_path = pathlib.Path(session_dir, "{}_concat.mp4".format(temp_file_prefix))
    concat_str = "concat:"
    for i in range(len(input_videos_paths)):
        concat_str = "{}{}{}".format(concat_str, "" if i == 0 else "|", input_videos_paths[i])
    concat_video = ffmpeg.input(concat_str)
    output_video = ffmpeg.output(concat_video, str(tmp_path), c="copy").overwrite_output()
    logging.debug("concat output_video: %s", output_video)
    output_args = ffmpeg.compile(output_video, cmd = CMD_FFMPEG)
    logging.debug("concat output_args: %s", output_args)
    ffmpeg.run(output_video, cmd = CMD_FFMPEG)
    return tmp_path

def create_final_video(concat_video_path, overlay_video_path, title_img_path, output_path):
    # Setup the overlay video sequence
    # FIXME: This currently uses a pre-rendered video.  Should figure out how to
    #        translate and scale the PNG image with FFMPEG filters directly.
    # FIXME: Probe the length of the concat_input_video so we can setup the fade out
    # NOTE: Have to use an mp4 as the output from the concat as the `.ts`
    #       intermediate file doesn't have frame counts in its meta data
    concat_input_info = ffmpeg.probe(concat_video_path)
    logging.debug("concat_input_info: %s", concat_input_info)
    num_frames = int(0)
    audio_duration = float(0.0)
    for stream in concat_input_info["streams"]:
        if stream["codec_type"] == "video":
            num_frames = int(stream["nb_frames"])
        if stream["codec_type"] == "audio":
            audio_duration = float(stream["duration"])
    logging.debug("num_frames: %s", num_frames)
    logging.debug("audio_duration: %s", audio_duration)

    if num_frames == 0:
        logging.error("No frames in the concat tmp video!")
        sys.exit(1)

    concat_input_video = ffmpeg.input(concat_video_path)
    title_input_image = ffmpeg.input(title_img_path)
    overlay_input_video = ffmpeg.input(overlay_video_path)
    # Draw session title on the screen
    # NOTE: FFMPEG needed to be recompiled to use the drawtext filter, so
    #       created an image with Pillow and overlayed that instead.
    title_overlay_video = ffmpeg.overlay(concat_input_video, title_input_image, x = 0, y = 0, enable = "between(t,3,13)")
    # Overlay the logo at the start
    overlay_video = ffmpeg.overlay(title_overlay_video, overlay_input_video, x = 0, y = 0)
    # Fade in and fade out
    logo_fade_length = 15
    fadein_video = ffmpeg.filter_(
        overlay_video,
        "fade",
        t="in",  # type
        s="0",  # start_frame
        n=str(logo_fade_length)  # nb_frames
    )
    fadeout_video = ffmpeg.filter_(
        fadein_video,
        "fade",
        t="out",  # type
        s=str(num_frames - logo_fade_length),  # start_frame
        n=str(logo_fade_length)  # nb_frames
    )
    # Fade audio in and out
    fadein_audio = ffmpeg.filter_(
        concat_input_video.audio,
        "afade",
        t="in",
        st=0.0,
        d=1.5
    )
    fadeout_audio = ffmpeg.filter_(
        fadein_audio,
        "afade",
        t="out",
        st=audio_duration - 1.5,
        d=1.5
    )
    logging.debug("fadeout_video: %s", fadeout_video)
    logging.debug("fadeout_audio: %s", fadeout_audio)
    # Render down the output video
    output_video = ffmpeg.output(fadeout_audio, fadeout_video, str(output_path)).overwrite_output()
    logging.debug("output_video: %s", output_video)
    output_args = ffmpeg.compile(output_video, cmd=CMD_FFMPEG)
    logging.debug("output_args: %s", output_args)
    ffmpeg.run(output_video, cmd = CMD_FFMPEG)

def create_description_txt(session_dir, spec, output_path):
    description_template_path = pathlib.Path(session_dir, "../DBCM Session Logo/")
    env = jinja2.Environment(loader = jinja2.FileSystemLoader(description_template_path))
    template = env.get_template("youtube_description.jinja2")
    result = template.render(spec = spec)
    with open(output_path, 'w') as of:
        of.write(result)

### CLASSES ###

### MAIN ###
def main():
    parser_description = """
    Process a session video.
    """

    parser = argparse.ArgumentParser(description = parser_description, formatter_class = argparse.RawTextHelpFormatter)
    parser.add_argument("-v", "--verbose", action = "store_true")
    parser.add_argument("--temp_file_prefix", default = "tmp", help = "The prefix for the temporary files.")
    parser.add_argument("session_dir", help = "The directory for the session video.")
    args = parser.parse_args()

    ## Set up logging
    logging.basicConfig(
        format="%(asctime)s:%(levelname)s:%(name)s:%(funcName)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO
    )
    logging.debug("args: %s", args)

    ## Load the spec JSON file
    input_json_path = pathlib.Path(args.session_dir, "spec.json")
    with open(input_json_path, 'r') as ij:
        video_spec = json.load(ij)
    logging.debug("video_spec: %s", video_spec)

    ## Setup some paths
    output_path = pathlib.Path(args.session_dir, "output.mp4")
    output_ytdesc_path = pathlib.Path(args.session_dir, "youtube_description.txt")

    ## Copy the logos into the work folder
    # NOTE: Used this to compress the overlay while maintaining the alpha layer
    #       https://stackoverflow.com/questions/50323082/compress-video-while-preserving-alpha-channel
    logo_video_name = "big_to_small_logo_session_transparent_comp.avi"
    logo_video_src_path = pathlib.Path(args.session_dir, "../DBCM Session Logo/", logo_video_name)
    logo_video_path = pathlib.Path(args.session_dir, logo_video_name)
    # NOTE: These will happily overwrite existing files.
    shutil.copyfile(logo_video_src_path, logo_video_path)

    ## Generate temp assets (e.g. title text image)
    # FIXME: Should probably set the width and height for the project and scale the inputs as needed...
    title_img_path = create_title_overlay_image(args.session_dir, args.temp_file_prefix, 1920, 1080, video_spec)

    ## Generate the filter set for FFMPEG
    # Generate the main video sequence
    tnscd_videos = []
    for mvid in video_spec["session_videos"]:
        tnscd_videos.append(pathlib.Path(args.session_dir, mvid))
    logging.debug("tnscd_videos: %s", tnscd_videos)

    # Transcode each of the inputs in case (usually) the container format is MP4
    main_videos_paths = create_intermediate_inputs(args.session_dir, args.temp_file_prefix, tnscd_videos)

    concat_tmp_path = create_concat_video(args.session_dir, args.temp_file_prefix, main_videos_paths)

    # Set up the overlay video sequence
    create_final_video(concat_tmp_path, logo_video_path, title_img_path, output_path)

    ## Output the YouTube description file
    create_description_txt(args.session_dir, video_spec, output_ytdesc_path)

    ## (MAYBE FUTURE) Upload output to YouTube.

    ## Cleanup
    for path_item in main_videos_paths:
        path_item.unlink()
    concat_tmp_path.unlink()
    title_img_path.unlink()

if __name__ == "__main__":
    main()
