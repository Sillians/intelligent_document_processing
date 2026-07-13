# Production TLS Runtime Files

The production workflow creates these untracked files from protected GitHub
`production` environment secrets:

```text
production.crt
production.key
```

Required secrets:

```text
PRODUCTION_TLS_CERT
PRODUCTION_TLS_KEY
```

Never commit certificate private keys. `docker-compose.prod.yml` mounts the
runtime files read-only into Traefik, and `dynamic.production.yml` enforces TLS
1.2 or newer, strict SNI, HTTPS-only API routing, and permanent HTTP redirects.
