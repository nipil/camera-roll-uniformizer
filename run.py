import argparse
import concurrent.futures
import dataclasses
import datetime
import io
import logging
import os
import sys
import time

import exif
import ffmpeg
import wand.image

MAX_CONFLICT_SUFFIXING = 10

REMOVE_EXTENSIONS = ('.aae',)

EXIF_IMAGE_EXTENSIONS = ('.jpg')

VIDEO_IMAGE_EXT = ('.mov', '.mp4')

TRANSFORM_IMAGE_EXTENSIONS = ('.jpeg', '.png', '.heic')
TARGET_IMAGEMAGICK_FORMAT = 'jpeg'
TARGET_IMAGEMAGICK_EXTENSION = '.jpg'

TRACE_GPX = 'trace.gpx'


class MyError(Exception):
    pass


class SkipFileError(MyError):
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


@dataclasses.dataclass(frozen=True)
class GpsInfo:
    timestamp: str
    latitude: float
    longitude: float
    altitude: float


def delete_file(path, *, dry_run=False):
    logging.debug(f'Removing file {path}')
    if not dry_run:
        os.remove(path)


def wand_transform_image(path, target_format, new_path, *, dry_run=False):
    if os.path.exists(new_path):
        raise TargetExistsError(f'{new_path} already exists')
    logging.info(f'Converting image {path} to {target_format} into {new_path}')
    if not dry_run:
        with wand.image.Image(filename=path) as original:
            with original.convert(target_format) as converted:
                converted.save(filename=new_path)
                delete_file(path, dry_run=dry_run)
    return path if dry_run else new_path


def exif_build_gps_coordinates(image, dt):
    lat = image.get('gps_latitude')
    lat_ref = image.get('gps_latitude_ref')
    lon = image.get('gps_longitude')
    lon_ref = image.get('gps_longitude_ref')
    alt = image.get('gps_altitude')
    alt_ref = image.get('gps_altitude_ref')
    logging.debug(f'GPS: {lat=} {lat_ref=} {lon=} {lon_ref=} {alt=} {alt_ref=}')
    if alt is None or alt_ref is None:
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


def exif_get_information(path):
    # https://exiv2.org/tags.html
    # https://exiftool.org/TagNames/EXIF.html
    logging.debug(f'Getting EXIF informations for {path}')
    with open(path, 'rb') as file:
        image = exif.Image(file)
        if not image.has_exif:
            raise ExifError(f'{path} has no exif information')
        exif_version = image.get('exif_version', 'Unknown')
        logging.debug(f'Exif version for {path}: {exif_version}')
        try:
            dt = image.datetime
        except AttributeError as e:
            raise ExifDateTimeError(f'{path} has no datetime information')
        dt = datetime.datetime.strptime(dt, '%Y:%m:%d %H:%M:%S')  # '2024:05:05 18:19:59'
        # build gps coordinates
        gps_coord = None
        try:
            gps_coord = exif_build_gps_coordinates(image, dt)
        except ExifGpsDataError as e:
            logging.warning(f'Cannot use {path} GPS coordinates: {e}')
        # build new name
        date_iso = dt.strftime('%Y-%m-%d')
        time_iso = dt.strftime('%H-%M-%S')
        name = dt.strftime(f'{date_iso}_{time_iso}_LOCAL')
        return name, date_iso, gps_coord


def ffmpeg_get_information(path):
    logging.debug(f'Getting FFMPEG timestamp name for {path}')
    infos = ffmpeg.probe(path)
    # MOV/MP4: format / tags / creation_time = '2024-05-12T05:38:26.000000Z'
    # MOV: format / tags / com.apple.quicktime.creationdate = '2024-05-12T14:38:26+0900'
    try:
        creation_time = infos['format']['tags']['creation_time']
    except KeyError as e:
        raise FfmpegError(f'{path} has no ffmpeg creation time')
    dt = datetime.datetime.strptime(creation_time, '%Y-%m-%dT%H:%M:%S.%fZ')
    date_iso = dt.strftime('%Y-%m-%d')
    time_iso = dt.strftime('%H-%M-%S')
    name = dt.strftime(f'{date_iso}_{time_iso}_UTC')
    # TODO: extract gps coordinates from video file ?
    gps_coord = None
    return name, date_iso, gps_coord


def create_directory(path, *, dry_run=False):
    logging.debug(f'Creating {path} folder')
    try:
        if not dry_run:
            os.mkdir(path)
            logging.info(f'Created {path} folder')
    except FileExistsError as e:
        pass


def rename_file(src_path, dst_path, *, dry_run=False):
    logging.info(f'Renaming {src_path} into {dst_path}')
    if not dry_run:
        os.rename(src_path, dst_path)


