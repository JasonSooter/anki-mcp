# The anki wheel ships prebuilt Rust extensions, so no toolchain is needed --
# slim is enough. aqt (the Anki GUI) is deliberately never installed.
# Pinned by digest; Renovate proposes digest updates weekly (renovate.json).
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# Pinned by digest: a moving tag would silently change the builder.
COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ANKI_MCP_DATA_DIR=/data \
    ANKI_MCP_STATE_DIR=/config \
    ANKI_MCP_PORT=8770

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# Install exactly what uv.lock records, then the project itself without
# re-resolving. A plain `uv pip install .` re-resolves every unpinned
# dependency at build time, so two builds of one commit could differ.
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && uv pip install --system --no-cache --no-deps . \
 && rm /tmp/requirements.txt

# The collection and the cached AnkiWeb key are bind-mounted over these at run
# time; creating them keeps the image runnable standalone for a smoke test.
RUN mkdir -p /data /config && chown -R 1000:1000 /data /config /app
USER 1000:1000

EXPOSE 8770

# Hits the one unauthenticated route. It returns 503 while the collection is
# not open, so a container that loses the collection is marked unhealthy
# rather than quietly serving errors.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8770/healthz', timeout=4).status==200 else 1)"

# Declared LAST on purpose: the arg changes on every commit, and anything below
# an ARG is rebuilt when it changes. Down here it invalidates only the label
# itself, so a revision bump does not re-run the pip install above.
#
# CI passes the commit SHA (see .github/workflows/ci.yml). `unknown` is what a
# plain `docker build` bakes when nothing sets GIT_REVISION -- treat such an
# image as unidentified rather than current: a container that cannot say what
# it was built from may be running unreviewed code, which is the failure this
# label exists to expose.
ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.revision="$GIT_REVISION"
LABEL org.opencontainers.image.source="https://github.com/JasonSooter/anki-mcp"
LABEL org.opencontainers.image.title="anki-mcp"

CMD ["python", "-m", "anki_mcp"]
