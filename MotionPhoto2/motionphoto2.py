#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import codecs
import exiftool
import filecmp
import itertools
import json
import logging
import os
import shutil
import sys

from pathlib import Path

# The portable Windows build's embeddable Python ships a ._pth file, which
# puts the interpreter in isolated mode - it no longer auto-adds the running
# script's own directory to sys.path, so sibling imports below (Muxer, utils)
# fail with ModuleNotFoundError unless we add it back explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    # Gooey pulls in wxPython, which has no prebuilt wheel on some platforms
    # (e.g. this project's homelab server) and is only needed for the GUI mode
    # triggered by running with zero CLI args. Fall back to a plain
    # ArgumentParser that ignores GUI-only kwargs for headless/CLI use.
    from gooey import GooeyParser
except ImportError:
    def _strip_gooey_kwargs(kwargs):
        # metavar is repurposed by Gooey as a friendly field label; plain
        # argparse (Python 3.14+) rejects it for store_true/store_false actions.
        kwargs.pop("widget", None)
        kwargs.pop("gooey_options", None)
        if kwargs.get("action") in ("store_true", "store_false"):
            kwargs.pop("metavar", None)
        return kwargs

    class GooeyParser(argparse.ArgumentParser):
        def add_argument(self, *args, **kwargs):
            return super().add_argument(*args, **_strip_gooey_kwargs(kwargs))

        def add_argument_group(self, title=None, description=None, gooey_options=None, **kwargs):
            group = super().add_argument_group(title, description, **kwargs)
            orig_add_argument = group.add_argument
            group.add_argument = lambda *a, **kw: orig_add_argument(*a, **_strip_gooey_kwargs(kw))
            return group

from Muxer import Muxer, MuxerInputError, OutputConflictError
from extract import EXTRACT_DIRNAME, ExtractError, exiftool_available, run_extract
from video_removal import normalize_mode, skip_folders, describe_destination
from utils import enrich_fname, is_motion_photo, extract_video_from_image, input_output_binary_compare, load_defaults, save_defaults, \
    output_with_extension, output_conflict, claim_output

logging.basicConfig(
    handlers=[logging.StreamHandler(sys.stdout)],
    level=logging.DEBUG,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] - %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S"
)

class Unbuffered(object):
   def __init__(self, stream):
       self.stream = stream
   def write(self, data):
       self.stream.write(data)
       self.stream.flush()
   def writelines(self, datas):
       self.stream.writelines(datas)
       self.stream.flush()
   def __getattr__(self, attr):
       return getattr(self.stream, attr)

def emit_result(name, status: str, reason: str = "", output: str = "", kind: str = "", video: str = ""):
    """One machine-readable line per file for the web UI's results table.
    kind (optional) is the finer classification the Preview filters on: ready,
    motion_leftover, motion_only, no_mov, no_photo, skipped, error. video is the
    relative path of the paired MOV (for motion_leftover)."""
    d = {"file": str(name), "status": status, "reason": reason, "output": str(output)}
    if kind:
        d["kind"] = kind
    if video:
        d["video"] = str(video)
    print("@@RESULT " + json.dumps(d), flush=True)


def get_metadata_safe(et, paths):
    """(metadata list, errors) for paths. One batch call first; if exiftool fails on it
    (an empty or corrupt file makes it exit 1), retry file by file so only the bad
    files are lost. errors maps the index of an unreadable file to exiftool's message;
    its metadata entry is {}. Unreadable files are never matched or touched."""
    paths = list(paths)
    if not paths:
        return [], {}
    try:
        metas = et.get_metadata(paths)
        if len(metas) == len(paths):
            return list(metas), {}
    except Exception:
        pass
    metas, errors = [], {}
    for i, p in enumerate(paths):
        try:
            m = et.get_metadata(p)
            metas.append(m[0] if m else {})
        except Exception as e:
            msg = (getattr(e, "stderr", "") or "").strip() or str(e) or e.__class__.__name__
            errors[i] = msg.replace("\n", " ")[:200]
            metas.append({})
    return metas, errors


def check_motion_photo(path, et):
    """(is_motion_photo, error). An unreadable file is reported, not fatal."""
    try:
        return is_motion_photo(str(path), et), ""
    except Exception as e:
        return False, str(e) or e.__class__.__name__


