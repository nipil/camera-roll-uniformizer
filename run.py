import json
import logging
import sys
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Generator, Any
from zoneinfo import ZoneInfo

import exif
import ffmpeg
from wand.image import Image

MAX_CONFLICT_SUFFIXING = 10

REMOVE_EXTENSIONS = ('.aae', '.json')

EXIF_IMAGE_EXTENSIONS = ('.jpg',)

VIDEO_IMAGE_EXT = ('.mov', '.mp4')

TRANSFORM_IMAGE_EXTENSIONS = ('.jpeg', '.heic')
TARGET_IMAGEMAGICK_FORMAT = 'jpeg'
TARGET_IMAGEMAGICK_EXTENSION = '.jpg'

TRACE_GPX = 'trace.gpx'

LOCAL_ZONE_INFO = ZoneInfo('Africa/Johannesburg')


class AppError(Exception):
    pass


class SkipFileError(AppError):
    pass


class TargetExistsError(SkipFileError):
    pass


class FfmpegError(SkipFileError):
    pass


class ExifError(SkipFileError):
    pass


class ExifDateTimeError(ExifError):
    pass


class ExifGpsDataError(ExifError):
    pass


@dataclass(frozen=True)
class GpsInfo:
    timestamp: str
    latitude: float
    longitude: float
    altitude: float


@dataclass(frozen=True)
class MediaInfo:
    date_iso: str
    time_iso: str
    gps: GpsInfo | None


def delete_file(path: Path) -> None:
    logging.debug(f'Removing file {path}')
    path.unlink(missing_ok=False)


def transform_image(src: Path, fmt: str, target: Path) -> Path:
    if target.exists():
        raise TargetExistsError(f'{target} already exists')
    logging.info(f'Converting image {src} to {fmt} into {target}')
    with Image(filename=src) as original:
        with original.convert(fmt) as converted:
            converted.save(filename=target)
            delete_file(src)
    return target


# FIXME
def exif_build_gps_coordinates(image: exif.Image, dt: datetime):
    lat = image.get('gps_latitude')
    lat_ref = image.get('gps_latitude_ref')
    lon = image.get('gps_longitude')
    lon_ref = image.get('gps_longitude_ref')
    alt = image.get('gps_altitude')
    alt_ref = image.get('gps_altitude_ref')
    logging.debug(f'GPS: {lat=} {lat_ref=} {lon=} {lon_ref=} {alt=} {alt_ref=}')
    if alt is None or alt_ref is None:
        # alt = 0
        # alt_ref = exif.GpsAltitudeRef.ABOVE_SEA_LEVEL
        # logging.info('Resetting missing GPS altitude in EXIF data to zero')
        raise ExifGpsDataError('Missing GPS altitude in EXIF data')
    if lat is None or lat_ref is None:
        raise ExifGpsDataError('Missing GPS latitude in EXIF data')
    if lon is None or lon_ref is None:
        raise ExifGpsDataError('Missing GPS longitude in EXIF data')
    if alt_ref not in (exif.GpsAltitudeRef.ABOVE_SEA_LEVEL, exif.GpsAltitudeRef.BELOW_SEA_LEVEL):
        raise ExifGpsDataError('Invalid GPS altitude in EXIF data')
    if lat_ref not in ('N', 'S') or len(lat) != 3:
        raise ExifGpsDataError('Invalid GPS latitude in EXIF data')
    if lon_ref not in ('E', 'W') or len(lon) != 3:
        raise ExifGpsDataError('Invalid GPS longitude in EXIF data')
    return GpsInfo(timestamp=dt.strftime('%Y-%m-%dT%H:%M:%SZ'),
                   latitude=(lat[0] + lat[1] / 60.0 + lat[2] / 3600.0) * (-1 if lat_ref == 'S' else 1),
                   longitude=(lon[0] + lon[1] / 60.0 + lon[2] / 3600.0) * (-1 if lon_ref == 'W' else 1),
                   altitude=alt * (-1 if alt_ref == exif.GpsAltitudeRef.BELOW_SEA_LEVEL else 1))


def exif_get_image_information(path: Path, image: exif.Image) -> MediaInfo:
    # https://exiv2.org/tags.html
    # https://exiftool.org/TagNames/EXIF.html
    exif_version = image.get('exif_version', 'Unknown')
    logging.debug(f'Exif version for {path}: {exif_version}')
    try:
        dt = datetime.strptime(image.datetime, '%Y:%m:%d %H:%M:%S')
    except AttributeError:
        raise ExifDateTimeError(f'{path} has no datetime information')
    dt = dt.replace(tzinfo=LOCAL_ZONE_INFO)
    try:
        gps = exif_build_gps_coordinates(image, dt)
    except ExifGpsDataError as e:
        logging.warning(f'Cannot use {path} GPS coordinates: {e}')
        gps = None
    return MediaInfo(date_iso=dt.strftime('%Y-%m-%d'), time_iso=dt.strftime('%H-%M-%S'), gps=gps)


def exif_get_path_information(path: Path) -> MediaInfo:
    logging.debug(f'Getting EXIF informations for {path}')
    with open(path, 'rb') as file:
        image = exif.Image(file)
        if not image.has_exif:
            raise ExifError(f'{path} has no exif information')
        return exif_get_image_information(path, image)


def dump_ffmpeg_infos(path: Path, infos: Any):
    with open(f'{path}.json', 'wt') as f:
        json.dump(infos, f, sort_keys=True, indent=4)


