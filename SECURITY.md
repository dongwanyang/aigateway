# Security Hardening Guide

## API Keys

Do not commit real Gateway API keys, provider keys, tokens, or credentials into the repository.

Use environment variables instead:

```bash
export AI_GATEWAY_API_KEY=your_gateway_key
```

Examples and documentation should use placeholders such as `YOUR_API_KEY`.

## Deployment Checklist

Before exposing AI Gateway outside a local development environment:

- Rotate any credentials that may have appeared in Git history.
- Use environment variables or secret managers for credentials.
- Change default passwords for monitoring systems.
- Restrict Redis, Qdrant, Prometheus, and Grafana access to internal networks.
- Pin Docker image versions for reproducible deployments.
- Review API authentication, quota enforcement, and request idempotency.

## Local Development

Development defaults may be simplified for convenience, but production deployments should apply network isolation, credential rotation, and access control.
