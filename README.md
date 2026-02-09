# YT-Plex

YouTube downloader with per-channel settings, SponsorBlock, and auto-cleanup. Built for Plex.

## Setup

1. Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

2. Set up passwordless SSH to your server:

```bash
ssh-copy-id user@your-server-ip
```

## Deploy

```bash
./deploy.sh
```

Pushes your code to the server and rebuilds the container. Your database and downloaded videos are preserved.

## Access

Open `http://your-server-ip:8080` in your browser.
