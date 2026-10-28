FROM denoland/deno:bin AS deno

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the prebuilt Deno binary instead of downloading/installing it during
# the build; the previous installer step repeatedly timed out on Railway.
COPY --from=deno /deno /usr/local/bin/deno
RUN deno --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY main.py temp_voice.py patch_main.py ./
RUN python patch_main.py && python -m py_compile main.py temp_voice.py

EXPOSE 8080
CMD ["python", "main.py"]
