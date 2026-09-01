FROM python:3.11-slim
RUN useradd -r -s /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# /app/data is the mount point for the velomate-token-data volume, which holds
# the refresh-token fallback. Docker creates a missing mount point as root, and
# the container runs as `app` — so without this the fallback write fails with
# EACCES and a rotated Strava token is lost on the next restart.
RUN mkdir -p /app/data && chown app:app /app/data
USER app
CMD ["python3", "-u", "main.py"]
