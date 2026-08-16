# Shortlist Audio has no dependencies beyond the Python standard library, so
# the image is just Python plus five source files. Nothing to compile, nothing
# to install, no wheels to pin.
FROM python:3.13-alpine

WORKDIR /app
COPY server.py library.py organise.py settings.py oggopus.py ./
COPY web ./web

# Input, output and state. Map all three in compose; the app neither knows nor
# cares whether they are local disks or NAS shares.
ENV SHORTLIST_LIBRARY=/library \
    SHORTLIST_OUTPUT=/media \
    SHORTLIST_STATE_DIR=/config \
    SHORTLIST_HOST=0.0.0.0 \
    SHORTLIST_PORT=7345 \
    PYTHONUNBUFFERED=1

VOLUME ["/library", "/media", "/config"]
EXPOSE 7345

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:7345/api/library', timeout=4)" || exit 1

CMD ["python3", "server.py"]
