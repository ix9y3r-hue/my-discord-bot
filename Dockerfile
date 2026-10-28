FROM denoland/deno:bin AS deno

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# Copy the prebuilt Deno binary instead of downloading/installing it during
# the build; the previous installer step repeatedly timed out on Railway.
COPY --from=deno /deno /usr/local/bin/deno
RUN deno --version

# Install the matching BgUtils PO Token generator script for yt-dlp.
# It runs locally inside this container; no public token server is exposed.
RUN git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /root/bgutil-ytdlp-pot-provider \
    && cd /root/bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY main.py temp_voice.py patch_main.py ./
RUN python patch_main.py && python -m py_compile main.py temp_voice.py

EXPOSE 8080
CMD ["python", "main.py"]
