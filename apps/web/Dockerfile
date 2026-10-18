FROM node:23-alpine
WORKDIR /app
RUN npm install -g pnpm
# The workspace-root pnpm-lock.yaml is authoritative but lies outside this
# build context (./apps/web); this dev-only image resolves fresh from package.json.
COPY package.json .npmrc ./
RUN pnpm install
COPY . .
EXPOSE 3000
CMD ["pnpm", "dev"]
