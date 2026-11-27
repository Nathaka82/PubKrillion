FROM node:24-bookworm-slim
WORKDIR /app
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
