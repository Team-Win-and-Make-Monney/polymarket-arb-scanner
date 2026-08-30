FROM nginx:alpine

# Railway injects a base64url-style service token. The official Nginx image
# substitutes only this value and leaves Nginx's own $variables intact.
ENV NGINX_ENVSUBST_FILTER=SXBET_PROXY_TOKEN

RUN rm -f /etc/nginx/conf.d/default.conf && \
    mkdir -p /etc/nginx/templates

RUN cat > /etc/nginx/templates/default.conf.template << 'EOF'
limit_req_zone $binary_remote_addr zone=sxbet_proxy:10m rate=5r/s;

map $http_x_proxy_token $proxy_authorized {
    default 0;
    "${SXBET_PROXY_TOKEN}" 1;
}

server {
    listen 8080;

    # Health check
    location /healthz {
        return 200 '{"status":"ok"}';
        add_header Content-Type application/json;
    }

    # Reverse proxy all other requests to SX Bet API
    location / {
        if ($proxy_authorized = 0) { return 401; }
        limit_req zone=sxbet_proxy burst=10 nodelay;
        limit_except GET { deny all; }

        proxy_pass https://api.sx.bet;
        proxy_ssl_server_name on;
        proxy_set_header Host api.sx.bet;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header Accept $http_accept;
        proxy_set_header Content-Type $http_content_type;
        proxy_set_header Authorization "";
        proxy_set_header X-Proxy-Token "";
        proxy_connect_timeout 15s;
        proxy_read_timeout 30s;
    }
}
EOF

EXPOSE 8080

CMD ["/bin/sh", "-c", "printf '%s' \"$SXBET_PROXY_TOKEN\" | grep -Eq '^[A-Za-z0-9_-]{32,}$' || { echo 'SXBET_PROXY_TOKEN must be a 32+ character base64url token' >&2; exit 1; }; exec nginx -g 'daemon off;' "]
