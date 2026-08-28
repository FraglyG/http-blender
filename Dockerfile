FROM blenderkit/headless-blender:blender-5.2

USER root

RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install
COPY server.js ./
COPY lib ./lib
COPY py ./py

# Sessions live here. Without a mounted volume this is container-local, which is survivable
# (sessions are working state, not deliverables) but means a redeploy drops in-flight work.
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 80
CMD ["node", "server.js"]
