# Twinarr - seed the same torrent on more trackers (by H0m3Lab)
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY twinarr.py /app/
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
VOLUME /data
ENTRYPOINT ["python3", "/app/twinarr.py"]
CMD ["daemon"]
