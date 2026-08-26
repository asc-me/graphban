# --- build stage ---
FROM node:22-alpine AS build
WORKDIR /app
# Disable any default supply-chain release-age gate for this offline-reproducible build.
ENV NPM_CONFIG_MINIMUM_RELEASE_AGE=0
RUN corepack enable
COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install
COPY . .
RUN pnpm build

# --- serve stage ---
FROM nginx:1.27-alpine
ARG GIT_SHA=unknown
# The nginx image renders templates/*.template with envsubst on startup. Defaults keep
# docker-compose working (listen 80, proxy to the `api` service); Railway overrides
# PORT + API_UPSTREAM. The filter limits substitution so nginx runtime vars survive (AL-27).
# NGINX_ENTRYPOINT_LOCAL_RESOLVERS activates the image's own 15-local-resolvers.envsh,
# which reads /etc/resolv.conf and exports NGINX_LOCAL_RESOLVERS. It is a `.envsh`, so the
# entrypoint SOURCES it and the export survives into 20-envsubst-on-templates.sh. That is
# what lets the template name a resolver without hardcoding a DNS address that differs
# between docker-compose and Railway (GRPH-523).
ENV PORT=80 \
    API_SCHEME=http \
    API_UPSTREAM=api:8000 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER=^(PORT|API_SCHEME|API_UPSTREAM|NGINX_LOCAL_RESOLVERS)$
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
# Release identity for the static bundle — queryable at /version.txt.
RUN echo "$GIT_SHA" > /usr/share/nginx/html/version.txt
EXPOSE 80
