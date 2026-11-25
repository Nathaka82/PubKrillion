FROM node:24-bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && python3 -m venv /opt/knowledge \
    && /opt/knowledge/bin/pip install --no-cache-dir duckdb==2.0.1 wordfreq==3.1.1 \
    && rm -rf /var/lib/apt/lists/*
ENV KNOWLEDGE_PYTHON=/opt/knowledge/bin/python
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY --chown=node:node server ./server
COPY --chown=node:node data ./data
COPY --chown=node:node public ./public
COPY --chown=node:node scripts ./scripts
RUN mkdir -p /app/storage /app/backups && chown node:node /app/storage /app/backups
USER node
ARG APP_REVISION=local
ENV APP_REVISION=$APP_REVISION
ENV NODE_ENV=production BIND_ADDRESS=0.0.0.0 PORT=3000
EXPOSE 3000
CMD ["node","server/main.ts"]
