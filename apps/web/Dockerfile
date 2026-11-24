# Multi-stage Cloud Run Dockerfile for the SyrinxOne Web UI (React + Vite + WebGL2)
# Build from repository root:
#   gcloud builds submit --config cloudbuild.web.yaml

FROM node:22-alpine AS builder
WORKDIR /app

# Copy workspace manifests first for layer caching
COPY package.json package-lock.json ./
COPY packages/shared/package.json ./packages/shared/
COPY apps/web/package.json ./apps/web/
COPY apps/desktop-electron/package.json ./apps/desktop-electron/

RUN npm ci --workspace=@syrinxone/shared --workspace=@syrinxone/web --include-workspace-root

# Copy source trees needed for the web build
COPY packages/shared ./packages/shared
COPY apps/web ./apps/web

ARG VITE_API_URL=https://syrinx-engine-466330924576.us-west1.run.app
ENV VITE_API_URL=${VITE_API_URL}

RUN npm run build --workspace=@syrinxone/shared \
    && npm run build --workspace=@syrinxone/web

FROM nginx:1.27-alpine
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/apps/web/dist /usr/share/nginx/html

EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
