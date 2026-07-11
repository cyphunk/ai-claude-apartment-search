# Playwright's official Python image ships Chromium + all system deps preinstalled.
# CRITICAL: this image tag bakes in ONE Chromium build, matching exactly this
# Playwright version. The pip pin in requirements.txt MUST stay in lockstep with
# it (same X.Y.Z). If they drift, pip installs a Playwright that looks for a
# Chromium build the image doesn't contain, and every launch fails with
# "Executable doesn't exist" — a silent, total scraper outage. Bump both together.
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY howoge_watch.py .

# Persisted seen-state lives on a mounted volume at /data (see README / railway.json)
ENV SEEN_PATH=/data/seen_listings.json

# Long-running worker: just run the loop.
CMD ["python3", "howoge_watch.py"]
