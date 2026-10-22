# Runs the Python API and serves the UI on one port (Render, Fly.io, Railway, any Docker host).
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY frontend frontend
ENV HOST=0.0.0.0 PORT=5000 TRUST_PROXY=1 RELAYDROP_DATA_DIR=/data
VOLUME /data
EXPOSE 5000
CMD ["sh", "-c", "cd backend && waitress-serve --host=0.0.0.0 --port=${PORT} --threads=8 run:app"]
