# camera-roll-uniformizer

python program to merge ios/android camera rolls, and making their naming and timestamps uniforms


# Install

Install binary : https://imagemagick.org/script/download.php#windows

    winget install --source winget --exact --id ImageMagick.ImageMagick.Q16

Set environment variable `MAGICK_HOME` to `C:\Program Files\ImageMagick-...`

Install package via PIP : `Wand`

Install binary : https://ffmpeg.org/download.html#build-windows

    winget install --source winget --exact --id Gyan.FFmpeg

Add install path to the user's environment variable `PATH`

Install package via PIP : `ffmpeg-python`

Install package via PIP : `exif`


# Run

Use `--dry-run` option to run in dry-run mode

Add folders and files as command line arguments to have them walked for files


# View GPS trace

Browse to https://gpx.studio/

Click on "load" GPX, choose "Desktop", browse to your `trace.gpx` file

Move the green cursor at the bottom to see the trace

# Misc

This `exiftool` command might help fix dates :

    exiftool "-AllDates<DateTimeOriginal" *
