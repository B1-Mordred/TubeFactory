FROM node:22.17.0-bookworm-slim

ENV NODE_ENV=production
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 libxrandr2 \
    libxkbcommon-dev libxfixes3 libxcomposite1 libxdamage1 libatk-bridge2.0-0 \
    libpango-1.0-0 libcairo2 libcups2 fonts-liberation && \
    rm -rf /var/lib/apt/lists/*
COPY services/render-service/package.json services/render-service/package-lock.json ./
RUN npm ci && npx remotion browser ensure
COPY services/render-service ./
RUN npm test
USER node
EXPOSE 8091
CMD ["node", "server.mjs"]
