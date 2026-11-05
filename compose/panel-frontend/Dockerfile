# Build stage only (output is static files) — node floats on the major line (22.x).
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
# package-lock.json is gitignored server-local state (see .gitignore): a fresh
# clone has none — fall back to `npm install` (resolves within the declared
# ranges). On the server, `make update` keeps the lockfile current, so builds
# there are reproducible (npm ci).
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY . .
RUN npm run build

FROM nginx:stable-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
