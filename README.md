# system_disk

A lightweight filesystem-activity monitor that identifies inactive or unused filesystems, for storage optimization on on-prem and cloud Linux hosts.

![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-FCC624?logo=linux&logoColor=black)
![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?logo=githubactions&logoColor=white)

## What it does

- Tracks **disk usage and I/O activity** over a configurable window (default 30 days).
- **Safely excludes system-critical mounts** so it never flags `/`, `/boot`, etc.
- Generates reports highlighting filesystems that can be shrunk, archived, or reclaimed.
- Ships with a **GitHub Actions workflow** for automated runs.

## Run it

```bash
python3 monitor_fs.py
```

---

Part of my portfolio — more at [npkpadala.com](https://npkpadala.com).
