FROM python:3.11-slim
RUN useradd -r -s /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER app
CMD ["python3", "-u", "main.py"]
