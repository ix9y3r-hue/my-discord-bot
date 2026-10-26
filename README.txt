YouTube playback troubleshooting files for the Discord bot

Included:
- requirements.txt: updates yt-dlp
- Dockerfile: installs FFmpeg and Deno, then applies the small main.py fix during build
- nixpacks.toml: adds Deno and applies the same fix when Railway uses Nixpacks
- patch_main.py: adds yt-dlp's Deno JavaScript runtime setting to the existing main.py

How to use:
1. Back up your project.
2. Extract these files into the root folder of your existing bot project, beside main.py and temp_voice.py, and replace the matching files.
3. Keep your existing main.py; the deployment build will apply the change automatically.
4. Redeploy on Railway.

This may help with YouTube JavaScript challenge failures, but cannot guarantee a fix if YouTube blocks Railway's IP or requires account cookies.
