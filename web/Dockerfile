# --- build stage ---
FROM node:22-alpine AS build
WORKDIR /app
# Disable any default supply-chain release-age gate for this offline-reproducible build.
ENV NPM_CONFIG_MINIMUM_RELEASE_AGE=0
RUN corepack enable
COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install
COPY . .
# Org-plane links. A production build without this variable must not fall back to localhost.
ARG VITE_CONSOLE_URL=https://graphban-console-console-production-ascme-labs.unkey.app
ENV VITE_CONSOLE_URL=$VITE_CONSOLE_URL
RUN pnpm build

# --- serve stage ---
FROM caddy:2.8-alpine
ARG GIT_SHA=unknown
# Caddy (GRPH-1635) replaced nginx. Defaults keep docker-compose working (HTTP on 80, HTTPS
# on 8443, proxy to the `api` service); Railway overrides PORT + API_UPSTREAM and sets
# HOSTED_MODE, which removes the HTTPS listener. The admin API stays on localhost inside the
# container (Caddyfile) and is not published. Upstream hostnames are re-resolved on each dial,
# which is what keeps a redeployed backend reachable (GRPH-523).
ENV PORT=80 \
    HTTPS_PORT=8443 \
    API_SCHEME=http \
    API_UPSTREAM=api:8000 \
    ASK_URL=http://api:8000/api/https/ask
COPY Caddyfile https.on.caddy https.off.caddy redirect.on.caddy redirect.off.caddy /etc/caddy/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=build /app/dist /usr/share/caddy/html
# Release identity for the static bundle — queryable at /version.txt.
RUN echo "$GIT_SHA" > /usr/share/caddy/html/version.txt
EXPOSE 80 8443
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
