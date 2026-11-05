# The base image is pinned by digest, so a rebuild cannot silently get a different image.
# To update it: docker pull python:3.12-slim, then copy the new digest from
# docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

# No .pyc files: the code folder is read-only for the app user.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# requirements.lock has every package with its exact version, including the ones pulled in
# by other packages, so two builds install the same thing.
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# The code stays owned by root, so the running app cannot change its own files.
# Only the export folder belongs to the app user.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /code/exports \
    && chown appuser:appuser /code/exports

USER appuser

ENTRYPOINT ["./entrypoint.sh"]
