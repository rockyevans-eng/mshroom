# MSHroom -- container image.
#
# Base: python:3.12-slim (the project targets Python 3.12+; CI also runs
# against whatever 3.12.x is current).
#
# Deliberately does NOT `pip install .` (the project package itself).
# hl7kit/dictionary.py loads hl7kit/data/fields_v251.json via a
# `Path(__file__).parent`-relative filesystem path, and app/main.py loads
# app/static/* the same way -- neither goes through Python package
# resources, and pyproject.toml's [tool.setuptools] block does not declare
# package-data for hl7kit/data/*.json. A real `pip install .` build could
# therefore silently drop the JSON dictionary (and/or static assets) from
# the installed wheel depending on setuptools' file-discovery defaults.
# The dev/test environment never exercises this because it uses
# `pip install -e .[dev]` (editable install -- points straight at the
# source tree, so the gap is invisible there). Sidestepping it entirely:
# install only the runtime dependencies, then copy the source tree
# verbatim and run it in place, exactly like the documented local dev
# command (`python -m uvicorn app.main:app ...` from the repo root).
FROM python:3.12-slim

WORKDIR /app

# Runtime dependencies only (matches [project.dependencies] in
# pyproject.toml -- NOT the [dev] extra, which is test-only).
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.29"

COPY hl7kit/ ./hl7kit/
COPY app/ ./app/
COPY corpus/ ./corpus/

# web UI (Viewer/Sender/Listener page + API) and MLLP listener.
EXPOSE 8550 6671

# capture.db lands at /app/capture.db (BASE_DIR/"capture.db" in
# app/main.py, where BASE_DIR is the parent of app/ -- i.e. this WORKDIR).
# docker-compose.yml bind-mounts that exact path to ./data/capture.db on
# the host so captures survive container restarts/recreation.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8550"]
