#!/bin/bash
# Paste this whole file into Oracle Cloud when creating the VM:
#   Create instance > Show advanced options > Management > "Paste cloud-init script"
# It installs everything on first boot (about 10 minutes). Progress log on the VM:
#   /var/log/leadgen-setup.log
set -euo pipefail
exec >>/var/log/leadgen-setup.log 2>&1
echo "=== leadgen first-boot setup $(date -Is) ==="

export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq git

if [[ ! -d /home/ubuntu/Lead-Generation/.git ]]; then
  git clone --quiet https://github.com/Amartya2005/Lead-Generation.git /home/ubuntu/Lead-Generation
  chown -R ubuntu:ubuntu /home/ubuntu/Lead-Generation
fi

bash /home/ubuntu/Lead-Generation/deploy/oracle/setup.sh --domain leads.focustriggerautomation.com
echo "=== done $(date -Is) ==="
