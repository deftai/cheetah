# Keep this equal to the `playwright` version in package.json. Playwright launches the browser
# build that matches its own version, and the image contains only that build; the check in the
# build stage fails the build if the two drift apart. A Playwright bump must change both.
ARG PLAYWRIGHT_VERSION=1.63.0

FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble AS build
ARG PLAYWRIGHT_VERSION

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci
RUN v="$(node -p "require('playwright/package.json').version")" \
    && [ "$v" = "${PLAYWRIGHT_VERSION}" ] \
    || { echo "playwright $v (package.json) != image tag ${PLAYWRIGHT_VERSION}: bump ARG PLAYWRIGHT_VERSION at the top of this Dockerfile to match" >&2; exit 1; }

COPY tsconfig.json ./
COPY src/ ./src/
RUN npx tsc

FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble

# The image installs Node from the NodeSource repository when it is built, so it can lag the
# Node security releases. Pick up the newest patch of the Node major it ships. Not reproducible
# on purpose, and it only runs on an uncached build (use --no-cache or --pull to refresh it).
# Remove it once the base image tag ships a patched Node.
RUN apt-get update \
    && apt-get install -y --only-upgrade nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci --production

COPY --from=build /app/dist/ ./dist/

ENV NODE_ENV=production
ENV STREAM=browser_jobs
ENV GROUP=browser-workers

USER pwuser

CMD ["node", "dist/index.js"]