def rename_without_overwrite(path, new_name, out_directory, extension, *, dry_run=False):
    create_directory(out_directory, dry_run=dry_run)
    i = 0
    while True:
        new_path = os.path.join(out_directory, f'{new_name}{extension}')
        if not os.path.exists(new_path):
            break
        new_name = new_name + '_'
        i = i + 1
        if i > MAX_CONFLICT_SUFFIXING:
            raise TargetExistsError(f'{new_path} still exists, not trying further prefixing')
    rename_file(path, new_path, dry_run=dry_run)


def process_media(path, *, dry_run=False):
    gps_coord = None
    name, extension = os.path.splitext(os.path.basename(path))
    low_extension = extension.lower()
    # remove useless files types
    if low_extension in REMOVE_EXTENSIONS:
        delete_file(path, dry_run=dry_run)
        return
    # convert to desired image format if needed
    if low_extension in TRANSFORM_IMAGE_EXTENSIONS:
        directory = os.path.dirname(path)
        new_path = os.path.join(directory, f'{name}{TARGET_IMAGEMAGICK_EXTENSION}')
        path = wand_transform_image(path, TARGET_IMAGEMAGICK_FORMAT, new_path, dry_run=dry_run)
        name, extension = os.path.splitext(os.path.basename(path))
        low_extension = extension.lower()
    # extract date and time, move and rename
    if low_extension in EXIF_IMAGE_EXTENSIONS:
        new_name, out_directory, gps_coord = exif_get_information(path)
        rename_without_overwrite(path, new_name, out_directory, low_extension, dry_run=dry_run)
    if low_extension in VIDEO_IMAGE_EXT:
        new_name, out_directory, gps_coord = ffmpeg_get_information(path)
        rename_without_overwrite(path, new_name, out_directory, low_extension, dry_run=dry_run)
    return gps_coord


def try_process_file(path, *, dry_run=False):
    try:
        return process_media(path, dry_run=dry_run)
    except SkipFileError as e:
        logging.warning(f'Skipping file: {e}')
    return None


def queue_file(path, *, executor, dry_run=False):
    logging.info(f'Queuing file {path}')
    yield executor.submit(try_process_file, path, dry_run=dry_run)


def process_directory(path, *, executor, dry_run=False):
    logging.debug(f'Processing directory {path}')
    for root, dirs, files in os.walk(path):
        for i, file in enumerate(files):
            path = os.path.join(root, file)
            yield from queue_file(path, executor=executor, dry_run=dry_run)
        for directory in dirs:
            path = os.path.join(root, directory)
            yield from process_directory(path, executor=executor, dry_run=dry_run)


def process_source(source, *, executor, dry_run=False):
    logging.debug(f'Processing source {source}')
    if os.path.isdir(source):
        yield from process_directory(source, executor=executor, dry_run=dry_run)
    else:
        yield from queue_file(source, executor=executor, dry_run=dry_run)


def process_sources(sources, *, executor, dry_run=False):
    logging.debug(f'Processing sources {sources}')
    for source in sources:
        yield from process_source(source, executor=executor, dry_run=dry_run)


def write_gpx_trace(entries):
    logging.info('Writing GPX trace')
    entries = sorted(entries, key=lambda d: d.timestamp)
    with io.StringIO() as buf:
        buf.write('''<?xml version="1.0" encoding="utf-8"?>
            <gpx version="1.0"
            creator="ExifTool 12.85"
            xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
            xmlns="http://www.topografix.com/GPX/1/0"
            xsi:schemaLocation="http://www.topografix.com/GPX/1/0 http://www.topografix.com/GPX/1/0/gpx.xsd">
            <trk>
            <number>1</number>
            <trkseg>\n''')
        for entry in entries:
            buf.write(f'''<trkpt lat="{entry.latitude}" lon="{entry.longitude}">
                <ele>{entry.altitude}</ele>
                <time>{entry.timestamp}</time>
                </trkpt>\n''')
        buf.write('''</trkseg>
               </trk>
               </gpx>\n''')
        with open(TRACE_GPX, 'w') as file:
            file.write(buf.getvalue())


def run(args):
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = process_sources(args.sources, executor=executor, dry_run=args.dry_run)
        results = [future.result() for future in futures if future.result() is not None]
        results = sorted(results, key=lambda gps_info: gps_info.timestamp)
        print(f'{len(results)=}')
    write_gpx_trace(results)


def check_positive_int(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f'{n} is an invalid positive int value')
    return n


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser()
    parser.add_argument('sources', nargs='+')
    parser.add_argument('--workers', type=check_positive_int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--log-level', choices=['debug', 'info', 'warning', 'error', 'critical'], default='warning')
    args = parser.parse_args(argv)
    logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
                        level=getattr(logging, args.log_level.upper()))

    logging.debug(f'Parsed arguments: {args}')

    try:
        start = time.perf_counter()
        run(args)
        logging.info(f'Processing completed in {time.perf_counter() - start:.2f} seconds')
    except MyError as e:
        logging.error(e)


if __name__ == '__main__':
    main()
