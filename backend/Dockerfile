FROM python:3.11-slim

# System deps needed by OpenCV at runtime. Even the headless build of OpenCV
# links against libGL / glib, so these must be present on the slim image.
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so Docker can cache this layer between code changes.
# requirements.txt already pins opencv-python-headless (correct for a headless
# server image) - do NOT switch to opencv-python or the slim image will fail
# pulling in GUI libraries it does not have.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hugging Face Spaces (Docker SDK) routes external traffic to port 7860.
EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
