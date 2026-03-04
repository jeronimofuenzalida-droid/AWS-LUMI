ARG BASE_IMAGE_URI
FROM ${BASE_IMAGE_URI}

WORKDIR /app

COPY app.py /app/app.py
COPY pos_tagging.py /app/pos_tagging.py

ENTRYPOINT ["python", "-u", "/app/app.py"]
