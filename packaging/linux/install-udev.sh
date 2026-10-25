#!/bin/sh
# Install the EDU09 udev rule (needs sudo) and reload udev. Replug the scope afterwards.
set -eu
here=$(dirname "$0")
sudo install -m 0644 "$here/99-velleman-edu09.rules" /etc/udev/rules.d/99-velleman-edu09.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "Installed. Unplug and replug the EDU09."
