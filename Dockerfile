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

RUN mkdir -p /tmp/blender_tasks

EXPOSE 80
CMD ["node", "server.js"]