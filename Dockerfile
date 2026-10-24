# Public, read-only demo of HireLoop: a made-up candidate and sample jobs.
# Every button that would change, run or send something is switched off.
# To use HireLoop for your own job search, run it on your computer instead
# (see README.md) - it signs in to your accounts and is not meant to be hosted.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JOBHUNT_READ_ONLY=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Render and most hosts pass the port in $PORT; 7860 suits Hugging Face Spaces.
EXPOSE 7860
CMD ["sh", "-c", "python run.py demo --read-only --host 0.0.0.0 --port ${PORT:-7860} --no-open --data-dir /tmp/hireloop-demo"]
