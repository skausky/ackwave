FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "numpy>=1.21" "pyserial>=3.5" "esptool>=4.7,<5"
COPY tools /app/tools
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
