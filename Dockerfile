# Shortform Audio Bookshelf has no dependencies beyond the Python standard library, so
# the image is just Python plus seven source files. Nothing to compile, nothing
# to install, no wheels to pin.
FROM python:3.13-alpine

WORKDIR /app
COPY server.py library.py organise.py settings.py oggopus.py metadata.py users.py ./
COPY web ./web

# /library is what gets played. /incoming is the download folder that Import
# takes files out of — never played from. /export is an optional tidy copy for
# other tools. /config is everything the app remembers.
#
# Not /media: the base image already has a root-owned /media, so an unmapped
# output folder looked like a real directory that could not be written to.
ENV SHORTLIST_LIBRARY=/library \
    SHORTLIST_IMPORT=/incoming \
    SHORTLIST_OUTPUT=/export \
    SHORTLIST_STATE_DIR=/config \
    SHORTLIST_HOST=0.0.0.0 \
    SHORTLIST_PORT=7345 \
    PYTHONUNBUFFERED=1

# No VOLUME declaration on purpose. It would make Docker invent an anonymous,
# root-owned volume for any of these paths a user does not map — which then
# fails to be writable, is invisible in the compose file, and quietly
# accumulates on disk. Bind mounts from compose are the only sane source.
EXPOSE 7345

# Probe the page shell, not the API: once accounts exist /api/library answers
# 401 and the container would be marked unhealthy forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:7345/', timeout=4)" || exit 1

CMD ["python3", "server.py"]