def ffmpeg_get_information(path: Path) -> MediaInfo:
    logging.debug(f'Getting FFMPEG timestamp name for {path}')
    try:
        infos = ffmpeg.probe(path)
    except FileNotFoundError as e:
        raise AppError(f'ffprobe raised a "file not found" exception : are you sure it is installed ?')
    dump_ffmpeg_infos(path, infos)
    try:
        # MOV/MP4: format / tags / creation_time = '2024-05-12T05:38:26.000000Z'
        # MOV: format / tags / com.apple.quicktime.creationdate = '2024-05-12T14:38:26+0900'
        creation_time = infos['format']['tags']['creation_time']
    except KeyError:
        raise FfmpegError(f'{path} has no ffmpeg creation time')
    dt = datetime.strptime(creation_time, '%Y-%m-%dT%H:%M:%S.%fZ')
    dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(LOCAL_ZONE_INFO)
    # TODO: extract gps coordinates from video file ?
    return MediaInfo(date_iso=dt.strftime('%Y-%m-%d'), time_iso=dt.strftime('%H-%M-%S'), gps=None)


def rename_without_overwrite(src: Path, folder: Path, info: MediaInfo, extension: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    i = 0
    while True:
        padding = '_' * i
        target = Path(folder) / f'{info.date_iso}_{info.time_iso}{padding}{extension}'
        if not target.exists():
            logging.info(f'Renaming {src} into {target}')
            return src.rename(target)
        i += 1
        if i > MAX_CONFLICT_SUFFIXING:
            raise TargetExistsError(f'{target} still exists, not trying further prefixing')


def process_media(path: Path) -> GpsInfo | None:
    gps = None
    if path.suffix.lower() in REMOVE_EXTENSIONS:
        delete_file(path)
        return None
    if path.suffix.lower() in TRANSFORM_IMAGE_EXTENSIONS and path.suffix.lower() != TARGET_IMAGEMAGICK_EXTENSION:
        path = transform_image(path, TARGET_IMAGEMAGICK_FORMAT, path.with_suffix(TARGET_IMAGEMAGICK_EXTENSION))
    if path.suffix.lower() in EXIF_IMAGE_EXTENSIONS:
        info = exif_get_path_information(path)
        gps = info.gps
        path = rename_without_overwrite(path, Path(info.date_iso), info, path.suffix.lower())
    if path.suffix.lower() in VIDEO_IMAGE_EXT:
        info = ffmpeg_get_information(path)
        gps = info.gps
        path = rename_without_overwrite(path, Path(info.date_iso), info, path.suffix.lower())
    logging.debug(f'Final {path} GPS coordinates: {gps}')
    return gps


def try_process_file(path: Path) -> GpsInfo | None:
    return None


def write_gpx_trace(entries: list[GpsInfo], *, output_file: Path) -> None:
    logging.info('Writing GPX trace')
    entries = sorted(entries, key=lambda d: d.timestamp)
    with open(output_file, 'w') as f:
        f.write('''<?xml version="1.0" encoding="utf-8"?>
            <gpx version="1.0"
            creator="ExifTool 12.85"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
            xmlns="https://www.topografix.com/GPX/1/0"
            xsi:schemaLocation="https://www.topografix.com/GPX/1/0 https://www.topografix.com/GPX/1/0/gpx.xsd">
            <trk>
            <number>1</number>
            <trkseg>\n''')
        for entry in entries:
            f.write(f'''<trkpt lat="{entry.latitude}" lon="{entry.longitude}">
                <ele>{entry.altitude}</ele>
                <time>{entry.timestamp}</time>
                </trkpt>\n''')
        f.write('''</trkseg>
               </trk>
               </gpx>\n''')


def get_directory_files(directory: Path) -> Generator[Path, None, None]:
    for child in directory.iterdir():
        if child.is_dir():
            yield from get_directory_files(child)
        else:
            yield child


def get_source_files(source: Path) -> Generator[Path, None, None]:
    if source.is_dir():
        yield from get_directory_files(source)
    else:
        yield source


def get_sources_files(sources: list[Path]) -> Generator[Path, None, None]:
    for source in sources:
        yield from get_source_files(source)


def process_files(files: list[Path]) -> Generator[GpsInfo, None, None]:
    for file in files:
        result = None
        try:
            result = process_media(file)
        except SkipFileError as e:
            logging.warning(f'Skipping file {file}: {e}')
        except AppError as e:
            logging.error(f'Application error: {e}')
            break
        if result is None:
            continue
        yield result


def main(argv: list[str] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    parser = ArgumentParser()
    parser.add_argument('sources', nargs='+', type=Path)
    parser.add_argument('--log-level', choices=['debug', 'info', 'warning', 'error', 'critical'], default='warning')
    args = parser.parse_args(argv)
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
                        level=getattr(logging, args.log_level.upper()))

    logging.debug(f'Parsed arguments: {args}')
    try:
        start = perf_counter()
        # TODO: provide hour shift to subprocessing
        files = list(get_sources_files(args.sources))
        results = list(process_files(files))
        write_gpx_trace(results, output_file=Path(TRACE_GPX))
        logging.info(f'Processing completed in {perf_counter() - start:.2f} seconds')
    except AppError as e:
        logging.error(e)


if __name__ == '__main__':
    main()
