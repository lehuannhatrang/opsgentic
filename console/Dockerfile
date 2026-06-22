FROM node:20-slim
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force
COPY server.js ./
COPY public ./public
EXPOSE 3000
USER node
CMD ["node", "server.js"]
