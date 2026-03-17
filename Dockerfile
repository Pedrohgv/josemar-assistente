# Use official OpenClaw image as base
FROM ghcr.io/openclaw/openclaw:latest

# Install Python and PDF processing dependencies
RUN apt-get update && apt-get install -y python3 python3-pip \
    && pip3 install --no-cache-dir pymupdf \
    && rm -rf /var/lib/apt/lists/*

# Create scripts directory
RUN mkdir -p /app/scripts

# Copy PDF extraction script
COPY scripts/pdf_extractor.py /app/scripts/

# Make script executable
RUN chmod +x /app/scripts/pdf_extractor.py

# Create skills directory
RUN mkdir -p /root/.openclaw/skills

# Copy custom skills
COPY skills/pdf-extractor /root/.openclaw/skills/pdf-extractor/

# Make skill executable
RUN chmod +x /root/.openclaw/skills/pdf-extractor/pdf-extractor

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/

# Make entrypoint executable
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set entrypoint
ENTRYPOINT ["docker-entrypoint.sh"]

# Set working directory
WORKDIR /app

# Default command (OpenClaw default)
CMD ["openclaw", "serve"]