def planned_output(args, image: str, output_directory, output_file=None) -> str:
    """Where Muxer would write the result (mirrors Muxer.__init__)."""
    if output_directory is not None:
        return os.path.join(str(output_directory), os.path.basename(image))
    if args.overwrite:
        return str(image)
    return output_file or enrich_fname(str(image), "LIVE")


def report_dry_run(args, name, image: str, video: str, output_directory, removal_kwargs, output_file=None,
                   ext=None, claimed=None):
    """--dry-run: say what a real run would do for this pair, touch nothing.
    ext is the image's real extension (File:FileTypeExtension): the real run writes
    IMG.jpeg as IMG.jpg, so the preview must check that name. A path another file
    already owns (or another photo of this run writes) is reported as a conflict."""
    base = planned_output(args, image, output_directory, output_file)
    out = output_with_extension(base, ext)
    why = output_conflict(out, image, video, claimed, rerun_ok=not args.overwrite and out == base)
    if why:
        print(f"[DRY-RUN] Would skip {name}: {why}")
        emit_result(name, "skipped", why[0].upper() + why[1:], out, kind="conflict")
        return
    claim_output(claimed, out)
    print(f"[DRY-RUN] Would convert {name} + {video} -> {out}")
    dest = describe_destination(removal_kwargs['video_removal_mode'], removal_kwargs.get('video_removal_root'),
                                removal_kwargs.get('video_removal_folder'))
    notes = []
    if args.overwrite:
        notes.append(f"original photo replaced, old copy -> {dest}")
    if args.delete_video:
        notes.append(f"video removed -> {dest}")
    emit_result(name, "planned", "dry run" + ("; " + "; ".join(notes) if notes else ""), out, kind="ready")
    if args.overwrite:
        print(f"[DRY-RUN] Would replace original photo {image}; old copy goes to {dest}")
    if args.delete_video:
        print(f"[DRY-RUN] Would remove video {video}; it goes to {dest}")


def run_extract_mode(args) -> int:
    """--extract: split Motion Photos into still + video (see extract.py)."""
    if (args.input_directory is None) == (args.input_image is None):
        print("[ERROR] --extract needs exactly one of --input-directory or --input-image")
        return 1
    if args.input_video is not None or args.copy_unmuxed or args.incremental_mode or args.overwrite:
        print("[ERROR] --extract cannot be combined with input-video, copy-unmuxed, incremental or overwrite (existing files are never overwritten)")
        return 1
    source = args.input_directory if args.input_directory is not None else args.input_image
    if not os.path.exists(source):
        print(f"[ERROR] Input does not exist: {source}")
        return 1
    et = None
    if exiftool_available():
        try:
            et = exiftool.ExifToolHelper(encoding="utf-8")
            et.run()
        except Exception as e:
            print(f"[WARNING] exiftool could not be started: {e}")
            et = None
    try:
        run_extract(args.input_directory, args.input_image, args.output_directory, args.recursive,
                    args.extract_mov, args.extract_still, args.dry_run, emit_result, et,
                    force=args.extract_force)
    except ExtractError as e:
        print(f"[ERROR] {e}")
        return 1
    finally:
        if et is not None:
            et.terminate()
    return 0


