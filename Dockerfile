# Shortform Audio Bookshelf has no dependencies beyond the Python standard library, so
# the image is just Python plus seven source files. Nothing to compile, nothing
# to install, no wheels to pin.
FROM python:3.13-alpine

WORKDIR /app
COPY server.py library.py organise.py settings.py oggopus.py metadata.py users.py ./
COPY web ./web

# /library is what gets played. /incoming is the download folder that Import
# takes files out of — never played from. /media is an optional tidy export.
# /config is everything the app remembers.
ENV SHORTLIST_LIBRARY=/library \
    SHORTLIST_IMPORT=/incoming \
    SHORTLIST_OUTPUT=/media \
    SHORTLIST_STATE_DIR=/config \
    SHORTLIST_HOST=0.0.0.0 \
    SHORTLIST_PORT=7345 \
    PYTHONUNBUFFERED=1

VOLUME ["/library", "/incoming", "/media", "/config"]
EXPOSE 7345

# Probe the page shell, not the API: once accounts exist /api/library answers
# 401 and the container would be marked unhealthy forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:7345/', timeout=4)" || exit 1

CMD ["python3", "server.py"]
