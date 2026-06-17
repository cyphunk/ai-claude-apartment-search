# Playwright's official Python image ships Chromium + all system deps preinstalled.
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY howoge_watch.py .

# Persisted seen-state lives on a mounted volume at /data (see README / railway.json)
ENV SEEN_PATH=/data/seen_listings.json

# Long-running worker: just run the loop.
CMD ["python3", "howoge_watch.py"]
