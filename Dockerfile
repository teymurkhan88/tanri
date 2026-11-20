FROM python:3.10-slim

WORKDIR /app

ENV XRAY_LOCATION_ASSET=/usr/local/bin/xray
ENV PATH="/usr/local/bin/xray:${PATH}"

# نصب Nginx و هسته رسمی Xray
# نسخه باید XHTTP داشته باشد (کانفیگ ۴ روی ترنسپورت xhttp است) ⇒ v25+ الزامی است.
ARG XRAY_VERSION=v26.11.9
RUN apt-get update && apt-get install -y nginx wget unzip procps curl ca-certificates && \
    rm -rf /etc/nginx/sites-enabled/* /etc/nginx/conf.d/* && \
    mkdir -p /usr/local/bin/xray /run /var/log/nginx && \
    ( for i in 1 2 3; do \
        wget -q -T 60 -O /tmp/xray.zip \
          "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" && break; \
        echo "download attempt $i failed, retrying..."; sleep 3; \
      done ) && \
    unzip -q /tmp/xray.zip -d /usr/local/bin/xray && \
    chmod +x /usr/local/bin/xray/xray && \
    rm -f /tmp/xray.zip && \
    /usr/local/bin/xray/xray -version && \
    printf '%s' '{"inbounds":[{"tag":"t","port":12999,"listen":"127.0.0.1","protocol":"vless","settings":{"clients":[{"id":"11111111-1111-1111-1111-111111111111"}],"decryption":"none"},"streamSettings":{"network":"xhttp","security":"none","xhttpSettings":{"path":"/probe","mode":"auto"}}}]}' > /tmp/xh.json && \
    /usr/local/bin/xray/xray run -test -c /tmp/xh.json && \
    apt-get clean

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
# لاگ‌ها بدون بافر بیرون بیایند تا در Railway/داکر بلافاصله دیده شوند
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python", "-u", "app.py"]
