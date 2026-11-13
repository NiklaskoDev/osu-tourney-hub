FROM python:3.15.0-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /src
RUN python -m venv /opt/venv

# Dependencies first so code changes do not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir --no-deps .


FROM python:3.15.0-slim-bookworm AS runtime

# uid 99 / gid 100 are the Unraid defaults; the entrypoint adjusts them to PUID / PGID.
RUN groupadd --non-unique --gid 100 app \
    && useradd --uid 99 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown app:app /data

COPY --from=build /opt/venv /opt/venv
COPY --chmod=0755 docker/entrypoint.sh /entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
