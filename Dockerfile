FROM debian:bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080

# Debian: DYMO's CUPS driver (printer-driver-dymo) is packaged for it. tini reaps the
# CUPS, Avahi and D-Bus processes of the optional network printer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip ca-certificates curl tini tzdata \
        dbus avahi-daemon cups printer-driver-dymo \
        fonts-dejavu fonts-roboto-unhinted fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Debian only packages MDI 1.x; fetch the release Home Assistant's icon names come from.
ARG MDI_VERSION=7.4.47
RUN mkdir -p /usr/share/mdi \
    && for file in css/materialdesignicons.css fonts/materialdesignicons-webfont.ttf fonts/materialdesignicons-webfont.woff2; do \
        curl -fsSL -o "/usr/share/mdi/$(basename "$file")" "https://cdn.jsdelivr.net/npm/@mdi/font@${MDI_VERSION}/${file}" || exit 1; \
    done

# The brand's display typeface (SIL Open Font License), pinned to a google/fonts commit.
ARG BRICOLAGE_COMMIT=6ce172f74aa355ea43eb964fa4a91570a4d3064d
RUN mkdir -p /usr/share/fonts/truetype/bricolage \
    && curl -fsSL -o /usr/share/fonts/truetype/bricolage/BricolageGrotesque.ttf \
        "https://raw.githubusercontent.com/google/fonts/${BRICOLAGE_COMMIT}/ofl/bricolagegrotesque/BricolageGrotesque%5Bopsz%2Cwdth%2Cwght%5D.ttf"

WORKDIR /app
COPY app/requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY app/ ./
# Mode 0700 makes CUPS run the backend as root, which it needs for the USB device.
RUN mv cups-backend-dymo /usr/lib/cups/backend/dymo && chmod 0700 /usr/lib/cups/backend/dymo

VOLUME /data
# 8080: web interface. 631: the optional network printer (IPP).
EXPOSE 8080 631

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD curl -fsS "http://127.0.0.1:${PORT}/favicon.svg" > /dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/app.py"]

LABEL \
    org.opencontainers.image.title="DYMO Label Studio" \
    org.opencontainers.image.description="Design and print labels on a USB DYMO LabelWriter, with Home Assistant integration" \
    org.opencontainers.image.source="https://github.com/madeby0203/dymo-label-studio" \
    org.opencontainers.image.licenses="MIT"
