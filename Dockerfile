FROM python:3.14-slim

# The schedule is in Kyiv time; without TZ the container runs in UTC and every hour is off by 2-3 h.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Kyiv

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py ./

# Render sets PORT; secrets come from the Secret File /etc/secrets/.env at runtime, never from the image.
CMD ["python", "main.py"]
