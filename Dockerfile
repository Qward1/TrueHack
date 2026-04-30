FROM python:3.12-slim AS lua-builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY vendor/lua-5.5.0 /build/lua-5.5.0

RUN cd /build/lua-5.5.0 \
    && make linux \
    && make INSTALL_TOP=/opt/lua install


FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=lua-builder /opt/lua /opt/lua
RUN ln -sf /opt/lua/bin/lua /usr/local/bin/lua55 \
    && ln -sf /opt/lua/bin/luac /usr/local/bin/luac55

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "/app/docker/app-entrypoint.sh"]
