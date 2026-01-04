# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a DVD auto-ripper utility for Linux servers. It automatically detects DVD insertion via udev, rips the disc using HandBrake, and transfers the output to a NAS.

## Architecture

- **scripts/dvd-utils.sh**: Bash library with helper functions (logging, state management, DVD operations, NAS transfer)
- **scripts/dvd-ripper.sh**: Main orchestration script that handles the complete workflow
- **config/dvd-ripper.conf.example**: Configuration template (deployed to /etc/dvd-ripper.conf)
- **deploy.sh**: Deployment script for syncing to remote server
- **remote-install.sh**: Installation script for remote server (requires sudo)

## Development Workflow

### Making Changes

1. Edit scripts locally
2. Commit and push to GitHub: `git push origin master`
3. SSH to server and pull changes: `ssh <user>@<server> 'cd ~/dvd-auto-ripper && git pull'`
4. Run install script: `ssh <user>@<server> 'cd ~/dvd-auto-ripper && sudo ./remote-install.sh'`

### Initial Server Setup

```bash
# SSH to server (use -J <jump-host> if behind a jump box)
ssh <user>@<server>

# Install dependencies (Ubuntu/Debian)
sudo apt update
sudo apt install -y git handbrake-cli rsync openssh-client eject

# Clone the repo
git clone https://github.com/mschober/dvd-auto-ripper.git ~/dvd-auto-ripper

# Run installation
cd ~/dvd-auto-ripper
sudo ./remote-install.sh
```

### Testing

Manual testing:
```bash
# On remote server
sudo /usr/local/bin/dvd-ripper.sh /dev/sr0
tail -f /var/log/dvd-ripper.log
```

### Deployment

```bash
# From local machine - commit and push changes
git add -A && git commit -m "Your message" && git push

# On remote server
ssh <user>@<server> 'cd ~/dvd-auto-ripper && git pull && sudo ./remote-install.sh'
```

## Dependencies

- HandBrake CLI (handbrake-cli)
- rsync
- openssh-client
- eject
- bash 4.0+

## Common Tasks

- **View logs**: `ssh <user>@<server> 'tail -f /var/log/dvd-ripper.log'`
- **Check config**: `ssh <user>@<server> 'sudo cat /etc/dvd-ripper.conf'`
- **Check for running rips**: `ssh <user>@<server> 'cat /var/run/dvd-ripper.pid'`
- **View staging files**: `ssh <user>@<server> 'ls -lh /var/tmp/dvd-rips/'`
