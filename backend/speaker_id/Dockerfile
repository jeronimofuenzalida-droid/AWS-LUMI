FROM public.ecr.aws/lambda/python:3.12

# Minimal tools for unpacking the ffmpeg static tarball.
RUN microdnf install -y tar xz && microdnf clean all

# Force all model/cache writes to /tmp (Lambda writable) instead of $HOME.
ENV HF_HOME=/tmp/hf
ENV XDG_CACHE_HOME=/tmp/.cache
ENV TORCH_HOME=/tmp/torch

# ffmpeg static build (for decoding mp3/m4a/etc and slicing audio)
RUN curl -L -o /tmp/ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
  && mkdir -p /tmp/ffmpeg \
  && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg --strip-components=1 \
  && mv /tmp/ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
  && mv /tmp/ffmpeg/ffprobe /usr/local/bin/ffprobe \
  && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
  && rm -rf /tmp/ffmpeg /tmp/ffmpeg.tar.xz

COPY requirements.txt ${LAMBDA_TASK_ROOT}/requirements.txt

# Install PyTorch CPU + SpeechBrain + deps.
# Note: Image size is large; this is the minimal path to real voice embeddings.
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Store downloaded model weights in /tmp (persists for warm containers).
ENV SB_MODEL_DIR=/tmp/models/spkrec

COPY app.py ${LAMBDA_TASK_ROOT}/app.py

CMD ["app.handler"]
