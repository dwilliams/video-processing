#!/usr/bin/env python3

### IMPORTS ###
import argparse
import json
import logging
import pathlib
import shutil
import sys

import ffmpeg


### GLOBALS ###
ffmpeg_cmd = ['ffmpeg', '-threads', '6']

### FUNCTIONS ###

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

    concat_tmp_path = pathlib.Path(args.session_dir, "{}_concat.mp4".format(args.temp_file_prefix))
    output_path = pathlib.Path(args.session_dir, "output.mp4")
    output_ytdesc_path = pathlib.Path(args.session_dir, "youtube_description.txt")

    ## Copy the logos into the work folder
    # NOTE: All things in this script run at 1080p29.97
    # NOTE: Used this to compress the overlay while maintaining the alpha layer
    #       https://stackoverflow.com/questions/50323082/compress-video-while-preserving-alpha-channel
    logo_video_name = "big_to_small_logo_session_transparent_comp.avi"
    logo_video_src_path = pathlib.Path(args.session_dir, "../DBCM Session Logo/", logo_video_name)
    logo_video_path = pathlib.Path(args.session_dir, logo_video_name)
    # NOTE: These will happily overwrite existing files.
    shutil.copyfile(logo_video_src_path, logo_video_path)

    ## Generate the filter set for FFMPEG
    # Generate the main video sequence
    tnscd_videos = []
    for mvid in video_spec["session_videos"]:
        tnscd_videos.append(pathlib.Path(args.session_dir, mvid))
    logging.debug("tnscd_videos: %s", tnscd_videos)

    # Transcode each of the inputs in case (usually) the container format is MP4
    # Per: https://trac.ffmpeg.org/wiki/Concatenate#Usingintermediatefiles
    # NOTE: This didn't exactly work, so have to re-encode the hard way before
    #       concatenation.
    main_videos = []
    for i in range(len(tnscd_videos)):
        mvid_path = tnscd_videos[i]
        tmp_output_path = pathlib.Path(args.session_dir, "{}_input{}.ts".format(args.temp_file_prefix, i))
        tmp_input = ffmpeg.input(mvid_path)
        tmp_output = ffmpeg.output(
            tmp_input,
            str(tmp_output_path),
            c = "copy"
        ).overwrite_output()
        output_args = ffmpeg.compile(tmp_output, cmd = ffmpeg_cmd)
        logging.debug("tmp_input: %s", tmp_input)
        logging.debug("tmp_output: %s", tmp_output)
        logging.debug("output_args: %s", output_args)
        ffmpeg.run(tmp_output, cmd = ffmpeg_cmd)
        main_videos.append(tmp_output_path)

    # NOTE: Have to use this version of concat (demuxer as part of the input)
    #       It's the only one that seems to work with the MP4s from the Garmin
    concat_str = "concat:"
    for i in range(len(main_videos)):
        concat_str = "{}{}{}".format(
            concat_str,
            "" if i == 0 else "|",
            main_videos[i]
        )
    concat_video = ffmpeg.input(concat_str)
    concat_output_video = ffmpeg.output(
        concat_video,
        str(concat_tmp_path),
        c="copy"
    ).overwrite_output()
    logging.debug("concat_output_video: %s", concat_output_video)
    concat_output_args = ffmpeg.compile(concat_output_video, cmd = ffmpeg_cmd)
    logging.debug("output_args: %s", concat_output_args)
    ffmpeg.run(concat_output_video, cmd = ffmpeg_cmd)

    # Setup the overlay video sequence
    # FIXME: This currently uses a pre-rendered video.  Should figure out how to
    #        translate and scale the PNG image with FFMPEG filters directly.
    # FIXME: Probe the length of the concat_input_video so we can setup the fade out
    # NOTE: Have to use an mp4 as the output from the concat as the `.ts`
    #       intermediate file doesn't have frame counts in its meta data
    concat_input_info = ffmpeg.probe(concat_tmp_path)
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

    concat_input_video = ffmpeg.input(concat_tmp_path)
    overlay_input_video = ffmpeg.input(logo_video_path)
    overlay_video = ffmpeg.overlay(
        concat_input_video,
        overlay_input_video,
        x = 0,
        y = 0
    )
    fade_length = 15
    fadein_video = ffmpeg.filter_(
        overlay_video,
        "fade",
        t="in", # type
        s="0", # start_frame
        n=str(fade_length) # nb_frames
    )
    fadeout_video = ffmpeg.filter_(
        fadein_video,
        "fade",
        t="out", # type
        s=str(num_frames - fade_length), # start_frame
        n=str(fade_length)  # nb_frames
    )
    fadein_audio = ffmpeg.filter_(
        concat_input_video.audio,
        "afade",
        t = "in",
        st = 0.0,
        d = 1.5
    )
    fadeout_audio = ffmpeg.filter_(
        fadein_audio,
        "afade",
        t = "out",
        st = audio_duration - 1.5,
        d = 1.5
    )
    logging.debug("fadeout_video: %s", fadeout_video)
    logging.debug("fadeout_audio: %s", fadeout_audio)

    output_video = ffmpeg.output( fadeout_audio,
        fadeout_video,
        str(output_path)
    ).overwrite_output()
    logging.debug("output_video: %s", output_video)
    output_args = ffmpeg.compile(output_video, cmd = ffmpeg_cmd)
    logging.debug("output_args: %s", output_args)
    ffmpeg.run(output_video, cmd = ffmpeg_cmd)

    ## Output the YouTube description file

    ## (MAYBE FUTURE) Upload output to YouTube.

    ## Cleanup
    # FIXME: remove the file at concat_tmp_path

if __name__ == "__main__":
    main()
