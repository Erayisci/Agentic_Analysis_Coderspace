# Frontend image: the Vite dev server (see docker-compose.yml comment for why
# dev mode rather than a production build+nginx -- this is a demo-day image).
FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm install

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