def main():
    
    defaults = load_defaults()
    
    parser = GooeyParser(
        prog="MotionPhoto2",
        description="Mux HEIC and JPG Live Photos into Google/Samsung Motion Photos",
    )
    
    dir_group = parser.add_argument_group(
        "Process a Directory",
        gooey_options={'columns':4}
    )

    dir_group.add_argument(
        "-id", 
        "--input-directory",
        metavar="Input Directory",
        help="Mux all the photos and videos in a directory",
        widget='DirChooser',
        gooey_options={'full_width':True, 'initial_value':defaults['input_directory']}
    )
    
    dir_group.add_argument(
        "-r",
        "--recursive",
        metavar="Recursive",
        action="store_true",
        help="Recursively process subdirectories",
        gooey_options={'initial_value':defaults['recursive']}
    )

    dir_group.add_argument(
        "-em", 
        "--exif-match",
        metavar="Match by EXIF",
        action="store_true",
        help="Match files by Live Photo metadata",
        gooey_options={'initial_value':defaults['exif_match']}
    )

    dir_group.add_argument(
        "-im",
        "--incremental-mode",
        metavar="Incremental Mode",
        action="store_true",
        help="Skip photos already muxed in output",
        gooey_options={'initial_value':defaults['incremental_mode']}
    )
    
    dir_group.add_argument(
        "-cu",
        "--copy-unmuxed",
        metavar="Copy Unmuxed",
        action="store_true",
        help="Copy other files",
        gooey_options={'initial_value':defaults['copy_unmuxed']}
    )

    dir_group.add_argument(
        "-od",
        "--output-directory",
        metavar="Output Directory",
        help="Directory where to save the resulting Motion Photos",
        widget='DirChooser',
        gooey_options={'full_width':True, 'initial_value':defaults['output_directory']}
    )

    settings_group = parser.add_argument_group(
        "Settings",
        gooey_options={'columns':4}
    )

    settings_group.add_argument(
        "-dv",
        "--delete-video",
        metavar="Delete Video",
        action="store_true",
        help="Delete video after muxing",
        gooey_options={'initial_value':defaults['delete_video']}
    )
    
    settings_group.add_argument(
        "--video-removal-mode",
        choices=["trash", "move", "delete"],
        default=None,
        help="How --delete-video / --overwrite dispose of originals: trash, move (to a folder) or delete (default: move in Docker, otherwise trash)",
        gooey_options={'visible':False}
    )

    settings_group.add_argument(
        "--video-removal-folder",
        default=None,
        help="Folder for --video-removal-mode move, used for removed videos and replaced photos (default: <input directory>/_originals)",
        gooey_options={'visible':False}
    )

    settings_group.add_argument(
        "--only-file",
        default=None,
        help="JSON file with a list of image paths (relative to the input directory) to convert; all other photos are ignored",
        gooey_options={'visible':False}
    )

    settings_group.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be converted and removed without writing, moving or deleting anything",
        gooey_options={'visible':False}
    )

    settings_group.add_argument(
        "-o",
        "--overwrite",
        metavar="Overwrite",
        action="store_true",
        help="Overwrite the original image",
        gooey_options={'initial_value':defaults['overwrite']}
    )

    settings_group.add_argument(
        "-kt",
        "--keep-temp",
        metavar="Keep Temp",
        action="store_true",
        help="Keep muxing temp files",
        gooey_options={'initial_value':defaults['keep_temp']}
    )
    
    settings_group.add_argument(
        "-v", 
        "--verbose",
        metavar="Verbose",
        action="store_true", 
        help="Verbose output",
        gooey_options={'initial_value':defaults['verbose']}
    )

    extract_group = parser.add_argument_group(
        "Extract Mode"
    )

    extract_group.add_argument(
        "--extract",
        action="store_true",
        help="Split Motion Photos into a still image and a separate video (uses the input directory or --input-image; --output-directory is optional, default: an _extracted folder next to each photo)",
        gooey_options={'visible':False}
    )

    extract_group.add_argument(
        "--extract-mov",
        action="store_true",
        help="With --extract: save the video as .mov (container renamed, bytes unchanged) when that is safe, otherwise .mp4",
        gooey_options={'visible':False}
    )

    extract_group.add_argument(
        "--extract-still",
        action="store_true",
        help="With --extract: also write <name>_still.jpg, the image without the video and motion photo XMP flags (the original is never modified)",
        gooey_options={'visible':False}
    )

    extract_group.add_argument(
        "--extract-force",
        action="store_true",
        help="With --extract: extract again even if the video was already extracted (the new file gets a _1 suffix)",
        gooey_options={'visible':False}
    )

    file_group = parser.add_argument_group(
        "Process a Single File"
    )

    file_group.add_argument(
        "-ii",
        "--input-image",
        metavar="Input Image",
        help="Input file image (.heic, .jpg)",
        widget='FileChooser',
        gooey_options={
            'wildcard':
                "HEIC file|*.heic|"
                "HEIF file|*.heif|"
                "JPG file|*.jpg|"
                "All files (*.*)|*.*",
            'message': "Select image file",
            'initial_value':defaults['input_image']
        }
    )
        
    file_group.add_argument(
        "-iv",
        "--input-video",
        metavar="Input Video",
        help="Input file video (.mov, .mp4)", 
        widget='FileChooser',
        gooey_options={
            'wildcard':
                "MOV file|*.mov|"
                "MP4 file|*.mp4|"
                "All files (*.*)|*.*",
            'message': "Select video file",
            'initial_value':defaults['input_video']
        }
    )

    file_group.add_argument(
        "-of", 
        "--output-file",
        metavar="Output File",
        help="Output Live Photo filename",
        widget='FileSaver',
        gooey_options={
            'wildcard':
                "HEIC file|*.heic|"
                "HEIF file|*.heif|"
                "JPG file|*.jpg|"
                "All files (*.*)|*.*",
            'message': "Target image file",
            'initial_value':defaults['output_file']
        }
    )

    file_group.add_argument(
        "-nx",
        "--no-xmp",
        metavar="No XMP",
        action="store_true",
        help="No XMP processing (just glue image and video using Samsung tags)",
        gooey_options={'visible':False, 'initial_value':defaults['no_xmp']}
    )

    args = parser.parse_args()

    if sys.stdout.encoding != 'UTF-8':
        sys.stdout = Unbuffered(codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict'))
    if sys.stderr.encoding != 'UTF-8':
        sys.stderr = Unbuffered(codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict'))    

    if args.extract:
        sys.exit(run_extract_mode(args))

    if args.input_directory is not None and (
        args.input_image is not None or args.input_video is not None
    ):
        print("[ERROR] Input directory cannot be use with input-image or input-video")
        sys.exit(1)

    if args.input_directory is None:
        if args.input_image is None or args.input_video is None:
            print("[ERROR] Please provide both input image/video or input directory")
            sys.exit(1)

    if args.output_directory is None and args.copy_unmuxed is True:
        print("[ERROR] Copy unmuxed cannot be used without output directory")
        sys.exit(1)

    if args.output_directory is None and args.incremental_mode is True:
        print("[ERROR] Incremental mode cannot be used without output directory")
        sys.exit(1)

    if args.output_directory is not None and args.overwrite is True:
        print("[ERROR] Output directory cannot be use overwrite option")
        sys.exit(1)

    if args.output_file is not None and args.overwrite is True:
        print("[ERROR] Output file cannot be use overwrite option")
        sys.exit(1)

    if args.copy_unmuxed is True and args.overwrite is True:
        print("[ERROR] Copy Unmuxed cannot be used with overwrite option")
        sys.exit(1)

    if args.copy_unmuxed is True and args.delete_video is True:
        print("[ERROR] Copy Unmuxed cannot be used with delete-video option")
        sys.exit(1)

    if args.output_directory is not None:
        output_directory = f"{Path(args.output_directory).resolve()}"
        if os.path.exists(output_directory) is False:
            os.mkdir(output_directory)
        elif os.path.isfile(output_directory):
            print("[ERROR] Output directory cannot be a file")
            sys.exit(1)
        elif args.copy_unmuxed:
            input_directory = f"{Path(args.input_directory).resolve()}"
            if os.path.samefile(input_directory, output_directory): # Input directory and output directory cannot be same if copying unmuxed files
                print("[ERROR] Output directory cannot be the same as input directory")
                sys.exit(1)

    defaults['input_directory']=args.input_directory
    defaults['recursive']=args.recursive
    defaults['exif_match']=args.exif_match
    defaults['incremental_mode']=args.incremental_mode
    defaults['copy_unmuxed']=args.copy_unmuxed
    defaults['output_directory']=args.output_directory
    defaults['delete_video']=args.delete_video
    defaults['overwrite']=args.overwrite
    defaults['keep_temp']=args.keep_temp
    defaults['verbose']=args.verbose
    defaults['input_image']=args.input_image
    defaults['input_video']=args.input_video
    defaults['output_file']=args.output_file
    defaults['no_xmp']=args.no_xmp
    if not args.dry_run:
        save_defaults(defaults)
            
    # Where --delete-video sends videos; in directory mode they must stay inside the input directory
    removal_root = f"{Path(args.input_directory).resolve()}" if args.input_directory is not None else None
    removal_kwargs = {
        'video_removal_mode': normalize_mode(args.video_removal_mode),
        'video_removal_folder': args.video_removal_folder,
        'video_removal_root': removal_root,
    }
    skip_folders_ = [str(f) for f in skip_folders(removal_root, args.video_removal_folder)] if removal_root else []
    only_images = None
    if args.only_file:
        if args.copy_unmuxed:
            print("[ERROR] --only-file cannot be combined with copy-unmuxed")
            sys.exit(1)
        with open(args.only_file, encoding="utf-8") as fh:
            only_images = {str(Path(x)) for x in json.load(fh)}

    logger = logging.getLogger("ExifTool")
    logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    
    with exiftool.ExifToolHelper(
        encoding="utf-8",
        logger=logger if args.verbose is True else None
    ) as et:

        if args.input_directory is not None:
            print(f"Converting files in {args.input_directory}")
            input_directory = Path(args.input_directory).resolve()
            
            # Get files based on recursive flag
            files = []
            if args.recursive:
                # Recursive mode: walk through all subdirectories
                for root, dirnames, filenames in os.walk(input_directory):
                    # never rescan videos that were moved aside by --delete-video
                    dirnames[:] = [d for d in dirnames if d != EXTRACT_DIRNAME
                                   and str((Path(root) / d).resolve()) not in skip_folders_]
                    for filename in filenames:
                        full_path = Path(root) / filename
                        rel_path = full_path.relative_to(input_directory)
                        files.append(str(rel_path))
            else:
                # Non-recursive mode: only process files in the specified directory
                for filename in os.listdir(input_directory):
                    if os.path.isfile(input_directory / filename):
                        files.append(filename)
            
            # Filter videos and images while preserving relative paths
            videos = [
                f for f in files
                if Path(f).suffix.lower() in [".mp4", ".mov"]
            ]
            
            images = [
                f for f in files
                if Path(f).suffix.lower() in [".heic", ".heif", ".avif", ".jpg", ".jpeg"]
            ]
            
            if only_images is not None:
                images = [f for f in images if str(Path(f)) in only_images]

            unsupported = [
                f for f in files
                if Path(f).suffix.lower() not in [".mp4", ".mov", ".heic", ".heif", ".avif", ".jpg", ".jpeg"]
            ]

            unmatched_images = []
            failed_pairs = []
            all_videos = list(videos)
            claimed_videos = set()     # videos that belong to some photo (so are not "MOV without a photo")
            blocked_videos = set()     # videos a photo was skipped for (output conflict): keep them
            claimed_outputs = set()    # output paths already taken in this run (two photos must not write one file)

            if not args.exif_match: # match by file name
                # Several photos can share one video by name (IMG.HEIC + IMG.jpeg + IMG.jpg
                # next to IMG.MOV). Count them first so --delete-video only removes the video
                # after the last of them, and never pop it from the list on first use.
                video_names = set(videos)
                def name_match(image):
                    base = str(Path(image).with_suffix(''))
                    return next((base + e for e in (".mp4", ".mov", ".MP4", ".MOV") if base + e in video_names), None)
                photos_left = {}
                for image in images:
                    v = name_match(image)
                    if v:
                        photos_left[v] = photos_left.get(v, 0) + 1

                i = 0
                for image in images:
                    i += 1
                    image_path = Path(image)
                    fname = str(image_path.with_suffix(''))
                    last_photo_for_video = True
                    v0 = name_match(image)
                    if v0:
                        photos_left[v0] -= 1
                        last_photo_for_video = photos_left[v0] <= 0

                    # Check if source image is already a motion photo
                    is_mp, err = check_motion_photo(input_directory / image, et)
                    if err:
                        print(f"[WARNING] Cannot read {image}: {err}")
                        emit_result(image, "error", "unreadable: " + err[:200], kind="error")
                        continue
                    if is_mp:
                        print(f"Input {image} is already a motion photo, skipping muxing...")
                        left = next((fname + e for e in (".mp4", ".mov", ".MP4", ".MOV") if fname + e in videos), "")
                        if left:
                            claimed_videos.add(left)
                            emit_result(image, "skipped", "already a motion photo; its MOV is still there",
                                        kind="motion_leftover", video=left)
                        else:
                            emit_result(image, "skipped", "already a motion photo", kind="motion_only")
                        if args.copy_unmuxed:
                            unmatched_images.append(image)
                        continue
                    
                    for ext in [".mp4", ".mov", ".MP4", ".MOV"]:
                        video_fname = fname + ext
                        if video_fname in video_names:
                            print(f"=========================[{i}/{len(images)}]")
                            video = video_fname
                            if video in videos:
                                videos.remove(video)     # matched: not an "unmuxed" video to copy
                            claimed_videos.add(video)

                            # Construct full paths for input files
                            input_image = input_directory / image
                            input_video = input_directory / video
                            
                            # Handle output directory structure
                            output_subdirectory = args.output_directory                   
                            if output_subdirectory is not None:
                                # Preserve directory structure in output
                                output_subdirectory = Path(output_subdirectory) / image_path.parent
                                output_subdirectory = output_subdirectory.resolve()
                                if not output_subdirectory.exists() and not args.dry_run:
                                    output_subdirectory.mkdir(parents=True, exist_ok=True)

                            if args.incremental_mode:
                                output_image_path = output_subdirectory / image_path.name
                                if os.path.exists(output_image_path) and input_output_binary_compare(input_video,output_image_path):
                                    print(f"Destination {image} is already a motion photo, skipping...")
                                    emit_result(image, "skipped", "already converted in the output folder", kind="skipped")
                                    break
                            
                            try:
                                if args.dry_run:
                                    try:
                                        real_ext = et.get_metadata(str(input_image))[0].get('File:FileTypeExtension')
                                    except Exception:
                                        real_ext = None
                                    report_dry_run(args, image, str(input_image), str(input_video), output_subdirectory,
                                                   removal_kwargs, ext=real_ext, claimed=claimed_outputs)
                                else:
                                    muxer = Muxer(
                                        image_fpath=str(input_image),
                                        video_fpath=str(input_video),
                                        exiftool=et,
                                        output_directory=str(output_subdirectory) if output_subdirectory else None,
                                        delete_video=args.delete_video and last_photo_for_video and video not in blocked_videos,
                                        **removal_kwargs,
                                        delete_temp=not args.keep_temp,
                                        overwrite=args.overwrite,
                                        no_xmp=args.no_xmp,
                                        verbose=args.verbose,
                                        claimed_outputs=claimed_outputs,
                                    )
                                    muxer.mux()
                                    emit_result(image, "converted", output=muxer.output_fpath)
                            except OutputConflictError as e:
                                blocked_videos.add(video)
                                print(f"[WARNING] Skipping {image}: {e}")
                                emit_result(image, "skipped", str(e), kind="conflict")
                            except MuxerInputError as e:
                                print(f"[WARNING] Skipping {image}: {e}")
                                emit_result(image, "error", str(e), kind="error")
                                failed_pairs.append((image, str(e)))
                            break
                    else:
                        print(f"No matching video found for {image}")
                        emit_result(image, "skipped", "no matching video", kind="no_mov")
                        if args.copy_unmuxed:
                            unmatched_images.append(image)

            else: # match by exif
                image_paths = [input_directory / img for img in images]
                video_paths = [input_directory / vid for vid in videos]
                print("Running in EXIF matching mode.")
                print("Getting metadata for images, please wait...")
                image_metadatas, image_errors = get_metadata_safe(et, [str(p) for p in image_paths])
                print("Getting metadata for videos, please wait...")
                video_metadatas, video_errors = get_metadata_safe(et, [str(p) for p in video_paths])
                for vi, msg in video_errors.items():
                    print(f"[WARNING] Cannot read {videos[vi]}: {msg}")
                    emit_result(videos[vi], "skipped", "unreadable: " + msg, kind="skipped")
                    claimed_videos.add(videos[vi])
                
                # Map content identifiers to video relative paths
                content_id_to_video = {}
                for meta, vid in zip(video_metadatas, videos):
                    content_id = meta.get('QuickTime:ContentIdentifier')
                    if content_id:
                        content_id_to_video.setdefault(content_id.strip(), []).append(vid)
                        if args.verbose:
                            print(f"[DEBUG] Mapped video {vid} to ContentIdentifier: {content_id}")
                
                # Several photos can share one video (e.g. HEIC + JPEG exports of the same
                # Live Photo). Count the photos per video so --delete-video only removes it
                # after the last of them, otherwise the rest fail with "Video file doesn't exist".
                photos_left = {}
                for img_meta in image_metadatas:
                    cid = str(img_meta.get('MakerNotes:ContentIdentifier') or "").strip()
                    if cid in content_id_to_video:
                        v = content_id_to_video[cid][0]
                        photos_left[v] = photos_left.get(v, 0) + 1

                # Match images to videos
                i = 0
                for img_idx, (img, img_meta) in enumerate(zip(images, image_metadatas)):
                    i += 1
                    if img_idx in image_errors:
                        print(f"[WARNING] Cannot read {img}: {image_errors[img_idx]}")
                        emit_result(img, "skipped", "unreadable: " + image_errors[img_idx], kind="skipped")
                        continue
                    cid0 = str(img_meta.get('MakerNotes:ContentIdentifier') or "").strip()
                    last_photo_for_video = True
                    if cid0 in content_id_to_video:
                        v0 = content_id_to_video[cid0][0]
                        photos_left[v0] -= 1
                        last_photo_for_video = photos_left[v0] <= 0

                    # Check if source image is already a motion photo
                    is_mp, err = check_motion_photo(input_directory / img, et)
                    if err:
                        print(f"[WARNING] Cannot read {img}: {err}")
                        emit_result(img, "error", "unreadable: " + err[:200], kind="error")
                        continue
                    if is_mp:
                        print(f"Input {img} is already a motion photo, skipping muxing...")
                        cid = str(img_meta.get('MakerNotes:ContentIdentifier') or "").strip()
                        left = (content_id_to_video.get(cid) or [""])[0] if cid else ""
                        if left:
                            claimed_videos.update(content_id_to_video[cid])
                            emit_result(img, "skipped", "already a motion photo; its MOV is still there",
                                        kind="motion_leftover", video=left)
                        else:
                            emit_result(img, "skipped", "already a motion photo", kind="motion_only")
                        if args.copy_unmuxed:
                            unmatched_images.append(img)
                        continue

                    content_id = img_meta.get('MakerNotes:ContentIdentifier')
                    if args.verbose and content_id:
                        print(f"[DEBUG] Image {img} has ContentIdentifier: {content_id}")
                    if content_id and content_id.strip() in content_id_to_video:
                        print(f"=========================[{i}/{len(images)}]")
                        video = content_id_to_video[content_id.strip()][0]
                        claimed_videos.update(content_id_to_video[content_id.strip()])

                        if args.copy_unmuxed:
                            for vid in content_id_to_video[content_id.strip()]:
                                if vid in videos:
                                    videos.remove(vid)
                        
                        # Construct full paths for input files
                        input_image = input_directory / img
                        input_video = input_directory / video
                        
                        # Handle output directory structure
                        output_subdirectory = args.output_directory
                        if output_subdirectory is not None:
                            image_path = Path(img)
                            output_subdirectory = Path(output_subdirectory) / image_path.parent
                            output_subdirectory = output_subdirectory.resolve()
                            if not output_subdirectory.exists() and not args.dry_run:
                                output_subdirectory.mkdir(parents=True, exist_ok=True)

                        if args.incremental_mode:
                            # Try matching using Content id
                            output_image_path = output_subdirectory / image_path.name
                            if os.path.exists(output_image_path):
                                output_metadata = get_metadata_safe(et, [str(output_image_path)])[0][0]
                                output_image_content_id = output_metadata.get('MakerNotes:ContentIdentifier')
                                output_video_data_from_image = extract_video_from_image(output_image_path, et)

                                # Check if content IDs match
                                if all((output_video_data_from_image,
                                        output_image_content_id,
                                        output_video_data_from_image.find(output_image_content_id.strip().encode()) != -1,
                                        output_video_data_from_image.find(content_id.strip().encode()) != -1)):
                                        if args.verbose:
                                            print(f"[DEBUG] ContentIdentifier '{content_id.strip()}' of the source {input_image} and {output_image_path} destination matches")
                                        print(f"Destination {img} as it is already a motion photo, skipping...")
                                        emit_result(img, "skipped", "already converted in the output folder", kind="skipped")
                                        continue
                                else:
                                    # Do binary comparison to check input and output
                                    if input_output_binary_compare(input_video,output_image_path):
                                        print(f"Destination {img} is already a motion photo, skipping...")
                                        emit_result(img, "skipped", "already converted in the output folder", kind="skipped")
                                        continue
                        
                        try:
                            if args.dry_run:
                                report_dry_run(args, img, str(input_image), str(input_video), output_subdirectory, removal_kwargs,
                                               ext=img_meta.get('File:FileTypeExtension'), claimed=claimed_outputs)
                            else:
                                muxer = Muxer(
                                    image_fpath=str(input_image),
                                    video_fpath=str(input_video),
                                    exiftool=et,
                                    output_directory=str(output_subdirectory) if output_subdirectory else None,
                                    delete_video=args.delete_video and last_photo_for_video and video not in blocked_videos,
                                    **removal_kwargs,
                                    delete_temp=not args.keep_temp,
                                    overwrite=args.overwrite,
                                    no_xmp=args.no_xmp,
                                    verbose=args.verbose,
                                    claimed_outputs=claimed_outputs,
                                )
                                muxer.mux()
                                emit_result(img, "converted", output=muxer.output_fpath)
                        except OutputConflictError as e:
                            blocked_videos.add(video)
                            print(f"[WARNING] Skipping {img}: {e}")
                            emit_result(img, "skipped", str(e), kind="conflict")
                        except MuxerInputError as e:
                            print(f"[WARNING] Skipping {img}: {e}")
                            emit_result(img, "error", str(e), kind="error")
                            failed_pairs.append((img, str(e)))
                    else:
                        print(f"No matching video found for {img}")
                        emit_result(img, "skipped", "no matching video", kind="no_mov")
                        if args.copy_unmuxed:
                            unmatched_images.append(img)

            if args.dry_run and only_images is None:      # MOVs no photo claims (informational)
                for vid in all_videos:
                    if vid not in claimed_videos:
                        emit_result(vid, "skipped", "no matching photo", kind="no_photo")

            # Copy unmuxed images and videos while preserving the directory structure
            if args.copy_unmuxed:
                print("=" * 25)
                print("Copying unmuxed files...")
                for file in itertools.chain(unmatched_images, videos, unsupported):
                    # Handle output directory structure
                    output_subdirectory = args.output_directory
                    if output_subdirectory is not None:
                        file_path = Path(file)
                        output_subdirectory = Path(output_subdirectory) / file_path.parent
                        output_subdirectory = output_subdirectory.resolve()
                        if not output_subdirectory.exists() and not args.dry_run:
                            output_subdirectory.mkdir(parents=True, exist_ok=True)
                    if args.dry_run:
                        print(f"[DRY-RUN] Would copy {file} to {output_subdirectory}")
                        continue
                    print(f"Copying {file} to {output_subdirectory}")
                    if os.path.exists(output_subdirectory / file_path.name) and filecmp.cmp(input_directory / file, output_subdirectory / file_path.name):
                        if args.verbose:
                            print(f"[DEBUG][WARNING] File {file} already exists in output directory, skipping..." )
                        continue
                    shutil.copy2(input_directory / file, output_subdirectory)
            print("=" * 25)

            if failed_pairs:
                print(f"[SUMMARY] {len(failed_pairs)} file(s) skipped due to missing pair:")
                for name, reason in failed_pairs:
                    print(f"  - {name}: {reason}")
        elif args.dry_run:
            report_dry_run(args, args.input_image, args.input_image, args.input_video,
                           None, removal_kwargs, output_file=args.output_file)
        else:
            muxer = Muxer(
                image_fpath=args.input_image,
                video_fpath=args.input_video,
                exiftool=et,
                output_fpath=args.output_file,
                output_directory=args.output_directory,
                delete_video=args.delete_video,
                **removal_kwargs,
                delete_temp=not args.keep_temp,
                overwrite=args.overwrite,
                no_xmp=args.no_xmp,
                verbose=args.verbose,
            )
            muxer.mux()
            emit_result(args.input_image, "converted", output=muxer.output_fpath)

if __name__ == "__main__":
    # Cancel from the web UI sends SIGTERM: exit normally so temp files are removed
    # and the exiftool helper is shut down instead of being orphaned.
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    if len(sys.argv) == 1:
        try:
            from gooey import Gooey
        except ImportError:
            print("[ERROR] GUI mode requires the 'gooey' package (not installed). Pass CLI arguments instead, e.g. --input-directory.")
            sys.exit(1)
        main = Gooey(program_name='MotionPhoto2',
                     default_size=(1100, 820),
                     progress_regex=r"^=+\[(\d+)/(\d+)]$",
                     progress_expr="x[0] / x[1] * 100",
                     show_restart_button=False,
                    )(main)
    # Gooey reruns the script with this parameter for the actual execution.
    # Since we don't use decorator to enable commandline use, remove this parameter
    # and just run the main when in commandline mode.
    if '--ignore-gooey' in sys.argv:
        sys.argv.remove('--ignore-gooey')
    main()
